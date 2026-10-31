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
  snip audio FILE --start S --end E [--format m4a|mp3|wav|flac] [-o OUT]
                                    Extract an audio segment (stream-copy for m4a).
  snip video FILE --start S --end E [--reencode] [--crop W:H:X:Y] [--scale WxH]
                                    Extract a video segment (stream-copy by default).
  snip frame FILE --at T [--crop W:H:X:Y] [-o OUT]
                                    Extract a single frame as JPG.
  snip batch FILE --from CLIPS.txt [--outdir DIR]
                                    Run many snips from a batch file (see README).
  setlist DIR [-o OUT] [--exclude PAT] [--font NAME] [--no-numbers]
                                    Concat all .mp4 clips in DIR (LastWriteTime
                                    order) into one video with per-segment
                                    song-name subtitles burnt in.

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


# --- snip: extract clips from a local video file ---

def _sanitize_time(s: str) -> str:
    return str(s).replace(":", "_").replace(".", "_")


def _run_ffmpeg(cmd: list[str]) -> None:
    print("$", " ".join(cmd))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit(r.returncode)


def _snip_audio_ff(in_path: Path, start: float, end: float,
                   out: Path, fmt: str = "m4a") -> None:
    if fmt == "m4a":
        acodec = ["-c:a", "copy"]
    elif fmt == "flac":
        acodec = ["-c:a", "flac"]
    elif fmt == "wav":
        acodec = ["-c:a", "pcm_s16le"]
    elif fmt == "mp3":
        acodec = ["-c:a", "libmp3lame", "-b:a", "320k"]
    else:
        raise ValueError(f"Unsupported audio format: {fmt}")
    cmd = ["ffmpeg", "-y",
           "-ss", fmt_time(start), "-to", fmt_time(end),
           "-i", str(in_path), "-vn"] + acodec + [str(out)]
    _run_ffmpeg(cmd)


def _snip_video_ff(in_path: Path, start: float, end: float, out: Path,
                   reencode: bool = False,
                   crop: str | None = None, scale: str | None = None) -> None:
    if reencode or crop or scale:
        cmd = ["ffmpeg", "-y", "-i", str(in_path),
               "-ss", fmt_time(start), "-to", fmt_time(end)]
        vfilters = []
        if crop:
            vfilters.append(f"crop={crop}")
        if scale:
            vfilters.append(f"scale={scale}")
        if vfilters:
            cmd += ["-vf", ",".join(vfilters)]
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-c:a", "aac", "-b:a", "192k",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    else:
        cmd = ["ffmpeg", "-y",
               "-ss", fmt_time(start), "-to", fmt_time(end),
               "-i", str(in_path),
               "-c", "copy", "-movflags", "+faststart", str(out)]
    _run_ffmpeg(cmd)


def _snip_frame_ff(in_path: Path, at: float, out: Path,
                   crop: str | None = None) -> None:
    cmd = ["ffmpeg", "-y",
           "-ss", fmt_time(at), "-i", str(in_path),
           "-frames:v", "1", "-q:v", "2"]
    if crop:
        cmd += ["-vf", f"crop={crop}"]
    cmd += [str(out)]
    _run_ffmpeg(cmd)


def _validate_input(path_str: str) -> Path:
    p = Path(path_str)
    if not p.exists():
        sys.stderr.write(f"File not found: {p}\n")
        sys.exit(1)
    return p


def cmd_snip_audio(args) -> None:
    in_path = _validate_input(args.input)
    start = parse_time(args.start)
    end = parse_time(args.end)
    if end <= start:
        sys.stderr.write(f"End ({end}) must be > start ({start}).\n")
        sys.exit(1)
    out = Path(args.output) if args.output else in_path.with_name(
        f"{in_path.stem}.a{_sanitize_time(args.start)}-{_sanitize_time(args.end)}.{args.format}"
    )
    _snip_audio_ff(in_path, start, end, out, args.format)
    print(f"Snipped audio: {out}")


def cmd_snip_video(args) -> None:
    in_path = _validate_input(args.input)
    start = parse_time(args.start)
    end = parse_time(args.end)
    if end <= start:
        sys.stderr.write(f"End ({end}) must be > start ({start}).\n")
        sys.exit(1)
    out = Path(args.output) if args.output else in_path.with_name(
        f"{in_path.stem}.v{_sanitize_time(args.start)}-{_sanitize_time(args.end)}.mp4"
    )
    _snip_video_ff(in_path, start, end, out,
                   args.reencode, args.crop, args.scale)
    print(f"Snipped video: {out}")


