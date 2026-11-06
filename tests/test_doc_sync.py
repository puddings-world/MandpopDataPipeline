"""Google Doc → setlist: membership, ordering, and the --from-doc sync."""

import json
import sys

import pytest

import pipeline
from processors import inbox_http
from processors.notes_ingest import merge_inbox, parse_notes
from processors.setlist_order import artist_group, build_aliases, order_setlist


# ─── membership ─────────────────────────────────────────────────────────

def test_every_doc_line_is_in_setlist_markers_are_presets():
    entries, _ = parse_notes("不刪\n聽海 -6 女版 !\n", listed=True)
    by = {e.title: e for e in entries}
    assert by["不刪"].in_setlist and not by["不刪"].has_markers
    assert by["聽海"].in_setlist and by["聽海"].key_offset == -6


def test_bare_inbox_pick_is_corpus_only():
    inbox, _ = parse_notes("夜曲\n告白氣球 -2\n")
    by = {e.title: e for e in inbox}
    assert not by["夜曲"].in_setlist
    assert by["告白氣球"].in_setlist


def test_inbox_markers_still_fill_a_bare_doc_line():
    doc, _ = parse_notes("夜曲\n", listed=True)
    inbox, _ = parse_notes("夜曲 -3 !\n")
    merged, warnings = merge_inbox(doc, inbox)
    assert (merged[0].key_offset, merged[0].favorite) == (-3, True)
    assert warnings == []


def test_doc_markers_beat_inbox_markers():
    doc, _ = parse_notes("夜曲 -1\n", listed=True)
    inbox, _ = parse_notes("夜曲 -3\n")
    merged, warnings = merge_inbox(doc, inbox)
    assert merged[0].key_offset == -1
    assert len(warnings) == 1


# ─── artist grouping ────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, group", [
    ("張韶涵 Angela Zhang", "張韶涵"),
    ("Angela 張韶涵", "張韶涵"),
    ("張韶涵Angela Zhang", "張韶涵"),
    ("周杰伦", "周杰倫"),
    ("郭静", "郭靜"),
    ("陳奕迅 Eason Chan, 王菲 Faye Wong", "陳奕迅"),
    ("薛之謙 & 歐陽娜娜", "薛之謙"),
    ("张碧晨/王赫野", "張碧晨"),
    ("蕭煌奇 Ricky Hsiao〈〉", "蕭煌奇"),
    ("en（王翊恩）", "王翊恩"),
    ("A-Lin", "A-Lin"),
    ("F.I.R.", "F.I.R."),
    ("Ordinary Path》+", "Ordinary Path"),
    ("》（", ""),
    (None, ""),
    ("  ", ""),
])
def test_artist_group(raw, group):
    assert artist_group(raw) == group


def test_english_names_join_chinese_group_via_corpus_aliases():
    aliases = build_aliases(["林俊傑 JJ Lin", "張韶涵 Angela Zhang", "A-Lin", None])
    assert artist_group("JJ Lin", aliases) == "林俊傑"
    assert artist_group("Angela Zhang", aliases) == "張韶涵"
    assert artist_group("A-Lin", aliases) == "A-Lin"


def test_group_label_is_traditional_but_keeps_surnames():
    # zhconv maps the surname 郁 → 鬱 and 钟 → 鐘; names must not change.
    rows = [{"title": "路過人間", "artist": "郁可唯 Yisa Yu", "favorite": False},
            {"title": "稻香", "artist": "周杰伦", "favorite": False},
            {"title": "晴天", "artist": "周杰倫 Jay Chou", "favorite": False},
            {"title": "好想愛這個世界啊", "artist": "华晨宇", "favorite": False},
            {"title": "我的愛", "artist": "钟汉良", "favorite": False}]
    out = {r["title"]: r["artist_group"] for r in order_setlist(rows)}
    assert out["路過人間"] == "郁可唯"
    assert out["稻香"] == out["晴天"] == "周杰倫"
    assert out["好想愛這個世界啊"] == "華晨宇"
    assert out["我的愛"] == "鍾漢良"


def test_order_pins_favorites_then_groups_by_artist():
    rows = [
        {"title": "倔強", "artist": "五月天 Mayday", "favorite": False},
        {"title": "無名歌", "artist": None, "favorite": False},
        {"title": "隱形的翅膀", "artist": "張韶涵", "favorite": False},
        {"title": "溫柔", "artist": "五月天", "favorite": True},
        {"title": "歐若拉", "artist": "張韶涵 Angela Zhang", "favorite": False},
        {"title": "稻香", "artist": "周杰伦", "favorite": False},
    ]
    out = order_setlist(rows)
    assert [r["title"] for r in out] == [
        "溫柔",                      # favorite pinned
        "倔強",                      # wu  (五月天)
        "歐若拉", "隱形的翅膀",       # zhang (張韶涵), titles by pinyin
        "稻香",                      # zhou (周杰倫)
        "無名歌",                    # unknown artist last
    ]
    assert [r["order"] for r in out] == list(range(6))
    assert out[2]["artist_group"] == out[3]["artist_group"] == "張韶涵"


