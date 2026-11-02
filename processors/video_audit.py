"""Audit which YouTube video each library song was built from.

Many early songs were downloaded before `pick_by_title` existed, so some
instrumentals come from live / talent-show / cover / KTV uploads, or even
the wrong song. For every done song this module compares the current video
with a fresh search and classifies it:

    ok       current video is the best candidate (or close enough)
    auto     clearly wrong AND safely replaceable: the current upload is
             live / cover / KTV / unavailable / not this song, and the best
             candidate is an official studio upload by the same artist with
             many more views → pinned automatically (notes_inbox.txt)
    suggest  a more popular official upload exists, but replacing is a
             judgement call (unknown artist, same-name songs, …) → shown in
             the inbox page's library editor for a human to pick
    pinned   the Doc or the inbox already pins a video — never touched

"Best candidate" = among uploads whose title names this song (`pick_by_title`
rules), studio before live, official before fan, then most views.

Results go to `$DATASET_PATH/video_audit.jsonl` (one row per song, rewritten
per run; songs checked within `--max-age` days are reused).
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

from crawlers.youtube import (_LIVE, _OFFICIAL, _norm, get_video_info, search_song,
                              title_names_song)
from processors.notes_ingest import video_id
from processors.setlist_order import artist_group

# Uploads that are the right song but not a usable source recording.
_NOISE = re.compile(r"cover|翻唱|piano|鋼琴|钢琴|伴奏|伴唱|karaoke|\bktv\b|instrumental|"
                    r"純音樂|纯音乐|remix|dj\b|抖音|女聲版|男聲版|女声版|男声版", re.I)
AUTO_VIEW_RATIO = 3.0      # best must have ≥ 3× the views to replace automatically
SUGGEST_VIEW_RATIO = 2.0   # … ≥ 2× to be worth suggesting at all
# "Another singer's version" as the ONLY problem relies on the credited
# artist, which can itself be wrong (離人: 林志炫 and 張學友 both have one),
# so the original must not be far less popular than ours.
OTHER_SINGER_MIN_RATIO = 0.5


def _brief(v: dict | None) -> dict | None:
    if not v:
        return None
    vid = v.get("id") or video_id(v.get("webpage_url") or v.get("url"))
    return {"id": vid, "url": f"https://youtu.be/{vid}" if vid else None,
            "title": v.get("title") or "",
            "channel": v.get("channel") or v.get("uploader") or "",
            "views": int(v.get("view_count") or v.get("views") or 0),
            "duration": int(v.get("duration") or 0)}


def _problems(title: str, v: dict, lyrics_end: float | None = None) -> list[str]:
    """Why an upload is not a good source recording ([] = looks fine)."""
    out = []
    # A cut / special edit can't carry the full lyrics (錯位時空: a 2:08
    # Olympics edit for a 3-minute LRC).
    if lyrics_end and v["duration"] and v["duration"] < lyrics_end * 0.9:
        out.append("shorter than the lyrics (edited version)")
    if _LIVE.search(v["title"]):
        out.append("live / talent show / fan video")
    if _NOISE.search(v["title"]):
        out.append("cover / KTV / remix")
    # Strict slot match OR plain containment ("周杰倫 晴天 (高音質)" is fine);
    # only a title that doesn't mention the song at all is suspicious.
    if not title_names_song(title, v["title"]) and _norm(title) not in _norm(v["title"]):
        out.append("title does not name this song")
    return out


def _official(v: dict) -> bool:
    """Official upload: says so in the title, or a YouTube Music "Artist -
    Topic" channel (the album master itself — the cleanest separation source)."""
    return bool(_OFFICIAL.search(v["title"])) or v["channel"].rstrip().endswith("- Topic")


def _by_artist(artist: str, v: dict) -> bool:
    a = _norm(artist)
    return bool(a) and a in _norm(v["title"] + " " + v["channel"])


