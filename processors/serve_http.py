"""Read-only HTTP interface for LAN consumers (interface ② in README).

Serves the processed subset of the dataset to devices that must not clone
the repo or mirror the dataset — e.g. a Mac that records vocals and only
needs one song's stems at a time.

    GET /                       single-page web UI (search / download / preview)
    GET /api/songs              JSON list: processed songs + setlist annotations
    GET /api/songs/{title}      one song: metadata + files present on disk
    GET /files/{title}/{kind}   stream a file; kind is whitelisted below

Design constraints (contract rules in README "Architecture" section):
  * Read-only by construction — no mutating endpoint exists.
  * Only the processed subset is visible; raw/misaligned songs are not.
  * Files are served from disk as-is (FLAC stems are already the
    compacted steady state; nothing is transcoded on the fly).
"""

from __future__ import annotations

import json
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

# Whitelist of servable per-song files → content type. Anything not in
# this dict (metadata.json, audit internals, separations/) is invisible.
FILE_KINDS = {
    "instrumental.flac": "audio/flac",
    "vocals.flac": "audio/flac",
    "instrumental.wav": "audio/wav",
    "vocals.wav": "audio/wav",
    "instrumental.m4a": "audio/mp4",
    "vocals.m4a": "audio/mp4",
    "lyrics.lrc": "text/plain; charset=utf-8",
    "lyrics.txt": "text/plain; charset=utf-8",
}

_TITLE_BAD = re.compile(r"[/\\]|\.\.")   # path traversal guard


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _song_files(song_dir: Path) -> dict[str, int]:
    """Which whitelisted files exist for a song, with sizes in bytes."""
    out = {}
    for kind in FILE_KINDS:
        p = song_dir / kind
        if p.exists():
            out[kind] = p.stat().st_size
    return out


