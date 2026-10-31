"""
MandpopDataPipeline — Mandarin pop song dataset builder.

Add songs:
    python pipeline.py add "告白氣球"                           # One song
    python pipeline.py add "告白氣球" "小幸運" "演員"            # Multiple songs
    python pipeline.py add --from songs.txt                     # From text file (one per line)
    python pipeline.py add --from songs.json                    # From JSON
    python pipeline.py add --from repertoire.json               # StreetPerformerMaster format
    python pipeline.py add "告白氣球" --url "https://..."       # With explicit YouTube URL
    python pipeline.py add --from songs.txt --lyrics-only       # Only crawl lyrics, skip download

Manage:
    python pipeline.py status                                   # Dataset stats
    python pipeline.py list                                     # List all songs + status
    python pipeline.py list --missing-lyrics                    # Songs without lyrics
    python pipeline.py list --misaligned                        # Songs flagged by audit
    python pipeline.py lyrics                                   # Crawl lyrics for all songs
    python pipeline.py audit                                    # Layer 1 alignment check
    python pipeline.py audit --layer2                           # Add Whisper drift check
    python pipeline.py export --request req.json --output ./out # Export subset

Daily notes + consumers:
    python pipeline.py ingest                                   # notes.txt → corpus + setlist
    python pipeline.py inbox                                    # Google Doc / YouTube intake UI
    python pipeline.py serve                                    # Read-only LAN server (Mac fetch)
    python pipeline.py metrics                                  # Append YT stats time series
    python pipeline.py features                                 # Compute audio features

Environment:
    DATASET_PATH=D:\\MandpopDataset   (in .env or environment variable)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()
_raw = os.environ.get("DATASET_PATH", ".")
DATASET_PATH = Path(_raw).resolve() if _raw != "." else Path(__file__).parent.resolve()
RAW_DATASET = DATASET_PATH / "raw_dataset.jsonl"             # everything we crawled
PROCESSED_DATASET = DATASET_PATH / "processed_dataset.jsonl"  # alignment-verified subset
AUDIT_REPORT = DATASET_PATH / "audit_report.jsonl"           # latest audit details
NOTES_TXT = DATASET_PATH / "notes.txt"                       # Google Doc mirror (ingest input)
NOTES_INBOX_TXT = DATASET_PATH / "notes_inbox.txt"           # browser-extension picks (ingest input)
SETLIST_JSONL = DATASET_PATH / "setlist.jsonl"               # performance view (karaoke app reads)
SETLIST_BLOCKED_JSONL = DATASET_PATH / "setlist_blocked.jsonl"  # Doc songs not karaoke-ready yet


# ─────────────────────────────────────────────
# raw → processed promotion
# ─────────────────────────────────────────────

def is_processed(rec: dict) -> bool:
    """Whether a raw-dataset record has passed every Phase 1 gate.

    A song qualifies for the processed dataset only when ALL of these hold:
      * `status == "done"` — Demucs ran, lyrics fetched.
      * `alignment ∈ {"ok", "fixed"}` — Layer 1 (VAD) + any Layer 2
        (WhisperX) check passed. `fixed` means auto-shift was applied
        and re-verified.
      * `lyrics == "lrc"` — has timestamps (plain text alone isn't enough
        for a karaoke deliverable).
      * `m4a == True` — playable instrumental.m4a exists on disk.

    Records with verdict `fixable` (offset measured but not yet shifted),
    `drift`, `misaligned`, `wrong_version`, or `no_data` stay raw-only.
    """
    if rec.get("status") != "done":
        return False
    if rec.get("alignment") not in ("ok", "fixed"):
        return False
    if rec.get("lyrics") != "lrc":
        return False
    if not rec.get("m4a"):
        return False
    return True


def write_processed_dataset(records: dict[str, dict]) -> int:
    """Regenerate processed_dataset.jsonl from raw records.

    Each entry gives a consumer everything they need to play the song:
      * Identifiers + metadata (title, artist, lyricist, composer, album)
      * Source attribution (youtube_url)
      * Concrete file paths to the playable audio and aligned LRC,
        relative to DATASET_PATH.

    Internal audit metrics (alignment offsets, layer 2 drift, status
    machinery) are NOT included — those live in raw_dataset.jsonl and
    audit_report.jsonl.
    """
    PROCESSED_DATASET.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for title in sorted(records):
        rec = records[title]
        if not is_processed(rec):
            continue
        rows.append({
            "title": title,
            "artist": rec.get("artist"),
            "lyricist": rec.get("lyricist"),
            "composer": rec.get("composer"),
            "album": rec.get("album"),
            "youtube_url": rec.get("youtube_url"),
            "files": {
                "instrumental": f"songs/{title}/instrumental.m4a",
                "lyrics": f"songs/{title}/lyrics.lrc",
            },
        })
    PROCESSED_DATASET.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
        if rows else "",
        encoding="utf-8",
    )
    return len(rows)


def songs_dir() -> Path:
    d = DATASET_PATH / "songs"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ─────────────────────────────────────────────
# Raw dataset (JSONL — one record per line)
# ─────────────────────────────────────────────

def load_raw() -> dict[str, dict]:
    """Load raw_dataset.jsonl into {title: record} dict."""
    if not RAW_DATASET.exists():
        return {}
    records = {}
    for line in RAW_DATASET.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            records[rec["title"]] = rec
        except (json.JSONDecodeError, KeyError):
            continue
    return records


def save_raw(records: dict[str, dict]):
    """Write raw_dataset.jsonl from {title: record} dict (sorted).

    Atomic (write temp + replace) so concurrent readers never see a
    half-written file. Concurrent WRITERS can still lose updates to each
    other — keep to one writing process at a time (e.g. don't run
    `review` while a `discover --add` registration is still going).
    """
    RAW_DATASET.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for title in sorted(records):
        lines.append(json.dumps(records[title], ensure_ascii=False))
    tmp = RAW_DATASET.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(RAW_DATASET)


def upsert_raw(title: str, **fields) -> dict:
    """Add or update one song in the raw dataset. Returns the record.

    Recomputes the `processed` flag and regenerates processed_dataset.jsonl
    so its membership stays consistent with each record's alignment +
    status fields.
    """
    records = load_raw()
    rec = records.get(title, {"title": title, "added_at": datetime.now().isoformat()})
    rec.update({k: v for k, v in fields.items() if v is not None})
    rec["processed"] = is_processed(rec)
    records[title] = rec
    save_raw(records)
    write_processed_dataset(records)
    return rec


def sync_raw_from_disk():
    """Scan songs_dir and update raw_dataset.jsonl from actual files.

    Useful for bootstrapping: if songs already exist on disk but the
    raw dataset doesn't know about them yet.
    """
    records = load_raw()
    sd = songs_dir()
    updated = 0
    for d in sd.iterdir():
        if not d.is_dir():
            continue
        title = d.name
        rec = records.get(title, {"title": title, "added_at": datetime.now().isoformat()})

        # Accept lossless stems in either wav (fresh) or flac (compacted)
        # — both encode the same content, compact just transcodes between.
        has_inst = ((d / "instrumental.wav").exists() or
                    (d / "instrumental.flac").exists())
        has_m4a = (d / "instrumental.m4a").exists()
        has_lrc = (d / "lyrics.lrc").exists()
        has_txt = (d / "lyrics.txt").exists()

        new_status = "done" if has_inst else "pending"
        lyrics_status = "lrc" if has_lrc else ("txt" if has_txt else "none")

        new_processed = is_processed({**rec, "status": new_status,
                                  "lyrics": lyrics_status, "m4a": has_m4a})
        if (rec.get("status") != new_status or rec.get("lyrics") != lyrics_status
                or rec.get("m4a") != has_m4a or rec.get("processed") != new_processed):
            rec["status"] = new_status
            rec["lyrics"] = lyrics_status
            rec["m4a"] = has_m4a
            rec["processed"] = new_processed
            updated += 1

        # Pull youtube_url from metadata.json if we don't have it yet.
        meta_path = d / "metadata.json"
        if not rec.get("youtube_url") and meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("youtube_url"):
                rec["youtube_url"] = meta["youtube_url"]

        records[title] = rec

    save_raw(records)
    write_processed_dataset(records)
    return updated


# ─────────────────────────────────────────────
# Song list parsing (accepts many formats)
# ─────────────────────────────────────────────

def parse_song_input(file_path: Path) -> list[dict]:
    """Parse a song list from various file formats.

    Supported:
        .txt   — one title per line, # comments, blank lines ignored
        .json  — {"songs": [{"title": "..."}]} or [{"title": "..."}] or ["title1", ...]
        .jsonl — one JSON object per line: {"title": "...", "youtube_url": "..."}

    StreetPerformerMaster's repertoire.json is auto-detected (has "version" + "songs").
    """
    text = file_path.read_text(encoding="utf-8")
    suffix = file_path.suffix.lower()

    if suffix == ".txt":
        songs = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Support "title  url" format (tab or multi-space separated).
            parts = line.split("\t") if "\t" in line else line.split("  ", 1)
            title = parts[0].strip()
            url = parts[1].strip() if len(parts) > 1 else ""
            songs.append({"title": title, "youtube_url": url} if url else {"title": title})
        return songs

    if suffix == ".jsonl":
        songs = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                songs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return songs

    # .json
    data = json.loads(text)

    # StreetPerformerMaster repertoire.json format.
    if isinstance(data, dict) and "songs" in data:
        raw = data["songs"]
        return [
            {"title": s["title"], **{k: v for k, v in s.items() if k != "title"}}
            if isinstance(s, dict) else {"title": s}
            for s in raw
        ]

    # Plain list of strings.
    if isinstance(data, list) and data and isinstance(data[0], str):
        return [{"title": t} for t in data]

    # List of dicts.
    if isinstance(data, list):
        return data

    return []


# ─────────────────────────────────────────────
# add: Download + Demucs + lyrics
# ─────────────────────────────────────────────

def add_song(title: str, youtube_url: str | None = None,
             device: str = "cuda", lyrics_only: bool = False,
             artist: str | None = None):
    """Full pipeline: search → download → separate → lyrics → align → encode.

    Without a URL, only a video whose title contains `title` is accepted;
    otherwise nothing is downloaded and `{"no_match": [top hit titles]}`
    is returned (the record stays `pending`, so a later ingest retries).
    """
    from crawlers.youtube import search_song, download_audio, get_video_info, pick_by_title
    from crawlers.lyrics import fetch_lyrics
    from processors.separate import separate_vocals
    from processors.encode import encode_song_dir

    song_dir = songs_dir() / title
    song_dir.mkdir(exist_ok=True)
    meta_path = song_dir / "metadata.json"

    # Register in raw dataset immediately (even before processing).
    upsert_raw(title, youtube_url=youtube_url, status="pending")

    already_processed = (song_dir / "instrumental.wav").exists()

    if lyrics_only:
        if (song_dir / "lyrics.lrc").exists():
            print(f"  {title}: lyrics already exist, skipping")
            return
        print(f"  {title}...", end=" ", flush=True)
        lrc, txt, source, info = fetch_lyrics(title)
        if lrc:
            (song_dir / "lyrics.lrc").write_text(lrc, encoding="utf-8")
        if txt:
            (song_dir / "lyrics.txt").write_text(txt, encoding="utf-8")
        lyrics_status = "lrc" if lrc else ("txt" if txt else "none")
        info_clean = {k: v for k, v in info.items() if k != "title"}
        upsert_raw(title, lyrics=lyrics_status, **info_clean)
        print(f"✓ {source}" if source else "✗ not found")
        return

    if already_processed:
        print(f"  {title}: already processed, skipping")
        upsert_raw(title, status="done")
        return

    print(f"\n{'='*50}")
    print(f"Processing: {title}")
    print(f"{'='*50}")

    # Step 1: Find YouTube URL.
    if not youtube_url:
        print("  [1/6] Searching YouTube...", flush=True)
        candidates = search_song(f"{title} {artist}" if artist else title)
        best = pick_by_title(title, candidates, artist)
        if best is None:
            seen = [c.get("title", "?") for c in candidates[:3]]
            print(f"  ✗ No video titled '{title}' — typo? or pin a URL in the doc")
            for s in seen:
                print(f"      top hit: {s}")
            return {"no_match": seen}
        youtube_url = best.get("url") or best.get("webpage_url")
        print(f"  Found: {best.get('title', '?')} "
              f"({best.get('view_count', 0) or 0:,} views)")

    # Step 2: Download.
    print("  [2/6] Downloading...", flush=True)
    original_path = song_dir / "original.mp3"
    if not original_path.exists():
        try:
            download_audio(youtube_url, original_path)
        except Exception as e:
            print(f"  ✗ Download failed: {e}")
            return

    # Step 3: Demucs.
    print("  [3/6] Demucs (htdemucs_ft)...", flush=True)
    if not (song_dir / "instrumental.wav").exists():
        try:
            separate_vocals(original_path, song_dir, device=device)
        except Exception as e:
            print(f"  ✗ Separation failed: {e}")
            return

    # Step 4: Lyrics.
    print("  [4/6] Lyrics...", end=" ", flush=True)
    lrc_text, plain_text, source, song_info = fetch_lyrics(title)
    lyrics_info = {"source": source, "has_timestamps": False}
    if lrc_text:
        (song_dir / "lyrics.lrc").write_text(lrc_text, encoding="utf-8")
        lyrics_info["has_timestamps"] = True
        print(f"✓ {source} (LRC)")
    if plain_text:
        (song_dir / "lyrics.txt").write_text(plain_text, encoding="utf-8")
        if not lrc_text:
            print(f"✓ {source} (text only)")
    if not lrc_text and not plain_text:
        print("✗ not found")

    # Step 5: Alignment gate.
    alignment_status = "unchecked"
    if lrc_text:
        print("  [5/6] Alignment check...", end=" ", flush=True)
        from processors.alignment import audit_song
        report = audit_song(song_dir, run_layer2=False, auto_fix=True)
        alignment_status = report.overall
        if report.auto_fixed_offset_sec is not None:
            print(f"shifted LRC by {report.auto_fixed_offset_sec:+.2f}s → {alignment_status}")
        else:
            l1 = report.layer1
            off = f"{l1.offset_sec:+.2f}s" if l1.offset_sec is not None else "?"
            print(f"{alignment_status} (offset={off})")

    # Step 6: Encode playable instrumental.m4a (lossless wav stays as master).
    print("  [6/6] Encode m4a...", end=" ", flush=True)
    m4a_ok = False
    try:
        m4a_path = encode_song_dir(song_dir)
        if m4a_path:
            size_mb = m4a_path.stat().st_size / (1024 * 1024)
            print(f"✓ {m4a_path.name} ({size_mb:.1f} MB)")
            m4a_ok = True
        else:
            print("✗ no instrumental.wav to encode")
    except Exception as e:
        print(f"✗ {e}")
    upsert_raw(title, m4a=m4a_ok)

    # Step 5: Metadata.
    yt_info = get_video_info(youtube_url) if youtube_url else {}
    meta = {
        "title": title,
        "youtube_url": youtube_url or "",
        "youtube_title": (yt_info or {}).get("title", ""),
        "duration_sec": (yt_info or {}).get("duration"),
        "view_count": (yt_info or {}).get("view_count"),
        "uploader": (yt_info or {}).get("uploader", ""),
        "artist": song_info.get("artist"),
        "album": song_info.get("album"),
        "lyricist": song_info.get("lyricist"),
        "composer": song_info.get("composer"),
        "separation_model": "htdemucs_ft",
        "lyrics": lyrics_info,
        "files": {
            f.stem: f.name for f in song_dir.iterdir()
            if f.is_file() and f.name != "metadata.json"
        },
        "processed_at": datetime.now().isoformat(),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # Update raw dataset. `status` reflects raw files only; the alignment
    # verdict lives in its own field, and the `processed` flag (computed
    # from status + alignment) is the gate for export.
    lyrics_status = "lrc" if lyrics_info.get("has_timestamps") else (
        "txt" if plain_text else "none")
    upsert_raw(title, status="done", youtube_url=youtube_url,
               lyrics=lyrics_status, alignment=alignment_status,
               **{k: v for k, v in song_info.items() if k != "title"})
    print(f"  ✓ Done: {song_dir}  [alignment={alignment_status}]")


# ─────────────────────────────────────────────
# refetch: rebuild a song after wrong YT version or wrong LRC source
# ─────────────────────────────────────────────

def refetch_song(title: str,
                 youtube_url: str | None = None,
                 candidate: int = 1,
                 lyrics_source: str | None = None,
                 lyrics_only: bool = False,
                 artist_override: str | None = None,
                 device: str = "cuda") -> None:
    """Re-run the pipeline for one song after a bad first attempt.

    Three usage modes:

      1. Wrong YouTube version (Group B from Phase 1 audit):
            refetch_song("楓", candidate=2)
            refetch_song("楓", youtube_url="https://youtu.be/...")
         The first form searches YouTube with `"title artist"` (artist
         pulled from the raw record if present) and picks the N-th
         candidate. The second form pins an exact URL.

      2. Wrong LRC source (Group D):
            refetch_song("K歌之王", lyrics_source="qq", lyrics_only=True)
         Re-fetches lyrics from a specific source only — audio stays.

      3. Full re-do:
            refetch_song("故事", candidate=2, lyrics_source="qq")
         Combines both — replaces YT version AND re-fetches LRC.

    Always deletes the affected files first (audio for full re-do,
    LRC files for lyrics-only) and resets the relevant fields in
    raw_dataset.jsonl so the alignment gate runs fresh.
    """
    from crawlers.youtube import search_song
    from crawlers.lyrics import fetch_lyrics_from

    song_dir = songs_dir() / title
    if not song_dir.exists():
        print(f"  {title}: not in dataset — use `add` instead of `refetch`")
        return

    # Lyrics-only path: just swap LRC, keep audio.
    if lyrics_only:
        if lyrics_source is None:
            lyrics_source = "qq"   # default to QQ since refetch usually
                                   # means "NetEase gave us wrong content"
        print(f"  {title}: refetching lyrics from {lyrics_source}...",
              end=" ", flush=True)
        # Wipe existing lyrics so the gate doesn't keep using them.
        for fname in ("lyrics.lrc", "lyrics.lrc.orig", "lyrics.txt"):
            (song_dir / fname).unlink(missing_ok=True)
        yt_url = load_raw().get(title, {}).get("youtube_url")
        lrc, txt, src, info = fetch_lyrics_from(title, lyrics_source, yt_url)
        if not lrc:
            print(f"✗ {lyrics_source} returned nothing")
            upsert_raw(title, lyrics="none", alignment="no_data")
            return
        (song_dir / "lyrics.lrc").write_text(lrc, encoding="utf-8")
        if txt:
            (song_dir / "lyrics.txt").write_text(txt, encoding="utf-8")
        # Lyrics-source 'title' from the LRC `[ti:]` header would collide
        # with our positional `title` arg; drop it from the splat.
        info_clean = {k: v for k, v in info.items() if k != "title"}
        upsert_raw(title, lyrics="lrc", alignment="unchecked", **info_clean)
        print(f"✓ {src}")
        # Re-run alignment gate against existing audio.
        from processors.alignment import audit_song
        report = audit_song(song_dir, run_layer2=False, auto_fix=True)
        upsert_raw(title, alignment=report.overall)
        print(f"    → alignment={report.overall}")
        return

    # Full refetch path: replace audio + lyrics + everything.
    # Persist artist override so it survives downstream enrich passes.
    if artist_override:
        upsert_raw(title, artist=artist_override)
    # 1. Decide YouTube URL — explicit > best filtered search hit.
    if not youtube_url:
        rec = load_raw().get(title, {})
        artist = artist_override or rec.get("artist") or ""
        artist_zh = "".join(c for c in artist if "一" <= c <= "鿿")

        # Run a broad search then re-rank: YouTube's view-sorted top hit
        # often returns the artist's MOST POPULAR song, not the song we
        # asked for (e.g. searching "楓 周杰倫" returns 晴天 because 晴天
        # has many more views). So we tier results:
        #   Tier 1: YT title contains BOTH the song title AND the artist
        #   Tier 2: YT title contains the song title
        #   Tier 3: anything else
        # Within each tier the existing view-count sort applies.
        query = f"{title} {artist_zh}".strip() if artist_zh else title
        print(f"  [1/?] Searching YouTube: {query!r} (candidate #{candidate})")
        raw_candidates = search_song(query)
        if not raw_candidates:
            print(f"  ✗ No YouTube results")
            return

        def norm(s: str) -> str:
            return "".join(ch for ch in (s or "").lower() if not ch.isspace())
        title_n = norm(title)
        artist_n = norm(artist_zh)

        tier1, tier2, tier3 = [], [], []
        for c in raw_candidates:
            yt_title_n = norm(c.get("title", ""))
            has_title = title_n in yt_title_n
            has_artist = bool(artist_n) and artist_n in yt_title_n
            if has_title and has_artist:
                tier1.append(c)
            elif has_title:
                tier2.append(c)
            else:
                tier3.append(c)
        ordered = tier1 + tier2 + tier3

        if len(ordered) < candidate:
            print(f"  ✗ Only {len(ordered)} candidate(s) — none at #{candidate}")
            return
        chosen = ordered[candidate - 1]
        youtube_url = chosen.get("url") or chosen.get("webpage_url")
        tier_name = ("title+artist" if chosen in tier1
                     else "title-only" if chosen in tier2 else "fallback")
        print(f"  Picked ({tier_name}): {chosen.get('title','?')} "
              f"({chosen.get('view_count', 0):,} views)")

    # 2. Wipe everything from the bad attempt so add_song's
    #    "already_processed" check doesn't short-circuit.
    for fname in ("original.mp3", "vocals.wav", "vocals.flac",
                  "instrumental.wav", "instrumental.flac",
                  "instrumental.m4a",
                  "lyrics.lrc", "lyrics.lrc.orig", "lyrics.txt"):
        (song_dir / fname).unlink(missing_ok=True)
    upsert_raw(title, youtube_url=youtube_url, status="pending",
               alignment="unchecked", m4a=False, lyrics="none")

    # 3. Hand off to the normal pipeline.
    add_song(title, youtube_url=youtube_url, device=device,
             lyrics_only=False)
    # 4. Re-pin the artist override — add_song's enrich would have
    # overwritten it with whatever the new YT title / NetEase search
    # returned, which for songs with pulled catalogs is often a cover.
    if artist_override:
        upsert_raw(title, artist=artist_override)
    # Optionally swap the LRC source after the fact.
    if lyrics_source:
        refetch_song(title, lyrics_source=lyrics_source, lyrics_only=True,
                     device=device)


def add_many(titles: list[str] | None = None, from_file: Path | None = None,
             url: str | None = None, device: str = "cuda",
             lyrics_only: bool = False):
    """Add one or more songs. Accepts titles directly or from a file."""
    songs = []

    if from_file:
        songs = parse_song_input(from_file)
        print(f"Loaded {len(songs)} songs from {from_file}")
    elif titles:
        songs = [{"title": t} for t in titles]

    if not songs:
        print("No songs to process.")
        return

    total = len(songs)
    for i, song in enumerate(songs, 1):
        title = song.get("title") or song.get("name", "")
        song_url = song.get("youtube_url") or url
        if not title:
            continue
        print(f"\n[{i}/{total}]", flush=True)
        add_song(title, youtube_url=song_url or None,
                 device=device, lyrics_only=lyrics_only)
        if not lyrics_only:
            time.sleep(1)


# ─────────────────────────────────────────────
# lyrics: Crawl lyrics for existing songs
# ─────────────────────────────────────────────

def crawl_all_lyrics():
    """Crawl lyrics for all songs that don't have them yet."""
    from crawlers.lyrics import fetch_lyrics

    sd = songs_dir()
    song_dirs = sorted([d for d in sd.iterdir() if d.is_dir()])
    total = len(song_dirs)
    found = 0
    skipped = 0

    print(f"Crawling lyrics for {total} songs...")
    for i, d in enumerate(song_dirs, 1):
        title = d.name
        if (d / "lyrics.lrc").exists() or (d / "lyrics.txt").exists():
            skipped += 1
            continue

        print(f"  [{i}/{total}] {title}...", end=" ", flush=True)
        lrc, txt, source, info = fetch_lyrics(title)
        if lrc:
            (d / "lyrics.lrc").write_text(lrc, encoding="utf-8")
            found += 1
            print(f"✓ {source}")
        if info:
            upsert_raw(title, **{k: v for k, v in info.items() if k != "title"})
        if txt:
            (d / "lyrics.txt").write_text(txt, encoding="utf-8")
            if not lrc:
                found += 1
                print(f"✓ {source} (text)")
        if not lrc and not txt:
            print("✗")
        time.sleep(1)

    print(f"\nDone: {found} new, {skipped} skipped")


