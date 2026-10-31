"""
Hotspot-driven A/B review for separation candidates.

Listening to whole songs per method does not scale. Instead we find the few
windows where the candidate instrumentals actually disagree and build a
listening page that loops only those segments, blind, with one-key voting.

Hotspot detection (per song, all candidates loudness-matched by RMS):

  1. STFT each instrumental (mono, n_fft 2048, hop 512) → log power (dB).
  2. Slide a 2 s window (1 s hop). In two bands:
       harmony  200 Hz – 5 kHz   lead-vs-backing decisions live here
       detail   5 kHz – 16 kHz   cymbals, air, sibilant leakage
     compute for each method its mean |dB difference| to the others; the
     method with the largest one is the "odd one out", its distance is
     the window's score, and the sign of its mean dB offset says whether
     it kept MORE or LESS energy than the rest.
  3. Skip near-silent windows (band energy 35 dB below the song's loudest).
  4. Divide scores by the song's median score per band (so "3.0" means
     three times the typical disagreement), then greedily pick the top
     windows with non-maximum suppression (no overlapping segments),
     ~60 % harmony / 40 % detail.
  5. Each pick becomes a 6 s segment centred on the window.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from urllib.parse import quote

import numpy as np
import soundfile as sf
from scipy.signal import stft

BANDS = {"harmony": (200.0, 5000.0), "detail": (5000.0, 16000.0)}
WIN_SEC = 2.0
HOP_SEC = 1.0
SEG_SEC = 6.0
SILENCE_DB = 35.0


def _load_mono(path: Path) -> tuple[np.ndarray, int]:
    y, sr = sf.read(str(path), always_2d=True, dtype="float32")
    y = y.mean(axis=1)
    return y / (np.sqrt(np.mean(y ** 2)) + 1e-12), sr


def find_hotspots(inst_paths: dict[str, Path], n: int = 5) -> list[dict]:
    """Return up to `n` disagreement segments for one song."""
    methods = list(inst_paths)
    if len(methods) < 2:
        return []

    specs, freqs, hop_t = {}, None, None
    for m, p in inst_paths.items():
        y, sr = _load_mono(p)
        f, t, Z = stft(y, fs=sr, nperseg=2048, noverlap=2048 - 512)
        specs[m] = 10 * np.log10(np.abs(Z) ** 2 + 1e-10)
        freqs, hop_t = f, t[1] - t[0]
    n_frames = min(s.shape[1] for s in specs.values())
    win = int(WIN_SEC / hop_t)
    hop = int(HOP_SEC / hop_t)

    cands: list[dict] = []
    for kind, (lo, hi) in BANDS.items():
        sel = (freqs >= lo) & (freqs < hi)
        band = {m: specs[m][sel, :n_frames] for m in methods}
        loud = np.mean([b.mean(axis=0) for b in band.values()], axis=0)
        ceiling = np.percentile(loud, 99)
        rows = []
        for w0 in range(0, n_frames - win, hop):
            if loud[w0:w0 + win].mean() < ceiling - SILENCE_DB:
                continue
            seg = {m: band[m][:, w0:w0 + win] for m in methods}
            dist = {m: np.mean([np.mean(np.abs(seg[m] - seg[o]))
                                for o in methods if o != m]) for m in methods}
            odd = max(dist, key=dist.get)
            offset = float(np.mean(seg[odd]) - np.mean(
                [np.mean(seg[o]) for o in methods if o != odd]))
            rows.append({"kind": kind, "center": (w0 + win / 2) * hop_t,
                         "raw": float(dist[odd]), "odd": odd,
                         "offset_db": round(offset, 1)})
        if rows:
            med = float(np.median([r["raw"] for r in rows])) or 1e-9
            for r in rows:
                r["score"] = round(r["raw"] / med, 2)
            cands.extend(rows)

    quota = {"harmony": max(1, round(n * 0.6))}
    quota["detail"] = max(0, n - quota["harmony"])
    picked: list[dict] = []

    def overlaps(c):
        return any(abs(c["center"] - p["center"]) < SEG_SEC for p in picked)

    for kind in ("harmony", "detail"):
        for c in sorted((c for c in cands if c["kind"] == kind),
                        key=lambda c: -c["score"]):
            if sum(p["kind"] == kind for p in picked) >= quota[kind]:
                break
            if not overlaps(c):
                picked.append(c)
    for c in sorted(cands, key=lambda c: -c["score"]):   # top up if short
        if len(picked) >= n:
            break
        if not overlaps(c):
            picked.append(c)

    out = []
    for c in sorted(picked, key=lambda c: c["center"]):
        start = max(0.0, c["center"] - SEG_SEC / 2)
        out.append({"start": round(start, 2), "end": round(start + SEG_SEC, 2),
                    "kind": c["kind"], "odd": c["odd"],
                    "offset_db": c["offset_db"], "score": c["score"]})
    return out


def _rms_db(path: Path) -> float:
    y, _ = sf.read(str(path), always_2d=True, dtype="float32")
    return float(20 * np.log10(np.sqrt(np.mean(y ** 2)) + 1e-12))


def _stem(d: Path, name: str) -> Path | None:
    for ext in ("flac", "wav"):
        if (d / f"{name}.{ext}").exists():
            return d / f"{name}.{ext}"
    return None


def write_review_page(dataset: Path, titles: list[str], methods: list[str] | None,
                      n: int = 5, out: Path | None = None) -> Path | None:
    """Detect hotspots for each song and write the blind review page."""
    songs_root = dataset / "songs"
    out = out or dataset / "exports" / "ab_review.html"
    out.parent.mkdir(exist_ok=True)

    def rel(p: Path) -> str:
        return quote(Path("..", p.relative_to(dataset)).as_posix())

    segments, used = [], set()
    for title in titles:
        d = songs_root / title
        avail = sorted(m.name for m in (d / "separations").glob("*") if m.is_dir())
        ms = [m for m in (methods or avail) if m in avail]
        if len(ms) < 2:
            print(f"  {title}: fewer than 2 candidate methods, skipped")
            continue
        insts = {m: _stem(d / "separations" / m, "instrumental") for m in ms}
        vocs = {m: _stem(d / "separations" / m, "vocals") for m in ms}
        print(f"  {title}: analysing {len(ms)} methods...", flush=True)
        hs = find_hotspots(insts, n=n)

        inst_db = {m: _rms_db(p) for m, p in insts.items()}
        voc_db = {m: _rms_db(p) for m, p in vocs.items() if p}
        i_floor = min(inst_db.values())
        v_floor = min(voc_db.values()) if voc_db else 0.0
        opts = {m: {"inst": rel(insts[m]),
                    "iv": round(10 ** ((i_floor - inst_db[m]) / 20), 3),
                    "voc": rel(vocs[m]) if vocs[m] else None,
                    "vv": round(10 ** ((v_floor - voc_db[m]) / 20), 3) if vocs[m] else 1.0}
                for m in ms}
        orig = d / "original.mp3"
        for h in hs:
            segments.append({"song": title, **h, "opts": opts,
                             "orig": rel(orig) if orig.exists() else None})
        used.update(ms)

    if not segments:
        print("No segments — need ≥ 2 candidate separations per song.")
        return None

    template = (Path(__file__).with_name("ab_review_template.html")
                .read_text(encoding="utf-8"))
    data = {"methods": sorted(used), "segments": segments,
            "seed": random.randint(1, 10 ** 9)}
    out.write_text(template.replace("__DATA__", json.dumps(data, ensure_ascii=False)),
                   encoding="utf-8")
    print(f"Wrote {out} ({len(segments)} segments, "
          f"~{len(segments) * len(used) * SEG_SEC / 60:.0f} min of listening)")
    return out
