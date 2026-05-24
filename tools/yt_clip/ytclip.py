#!/usr/bin/env python3
"""ytclip - download a YouTube video, optionally trim / speed-change it, upload.

Subcommands:
  auth [--profile NAME]             Run one-time OAuth flow.
  download URL [-o FILE]            Download with yt-dlp (mp4 if available).
  clip FILE [--start S] [--end E] [--speed N] [-o OUT] [--fast]
                                    Re-encode with ffmpeg. Any of trim/speed
                                    may be omitted; at least one is required.
  upload FILE --title T [--description D] [--privacy P] [--profile NAME]
                                    Upload a video to YouTube.
  auto URL [--start S] [--end E] [--speed N] [--title T] ...
                                    Download (+ optionally process) + upload.

Time format: seconds (12, 12.5) or mm:ss / hh:mm:ss (1:23, 0:01:23).
Speed: playback multiplier, e.g. 1.25 = 25% faster (pitch preserved).
Profile: which token file to use; default = token.json,
         --profile work = token.work.json (so multiple channels can coexist).
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOWNLOADS = HERE / "downloads"
CLIENT_SECRET = HERE / "client_secret.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _token_path(profile: str | None) -> Path:
    return HERE / (f"token.{profile}.json" if profile else "token.json")


def parse_time(s: str) -> float:
    s = str(s).strip()
    if ":" in s:
        parts = s.split(":")
        if len(parts) == 2:
            m, sec = parts
            return int(m) * 60 + float(sec)
        if len(parts) == 3:
            h, m, sec = parts
            return int(h) * 3600 + int(m) * 60 + float(sec)
        raise ValueError(f"Unrecognized time format: {s!r}")
    return float(s)


def fmt_time(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def cmd_download(args) -> str:
    DOWNLOADS.mkdir(exist_ok=True)
    out_template = args.output or str(DOWNLOADS / "%(title)s [%(id)s].%(ext)s")
    cmd = [
        "yt-dlp",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", out_template,
        "--print", "after_move:filepath",
        "--no-simulate",
        args.url,
    ]
    print("$", " ".join(cmd))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    filepath = lines[-1] if lines else None
    if not filepath or not Path(filepath).exists():
        sys.stderr.write("Could not determine downloaded filepath.\n")
        sys.stderr.write(result.stdout)
        sys.exit(1)
    print(f"Downloaded: {filepath}")
    return filepath


def cmd_clip(args, input_path: str | None = None) -> str:
    in_path = Path(input_path or args.input)
    start = parse_time(args.start) if args.start else None
    end = parse_time(args.end) if args.end else None
    speed = float(args.speed) if args.speed else None
    if start is None and end is None and speed is None:
        sys.stderr.write("Need at least one of --start, --end, --speed.\n")
        sys.exit(1)
    if start is not None and end is not None and end <= start:
        sys.stderr.write(f"End ({end}) must be greater than start ({start}).\n")
        sys.exit(1)
    if speed is not None and (speed <= 0 or speed > 100):
        sys.stderr.write(f"Speed must be in (0, 100]; got {speed}.\n")
        sys.exit(1)

    out = args.output or str(in_path.with_name(in_path.stem + ".clip.mp4"))

    if args.fast and speed is None:
        cmd = ["ffmpeg", "-y"]
        if start is not None:
            cmd += ["-ss", fmt_time(start)]
        if end is not None:
            cmd += ["-to", fmt_time(end)]
        cmd += ["-i", str(in_path), "-c", "copy", out]
    else:
        cmd = ["ffmpeg", "-y", "-i", str(in_path)]
        if start is not None:
            cmd += ["-ss", fmt_time(start)]
        if end is not None:
            cmd += ["-to", fmt_time(end)]
        if speed is not None:
            cmd += ["-filter:v", f"setpts=PTS/{speed}",
                    "-filter:a", f"atempo={speed}"]
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", out]
    print("$", " ".join(cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(result.returncode)
    print(f"Clipped: {out}")
    return out


def _import_google():
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        return InstalledAppFlow, Request, Credentials, build, MediaFileUpload
    except ImportError:
        sys.stderr.write(
            "Missing Google API libraries. Install with:\n"
            "  pip install google-auth-oauthlib google-api-python-client\n"
        )
        sys.exit(1)


def _get_credentials(profile: str | None = None):
    InstalledAppFlow, Request, Credentials, _, _ = _import_google()
    token_path = _token_path(profile)
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not CLIENT_SECRET.exists():
            sys.stderr.write(
                f"Missing OAuth client secret at {CLIENT_SECRET}.\n"
                "Create one at https://console.cloud.google.com/apis/credentials\n"
                "  (OAuth 2.0 Client ID -> Desktop app), download the JSON,\n"
                "  save it as client_secret.json in this folder, then re-run `auth`.\n"
            )
            sys.exit(1)
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
        # prompt='consent select_account' so the browser always lets us pick
        # which Google account / YouTube channel to authorize.
        creds = flow.run_local_server(port=0, prompt="consent select_account")
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def cmd_auth(args) -> None:
    _get_credentials(args.profile)
    print(f"OAuth token saved to {_token_path(args.profile)}")


def cmd_upload(args, file_path: str | None = None) -> str:
    path = Path(file_path or args.input)
    if not path.exists():
        sys.stderr.write(f"File not found: {path}\n")
        sys.exit(1)
    _, _, _, build, MediaFileUpload = _import_google()
    creds = _get_credentials(getattr(args, "profile", None))
    youtube = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": args.title or path.stem,
            "description": args.description or "",
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": args.privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(path), chunksize=4 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    print(f"Uploading {path} ({path.stat().st_size / 1e6:.1f} MB)...")
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"  {int(status.progress() * 100)}%", end="\r", flush=True)
    print()
    video_id = response["id"]
    url = f"https://youtu.be/{video_id}"
    print(f"Uploaded ({args.privacy}): {url}")
    return url


def cmd_auto(args) -> None:
    downloaded = cmd_download(argparse.Namespace(url=args.url, output=None))
    needs_processing = bool(args.start or args.end or args.speed)
    if needs_processing:
        clipped = cmd_clip(
            argparse.Namespace(input=downloaded, output=None,
                               start=args.start, end=args.end,
                               speed=args.speed, fast=args.fast),
            downloaded,
        )
        upload_path = clipped
    else:
        clipped = None
        upload_path = downloaded
    url = cmd_upload(
        argparse.Namespace(input=upload_path, title=args.title,
                           description=args.description, privacy=args.privacy,
                           profile=args.profile),
        upload_path,
    )
    if not args.keep:
        for f in (downloaded, clipped):
            if not f:
                continue
            try:
                Path(f).unlink()
            except OSError as e:
                sys.stderr.write(f"  (cleanup) could not delete {f}: {e}\n")
    print(f"\n=> {url}")


def main() -> None:
    p = argparse.ArgumentParser(
        prog="ytclip",
        description="Download, trim, and upload YouTube videos as unlisted.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("auth", help="Run one-time OAuth flow")
    sp.add_argument("--profile", help="Token profile name (default: token.json)")
    sp.set_defaults(func=cmd_auth)

    sp = sub.add_parser("download", help="Download a YouTube video")
    sp.add_argument("url")
    sp.add_argument("-o", "--output", help="Output filename or yt-dlp template")
    sp.set_defaults(func=cmd_download)

    sp = sub.add_parser("clip", help="Trim and/or change speed of a local video")
    sp.add_argument("input")
    sp.add_argument("--start", help="Start time (sec or mm:ss)")
    sp.add_argument("--end", help="End time (sec or mm:ss)")
    sp.add_argument("--speed", help="Playback multiplier, e.g. 1.25")
    sp.add_argument("-o", "--output", help="Output filename")
    sp.add_argument("--fast", action="store_true",
                    help="Stream-copy (fast, keyframe-aligned; no --speed allowed)")
    sp.set_defaults(func=cmd_clip)

    sp = sub.add_parser("upload", help="Upload a video to YouTube")
    sp.add_argument("input")
    sp.add_argument("--title", help="Video title (default: filename stem)")
    sp.add_argument("--description", default="", help="Video description")
    sp.add_argument("--privacy", choices=["unlisted", "private", "public"],
                    default="unlisted")
    sp.add_argument("--profile", help="Token profile name (default: token.json)")
    sp.set_defaults(func=cmd_upload)

    sp = sub.add_parser("auto",
                        help="Download (+ optional trim/speed) + upload")
    sp.add_argument("url")
    sp.add_argument("--start", help="Trim start time (optional)")
    sp.add_argument("--end", help="Trim end time (optional)")
    sp.add_argument("--speed", help="Playback multiplier, e.g. 1.25 (optional)")
    sp.add_argument("--title")
    sp.add_argument("--description", default="")
    sp.add_argument("--privacy", choices=["unlisted", "private", "public"],
                    default="unlisted")
    sp.add_argument("--fast", action="store_true",
                    help="Stream-copy for the clip step")
    sp.add_argument("--keep", action="store_true",
                    help="Keep local downloaded/clipped files after upload")
    sp.add_argument("--profile", help="Token profile name (default: token.json)")
    sp.set_defaults(func=cmd_auto)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