def _lan_ip() -> str:
    """Best-effort LAN IP for the startup banner (no traffic is sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


# ─────────────────────────────────────────────
# Web UI (interface ②'s human face — Mac needs only a browser)
# ─────────────────────────────────────────────

_INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MandpopDataset — 點歌下載</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "PingFang TC", "Microsoft JhengHei", sans-serif;
         background: #14161a; color: #e8e8e8; margin: 0; padding: 1.2rem; }
  h1 { font-size: 1.25rem; margin: 0 0 .75rem; }
  h1 small { color: #8a8f98; font-weight: 400; font-size: .8rem; margin-left: .6rem; }
  #q { width: 100%; padding: .6rem .8rem; font-size: 1rem; border-radius: 8px;
       border: 1px solid #333; background: #1d2026; color: #eee; outline: none; }
  #q:focus { border-color: #5b8cff; }
  #count { color: #8a8f98; font-size: .8rem; margin: .5rem 0; }
  .song { background: #1d2026; border-radius: 10px; padding: .7rem .9rem;
          margin: .5rem 0; }
  .head { display: flex; align-items: baseline; gap: .6rem; flex-wrap: wrap; }
  .title { font-size: 1.05rem; font-weight: 600; }
  .artist { color: #9aa0aa; font-size: .85rem; }
  .badge { font-size: .72rem; padding: .1rem .45rem; border-radius: 999px;
           border: 1px solid #444; color: #cfd3da; }
  .badge.key { border-color: #5b8cff; color: #9db8ff; }
  .badge.fav { border-color: #e8b93c; color: #f0cd6d; }
  .files { margin-top: .45rem; display: flex; gap: .4rem; flex-wrap: wrap; }
  .files a { font-size: .78rem; padding: .28rem .6rem; border-radius: 6px;
             background: #2a2e36; color: #cfe0ff; text-decoration: none;
             border: 1px solid #3a3f49; }
  .files a:hover { background: #343a45; }
  .size { color: #7b8089; }
  audio { margin-top: .5rem; width: 100%; height: 32px; display: none; }
  .preview-btn { cursor: pointer; }
</style>
</head>
<body>
<h1>MandpopDataset 點歌下載<small id="host"></small></h1>
<input id="q" type="search" placeholder="搜尋歌名 / 歌手…" autofocus>
<div id="count"></div>
<div id="list"></div>
<script>
let SONGS = [];
const fmt = b => b > 1048576 ? (b/1048576).toFixed(1)+' MB' : Math.round(b/1024)+' KB';
const esc = s => (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function fileLink(title, kind, label, size) {
  return `<a href="/files/${encodeURIComponent(title)}/${kind}" download>` +
         `${label} <span class="size">${fmt(size)}</span></a>`;
}

function render(filter) {
  const f = (filter||'').trim().toLowerCase();
  const rows = SONGS.filter(s =>
    !f || s.title.toLowerCase().includes(f) || (s.artist||'').toLowerCase().includes(f));
  document.getElementById('count').textContent =
    rows.length + ' / ' + SONGS.length + ' 首（processed 子集）';
  document.getElementById('list').innerHTML = rows.map(s => {
    const fl = s.files_on_disk || {};
    const badges = [];
    if (s.setlist) {
      if (s.setlist.key_offset) badges.push(`<span class="badge key">key ${s.setlist.key_offset>0?'+':''}${s.setlist.key_offset}</span>`);
      if (s.setlist.gender_variant) badges.push(`<span class="badge">${s.setlist.gender_variant==='female'?'女版':'男版'}</span>`);
      if (s.setlist.favorite) badges.push('<span class="badge fav">★ 最愛</span>');
    }
    const links = [];
    if (fl['instrumental.flac']) links.push(fileLink(s.title,'instrumental.flac','伴奏 FLAC',fl['instrumental.flac']));
    if (fl['vocals.flac'])       links.push(fileLink(s.title,'vocals.flac','人聲 FLAC',fl['vocals.flac']));
    if (fl['instrumental.wav'])  links.push(fileLink(s.title,'instrumental.wav','伴奏 WAV',fl['instrumental.wav']));
    if (fl['vocals.wav'])        links.push(fileLink(s.title,'vocals.wav','人聲 WAV',fl['vocals.wav']));
    if (fl['instrumental.m4a'])  links.push(fileLink(s.title,'instrumental.m4a','伴奏 m4a',fl['instrumental.m4a']));
    if (fl['vocals.m4a'])        links.push(fileLink(s.title,'vocals.m4a','導唱 m4a',fl['vocals.m4a']));
    if (fl['lyrics.lrc'])        links.push(fileLink(s.title,'lyrics.lrc','歌詞 LRC',fl['lyrics.lrc']));
    const previewSrc = fl['instrumental.m4a'] ? 'instrumental.m4a'
                     : fl['instrumental.flac'] ? 'instrumental.flac' : null;
    if (previewSrc) links.push(`<a class="preview-btn" onclick="preview(this,'${encodeURIComponent(s.title)}','${previewSrc}')">▶ 試聽</a>`);
    return `<div class="song"><div class="head">` +
           `<span class="title">${esc(s.title)}</span>` +
           `<span class="artist">${esc(s.artist||'')}</span>${badges.join('')}` +
           `</div><div class="files">${links.join('')}</div>` +
           `<audio controls preload="none"></audio></div>`;
  }).join('');
}

function preview(btn, title, kind) {
  const audio = btn.closest('.song').querySelector('audio');
  if (audio.style.display === 'block') { audio.pause(); audio.style.display = 'none'; return; }
  document.querySelectorAll('audio').forEach(a => { a.pause(); a.style.display='none'; });
  audio.src = `/files/${title}/${kind}`;
  audio.style.display = 'block';
  audio.play();
}

document.getElementById('host').textContent = location.host;
document.getElementById('q').addEventListener('input', e => render(e.target.value));
fetch('/api/songs').then(r => r.json()).then(d => { SONGS = d; render(''); });
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    # Set by serve_dataset() before the server starts.
    dataset_path: Path = Path(".")
    protocol_version = "HTTP/1.1"

    # ── helpers ──────────────────────────────

    def _send(self, code: int, body: bytes, ctype: str,
              extra: dict[str, str] | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _not_found(self, msg: str = "not found"):
        self._json({"error": msg}, 404)

    def _songs_index(self) -> list[dict]:
        """Processed songs merged with setlist annotations + on-disk files."""
        processed = _load_jsonl(self.dataset_path / "processed_dataset.jsonl")
        setlist = {r["title"]: r
                   for r in _load_jsonl(self.dataset_path / "setlist.jsonl")}
        songs_root = self.dataset_path / "songs"
        out = []
        for rec in processed:
            title = rec["title"]
            entry = {
                "title": title,
                "artist": rec.get("artist"),
                "album": rec.get("album"),
                "files_on_disk": _song_files(songs_root / title),
            }
            sl = setlist.get(title)
            if sl:
                entry["setlist"] = {
                    "key_offset": sl.get("key_offset", 0),
                    "gender_variant": sl.get("gender_variant"),
                    "favorite": sl.get("favorite", False),
                }
            out.append(entry)
        return out

    # ── routes ───────────────────────────────

    def do_GET(self):  # noqa: N802 (http.server API)
        path = unquote(self.path.split("?", 1)[0])

        if path in ("/", "/index.html"):
            self._send(200, _INDEX_HTML.encode("utf-8"),
                       "text/html; charset=utf-8")
            return

        if path == "/api/songs":
            self._json(self._songs_index())
            return

        m = re.fullmatch(r"/api/songs/([^/]+)", path)
        if m:
            title = m.group(1)
            for entry in self._songs_index():
                if entry["title"] == title:
                    self._json(entry)
                    return
            self._not_found(f"song not in processed dataset: {title}")
            return

        m = re.fullmatch(r"/files/([^/]+)/([^/]+)", path)
        if m:
            self._serve_file(m.group(1), m.group(2))
            return

        self._not_found()

    def _serve_file(self, title: str, kind: str):
        if _TITLE_BAD.search(title) or kind not in FILE_KINDS:
            self._not_found("bad path")
            return
        fpath = self.dataset_path / "songs" / title / kind
        if not fpath.exists():
            self._not_found(f"no {kind} for {title}")
            return

        size = fpath.stat().st_size
        ctype = FILE_KINDS[kind]

        # Single-range support so <audio> preview can seek.
        range_header = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if range_header:
            m = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                else:                      # suffix range: bytes=-N
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    self._json({"error": "range not satisfiable"}, 416)
                    return
                code = 206

        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        # RFC 6266 UTF-8 filename so Chinese titles download intact.
        from urllib.parse import quote
        self.send_header(
            "Content-Disposition",
            f"inline; filename*=UTF-8''{quote(title)}_{kind}")
        self.end_headers()

        with fpath.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 256, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (ConnectionAbortedError, BrokenPipeError):
                    return                 # client cancelled the download
                remaining -= len(chunk)

    def log_message(self, fmt, *args):    # quieter, single-line access log
        print(f"  {self.address_string()}  {fmt % args}")


def serve_dataset(dataset_path: Path, host: str = "0.0.0.0", port: int = 8765):
    """Run the read-only LAN server until Ctrl-C."""
    _Handler.dataset_path = Path(dataset_path)
    httpd = ThreadingHTTPServer((host, port), _Handler)

    n_songs = len(_load_jsonl(Path(dataset_path) / "processed_dataset.jsonl"))
    print(f"MandpopDataset serve — read-only, processed subset only")
    print(f"  dataset : {dataset_path}  ({n_songs} songs)")
    print(f"  local   : http://127.0.0.1:{port}/")
    print(f"  LAN     : http://{_lan_ip()}:{port}/   ← open this on the Mac")
    print(f"  stop    : Ctrl-C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
