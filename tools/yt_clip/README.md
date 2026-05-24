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