def test_setlist_rows_skip_unready_and_prefer_doc_artist():
    entries, _ = parse_notes("稻香／周杰倫\n夜曲\n告白氣球 !\n", listed=True)
    ready = {"processed": True, "artist": "Timeless Music"}
    records = {"稻香": ready, "告白氣球": {**ready, "artist": "周杰倫"},
               "夜曲": {"status": "done", "lyrics": "txt"}}
    rows, blocked = pipeline.build_setlist_rows(
        entries, records, has_guide=lambda t: t == "稻香")
    assert [r["title"] for r in rows] == ["告白氣球", "稻香"]
    assert rows[1]["artist"] == "周杰倫"          # Doc hint beats channel name
    assert "guide_vocal" in rows[1]["files"]
    assert blocked == [("夜曲", "no timestamped lyrics")]


def test_write_setlist_also_lists_blocked_doc_songs(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "SETLIST_JSONL", tmp_path / "setlist.jsonl")
    monkeypatch.setattr(pipeline, "SETLIST_BLOCKED_JSONL", tmp_path / "setlist_blocked.jsonl")
    monkeypatch.setattr(pipeline, "songs_dir", lambda: tmp_path)
    entries, _ = parse_notes("稻香\n夜曲\n", listed=True)
    n, _ = pipeline.write_setlist(entries, {"稻香": {"processed": True}})
    assert n == 1
    blocked = (tmp_path / "setlist_blocked.jsonl").read_text(encoding="utf-8")
    assert blocked == '{"title": "夜曲", "reason": "not in corpus yet (run ingest without --no-add)"}\n'


# ─── tracked doc + --from-doc ───────────────────────────────────────────

@pytest.fixture
def dataset(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "DATASET_PATH", tmp_path)
    monkeypatch.setattr(pipeline, "NOTES_TXT", tmp_path / "notes.txt")
    monkeypatch.setattr(pipeline, "NOTES_INBOX_TXT", tmp_path / "notes_inbox.txt")
    monkeypatch.setattr(pipeline, "load_raw", lambda: {})
    monkeypatch.delenv("NOTES_DOC_URL", raising=False)
    monkeypatch.delenv("INBOX_POST_INGEST_CMD", raising=False)
    return tmp_path


DOC = "https://docs.google.com/document/d/" + "x" * 30 + "/edit"


def test_doc_url_precedence(dataset, monkeypatch):
    with pytest.raises(ValueError):
        inbox_http.resolve_doc_url()
    assert inbox_http.resolve_doc_url(DOC) == DOC          # CLI arg is saved
    assert inbox_http.resolve_doc_url() == DOC
    monkeypatch.setenv("NOTES_DOC_URL", DOC + "?env")
    assert inbox_http.resolve_doc_url() == DOC + "?env"


def test_sync_doc_fetch_failure_leaves_notes_untouched(dataset, monkeypatch):
    (dataset / "notes.txt").write_text("夜曲\n", encoding="utf-8")

    def denied(url):
        raise PermissionError("not shared")
    monkeypatch.setattr(inbox_http, "fetch_doc_text", denied)
    assert pipeline.sync_doc(DOC) == 1
    assert (dataset / "notes.txt").read_text(encoding="utf-8") == "夜曲\n"


def test_sync_doc_writes_notes_ingests_and_runs_hook(dataset, monkeypatch):
    (dataset / "notes.txt").write_text("# header\n夜曲\n", encoding="utf-8")
    monkeypatch.setattr(inbox_http, "fetch_doc_text", lambda url: "夜曲\n稻香 !\n")
    ran = []
    monkeypatch.setattr(pipeline, "ingest_notes", lambda **kw: ran.append(kw))
    marker = dataset / "hook_ran"
    monkeypatch.setenv("INBOX_POST_INGEST_CMD",
                       f'"{sys.executable}" -c "open(r\'{marker}\', \'w\').close()"')

    assert pipeline.sync_doc(DOC) == 0
    notes = (dataset / "notes.txt").read_text(encoding="utf-8")
    assert notes.startswith("# header\n") and "稻香 !" in notes
    assert (dataset / "notes.txt.bak").exists()
    assert len(ran) == 1 and marker.exists()


