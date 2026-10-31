"""Song inbox server — one place to feed new songs into the pipeline.

PRODUCER-side tool (like `review`). Two inputs, one processing queue:

  * Google Doc  — paste the whole doc (or one-click fetch a link-shared
                  doc), preview the diff against notes.txt, then apply.
  * YouTube     — the Chrome extension in tools/youtube_inbox_extension/
                  POSTs "this video, this key" picks into notes_inbox.txt.

Either input kicks off `pipeline.py ingest` in a background subprocess
(one at a time; requests arriving mid-run coalesce into one re-run).
After a successful ingest an optional post-command runs — e.g. the
StreetPerformerMaster phone sync — configured via `--then` or
INBOX_POST_INGEST_CMD, so the producer never imports a consumer.

Endpoints (JSON bodies require Content-Type: application/json, which
forces a CORS preflight we never answer — random web pages can't POST
here; the extension's service worker is exempt via host_permissions):

    GET  /                       inbox UI
    GET  /api/state              doc url, inbox lines, job status
    POST /api/notes/fetch        {"url"?}  → {"text"} from a Google Doc
    POST /api/notes/preview      {"text"}  → diff vs current notes.txt
    POST /api/notes/apply        {"text"}  → write notes.txt + run ingest
    POST /api/youtube            {"url","title","artist"?,"key_offset"?,
                                  "gender_variant"?,"favorite"?,"sing"?,
                                  "comment"?}  → upsert inbox line + ingest
    POST /api/inbox/remove       {"title"}
    POST /api/job/run            run ingest now
    GET  /api/job?since=N        status + log lines from index N
    GET  /api/guess?text=...     longest corpus title inside a video title
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from processors.notes_ingest import NoteEntry, format_line, parse_notes, video_id

REPO_DIR = Path(__file__).resolve().parent.parent
_DOC_ID = re.compile(r"/document/d/([\w-]{20,})")
_INBOX_HEADER = (
    "# notes_inbox.txt — songs picked from YouTube via the browser extension\n"
    "# Same grammar as notes.txt. Google Doc (notes.txt) wins wherever it says\n"
    "# something about the same title; copy lines into the Doc to make them permanent.\n"
)


# ─── paths (resolved from pipeline so DATASET_PATH is honoured) ─────────

def _paths():
    import pipeline
    return pipeline.NOTES_TXT, pipeline.NOTES_INBOX_TXT, pipeline.DATASET_PATH / "inbox_config.json"


def _load_config() -> dict:
    try:
        return json.loads(_paths()[2].read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_config(cfg: dict) -> None:
    _paths()[2].write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_doc_url(cli_url: str | None = None) -> str:
    """The tracked Google Doc: CLI arg > NOTES_DOC_URL > inbox_config.json.

    A URL passed on the CLI is saved as the new tracked doc (the same slot
    the inbox page's 「抓最新」 uses), so later runs need no argument.
    """
    if cli_url:
        cfg = _load_config()
        if cfg.get("doc_url") != cli_url:
            cfg["doc_url"] = cli_url
            _save_config(cfg)
        return cli_url
    url = os.environ.get("NOTES_DOC_URL") or _load_config().get("doc_url")
    if not url:
        raise ValueError("No Google Doc tracked: pass `ingest --from-doc URL` once, "
                         "or set NOTES_DOC_URL in .env")
    return url


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ─── Google Doc ─────────────────────────────────────────────────────────

def fetch_doc_text(url: str) -> str:
    """Plain-text export of a Google Doc shared as 'anyone with the link'."""
    m = _DOC_ID.search(url)
    if not m:
        raise ValueError("不是 Google Doc 連結（需要 docs.google.com/document/d/...）")
    export = f"https://docs.google.com/document/d/{m.group(1)}/export?format=txt"
    req = urllib.request.Request(export, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8-sig", errors="replace")
            final_url = resp.geturl()
    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            raise PermissionError("Doc 沒有開放連結檢視：共用 → 一般存取權 → 知道連結的任何人") from err
        raise ValueError(f"抓不到這份 Doc（HTTP {err.code}），連結是不是貼錯了？") from err
    if "accounts.google.com" in final_url or body.lstrip().startswith("<!DOCTYPE"):
        raise PermissionError("Doc 沒有開放連結檢視：共用 → 一般存取權 → 知道連結的任何人")
    return body.replace("\r\n", "\n")


# ─── notes diff ─────────────────────────────────────────────────────────

def _sig(e: NoteEntry) -> tuple:
    return (e.key_offset, e.gender_variant, e.favorite, e.in_setlist,
            video_id(e.youtube_url), e.artist_hint)


def diff_notes(old_text: str, new_text: str) -> dict:
    import pipeline
    old, _ = parse_notes(old_text, listed=True)
    new, warnings = parse_notes(new_text, listed=True)
    old_by = {e.title: e for e in old}
    new_by = {e.title: e for e in new}
    records = pipeline.load_raw()

    def needs_download(e: NoteEntry) -> bool:
        rec = records.get(e.title)
        return rec is None or rec.get("status") == "pending"

    added = [{"title": e.title, "line": format_line(e), "setlist": e.in_setlist,
              "download": needs_download(e)}
             for e in new if e.title not in old_by]
    removed = [{"title": e.title, "line": format_line(e), "setlist": e.in_setlist}
               for e in old if e.title not in new_by]
    changed = [{"title": e.title, "before": format_line(old_by[e.title]),
                "after": format_line(e)}
               for e in new if e.title in old_by and _sig(e) != _sig(old_by[e.title])]
    return {
        "total": len(new),
        "setlist": sum(e.in_setlist for e in new),
        "added": added, "removed": removed, "changed": changed,
        "warnings": warnings,
    }


def write_notes(new_text: str) -> None:
    """Replace notes.txt, keeping its leading # header and a .bak copy."""
    notes, _, _ = _paths()
    old = _read(notes)
    if old:
        notes.with_suffix(".txt.bak").write_text(old, encoding="utf-8")
    header = []
    for line in old.splitlines():
        if not line.startswith("#"):
            break
        header.append(line)
    body = new_text.strip("\n") + "\n"
    if header and not body.lstrip().startswith("#"):
        body = "\n".join(header) + "\n" + body
    notes.write_text(body, encoding="utf-8")


# ─── YouTube inbox ──────────────────────────────────────────────────────

def _inbox_entries() -> list[NoteEntry]:
    return parse_notes(_read(_paths()[1]))[0]


def _write_inbox(entries: list[NoteEntry]) -> None:
    _paths()[1].write_text(
        _INBOX_HEADER + "".join(format_line(e) + "\n" for e in entries), encoding="utf-8")


def upsert_inbox(body: dict) -> dict:
    title = (body.get("title") or "").strip()
    url = (body.get("url") or "").strip()
    vid = video_id(url)
    if not title:
        raise ValueError("歌名必填")
    if vid is None:
        raise ValueError("不是 YouTube 影片網址")
    gender = body.get("gender_variant") or None
    if gender not in (None, "female", "male"):
        raise ValueError("gender_variant must be female / male / null")
    comment = re.sub(r"[#＃\n]", " ", body.get("comment") or "").strip() or None
    entry = NoteEntry(
        title=title,
        artist_hint=(body.get("artist") or "").strip().replace("/", " ") or None,
        key_offset=int(body.get("key_offset") or 0),
        gender_variant=gender,
        favorite=bool(body.get("favorite")),
        sing=bool(body.get("sing", True)),
        youtube_url=f"https://youtu.be/{vid}",
        comment=comment,
    )
    # The inbox is plain notes grammar — refuse anything that won't read back.
    back = parse_notes(format_line(entry))[0]
    if len(back) != 1 or back[0].title != title or back[0].key_offset != entry.key_offset:
        raise ValueError(f"歌名或歌手含有會被當成標記的字（例如 !、唱、+3）：{format_line(entry)}")
    entries = [e for e in _inbox_entries() if e.title != title] + [entry]
    _write_inbox(entries)

    # Tell the caller when the Doc already speaks for this title.
    notes, _, _ = _paths()
    doc = {e.title: e for e in parse_notes(_read(notes))[0]}.get(title)
    note = None
    video_key = int(body.get("video_key") or 0)
    if doc is not None and doc.key_offset and video_key and not doc.youtube_url:
        # Doc's key was relative to the old (original-key) video; this one
        # is already shifted, so the two offsets would stack.
        note = (f"這支影片本身已經 {video_key:+d}key，Doc 又寫 {doc.key_offset:+d}，"
                f"會疊加成 {doc.key_offset + video_key:+d}；請把 Doc 那行改成想要的再調整量")
    elif doc is not None and doc.has_markers and entry.has_markers and (
            (doc.key_offset, doc.gender_variant) != (entry.key_offset, entry.gender_variant)):
        note = f"Google Doc 寫的是「{format_line(doc)}」，以 Doc 為準；要改調請改 Doc"
    return {"line": format_line(entry), "doc_conflict": note}


_TITLES: tuple[float, list[str]] | None = None


def guess_title(text: str) -> dict:
    """Longest known corpus title contained in a raw video title.

    Disambiguates "歌名 - 歌手" vs "歌手 - 歌名" and separator-less titles
    for the extension's prefill; also says whether the song is processed.
    """
    global _TITLES
    import pipeline
    mtime = pipeline.RAW_DATASET.stat().st_mtime
    if _TITLES is None or _TITLES[0] != mtime:
        titles = sorted(pipeline.load_raw(), key=len, reverse=True)
        _TITLES = (mtime, [t for t in titles if len(t) >= 2])
    hit = next((t for t in _TITLES[1] if t in text), None)
    if hit is None:
        return {"title": None}
    rec = pipeline.load_raw().get(hit, {})
    return {"title": hit, "artist": rec.get("artist"),
            "processed": bool(rec.get("processed")), "youtube_url": rec.get("youtube_url")}


def remove_inbox(title: str) -> bool:
    entries = _inbox_entries()
    kept = [e for e in entries if e.title != title]
    _write_inbox(kept)
    return len(kept) != len(entries)


# ─── background ingest job ──────────────────────────────────────────────

class _Job:
    """Single-flight ingest runner; requests during a run coalesce."""

    def __init__(self, then_cmd: str | None):
        self.then_cmd = then_cmd
        self.lock = threading.Lock()
        self.running = False
        self.pending = False
        self.log: list[str] = []
        self.last_rc: int | None = None
        self.started_at: str | None = None
        self.finished_at: str | None = None

    def _emit(self, line: str) -> None:
        with self.lock:
            self.log.append(line.rstrip("\n"))
            if len(self.log) > 5000:
                del self.log[:1000]

    def request(self, reason: str) -> None:
        with self.lock:
            if self.running:
                self.pending = True
                self.log.append(f"── 已排入下一輪（{reason}）")
                return
            self.running = True
        threading.Thread(target=self._loop, args=(reason,), daemon=True).start()

    def _stream(self, cmd, shell: bool = False) -> int:
        import pipeline
        # Children inherit our env; a relative DATASET_PATH (".") from this
        # repo's .env would resolve against the consumer's cwd instead.
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
                   DATASET_PATH=str(pipeline.DATASET_PATH))
        proc = subprocess.Popen(
            cmd, cwd=REPO_DIR, shell=shell, env=env, text=True,
            encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for line in proc.stdout:
            self._emit(line)
        return proc.wait()

    def _loop(self, reason: str) -> None:
        while True:
            self.started_at = datetime.now().isoformat(timespec="seconds")
            self._emit(f"══ {self.started_at} ingest 開始（{reason}）")
            rc = self._stream([sys.executable, "-u", str(REPO_DIR / "pipeline.py"), "ingest"])
            self._emit(f"══ ingest 結束 rc={rc}")
            if rc == 0 and self.then_cmd:
                self._emit(f"══ 接著執行：{self.then_cmd}")
                rc = self._stream(self.then_cmd, shell=True)
                self._emit(f"══ 後續指令結束 rc={rc}")
            self.last_rc = rc
            self.finished_at = datetime.now().isoformat(timespec="seconds")
            with self.lock:
                if not self.pending:
                    self.running = False
                    return
                self.pending = False
            reason = "排隊中的新請求"

    def status(self, since: int) -> dict:
        with self.lock:
            return {"running": self.running, "pending": self.pending,
                    "last_rc": self.last_rc, "started_at": self.started_at,
                    "finished_at": self.finished_at, "next": len(self.log),
                    "lines": self.log[since:]}


JOB: _Job | None = None


# ─── HTTP ───────────────────────────────────────────────────────────────

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
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif u.path == "/api/state":
            notes, _, _ = _paths()
            self._json({
                "doc_url": _load_config().get("doc_url", ""),
                "notes_text": _read(notes),
                "inbox": [{"title": e.title, "line": format_line(e)} for e in _inbox_entries()],
                "post_cmd": JOB.then_cmd,
                "job": JOB.status(len(JOB.log)),
            })
        elif u.path == "/api/guess":
            self._json(guess_title(parse_qs(u.query).get("text", [""])[0]))
        elif u.path == "/api/job":
            since = int(parse_qs(u.query).get("since", ["0"])[0] or 0)
            self._json(JOB.status(since))
        else:
            self._json({"error": "not_found"}, 404)

    def do_POST(self):  # noqa: N802
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._json({"error": "json_required"}, 415)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json({"error": "bad_json"}, 400)

        path = urlparse(self.path).path
        try:
            if path == "/api/notes/fetch":
                url = (body.get("url") or _load_config().get("doc_url") or "").strip()
                text = fetch_doc_text(url)
                _save_config({**_load_config(), "doc_url": url})
                self._json({"text": text})
            elif path == "/api/notes/preview":
                notes, _, _ = _paths()
                self._json(diff_notes(_read(notes), body.get("text") or ""))
            elif path == "/api/notes/apply":
                text = body.get("text") or ""
                if not parse_notes(text)[0]:
                    return self._json({"error": "內容解析不到任何歌，沒有套用"}, 400)
                write_notes(text)
                JOB.request("Google Doc 更新")
                self._json({"ok": True})
            elif path == "/api/youtube":
                res = upsert_inbox(body)
                JOB.request(f"YouTube：{body.get('title')}")
                self._json({"ok": True, **res})
            elif path == "/api/inbox/remove":
                self._json({"ok": remove_inbox(body.get("title") or "")})
            elif path == "/api/job/run":
                JOB.request("手動執行")
                self._json({"ok": True})
            else:
                self._json({"error": "not_found"}, 404)
        except (ValueError, PermissionError) as err:
            self._json({"error": str(err)}, 400)
        except Exception as err:            # surface, don't kill the server
            self._json({"error": f"{type(err).__name__}: {err}"}, 500)

    def log_message(self, fmt, *args):
        pass


def serve_inbox(host: str = "127.0.0.1", port: int = 8767,
                then_cmd: str | None = None) -> None:
    """Run the inbox server until Ctrl-C."""
    global JOB
    then_cmd = then_cmd or os.environ.get("INBOX_POST_INGEST_CMD") or None
    JOB = _Job(then_cmd)
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print("MandpopDataset inbox — Google Doc + YouTube → ingest")
    print(f"  open  : http://127.0.0.1:{port}/")
    print(f"  then  : {then_cmd or '(none — set --then or INBOX_POST_INGEST_CMD)'}")
    print("  extension: load tools/youtube_inbox_extension/ in chrome://extensions")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()


PAGE = r"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>收歌站</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1f;--mute:#6b6b70;--line:#e3e3e0;--acc:#c2410c;--ok:#15803d;--bad:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#161617;--card:#212123;--fg:#eee;--mute:#9a9aa0;--line:#333336;--acc:#fb923c;--ok:#4ade80;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,"Noto Sans TC",sans-serif}
main{max-width:980px;margin:0 auto;padding:20px 16px 60px}h1{font-size:22px;margin:0 0 4px}.sub{color:var(--mute);margin:0 0 18px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
h2{font-size:16px;margin:0 0 10px}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input[type=text]{flex:1;min-width:200px}input,textarea{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 10px}
textarea{width:100%;height:220px;margin-top:10px;font-family:ui-monospace,Consolas,monospace;font-size:13px}
button{font:inherit;border:1px solid var(--line);background:var(--bg);color:var(--fg);border-radius:8px;padding:7px 14px;cursor:pointer}
button.pri{background:var(--acc);border-color:var(--acc);color:#fff}button:disabled{opacity:.5;cursor:default}
.diff{margin-top:12px;font-size:14px}.diff h3{font-size:14px;margin:10px 0 4px}.diff li{font-family:ui-monospace,Consolas,monospace;font-size:13px}
.add{color:var(--ok)}.rm{color:var(--bad)}.tag{font-size:11px;border:1px solid var(--line);border-radius:4px;padding:0 4px;margin-left:4px;color:var(--mute)}
.msg{margin-top:8px;color:var(--mute)}.err{color:var(--bad)}
ul{margin:4px 0;padding-left:20px}#inbox li{display:flex;justify-content:space-between;gap:8px;padding:3px 0;border-bottom:1px dashed var(--line)}
#inbox code{word-break:break-all}pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;height:280px;overflow:auto;font-size:12px;white-space:pre-wrap;margin:10px 0 0}
.status{font-weight:600}
</style></head><body><main>
<h1>收歌站</h1><p class="sub">Google Doc 或 YouTube 擴充功能 → 下載、分離、歌詞對齊 → 進歌單</p>

<section><h2>① Google Doc</h2>
<div class="row"><input type="text" id="docurl" placeholder="Google Doc 連結（設成「知道連結的任何人可檢視」）">
<button id="fetch">抓最新</button></div>
<textarea id="text" placeholder="或直接把整份 Doc 貼在這裡"></textarea>
<div class="row" style="margin-top:8px"><button id="preview">預覽差異</button>
<button id="apply" class="pri" disabled>套用並開始處理</button></div>
<div id="docmsg" class="msg"></div><div id="diff" class="diff"></div></section>

<section><h2>② YouTube 收件匣</h2>
<p class="msg" style="margin-top:0">擴充功能存的歌在這裡。Doc 有寫到同一首時以 Doc 為準；想永久保留就把這行貼進 Doc。</p>
<ul id="inbox"></ul></section>

<section><h2>③ 處理進度 <span id="jobstat" class="status"></span></h2>
<div class="row"><button id="run">重新執行 ingest</button><span id="postcmd" class="msg"></span></div>
<pre id="log"></pre></section>
</main><script>
const $=s=>document.querySelector(s);
const post=(u,b)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(async r=>{const j=await r.json();if(!r.ok)throw new Error(j.error||r.status);return j});
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let since=0, previewed=null;
function msg(t,bad){$('#docmsg').innerHTML=t?`<span class="${bad?'err':''}">${esc(t)}</span>`:''}
function renderInbox(list){$('#inbox').innerHTML=list.length?list.map(i=>`<li><code>${esc(i.line)}</code><span><button data-copy="${esc(i.line)}">複製</button> <button data-rm="${esc(i.title)}">刪除</button></span></li>`).join(''):'<li class="msg">（空）</li>'}
async function state(){const s=await fetch('/api/state').then(r=>r.json());
 $('#docurl').value=s.doc_url;if(!$('#text').value)$('#text').value=s.notes_text;
 renderInbox(s.inbox);$('#postcmd').textContent=s.post_cmd?`處理完接著：${s.post_cmd}`:'';since=0;$('#log').textContent='';}
$('#text').oninput=()=>{previewed=null;$('#apply').disabled=true};
$('#fetch').onclick=async()=>{msg('抓取中…');try{const r=await post('/api/notes/fetch',{url:$('#docurl').value});$('#text').value=r.text;msg('已抓到最新內容');await doPreview()}catch(e){msg(e.message,1)}};
async function doPreview(){try{const d=await post('/api/notes/preview',{text:$('#text').value});previewed=$('#text').value;
 const li=(a,f)=>a.length?'<ul>'+a.map(f).join('')+'</ul>':'<p class="msg">（無）</p>';
 $('#diff').innerHTML=`<p>共 ${d.total} 首，其中 ${d.setlist} 首有標記要唱</p>
 <h3 class="add">新增 ${d.added.length}</h3>${li(d.added,x=>`<li class="add">+ ${esc(x.line)}${x.setlist?'<span class="tag">進歌單</span>':''}${x.download?'<span class="tag">要下載</span>':''}</li>`)}
 <h3>變更 ${d.changed.length}</h3>${li(d.changed,x=>`<li>${esc(x.before)} → <b>${esc(x.after)}</b></li>`)}
 <h3 class="rm">移除 ${d.removed.length}</h3>${li(d.removed,x=>`<li class="rm">− ${esc(x.line)}</li>`)}
 ${d.warnings.length?'<h3 class="err">警告</h3><ul>'+d.warnings.map(w=>`<li class="err">${esc(w)}</li>`).join('')+'</ul>':''}`;
 $('#apply').disabled=false;msg('')}catch(e){msg(e.message,1)}}
$('#preview').onclick=doPreview;
$('#apply').onclick=async()=>{if(previewed!==$('#text').value)return doPreview();try{await post('/api/notes/apply',{text:previewed});msg('已寫入 notes.txt，開始處理');$('#apply').disabled=true;$('#diff').innerHTML=''}catch(e){msg(e.message,1)}};
$('#inbox').onclick=async e=>{const c=e.target.dataset.copy,r=e.target.dataset.rm;
 if(c){await navigator.clipboard.writeText(c);e.target.textContent='已複製'}
 if(r&&confirm(`從收件匣刪除「${r}」？（已下載的檔案不會刪）`)){await post('/api/inbox/remove',{title:r});state()}};
$('#run').onclick=()=>post('/api/job/run');
async function poll(){try{const j=await fetch('/api/job?since='+since).then(r=>r.json());
 if(j.lines.length){const pre=$('#log'),stick=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-20;pre.textContent+=j.lines.join('\n')+'\n';if(stick)pre.scrollTop=pre.scrollHeight}
 since=j.next;$('#jobstat').textContent=j.running?(j.pending?'處理中（還有一輪排隊）':'處理中…'):(j.last_rc==null?'閒置':j.last_rc===0?'✓ 完成':'✗ 失敗 rc='+j.last_rc);
 if(!j.running&&j.lines.length)fetch('/api/state').then(r=>r.json()).then(s=>renderInbox(s.inbox))}catch(e){}
 setTimeout(poll,1500)}
state().then(poll);
</script></body></html>
"""
