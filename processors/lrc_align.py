"""Per-line LRC re-timing against the vocals stem (alignment "Layer 1.5").

Layer 1 only shifts the whole LRC so the FIRST line matches. That leaves two
common failures that make lyrics drift further from the singing as the song
goes on:

  * tempo mismatch — re-uploads are often sped up 1–4 % (Content ID evasion)
    while the LRC was timed on the album master;
  * structure mismatch — an MV with a longer intro or an extra interlude, so
    every line after the insertion is late by a constant.

Both are fixed by assigning every lyric line to a vocal onset with dynamic
programming:

    onset envelope  E(t)  = positive dB rise of the vocals stem over 200 ms
    candidates            = local maxima of E (≥ 0.25 s apart)
    line i → candidate k  maximizes  Σ E(p_k)
                          − λ · Σ log²((p_k − p_j) / (t_i − t_j))   (gap ratio)
                          − μ · Σ ((p_k − prior_i) / σ)²            (global fit)

The gap term keeps neighbouring lines in their LRC proportions but is
relaxed across long gaps (instrumental breaks), where arrangements differ;
a gap far outside the LRC proportion (an inserted or cut passage) costs a
flat INSERT_PEN, so it is chosen only when the lines after it then land.
The prior is the best global `a·t + b` fit, so the DP stays near a sane
mapping. The result is applied only when it is clearly better than the LRC
as-is (see `should_apply`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
import soundfile as sf

HOP = 0.02                  # envelope frame (s)
ONSET_RISE = 0.06           # onset = dB rise over this window; longer windows peak late
PEAK_MIN_GAP = 0.25         # candidate onsets at least this far apart (s)
PEAK_MIN = 0.04             # ignore onsets weaker than this (normalized)
LAMBDA = 6                 # gap-ratio penalty
LONG_GAP = 8.0              # LRC gaps longer than this: arrangement may differ
LONG_GAP_RELAX = 0.15       # … so their gap penalty is scaled down
MAX_RATIO = 2.5             # gap ratios beyond [1/MAX_RATIO, MAX_RATIO] …
INSERT_PEN = 1.5            # … cost a flat penalty: an inserted / cut passage
                            # (MV dialogue, extended interlude) — worth ~1.5 onsets
PRIOR_SIGMA = 12.0          # global-fit prior width (s)
MU = 0.1


@dataclass
class AlignResult:
    old: np.ndarray          # LRC line starts (s)
    new: np.ndarray          # aligned starts (s)
    a: float                 # global fit used as prior
    b: float
    score_old: float         # mean onset strength at old / new starts
    score_new: float
    strong_old: float        # fraction of lines landing on a strong onset
    strong_new: float

    @property
    def max_shift(self) -> float:
        return float(np.max(np.abs(self.new - self.old))) if len(self.old) else 0.0


# ─── signal ─────────────────────────────────────────────────────────────

def onset_envelope(vocals_path) -> np.ndarray:
    y, sr = sf.read(str(vocals_path), always_2d=True, dtype="float32")
    return envelope_from_samples(y.mean(axis=1), sr)


def envelope_from_samples(y: np.ndarray, sr: int) -> np.ndarray:
    hop = int(sr * HOP)
    n = len(y) // hop
    rms = np.sqrt((y[: n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    db = np.maximum(db, db.max() - 50)
    k = int(ONSET_RISE / HOP)
    prev = np.concatenate([np.full(k, db[0]), db[:-k]])
    on = np.clip(db - prev, 0, None)
    on = np.convolve(on, np.ones(3) / 3, mode="same")
    return on / (on.max() + 1e-9)


def voiced_mask(y: np.ndarray, sr: int, rel: float = 0.3) -> np.ndarray:
    """Per-frame "someone is singing": RMS above `rel` × the stem's RMS."""
    hop = int(sr * HOP)
    n = len(y) // hop
    rms = np.sqrt((y[: n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
    return rms > rel * np.sqrt(np.mean(y ** 2))


def voiced_after(mask: np.ndarray, starts: np.ndarray, window: float = 1.0) -> float:
    """Mean voiced share of the first `window` s after each line start.

    Independent of the onset score the DP optimizes, so it is the guard
    that a re-timing really put lines where the singing starts.
    """
    w = int(window / HOP)
    vals = [mask[int(t / HOP):int(t / HOP) + w].mean() if 0 <= int(t / HOP) < len(mask) else 0.0
            for t in starts]
    return float(np.mean(vals)) if vals else 0.0


def peaks(env: np.ndarray) -> np.ndarray:
    """Frame indices of local onset maxima, strongest kept within PEAK_MIN_GAP."""
    gap = max(1, int(PEAK_MIN_GAP / HOP))
    idx = [i for i in range(1, len(env) - 1)
           if env[i] >= PEAK_MIN and env[i] >= env[i - 1] and env[i] > env[i + 1]]
    idx.sort(key=lambda i: -env[i])
    taken = np.zeros(len(env), bool)
    out = []
    for i in idx:
        if not taken[max(0, i - gap):i + gap].any():
            taken[i] = True
            out.append(i)
    return np.array(sorted(out), dtype=int)


def strength_at(env: np.ndarray, times: np.ndarray, tol: float = 0.15) -> np.ndarray:
    """Max envelope within ±tol of each time."""
    w = int(tol / HOP)
    out = []
    for t in times:
        c = int(round(t / HOP))
        lo, hi = max(0, c - w), min(len(env), c + w + 1)
        out.append(env[lo:hi].max() if hi > lo else 0.0)
    return np.array(out)


# ─── fitting ────────────────────────────────────────────────────────────

def global_fit(starts: np.ndarray, env: np.ndarray) -> tuple[float, float]:
    """Best a·t + b (a ∈ [0.85, 1.15], |b| ≤ 20 s) by summed onset strength."""
    n = len(env)
    bs = np.arange(-20.0, 20.0, HOP)
    best = (-1.0, 1.0, 0.0)
    for a in np.arange(0.85, 1.15001, 0.0025):
        idx = np.round((a * starts[:, None] + bs[None, :]) / HOP).astype(int)
        ok = (idx >= 0) & (idx < n)
        s = np.where(ok, env[np.clip(idx, 0, n - 1)], 0).sum(axis=0)
        j = int(s.argmax())
        # prefer a = 1 on ties (no evidence of a tempo change)
        if s[j] > best[0] * 1.002 or (abs(a - 1) < 1e-9 and s[j] >= best[0]):
            best = (float(s[j]), float(a), float(bs[j]))
    return best[1], best[2]


def align(starts: np.ndarray, env: np.ndarray) -> AlignResult:
    """Assign each LRC line start to a vocal onset (see module docstring)."""
    starts = np.asarray(starts, float)
    a, b = global_fit(starts, env)
    cand = peaks(env)
    if len(starts) < 2 or len(cand) < len(starts):
        new = starts.copy()
    else:
        new = _dp(starts, cand * HOP, env[cand], a * starts + b)
    s_old, s_new = strength_at(env, starts), strength_at(env, new)
    return AlignResult(starts, new, a, b, float(s_old.mean()), float(s_new.mean()),
                       float((s_old > 0.3).mean()), float((s_new > 0.3).mean()))


def _dp(starts, ctimes, cstr, prior) -> np.ndarray:
    n, m = len(starts), len(ctimes)
    prior_cost = MU * ((ctimes[None, :] - prior[:, None]) / PRIOR_SIGMA) ** 2   # n × m
    score = np.full((n, m), -np.inf)
    back = np.zeros((n, m), dtype=int)
    score[0] = cstr - prior_cost[0]
    for i in range(1, n):
        g_lrc = max(starts[i] - starts[i - 1], 0.05)
        lam = LAMBDA * (LONG_GAP_RELAX if g_lrc > LONG_GAP else 1.0)
        gap = ctimes[None, :] - ctimes[:, None]                 # j × k
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = gap / g_lrc
            pen = lam * np.log(ratio) ** 2
        lo = 1 / MAX_RATIO if g_lrc <= LONG_GAP else 0.2
        hi = MAX_RATIO if g_lrc <= LONG_GAP else 5.0
        pen = np.where((ratio >= lo) & (ratio <= hi), pen, INSERT_PEN)
        pen = np.where(gap >= 0.5, pen, np.inf)          # lines can't collapse
        tot = score[i - 1][:, None] - pen                        # j × k
        back[i] = np.argmax(tot, axis=0)
        score[i] = tot[back[i], np.arange(m)] + cstr - prior_cost[i]
    k = int(np.argmax(score[-1]))
    if not np.isfinite(score[-1, k]):
        return starts.copy()
    path = [k]
    for i in range(n - 1, 0, -1):
        k = back[i, k]
        path.append(k)
    return ctimes[np.array(path[::-1])]


def should_apply(r: AlignResult) -> bool:
    """Only re-time when the evidence is clear.

    The aligned starts must land on onsets much more often than the LRC
    as-is, and most lines must land on a strong onset afterwards; small
    jitter (< 0.3 s everywhere) isn't worth rewriting the file for.
    """
    return (r.max_shift >= 0.3 and r.strong_new >= 0.6
            and r.score_new >= r.score_old * 1.15 and r.strong_new > r.strong_old + 0.1)


# ─── LRC rewrite ────────────────────────────────────────────────────────

_TS = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")


def _sec(m: re.Match) -> float:
    frac = m.group(3) or "0"
    return int(m.group(1)) * 60 + int(m.group(2)) + int(frac) / 10 ** len(frac)


def _fmt(t: float) -> str:
    t = max(t, 0.0)
    mm = int(t // 60)
    return f"[{mm:02d}:{t - mm * 60:05.2f}]"


def parse_starts(text: str) -> np.ndarray:
    """Sorted unique start times of the sung lines (credits / tags excluded)."""
    from processors.alignment import _is_meta_line
    out = set()
    for line in text.splitlines():
        stamps = list(_TS.finditer(line))
        body = _TS.sub("", line).strip()
        if stamps and body and not _is_meta_line(body):
            out.update(round(_sec(m), 3) for m in stamps)
    return np.array(sorted(out))


def retime_lrc(text: str, old: np.ndarray, new: np.ndarray) -> str:
    """Rewrite every timestamp through the piecewise-linear old → new map.

    Lyric starts move exactly to their aligned time; anything between (or
    metadata before the first line) is interpolated / shifted with its
    neighbours, so repeated-timestamp lines and credits stay consistent.
    """
    order = np.argsort(old)
    xo, xn = np.asarray(old, float)[order], np.asarray(new, float)[order]
    xo, keep = np.unique(xo, return_index=True)
    xn = xn[keep]

    def mapping(t: float) -> float:
        if t <= xo[0]:
            return t + (xn[0] - xo[0])
        if t >= xo[-1]:
            return t + (xn[-1] - xo[-1])
        return float(np.interp(t, xo, xn))

    return _TS.sub(lambda m: _fmt(mapping(_sec(m))), text)
