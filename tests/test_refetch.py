"""Re-fetching a song onto a new video, and which Doc songs still need adding."""

import pipeline


def test_needs_add_covers_records_that_only_hold_presets():
    assert pipeline.needs_add(None)
    assert pipeline.needs_add({"title": "Mojito", "key_offset": 0})   # presets only
    assert pipeline.needs_add({"status": "pending"})
    assert not pipeline.needs_add({"status": "done"})


def test_wipe_song_audio_drops_stale_separations(tmp_path):
    d = tmp_path / "雨後"
    (d / "separations" / "karaoke_becruily").mkdir(parents=True)
    (d / "separations" / "karaoke_becruily" / "instrumental.flac").write_bytes(b"old video")
    for name in ("original.mp3", "instrumental.wav", "instrumental.m4a", "vocals.m4a", "lyrics.lrc"):
        (d / name).write_bytes(b"old")
    (d / "metadata.json").write_text("{}", encoding="utf-8")

    pipeline.wipe_song_audio(d)

    assert not (d / "separations").exists()        # karaoke batch must re-separate
    assert not any((d / n).exists() for n in ("original.mp3", "instrumental.m4a", "vocals.m4a"))
    assert (d / "metadata.json").exists()
