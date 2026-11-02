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
import time
from pathlib import Path


def fetch_lyrics_from(title: str, source: str, youtube_url: str | None = None,
                      artist: str | None = None,
                      ) -> tuple[str | None, str | None, str | None, dict]:
    """Fetch lyrics from a SPECIFIC source — used by refetch when the
    default fallback chain picked a wrong-version LRC.

    `source` is one of "netease", "qq", "youtube".
    """
    info: dict[str, str] = {}
    if source == "netease":
        lrc, txt, api_info = _fetch_netease(title, artist)
    elif source == "qq":
        lrc, txt, api_info = _fetch_qq_music(title, artist)
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
        return fill_backdated_blanks(lrc), txt, source, info
    return None, None, None, {}


def by_artist(artist: str | None, credits: str) -> bool:
    """True when `credits` names the wanted singer (or none was asked for)."""
    from processors.setlist_order import artist_group
    return (not artist) or _norm_name(artist_group(artist)) in _norm_name(credits or "")


def lyrics_rank(info: dict, artist: str | None) -> tuple[bool, bool]:
    """Sort key for a source's hit: (not by the wanted artist, not exact name)."""
    from processors.setlist_order import artist_group
    by_artist = (not artist) or (
        _norm_name(artist_group(artist)) in _norm_name(info.get("artist") or ""))
    return (not by_artist, info.get("match") == "variant")


def fetch_lyrics(title: str, youtube_url: str | None = None, artist: str | None = None,
                 ) -> tuple[str | None, str | None, str | None, dict]:
    """Try all sources to get lyrics + song metadata for a title.

    Returns (lrc_text, plain_text, source_name, info).

    `info` is a dict that may contain `artist`, `lyricist`, `composer`,
    `album`. Empty if nothing was extractable. We merge API-returned
    artist/album fields with anything parseable from the LRC text itself
    (header tags + the credit lines that appear in the first ~30 s of
    most Mandarin LRCs).
    """
    # 1. NetEase, 2. QQ Music (especially for Jay Chou). Each returns only a
    # result whose name (and artist, when known) matches; an exact name
    # from either source beats a bracketed variant from the other.
    # With an artist, a hit by that artist beats one by somebody else even
    # if the other source answered first: NetEase lacks most of Jay Chou's
    # catalogue and returns a cover (說了再見 → 阿杰, 迷迭香 → 張宇's song).
    best, best_rank = None, None
    for i, (name, fetch) in enumerate((("netease", _fetch_netease), ("qq_music", _fetch_qq_music))):
        lrc, txt, api_info = fetch(title, artist)
        if not (lrc and len(lrc) > 30):
            continue
        info = dict(api_info)
        info.update({k: v for k, v in extract_lrc_metadata(lrc).items() if k not in info})
        rank = lyrics_rank(info, artist) + (i,)
        if best_rank is None or rank < best_rank:
            best, best_rank = (lrc, txt, name, info), rank
        if rank[:2] == (False, False):        # by the artist, exact name: done
            break
    if best:
        return (fill_backdated_blanks(best[0]),) + best[1:]

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


_LRC_TS = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
BACKDATE_MIN_SEC = 5.0


def fill_backdated_blanks(lrc: str) -> str:
    """Give the text back to a repeated line that some LRCs leave blank.

    QQ sometimes splits `[01:30.92][02:41.52]就这么样` into the text line at
    02:41 followed by a bare `[01:30.92]`: the first chorus then shows
    nothing (最近好嗎, 有形的翅膀). A timestamp-only line that is earlier than
    the text line just before it is that text's earlier occurrence.
    """
    out, prev_t, prev_body = [], None, None
    for line in lrc.splitlines():
        stamps = list(_LRC_TS.finditer(line))
        body = _LRC_TS.sub("", line).strip()
        if stamps:
            t = int(stamps[0].group(1)) * 60 + float(stamps[0].group(2))
            if body:
                prev_t, prev_body = t, body
            elif prev_body and t < prev_t - BACKDATE_MIN_SEC:
                line = line.rstrip() + prev_body
        out.append(line)
    return "\n".join(out) + ("\n" if lrc.endswith("\n") else "")


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

# Versions whose lyrics/timing are not the studio recording's.
_BAD_VERSION = re.compile(r"live|伴奏|伴唱|\bdj\b|remix|cover|翻唱|純音樂|纯音乐|instrumental|karaoke|"
                          r"鋼琴|钢琴|吉他|片段|铃声|鈴聲", re.I)


def _norm_name(s: str) -> str:
    try:
        from zhconv import convert
        # Simplified is the canonical form: variant traditional characters
        # (艷/豔, 裡/裏) all collapse to one simplified character.
        s = convert(s or "", "zh-cn")
    except ImportError:
        pass
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def search_queries(title: str, artist: str | None = None) -> list[str]:
    """Queries to try in order: with the singer, then the title alone, then
    the simplified-Chinese title (both sites index simplified names, and a
    variant character such as 艷/豔 or 沒/没 can hide the song).
    """
    qs = ([f"{title} {artist}"] if artist else []) + [title]
    try:
        from zhconv import convert
        simp = convert(title, "zh-cn")
        if artist:
            qs.append(f"{simp} {convert(artist, 'zh-cn')}")
        qs.append(simp)
    except ImportError:
        pass
    return list(dict.fromkeys(qs))


