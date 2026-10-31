"""YouTube audio/video download via yt-dlp."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


def search_song(title: str, max_results: int = 15) -> list[dict]:
    """Search YouTube for a song, return candidate videos sorted by view count.

    Filters out covers, piano versions, karaoke, Shorts, and long mixes.
    """
    cmd = [
        "yt-dlp", f"ytsearch{max_results}:{title} 官方 MV",
        "--dump-json", "--flat-playlist", "--no-warnings", "--skip-download",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        return []

    videos = []
    noise = ["cover", "翻唱", "piano", "鋼琴", "伴奏", "karaoke", "ktv", "instrumental"]
    for line in result.stdout.strip().splitlines():
        try:
            v = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = (v.get("title") or "").lower()
        dur = v.get("duration") or 0
        if any(n in t for n in noise):
            continue
        if dur < 60 or dur > 600:
            continue
        videos.append(v)

    videos.sort(key=lambda v: v.get("view_count") or 0, reverse=True)
    return videos


def _norm(s: str) -> str:
    """Compare form: traditional, lowercase, letters/digits/CJK only."""
    try:
        from zhconv import convert
        s = convert(s or "", "zh-tw")
    except ImportError:
        pass
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


# Song-name slots in typical MV titles: 《X》 【X】 「X」 『X』 "X"
_BRACKETED = re.compile(r"[《【「『\[\"“]([^》】」』\]\"”]+)[》】」』\]\"”]")


def _cjk(s: str) -> str:
    return "".join(ch for ch in _norm(s) if "一" <= ch <= "鿿")


# Live / talent-show / fan-video uploads: right song, wrong recording.
_LIVE = re.compile(r"live|現場|现场|演唱會|演唱会|純享|纯享|好聲音|好声音|我們的歌|我们的歌|"
                   r"天賜的聲音|天赐的声音|夢想的聲音|梦想的声音|聲林之王|公演|獻唱|献唱|"
                   r"花絮|精華|精华|vietsub|fmv|\bep\.?\s?\d", re.I)
_OFFICIAL = re.compile(r"official|官方|\bmv\b", re.I)


def pick_by_title(title: str, candidates: list[dict],
                  artist: str | None = None) -> dict | None:
    """Pick the candidate that is actually this song, or None.

    Doc lines are exact song names, so some name slot of the video title
    (《X》/【X】/「X」/"X", an ` - `/`|` segment, or the whole title) must
    equal the song title — simplified/traditional and punctuation
    insensitive; a CJK title may carry an English subtitle in the slot.
    Mere containment is not enough (「手沖咖啡」 vlogs, 「牙套」 dentists).
    Ranking, view order breaking ties: studio before live/talent-show,
    official before fan uploads, artist hit before miss. YouTube's top hit
    by views is often another song by the same artist, hence no fallback.
    """
    t, tc = _norm(title), _cjk(title)
    a = _norm(artist) if artist else ""
    if not t:
        return None
    ranked = []
    for i, c in enumerate(candidates):
        yt = c.get("title") or ""
        slots = [yt] + _BRACKETED.findall(yt) + re.split(r"\s[-–|/｜]\s|[-–|/｜]", yt)
        if not any(_norm(s) == t or (tc and _cjk(s) == tc) for s in slots):
            continue
        has_artist = bool(a) and a in _norm(yt + " " + (c.get("channel") or c.get("uploader") or ""))
        ranked.append((bool(_LIVE.search(yt)), not _OFFICIAL.search(yt), not has_artist, i, c))
    return min(ranked, key=lambda r: r[:4])[4] if ranked else None


def download_audio(url: str, output_path: Path) -> Path:
    """Download best audio from a YouTube URL as mp3."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "yt-dlp", "-f", "bestaudio",
        "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "-o", str(output_path),
        "--no-playlist",
        url,
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"Download failed: {result.stderr[:300]}")

    # yt-dlp may append extension
    for candidate in [output_path, output_path.with_suffix(".mp3")]:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"Downloaded file not found at {output_path}")


def get_video_info(url: str) -> dict | None:
    """Fetch video metadata without downloading."""
    result = subprocess.run(
        ["yt-dlp", "--dump-json", "--no-download", url],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
