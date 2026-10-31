"""Restore original.mp3 for compacted songs by re-downloading from YouTube.

`pipeline.py compact` deletes `songs/{title}/original.mp3` to save disk
(~70%/song), on the premise that it is regenerable from
`metadata.json[youtube_url]`. This script is that regeneration path:

    python restore_originals.py              # restore every missing original
    python restore_originals.py --dry-run    # just list what would be fetched
    python restore_originals.py --limit 5    # first 5 (smoke test)
    python restore_originals.py 一路向北      # specific title(s) only

Idempotent: songs that already have original.mp3 are skipped, so an
interrupted run can simply be re-launched. Downstream, StreetPerformerMaster's
sync_to_android.py picks the restored files up as `{title}.original.mp3` on
the phone (guide-mode / 導唱 source).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DATASET_PATH = Path(__file__).resolve().parent
SONGS_DIR = DATASET_PATH / "songs"

sys.path.insert(0, str(DATASET_PATH))
from crawlers.youtube import download_audio  # noqa: E402


def find_missing(titles: list[str] | None) -> list[tuple[str, Path, str | None]]:
    """Return (title, song_dir, youtube_url|None) for dirs lacking original.mp3."""
    rows = []
    dirs = ([SONGS_DIR / t for t in titles] if titles
            else sorted(d for d in SONGS_DIR.iterdir() if d.is_dir()))
    for d in dirs:
        if not d.is_dir():
            print(f"  !! no such song dir: {d.name}")
            continue
        if (d / "original.mp3").exists():
            continue
        url = None
        meta = d / "metadata.json"
        if meta.exists():
            try:
                url = json.loads(meta.read_text(encoding="utf-8")).get("youtube_url")
            except Exception:
                pass
        rows.append((d.name, d, url))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("titles", nargs="*", help="restrict to these song titles")
    ap.add_argument("--limit", type=int, default=None, help="stop after N downloads")
    ap.add_argument("--dry-run", action="store_true", help="list, don't download")
    args = ap.parse_args()

    rows = find_missing(args.titles or None)
    no_url = [t for t, _, u in rows if not u]
    todo = [(t, d, u) for t, d, u in rows if u]
    if args.limit:
        todo = todo[: args.limit]

    print(f"missing original.mp3: {len(rows)}  |  fetchable: {len(todo)}"
          f"  |  no youtube_url: {len(no_url)}")
    for t in no_url:
        print(f"  !! no url: {t}")
    if args.dry_run:
        for t, _, u in todo:
            print(f"  would fetch: {t}  <-  {u}")
        return 0

    ok, failed = 0, []
    for i, (title, d, url) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {title}", flush=True)
        try:
            download_audio(url, d / "original.mp3")
            ok += 1
        except Exception as e:
            print(f"  ✗ {e}")
            failed.append(title)

    print(f"\nDone: restored={ok} failed={len(failed)} no_url={len(no_url)}")
    if failed:
        print("Failed (re-run to retry):")
        for t in failed:
            print(f"  - {t}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