def test_sync_doc_no_then_skips_hook(dataset, monkeypatch):
    monkeypatch.setattr(inbox_http, "fetch_doc_text", lambda url: "夜曲\n")
    monkeypatch.setattr(pipeline, "ingest_notes", lambda **kw: None)
    marker = dataset / "hook_ran"
    monkeypatch.setenv("INBOX_POST_INGEST_CMD",
                       f'"{sys.executable}" -c "open(r\'{marker}\', \'w\').close()"')
    assert pipeline.sync_doc(DOC, run_then=False) == 0
    assert not marker.exists()


def test_refresh_setlist_only_rewrites_setlist_files(dataset, monkeypatch):
    (dataset / "notes.txt").write_text("稻香 !\n夜曲\n", encoding="utf-8")
    (dataset / "notes_inbox.txt").write_text("晴天 -2\n", encoding="utf-8")
    monkeypatch.setattr(pipeline, "SETLIST_JSONL", dataset / "setlist.jsonl")
    monkeypatch.setattr(pipeline, "SETLIST_BLOCKED_JSONL", dataset / "setlist_blocked.jsonl")
    monkeypatch.setattr(pipeline, "songs_dir", lambda: dataset)
    monkeypatch.setattr(pipeline, "load_raw", lambda: {"稻香": {"processed": True},
                                                       "晴天": {"processed": True}})
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    assert pipeline.refresh_setlist() == 0
    titles = [json.loads(l)["title"] for l in
              (dataset / "setlist.jsonl").read_text(encoding="utf-8").splitlines()]
    assert titles == ["稻香", "晴天"]                       # favorite first; inbox pick with a marker
    assert "夜曲" in (dataset / "setlist_blocked.jsonl").read_text(encoding="utf-8")


def test_singer_version_token_is_an_artist_hint_not_part_of_the_title():
    entries, _ = parse_notes("給我一個理由忘記 en版 # 或女版 -4\n聽海 女版 -6\n", listed=True)
    by = {e.title: e for e in entries}
    assert by["給我一個理由忘記"].artist_hint == "en"
    assert by["給我一個理由忘記"].key_offset == 0
    assert by["聽海"].gender_variant == "female" and by["聽海"].artist_hint is None


def test_artist_headings_group_songs_and_lines_can_override():
    text = "錄音歌序：\n【周杰倫】\n晴天 !\n哪裡都是你\n【黃鶯鶯】\n哭砂 張惠妹版 -2\n【未分類】\n大魚\n"
    by = {e.title: e for e in parse_notes(text, listed=True)[0]}
    assert set(by) == {"晴天", "哪裡都是你", "哭砂", "大魚"}       # headings are not songs
    assert by["晴天"].artist_hint == "周杰倫" and by["晴天"].favorite
    assert by["哪裡都是你"].artist_hint == "周杰倫"
    assert (by["哭砂"].artist_hint, by["哭砂"].key_offset) == ("張惠妹", -2)  # version singer wins
    assert by["大魚"].artist_hint is None                         # 未分類 is not an artist


def test_pin_typed_after_a_comment_still_pins():
    text = "靜悄悄 男版 # 或-2 ！\n追光者-3  !\n我要快樂 -3！\n後來 男版 # 或女版-5\n"
    by = {e.title: e for e in parse_notes(text, listed=True)[0]}
    assert by["靜悄悄"].favorite and by["靜悄悄"].gender_variant == "male"
    assert by["靜悄悄"].comment == "或-2" and by["靜悄悄"].key_offset == 0
    assert by["追光者"].favorite and by["追光者"].key_offset == -3
    assert by["我要快樂"].favorite and by["我要快樂"].key_offset == -3
    assert not by["後來"].favorite and by["後來"].comment == "或女版-5"


def test_sync_doc_if_changed_skips_ingest_and_hook_when_doc_matches(dataset, monkeypatch):
    (dataset / "notes.txt").write_text("# header\n夜曲\n稻香 !\n", encoding="utf-8")
    monkeypatch.setattr(inbox_http, "fetch_doc_text", lambda url: "夜曲\n稻香 ！\n")
    ran = []
    monkeypatch.setattr(pipeline, "ingest_notes", lambda **kw: ran.append(kw))
    marker = dataset / "hook_ran"
    monkeypatch.setenv("INBOX_POST_INGEST_CMD",
                       f'"{sys.executable}" -c "open(r\'{marker}\', \'w\').close()"')
    assert pipeline.sync_doc(DOC, if_changed=True) == 0
    assert ran == [] and not marker.exists()

    monkeypatch.setattr(inbox_http, "fetch_doc_text", lambda url: "夜曲\n稻香 !\n晴天\n")
    assert pipeline.sync_doc(DOC, if_changed=True) == 0
    assert len(ran) == 1 and marker.exists()
    assert "晴天" in (dataset / "notes.txt").read_text(encoding="utf-8")
