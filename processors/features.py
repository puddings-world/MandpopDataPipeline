"""Audio feature extraction — `features.json` producer (README spec).

Everything is computed LOCALLY from the separated stems (no third-party
audio API), so features are reproducible by anyone who re-runs the
pipeline on their own machine:

    bpm               librosa beat tracking on the reconstructed mix
    key / mode        Krumhansl-Schmuckler correlation on chroma
    loudness_lufs     ffmpeg ebur128 on vocals+instrumental amix
    energy            mean frame RMS in dBFS mapped to [0, 1]
    dynamic_range_db  p95 − p10 of frame RMS (arrangement dynamics)
    vocal_ratio       vocals-stem energy / total stem energy
    sections          heuristic v1 segmentation (intro/verse/chorus/…)

The mix is reconstructed by summing the two stems — Demucs stems add
back to (approximately) the original, which is good enough for tempo /
key / loudness and avoids keeping original.mp3 around after compact.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

FEATURES_VERSION = 1

# Krumhansl-Schmuckler key profiles (major / minor).
_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
                   2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
                   2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
_PITCHES = ["C", "C#", "D", "D#", "E", "F",
            "F#", "G", "G#", "A", "A#", "B"]


def _require_librosa():
    try:
        import librosa  # noqa: F401
        return librosa
    except ImportError:
        raise RuntimeError(
            "librosa is required for feature extraction: pip install librosa")


def _find_stem(song_dir: Path, name: str) -> Path | None:
    for ext in (".flac", ".wav"):
        p = song_dir / f"{name}{ext}"
        if p.exists():
            return p
    return None


def _estimate_key(chroma_mean: np.ndarray) -> tuple[str, str, float]:
    """Correlate mean chroma against all 24 rotated K-S profiles."""
    best = ("C", "major", -2.0)
    scores = []
    for mode, profile in (("major", _MAJOR), ("minor", _MINOR)):
        for shift in range(12):
            rotated = np.roll(profile, shift)
            r = float(np.corrcoef(chroma_mean, rotated)[0, 1])
            scores.append(r)
            if r > best[2]:
                best = (_PITCHES[shift], mode, r)
    # Confidence = margin of the winner over the runner-up, squashed to [0,1].
    scores.sort(reverse=True)
    margin = scores[0] - scores[1]
    confidence = float(min(1.0, max(0.0, 0.5 + margin * 5)))
    return best[0], best[1], confidence


def _loudness_lufs(vocals: Path, instrumental: Path) -> float | None:
    """Integrated LUFS of the reconstructed mix via ffmpeg ebur128."""
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(vocals), "-i", str(instrumental),
        "-filter_complex", "[0:a][1:a]amix=inputs=2:normalize=0,ebur128",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    # Summary block ends with "I: -9.3 LUFS".
    matches = re.findall(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", result.stderr)
    return float(matches[-1]) if matches else None


def _label_sections(bound_times: list[float], seg_chroma: list[np.ndarray],
                    seg_vocal_rms: list[float], seg_energy: list[float],
                    duration: float) -> list[dict]:
    """Heuristic v1 labelling: cluster segments by chroma similarity;
    the most-repeated high-energy cluster = chorus, first segment with
    little vocal energy = intro, late singletons = bridge."""
    n = len(bound_times)
    if n == 0:
        return []

    # Greedy clustering on chroma correlation.
    cluster_of = [-1] * n
    reps: list[np.ndarray] = []
    for i, c in enumerate(seg_chroma):
        for ci, rep in enumerate(reps):
            denom = (np.linalg.norm(c) * np.linalg.norm(rep)) or 1e-9
            if float(np.dot(c, rep) / denom) > 0.93:
                cluster_of[i] = ci
                break
        if cluster_of[i] == -1:
            cluster_of[i] = len(reps)
            reps.append(c)

    counts = {ci: cluster_of.count(ci) for ci in set(cluster_of)}
    # Chorus = the repeated cluster with the highest mean energy.
    chorus_cluster = None
    best_e = -1.0
    for ci, cnt in counts.items():
        if cnt < 2:
            continue
        e = float(np.mean([seg_energy[i] for i in range(n)
                           if cluster_of[i] == ci]))
        if e > best_e:
            best_e, chorus_cluster = e, ci

    vocal_floor = max(seg_vocal_rms) * 0.15 if seg_vocal_rms else 0.0
    labels = []
    for i in range(n):
        start = bound_times[i]
        if i == 0 and seg_vocal_rms[i] <= vocal_floor:
            labels.append("intro")
        elif cluster_of[i] == chorus_cluster:
            labels.append("chorus")
        elif i == n - 1 and seg_vocal_rms[i] <= vocal_floor:
            labels.append("outro")
        elif counts[cluster_of[i]] == 1 and start > duration * 0.4:
            labels.append("bridge")
        else:
            labels.append("verse")
    return [{"label": lab, "start_sec": round(t, 2)}
            for lab, t in zip(labels, bound_times)]


def extract_features(song_dir: Path) -> dict | None:
    """Compute the features.json document for one song.

    Returns None when the song has no stems on disk (nothing to analyze).
    """
    librosa = _require_librosa()

    vocals_p = _find_stem(song_dir, "vocals")
    inst_p = _find_stem(song_dir, "instrumental")
    if vocals_p is None or inst_p is None:
        return None

    sr = 22050
    y_voc, _ = librosa.load(str(vocals_p), sr=sr, mono=True)
    y_ins, _ = librosa.load(str(inst_p), sr=sr, mono=True)
    m = min(len(y_voc), len(y_ins))
    y_voc, y_ins = y_voc[:m], y_ins[:m]
    y_mix = y_voc + y_ins
    duration = m / sr

    # ── tempo ──
    tempo = librosa.beat.beat_track(y=y_mix, sr=sr)[0]
    bpm = float(np.atleast_1d(tempo)[0])

    # ── key (instrumental only: cleaner harmony, no vocal vibrato) ──
    chroma = librosa.feature.chroma_cqt(y=y_ins, sr=sr)
    key, mode, key_conf = _estimate_key(chroma.mean(axis=1))

    # ── energy / dynamics (frame RMS of the mix) ──
    rms = librosa.feature.rms(y=y_mix)[0]
    active = rms[rms > np.max(rms) * 0.02]          # drop lead-in silence
    rms_db = 20 * np.log10(np.maximum(active, 1e-6))
    energy = float(np.clip((np.mean(rms_db) + 40) / 40, 0, 1))
    dyn_range = float(np.percentile(rms_db, 95) - np.percentile(rms_db, 10))

    # ── vocal presence ──
    e_voc = float(np.sum(y_voc.astype(np.float64) ** 2))
    e_ins = float(np.sum(y_ins.astype(np.float64) ** 2))
    vocal_ratio = e_voc / (e_voc + e_ins) if (e_voc + e_ins) > 0 else 0.0

    # ── loudness (proper LUFS via ffmpeg, on the stems as-is) ──
    lufs = _loudness_lufs(vocals_p, inst_p)

    # ── sections (beat-synchronous agglomerative segmentation) ──
    sections = []
    try:
        hop = 512
        n_segments = int(np.clip(duration / 25, 4, 12))
        mfcc = librosa.feature.mfcc(y=y_mix, sr=sr, n_mfcc=13)
        feats = np.vstack([librosa.util.normalize(chroma, axis=0),
                           librosa.util.normalize(mfcc, axis=0)])
        bounds = librosa.segment.agglomerative(feats, n_segments)
        bound_times = librosa.frames_to_time(bounds, sr=sr,
                                             hop_length=hop).tolist()
        rms_voc = librosa.feature.rms(y=y_voc)[0]
        rms_mix = librosa.feature.rms(y=y_mix)[0]
        seg_chroma, seg_vrms, seg_energy = [], [], []
        frame_bounds = list(bounds) + [chroma.shape[1]]
        for i in range(len(bounds)):
            a, b = frame_bounds[i], max(frame_bounds[i] + 1, frame_bounds[i + 1])
            seg_chroma.append(chroma[:, a:b].mean(axis=1))
            seg_vrms.append(float(rms_voc[a:min(b, len(rms_voc))].mean()))
            seg_energy.append(float(rms_mix[a:min(b, len(rms_mix))].mean()))
        sections = _label_sections(bound_times, seg_chroma, seg_vrms,
                                   seg_energy, duration)
    except Exception:
        pass                              # sections stay [] — heuristic only

    return {
        "version": FEATURES_VERSION,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "source": {"vocals": vocals_p.name, "instrumental": inst_p.name},
        "duration_sec": round(duration, 2),
        "bpm": round(bpm, 1),
        "key": key,
        "mode": mode,
        "key_confidence": round(key_conf, 2),
        "loudness_lufs": round(lufs, 1) if lufs is not None else None,
        "energy": round(energy, 2),
        "dynamic_range_db": round(dyn_range, 1),
        "vocal_ratio": round(vocal_ratio, 2),
        "sections": sections,
    }


def write_features(song_dir: Path, overwrite: bool = False) -> dict | None:
    """Compute + persist features.json for one song. Returns the doc."""
    out_path = song_dir / "features.json"
    if out_path.exists() and not overwrite:
        return json.loads(out_path.read_text(encoding="utf-8"))
    doc = extract_features(song_dir)
    if doc is not None:
        out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    return doc