# ─────────────────────────────────────────────
# compact: shrink storage without losing data
# ─────────────────────────────────────────────

# Per-song disk savings come from two moves:
#   1. Delete `original.mp3` — regenerable from `metadata.json[youtube_url]`.
#   2. Transcode `vocals.wav` + `instrumental.wav` → FLAC. Lossless
#      (bit-exact round-trip) and ~50% smaller. Downstream consumers
#      (VocalStudio, re-Demucs, re-audit) still read them transparently.
#
# Aggressive mode (--aggressive) also drops the resulting FLAC stems —
# only do that if you're sure nothing downstream needs the stems anymore.

def compact_dataset(include_raw: bool = False, aggressive: bool = False,
                    dry_run: bool = False):
    """Shrink per-song storage while preserving stems losslessly.

    Default (~70% saving / song): delete `original.mp3`, transcode
    `vocals.wav` and `instrumental.wav` to lossless FLAC.

    `--aggressive` (~95% saving / song): also delete the resulting FLAC
    stems. Use only when you're certain no downstream app needs them.

    Only acts on songs already in the processed dataset by default; pass
    `include_raw=True` to compact failed/pending entries too.
    """
    from processors.encode import wav_to_flac

    records = load_raw()
    sd = songs_dir()
    candidates = [(t, r) for t, r in sorted(records.items())
                  if include_raw or is_processed(r)]

    total_freed = 0
    songs_touched = 0
    mp3_deleted = 0
    flac_created = 0
    stems_dropped = 0
    errors: list[tuple[str, str]] = []

    for title, _ in candidates:
        song_dir = sd / title
        if not song_dir.exists():
            continue
        before = _dir_audio_bytes(song_dir)

        # 1. Drop original.mp3 — always.
        mp3 = song_dir / "original.mp3"
        if mp3.exists():
            if not dry_run:
                mp3.unlink()
            mp3_deleted += 1

        # 2. Transcode wav stems → flac.
        for stem in ("vocals", "instrumental"):
            wav = song_dir / f"{stem}.wav"
            if not wav.exists():
                continue
            if dry_run:
                flac_created += 1
                continue
            try:
                wav_to_flac(wav, remove_wav=True)
                flac_created += 1
            except Exception as e:
                errors.append((title, f"{stem}: {e}"))

        # 3. Aggressive: drop the flacs too.
        if aggressive:
            for stem in ("vocals", "instrumental"):
                flac = song_dir / f"{stem}.flac"
                if flac.exists():
                    if not dry_run:
                        flac.unlink()
                    stems_dropped += 1

        after = _dir_audio_bytes(song_dir) if not dry_run else 0
        freed = before - after if not dry_run else _estimate_freed(song_dir, aggressive)
        if freed > 0:
            songs_touched += 1
            total_freed += freed
            print(f"  {'-' if dry_run else '✓'} {title}: "
                  f"{freed / (1024 ** 2):.1f} MB")

    action = "Would free" if dry_run else "Freed"
    scope = "processed + raw" if include_raw else "processed only"
    print(f"\n{action} {total_freed / (1024 ** 3):.2f} GB "
          f"across {songs_touched} songs, scope: {scope}")
    print(f"  original.mp3 removed: {mp3_deleted}")
    print(f"  wav→flac transcoded:  {flac_created}")
    if aggressive:
        print(f"  flac stems dropped:   {stems_dropped}")
    if errors:
        print(f"\n{len(errors)} error(s):")
        for title, msg in errors[:10]:
            print(f"  ✗ {title}: {msg}")
    if dry_run:
        print("(dry-run — re-run without --dry-run to actually compact)")


