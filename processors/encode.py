"""Audio encoding helpers.

  * `encode_m4a` — AAC encode of the lossless master to the playable
    `instrumental.m4a` deliverable that consumer apps load.
  * `wav_to_flac` — lossless transcode used by `pipeline.py compact`
    to halve stem storage without losing any audio fidelity.
  * `find_stem` — utility for the rest of the codebase so callers don't
    care whether a stem is currently stored as `.wav` or `.flac`.

Per-song layout after the compact step (`pipeline.py compact`):

    instrumental.m4a      AAC 192k playable, consumer apps load this
    instrumental.flac     lossless stem, downstream (VocalStudio, re-Demucs)
    vocals.flac           lossless stem, downstream
    lyrics.lrc / .txt / metadata.json

Before compact, `instrumental.wav` and `vocals.wav` are the lossless
masters; compact just transcodes them to FLAC (bit-exact) and removes
the WAV copy.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

DEFAULT_BITRATE = "192k"

# Stems are accepted in either container, with FLAC preferred (smaller
# but lossless). New songs land as WAV from Demucs; compact converts.
STEM_SUFFIXES = (".flac", ".wav")


def find_stem(song_dir: Path, stem_name: str) -> Path | None:
    """Return the on-disk path of a stem (`vocals` / `instrumental`).

    Tries `.flac` first (preferred), then `.wav`. Returns None if the
    stem hasn't been produced yet.
    """
    for ext in STEM_SUFFIXES:
        p = song_dir / f"{stem_name}{ext}"
        if p.exists():
            return p
    return None


def wav_to_flac(wav_path: Path, remove_wav: bool = True) -> Path:
    """Losslessly transcode a WAV to FLAC (bit-exact decode round-trip).

    FLAC typically halves the file size for our 44.1 kHz stereo stems.
    If `remove_wav` is True (the default), the source WAV is deleted
    once the FLAC is confirmed to be on disk and non-empty.
    """
    wav_path = Path(wav_path)
    if not wav_path.exists():
        raise FileNotFoundError(f"missing wav: {wav_path}")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found on PATH")

    flac_path = wav_path.with_suffix(".flac")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(wav_path),
        "-c:a", "flac", "-compression_level", "5",
        str(flac_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"flac encode failed: {result.stderr.strip()[-500:]}")
    if not flac_path.exists() or flac_path.stat().st_size == 0:
        raise RuntimeError(f"flac encode produced no output: {flac_path}")
    if remove_wav:
        wav_path.unlink()
    return flac_path


def encode_m4a(wav_path: Path, m4a_path: Path,
               bitrate: str = DEFAULT_BITRATE,
               overwrite: bool = False) -> Path:
    """Encode `wav_path` to AAC inside an M4A container at `m4a_path`.

    Uses ffmpeg's `aac` encoder (always available). For instrumental
    backing tracks 192k AAC is transparent; consumer apps that want
    smaller files can re-encode from the wav master.
    """
    wav_path = Path(wav_path)
    m4a_path = Path(m4a_path)

    if not wav_path.exists():
        raise FileNotFoundError(f"missing wav: {wav_path}")
    if m4a_path.exists() and not overwrite:
        return m4a_path
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found on PATH")

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(wav_path),
        "-c:a", "aac", "-b:a", bitrate,
        "-movflags", "+faststart",
        str(m4a_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()[-500:]}")
    return m4a_path


def encode_song_dir(song_dir: Path, bitrate: str = DEFAULT_BITRATE,
                    overwrite: bool = False) -> Path | None:
    """Encode the song's instrumental stem to instrumental.m4a if missing.

    Accepts either `instrumental.wav` or `instrumental.flac` as input
    (compact converts wav→flac after the m4a is produced, so older songs
    have flac stems but the m4a stays consumer-facing).

    Returns the m4a path if produced/present, None if there's no
    instrumental stem on disk.
    """
    src = find_stem(song_dir, "instrumental")
    if src is None:
        return None
    m4a = song_dir / "instrumental.m4a"
    return encode_m4a(src, m4a, bitrate=bitrate, overwrite=overwrite)
