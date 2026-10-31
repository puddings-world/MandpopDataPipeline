"""ytgrab — minimal standalone YouTube downloader.

Independent of the dataset pipeline: give it a YouTube URL, get a file
on disk. Used for ad-hoc music/video fetches that don't need Demucs,
alignment, or any of the audit gates.

Usage:
    python ytgrab.py <URL>                          # audio (mp3, default)
    python ytgrab.py <URL> --video                  # video (mp4)
    python ytgrab.py <URL> --format m4a             # audio in m4a/aac container
    python ytgrab.py <URL> --format wav             # lossless wav
    python ytgrab.py <URL> -o my_song.mp3           # custom output path
    python ytgrab.py <URL> -o ./downloads/          # directory (auto-named)
    python ytgrab.py <URL> --quality 0              # best audio quality (default)
    python ytgrab.py <URL> --quality 192k           # specific bitrate
    python ytgrab.py <URL> --info                   # show metadata, don't download
    python ytgrab.py URL1 URL2 URL3                 # batch
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

AUDIO_FORMATS = ("mp3", "m4a", "wav", "opus", "flac")
DEFAULT_AUDIO = "mp3"
DEFAULT_VIDEO = "mp4"


def _check_tools() -> None:
    """yt-dlp + ffmpeg are required. Fail fast with a useful hint."""
    missing = [t for t in ("yt-dlp", "ffmpeg") if not shutil.which(t)]
    if missing:
        sys.stderr.write(
            f"ytgrab: missing tool(s): {', '.join(missing)}\n"
            f"  Install:\n"
            f"    pip install yt-dlp        # or winget/brew/apt\n"
            f"    winget install ffmpeg     # macOS: brew install ffmpeg\n"
        )
        sys.exit(2)


def _sanitize_stem(name: str) -> str:
    """Make a filename stem safe across Windows/macOS/Linux."""
    name = re.sub(r"[\\/:*?\"<>|]", "_", name).strip()
    return name[:120] or "youtube_download"


def get_info(url: str) -> dict | None:
    """Fetch metadata for a URL without downloading."""
    result = subprocess.run(
        ["yt-dlp", "--dump-single-json", "--no-warnings",
         "--no-playlist", url],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _resolve_output(url: str, output: str | None, suffix: str) -> Path:
    """Decide where the file goes.

    Rules:
      * No --output → `<sanitized title>.<suffix>` in cwd.
      * --output ends with `/` or is an existing directory → put it inside
        with the sanitized title as stem.
      * --output is a full path → use exactly as given (still ensure suffix).
    """
    if output is None:
        info = get_info(url) or {}
        stem = _sanitize_stem(info.get("title") or info.get("id") or "youtube")
        return Path(f"{stem}.{suffix}")

    p = Path(output)
    if output.endswith(("/", "\\")) or (p.exists() and p.is_dir()):
        p.mkdir(parents=True, exist_ok=True)
        info = get_info(url) or {}
        stem = _sanitize_stem(info.get("title") or info.get("id") or "youtube")
        return p / f"{stem}.{suffix}"

    if p.suffix.lower().lstrip(".") != suffix.lower():
        p = p.with_suffix(f".{suffix}")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def download_audio(url: str, out_path: Path, audio_format: str = DEFAULT_AUDIO,
                   quality: str = "0") -> Path:
    """Pull bestaudio and transcode to `audio_format` at `quality`."""
    cmd = [
        "yt-dlp", "-f", "bestaudio",
        "-x", "--audio-format", audio_format,
        "--audio-quality", str(quality),
        "--no-playlist",
        "-o", str(out_path.with_suffix("")) + ".%(ext)s",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed:\n{result.stderr.strip()[-600:]}")

    # yt-dlp picks the suffix; locate whatever it wrote.
    expected = out_path.with_suffix(f".{audio_format}")
    if expected.exists():
        return expected
    for cand in out_path.parent.glob(f"{out_path.stem}.*"):
        if cand.suffix.lstrip(".") in AUDIO_FORMATS:
            return cand
    raise FileNotFoundError(f"yt-dlp completed but output not found near {out_path}")


def download_video(url: str, out_path: Path) -> Path:
    """Pull best combined video+audio as mp4."""
    cmd = [
        "yt-dlp",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "-o", str(out_path.with_suffix("")) + ".%(ext)s",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed:\n{result.stderr.strip()[-600:]}")

    expected = out_path.with_suffix(".mp4")
    if expected.exists():
        return expected
    for cand in out_path.parent.glob(f"{out_path.stem}.*"):
        if cand.suffix.lower() == ".mp4":
            return cand
    raise FileNotFoundError(f"yt-dlp completed but output not found near {out_path}")


def _print_info(info: dict) -> None:
    """One-screen summary instead of yt-dlp's full JSON dump."""
    keys = (
        ("Title", "title"),
        ("Uploader", "uploader"),
        ("Channel URL", "channel_url"),
        ("Duration (s)", "duration"),
        ("Views", "view_count"),
        ("Upload date", "upload_date"),
        ("URL", "webpage_url"),
    )
    for label, key in keys:
        value = info.get(key)
        if value is not None:
            print(f"  {label:14s} {value}")
    formats = info.get("formats") or []
    print(f"  Formats        {len(formats)} available")