def _dir_audio_bytes(d: Path) -> int:
    """Sum of audio file sizes in a song dir (used for before/after delta)."""
    total = 0
    for ext in (".mp3", ".wav", ".flac", ".m4a"):
        for f in d.glob(f"*{ext}"):
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def _estimate_freed(d: Path, aggressive: bool) -> int:
    """Best-effort guess of freed bytes for --dry-run output."""
    total = 0
    mp3 = d / "original.mp3"
    if mp3.exists():
        total += mp3.stat().st_size
    for stem in ("vocals", "instrumental"):
        wav = d / f"{stem}.wav"
        if wav.exists():
            # FLAC typically ~50% of WAV; aggressive deletes everything.
            wav_size = wav.stat().st_size
            total += wav_size if aggressive else wav_size // 2
        elif aggressive:
            flac = d / f"{stem}.flac"
            if flac.exists():
                total += flac.stat().st_size
    return total


# ─────────────────────────────────────────────
# separate: run alternative separators (non-destructive, for A/B)
# ─────────────────────────────────────────────

# Candidate stems go to songs/{title}/separations/{method}/ instead of the
# canonical active location. The Phase 1 flow (htdemucs_ft → song root) is
# untouched. This lets you stage alternatives (BS-RoFormer, Mel-Band, MDX23C,
# ...) per song for a comparison UI / blind A/B before any promotion decision.
#
# Source mix priority:
#   1. songs/{title}/original.mp3            (uncompacted songs)
#   2. Re-download from metadata.json[youtube_url] to a temp file
#
# We DON'T sum the existing Demucs vocals+instrumental as a fallback source
# — that would feed the new separator a mix already coloured by the old one,
# biasing the comparison.

def separate_candidates(
    titles: list[str] | None = None,
    all_processed: bool = False,
    method: str = "bs_roformer",
    device: str = "cuda",
    output_format: str = "flac",
    overwrite: bool = False,
):
    """Produce candidate stems with an alternative separator.

    Writes to `songs/{title}/separations/{method}/`. Canonical active stems
    at the song root are untouched. Skips songs that already have this
    method on disk unless `overwrite=True`.
    """
    from processors.separate import separate_to_dir, list_methods

    available = list_methods()
    if method not in available:
        print(f"Unknown method '{method}'. Available:")
        for k, v in available.items():
            print(f"  {k:25s}  {v}")
        return

    if method == "htdemucs_ft":
        print("htdemucs_ft is the canonical Phase 1 method — already produced "
              "by `pipeline.py add` at song root. No `separations/htdemucs_ft/` "
              "copy needed. Use --method bs_roformer / mel_band_roformer / "
              "mdx23c to stage alternatives.")
        return

    sd = songs_dir()
    records = load_raw()

    if all_processed:
        chosen = [t for t in sorted(records) if records[t].get("processed")]
    elif titles:
        chosen = list(titles)
    else:
        print("Pass song titles or --all.")
        return

    print(f"Separation method: {method}")
    print(f"Output format:    {output_format}")
    print(f"Songs:            {len(chosen)}")
    print()

    ok = skipped = failed = no_source = 0

    for i, title in enumerate(chosen, 1):
        song_dir = sd / title
        if not song_dir.exists():
            print(f"  [{i}/{len(chosen)}] {title}: song dir missing")
            no_source += 1
            continue

        out_dir = song_dir / "separations" / method
        existing = ((out_dir / f"instrumental.{output_format}").exists() or
                    (out_dir / "instrumental.wav").exists() or
                    (out_dir / "instrumental.flac").exists())
        if existing and not overwrite:
            print(f"  [{i}/{len(chosen)}] {title}: already has {method}, skipping")
            skipped += 1
            continue

        src = _resolve_source_mix(song_dir, title, records)
        if src is None:
            print(f"  [{i}/{len(chosen)}] {title}: "
                  f"no original.mp3 and no youtube_url, skipping")
            no_source += 1
            continue

        print(f"  [{i}/{len(chosen)}] {title}", flush=True)
        try:
            meta = separate_to_dir(src["path"], out_dir, method=method,
                                   device=device, output_format=output_format)
            print(f"      ✓ {meta['runtime_sec']}s  → "
                  f"{out_dir.relative_to(DATASET_PATH)}")
            ok += 1
        except Exception as e:
            print(f"      ✗ {e}")
            failed += 1
        finally:
            if src.get("cleanup"):
                Path(src["path"]).unlink(missing_ok=True)

    print(f"\nDone: ok={ok} skipped={skipped} failed={failed} "
          f"no_source={no_source}")


def _resolve_source_mix(song_dir: Path, title: str,
                        records: dict) -> dict | None:
    """Find or fetch the original mix to feed a separator.

    Returns {"path": Path, "cleanup": bool} (cleanup=True for temp downloads)
    or None when no source can be obtained.
    """
    orig = song_dir / "original.mp3"
    if orig.exists():
        return {"path": orig, "cleanup": False}

    youtube_url = None
    meta_path = song_dir / "metadata.json"
    if meta_path.exists():
        try:
            youtube_url = json.loads(
                meta_path.read_text(encoding="utf-8")
            ).get("youtube_url")
        except Exception:
            pass
    if not youtube_url:
        youtube_url = records.get(title, {}).get("youtube_url")
    if not youtube_url:
        return None

    from crawlers.youtube import download_audio
    import tempfile
    tmp_dir = Path(tempfile.gettempdir()) / "mandpop_resep"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in title if c.isalnum() or c in "._- ")[:60] or "song"
    tmp = tmp_dir / f"{safe}_{os.getpid()}.mp3"
    print(f"      re-downloading source from YouTube...")
    download_audio(youtube_url, tmp)
    return {"path": tmp, "cleanup": True}


def show_separation_status():
    """Print which alternative separation methods exist for each song."""
    sd = songs_dir()
    records = load_raw()

    discovered: set[str] = set()
    for d in sd.iterdir():
        if not d.is_dir():
            continue
        sep_dir = d / "separations"
        if sep_dir.exists():
            for m in sep_dir.iterdir():
                if m.is_dir():
                    discovered.add(m.name)

    if not discovered:
        print("No alternative separations on disk yet.")
        print("Try:  pipeline.py separate <title> --method bs_roformer")
        return

    methods = sorted(discovered)
    titles = sorted(t for t, r in records.items() if r.get("processed"))
    counts = {m: 0 for m in methods}

    print(f"Methods on disk: {', '.join(methods)}")
    print(f"(✓ = stems present, · = missing)")
    print()
    header = "  " + " ".join(f"{m[:12]:>12s}" for m in methods) + "  title"
    print(header)
    for title in titles:
        song_dir = sd / title
        marks = []
        for m in methods:
            m_dir = song_dir / "separations" / m
            has = ((m_dir / "instrumental.wav").exists() or
                   (m_dir / "instrumental.flac").exists())
            marks.append(f"{'✓':>12s}" if has else f"{'·':>12s}")
            if has:
                counts[m] += 1
        print("  " + " ".join(marks) + "  " + title)

    print(f"\nTotals (out of {len(titles)} processed songs):")
    for m in methods:
        print(f"  {m:25s}  {counts[m]}")


