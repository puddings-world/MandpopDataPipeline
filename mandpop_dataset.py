"""Lightweight reader for the MandpopDataset.

Use this from any external project that wants to load the processed
dataset — verified instrumentals + alignment-checked LRC lyrics — without
having to install the full pipeline (yt-dlp, Demucs, WhisperX, …).

Install
-------
    pip install -e /path/to/MandpopDataPipeline

Then point `DATASET_PATH` at the dataset directory (env var or `.env` at
the consumer project root) and:

    >>> from mandpop_dataset import Dataset
    >>> ds = Dataset()
    >>> len(ds)
    108
    >>> for song in ds:
    ...     print(song.title, "—", song.artist)
    ...
    >>> song = ds["一葷一素"]
    >>> song.audio_path
    PosixPath('/.../MandpopDataset/songs/一葷一素/instrumental.m4a')
    >>> for line in song.lyrics():
    ...     print(f"[{line.time_sec:7.2f}s] {line.text}")

The reader only exposes the processed (alignment-verified) subset. To
inspect raw catalogs, audit reports, or anything pipeline-internal, use
the `pipeline.py` CLI directly — those concerns deliberately don't leak
into the consumer-facing API.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

try:  # `.env` support is convenient but not required.
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass


__all__ = ["Dataset", "Song", "LyricLine", "parse_lrc"]


# ─────────────────────────────────────────────────────────────────────────
# LRC parsing (slim, self-contained — no numpy / no pipeline dependencies)
# ─────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LyricLine:
    """One timestamped lyric line."""
    time_sec: float
    text: str


_TIMESTAMP_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:\.(\d{1,3}))?\]")
_HEADER_RE = re.compile(r"^\s*\[(ti|ar|al|by|au|offset)\s*:", re.IGNORECASE)
_CREDIT_RE = re.compile(
    r"^\s*("
    r"作[词詞曲]|编[曲]|編[曲]|制作|製作|监制|監製|出品|发行|發行|"
    r"录音|錄音|混音|母带|母帶|演唱|和声|和聲|"
    r"by|lyric|composer|arranger|producer|recorded|mixed|mastered"
    r")",
    re.IGNORECASE,
)


def parse_lrc(lrc_text: str, drop_credits: bool = True) -> list[LyricLine]:
    """Parse LRC text into [LyricLine(time_sec, text)] sorted by time.

    Header tags (`[ti:]`, `[ar:]`, `[al:]`, `[offset:]` etc.) are always
    skipped. Production-credit lines (作詞 / 作曲 / 演唱 …) are skipped by
    default — set `drop_credits=False` to keep them.
    """
    lines: list[LyricLine] = []
    for raw in lrc_text.splitlines():
        if _HEADER_RE.match(raw):
            continue
        stamps = list(_TIMESTAMP_RE.finditer(raw))
        if not stamps:
            continue
        text = _TIMESTAMP_RE.sub("", raw).strip()
        if not text:
            continue
        if drop_credits and _CREDIT_RE.match(text):
            continue
        for m in stamps:
            mm = int(m.group(1))
            ss = int(m.group(2))
            frac = (m.group(3) or "0").ljust(3, "0")[:3]
            t = mm * 60 + ss + int(frac) / 1000.0
            lines.append(LyricLine(t, text))
    lines.sort(key=lambda l: l.time_sec)
    return lines


# ─────────────────────────────────────────────────────────────────────────
# Song record
# ─────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Song:
    """One entry from processed_dataset.jsonl, with absolute file paths."""
    title: str
    artist: str | None
    lyricist: str | None
    composer: str | None
    album: str | None
    youtube_url: str | None
    audio_path: Path
    lyrics_path: Path

    def lyrics(self, drop_credits: bool = True) -> list[LyricLine]:
        """Parsed LRC contents (sorted, header / credit lines filtered)."""
        return parse_lrc(self.lyrics_path.read_text(encoding="utf-8"),
                         drop_credits=drop_credits)

    def lyrics_text(self) -> str:
        """Raw LRC text on disk — use this if you want to render timestamps."""
        return self.lyrics_path.read_text(encoding="utf-8")

    def audio_exists(self) -> bool:
        return self.audio_path.exists()


# ─────────────────────────────────────────────────────────────────────────
# Dataset handle
# ─────────────────────────────────────────────────────────────────────────

class Dataset:
    """Read-only handle for the MandpopDataset processed subset.

    Dict-like by title: `ds["一葷一素"]`, `"一葷一素" in ds`, `for song in ds`,
    `len(ds)`. Plus a few convenience filters.
    """

    def __init__(self, path: str | Path | None = None,
                 require_audio_exists: bool = False):
        """
        Args:
            path: dataset root. Defaults to the `DATASET_PATH` env var.
            require_audio_exists: if True, drop entries whose
                `instrumental.m4a` file is missing on disk. The JSONL
                schema records `m4a == True` but the file could still be
                gitignored on a fresh clone.
        """
        resolved = path or os.environ.get("DATASET_PATH")
        if resolved is None:
            raise RuntimeError(
                "DATASET_PATH is not set. Either pass `path=` to "
                "Dataset(...) or set the env var (a .env file at your "
                "project root works too)."
            )
        self.path = Path(resolved).resolve()
        if not self.path.exists():
            raise FileNotFoundError(f"Dataset directory not found: {self.path}")

        self.processed_jsonl = self.path / "processed_dataset.jsonl"
        if not self.processed_jsonl.exists():
            raise FileNotFoundError(
                f"{self.processed_jsonl} is missing — has the pipeline "
                f"produced any verified entries yet? Run `python pipeline.py "
                f"status` against this directory to check."
            )

        self._require_audio_exists = require_audio_exists
        self._songs: dict[str, Song] = {}
        self.reload()

    # ── (re)loading ─────────────────────────────────────────────────────

    def reload(self) -> None:
        """Re-read processed_dataset.jsonl from disk. Call if the
        pipeline has been re-run since the Dataset was constructed."""
        self._songs.clear()
        for line in self.processed_jsonl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            files = rec.get("files") or {}
            song = Song(
                title=rec["title"],
                artist=rec.get("artist"),
                lyricist=rec.get("lyricist"),
                composer=rec.get("composer"),
                album=rec.get("album"),
                youtube_url=rec.get("youtube_url"),
                audio_path=self.path / files.get("instrumental", ""),
                lyrics_path=self.path / files.get("lyrics", ""),
            )
            if self._require_audio_exists and not song.audio_exists():
                continue
            self._songs[song.title] = song

    # ── dict-like API ───────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._songs)

    def __iter__(self) -> Iterator[Song]:
        return iter(self._songs.values())

    def __getitem__(self, title: str) -> Song:
        return self._songs[title]

    def __contains__(self, title: str) -> bool:
        return title in self._songs

    def get(self, title: str, default: Song | None = None) -> Song | None:
        return self._songs.get(title, default)

    def titles(self) -> list[str]:
        return list(self._songs)

    def all(self) -> list[Song]:
        return list(self._songs.values())

    # ── filtering helpers ───────────────────────────────────────────────

    def filter(self, *,
               artist: str | None = None,
               album: str | None = None,
               lyricist: str | None = None,
               composer: str | None = None,
               where: Callable[[Song], bool] | None = None) -> list[Song]:
        """Keyword filters + an optional `where` predicate.

            ds.filter(artist="周杰倫")
            ds.filter(where=lambda s: s.album and "MP3" in s.album)
        """
        out = []
        for s in self._songs.values():
            if artist and s.artist != artist:
                continue
            if album and s.album != album:
                continue
            if lyricist and s.lyricist != lyricist:
                continue
            if composer and s.composer != composer:
                continue
            if where and not where(s):
                continue
            out.append(s)
        return out

    def artists(self) -> set[str]:
        """All distinct artist values present."""
        return {s.artist for s in self._songs.values() if s.artist}

    def albums(self) -> set[str]:
        return {s.album for s in self._songs.values() if s.album}

    # ── debug ───────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return f"<Dataset path={self.path} songs={len(self._songs)}>"
