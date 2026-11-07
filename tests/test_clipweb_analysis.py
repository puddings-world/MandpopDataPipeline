"""clipweb audio analysis: vocal onsets, audible span, chorus candidates."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "yt_clip"))

from clipweb import (  # noqa: E402
    FRAME_HOP_S,
    SR,
    _runs_above,
    _smooth,
    analyze_samples,
)


def tone(seconds, freq, amp=0.3, sr=SR):
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def chord(seconds, root_hz, amp=0.3):
    """A triad, so chroma has real harmonic content to match on."""
    return (tone(seconds, root_hz, amp)
            + tone(seconds, root_hz * 2 ** (4 / 12), amp)
            + tone(seconds, root_hz * 2 ** (7 / 12), amp)).astype(np.float32)


def silence(seconds, sr=SR):
    return np.zeros(int(seconds * sr), dtype=np.float32)


def song(sections):
    """Concatenate (seconds, root_hz, amp) sections; amp 0 means silence."""
    parts = [silence(s) if a == 0 else chord(s, f, a) for s, f, a in sections]
    return np.concatenate(parts)


# --- primitives ---------------------------------------------------------


def test_runs_above_finds_contiguous_spans():
    mask = np.array([0, 1, 1, 1, 0, 1, 1, 0, 1, 1, 1, 1], dtype=bool)
    assert _runs_above(mask, 3) == [(1, 4), (8, 12)]


def test_runs_above_includes_runs_touching_either_edge():
    mask = np.array([1, 1, 1, 0, 1, 1, 1], dtype=bool)
    assert _runs_above(mask, 3) == [(0, 3), (4, 7)]


def test_runs_above_drops_runs_shorter_than_minimum():
    mask = np.array([1, 1, 0, 1, 1, 1, 1], dtype=bool)
    assert _runs_above(mask, 3) == [(3, 7)]


def test_smooth_preserves_length_and_flattens_spikes():
    a = np.zeros(100, dtype=np.float32)
    a[50] = 1.0
    sm = _smooth(a, 0.5)
    assert len(sm) == len(a)
    assert sm[50] < a[50]
    assert sm[48] > 0  # energy spread into neighbours


# --- vocal onsets -------------------------------------------------------


def test_onset_lands_where_sound_starts_after_silence():
    x = np.concatenate([silence(8), tone(6, 440)])
    r = analyze_samples(x)
    assert r["vocal_start"] is not None
    assert abs(r["vocal_start"] - 8.0) < 0.6


def test_each_phrase_after_a_gap_gets_its_own_onset():
    gaps = [4.0, 20.0, 36.0]
    x = silence(4)
    for _ in gaps:
        x = np.concatenate([x, tone(12, 440), silence(4)])
    found = [o["t"] for o in analyze_samples(x)["onsets"]]
    for expected in gaps:
        assert any(abs(t - expected) < 1.0 for t in found), (expected, found)


def test_onsets_are_spaced_apart_not_one_per_frame():
    x = np.concatenate([silence(5), tone(20, 440)])
    ts = [o["t"] for o in analyze_samples(x)["onsets"]]
    assert all(b - a >= 2.5 for a, b in zip(ts, ts[1:]))


def test_steady_tone_with_no_entry_reports_no_vocal_start():
    assert analyze_samples(tone(30, 440))["vocal_start"] is None


def test_too_short_input_degrades_to_empty_result():
    r = analyze_samples(tone(0.5, 440))
    assert r["onsets"] == [] and r["chorus"] is None


# --- audible span -------------------------------------------------------


def test_audible_span_trims_leading_and_trailing_silence():
    x = np.concatenate([silence(6), tone(20, 440), silence(7)])
    r = analyze_samples(x)
    assert abs(r["audio_start"] - 6.0) < 0.8
    assert abs(r["audio_end"] - 26.0) < 0.8


# --- chorus candidates --------------------------------------------------


def verse_chorus_song():
    """intro, verse A, chorus B (loud), verse A, chorus B — B repeats at a fixed lag."""
    return song([
        (10, 220.0, 0.10),   # intro
        (2, 0, 0),
        (24, 220.0, 0.18),   # verse 1
        (24, 330.0, 0.42),   # chorus 1  (t=36..60)
        (24, 220.0, 0.18),   # verse 2
        (24, 330.0, 0.42),   # chorus 2  (t=84..108)
        (6, 0, 0),
    ])


def test_chorus_candidates_are_found_and_ordered_in_time():
    r = analyze_samples(verse_chorus_song())
    cands = r["chorus_candidates"]
    assert cands, "expected at least one repeated section"
    starts = [c["start"] for c in cands]
    assert starts == sorted(starts)
    assert all(c["end"] > c["start"] for c in cands)


def test_selected_chorus_is_the_first_one_not_the_last():
    r = analyze_samples(verse_chorus_song())
    assert r["chorus"] is not None
    # The two choruses sit at 36-60 s and 84-108 s; the pick must be the earlier.
    assert r["chorus"]["start"] < 75.0
    assert r["chorus"] is r["chorus_candidates"][r["chorus_index"]]


def test_selected_chorus_starts_after_the_first_sung_line():
    r = analyze_samples(verse_chorus_song())
    assert r["vocal_start"] is not None
    assert r["chorus"]["start"] >= r["vocal_start"]


def test_chorus_absent_when_nothing_repeats():
    """A slow chirp never revisits a pitch, so no section can recur."""
    seconds = 100
    t = np.arange(int(seconds * SR)) / SR
    f = 110.0 * 2 ** (3.0 * t / seconds)        # sweeps 3 octaves, monotonic
    phase = 2 * np.pi * np.cumsum(f) / SR
    x = np.concatenate([silence(5), (0.3 * np.sin(phase)).astype(np.float32)])
    r = analyze_samples(x)
    assert r["chorus"] is None
    assert r["chorus_index"] is None


def test_result_always_carries_every_key():
    for x in (tone(0.5, 440), tone(30, 440), verse_chorus_song()):
        r = analyze_samples(x)
        assert set(r) == {"onsets", "vocal_start", "audio_start", "audio_end",
                          "chorus", "chorus_candidates", "chorus_index"}


def test_onset_times_are_within_the_clip():
    x = verse_chorus_song()
    r = analyze_samples(x)
    dur = len(x) / SR
    assert all(0 <= o["t"] <= dur for o in r["onsets"])
    assert all(0 <= c["start"] < c["end"] <= dur + FRAME_HOP_S
               for c in r["chorus_candidates"])