def write_ab_page(titles: list[str] | None = None) -> Path | None:
    """Write exports/ab_compare.html — a local listening page for A/B.

    One player per song; buttons (or number keys) switch between the
    original mix, the active htdemucs_ft stems and every separations/{method}/
    candidate while keeping the playback position. "Blind" hides and
    shuffles the method names. Audio is referenced by relative path — the
    page only works opened from disk next to the dataset, nothing is copied.
    """
    from urllib.parse import quote

    sd = songs_dir()
    if titles:
        chosen = list(titles)
    else:
        chosen = sorted(d.name for d in sd.iterdir()
                        if (d / "separations").is_dir())
    out_dir = DATASET_PATH / "exports"
    out_dir.mkdir(exist_ok=True)

    def first(d: Path, stem: str) -> Path | None:
        for ext in ("flac", "wav", "mp3", "m4a"):
            p = d / f"{stem}.{ext}"
            if p.exists():
                return p
        return None

    songs = []
    for title in chosen:
        d = sd / title
        sources = []
        orig = d / "original.mp3"
        if orig.exists():
            sources.append(("original mix", orig))
        for label, base in [("htdemucs_ft (active)", d)] + [
                (m.name, m) for m in sorted((d / "separations").glob("*"))
                if m.is_dir()]:
            for stem in ("instrumental", "vocals"):
                p = first(base, stem)
                if p:
                    sources.append((f"{label} · {stem}", p))
        if len(sources) > 1:
            # Loudness-match: audio-separator peak-normalizes (≈ 2–3 dB quieter
            # than Demucs) and louder reads as "better" in a blind test. Attenuate
            # each source to the quietest one's RMS via <audio>.volume (≤ 1).
            # Only compare like with like: instrumentals vs instrumentals,
            # vocals vs vocals (the original mix stands alone).
            rms = {p: _rms_db(p) for _, p in sources}
            kind = {p: lbl.rsplit("· ", 1)[-1] for lbl, p in sources}
            floor = {}
            for p, v in rms.items():
                if v is not None:
                    floor[kind[p]] = min(v, floor.get(kind[p], v))
            songs.append({
                "title": title,
                "sources": [{"label": lbl,
                             "src": "../" + quote(p.relative_to(DATASET_PATH).as_posix()),
                             "vol": round(10 ** ((floor[kind[p]] - rms[p]) / 20), 3)
                                    if rms[p] is not None else 1.0}
                            for lbl, p in sources],
            })

    if not songs:
        print("No songs with candidate separations. Run `separate` first.")
        return None

    html = _AB_PAGE_TEMPLATE.replace(
        "__DATA__", json.dumps(songs, ensure_ascii=False))
    out = out_dir / "ab_compare.html"
    out.write_text(html, encoding="utf-8")
    print(f"Wrote {out} ({len(songs)} songs) — open it in a browser.")
    return out


def _rms_db(path: Path) -> float | None:
    """Whole-file RMS in dBFS (None if unreadable)."""
    try:
        if path.suffix in (".flac", ".wav"):
            import soundfile as sf
            y, _ = sf.read(str(path), always_2d=True)
        else:
            import librosa
            y, _ = librosa.load(str(path), sr=None, mono=False)
        import numpy as np
        return float(20 * np.log10(np.sqrt(np.mean(np.square(y))) + 1e-12))
    except Exception:
        return None