def pick_song(cands: list[tuple[str, str]], title: str,
              artist: str | None = None) -> tuple[int, str] | None:
    """Choose the search result that is this song: (index, "exact"|"variant").

    `cands` = [(song name, artists)] in the source's order. The name must be
    the title (a bracketed suffix like "(電影版)" makes it a "variant";
    live / 伴奏 / DJ / cover versions never count). With an artist, a result
    by that artist wins; without one, the first exact name does. Returns
    None rather than guessing: a missing LRC beats another song's lyrics
    (少年 by 梦然 got nothing because results[0] was someone else's).
    """
    from processors.setlist_order import artist_group
    t = _norm_name(title)
    a = _norm_name(artist_group(artist)) if artist else ""
    best = None
    for i, (name, arts) in enumerate(cands):
        if _BAD_VERSION.search(name):
            continue
        base = _norm_name(re.sub(r"[（(\[【].*?[)）\]】]", "", name))
        exact = _norm_name(name) == t
        if not exact and base != t:
            continue
        by_artist = bool(a) and a in _norm_name(arts)
        rank = (not by_artist, not exact, i)
        if best is None or rank < best[0]:
            best = (rank, i, "exact" if exact else "variant")
    if best is None:
        return None
    return best[1], best[2]


def _fetch_netease(title: str, artist: str | None = None) -> tuple[str | None, str | None, dict]:
    """Fetch LRC + plain text + song metadata (artist/album) from NetEase."""
    import requests

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://music.163.com/",
    }

    try:
        top, match = None, None
        for query in search_queries(title, artist):
            resp = requests.post(
                "https://music.163.com/api/search/get",
                data={"s": query, "type": 1, "limit": 10, "offset": 0},
                headers=headers, timeout=15,
            )
            if resp.status_code != 200:
                continue
            songs = resp.json().get("result", {}).get("songs", []) or []
            hit = pick_song([(x.get("name", ""),
                              "/".join(a.get("name", "") for a in x.get("artists") or []))
                             for x in songs], title, artist)
            if hit and top is None:
                top, match = songs[hit[0]], hit[1]
            # Keep looking while the hit is someone else's song: a throttled
            # or thin "title artist" query must not hand back the title-only
            # result (魚 by 怕胖團 came back as 陳綺貞's).
            if hit and by_artist(artist, "/".join(a.get("name", "") for a in songs[hit[0]].get("artists") or [])):
                top, match = songs[hit[0]], hit[1]
                break
        if top is None:
            return None, None, {}
        info: dict[str, str] = {"match": match}
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

QQ_RETRIES = 3
QQ_BACKOFF_SEC = 3.0


def _fetch_qq_music(title: str, artist: str | None = None) -> tuple[str | None, str | None, dict]:
    """Fetch LRC + plain text + song metadata (artist/album) from QQ Music."""
    import requests

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://y.qq.com/",
    }

    try:
        top, match = None, None
        for query in search_queries(title, artist):
            search_data = json.dumps({
                "search": {
                    "method": "DoSearchForQQMusicDesktop",
                    "module": "music.search.SearchCgiService",
                    "param": {
                        "search_type": 0,
                        "query": query,
                        "page_num": 1,
                        "num_per_page": 10,
                    },
                }
            })
            # QQ throttles bursts with search code 2001 and an empty list;
            # back off instead of concluding the song does not exist.
            for attempt in range(QQ_RETRIES + 1):
                resp = requests.get(
                    "https://u.y.qq.com/cgi-bin/musicu.fcg",
                    params={"data": search_data},
                    headers=headers, timeout=15,
                )
                if resp.status_code != 200 or resp.json().get("search", {}).get("code") != 2001:
                    break
                time.sleep(QQ_BACKOFF_SEC * (attempt + 1))
            if resp.status_code != 200:
                continue
            songs = (resp.json()
                     .get("search", {})
                     .get("data", {})
                     .get("body", {})
                     .get("song", {})
                     .get("list", [])) or []
            hit = pick_song([(x.get("name") or x.get("title", ""),
                              "/".join(sg.get("name", "") for sg in x.get("singer") or []))
                             for x in songs], title, artist)
            if hit and top is None:
                top, match = songs[hit[0]], hit[1]
            # Keep looking while the hit is someone else's song: a throttled
            # or thin "title artist" query must not hand back the title-only
            # result (魚 by 怕胖團 came back as 陳綺貞's).
            if hit and by_artist(artist, "/".join(sg.get("name", "") for sg in songs[hit[0]].get("singer") or [])):
                top, match = songs[hit[0]], hit[1]
                break
        if top is None:
            return None, None, {}
        songmid = top.get("mid", "")
        if not songmid:
            return None, None, {}

        info: dict[str, str] = {"match": match}
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
