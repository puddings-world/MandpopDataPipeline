"""Library video audit: verdict rules and pinning into notes_inbox.txt."""

import pytest

import pipeline
from crawlers.youtube import title_names_song
from processors import inbox_http
from processors.notes_ingest import parse_notes
from processors.video_audit import classify, rank_candidates, reclassify


def vid(id_, title, views, channel=""):
    return {"id": id_ + "x" * (11 - len(id_)), "title": title, "view_count": views,
            "channel": channel, "duration": 240}


OFFICIAL = vid("off", "周杰倫 Jay Chou【晴天】Official MV", 300_000_000, "周杰倫 Jay Chou")
LIVE = vid("live", "周杰倫【晴天】Live 演唱會", 2_000_000, "fan")
COVER = vid("cov", "晴天 cover 鋼琴版", 5_000_000, "piano guy")
OTHER_SONG = vid("oth", "周杰倫【稻香】Official MV", 400_000_000, "周杰倫 Jay Chou")


def test_title_names_song_needs_an_exact_slot():
    assert title_names_song("晴天", "周杰倫【晴天】Official MV")
    assert title_names_song("晴天", "周杰倫 - 晴天")
    assert not title_names_song("晴天", "晴天娃娃 vlog")


def test_rank_drops_other_songs_and_covers_and_puts_official_first():
    ranked = rank_candidates("晴天", [LIVE, COVER, OTHER_SONG, OFFICIAL])
    assert [c["id"][:3] for c in ranked] == ["off", "liv"]


def test_live_source_with_same_artist_official_is_auto():
    r = classify("晴天", "周杰倫 Jay Chou", LIVE, [OFFICIAL, LIVE])
    assert r["verdict"] == "auto"
    assert r["best"]["id"] == OFFICIAL["id"]
    assert "live / talent show / fan video" in r["reasons"]


def test_unknown_artist_is_only_suggested():
    # e.g. 故事 credited to a kids channel: same-name songs exist, a human picks.
    r = classify("晴天", "宝宝巴士", COVER, [OFFICIAL])
    assert r["verdict"] == "suggest"
    assert any("artist unconfirmed" in x for x in r["reasons"])


def test_current_best_is_ok_and_pins_are_never_touched():
    assert classify("晴天", "周杰倫", OFFICIAL, [OFFICIAL, LIVE])["verdict"] == "ok"
    assert classify("晴天", "周杰倫", LIVE, [OFFICIAL], pinned=True)["verdict"] == "pinned"


def test_clean_studio_upload_with_fewer_views_is_suggested_not_replaced():
    fan = vid("fan", "周杰倫 晴天 (高音質)", 50_000_000, "周杰倫 fan")
    r = classify("晴天", "周杰倫", fan, [OFFICIAL, fan])
    assert r["verdict"] == "suggest"                 # 6× views but nothing wrong with it


def test_similar_popularity_is_ok():
    alt = vid("alt", "周杰倫【晴天】MV", 200_000_000, "JVR Music")
    assert classify("晴天", "周杰倫", alt, [OFFICIAL, alt])["verdict"] == "ok"


def test_cover_by_another_singer_is_auto_even_with_more_views():
    # 一直很安靜: a cover lyric video (7.4M) out-views 阿桑's official MV (4.3M).
    cover = vid("en", "en - 一直很安靜『動態歌詞』", 7_374_919, "Lyrics Channel")
    official = vid("asang", "阿桑 [ 一直很安靜 ] Official Music Video", 4_310_922, "阿桑")
    r = classify("一直很安靜", "阿桑", cover, [cover, official])
    assert r["verdict"] == "auto"
    assert "another singer's version" in r["reasons"]


def test_ok_rows_carry_no_reasons():
    alt = vid("alt", "周杰倫【晴天】MV", 200_000_000, "JVR Music")
    assert classify("晴天", "周杰倫", alt, [OFFICIAL, alt])["reasons"] == []


def test_topic_channel_counts_as_official_studio():
    topic = vid("top", "一直很安靜", 2_290_000, "阿桑 - Topic")
    live = vid("liv", "阿桑【一直很安靜】Live 演唱會", 100_000, "fan")
    r = classify("一直很安靜", "阿桑", live, [live, topic])
    assert r["verdict"] == "auto" and r["best"]["id"] == topic["id"]


def test_unavailable_video_with_official_replacement_is_auto():
    assert classify("晴天", "周杰倫", None, [OFFICIAL])["verdict"] == "auto"


# ─── pinning ────────────────────────────────────────────────────────────

@pytest.fixture
def dataset(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "DATASET_PATH", tmp_path)
    monkeypatch.setattr(pipeline, "NOTES_TXT", tmp_path / "notes.txt")
    monkeypatch.setattr(pipeline, "NOTES_INBOX_TXT", tmp_path / "notes_inbox.txt")
    return tmp_path


def test_pin_adds_url_without_joining_the_setlist(dataset):
    inbox_http.pin_video("晴天", "https://www.youtube.com/watch?v=abcdefghijk")
    [e] = parse_notes((dataset / "notes_inbox.txt").read_text(encoding="utf-8"))[0]
    assert e.youtube_url == "https://youtu.be/abcdefghijk"
    assert not e.in_setlist
    assert inbox_http.pinned_titles() == {"晴天"}


def test_pin_keeps_existing_inbox_markers(dataset):
    (dataset / "notes_inbox.txt").write_text("晴天 -2 !\n", encoding="utf-8")
    inbox_http.pin_video("晴天", "https://youtu.be/abcdefghijk")
    [e] = parse_notes((dataset / "notes_inbox.txt").read_text(encoding="utf-8"))[0]
    assert (e.key_offset, e.favorite, e.youtube_url) == (-2, True, "https://youtu.be/abcdefghijk")


def test_pin_rejects_non_youtube_urls(dataset):
    with pytest.raises(ValueError):
        inbox_http.pin_video("晴天", "https://example.com/x")


def test_reclassify_from_stored_row_matches_live_verdict():
    live = classify("晴天", "周杰倫 Jay Chou", LIVE, [OFFICIAL, LIVE])
    stored = {**live, "title": "晴天", "artist": "周杰倫"}
    assert reclassify(stored, pinned=False)["verdict"] == live["verdict"] == "auto"
    assert reclassify(stored, pinned=True)["verdict"] == "pinned"


def test_talent_show_episode_counts_as_live():
    from crawlers.youtube import _LIVE
    assert _LIVE.search('Hua Chenyu《平凡之路》Ordinary Path "Singer 2018" Episode 11')
    assert _LIVE.search("歌手2018 第十一期")


def test_only_other_singer_and_much_less_popular_original_is_suggested():
    # 離人: ours is 林志炫's famous recording; credited 張學友's has 0.2× the views.
    ours = vid("lin", "林志炫 - 離人", 10_000_000, "林志炫")
    theirs = vid("jacky", "張學友 - 離人 (Official Video)", 2_000_000, "張學友")
    assert classify("離人", "張學友", ours, [ours, theirs])["verdict"] == "suggest"
