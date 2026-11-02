"""Lyrics search results must be this song (title, and artist when known)."""

from crawlers.lyrics import pick_song


def test_artist_match_wins_over_search_order():
    # 少年: results[0] used to be taken blindly.
    cands = [("少年", "庄园"), ("少年", "Dave"), ("少年", "梦然"), ("是你", "梦然")]
    assert pick_song(cands, "少年", "梦然") == (2, "exact")


def test_another_song_is_never_picked():
    assert pick_song([("是你", "梦然"), ("玫瑰少年", "蔡依林")], "少年", "梦然") is None


def test_bracketed_version_is_a_variant_and_live_or_karaoke_never():
    cands = [("少年 (Live)", "梦然"), ("少年 (伴奏)", "梦然"), ("少年 (低频公益版)", "梦然")]
    assert pick_song(cands, "少年", "梦然") == (2, "variant")
    assert pick_song([("少年 (Live)", "梦然")], "少年", "梦然") is None


def test_simplified_traditional_and_bilingual_artist_credits_match():
    assert pick_song([("小幸运", "田馥甄")], "小幸運", "田馥甄 Hebe Tien") == (0, "exact")
    assert pick_song([("再见", "G.E.M.邓紫棋")], "再見", "G.E.M.鄧紫棋") == (0, "exact")


def test_without_artist_first_exact_name_wins():
    assert pick_song([("晴天娃娃", "x"), ("晴天", "周杰倫")], "晴天") == (1, "exact")


def test_fetch_prefers_the_source_that_has_the_artist(monkeypatch):
    """NetEase lacks Jay Chou; its exact-name hit is a cover, QQ has the original."""
    from crawlers import lyrics
    lrc = "[00:01.00]" + "歌詞" * 20
    monkeypatch.setattr(lyrics, "_fetch_netease",
                        lambda t, a=None: (lrc + "翻唱", "x", {"artist": "张宇", "match": "exact"}))
    monkeypatch.setattr(lyrics, "_fetch_qq_music",
                        lambda t, a=None: (lrc + "原唱", "y", {"artist": "周杰伦", "match": "exact"}))
    got, _, source, info = lyrics.fetch_lyrics("迷迭香", artist="周杰倫")
    assert source == "qq_music" and got.endswith("原唱")
    # without an artist the first source's exact hit still wins
    assert lyrics.fetch_lyrics("迷迭香")[2] == "netease"


def test_variant_characters_and_simplified_queries():
    from crawlers.lyrics import pick_song, search_queries
    # 艷 (Doc) vs 艳 (site): same song
    assert pick_song([("痛快的哀艳", "苏打绿")], "痛快的哀艷", "蘇打綠") == (0, "exact")
    qs = search_queries("人生沒有標準答案", "毛不易")
    assert qs[0] == "人生沒有標準答案 毛不易" and "人生没有标准答案" in qs


def test_backdated_blank_lines_get_their_text_back():
    from crawlers.lyrics import fill_backdated_blanks
    lrc = "[02:41.52]就这么样\n[01:30.92]\n[02:44.21]离开你\n[02:50.00]\n"
    out = fill_backdated_blanks(lrc).splitlines()
    assert out[1] == "[01:30.92]就这么样"          # earlier occurrence of the chorus
    assert out[3] == "[02:50.00]"                  # a normal end-of-line blank stays blank


def test_search_keeps_looking_for_the_artist_across_queries(monkeypatch):
    """A thin first query must not settle on another singer's same-title song."""
    import requests
    from crawlers import lyrics

    class Resp:
        status_code = 200
        def __init__(self, data): self._d = data
        def json(self): return self._d

    def song(i, name, artist):
        return {"id": i, "name": name, "artists": [{"name": artist}], "album": {"name": "x"}}

    def fake_post(url, data=None, **kw):
        q = data["s"]
        songs = [song(1, "鱼", "陈绮贞")]
        if q in ("鱼", "魚"):
            songs.append(song(2, "鱼", "怕胖团"))
        return Resp({"result": {"songs": songs}})

    def fake_get(url, **kw):
        sid = url.split("id=")[1].split("&")[0]
        return Resp({"lrc": {"lyric": f"[00:01.00]{'怕胖团' if sid == '2' else '陈绮贞'}的歌詞" + "啦" * 30}})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)
    lrc, _, info = lyrics._fetch_netease("魚", "怕胖團")
    assert info["artist"] == "怕胖团" and "怕胖团" in lrc
