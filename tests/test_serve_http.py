"""Interface ② (pipeline.py serve): which files a LAN consumer can fetch."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import pytest

from processors import serve_http


@pytest.fixture()
def server(tmp_path):
    song = tmp_path / "songs" / "心的距離"
    (song / "separations" / "karaoke_becruily").mkdir(parents=True)
    (song / "separations" / "karaoke_becruily" / "instrumental.flac").write_bytes(b"karaoke")
    (song / "separations" / "inst_becruily").mkdir()
    (song / "separations" / "inst_becruily" / "instrumental.flac").write_bytes(b"other")
    (song / "original.mp3").write_bytes(b"full mix")
    (song / "instrumental.flac").write_bytes(b"full removal")
    (song / "metadata.json").write_text("{}", encoding="utf-8")
    (tmp_path / "processed_dataset.jsonl").write_text(
        json.dumps({"title": "心的距離", "artist": "陳奕迅"}, ensure_ascii=False) + "\n",
        encoding="utf-8")

    serve_http._Handler.dataset_path = tmp_path
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve_http._Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.read(), r.headers


def test_karaoke_backing_and_original_are_downloadable(server):
    t = quote("心的距離")
    assert get(f"{server}/files/{t}/karaoke.flac")[1] == b"karaoke"
    status, body, headers = get(f"{server}/files/{t}/original.mp3")
    assert body == b"full mix" and headers["Content-Type"] == "audio/mpeg"
    assert get(f"{server}/files/{t}/instrumental.flac")[1] == b"full removal"


def test_song_listing_reports_the_new_kinds(server):
    files = json.loads(get(f"{server}/api/songs/{quote('心的距離')}")[1])["files_on_disk"]
    assert files["karaoke.flac"] == len(b"karaoke")
    assert files["original.mp3"] == len(b"full mix")


def test_other_files_stay_hidden(server):
    t = quote("心的距離")
    for kind in ("metadata.json", "separations", "inst_becruily"):
        with pytest.raises(urllib.error.HTTPError) as e:
            get(f"{server}/files/{t}/{kind}")
        assert e.value.code == 404