_AB_PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Separation A/B</title>
<style>
:root{--bg:#fafafa;--fg:#1d1d1f;--card:#fff;--line:#ddd;--acc:#2563eb;--mut:#666}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#eee;--card:#1e1e1e;--line:#333;--acc:#60a5fa;--mut:#999}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}
.song{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:0 0 14px;max-width:900px}
h2{font-size:17px;margin:0 0 8px}
.btns{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
button{border:1px solid var(--line);background:transparent;color:var(--fg);border-radius:6px;padding:4px 10px;cursor:pointer;font:inherit}
button.on{background:var(--acc);border-color:var(--acc);color:#fff}
audio{width:100%}
.hint{color:var(--mut);font-size:12px}
</style></head><body>
<p class="hint">點按鈕（或按數字鍵 1–9，作用於最後操作的那首）切換來源，播放位置會保留。各來源已做響度對齊（RMS），請勿再調整播放器音量。
<label><input type="checkbox" id="blind"> 盲測（隱藏並打亂方法名稱）</label>
<button id="reveal">揭曉</button></p>
<div id="root"></div>
<script>
const SONGS = __DATA__;
let active = null;
const root = document.getElementById('root');
function render(blind){
  root.innerHTML = '';
  SONGS.forEach(song => {
    const card = document.createElement('div'); card.className = 'song';
    root.appendChild(card);
    card.innerHTML = '<h2></h2><div class="btns"></div><audio controls preload="none"></audio>';
    card.querySelector('h2').textContent = song.title;
    const audio = card.querySelector('audio'), box = card.querySelector('.btns');
    let order = song.sources.map((s, i) => i);
    if (blind) order = order.filter(i => song.sources[i].label.includes('instrumental'))
                            .sort(() => Math.random() - .5);
    order.forEach((idx, n) => {
      const s = song.sources[idx], b = document.createElement('button');
      b.textContent = blind ? 'Option ' + String.fromCharCode(65 + n) : (n + 1) + '. ' + s.label;
      b.dataset.real = s.label;
      b.onclick = () => {
        const t = audio.currentTime, playing = !audio.paused;
        audio.src = s.src; audio.volume = s.vol; audio.currentTime = t;
        audio.addEventListener('loadedmetadata', () => { audio.currentTime = t; if (playing) audio.play(); }, {once: true});
        box.querySelectorAll('button').forEach(x => x.classList.remove('on'));
        b.classList.add('on'); active = box;
      };
      box.appendChild(b);
    });
    const firstBtn = box.querySelector('button');
    if (firstBtn) firstBtn.click();
  });
  active = null;
}
document.getElementById('blind').onchange = e => render(e.target.checked);
document.getElementById('reveal').onclick = () =>
  document.querySelectorAll('.btns button').forEach(b => { if (!b.textContent.includes(b.dataset.real)) b.textContent += ' = ' + b.dataset.real; });
document.addEventListener('keydown', e => {
  if (!active || e.target.tagName === 'INPUT') return;
  const b = active.querySelectorAll('button')[+e.key - 1]; if (b) b.click();
});
render(false);
</script></body></html>
"""


# ─────────────────────────────────────────────
# enrich: backfill artist / lyricist / composer / album from LRC + APIs
# ─────────────────────────────────────────────

def enrich_dataset(use_api: bool = True, force: bool = False):
    """Fill in artist / lyricist / composer / album for existing entries.

    Source priority for the `artist` field (each fills only what's still missing):
      1. LRC `[ar:]` header / 演唱 credit line — most reliable when present.
      2. YouTube title — official MV titles almost always start with the
         artist before the song title in brackets. Highly reliable for
         entries we crawled, and resilient to NetEase catalog removals
         (Jay Chou etc.).
      3. NetEase / QQ search-result artist — last resort. Often wrong
         for catalog-pulled artists (returns covers).

    Lyricist / composer / album come from LRC + the API result. Updates
    raw_dataset.jsonl (and processed_dataset.jsonl) and rewrites each
    metadata.json so consumers see the same fields.
    """
    from crawlers.lyrics import (extract_lrc_metadata, _fetch_netease,
                                  _fetch_qq_music,
                                  extract_artist_from_youtube_title)

    records = load_raw()
    sd = songs_dir()
    fields = ("artist", "lyricist", "composer", "album")

    counts = {"lrc": 0, "youtube": 0, "api": 0, "skipped": 0, "no_data": 0}

    titles = sorted(records)
    for i, title in enumerate(titles, 1):
        rec = records[title]
        already_has = {f for f in fields if rec.get(f)}
        if not force and "artist" in already_has and "lyricist" in already_has:
            counts["skipped"] += 1
            continue

        new_info: dict[str, str] = {}

        # 1. Parse the on-disk LRC.
        lrc_path = sd / title / "lyrics.lrc"
        if lrc_path.exists():
            lrc_meta = extract_lrc_metadata(lrc_path.read_text(encoding="utf-8"))
            if lrc_meta:
                new_info.update(lrc_meta)
                counts["lrc"] += 1

        need_artist = "artist" not in (set(new_info) | already_has)

        # 2. YouTube title parse — preferred over the API for artist.
        if need_artist:
            meta_path = sd / title / "metadata.json"
            yt_title = ""
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    yt_title = meta.get("youtube_title") or ""
                except Exception:
                    pass
            if yt_title:
                guess = extract_artist_from_youtube_title(yt_title, title)
                if guess:
                    new_info["artist"] = guess
                    need_artist = False
                    counts["youtube"] += 1

        # 3. API fallback for whatever's still missing (album especially).
        if use_api and (need_artist or "album" not in (set(new_info) | already_has)):
            _, _, api_info = _fetch_netease(title)
            if not api_info.get("artist"):
                _, _, qq = _fetch_qq_music(title)
                api_info = {**api_info, **qq}
            for k, v in api_info.items():
                if k not in new_info and not rec.get(k):
                    new_info[k] = v
            if api_info.get("artist") and "artist" not in already_has and \
               new_info.get("artist") == api_info.get("artist"):
                counts["api"] += 1
            time.sleep(0.5)

        if not new_info and not any(rec.get(f) for f in fields):
            counts["no_data"] += 1

        if not force:
            new_info = {k: v for k, v in new_info.items() if not rec.get(k)}

        if new_info:
            upsert_raw(title, **new_info)
            meta_path = sd / title / "metadata.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    meta.update(new_info)
                    meta_path.write_text(
                        json.dumps(meta, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception:
                    pass

        joined = " ".join(f"{k}={v}" for k, v in new_info.items()) or "(no change)"
        print(f"  [{i}/{len(titles)}] {title}: {joined}")

    print(f"\nDone: {counts['lrc']} from LRC, {counts['youtube']} from YouTube title, "
          f"{counts['api']} from API, {counts['skipped']} already filled, "
          f"{counts['no_data']} no data found")


# ─────────────────────────────────────────────
# encode: backfill instrumental.m4a for existing songs
# ─────────────────────────────────────────────

def encode_dataset(bitrate: str = "192k", overwrite: bool = False):
    """Encode instrumental.m4a for every song that has instrumental.wav."""
    from processors.encode import encode_song_dir

    sd = songs_dir()
    song_dirs = sorted(d for d in sd.iterdir() if d.is_dir())
    total = len(song_dirs)
    encoded = 0
    skipped = 0
    failed = 0
    no_wav = 0

    print(f"Encoding {total} songs to {bitrate} m4a...")
    for i, d in enumerate(song_dirs, 1):
        m4a = d / "instrumental.m4a"
        if m4a.exists() and not overwrite:
            skipped += 1
            continue
        try:
            result = encode_song_dir(d, bitrate=bitrate, overwrite=overwrite)
            if result is None:
                no_wav += 1
                print(f"  [{i}/{total}] {d.name}  ✗ no instrumental.wav")
            else:
                encoded += 1
                size_mb = result.stat().st_size / (1024 * 1024)
                print(f"  [{i}/{total}] {d.name}  ✓ {size_mb:.1f} MB")
        except Exception as e:
            failed += 1
            print(f"  [{i}/{total}] {d.name}  ✗ {e}")

    print(f"\nDone: encoded={encoded} skipped={skipped} "
          f"no_wav={no_wav} failed={failed}")


# ─────────────────────────────────────────────
# audit: alignment check (Layer 1 + optional Layer 2)
# ─────────────────────────────────────────────

def audit_dataset(layer2: bool = False, auto_fix: bool = True,
                  device: str = "cuda",
                  report_path: Path | None = None,
                  only_unchecked: bool = False,
                  only_titles: list[str] | None = None,
                  only_processed: bool = False):
    """Audit alignment between vocals.wav and lyrics.lrc for every song.

    Layer 1 always runs (fast, energy-based VAD vs first lyric line).
    Layer 2 (Whisper drift) runs if requested AND faster-whisper is installed.
    """
    from processors.alignment import audit_all

    sd = songs_dir()
    report_path = report_path or AUDIT_REPORT
    records = load_raw()

    counts = {"ok": 0, "fixed": 0, "fixable": 0, "drift": 0,
              "misaligned": 0, "no_data": 0, "skipped": 0}
    bad_titles: list[tuple[str, str, float | None]] = []

    titles_filter = list(only_titles) if only_titles else None
    if only_processed:
        processed_titles = [t for t, r in records.items() if r.get("processed")]
        titles_filter = (sorted(set(titles_filter) & set(processed_titles))
                         if titles_filter else processed_titles)

    n_total = len([d for d in sd.iterdir() if d.is_dir()])
    n_run = len(titles_filter) if titles_filter is not None else n_total
    print(f"Auditing {n_run} of {n_total} songs"
          f"{' (Layer 1 + Layer 2)' if layer2 else ' (Layer 1 only)'}"
          f"{' [auto-fix on]' if auto_fix else ' [report only]'}")

    with report_path.open("w", encoding="utf-8") as fp:
        for report in audit_all(sd, run_layer2=layer2, auto_fix=auto_fix,
                                whisper_device=device, progress=False,
                                titles=titles_filter):
            # Skip already-checked songs if asked.
            existing = records.get(report.title, {}).get("alignment")
            if only_unchecked and existing and existing != "unchecked":
                counts["skipped"] += 1
                continue

            counts[report.overall] = counts.get(report.overall, 0) + 1
            if report.overall not in ("ok",):
                off = report.layer1.offset_sec
                bad_titles.append((report.title, report.overall, off))

            fp.write(json.dumps(report.to_dict(), ensure_ascii=False) + "\n")

            # Update raw dataset. We only touch `alignment`; status stays
            # whatever it was (the audit isn't redoing the crawl). The
            # `processed` gate is recomputed inside upsert_raw.
            #
            # Safety for compacted songs: if a previously-verified song
            # (alignment ∈ {ok, fixed}) now reports `no_data` because its
            # intermediate audio (vocals.wav etc.) was compacted away,
            # keep the original verdict instead of demoting it. The
            # compaction is intentional — losing the verdict on every
            # audit would be a footgun.
            prior_verdict = records.get(report.title, {}).get("alignment")
            if (report.overall == "no_data" and
                    prior_verdict in ("ok", "fixed")):
                print(f"  · [compacted ] kept '{prior_verdict}' "
                      f"(vocals/instrumental missing): {report.title}")
                continue
            upsert_raw(report.title, alignment=report.overall)

            # Inline progress so long runs are tolerable.
            mark = {"ok": "✓", "fixed": "↻", "fixable": "·", "drift": "~",
                    "misaligned": "✗", "no_data": "?", "skipped": "·"}.get(
                report.overall, "?")
            off_s = (f"{report.layer1.offset_sec:+.2f}s"
                     if report.layer1.offset_sec is not None else "—")
            print(f"  {mark} [{report.overall:11s}] off={off_s:>8s}  {report.title}")

    print(f"\nReport written: {report_path}")
    print(f"Summary:")
    for k in ("ok", "fixed", "fixable", "drift", "misaligned", "no_data", "skipped"):
        if counts.get(k):
            print(f"  {k:11s} {counts[k]}")

    if bad_titles:
        print(f"\nNeed attention ({len(bad_titles)}):")
        for t, v, off in bad_titles:
            off_s = f"{off:+.2f}s" if off is not None else "—"
            print(f"  [{v:11s}] {off_s:>8s}  {t}")


# ─────────────────────────────────────────────
# status + list
# ─────────────────────────────────────────────

def show_status():
    records = load_raw()
    if not records:
        print(f"Raw dataset empty. Run: pipeline.py sync  (to scan existing files)")
        print(f"  or: pipeline.py add --from songs.txt    (to add songs)")
        return

    total = len(records)
    done = sum(1 for r in records.values() if r.get("status") == "done")
    pending = sum(1 for r in records.values() if r.get("status") == "pending")
    has_lrc = sum(1 for r in records.values() if r.get("lyrics") == "lrc")
    has_txt = sum(1 for r in records.values() if r.get("lyrics") == "txt")
    no_lyrics = sum(1 for r in records.values() if r.get("lyrics") in (None, "none"))
    align_ok = sum(1 for r in records.values() if r.get("alignment") == "ok")
    align_fixed = sum(1 for r in records.values() if r.get("alignment") == "fixed")
    align_fixable = sum(1 for r in records.values() if r.get("alignment") == "fixable")
    align_drift = sum(1 for r in records.values() if r.get("alignment") == "drift")
    align_misaligned = sum(1 for r in records.values()
                           if r.get("alignment") == "misaligned")
    align_unchecked = sum(1 for r in records.values()
                          if r.get("alignment") in (None, "unchecked"))

    has_m4a = sum(1 for r in records.values() if r.get("m4a"))
    processed = sum(1 for r in records.values() if r.get("processed"))
    raw_only = total - processed

    print(f"Dataset: {DATASET_PATH}")
    print(f"  ── Datasets ─────────────────────────────")
    print(f"  Raw dataset (raw_dataset.jsonl):       {total}")
    print(f"  Processed (processed_dataset.jsonl):   {processed}")
    print(f"    Raw-only (not yet processed):        {raw_only}")
    print(f"  ── Pipeline state ───────────────────────")
    print(f"  Status (done / pending):             {done} / {pending}")
    print(f"  Lyrics (LRC / TXT / none):           {has_lrc} / {has_txt} / {no_lyrics}")
    print(f"  Alignment ok / fixed:                {align_ok} / {align_fixed}     (→ processed)")
    print(f"  Alignment fixable:                   {align_fixable}             (run `audit` to apply)")
    print(f"  Alignment drift / misaligned:        {align_drift} / {align_misaligned}")
    print(f"  Alignment unchecked:                 {align_unchecked}")
    print(f"  M4A encoded:                         {has_m4a}")


def list_songs(missing_lyrics: bool = False, missing_audio: bool = False,
               pending: bool = False, misaligned: bool = False,
               unchecked: bool = False, processed_only: bool = False,
               raw_only: bool = False):
    records = load_raw()
    if not records:
        print("Raw dataset empty. Run: pipeline.py sync")
        return

    for title in sorted(records):
        rec = records[title]
        status = rec.get("status", "?")
        lyrics = rec.get("lyrics", "none")
        alignment = rec.get("alignment", "unchecked")

        if missing_lyrics and lyrics not in (None, "none"):
            continue
        if missing_audio and status == "done":
            continue
        if pending and status != "pending":
            continue
        if misaligned and alignment not in ("misaligned", "drift"):
            continue
        if unchecked and alignment not in (None, "unchecked"):
            continue
        if processed_only and not rec.get("processed"):
            continue
        if raw_only and rec.get("processed"):
            continue

        icon = "♫" if status == "done" else ("⏳" if status == "pending" else "?")
        lrc_icon = "LRC" if lyrics == "lrc" else ("TXT" if lyrics == "txt" else "---")
        align_icon = {"ok": "○", "fixed": "↻", "drift": "~",
                      "misaligned": "✗", "no_data": "?",
                      "unchecked": "·"}.get(alignment, "·")
        label_icon = "L" if rec.get("processed") else "·"
        print(f"  {icon} {lrc_icon} {align_icon} {label_icon}  {title}")


# ─────────────────────────────────────────────
# export
# ─────────────────────────────────────────────

def export_for_project(request_file: Path, output_dir: Path):
    """Export a subset based on a request JSON."""
    import shutil

    req = json.loads(request_file.read_text(encoding="utf-8"))

    # Accept both {"songs": [...]} and plain list.
    titles = req.get("songs", req) if isinstance(req, dict) else req
    if isinstance(titles, list) and titles and isinstance(titles[0], dict):
        titles = [s.get("title", "") for s in titles]
    wanted_files = req.get("outputs", ["instrumental.m4a", "lyrics.lrc"]) \
        if isinstance(req, dict) else ["instrumental.m4a", "lyrics.lrc"]
    # By default `export` only emits the processed subset. Setting
    # `include_raw` (or `include_misaligned` for backward compat) bypasses
    # the gate — useful for diagnostic dumps but never for consumer apps.
    include_raw = bool(req.get("include_raw") or req.get("include_misaligned")) \
        if isinstance(req, dict) else False

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sd = songs_dir()
    records = load_raw()

    copied = 0
    missing = 0
    skipped_unprocessed = 0
    for title in titles:
        if not title:
            continue
        rec = records.get(title, {})
        if not include_raw and not rec.get("processed"):
            reason = rec.get("alignment") or rec.get("status") or "no record"
            print(f"  ⚠ {title}: skipped (not processed — {reason})")
            skipped_unprocessed += 1
            continue
        src = sd / title
        if not src.exists():
            print(f"  ⚠ {title}: not in dataset")
            missing += 1
            continue
        for fname in wanted_files:
            src_file = src / fname
            if src_file.exists():
                dst = output_dir / f"{title}{src_file.suffix}"
                shutil.copy2(src_file, dst)
                copied += 1

    print(f"Exported: {copied} files, {missing} not found, "
          f"{skipped_unprocessed} skipped (not in processed dataset)")


# ─────────────────────────────────────────────
# ingest: notes.txt (Google Doc mirror) → corpus + setlist
# ─────────────────────────────────────────────

def _write_presets(song_dir: Path, entry) -> None:
    """Persist performance presets from a notes entry into metadata.json."""
    meta_path = song_dir / "metadata.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        meta = {"title": entry.title}
    meta["key_offset"] = entry.key_offset
    meta["gender_variant"] = entry.gender_variant
    meta["favorite"] = entry.favorite
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                         encoding="utf-8")


def _blocked_reason(rec: dict | None) -> str:
    """Human-readable reason a setlist song isn't karaoke-ready yet."""
    if rec is None:
        return "not in corpus yet (run ingest without --no-add)"
    if rec.get("status") != "done":
        return "not downloaded/separated yet"
    if rec.get("lyrics") != "lrc":
        return "no timestamped lyrics"
    if rec.get("alignment") not in ("ok", "fixed"):
        return f"alignment={rec.get('alignment', 'unchecked')}"
    if not rec.get("m4a"):
        return "no instrumental.m4a (run encode)"
    return "unknown"


def build_setlist_rows(entries, records: dict[str, dict],
                       has_guide=lambda title: False) -> tuple[list[dict], list]:
    """Setlist rows in menu order (favorites pinned, then by artist).

    Only karaoke-ready songs become rows (README contract) — blocked
    entries are returned for the ingest report instead. A Doc `／歌手`
    hint beats the dataset's artist, so a wrong credit is fixed in the Doc.
    """
    from processors.setlist_order import build_aliases, order_setlist

    rows, blocked = [], []
    for e in sorted((e for e in entries if e.in_setlist), key=lambda x: x.title):
        rec = records.get(e.title)
        if rec is None or not rec.get("processed"):
            blocked.append((e.title, _blocked_reason(rec)))
            continue
        files = {
            "instrumental": f"songs/{e.title}/instrumental.m4a",
            "lyrics": f"songs/{e.title}/lyrics.lrc",
        }
        if has_guide(e.title):
            files["guide_vocal"] = f"songs/{e.title}/vocals.m4a"
        rows.append({
            "title": e.title,
            "artist": e.artist_hint or rec.get("artist"),
            "key_offset": e.key_offset,
            "gender_variant": e.gender_variant,
            "favorite": e.favorite,
            "files": files,
        })
    aliases = build_aliases(r.get("artist") for r in records.values())
    return order_setlist(rows, aliases), blocked


def write_setlist(entries, records: dict[str, dict]) -> tuple[int, list]:
    """Regenerate setlist.jsonl from parsed notes entries."""
    sd = songs_dir()
    rows, blocked = build_setlist_rows(
        entries, records, has_guide=lambda t: (sd / t / "vocals.m4a").exists())
    SETLIST_JSONL.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
        if rows else "",
        encoding="utf-8",
    )
    # Doc songs that aren't karaoke-ready yet: lets a consumer tell "still
    # in the Doc, temporarily blocked" from "removed from the Doc".
    SETLIST_BLOCKED_JSONL.write_text(
        "".join(json.dumps({"title": t, "reason": why}, ensure_ascii=False) + "\n"
                for t, why in blocked),
        encoding="utf-8",
    )
    return len(rows), blocked


def ingest_notes(notes_path: Path | None = None, device: str = "cuda",
                 no_add: bool = False, dry_run: bool = False):
    """Digest the pasted Google Doc notes (see README grammar).

    Every song line feeds the research corpus (auto-`add` when missing) and
    is a setlist member: its presets are written, a guide-vocal m4a is
    encoded, and it lands in setlist.jsonl once karaoke-ready. Inbox picks
    join the setlist only when they carry a marker.
    """
    from processors.notes_ingest import parse_notes, merge_inbox, video_id

    notes_path = notes_path or NOTES_TXT
    if not notes_path.exists() and not NOTES_INBOX_TXT.exists():
        print(f"Notes file not found: {notes_path}")
        print("Paste your Google Doc song notes into it (grammar in README).")
        return

    entries, warnings = ([], [])
    if notes_path.exists():
        entries, warnings = parse_notes(notes_path.read_text(encoding="utf-8"),
                                        listed=True)
    if NOTES_INBOX_TXT.exists():
        inbox, inbox_warn = parse_notes(NOTES_INBOX_TXT.read_text(encoding="utf-8"))
        entries, merge_warn = merge_inbox(entries, inbox)
        warnings += [f"{NOTES_INBOX_TXT.name}: {w}" for w in inbox_warn + merge_warn]
        print(f"Merged {len(inbox)} songs from {NOTES_INBOX_TXT.name}")
    setlist_entries = [e for e in entries if e.in_setlist]
    print(f"Parsed {len(entries)} songs from {notes_path.name} "
          f"({len(setlist_entries)} in setlist, "
          f"{sum(e.has_markers for e in entries)} with performance markers)")
    for w in warnings:
        print(f"  ⚠ {w}")

    records = load_raw()
    # Discovered-but-unprocessed titles sit in raw as `pending`; a song the
    # maintainer actually wrote down must not wait behind the review queue.
    missing = [e for e in entries
               if e.title not in records or records[e.title].get("status") == "pending"]
    # A pinned URL that differs from the downloaded video = "use THIS version".
    repinned = [e for e in entries
                if e.youtube_url and e.title in records
                and records[e.title].get("status") == "done"
                and video_id(e.youtube_url) != video_id(records[e.title].get("youtube_url"))]
    no_match = []   # (entry, top-hit titles) — probable typos, reported at the end

    if dry_run:
        print(f"\n[dry-run] would add {len(missing)} new songs to corpus:")
        for e in missing:
            print(f"  + {e.title}" + (f" ({e.artist_hint})" if e.artist_hint else ""))
        n_ready = sum(1 for e in setlist_entries
                      if records.get(e.title, {}).get("processed"))
        print(f"[dry-run] would refetch {len(repinned)} songs pinned to a new video:")
        for e in repinned:
            print(f"  ↻ {e.title} → {e.youtube_url}")
        print(f"[dry-run] setlist: {n_ready}/{len(setlist_entries)} ready")
        return

    # 1. Corpus: every noted song gets added (the research layer is
    #    the primary purpose — singability is irrelevant here).
    if missing and no_add:
        print(f"\n--no-add: skipping {len(missing)} songs not yet in corpus:")
        for e in missing:
            print(f"  · {e.title}")
    elif missing:
        print(f"\nAdding {len(missing)} new songs to the research corpus...")
        for i, e in enumerate(missing, 1):
            print(f"\n[{i}/{len(missing)}]", flush=True)
            if e.artist_hint:               # survives downstream enrich
                upsert_raw(e.title, artist=e.artist_hint)
            r = add_song(e.title, youtube_url=e.youtube_url, device=device,
                         artist=e.artist_hint)
            if isinstance(r, dict) and "no_match" in r:
                no_match.append((e, r["no_match"]))
        records = load_raw()
    if repinned and not no_add:
        print(f"\nRefetching {len(repinned)} songs pinned to a different video...")
        for i, e in enumerate(repinned, 1):
            print(f"\n[{i}/{len(repinned)}] {e.title} → {e.youtube_url}", flush=True)
            refetch_song(e.title, youtube_url=e.youtube_url, device=device)
        records = load_raw()

    # 2. Setlist presets → metadata.json (+ mirror into raw record so
    #    downstream tools can filter without opening every metadata file).
    sd = songs_dir()
    for e in setlist_entries:
        song_dir = sd / e.title
        if song_dir.exists():
            _write_presets(song_dir, e)
        upsert_raw(e.title, key_offset=e.key_offset,
                   gender_variant=e.gender_variant, favorite=e.favorite)
    records = load_raw()

    # 3. Guide vocal (導唱) m4a for ready setlist songs.
    from processors.encode import encode_m4a, find_stem
    encoded = 0
    for e in setlist_entries:
        rec = records.get(e.title)
        if not (rec and rec.get("processed")):
            continue
        song_dir = sd / e.title
        if (song_dir / "vocals.m4a").exists():
            continue
        src = find_stem(song_dir, "vocals")
        if src is not None:
            try:
                encode_m4a(src, song_dir / "vocals.m4a")
                encoded += 1
            except RuntimeError as err:
                print(f"  ⚠ guide vocal encode failed for {e.title}: {err}")
    if encoded:
        print(f"Encoded {encoded} guide-vocal m4a files")

    # 4. Regenerate setlist.jsonl + report.
    n_ready, blocked = write_setlist(entries, records)
    print(f"\nsetlist.jsonl: {n_ready} songs ready "
          f"({len(setlist_entries)} in the setlist)")
    if blocked:
        print("Blocked (in notes, not karaoke-ready yet):")
        for title, reason in blocked:
            print(f"  ✗ {title}: {reason}")
    if no_match:
        print("\nNo YouTube video carries these exact titles — typo in the doc?"
              " (fix the title, or add `/artist` or a YouTube URL to the line)")
        for e, seen in no_match:
            print(f"  ? line {e.lineno}: {e.title}")
            for s in seen:
                print(f"      top hit: {s}")


def sync_doc(url: str | None = None, device: str = "cuda", no_add: bool = False,
             dry_run: bool = False, run_then: bool = True) -> int:
    """Fetch the tracked Google Doc → notes.txt → ingest → post-ingest hook.

    The non-interactive twin of the inbox page's 「抓最新」+「套用」, for
    schedulers. Returns a process exit code; on a fetch error notes.txt is
    left untouched.
    """
    from processors.inbox_http import (diff_notes, fetch_doc_text,
                                       resolve_doc_url, write_notes)

    try:
        doc_url = resolve_doc_url(url)
        text = fetch_doc_text(doc_url)
    except (ValueError, PermissionError, OSError) as err:
        print(f"Doc sync failed: {err}")
        return 1
    old = NOTES_TXT.read_text(encoding="utf-8") if NOTES_TXT.exists() else ""
    d = diff_notes(old, text)
    print(f"Google Doc: {d['total']} songs — +{len(d['added'])} "
          f"−{len(d['removed'])} ~{len(d['changed'])} vs {NOTES_TXT.name}")
    for a in d["added"]:
        print(f"  + {a['line']}")
    for r in d["removed"]:
        print(f"  − {r['line']}")
    for c in d["changed"]:
        print(f"  ~ {c['before']}  →  {c['after']}")
    if dry_run:
        print(f"[dry-run] {NOTES_TXT.name} not written")
        return 0
    if d["added"] or d["removed"] or d["changed"]:
        write_notes(text)
        print(f"Wrote {NOTES_TXT.name} (previous copy → {NOTES_TXT.name}.bak)")

    ingest_notes(device=device, no_add=no_add)

    then_cmd = os.environ.get("INBOX_POST_INGEST_CMD") if run_then else None
    if then_cmd:
        print(f"\n══ post-ingest: {then_cmd}", flush=True)
        # Absolute DATASET_PATH: a relative "." would resolve in the consumer.
        env = dict(os.environ, DATASET_PATH=str(DATASET_PATH))
        return subprocess.run(then_cmd, shell=True, env=env).returncode
    return 0


# ─────────────────────────────────────────────
# metrics: append YT popularity snapshots (time series)
# ─────────────────────────────────────────────

def metrics_dataset(only_titles: list[str] | None = None,
                    processed_only: bool = False,
                    sleep_sec: float = 0.5):
    """Append one popularity snapshot per song to songs/{t}/metrics.jsonl.

    Append-only by design (README spec): every crawl adds one line per
    song; old lines are never rewritten — the history IS the data.
    Defaults to the whole raw corpus (research wants breadth); the only
    requirements are a youtube_url and a song directory on disk.
    """
    from datetime import timezone
    from crawlers.youtube import get_video_info

    records = load_raw()
    if only_titles:
        titles = [t for t in only_titles if t in records]
        for t in only_titles:
            if t not in records:
                print(f"  ⚠ not in raw dataset: {t}")
    else:
        titles = sorted(t for t in records
                        if not processed_only or records[t].get("processed"))

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sd = songs_dir()
    ok = skipped = failed = 0
    for i, title in enumerate(titles, 1):
        rec = records[title]
        url = rec.get("youtube_url")
        song_dir = sd / title
        if not url or not song_dir.exists():
            skipped += 1
            continue
        info = get_video_info(url)
        if not info:
            print(f"  [{i}/{len(titles)}] {title}: fetch failed")
            failed += 1
            continue
        row = {
            "ts": ts,
            "source": "youtube",
            "video_id": info.get("id"),
            "views": info.get("view_count"),
            "likes": info.get("like_count"),
            "comments": info.get("comment_count"),
        }
        with (song_dir / "metrics.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        views = row["views"]
        print(f"  [{i}/{len(titles)}] {title}: "
              f"{views:,} views" if views is not None else
              f"  [{i}/{len(titles)}] {title}: recorded (views hidden)")
        ok += 1
        time.sleep(sleep_sec)

    print(f"\nmetrics: {ok} recorded, {skipped} skipped (no URL/dir), "
          f"{failed} failed")


# ─────────────────────────────────────────────
# features: local audio analysis → features.json
# ─────────────────────────────────────────────

def features_dataset(only_titles: list[str] | None = None,
                     overwrite: bool = False):
    """Compute features.json for songs with stems on disk (README spec)."""
    from processors.features import write_features

    records = load_raw()
    if only_titles:
        titles = [t for t in only_titles if t in records]
    else:
        titles = sorted(t for t in records if records[t].get("processed"))

    sd = songs_dir()
    ok = skipped = failed = 0
    for i, title in enumerate(titles, 1):
        song_dir = sd / title
        out_path = song_dir / "features.json"
        if out_path.exists() and not overwrite:
            skipped += 1
            continue
        try:
            doc = write_features(song_dir, overwrite=overwrite)
        except RuntimeError as err:      # librosa missing → fail fast, clearly
            print(f"✗ {err}")
            return
        except Exception as err:
            print(f"  [{i}/{len(titles)}] {title}: ✗ {err}")
            failed += 1
            continue
        if doc is None:
            print(f"  [{i}/{len(titles)}] {title}: no stems on disk, skipping")
            skipped += 1
            continue
        print(f"  [{i}/{len(titles)}] {title}: "
              f"{doc['bpm']:.0f} bpm, {doc['key']} {doc['mode']}, "
              f"{doc['loudness_lufs']} LUFS, vocal {doc['vocal_ratio']:.0%}")
        ok += 1

    print(f"\nfeatures: {ok} computed, {skipped} skipped, {failed} failed")


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="MandpopDataPipeline — Mandarin pop song dataset builder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # add
    p_add = sub.add_parser("add", help="Add songs to the dataset",
                           formatter_class=argparse.RawDescriptionHelpFormatter,
                           epilog=(
                               "examples:\n"
                               '  pipeline.py add "告白氣球"\n'
                               '  pipeline.py add "告白氣球" "小幸運" "演員"\n'
                               '  pipeline.py add --from songs.txt\n'
                               '  pipeline.py add --from repertoire.json\n'
                               '  pipeline.py add --from songs.txt --lyrics-only'
                           ))
    p_add.add_argument("titles", nargs="*", help="Song title(s)")
    p_add.add_argument("--from", dest="from_file", type=Path,
                       help="Load songs from file (.txt / .json / .jsonl)")
    p_add.add_argument("--url", help="YouTube URL (only for single song)")
    p_add.add_argument("--device", default="cuda", choices=["cpu", "cuda"])
    p_add.add_argument("--lyrics-only", action="store_true",
                       help="Only crawl lyrics, skip download + separation")
    p_add.add_argument("--accepted", action="store_true",
                       help="Process every pending song accepted in `pipeline.py review`")

    # list
    p_list = sub.add_parser("list", help="List songs in the dataset")
    p_list.add_argument("--missing-lyrics", action="store_true",
                        help="Only show songs without lyrics")
    p_list.add_argument("--missing-audio", action="store_true",
                        help="Only show songs without instrumental")
    p_list.add_argument("--pending", action="store_true",
                        help="Only show songs not yet processed")
    p_list.add_argument("--misaligned", action="store_true",
                        help="Only show songs flagged as misaligned or drifting")
    p_list.add_argument("--unchecked", action="store_true",
                        help="Only show songs whose alignment was never audited")
    p_list.add_argument("--processed", action="store_true",
                        help="Only show songs in the processed dataset")
    p_list.add_argument("--raw-only", action="store_true",
                        help="Only show raw songs that did not promote to processed")

    # refetch
    p_refetch = sub.add_parser(
        "refetch",
        help="Re-fetch a song after wrong YouTube version or wrong LRC source",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  pipeline.py refetch 楓                          # next-best YT (uses artist hint)\n"
            "  pipeline.py refetch 楓 --candidate 2            # explicit 2nd-best YT\n"
            "  pipeline.py refetch 楓 --url https://youtu.be/X # pin exact URL\n"
            "  pipeline.py refetch K歌之王 --lyrics-only --source qq\n"
        ))
    p_refetch.add_argument("title", help="Song title (must already be in raw_dataset)")
    p_refetch.add_argument("--url", help="Exact YouTube URL — bypasses search")
    p_refetch.add_argument("--candidate", type=int, default=1,
                           help="N-th search result to pick (1 = best)")
    p_refetch.add_argument("--lyrics-only", action="store_true",
                           help="Don't re-download audio, just swap the LRC")
    p_refetch.add_argument("--source", choices=("netease", "qq", "youtube"),
                           help="Force a specific LRC source")
    p_refetch.add_argument("--artist",
                           help="Pin the artist name (used in YT search query AND "
                                "preserved through enrich, so cover-artist results don't overwrite it)")
    p_refetch.add_argument("--device", default="cuda", choices=["cpu", "cuda"])

    # lyrics
    sub.add_parser("lyrics", help="Crawl lyrics for all songs without them")

    # status
    sub.add_parser("status", help="Show dataset statistics")

    # discover
    p_disc = sub.add_parser("discover",
                            help="Discover popular songs from music charts / playlists")
    p_disc.add_argument("--era",
                        help="Era: 2000s / 2010s / 2020s / classic / ktv / all")
    p_disc.add_argument("--chart",
                        help="NetEase chart: hot / new / original / surge")
    p_disc.add_argument("--playlist", type=int,
                        help="Specific NetEase playlist ID")
    p_disc.add_argument("--artists", action="store_true",
                        help="Similar-song expansion: hot-50 of every artist "
                             "already in the corpus")
    p_disc.add_argument("--add", action="store_true",
                        help="Also register discovered songs in raw dataset (pending)")
    p_disc.add_argument("--process", action="store_true",
                        help="Also download + separate + lyrics (slow)")
    p_disc.add_argument("--output", "-o", type=Path,
                        help="Save song list to file (default: print to stdout)")
    p_disc.add_argument("--device", default="cuda", choices=["cpu", "cuda"])

    # sync
    sub.add_parser("sync", help="Scan disk and update raw_dataset.jsonl from existing files")

    # compact
    p_compact = sub.add_parser(
        "compact",
        help="Shrink disk: drop original.mp3 + transcode wav stems to lossless FLAC",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  pipeline.py compact --dry-run       # preview what would change\n"
            "  pipeline.py compact                 # default: lossless ~70% saving / song\n"
            "  pipeline.py compact --aggressive    # also drop FLAC stems (~95% saving)\n"
            "  pipeline.py compact --include-raw   # also compact raw-only entries\n"
        ))
    p_compact.add_argument("--dry-run", action="store_true",
                           help="Show what would change without touching disk")
    p_compact.add_argument("--aggressive", action="store_true",
                           help="Also delete FLAC stems (only if no downstream app needs them)")
    p_compact.add_argument("--include-raw", action="store_true",
                           help="Also compact raw-only entries (default: processed only)")

    # separate
    p_sep = sub.add_parser(
        "separate",
        help="Run alternative separators (non-destructive, for A/B comparison)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            '  pipeline.py separate "告白氣球" --method bs_roformer\n'
            '  pipeline.py separate "告白氣球" "小幸運" --method mel_band_roformer\n'
            '  pipeline.py separate --all --method bs_roformer\n'
            '  pipeline.py separate --list-methods\n'
            '  pipeline.py separate --status\n'
            '  pipeline.py separate --ab-page\n'
            '  pipeline.py separate --review --methods karaoke_aufr33,karaoke_becruily,karaoke_gabox_v2\n'
            "\n"
            "Outputs land in songs/{title}/separations/{method}/ — the canonical\n"
            "active stems at song root are untouched. Source mix comes from\n"
            "original.mp3 if present, otherwise re-downloads from youtube_url.\n"
        ))
    p_sep.add_argument("titles", nargs="*",
                       help="Song titles to separate (omit with --all)")
    p_sep.add_argument("--all", action="store_true",
                       help="Process every song in the processed dataset")
    p_sep.add_argument("--method", default="bs_roformer",
                       help="Separator method id (see --list-methods)")
    p_sep.add_argument("--device", default="cuda", choices=["cpu", "cuda"])
    p_sep.add_argument("--format", default="flac", choices=["wav", "flac"],
                       dest="output_format",
                       help="Candidate stems output format (default: flac, ~half WAV size)")
    p_sep.add_argument("--overwrite", action="store_true",
                       help="Re-run even if separations/{method}/ already exists")
    p_sep.add_argument("--list-methods", action="store_true",
                       help="List available separator methods and exit")
    p_sep.add_argument("--status", action="store_true",
                       help="Show which methods have been tried per song")
    p_sep.add_argument("--ab-page", action="store_true",
                       help="Write exports/ab_compare.html, a local listening page "
                            "(given titles, or every song with separations/)")
    p_sep.add_argument("--review", action="store_true",
                       help="Write exports/ab_review.html: auto-detected disagreement "
                            "hotspots only, looped, blind, one-key voting")
    p_sep.add_argument("--methods", default=None,
                       help="Comma-separated methods to compare in --review "
                            "(default: every candidate on disk)")
    p_sep.add_argument("--segments", type=int, default=5,
                       help="Hotspot segments per song for --review (default: 5)")

    # enrich
    p_enrich = sub.add_parser(
        "enrich",
        help="Backfill artist / lyricist / composer / album from LRC + APIs")
    p_enrich.add_argument("--no-api", action="store_true",
                          help="Don't re-query NetEase / QQ for missing artist info")
    p_enrich.add_argument("--force", action="store_true",
                          help="Overwrite fields even when already populated")

    # encode
    p_enc = sub.add_parser(
        "encode",
        help="Encode instrumental.wav → instrumental.m4a (playable deliverable)")
    p_enc.add_argument("--bitrate", default="192k",
                       help="AAC bitrate (default 192k)")
    p_enc.add_argument("--overwrite", action="store_true",
                       help="Re-encode even if instrumental.m4a already exists")

    # audit
    p_audit = sub.add_parser(
        "audit",
        help="Check alignment between vocals.wav and lyrics.lrc",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  pipeline.py audit                     # Layer 1 only, auto-fix offsets\n"
            "  pipeline.py audit --layer2            # Layer 1 + Whisper drift check\n"
            "  pipeline.py audit --no-fix            # Report only, no LRC modification\n"
            "  pipeline.py audit --only-unchecked    # Skip songs already audited\n"
        ))
    p_audit.add_argument("--layer2", action="store_true",
                         help="Also run Whisper-based drift check (slower, needs faster-whisper)")
    p_audit.add_argument("--no-fix", action="store_true",
                         help="Do not auto-shift LRC for fixable offsets, just report")
    p_audit.add_argument("--device", default="cuda", choices=["cpu", "cuda"],
                         help="Device for Whisper in Layer 2")
    p_audit.add_argument("--report", type=Path, default=None,
                         help="Output JSONL path (default: ./audit_report.jsonl)")
    p_audit.add_argument("--only-unchecked", action="store_true",
                         help="Skip songs whose alignment field is already set")
    p_audit.add_argument("--only", nargs="+", metavar="TITLE",
                         help="Run audit on these specific song titles only")
    p_audit.add_argument("--only-processed", action="store_true",
                         help="Run audit only on songs already in the processed subset")

    # export
    p_export = sub.add_parser("export", help="Export subset for a consumer project")
    p_export.add_argument("--request", type=Path, required=True,
                          help="JSON file listing songs + desired outputs")
    p_export.add_argument("--output", type=Path, required=True,
                          help="Output directory")

    # ingest
    p_ing = sub.add_parser(
        "ingest",
        help="Digest notes.txt (Google Doc mirror) → corpus + setlist",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "notes.txt grammar (one song per line, see README):\n"
            "  聽海 -6 女版 !        corpus + setlist: key -6, female, pinned\n"
            "  夜曲                  corpus + setlist, default presets\n"
            "\n"
            "examples:\n"
            "  pipeline.py ingest                # full run (adds missing songs)\n"
            "  pipeline.py ingest --dry-run      # parse + report, write nothing\n"
            "  pipeline.py ingest --no-add       # update presets/setlist only\n"
            "  pipeline.py ingest --from-doc     # fetch the tracked Google Doc first\n"
        ))
    p_ing.add_argument("--file", type=Path, default=None,
                       help="Notes file (default: notes.txt at dataset root)")
    p_ing.add_argument("--dry-run", action="store_true",
                       help="Parse and report only, write nothing")
    p_ing.add_argument("--no-add", action="store_true",
                       help="Skip auto-adding songs missing from the corpus")
    p_ing.add_argument("--device", default="cuda", choices=["cpu", "cuda"])
    p_ing.add_argument("--from-doc", nargs="?", const="", default=None, metavar="URL",
                       help="Fetch the Google Doc into notes.txt first, then run "
                            "INBOX_POST_INGEST_CMD. Default doc: NOTES_DOC_URL, else "
                            "inbox_config.json; a URL given here becomes the tracked doc")
    p_ing.add_argument("--no-then", action="store_true",
                       help="With --from-doc: skip INBOX_POST_INGEST_CMD")

    # serve
    p_serve = sub.add_parser(
        "serve",
        help="Read-only LAN server: on-demand per-song fetch (Mac recording)")
    p_serve.add_argument("--host", default="0.0.0.0",
                         help="Bind address (default 0.0.0.0 = all interfaces)")
    p_serve.add_argument("--port", type=int, default=8765)

    # review
    p_rev = sub.add_parser(
        "review",
        help="Swipe-review UI: triage discovered pending songs (←reject →accept)")
    p_rev.add_argument("--host", default="127.0.0.1",
                       help="Bind address (default localhost; use 0.0.0.0 for LAN)")
    p_rev.add_argument("--port", type=int, default=8766)
    p_rev.add_argument("--bulk-accept", action="store_true",
                       help="No UI: accept every unreviewed pending song with "
                            ">= --min-sources corroborating discoveries, then exit")
    p_rev.add_argument("--min-sources", type=int, default=3,
                       help="Corroboration threshold for --bulk-accept (default 3)")
    p_rev.add_argument("--dry-run", action="store_true",
                       help="With --bulk-accept: report what would be accepted")

    # inbox
    p_inb = sub.add_parser(
        "inbox",
        help="Song inbox UI: Google Doc paste/fetch + YouTube extension → ingest")
    p_inb.add_argument("--host", default="127.0.0.1",
                       help="Bind address (default localhost)")
    p_inb.add_argument("--port", type=int, default=8767)
    p_inb.add_argument("--then", default=None, metavar="CMD",
                       help="Shell command to run after each successful ingest "
                            "(default: $INBOX_POST_INGEST_CMD), e.g. a phone sync")

    # metrics
    p_met = sub.add_parser(
        "metrics",
        help="Append a YT popularity snapshot to each song's metrics.jsonl")
    p_met.add_argument("--only", nargs="+", metavar="TITLE",
                       help="Only these titles (default: whole raw corpus)")
    p_met.add_argument("--processed-only", action="store_true",
                       help="Only songs in the processed subset")

    # features
    p_feat = sub.add_parser(
        "features",
        help="Compute audio features (bpm/key/loudness/sections) → features.json")
    p_feat.add_argument("--only", nargs="+", metavar="TITLE",
                        help="Only these titles (default: processed subset)")
    p_feat.add_argument("--overwrite", action="store_true",
                        help="Recompute even when features.json exists")

    args = parser.parse_args()

    if args.command == "add":
        if args.accepted:
            # Process songs the maintainer accepted in `pipeline.py review`.
            # The review UI cached each song's previewed youtube_url, so
            # what was heard is exactly what gets downloaded.
            records = load_raw()
            accepted = [(t, r.get("youtube_url")) for t, r in sorted(records.items())
                        if r.get("review") == "accepted" and r.get("status") != "done"]
            if not accepted:
                print("No accepted pending songs. Run `pipeline.py review` first.")
                return
            print(f"Processing {len(accepted)} accepted songs...")
            for i, (t, url) in enumerate(accepted, 1):
                print(f"\n[{i}/{len(accepted)}]", flush=True)
                add_song(t, youtube_url=url, device=args.device)
                time.sleep(1)
            return
        if not args.titles and not args.from_file:
            p_add.print_help()
            return
        add_many(titles=args.titles or None, from_file=args.from_file,
                 url=args.url, device=args.device,
                 lyrics_only=args.lyrics_only)
    elif args.command == "list":
        list_songs(missing_lyrics=args.missing_lyrics,
                   missing_audio=args.missing_audio,
                   pending=args.pending,
                   misaligned=args.misaligned,
                   unchecked=args.unchecked,
                   processed_only=args.processed,
                   raw_only=args.raw_only)
    elif args.command == "audit":
        audit_dataset(layer2=args.layer2, auto_fix=not args.no_fix,
                      device=args.device, report_path=args.report,
                      only_unchecked=args.only_unchecked,
                      only_titles=args.only,
                      only_processed=args.only_processed)
    elif args.command == "encode":
        encode_dataset(bitrate=args.bitrate, overwrite=args.overwrite)
    elif args.command == "separate":
        from processors.separate import list_methods
        if args.list_methods:
            print("Available separator methods:")
            for k, v in list_methods().items():
                marker = " (canonical, run via `add`)" if k == "htdemucs_ft" else ""
                print(f"  {k:25s}  {v}{marker}")
            return
        if args.status:
            show_separation_status()
            return
        if args.ab_page:
            write_ab_page(args.titles or None)
            return
        if args.review:
            from processors.ab_review import write_review_page
            titles = args.titles or sorted(
                d.name for d in songs_dir().iterdir() if (d / "separations").is_dir())
            methods = args.methods.split(",") if args.methods else None
            write_review_page(DATASET_PATH, titles, methods, n=args.segments)
            return
        if not args.titles and not args.all:
            p_sep.print_help()
            return
        separate_candidates(
            titles=args.titles or None,
            all_processed=args.all,
            method=args.method,
            device=args.device,
            output_format=args.output_format,
            overwrite=args.overwrite,
        )
    elif args.command == "enrich":
        enrich_dataset(use_api=not args.no_api, force=args.force)
    elif args.command == "compact":
        compact_dataset(include_raw=args.include_raw,
                        aggressive=args.aggressive,
                        dry_run=args.dry_run)
    elif args.command == "refetch":
        refetch_song(args.title, youtube_url=args.url,
                     candidate=args.candidate,
                     lyrics_source=args.source,
                     lyrics_only=args.lyrics_only,
                     artist_override=args.artist,
                     device=args.device)
    elif args.command == "lyrics":
        crawl_all_lyrics()
    elif args.command == "discover":
        from crawlers.charts import discover_songs, discover_artist_songs
        if args.artists:
            done_artists = sorted({r.get("artist") for r in load_raw().values()
                                   if r.get("status") == "done" and r.get("artist")})
            print(f"Expanding from {len(done_artists)} corpus artists...")
            songs = discover_artist_songs(done_artists)
        else:
            songs = discover_songs(era=args.era, playlist_id=args.playlist,
                                   chart=args.chart)
        # NetEase returns Simplified Chinese; the corpus is Traditional.
        # Normalize titles/artists so 演员 dedups against 演員 and new
        # song folders stay consistent with the existing dataset.
        try:
            from zhconv import convert as _zh
            for s in songs:
                s["title"] = _zh(s["title"], "zh-hant")
                if s.get("artist"):
                    s["artist"] = _zh(s["artist"], "zh-hant")
            # Re-dedup after conversion (简/繁 variants may collapse).
            seen: set[str] = set()
            songs = [s for s in songs
                     if not (s["title"] in seen or seen.add(s["title"]))]
        except ImportError:
            print("  ⚠ zhconv not installed — skipping 简→繁 normalization")
        # Deduplicate against raw dataset.
        existing = load_raw()
        new_songs = [s for s in songs if s["title"] not in existing]
        print(f"\nDiscovered {len(songs)} songs, {len(new_songs)} new "
              f"({len(songs) - len(new_songs)} already in raw dataset)")

        if args.output:
            args.output.write_text(
                "\n".join(s["title"] for s in new_songs) + "\n",
                encoding="utf-8",
            )
            print(f"Saved {len(new_songs)} new titles to {args.output}")

        if args.add or args.process:
            # Provenance (README `discovered_from`): record where/when each
            # song surfaced. Existing songs get the new sighting appended
            # too — "this song ALSO charted here" is research signal.
            # Single load → merge all → single save: registering thousands
            # of songs via per-song upsert_raw is O(n²) on the jsonl file.
            seen_at = datetime.now().strftime("%Y-%m-%d")
            kind = "artist_hot" if args.artists else "playlist"
            records_all = load_raw()
            for s in songs:
                rec = records_all.get(s["title"])
                if rec is None:
                    rec = {"title": s["title"],
                           "added_at": datetime.now().isoformat(),
                           "status": "pending"}
                    if s.get("artist"):
                        rec["artist"] = s["artist"]
                    if s.get("netease_id"):
                        rec["netease_id"] = s["netease_id"]
                    records_all[s["title"]] = rec
                prov = {
                    "provider": "netease",
                    "kind": kind,
                    "name": s.get("playlist_name") or "",
                    "rank": s.get("rank"),
                    "seen_at": seen_at,
                }
                prior = rec.get("discovered_from", [])
                if not any(p.get("provider") == prov["provider"]
                           and p.get("name") == prov["name"] for p in prior):
                    prior.append(prov)
                rec["discovered_from"] = prior
                rec["processed"] = is_processed(rec)
            save_raw(records_all)
            write_processed_dataset(records_all)
            print(f"Registered {len(new_songs)} new songs as pending "
                  f"(+ provenance on {len(songs) - len(new_songs)} existing)")

        if args.process:
            print(f"\nProcessing {len(new_songs)} songs...")
            for i, s in enumerate(new_songs, 1):
                print(f"\n[{i}/{len(new_songs)}]", flush=True)
                add_song(s["title"], device=args.device)
                time.sleep(1)

        if not args.output and not args.add and not args.process:
            # Just print the new songs.
            for s in new_songs:
                print(f"  {s['title']}  ({s.get('artist', '')})")

    elif args.command == "sync":
        updated = sync_raw_from_disk()
        records = load_raw()
        print(f"Synced: {updated} records updated, {len(records)} total in raw dataset")
    elif args.command == "status":
        show_status()
    elif args.command == "export":
        export_for_project(args.request, args.output)
    elif args.command == "ingest":
        if args.from_doc is not None:
            sys.exit(sync_doc(args.from_doc or None, device=args.device,
                              no_add=args.no_add, dry_run=args.dry_run,
                              run_then=not args.no_then))
        ingest_notes(notes_path=args.file, device=args.device,
                     no_add=args.no_add, dry_run=args.dry_run)
    elif args.command == "serve":
        from processors.serve_http import serve_dataset
        serve_dataset(DATASET_PATH, host=args.host, port=args.port)
    elif args.command == "review":
        if args.bulk_accept:
            records = load_raw()
            hits = [t for t, r in sorted(records.items())
                    if r.get("status") == "pending" and not r.get("review")
                    and len(r.get("discovered_from", [])) >= args.min_sources]
            print(f"{len(hits)} unreviewed pending songs with "
                  f">= {args.min_sources} sources")
            if args.dry_run:
                for t in hits[:40]:
                    print(f"  {t}")
                if len(hits) > 40:
                    print(f"  ... and {len(hits) - 40} more")
                return
            now = datetime.now().isoformat()
            for t in hits:
                records[t]["review"] = "accepted"
                records[t]["reviewed_at"] = now
            save_raw(records)
            print(f"Accepted {len(hits)} songs — run `pipeline.py add --accepted` "
                  f"to process them.")
            return
        from processors.review_http import serve_review
        serve_review(host=args.host, port=args.port)
    elif args.command == "inbox":
        from processors.inbox_http import serve_inbox
        serve_inbox(host=args.host, port=args.port, then_cmd=args.then)
    elif args.command == "metrics":
        metrics_dataset(only_titles=args.only,
                        processed_only=args.processed_only)
    elif args.command == "features":
        features_dataset(only_titles=args.only, overwrite=args.overwrite)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
