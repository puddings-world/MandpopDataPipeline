"""Discover popular Mandarin pop songs from music charts and playlists.

Sources:
    - NetEase Cloud Music playlists (curated by era / genre / mood)
    - NetEase Cloud Music top charts

Usage:
    from crawlers.charts import discover_songs
    songs = discover_songs(era="2000s")

CLI:
    python -m crawlers.charts --era all
    python -m crawlers.charts --era 2010s
    python -m crawlers.charts --playlist 12345678
    python -m crawlers.charts --chart hot
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://music.163.com/",
}

# Search keywords per era — used to DYNAMICALLY find playlists (not hardcoded IDs).
ERA_SEARCH_KEYWORDS = {
    "2000s":   ["华语经典 2000", "2000年代 华语金曲", "千禧年 华语", "00年代 经典 华语"],
    "2010s":   ["华语经典 2010", "2010年代 华语金曲", "2010s 华语热门"],
    "2020s":   ["华语新歌 2020", "2020年代 华语", "2023 华语热歌", "2024 华语新歌"],
    "classic": ["华语经典金曲", "华语经典老歌 500", "80 90 华语经典",
                "90年代 华语经典", "华语怀旧金曲"],
    "ktv":     ["KTV必点 华语", "KTV 华语金曲", "KTV热唱 华语"],
    "love":    ["华语情歌 经典", "华语抒情", "失恋 华语 经典"],
    "rock":    ["华语摇滚 经典", "华语独立"],
    "folk":    ["华语民谣 经典", "校园民谣"],
    "rnb":     ["华语R&B 经典", "华语 节奏蓝调"],
    "movie":   ["影视金曲 华语", "电影主题曲 华语经典"],
    "duet":    ["对唱情歌 华语", "男女对唱 经典"],
}

# How wide each era search goes: playlists examined per keyword.
PLAYLISTS_PER_KEYWORD = 3


def _fetch_track_details(track_ids: list[int], batch_size: int = 200) -> list[dict]:
    """Fetch track details in batches from NetEase song detail API."""
    all_tracks = []
    for i in range(0, len(track_ids), batch_size):
        batch = track_ids[i:i + batch_size]
        c_param = json.dumps([{"id": tid} for tid in batch])
        try:
            resp = requests.post(
                "https://music.163.com/api/v3/song/detail",
                data={"c": c_param},
                headers=HEADERS, timeout=20,
            )
            if resp.status_code == 200:
                songs = resp.json().get("songs", [])
                all_tracks.extend(songs)
        except Exception:
            pass
        if i + batch_size < len(track_ids):
            time.sleep(0.5)
    return all_tracks


def fetch_playlist(playlist_id: int) -> list[dict]:
    """Fetch all songs from a NetEase playlist.

    Uses the v6 API which returns track IDs for large playlists, then
    fetches track details in batches.

    Returns list of {"title": "...", "artist": "...", "netease_id": ...}
    """
    # v6 detail: inline `tracks` are truncated (~10) but `trackIds` is the
    # complete list — fetch full track details in batches from those IDs.
    # (The legacy /api/playlist/detail endpoint returns neither.)
    url = f"https://music.163.com/api/v6/playlist/detail?id={playlist_id}&n=1000"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            return []
        data = resp.json()
        pl = data.get("playlist") or data.get("result") or {}
        tracks = pl.get("tracks", [])
        track_ids = [t["id"] for t in pl.get("trackIds", [])]
        if track_ids and len(tracks) < len(track_ids):
            tracks = _fetch_track_details(track_ids)
        songs = []
        for rank, t in enumerate(tracks, 1):
            title = t.get("name", "").strip()
            # Handle both old API ("artists") and v3 API ("ar") formats.
            raw_artists = t.get("artists") or t.get("ar") or []
            artists = [a.get("name", "") for a in raw_artists]
            artist = ", ".join(a for a in artists if a)
            if title:
                songs.append({
                    "title": title,
                    "artist": artist,
                    "netease_id": t.get("id"),
                    "rank": rank,          # position within the playlist
                })
        return songs
    except Exception as e:
        print(f"  Error fetching playlist {playlist_id}: {e}", file=sys.stderr)
        return []


def search_playlists(keyword: str, limit: int = 3) -> list[dict]:
    """Search NetEase for playlists matching a keyword.

    Returns list of {"id": ..., "name": ..., "trackCount": ...}
    sorted by track count (largest first).
    """
    try:
        resp = requests.post(
            "https://music.163.com/api/search/get",
            data={"s": keyword, "type": 1000, "limit": limit, "offset": 0},
            headers=HEADERS, timeout=15,
        )
        if resp.status_code != 200:
            return []
        playlists = resp.json().get("result", {}).get("playlists", [])
        return sorted(playlists, key=lambda p: p.get("trackCount", 0), reverse=True)
    except Exception as e:
        print(f"  Search error: {e}", file=sys.stderr)
        return []


def search_artist_id(name: str) -> tuple[int | None, str | None]:
    """Resolve an artist name → (netease artist id, canonical name)."""
    try:
        resp = requests.post(
            "https://music.163.com/api/search/get",
            data={"s": name, "type": 100, "limit": 3},
            headers=HEADERS, timeout=15,
        )
        artists = resp.json().get("result", {}).get("artists", [])
        if artists:
            return artists[0]["id"], artists[0].get("name")
    except Exception:
        pass
    return None, None


def fetch_artist_hot_songs(artist_id: int, artist_name: str) -> list[dict]:
    """An artist's 50 hottest songs (NetEase v1 artist endpoint)."""
    try:
        resp = requests.get(f"https://music.163.com/api/v1/artist/{artist_id}",
                            headers=HEADERS, timeout=20)
        hot = resp.json().get("hotSongs", [])
    except Exception:
        return []
    songs = []
    for rank, s in enumerate(hot, 1):
        title = (s.get("name") or "").strip()
        if title:
            songs.append({
                "title": title,
                "artist": artist_name,
                "netease_id": s.get("id"),
                "rank": rank,
                "playlist_name": f"artist:{artist_name}",
            })
    return songs


