"""Queue pause/resume, low-impact settings, karaoke promote / revert."""

import json
import os

import pytest

import pipeline
from processors import resources


def test_pause_flag_round_trip(tmp_path):
    assert not resources.is_paused(tmp_path)
    resources.set_paused(tmp_path, True)
    assert resources.is_paused(tmp_path)
    resources.set_paused(tmp_path, False)
    assert not resources.is_paused(tmp_path)


def test_wait_if_paused_blocks_until_resumed(tmp_path):
    resources.set_paused(tmp_path, True)
    naps = []

    def fake_sleep(sec):                      # the user resumes after two polls
        naps.append(sec)
        if len(naps) == 2:
            resources.set_paused(tmp_path, False)

    assert resources.wait_if_paused(tmp_path, poll_sec=1, sleep=fake_sleep) is True
    assert naps == [1, 1]
    assert resources.wait_if_paused(tmp_path, sleep=fake_sleep) is False


def test_thread_cap_defaults_to_a_quarter_of_cores(monkeypatch):
    monkeypatch.delenv("MANDPOP_THREADS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    assert resources.thread_cap() == 4
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    assert resources.thread_cap() == 2                     # never below 2
    monkeypatch.setenv("MANDPOP_THREADS", "6")
    assert resources.thread_cap() == 6


@pytest.fixture
def song(tmp_path, monkeypatch):
    d = tmp_path / "songs" / "晴天"
    d.mkdir(parents=True)
    (d / "original.mp3").write_bytes(b"mix")
    (d / "instrumental.m4a").write_bytes(b"htdemucs-inst")
    (d / "vocals.m4a").write_bytes(b"htdemucs-voc")
    (d / "metadata.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pipeline, "DATASET_PATH", tmp_path)
    monkeypatch.setattr(pipeline, "songs_dir", lambda: tmp_path / "songs")
    monkeypatch.setattr(pipeline, "load_raw", lambda: {"晴天": {"processed": True}})
    raw = {}
    monkeypatch.setattr(pipeline, "upsert_raw", lambda t, **kw: raw.setdefault(t, {}).update(kw))

    import processors.separate as sep
    import processors.encode as enc

    def fake_separate(src, out, method, output_format):
        out.mkdir(parents=True, exist_ok=True)
        (out / "instrumental.flac").write_bytes(b"karaoke-inst")
        (out / "vocals.flac").write_bytes(b"karaoke-lead")
    monkeypatch.setattr(sep, "separate_to_dir", fake_separate)
    monkeypatch.setattr(enc, "encode_m4a",
                        lambda src, dst, overwrite=False: dst.write_bytes(b"m4a:" + src.read_bytes()))
    return d, raw


def test_promote_switches_app_files_and_keeps_a_backup(song):
    d, raw = song
    counts = pipeline.promote_karaoke(method="karaoke_becruily")
    assert counts["promoted"] == 1
    assert (d / "instrumental.m4a").read_bytes() == b"m4a:karaoke-inst"
    assert (d / "vocals.m4a").read_bytes() == b"m4a:karaoke-lead"           # lead-only guide
    backup = d / "separations" / "htdemucs_ft"
    assert (backup / "instrumental.m4a").read_bytes() == b"htdemucs-inst"
    meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
    assert meta["instrumental_method"] == "karaoke_becruily"
    assert raw["晴天"]["instrumental_method"] == "karaoke_becruily"
    # a second batch run skips songs already promoted
    assert pipeline.promote_karaoke(method="karaoke_becruily")["skipped"] == 1


def test_revert_restores_the_htdemucs_backing(song):
    d, raw = song
    pipeline.promote_karaoke(method="karaoke_becruily")
    assert pipeline.promote_karaoke(revert=True)["reverted"] == 1
    assert (d / "instrumental.m4a").read_bytes() == b"htdemucs-inst"
    assert (d / "vocals.m4a").read_bytes() == b"htdemucs-voc"
    assert raw["晴天"]["instrumental_method"] == "htdemucs_ft"


def test_ignore_pause_lets_one_job_through(tmp_path, monkeypatch):
    resources.set_paused(tmp_path, True)
    monkeypatch.setenv("MANDPOP_IGNORE_PAUSE", "1")
    assert resources.wait_if_paused(tmp_path, sleep=lambda s: pytest.fail("should not wait")) is False
