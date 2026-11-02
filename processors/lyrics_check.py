"""Flag songs whose lyrics still don't fit their audio after alignment.

Layer 1.5 re-times what it can; what's left is usually the wrong source
(an edited / live / different-arrangement upload) or an LRC for another
version — both fixed by picking another video in the inbox editor.

Per song, from the vocals stem and the current lyrics.lrc:

    voiced   share of the first second after each line start that is sung
             (the independent check Layer 1.5 uses; aligned songs ≈ 0.9)
    tail     audio length minus the last sung line's start (s); negative
             means the lyrics run past the end of the audio

Verdict `suspect` when voiced < VOICED_MIN, or the lyrics outrun the audio,
or the audio continues far past the last line (a longer edit / extra
section the LRC doesn't have). Results: $DATASET_PATH/lyrics_audit.jsonl.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import soundfile as sf

from processors.lrc_align import parse_starts, voiced_after, voiced_mask

VOICED_MIN = 0.7
TAIL_MIN = -2.0        # last line starts after the audio ends → wrong version
TAIL_MAX = 90.0        # > 1.5 min of audio after the last line → extra section


def judge(voiced: float, tail: float) -> tuple[str, list[str]]:
    reasons = []
    if voiced < VOICED_MIN:
        reasons.append(f"lines often start where nobody sings (voiced {voiced:.2f})")
    if tail < TAIL_MIN:
        reasons.append(f"lyrics run {-tail:.0f}s past the end of the audio")
    if tail > TAIL_MAX:
        reasons.append(f"audio runs {tail:.0f}s past the last lyric line")
    return ("suspect" if reasons else "ok"), reasons


def check_song(song_dir: Path) -> dict | None:
    from processors.encode import find_stem
    vocals, lrc = find_stem(song_dir, "vocals"), song_dir / "lyrics.lrc"
    if vocals is None or not lrc.exists():
        return None
    starts = parse_starts(lrc.read_text(encoding="utf-8", errors="replace"))
    if len(starts) < 4:
        return None
    y, sr = sf.read(str(vocals), always_2d=True, dtype="float32")
    y = y.mean(axis=1)
    voiced = voiced_after(voiced_mask(y, sr), starts)
    tail = len(y) / sr - float(starts[-1])
    verdict, reasons = judge(voiced, tail)
    return {"title": song_dir.name, "verdict": verdict, "reasons": reasons,
            "voiced": round(voiced, 3), "tail_sec": round(tail, 1),
            "lrc_mtime": lrc.stat().st_mtime, "vocals_mtime": vocals.stat().st_mtime,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def load(path: Path) -> dict[str, dict]:
    rows = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                rows[r["title"]] = r
    return rows


def save(path: Path, rows: dict[str, dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                            for r in sorted(rows.values(), key=lambda r: r["title"])),
                    encoding="utf-8")


def is_current(row: dict, song_dir: Path) -> bool:
    """Row still describes the files on disk (no re-time / refetch since)."""
    from processors.encode import find_stem
    vocals, lrc = find_stem(song_dir, "vocals"), song_dir / "lyrics.lrc"
    return (vocals is not None and lrc.exists()
            and row.get("lrc_mtime") == lrc.stat().st_mtime
            and row.get("vocals_mtime") == vocals.stat().st_mtime)