def rank_candidates(title: str, candidates: list[dict]) -> list[dict]:
    """Uploads naming this song, best first (studio, official, views)."""
    seen, out = set(), []
    for c in map(_brief, candidates):
        if not c or not c["id"] or c["id"] in seen:
            continue
        seen.add(c["id"])
        if title_names_song(title, c["title"]) and not _NOISE.search(c["title"]):
            out.append(c)
    return sorted(out, key=lambda c: (bool(_LIVE.search(c["title"])),
                                      not _official(c), -c["views"]))


def classify(title: str, artist: str | None, current: dict | None,
             candidates: list[dict], pinned: bool = False,
             lyrics_end: float | None = None) -> dict:
    """Verdict for one song. `current`/`candidates` are yt-dlp info dicts."""
    cur = _brief(current)
    ranked = rank_candidates(title, candidates)
    best = ranked[0] if ranked else None
    row = {"verdict": "ok", "reasons": [], "current": cur, "best": best,
           "candidates": ranked[:6]}
    if pinned:
        row["verdict"] = "pinned"
        return row
    if best is None:
        row["reasons"] = ["no candidate names this song"]
        return row
    if cur is not None and cur["id"] == best["id"]:
        return row

    problems = ["video unavailable"] if cur is None else _problems(title, cur, lyrics_end)
    cur_views = cur["views"] if cur else 0
    ratio = best["views"] / max(cur_views, 1)
    best_clean = not _LIVE.search(best["title"]) and _official(best)
    group = artist_group(artist)
    same_artist = bool(group) and _by_artist(group, best)
    # Another singer's version (cover lyric videos often out-view the
    # original): the credited artist is on the best upload but not on ours.
    other_singer = same_artist and cur is not None and not _by_artist(group, cur)
    if other_singer:
        problems.append("another singer's version")

    only_other_singer = problems == ["another singer's version"]
    replace_cover = other_singer and (not only_other_singer or ratio >= OTHER_SINGER_MIN_RATIO)
    if problems and best_clean and same_artist and (ratio >= AUTO_VIEW_RATIO or replace_cover):
        row["verdict"] = "auto"
    elif problems or ratio >= SUGGEST_VIEW_RATIO:
        row["verdict"] = "suggest"
        if problems and not same_artist:
            problems.append("artist unconfirmed — pick by hand")
    if row["verdict"] != "ok":
        row["reasons"] = problems + [f"best has {ratio:.1f}× the views"]
    return row


def audit_song(title: str, rec: dict, pinned: bool) -> dict:
    """Network I/O for one song: current video info + two searches."""
    artist = rec.get("artist_hint") or rec.get("artist")
    group = artist_group(artist)
    current = get_video_info(rec["youtube_url"]) if rec.get("youtube_url") else None
    cands = search_song(f"{title} {group}".strip(), max_results=12)
    cands += search_song(title, max_results=12)
    row = classify(title, artist, current, cands, pinned=pinned,
                   lyrics_end=_last_sung_line(title))
    row.update(title=title, artist=group or artist or "",
               checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return row


def reclassify(row: dict, pinned: bool) -> dict:
    """Re-run `classify` on a stored row (no network) — for rule changes."""
    cands = row.get("candidates") or []
    new = classify(row["title"], row.get("artist"), row.get("current"), cands, pinned=pinned)
    return {**row, **new}


def _last_sung_line(title: str) -> float | None:
    """Start of the last sung LRC line (credits excluded), in seconds."""
    import pipeline
    from processors.lrc_align import parse_starts
    lrc = pipeline.songs_dir() / title / "lyrics.lrc"
    if not lrc.exists():
        return None
    starts = parse_starts(lrc.read_text(encoding="utf-8", errors="replace"))
    return float(starts[-1]) if len(starts) else None


def load_audit(path) -> dict[str, dict]:
    rows = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                rows[r["title"]] = r
    return rows


def write_audit(path, rows: dict[str, dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                            for r in sorted(rows.values(), key=lambda r: r["title"])),
                    encoding="utf-8")


def is_fresh(row: dict, max_age_days: float) -> bool:
    try:
        at = datetime.fromisoformat(row["checked_at"])
    except (KeyError, ValueError):
        return False
    return (datetime.now(timezone.utc) - at).total_seconds() < max_age_days * 86400


def sleep_between():
    time.sleep(0.5)      # be gentle with YouTube
