"""Lyrics crawlers — multiple sources with fallback chain.

Priority:
    1. NetEase Cloud Music API (free, has LRC timestamps, best for Mandarin)
    2. QQ Music API (free, has LRC, covers Jay Chou + NetEase gaps)
    3. YouTube auto-generated subtitles (has timestamps, last resort)

Usage:
    from crawlers.lyrics import fetch_lyrics
    lrc, txt, source = fetch_lyrics("告白氣球")
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


def fetch_lyrics_from(title: str, source: str, youtube_url: str | None = None,
                      ) -> tuple[str | None, str | None, str | None, dict]:
    """Fetch lyrics from a SPECIFIC source — used by refetch when the
    default fallback chain picked a wrong-version LRC.

    `source` is one of "netease", "qq", "youtube".
    """
    info: dict[str, str] = {}
    if source == "netease":
        lrc, txt, api_info = _fetch_netease(title)
    elif source == "qq":
        lrc, txt, api_info = _fetch_qq_music(title)
    elif source == "youtube":
        if not youtube_url:
            return None, None, None, {}
        yt_lrc = fetch_lyrics_youtube_subs(youtube_url)
        if yt_lrc and len(yt_lrc) > 30:
            txt = "\n".join(
                re.sub(r"\[[\d:.]+\]", "", l).strip()
                for l in yt_lrc.splitlines() if l.strip()
            )
            return yt_lrc, txt, "youtube_subs", {}
        return None, None, None, {}
    else:
        raise ValueError(f"unknown source {source!r}; "
                         f"use 'netease', 'qq', or 'youtube'")

    if lrc and len(lrc) > 30:
        info.update(api_info)
        info.update({k: v for k, v in extract_lrc_metadata(lrc).items()
                     if k not in info})
        return lrc, txt, source, info
    return None, None, None, {}


def fetch_lyrics(title: str, youtube_url: str | None = None,
                 ) -> tuple[str | None, str | None, str | None, dict]:
    """Try all sources to get lyrics + song metadata for a title.

    Returns (lrc_text, plain_text, source_name, info).

    `info` is a dict that may contain `artist`, `lyricist`, `composer`,
    `album`. Empty if nothing was extractable. We merge API-returned
    artist/album fields with anything parseable from the LRC text itself
    (header tags + the credit lines that appear in the first ~30 s of
    most Mandarin LRCs).
    """
    info: dict[str, str] = {}

    # 1. NetEase Cloud Music.
    lrc, txt, api_info = _fetch_netease(title)
    if lrc and len(lrc) > 30:
        info.update(api_info)
        info.update({k: v for k, v in extract_lrc_metadata(lrc).items()
                     if k not in info})
        return lrc, txt, "netease", info

    # 2. QQ Music (especially for Jay Chou).
    lrc, txt, api_info = _fetch_qq_music(title)
    if lrc and len(lrc) > 30:
        info.update(api_info)
        info.update({k: v for k, v in extract_lrc_metadata(lrc).items()
                     if k not in info})
        return lrc, txt, "qq_music", info

    # 3. YouTube auto-subs.
    if youtube_url:
        yt_lrc = fetch_lyrics_youtube_subs(youtube_url)
        if yt_lrc and len(yt_lrc) > 30:
            txt = "\n".join(
                re.sub(r"\[[\d:.]+\]", "", l).strip()
                for l in yt_lrc.splitlines() if l.strip()
            )
            return yt_lrc, txt, "youtube_subs", {}

    return None, None, None, {}


def _lrc_to_plain(lrc: str) -> str:
    """Strip [mm:ss.xx] timestamps from LRC to get plain text."""
    lines = [re.sub(r"\[[\d:.]+\]", "", l).strip() for l in lrc.splitlines()]
    return "\n".join(l for l in lines if l)


# ─────────────────────────────────────────────
# LRC metadata extraction (artist / lyricist / composer / album)
# ─────────────────────────────────────────────

# LRC header tags. `[ar:周杰伦]`, `[ti:晴天]`, `[al:十一月的萧邦]`, `[by:某某]`.
_HEADER_RE = re.compile(r"^\s*\[(ti|ar|al|by|au)\s*:\s*([^\]]+)\]\s*$",
                        re.IGNORECASE)

# Inline credit lines that appear with timestamps in the first ~30 s of
# many Mandarin LRCs: `[00:00.00] 作词 : 方文山`. Keyword may be Chinese
# (作词/作詞/词/作曲/曲/编曲/編曲/制作人/監製/演唱/原唱) or English
# (Lyrics by, Composed by, etc.).
_CREDIT_LINE_RES = [
    (re.compile(r"作\s*詞\s*[:：]\s*([^\[\]/]+?)(?:\s*$|\s*[/／、])"), "lyricist"),
    (re.compile(r"作\s*词\s*[:：]\s*([^\[\]/]+?)(?:\s*$|\s*[/／、])"), "lyricist"),
    (re.compile(r"^\s*詞\s*[:：]\s*([^\[\]/]+?)(?:\s*$|\s*[/／、])"), "lyricist"),
    (re.compile(r"^\s*词\s*[:：]\s*([^\[\]/]+?)(?:\s*$|\s*[/／、])"), "lyricist"),
    (re.compile(r"作\s*曲\s*[:：]\s*([^\[\]/]+?)(?:\s*$|\s*[/／、])"), "composer"),
    (re.compile(r"^\s*曲\s*[:：]\s*([^\[\]/]+?)(?:\s*$|\s*[/／、])"), "composer"),
    (re.compile(r"演\s*唱\s*[:：]\s*([^\[\]/]+?)(?:\s*$|\s*[/／、])"), "artist"),
    (re.compile(r"原\s*唱\s*[:：]\s*([^\[\]/]+?)(?:\s*$|\s*[/／、])"), "artist"),
    (re.compile(r"Lyrics?\s+by\s*[:：]?\s*([^\[\]/]+?)(?:\s*$|\s*[/／、])", re.IGNORECASE), "lyricist"),
    (re.compile(r"Composed\s+by\s*[:：]?\s*([^\[\]/]+?)(?:\s*$|\s*[/／、])", re.IGNORECASE), "composer"),
]

# Strip leading [mm:ss.xx] timestamps from a line so the credit regexes see plain text.
_TS_STRIP = re.compile(r"^\s*(?:\[\d{1,3}:\d{2}(?:\.\d{1,3})?\]\s*)+")


_YT_TITLE_BRACKETS = re.compile(r"[\[\【「『《\(]")


def extract_artist_from_youtube_title(yt_title: str, song_title: str = "") -> str | None:
    """Heuristic: pull the artist out of an official MV's YouTube title.

    Most Mandarin official MVs follow one of these layouts:
      "周杰倫 Jay Chou【楓 Maple】-Official Music Video"
      "陳奕迅 Eason Chan《K歌之王(國)》[Official MV]"
      "蘇打綠 sodagreen -【小情歌】 Official Music Video"
      "MAYDAY五月天 [頑固] - Official MV"

    The artist always sits before the first bracket / dash that wraps
    the song title. We split on those, take the leading chunk, and
    strip "MV", "Official", trailing dashes, etc.
    """
    if not yt_title:
        return None
    # Some MVs put a tag in the first bracket pair (純享 / Live / 高畫質) and
    # the artist name only appears after that — skip until the first chunk
    # that doesn't look like a tag.
    _PREFIX_TAGS = {"純享", "纯享", "Live", "live", "MV", "高清",
                    "高畫質", "高画质", "HD", "HQ", "完整版"}
    parts = re.split(r"[\[\【「『《\(\)』」】\]]", yt_title)
    head = ""
    for p in parts:
        p = p.strip()
        if not p or p in _PREFIX_TAGS:
            continue
        head = p
        break
    if not head:
        return None
    head = re.split(r"\s[-–—]\s", head, 1)[0]
    head = re.sub(r"(?i)\s*(official\s+(music\s+)?video|official\s+mv|mv|HD|HQ|高画质|高畫質)\s*$",
                  "", head).strip()
    head = head.strip(" -–—:|/／·,，、")
    if not head:
        return None
    # If song title accidentally got included, strip it.
    if song_title:
        head = head.replace(song_title, "").strip(" -–—:|/／·,，、")
    if 1 <= len(head) <= 40:
        return head or None
    return None


def extract_lrc_metadata(lrc_text: str) -> dict:
    """Pull artist / lyricist / composer / album / title from an LRC.

    Reads two kinds of signal:

      * `[ar:]` / `[al:]` / `[ti:]` / `[by:]` header tags.
      * Timestamped credit lines that appear in the first ~30 s, e.g.
        `[00:00.00] 作词 : 方文山` or `[00:01.00]Composed by: 周杰倫`.

    Returns a dict with keys present only when found — never overwrites
    keys with None. The first hit wins (LRC headers come before credit
    lines and tend to be authoritative).
    """
    info: dict[str, str] = {}
    if not lrc_text:
        return info

    tag_to_field = {"ar": "artist", "al": "album", "ti": "title", "by": "by"}

    for raw in lrc_text.splitlines():
        m = _HEADER_RE.match(raw)
        if m:
            tag = m.group(1).lower()
            value = m.group(2).strip()
            field = tag_to_field.get(tag)
            if field and value and field not in info:
                info[field] = value
            continue

        # Strip LRC timestamps; only inspect early credit lines.
        body = _TS_STRIP.sub("", raw).strip()
        if not body:
            continue
        for pattern, field in _CREDIT_LINE_RES:
            m = pattern.search(body)
            if m and field not in info:
                value = m.group(1).strip().rstrip("）)")
                if value and value.lower() not in ("by", "lyric", "composer"):
                    info[field] = value
                break

    return info


# ─────────────────────────────────────────────
# Source 1: NetEase Cloud Music
# ─────────────────────────────────────────────

def _fetch_netease(title: str) -> tuple[str | None, str | None, dict]:
    """Fetch LRC + plain text + song metadata (artist/album) from NetEase."""
    import requests

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://music.163.com/",
    }

    try:
        resp = requests.post(
            "https://music.163.com/api/search/get",
            data={"s": title, "type": 1, "limit": 5, "offset": 0},
            headers=headers, timeout=15,
        )
        if resp.status_code != 200:
            return None, None, {}
        songs = resp.json().get("result", {}).get("songs", [])
        if not songs:
            return None, None, {}

        top = songs[0]
        info: dict[str, str] = {}
        artists = top.get("artists") or []
        if artists:
            info["artist"] = "/".join(
                a["name"] for a in artists if isinstance(a, dict) and a.get("name")
            )
        album = top.get("album") or {}
        if isinstance(album, dict) and album.get("name"):
            info["album"] = album["name"]

        resp2 = requests.get(
            f"https://music.163.com/api/song/lyric?id={top['id']}&lv=1&tv=1",
            headers=headers, timeout=15,
        )
        if resp2.status_code != 200:
            return None, None, info
        lrc = resp2.json().get("lrc", {}).get("lyric")
        return ((lrc, _lrc_to_plain(lrc), info) if lrc else (None, None, info))
    except Exception as e:
        print(f"  NetEase error: {e}")
        return None, None, {}


# ─────────────────────────────────────────────
# Source 2: QQ Music
# ─────────────────────────────────────────────

def _fetch_qq_music(title: str) -> tuple[str | None, str | None, dict]:
    """Fetch LRC + plain text + song metadata (artist/album) from QQ Music."""
    import requests

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://y.qq.com/",
    }

    try:
        search_data = json.dumps({
            "search": {
                "method": "DoSearchForQQMusicDesktop",
                "module": "music.search.SearchCgiService",
                "param": {
                    "search_type": 0,
                    "query": title,
                    "page_num": 1,
                    "num_per_page": 5,
                },
            }
        })
        resp = requests.get(
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
            params={"data": search_data},
            headers=headers, timeout=15,
        )
        if resp.status_code != 200:
            return None, None, {}
        songs = (resp.json()
                 .get("search", {})
                 .get("data", {})
                 .get("body", {})
                 .get("song", {})
                 .get("list", []))
        if not songs:
            return None, None, {}

        top = songs[0]
        songmid = top.get("mid", "")
        if not songmid:
            return None, None, {}

        info: dict[str, str] = {}
        singers = top.get("singer") or []
        if singers:
            info["artist"] = "/".join(
                s["name"] for s in singers if isinstance(s, dict) and s.get("name")
            )
        album = top.get("album") or {}
        if isinstance(album, dict) and album.get("name"):
            info["album"] = album["name"]

        resp2 = requests.get(
            "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
            params={"songmid": songmid, "format": "json", "nobase64": 1},
            headers=headers, timeout=15,
        )
        if resp2.status_code != 200:
            return None, None, info
        lrc = resp2.json().get("lyric", "")
        if not lrc:
            return None, None, info
        return lrc, _lrc_to_plain(lrc), info
    except Exception as e:
        print(f"  QQ Music error: {e}")
        return None, None, {}


# ─────────────────────────────────────────────
# Source 3: YouTube auto-subs
# ─────────────────────────────────────────────

def fetch_lyrics_youtube_subs(youtube_url: str) -> str | None:
    if not youtube_url:
        return None
    try:
        result = subprocess.run(
            ["yt-dlp", "--write-auto-subs", "--sub-lang", "zh",
             "--skip-download", "--sub-format", "vtt",
             "-o", "%(id)s", youtube_url],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=30,
        )
        for vtt in Path(".").glob("*.zh*.vtt"):
            text = vtt.read_text(encoding="utf-8", errors="replace")
            vtt.unlink()
            return _vtt_to_lrc(text)
        return None
    except Exception:
        return None


def _vtt_to_lrc(vtt_text: str) -> str:
    lines = []
    time_re = re.compile(r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})")
    current_time = ""
    for line in vtt_text.splitlines():
        m = time_re.match(line)
        if m:
            h, mi, s, ms = int(m[1]), int(m[2]), int(m[3]), int(m[4])
            total_min = h * 60 + mi
            current_time = f"[{total_min:02d}:{s:02d}.{ms // 10:02d}]"
        elif line.strip() and current_time and not line.startswith(("WEBVTT", "Kind:")):
            clean = re.sub(r"<[^>]+>", "", line).strip()
            if clean:
                lines.append(f"{current_time}{clean}")
                current_time = ""
    deduped = []
    for ln in lines:
        text_part = ln.split("]", 1)[-1] if "]" in ln else ln
        if not deduped or text_part != deduped[-1].split("]", 1)[-1]:
            deduped.append(ln)
    return "\n".join(deduped)
