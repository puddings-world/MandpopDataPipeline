# ytclip

Download a YouTube video, trim head/tail, upload to your own channel as
unlisted, and get a shareable URL back.

This tool is **independent of the dataset pipeline** — it just lives in the
same repo for convenience. It only shares `yt-dlp` with the rest of the
project; everything else is local to this folder.

## Setup (one time)

### 1. Install Python deps

```powershell
pip install -r tools/yt_clip/requirements.txt
```

(`yt-dlp` and `ffmpeg` are already required by the main project. If `ffmpeg`
isn't on PATH, add it.)

### 2. Create an OAuth client

YouTube uploads require OAuth 2.0. The first run needs you to do this once:

1. Go to <https://console.cloud.google.com/apis/credentials>.
2. Create a project if you don't have one.
3. Enable **YouTube Data API v3** (APIs & Services → Library).
4. **Configure OAuth consent screen**: External, fill in app name + your
   email. Add yourself as a **Test User** (otherwise your token is capped
   to 7-day validity).
5. **Create credentials → OAuth client ID → Desktop app**. Download the
   JSON, rename it to `client_secret.json`, and put it in this folder
   (`tools/yt_clip/client_secret.json`).

### 3. Run the auth flow

```powershell
python tools/yt_clip/ytclip.py auth
```

A browser opens, you grant access, the token is saved to `token.json` in
this folder. After this, uploads run non-interactively (token auto-refreshes).

`client_secret.json` and `token.json` are gitignored — they never get
committed.

## Usage

Time arguments accept seconds (`12`, `12.5`) or `mm:ss` / `hh:mm:ss`
(`1:23`, `0:01:23`). `--speed` is a playback multiplier (e.g. `1.25` =
25 % faster, audio pitch preserved via `atempo`).

### One-shot (recommended)

```powershell
# Download + upload as-is (no trim, no speed change)
python tools/yt_clip/ytclip.py auto "https://www.youtube.com/watch?v=PbKNStnhUZY"

# Trim and upload
python tools/yt_clip/ytclip.py auto <URL> --start 0:05 --end 3:42 --title "Clip"

# Speed up to 1.25x and upload
python tools/yt_clip/ytclip.py auto <URL> --speed 1.25 --title "Sped up"

# Trim + speed in one shot (single ffmpeg re-encode)
python tools/yt_clip/ytclip.py auto <URL> --start 0:05 --end 3:42 --speed 1.25
```

Defaults: uploads as **unlisted**, cleans up local files after upload.
Add `--keep` to retain them. Add `--fast` to stream-copy the trim
(snaps to keyframes; not compatible with `--speed`).

### Step-by-step (for debugging)

```powershell
python tools/yt_clip/ytclip.py download <URL>
python tools/yt_clip/ytclip.py clip downloads/foo.mp4 --speed 1.25 -o foo.fast.mp4
python tools/yt_clip/ytclip.py upload foo.fast.mp4 --title "Sped up" --privacy unlisted
```

### Snip: extract audio / video / frame clips from a local file

Once you've downloaded (or otherwise have) a long video, `snip` pulls
out small pieces from it — audio-only segments, video segments, or
single frames. Handy for grabbing highlights from a stream, a lyric
section from a cover, a poster frame for a thumbnail, etc.

```powershell
# Audio segment (stream-copied m4a, zero re-encode)
python tools/yt_clip/ytclip.py snip audio long.mp4 --start 12:34 --end 13:45

# Same but re-encoded to flac / mp3 / wav
python tools/yt_clip/ytclip.py snip audio long.mp4 --start 12:34 --end 13:45 --format flac

# Video segment (stream-copied mp4 by default, ~keyframe-aligned)
python tools/yt_clip/ytclip.py snip video long.mp4 --start 05:10 --end 05:40

# Frame-accurate video cut (re-encodes)
python tools/yt_clip/ytclip.py snip video long.mp4 --start 05:10 --end 05:40 --reencode

# Crop a portrait 9:16 window from a 1920×1080 source and re-encode
python tools/yt_clip/ytclip.py snip video long.mp4 --start 5:10 --end 5:40 --crop 608:1080:656:0

# Single frame as JPG at 02:33
python tools/yt_clip/ytclip.py snip frame long.mp4 --at 02:33

# Cropped frame (useful for thumbnails)
python tools/yt_clip/ytclip.py snip frame long.mp4 --at 02:33 --crop 720:720:600:180 -o thumb.jpg
```

Default output paths embed the timestamps so you can extract multiple
clips without overwriting: e.g. `long.a12_34-13_45.m4a`,
`long.v5_10-5_40.mp4`, `long.f2_33.jpg`. Use `-o` to override.

#### Batch mode (`snip batch`)

For long videos where you want many clips in one pass, write a plain-text
batch file (one clip per line) and hand it to `snip batch`:

```
# clips.txt — comments and blank lines OK
# <type>  <start>   <end|at>   <name>
audio     12:34     13:45      intro-hook
audio     45:10     46:00      chorus
video     05:10     05:40      guitar-solo
frame     02:33                title-frame
```

```powershell
python tools/yt_clip/ytclip.py snip batch long.mp4 --from clips.txt
#  → writes long.clips/intro-hook.m4a, long.clips/chorus.m4a,
#    long.clips/guitar-solo.mp4, long.clips/title-frame.jpg
```

Output lands in `<input-stem>.clips/` next to the source unless you pass
`--outdir`. Names in the batch file become the output filenames (no
extension; the extension is chosen by clip type).

### Setlist: compile clips into one titled video (`setlist`)

Given a directory of `.mp4` clips (e.g. song covers pulled out of a long
stream via `snip`), builds one continuous video with a subtitle label
showing the current filename during each segment. Clips concat in
LastWriteTime order (= your export order). Re-run whenever new clips
land in the directory; the previous compilation is auto-excluded.

```powershell
# Default: writes <dirname>_setlist.mp4 in the directory
python tools/yt_clip/ytclip.py setlist "D:\practice\2026-08-01"

# Custom output name, keep 01./02./... numbering
python tools/yt_clip/ytclip.py setlist "D:\practice\2026-08-01" `
    -o "practice-highlights.mp4"

# Skip numeric prefix, position labels top-right, use a different font
python tools/yt_clip/ytclip.py setlist "D:\practice\2026-08-01" `
    --no-numbers --align 7 --font "標楷體"

# Exclude additional files (source dump, previous highlight cut, etc.)
python tools/yt_clip/ytclip.py setlist "D:\practice\2026-08-01" `
    --exclude "raw_stream" --exclude "reference"
```

Subtitle placement uses ASS alignment codes: `1/2/3` = bottom
left/centre/right, `5/6/7` = top left/centre/right. Labels are burnt in
(not soft-subs), so the output is a standard mp4 with no external
subtitle file needed.

### Multiple channels (`--profile`)

Each Google account / YouTube channel gets its own token file. The default
profile writes `token.json`; named profiles write `token.<name>.json`.

```powershell
# First-time auth for a secondary channel
python tools/yt_clip/ytclip.py auth --profile work
#  → browser prompts you to pick the account / channel; saves token.work.json

# Upload using that profile
python tools/yt_clip/ytclip.py auto <URL> --profile work --title "..."
```

The OAuth flow runs with `prompt="consent select_account"`, so every `auth`
opens the account picker — pick a different Google account or Brand
account to authorize a different channel.

## Privacy levels

| `--privacy` | Visible to                         | Shareable via URL |
|-------------|------------------------------------|-------------------|
| `unlisted`  | Anyone with the link (default)     | yes               |
| `private`   | Only you and people you invite     | no                |
| `public`    | Everyone, listed on your channel   | yes               |

## Quota

YouTube Data API gives 10,000 units/day by default. A video upload costs
**1,600 units**, so the cap is ~6 uploads/day. Request more in the Cloud
Console if you need it.

## Notes

- Default ffmpeg trim **re-encodes** (libx264 CRF 20 + AAC 192k) for
  frame-accurate cuts. Use `--fast` for keyframe-aligned stream copy.
- `--speed` uses `setpts=PTS/N` (video) + `atempo=N` (audio); pitch is
  preserved. Requires re-encode, so it can't combine with `--fast`.
- Downloads go into `tools/yt_clip/downloads/` (gitignored).
- If you change the OAuth scope or revoke the token, delete the relevant
  `token*.json` and re-run `auth`.