def cmd_snip_frame(args) -> None:
    in_path = _validate_input(args.input)
    at = parse_time(args.at)
    out = Path(args.output) if args.output else in_path.with_name(
        f"{in_path.stem}.f{_sanitize_time(args.at)}.jpg"
    )
    _snip_frame_ff(in_path, at, out, args.crop)
    print(f"Snipped frame: {out}")


def cmd_snip_batch(args) -> None:
    """Batch snip. Batch file format (one clip per line):
        audio  <start>  <end>  <name>
        video  <start>  <end>  <name>
        frame  <at>            <name>
    Lines starting with '#' and blank lines are ignored.
    """
    in_path = _validate_input(args.input)
    batch_file = Path(args.batch_file)
    if not batch_file.exists():
        sys.stderr.write(f"Batch file not found: {batch_file}\n")
        sys.exit(1)
    outdir = Path(args.outdir) if args.outdir else in_path.parent / f"{in_path.stem}.clips"
    outdir.mkdir(exist_ok=True)
    done, failed = 0, 0
    for lineno, raw in enumerate(batch_file.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=3)
        try:
            kind = parts[0].lower()
            if kind == "audio":
                if len(parts) < 4:
                    raise ValueError("audio needs: audio <start> <end> <name>")
                start, end, name = parts[1], parts[2], parts[3]
                _snip_audio_ff(in_path, parse_time(start), parse_time(end),
                               outdir / f"{name}.m4a", "m4a")
            elif kind == "video":
                if len(parts) < 4:
                    raise ValueError("video needs: video <start> <end> <name>")
                start, end, name = parts[1], parts[2], parts[3]
                _snip_video_ff(in_path, parse_time(start), parse_time(end),
                               outdir / f"{name}.mp4")
            elif kind == "frame":
                if len(parts) < 3:
                    raise ValueError("frame needs: frame <at> <name>")
                at, name = parts[1], parts[2]
                _snip_frame_ff(in_path, parse_time(at),
                               outdir / f"{name}.jpg")
            else:
                raise ValueError(f"unknown clip type: {kind!r}")
            done += 1
        except SystemExit:
            failed += 1
        except Exception as e:
            sys.stderr.write(f"Line {lineno}: {e}\n")
            failed += 1
    print(f"\nBatch: {done} succeeded, {failed} failed. Output dir: {outdir}")


# --- setlist: concat multiple mp4 clips into one video with subtitle labels ---

def _sec_to_srt(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s_int = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms >= 1000:
        ms = 999
    return f"{h:02d}:{m:02d}:{s_int:02d},{ms:03d}"


def _ffprobe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"ffprobe failed on {path}: {r.stderr.strip()}")
    return float(r.stdout.strip())


