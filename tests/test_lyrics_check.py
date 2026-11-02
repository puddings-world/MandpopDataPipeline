"""Lyrics-vs-audio suspect rules."""

import numpy as np
import soundfile as sf

from processors.lyrics_check import check_song, judge


def test_judge_rules():
    assert judge(0.92, 10.0) == ("ok", [])
    assert judge(0.55, 10.0)[0] == "suspect"            # lines start where nobody sings
    assert judge(0.9, -30.0)[0] == "suspect"            # LRC longer than the audio (edited upload)
    assert judge(0.9, 150.0)[0] == "suspect"            # audio has a big section the LRC lacks


def test_check_song_on_synthetic_stem(tmp_path):
    sr = 16000
    y = np.zeros(60 * sr, np.float32)
    starts = [5, 12, 19, 26, 33, 40]
    for s in starts:                                    # singing right at each line start
        y[s * sr:(s + 3) * sr] = 0.3
    d = tmp_path / "歌"
    d.mkdir()
    sf.write(d / "vocals.wav", y, sr)
    (d / "lyrics.lrc").write_text("".join(f"[00:{s:02d}.00]第{i}句\n" for i, s in enumerate(starts)),
                                  encoding="utf-8")
    row = check_song(d)
    assert row["verdict"] == "ok" and row["voiced"] > 0.9 and row["tail_sec"] == 20.0