def discover_artist_songs(artist_names: list[str]) -> list[dict]:
    """Similar-song expansion: hot-50 of every given artist.

    The natural \"more like what I already collect\" source — feed it the
    distinct artists of the existing corpus.
    """
    all_songs = []
    for i, name in enumerate(sorted(set(a for a in artist_names if a)), 1):
        aid, canonical = search_artist_id(name)
        if aid is None:
            print(f"  [{i}] {name}: artist not found")
            continue
        songs = fetch_artist_hot_songs(aid, canonical or name)
        print(f"  [{i}] {canonical or name}: {len(songs)} hot songs")
        all_songs.extend(songs)
        time.sleep(0.5)
    unique = clean_and_filter(all_songs)
    return filter_mandarin(unique)


def filter_mandarin(songs: list[dict]) -> list[dict]:
    """Rough filter: keep songs whose title contains CJK characters
    (removes English-only, Korean-only, Japanese-only tracks).
    """
    cjk_re = re.compile(r"[\u4e00-\u9fff]")
    return [s for s in songs if cjk_re.search(s["title"])]


# Cover/instrumental junk that pollutes NetEase playlists. Checked against
# BOTH title and artist (e.g. artist "\u8d35\u65cf\u94a2\u7434\u8f7b\u97f3\u4e50" is a piano-cover mill).
_NOISE = re.compile(
    r"\u94a2\u7434|\u92fc\u7434|piano|\u5409\u4ed6\u7248|\u7ffb\u5531|cover|\u4f34\u594f|instrumental|\u7eaf\u97f3\u4e50|\u7d14\u97f3\u6a02|"
    r"\u94c3\u58f0|\u9234\u8072|dj\u7248|dj |remix|\u6296\u97f3|\u6162\u901f|\u52a0\u901f|(?:0\.8|1\.25)x|\u4e32\u70e7|\u4e32\u71d2",
    re.IGNORECASE)
# Decorations to strip so variants dedup to the base title:
# (Live) / \uff08\u6df1\u60c5\u7248\uff09 / \u3010\u539f\u5531: \u2026\u3011 / [xxx]
_DECOR = re.compile(r"[\uff08(\u3010\[][^\uff09)\u3011\]]*[\uff09)\u3011\]]")


def clean_and_filter(songs: list[dict]) -> list[dict]:
    """Drop covers/instrumentals, strip decorations, dedup on base title."""
    out, seen = [], set()
    for s in songs:
        if _NOISE.search(s["title"]) or _NOISE.search(s.get("artist") or ""):
            continue
        title = _DECOR.sub("", s["title"]).strip(" -\u2014\u00b7")
        if not title or title in seen:
            continue
        seen.add(title)
        out.append({**s, "title": title})
    return out


