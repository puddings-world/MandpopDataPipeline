"""Swipe-review server — human triage gate for discovered songs.

This is a PRODUCER-side tool (unlike `serve`, which is a read-only
consumer interface): discovered songs sit in raw_dataset.jsonl as
`status=pending`, and this UI lets the maintainer sweep through them
one at a time deciding what deserves pipeline processing.

    →  (ArrowRight)  accept — queue for `pipeline.py add --accepted`
    ←  (ArrowLeft)   reject — stays in raw with review=rejected, never processed
    ↓  (ArrowDown)   skip — decide later (no write)
    U / Backspace    undo the previous verdict

The YouTube preview is resolved lazily per song (top filtered search
hit) and CACHED into the record's youtube_url — so the video you
previewed is exactly the video `add` will download. Verdicts land in
the record's `review` field; nothing else in the pipeline changes.

Endpoints:
    GET  /                        review UI
    GET  /api/review/queue        pending songs without a verdict + stats
    GET  /api/review/resolve/{t}  top YT candidate for a song (cached)
    POST /api/review/verdict      {"title": ..., "verdict": "accepted"|"rejected"|"undo"}
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

# In-memory preview caches (signed googlevideo URLs expire, so never persist).
_EMBED_OK: dict[str, bool] = {}
_AUDIO_URL: dict[str, tuple[float, str]] = {}     # video_id → (fetched_at, url)
_AUDIO_TTL = 3 * 3600


def _check_embeddable(video_id: str) -> bool:
    """oEmbed returns 401 for videos whose uploader disabled embedding."""
    if video_id in _EMBED_OK:
        return _EMBED_OK[video_id]
    ok = False
    try:
        req = urllib.request.Request(
            "https://www.youtube.com/oembed?format=json&url="
            f"https://www.youtube.com/watch?v={video_id}",
            headers={"User-Agent": "Mozilla/5.0"})
        urllib.request.urlopen(req, timeout=6)
        ok = True
    except Exception:
        ok = False
    _EMBED_OK[video_id] = ok
    return ok


def _rank_candidates(cands: list[dict], title: str, artist: str | None) -> list[dict]:
    """Re-rank search hits: YouTube's view-sorted results often return the
    artist's MOST POPULAR song instead of the requested one (e.g. searching
    一千年以後 林俊傑 → 江南). Tier by whether the YT title actually contains
    the song title / artist (simplified-normalized), then by views."""
    try:
        from zhconv import convert
        def norm(s):
            return convert((s or "").lower().replace(" ", ""), "zh-hans")
    except ImportError:
        def norm(s):
            return (s or "").lower().replace(" ", "")
    t = norm(title)
    artist_cjk = "".join(c for c in (artist or "") if "一" <= c <= "鿿")
    a = norm(artist_cjk)

    def key(c):
        yt = norm(c.get("title"))
        has_t = t and t in yt
        has_a = a and a in yt
        tier = 0 if (has_t and has_a) else 1 if has_t else 2
        return (tier, -(c.get("view_count") or 0))

    return sorted(cands, key=key)


def _audio_stream_url(video_id: str) -> str | None:
    """Direct bestaudio stream URL via yt-dlp — fallback preview for
    embed-disabled videos. Same-machine playback only (URLs are IP-bound)."""
    hit = _AUDIO_URL.get(video_id)
    if hit and time.time() - hit[0] < _AUDIO_TTL:
        return hit[1]
    result = subprocess.run(
        ["yt-dlp", "-f", "bestaudio", "-g", "--no-playlist",
         f"https://www.youtube.com/watch?v={video_id}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        return None
    lines = result.stdout.strip().splitlines()
    url = lines[-1] if lines else None
    if url:
        _AUDIO_URL[video_id] = (time.time(), url)
    return url

_INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MandpopDataset — 快篩</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "PingFang TC", "Microsoft JhengHei", sans-serif;
         background: #14161a; color: #e8e8e8; margin: 0; min-height: 100vh;
         display: flex; flex-direction: column; align-items: center; padding: 1rem; }
  #filters { display: flex; gap: .5rem; margin-bottom: .6rem; }
  .chip { font-size: .8rem; padding: .25rem .8rem; border-radius: 999px;
          border: 1px solid #3a3f49; color: #9aa0aa; cursor: pointer; }
  .chip.active { border-color: #5b8cff; color: #cfe0ff; background: #232a3a; }
  #stats { color: #8a8f98; font-size: .85rem; margin-bottom: .8rem; }
  #stats b { color: #cfd3da; }
  #card { width: min(680px, 96vw); background: #1d2026; border-radius: 16px;
          padding: 1.2rem 1.4rem; transition: transform .18s ease, opacity .18s ease; }
  #card.swipe-right { transform: translateX(70vw) rotate(8deg); opacity: 0; }
  #card.swipe-left  { transform: translateX(-70vw) rotate(-8deg); opacity: 0; }
  #card.swipe-down  { transform: translateY(40vh); opacity: 0; }
  .title { font-size: 1.6rem; font-weight: 700; }
  .artist { color: #9aa0aa; font-size: 1.05rem; margin-top: .15rem; }
  .prov { color: #6d727c; font-size: .78rem; margin-top: .5rem; }
  .ytmeta { color: #8a8f98; font-size: .8rem; margin-top: .5rem; }
  #player { width: 100%; aspect-ratio: 16/9; border: 0; border-radius: 10px;
            margin-top: .8rem; background: #000; }
  #thumb { width: 100%; aspect-ratio: 16/9; object-fit: cover; border-radius: 10px;
           margin-top: .8rem; background: #000; display: none; }
  #apreview { width: 100%; margin-top: .5rem; display: none; }
  #controls { display: flex; gap: .7rem; margin-top: 1.1rem; justify-content: center; }
  button { font-size: 1rem; padding: .55rem 1.3rem; border-radius: 10px;
           border: 1px solid #3a3f49; background: #2a2e36; color: #eee; cursor: pointer; }
  button:hover { background: #343a45; }
  #btn-no  { border-color: #a04040; }  #btn-no:hover { background: #4a2626; }
  #btn-yes { border-color: #3f8f4f; }  #btn-yes:hover { background: #24402a; }
  #hint { color: #6d727c; font-size: .78rem; margin-top: .9rem; text-align: center; }
  #done { display: none; font-size: 1.3rem; margin-top: 3rem; text-align: center; }
</style>
</head>
<body>
<div id="filters">
  <span class="chip active" data-min="1">全部</span>
  <span class="chip" data-min="2">≥2 來源</span>
  <span class="chip" data-min="3">≥3 來源</span>
</div>
<div id="stats"></div>
<div id="card">
  <div class="title"></div>
  <div class="artist"></div>
  <div class="prov"></div>
  <!-- Audio-first preview: thumbnail + bestaudio stream (always playable).
       The iframe is a fallback for songs whose stream resolution failed;
       it sends our origin as referer (error 153 without one). -->
  <img id="thumb" alt="">
  <audio id="apreview" controls preload="none"></audio>
  <iframe id="player" allow="autoplay; encrypted-media" referrerpolicy="origin"
          style="display:none"></iframe>
  <div class="ytmeta"></div>
  <div id="controls">
    <button id="btn-no">← 不要</button>
    <button id="btn-skip">↓ 跳過</button>
    <button id="btn-yes">要 →</button>
  </div>
  <div id="hint">鍵盤：← 不要　→ 要　↓ 跳過　U 反悔上一首</div>
</div>
<div id="done">🎉 這一批篩完了！接受的歌執行：<br>
  <code>python pipeline.py add --accepted</code></div>
<script>
let allQueue = [], queue = [], idx = 0, minSrc = 1;
let history = [], stats = {accepted:0, rejected:0};
const searchCache = new Map(), audioCache = new Map();
const $ = s => document.querySelector(s);
const esc = s => (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function load() {
  const d = await fetch('/api/review/queue').then(r => r.json());
  allQueue = d.queue; stats = d.stats;
  applyFilter(minSrc);
}

function applyFilter(n) {
  minSrc = n;
  queue = allQueue.filter(q => (q.discovered_from || []).length >= n);
  idx = 0; history = [];
  document.querySelectorAll('.chip').forEach(c =>
    c.classList.toggle('active', +c.dataset.min === n));
  $('#card').style.display = 'block'; $('#done').style.display = 'none';
  show();
}

function updateStats() {
  $('#stats').innerHTML =
    `待篩 <b>${queue.length - idx}</b>　已接受 <b>${stats.accepted}</b>　` +
    `已拒絕 <b>${stats.rejected}</b>`;
}

function resolve(title) {
  if (!searchCache.has(title))
    searchCache.set(title, fetch('/api/review/resolve/' + encodeURIComponent(title))
      .then(r => r.json()).catch(() => null));
  return searchCache.get(title);
}

function audio(vid) {
  if (!audioCache.has(vid))
    audioCache.set(vid, fetch('/api/review/audio/' + vid)
      .then(r => r.json()).then(d => d.audio_url).catch(() => null));
  return audioCache.get(vid);
}

// Chain search → audio so a prefetched song is FULLY ready to play.
const prefetch = title => resolve(title).then(r => r && r.video_id && audio(r.video_id));

async function show() {
  updateStats();
  if (idx >= queue.length) {
    $('#card').style.display = 'none'; $('#done').style.display = 'block';
    return;
  }
  const s = queue[idx];
  $('#card').className = '';
  $('.title').textContent = s.title;
  $('.artist').textContent = s.artist || '';
  $('.prov').textContent = (s.discovered_from || [])
    .map(p => `${p.name}${p.rank ? ' #' + p.rank : ''}`).join('　');
  const ap = $('#apreview'), th = $('#thumb'), pl = $('#player');
  ap.pause(); ap.src = ''; ap.style.display = 'none';
  th.style.display = 'none'; pl.src = ''; pl.style.display = 'none';
  $('.ytmeta').textContent = '搜尋 YouTube…';

  // Current card FIRST — prefetch must not compete for connections.
  const r = await resolve(s.title);
  if (queue[idx] !== s) return;           // user already moved on
  if (r && r.video_id) {
    // Thumbnail + metadata render immediately; audio arrives second.
    th.src = `https://i.ytimg.com/vi/${r.video_id}/hqdefault.jpg`;
    th.style.display = 'block';
    $('.ytmeta').innerHTML =
      `${esc(r.yt_title || '')}　·　${(r.view_count || 0).toLocaleString()} views` +
      (r.duration ? `　·　${Math.floor(r.duration/60)}:${String(r.duration%60).padStart(2,'0')}` : '') +
      `　·　<span id="astate">解析音訊中…</span>　` +
      `<a style="color:#9db8ff" target="_blank"
        href="https://www.youtube.com/watch?v=${r.video_id}">在 YouTube 開啟 ↗</a>`;
    const au = await audio(r.video_id);
    if (queue[idx] !== s) return;
    if (au) {
      ap.src = au; ap.style.display = 'block';
      ap.play().catch(() => {});          // autoplay may need a user gesture
      const el = $('#astate'); if (el) el.remove();
    } else {
      // Stream resolution failed — YouTube embed as last resort.
      const el = $('#astate'); if (el) el.remove();
      th.style.display = 'none';
      pl.style.display = 'block';
      pl.src = `https://www.youtube.com/embed/${r.video_id}?autoplay=1`;
    }
  } else {
    $('.ytmeta').innerHTML = `找不到合適影片 — <a style="color:#9db8ff" target="_blank"
      href="https://www.youtube.com/results?search_query=${encodeURIComponent(s.title + ' ' + (s.artist||'') + ' MV')}">手動搜尋</a>`;
  }
  // Now warm up the next few (search + audio, fully playable on arrival).
  for (let j = idx + 1; j < Math.min(idx + 4, queue.length); j++) prefetch(queue[j].title);
}

async function verdict(v, anim) {
  if (idx >= queue.length) return;
  const s = queue[idx];
  $('#card').className = anim;
  if (v !== 'skip') {
    await fetch('/api/review/verdict', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({title: s.title, verdict: v})});
    stats[v === 'accepted' ? 'accepted' : 'rejected']++;
  }
  history.push({title: s.title, verdict: v});
  setTimeout(() => { idx++; show(); }, 190);
}

async function undo() {
  const last = history.pop();
  if (!last || idx === 0) return;
  if (last.verdict !== 'skip') {
    await fetch('/api/review/verdict', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({title: last.title, verdict: 'undo'})});
    stats[last.verdict === 'accepted' ? 'accepted' : 'rejected']--;
  }
  idx--; show();
}

document.addEventListener('keydown', e => {
  if (e.key === 'ArrowRight') verdict('accepted', 'swipe-right');
  else if (e.key === 'ArrowLeft') verdict('rejected', 'swipe-left');
  else if (e.key === 'ArrowDown') verdict('skip', 'swipe-down');
  else if (e.key === 'u' || e.key === 'U' || e.key === 'Backspace') undo();
});
$('#btn-yes').onclick = () => verdict('accepted', 'swipe-right');
$('#btn-no').onclick = () => verdict('rejected', 'swipe-left');
$('#btn-skip').onclick = () => verdict('skip', 'swipe-down');
document.querySelectorAll('.chip').forEach(c =>
  c.onclick = () => applyFilter(+c.dataset.min));
load();
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802
        path = unquote(self.path.split("?", 1)[0])

        if path in ("/", "/index.html"):
            self._send(200, _INDEX_HTML.encode("utf-8"),
                       "text/html; charset=utf-8")
            return

        if path == "/api/review/queue":
            import pipeline
            records = pipeline.load_raw()
            queue = [{
                "title": t,
                "artist": r.get("artist"),
                "discovered_from": r.get("discovered_from", []),
            } for t, r in records.items()
                if r.get("status") == "pending" and not r.get("review")]
            # Most-corroborated first: a song found by many playlists is
            # far more likely to be a keeper than a one-playlist stray.
            queue.sort(key=lambda q: (-len(q["discovered_from"]), q["title"]))
            stats = {
                "accepted": sum(1 for r in records.values()
                                if r.get("review") == "accepted"),
                "rejected": sum(1 for r in records.values()
                                if r.get("review") == "rejected"),
            }
            self._json({"queue": queue, "stats": stats})
            return

        m = re.fullmatch(r"/api/review/resolve/(.+)", path)
        if m:
            self._resolve(m.group(1))
            return

        m = re.fullmatch(r"/api/review/audio/([\w-]{11})", path)
        if m:
            # Separate endpoint so the card can show thumbnail + metadata
            # as soon as the search lands, while audio resolves after.
            self._json({"audio_url": _audio_stream_url(m.group(1))})
            return

        self._json({"error": "not found"}, 404)

    def _resolve(self, title: str):
        """Top YouTube candidate for a song; cache the URL into the record
        so accept → `add --accepted` downloads exactly what was previewed."""
        import pipeline
        from crawlers.youtube import search_song

        records = pipeline.load_raw()
        rec = records.get(title)
        if rec is None:
            self._json({"error": "unknown title"}, 404)
            return

        cached = rec.get("youtube_url")
        m = cached and re.search(r"(?:v=|youtu\.be/)([\w-]{11})", cached)
        if m:
            vid = m.group(1)
            meta = {"yt_title": rec.get("youtube_title"),
                    "view_count": rec.get("view_count"),
                    "duration": rec.get("duration_sec")}
        else:
            query = f"{title} {rec.get('artist') or ''}".strip()
            candidates = search_song(query, max_results=6)
            if not candidates:
                self._json({"video_id": None})
                return
            top = _rank_candidates(candidates, title, rec.get("artist"))[0]
            url = top.get("url") or top.get("webpage_url") or ""
            vid = top.get("id")
            meta = {"yt_title": top.get("title"),
                    "view_count": top.get("view_count"),
                    "duration": top.get("duration")}
            pipeline.upsert_raw(title, youtube_url=url,
                                youtube_title=top.get("title"),
                                view_count=top.get("view_count"),
                                duration_sec=top.get("duration"))

        # Audio stream URL is resolved by /api/review/audio/{vid} in a
        # second request — YouTube embed restrictions (age/region/login)
        # are undetectable up front, so audio-first is the only preview
        # that always works; splitting the calls lets the card render early.
        self._json({"video_id": vid, **meta})

    def do_POST(self):  # noqa: N802
        if unquote(self.path.split("?", 1)[0]) != "/api/review/verdict":
            self._json({"error": "not found"}, 404)
            return
        import pipeline

        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            title = body["title"]
            verdict = body["verdict"]
            assert verdict in ("accepted", "rejected", "undo")
        except (json.JSONDecodeError, KeyError, AssertionError):
            self._json({"error": "bad request"}, 400)
            return

        records = pipeline.load_raw()
        if title not in records:
            self._json({"error": "unknown title"}, 404)
            return
        if verdict == "undo":
            # upsert_raw drops None values, so clear the field directly.
            records[title].pop("review", None)
            records[title].pop("reviewed_at", None)
            pipeline.save_raw(records)
        else:
            pipeline.upsert_raw(title, review=verdict,
                                reviewed_at=datetime.now().isoformat())
        self._json({"ok": True})

    def log_message(self, fmt, *args):
        pass                              # keep the review session quiet


def serve_review(host: str = "127.0.0.1", port: int = 8766):
    """Run the swipe-review server until Ctrl-C."""
    import pipeline
    records = pipeline.load_raw()
    n_queue = sum(1 for r in records.values()
                  if r.get("status") == "pending" and not r.get("review"))
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(f"MandpopDataset review — human triage for discovered songs")
    print(f"  queue : {n_queue} pending songs")
    print(f"  open  : http://127.0.0.1:{port}/")
    print(f"  keys  : ← 不要   → 要   ↓ 跳過   U 反悔")
    print(f"  then  : python pipeline.py add --accepted")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