def fetch_one(url: str, audio_format: str | None, video: bool,
              quality: str, output: str | None,
              show_info: bool) -> Path | None:
    """Fetch a single URL according to the parsed args."""
    print(f"\n→ {url}")

    info = get_info(url)
    if info is None:
        print("  ✗ failed to fetch info — bad URL?")
        return None

    if show_info:
        _print_info(info)
        return None

    suffix = "mp4" if video else (audio_format or DEFAULT_AUDIO)
    out_path = _resolve_output(url, output, suffix)
    print(f"  → {out_path}")

    try:
        if video:
            written = download_video(url, out_path)
        else:
            written = download_audio(url, out_path, audio_format or DEFAULT_AUDIO,
                                     quality)
    except (RuntimeError, FileNotFoundError) as e:
        print(f"  ✗ {e}")
        return None

    size_mb = written.stat().st_size / (1024 * 1024)
    print(f"  ✓ {written.name} ({size_mb:.1f} MB)")
    return written


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ytgrab",
        description="Minimal YouTube downloader — audio or video from a URL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  ytgrab.py URL                          → ./<title>.mp3 (best quality)\n"
            "  ytgrab.py URL --format m4a             → ./<title>.m4a\n"
            "  ytgrab.py URL --format wav             → lossless ./<title>.wav\n"
            "  ytgrab.py URL --video                  → ./<title>.mp4\n"
            "  ytgrab.py URL -o ./downloads/          → ./downloads/<title>.mp3\n"
            "  ytgrab.py URL -o song.m4a              → exact path, m4a inferred\n"
            "  ytgrab.py URL --info                   → show metadata, skip download\n"
            "  ytgrab.py URL1 URL2 URL3               → batch\n"
        ),
    )
    parser.add_argument("urls", nargs="+", help="YouTube URL(s)")

    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--video", action="store_true",
                     help="Download video (mp4) instead of audio")
    grp.add_argument("--format", choices=AUDIO_FORMATS, default=None,
                     help=f"Audio format (default {DEFAULT_AUDIO})")

    parser.add_argument("--quality", default="0",
                        help="Audio quality: 0 (best, default) or a bitrate like 192k")
    parser.add_argument("-o", "--output",
                        help="Output file or directory (auto-named if directory)")
    parser.add_argument("--info", action="store_true",
                        help="Show video metadata only, don't download")

    args = parser.parse_args()

    if not args.info:
        _check_tools()

    failures = 0
    for url in args.urls:
        result = fetch_one(url, args.format, args.video, args.quality,
                           args.output, args.info)
        if result is None and not args.info:
            failures += 1

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