def discover_songs(
    era: str | None = None,
    playlist_id: int | None = None,
    chart: str | None = None,
    mandarin_only: bool = True,
) -> list[dict]:
    """Discover songs from specified source(s).

    Args:
        era: "2000s", "2010s", "2020s", "classic", "ktv", or "all"
        playlist_id: specific NetEase playlist ID
        chart: "hot", "new", "original", "surge"
        mandarin_only: filter out non-CJK titles

    Returns deduplicated list of {"title", "artist", "netease_id"}.
    """
    all_songs = []

    def _stamp(songs: list[dict], playlist_name: str) -> list[dict]:
        """Attach provenance so `pipeline.py discover --add` can record
        WHERE each song was found (README: `discovered_from`)."""
        for s in songs:
            s["playlist_name"] = playlist_name
        return songs

    if playlist_id:
        print(f"Fetching playlist {playlist_id}...")
        all_songs.extend(_stamp(fetch_playlist(playlist_id),
                                f"playlist:{playlist_id}"))

    elif chart:
        # Search for chart-like playlists.
        print(f"Searching for chart playlists: {chart}...")
        results = search_playlists(f"华语 {chart} 排行榜")
        for p in results[:2]:
            print(f"  {p['name']} ({p.get('trackCount', 0)} tracks)...",
                  end=" ", flush=True)
            songs = fetch_playlist(p["id"])
            print(f"fetched {len(songs)}")
            all_songs.extend(_stamp(songs, p["name"]))
            time.sleep(1)

    elif era:
        eras = list(ERA_SEARCH_KEYWORDS.keys()) if era == "all" else [era]
        for e in eras:
            keywords = ERA_SEARCH_KEYWORDS.get(e, [])
            if not keywords:
                print(f"  Unknown era: {e}. Available: {list(ERA_SEARCH_KEYWORDS.keys())}")
                continue
            for kw in keywords:
                print(f"  [{e}] Searching: \"{kw}\"...", flush=True)
                results = search_playlists(kw, limit=PLAYLISTS_PER_KEYWORD + 2)
                for p in results[:PLAYLISTS_PER_KEYWORD]:
                    pid = p["id"]
                    name = p["name"]
                    tc = p.get("trackCount", 0)
                    if tc < 10:
                        continue
                    print(f"    → {name} ({tc} tracks)...", end=" ", flush=True)
                    songs = fetch_playlist(pid)
                    print(f"fetched {len(songs)}")
                    all_songs.extend(_stamp(songs, name))
                    time.sleep(1)

    # Drop covers/piano/DJ junk, strip (Live)-style decorations, dedup.
    unique = clean_and_filter(all_songs)

    if mandarin_only:
        unique = filter_mandarin(unique)

    return unique


def main():
    parser = argparse.ArgumentParser(
        description="Discover popular Mandarin pop songs from music charts",
    )
    parser.add_argument("--era", help="Era: 2000s / 2010s / 2020s / classic / ktv / all")
    parser.add_argument("--playlist", type=int, help="NetEase playlist ID")
    parser.add_argument("--chart", help="Chart: hot / new / original / surge")
    parser.add_argument("--format", choices=["txt", "json", "jsonl"], default="txt",
                        help="Output format (default: txt)")
    parser.add_argument("--output", "-o", type=str, help="Output file (default: stdout)")
    args = parser.parse_args()

    if not any([args.era, args.playlist, args.chart]):
        parser.print_help()
        print("\nExamples:")
        print("  python -m crawlers.charts --era all")
        print("  python -m crawlers.charts --era 2000s --format txt -o songs_2000s.txt")
        print("  python -m crawlers.charts --chart hot --format jsonl")
        print('  python -m crawlers.charts --era all -o new_songs.txt && python pipeline.py add --from new_songs.txt')
        return

    songs = discover_songs(era=args.era, playlist_id=args.playlist, chart=args.chart)
    print(f"\nDiscovered {len(songs)} unique Mandarin songs", file=sys.stderr)

    # Format output.
    if args.format == "txt":
        lines = [s["title"] for s in songs]
        output = "\n".join(lines)
    elif args.format == "json":
        output = json.dumps(songs, ensure_ascii=False, indent=2)
    else:  # jsonl
        output = "\n".join(json.dumps(s, ensure_ascii=False) for s in songs)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output + "\n")
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