def cmd_setlist(args) -> None:
    import tempfile

    src_dir = Path(args.dir)
    if not src_dir.is_dir():
        sys.stderr.write(f"Not a directory: {src_dir}\n")
        sys.exit(1)

    out_path = Path(args.output) if args.output else src_dir / f"{src_dir.name}_setlist.mp4"

    # Auto-exclude the output name so re-runs don't feed the compilation to itself
    exclude_patterns = list(args.exclude or []) + [out_path.name]

    all_mp4 = sorted(src_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    clips = [
        p for p in all_mp4
        if p.stat().st_size > 0
        and not any(pat in p.name for pat in exclude_patterns)
    ]
    if not clips:
        sys.stderr.write(f"No clips found in {src_dir} after exclusions.\n")
        sys.exit(1)

    print(f"discovered {len(clips)} clips in {src_dir}")
    songs, cum = [], 0.0
    for p in clips:
        dur = _ffprobe_duration(p)
        songs.append({"name": p.stem, "path": p, "dur": dur,
                      "start": cum, "end": cum + dur})
        print(f"  {p.stem:24s}  {dur:6.1f}s   (cum→ {cum+dur:6.1f}s)")
        cum += dur
    print(f"total: {cum:.1f}s = {cum/60:.1f} min · {len(songs)} clips")

    # Write concat and SRT to a temp dir with an ASCII-only path.
    # ffmpeg's subtitles filter can't easily handle Windows paths with `:`,
    # so we `cd` there and reference the SRT by bare filename.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        concat_txt = tmp / "concat.txt"
        subs_srt = tmp / "subs.srt"
        with open(concat_txt, "w", encoding="utf-8") as f:
            for s in songs:
                pstr = str(s["path"]).replace("'", r"'\''")
                f.write(f"file '{pstr}'\n")
        with open(subs_srt, "w", encoding="utf-8") as f:
            for i, s in enumerate(songs, 1):
                label = s["name"] if args.no_numbers else f"{i:02d}. {s['name']}"
                f.write(f"{i}\n{_sec_to_srt(s['start'])} --> "
                        f"{_sec_to_srt(s['end'])}\n{label}\n\n")

        style = ",".join([
            f"FontName={args.font}",
            f"FontSize={args.font_size}",
            "PrimaryColour=&H00FFFFFF",
            "OutlineColour=&H00000000",
            "BorderStyle=1",
            "Outline=2",
            "Shadow=1",
            f"Alignment={args.align}",
            f"MarginV={args.margin}",
        ])
        vf = f"subtitles={subs_srt.name}:force_style='{style}'"

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_txt),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "medium", "-crf", str(args.crf),
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(out_path),
        ]
        print(f"$ (cwd={subs_srt.parent})\n$ {' '.join(cmd)}\n")
        r = subprocess.run(cmd, cwd=str(subs_srt.parent))
        if r.returncode != 0:
            sys.exit(r.returncode)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\n✓ done: {out_path}  ({size_mb:.1f} MB)")


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

    # snip: extract audio / video / frame clips from a local file
    sp_snip = sub.add_parser("snip",
                             help="Extract audio / video / frame clips from a video file")
    snip_sub = sp_snip.add_subparsers(dest="snip_type", required=True)

    sp = snip_sub.add_parser("audio", help="Extract an audio segment")
    sp.add_argument("input")
    sp.add_argument("--start", required=True, help="Start time (sec or mm:ss)")
    sp.add_argument("--end", required=True, help="End time (sec or mm:ss)")
    sp.add_argument("--format", choices=["m4a", "mp3", "wav", "flac"],
                    default="m4a", help="Output audio format (default: m4a, stream-copy)")
    sp.add_argument("-o", "--output", help="Output filename")
    sp.set_defaults(func=cmd_snip_audio)

    sp = snip_sub.add_parser("video", help="Extract a video segment")
    sp.add_argument("input")
    sp.add_argument("--start", required=True)
    sp.add_argument("--end", required=True)
    sp.add_argument("--reencode", action="store_true",
                    help="Re-encode for frame-accurate cut (default: stream-copy)")
    sp.add_argument("--crop", help="ffmpeg crop filter, e.g. 1080:1920:420:0")
    sp.add_argument("--scale", help="ffmpeg scale filter, e.g. 1280:720 or 720:-2")
    sp.add_argument("-o", "--output")
    sp.set_defaults(func=cmd_snip_video)

    sp = snip_sub.add_parser("frame", help="Extract a single frame as JPG")
    sp.add_argument("input")
    sp.add_argument("--at", required=True, help="Timestamp (sec or mm:ss)")
    sp.add_argument("--crop", help="ffmpeg crop filter, e.g. 1080:1920:420:0")
    sp.add_argument("-o", "--output")
    sp.set_defaults(func=cmd_snip_frame)

    sp = snip_sub.add_parser("batch",
                             help="Extract many clips from a batch file")
    sp.add_argument("input")
    sp.add_argument("--from", dest="batch_file", required=True,
                    help="Batch file (one clip per line, see README)")
    sp.add_argument("--outdir", help="Output dir (default: <input-stem>.clips/)")
    sp.set_defaults(func=cmd_snip_batch)

    # setlist: compile many clips into one video with subtitle labels
    sp = sub.add_parser("setlist",
                        help="Concat .mp4 clips in a directory into one video "
                             "with song-name subtitle labels")
    sp.add_argument("dir",
                    help="Directory of .mp4 clips (each file becomes one "
                         "labelled segment; segments concat in LastWriteTime order)")
    sp.add_argument("-o", "--output",
                    help="Output filename (default: <dirname>_setlist.mp4 in DIR)")
    sp.add_argument("--exclude", action="append", metavar="PATTERN",
                    help="Filename substring to skip (repeatable). "
                         "The output filename is always auto-excluded.")
    sp.add_argument("--font", default="Microsoft JhengHei",
                    help="Subtitle font name (default: Microsoft JhengHei)")
    sp.add_argument("--font-size", default=32, type=int,
                    help="Subtitle font size (default 32)")
    sp.add_argument("--align", default=2, type=int,
                    help="ASS alignment: 1=BL 2=BC 3=BR / 5=TL 6=TC 7=TR (default 2)")
    sp.add_argument("--margin", default=40, type=int,
                    help="Vertical margin in px (default 40)")
    sp.add_argument("--crf", default=23, type=int,
                    help="libx264 CRF quality, lower=better (default 23)")
    sp.add_argument("--no-numbers", action="store_true",
                    help="Skip 01./02./... prefix; label with song name only")
    sp.set_defaults(func=cmd_setlist)

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
