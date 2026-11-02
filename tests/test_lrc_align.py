"""Per-line LRC re-timing: recovers tempo and structure mismatches."""

import numpy as np

from processors.lrc_align import HOP, align, parse_starts, retime_lrc, should_apply, voiced_after


def lrc_times(n=30, seed=0):
    rng = np.random.default_rng(seed)
    gaps = rng.uniform(2.5, 5.0, n)
    gaps[15] = 14.0                         # instrumental break in the LRC version
    return 10 + np.cumsum(gaps)


def envelope(onsets, length_s, seed=1, noise_peaks=60):
    rng = np.random.default_rng(seed)
    env = np.zeros(int(length_s / HOP))
    for t in onsets:                        # sung line starts: strong onsets
        i = int(round(t / HOP))
        env[max(0, i - 2):i + 3] = [0.5, 0.8, 1.0, 0.8, 0.5][: len(env[max(0, i - 2):i + 3])]
    for t in rng.uniform(0, length_s, noise_peaks):   # mid-line syllables: weaker onsets
        i = int(round(t / HOP))
        env[i] = max(env[i], rng.uniform(0.1, 0.35))
    return env


def test_already_aligned_lrc_is_left_alone():
    t = lrc_times()
    r = align(t, envelope(t, t[-1] + 20))
    assert np.allclose(r.new, t, atol=0.05)
    assert not should_apply(r)


def test_sped_up_upload_is_retimed():
    t = lrc_times()
    sung = 0.5 + t / 1.03                   # audio 3 % faster than the LRC master
    r = align(t, envelope(sung, sung[-1] + 20))
    assert abs(r.a - 1 / 1.03) < 0.006
    assert np.mean(np.abs(r.new - sung) < 0.06) > 0.9
    assert should_apply(r)


def test_inserted_interlude_is_absorbed():
    t = lrc_times()
    sung = t + 6.0                          # longer intro
    sung[16:] += 9.0                        # MV adds 9 s before line 17
    r = align(t, envelope(sung, sung[-1] + 20))
    assert np.mean(np.abs(r.new - sung) < 0.06) > 0.9
    assert should_apply(r)


def test_retime_moves_lines_and_keeps_text():
    text = "[ti:晴天]\n[00:01.00]作詞：周杰倫\n[00:10.00]第一句\n[00:20.00][00:40.00]副歌\n[00:30.00]第三句\n"
    old = parse_starts(text)
    assert list(old) == [10.0, 20.0, 30.0, 40.0]
    out = retime_lrc(text, old, old * 0.95 + 2)
    assert "[ti:晴天]" in out
    assert "[00:11.50]第一句" in out
    assert "[00:21.00][00:40.00]副歌" in out          # both stamps of a repeated line
    assert "[00:30.50]第三句" in out
    assert "[00:02.50]作詞" in out                   # credits shift with the first line


def test_audit_song_retimes_a_drifting_song(tmp_path):
    import soundfile as sf
    from processors.alignment import audit_song

    sr = 16000
    t = lrc_times()
    sung = t / 1.03                                # vocals 3 % faster than the LRC
    rng = np.random.default_rng(3)
    y = np.zeros(int((sung[-1] + 10) * sr), dtype=np.float32)
    for s in sung:                                 # a 1.5 s sung phrase per line
        i = int(s * sr)
        y[i:i + int(1.5 * sr)] = 0.3 * rng.standard_normal(int(1.5 * sr)).astype(np.float32)
    d = tmp_path / "測試歌"
    d.mkdir()
    sf.write(d / "vocals.wav", y, sr)
    lrc = "".join(f"[{int(x // 60):02d}:{x % 60:05.2f}]第{i}句\n" for i, x in enumerate(t))
    (d / "lyrics.lrc").write_text(lrc, encoding="utf-8")

    report = audit_song(d, run_layer2=False, auto_fix=True)

    assert report.retime_max_shift_sec is not None
    assert report.overall == "fixed"
    assert (d / "lyrics.lrc.orig").read_text(encoding="utf-8") == lrc
    new = parse_starts((d / "lyrics.lrc").read_text(encoding="utf-8"))
    assert np.mean(np.abs(new - sung) < 0.1) > 0.9


def test_voiced_after_measures_singing_right_after_line_starts():
    mask = np.zeros(int(20 / HOP), bool)
    mask[int(5 / HOP):int(7 / HOP)] = True             # singing 5–7 s
    assert voiced_after(mask, np.array([5.0])) == 1.0
    assert voiced_after(mask, np.array([8.0])) == 0.0
    assert abs(voiced_after(mask, np.array([4.5])) - 0.5) < 0.05


def test_op_sp_labels_are_credits_but_lyrics_starting_with_sp_are_not():
    from processors.alignment import _is_meta_line
    assert _is_meta_line("OP：腾煌星象")
    assert _is_meta_line("SP: 某某")
    assert not _is_meta_line("spring is coming")
    assert not _is_meta_line("填不满半排观众的电影")


def test_intro_speech_does_not_fail_an_aligned_song(tmp_path):
    # 小時候: MV dialogue at 6 s, first sung line at 87 s, LRC already right.
    import soundfile as sf
    from processors.alignment import audit_song
    sr = 16000
    t = lrc_times() + 77                            # lines start ≈ 87 s
    rng = np.random.default_rng(5)
    y = np.zeros(int((t[-1] + 10) * sr), dtype=np.float32)
    y[6 * sr:9 * sr] = 0.2 * rng.standard_normal(3 * sr).astype(np.float32)   # intro speech
    for s in t:
        i = int(s * sr)
        y[i:i + int(1.5 * sr)] = 0.3 * rng.standard_normal(int(1.5 * sr)).astype(np.float32)
    d = tmp_path / "小時候"
    d.mkdir()
    sf.write(d / "vocals.wav", y, sr)
    lrc = "".join(f"[{int(x // 60):02d}:{x % 60:05.2f}]第{i}句\n" for i, x in enumerate(t))
    (d / "lyrics.lrc").write_text(lrc, encoding="utf-8")
    report = audit_song(d, run_layer2=False, auto_fix=True)
    assert report.overall == "ok"
    assert (d / "lyrics.lrc").read_text(encoding="utf-8") == lrc   # untouched
