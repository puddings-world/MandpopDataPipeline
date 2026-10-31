// Floating "＋ 收歌" button on YouTube watch pages → panel → background → inbox.
(() => {
  const host = document.createElement("div");
  host.id = "mandpop-inbox";
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `
<style>
  :host{all:initial}
  *{box-sizing:border-box;font:14px/1.45 system-ui,"Noto Sans TC",sans-serif}
  .fab{position:fixed;right:20px;bottom:24px;z-index:2147483646;background:#c2410c;color:#fff;border:0;
       border-radius:999px;padding:10px 16px;font-weight:600;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.3)}
  .panel{position:fixed;right:20px;bottom:76px;z-index:2147483647;width:340px;max-width:calc(100vw - 32px);
         background:#fff;color:#1d1d1f;border:1px solid #ddd;border-radius:12px;padding:14px;
         box-shadow:0 10px 30px rgba(0,0,0,.25);display:none}
  @media (prefers-color-scheme:dark){.panel{background:#212123;color:#eee;border-color:#3a3a3d}
    input,select,textarea{background:#161617!important;color:#eee!important;border-color:#3a3a3d!important}}
  .panel.open{display:block}
  h3{margin:0 0 8px;font-size:15px;font-weight:700}
  label{display:block;margin:8px 0 2px;font-size:12px;opacity:.75}
  input[type=text],select,textarea{width:100%;padding:6px 8px;border:1px solid #ccc;border-radius:6px;background:#fff;color:#111}
  textarea{height:44px;resize:vertical}
  .key{display:flex;align-items:center;gap:8px}
  .key button{width:34px;height:30px;border:1px solid #ccc;border-radius:6px;background:transparent;color:inherit;cursor:pointer;font-size:16px}
  .key output{min-width:44px;text-align:center;font-weight:700;font-size:16px}
  .checks{display:flex;gap:14px;margin-top:10px}.checks label{display:flex;gap:4px;align-items:center;margin:0;font-size:13px;opacity:1}
  .info{margin-top:8px;font-size:12px;padding:6px 8px;border-radius:6px;background:rgba(194,65,12,.1)}
  .info:empty{display:none}
  .actions{display:flex;justify-content:flex-end;gap:8px;margin-top:12px}
  .actions button{border-radius:6px;padding:6px 14px;cursor:pointer;border:1px solid #ccc;background:transparent;color:inherit}
  .actions .save{background:#c2410c;border-color:#c2410c;color:#fff;font-weight:600}
  .status{margin-top:8px;font-size:12px;white-space:pre-wrap}.ok{color:#15803d}.err{color:#dc2626}
</style>
<button class="fab" title="把這支影片存進收歌站">＋ 收歌</button>
<div class="panel">
  <h3>收進歌庫</h3>
  <label>歌名</label><input type="text" id="title">
  <label>歌手</label><input type="text" id="artist">
  <label>預設升降（app 播放時套用，現場還是可以再調）</label>
  <div class="key"><button id="down">−</button><output id="key">0</output><button id="up">＋</button></div>
  <div class="info" id="vkey"></div>
  <label>版本</label>
  <select id="gender"><option value="">不分</option><option value="female">女版</option><option value="male">男版</option></select>
  <div class="checks">
    <label><input type="checkbox" id="sing" checked>進歌單</label>
    <label><input type="checkbox" id="fav">最愛 !</label>
  </div>
  <label>備註</label><textarea id="comment"></textarea>
  <div class="info" id="known"></div>
  <div class="actions"><button id="cancel">取消</button><button class="save" id="save">存進收歌站</button></div>
  <div class="status" id="status"></div>
</div>`;

  const $ = (s) => root.querySelector(s);
  const panel = $(".panel");
  let key = 0;
  let videoKey = 0;
  const setKey = (k) => { key = Math.max(-12, Math.min(12, k)); $("#key").value = (key > 0 ? "+" : "") + key; };

  const videoIdOf = (u) => new URL(u).searchParams.get("v");
  const pageTitle = () =>
    document.querySelector("h1.ytd-watch-metadata yt-formatted-string")?.textContent?.trim() ||
    document.title.replace(/\s+-\s+YouTube$/, "").replace(/^\(\d+\)\s*/, "");
  const pageChannel = () =>
    document.querySelector("ytd-watch-metadata ytd-channel-name a")?.textContent?.trim() ||
    document.querySelector("#owner #channel-name a")?.textContent?.trim() || "";

  function status(text, cls) { $("#status").className = "status " + (cls || ""); $("#status").textContent = text; }

  function open() {
    const raw = pageTitle();
    const p = SongParse.parseVideo(raw, pageChannel());
    $("#title").value = p.title;
    $("#artist").value = p.artist;
    $("#gender").value = p.gender || "";
    $("#fav").checked = false;
    $("#sing").checked = true;
    videoKey = p.videoKey;
    setKey(0);
    $("#comment").value = videoKey ? `影片本身 ${videoKey > 0 ? "+" : ""}${videoKey}key` : "";
    $("#vkey").textContent = videoKey
      ? `這支影片本身已經${videoKey < 0 ? "降" : "升"} ${Math.abs(videoKey)} key，下載的伴奏就是這個調；上面的升降是播放時「再」調整，通常保持 0。`
      : "";
    $("#known").textContent = "";
    status("");
    panel.classList.add("open");

    chrome.runtime.sendMessage({ type: "guess", text: raw }, (r) => {
      if (!r?.ok) return status("收歌站沒開：在 D:\\MandpopDataset 執行 python pipeline.py inbox", "err");
      if (!r.title) return;
      $("#title").value = r.title;
      if (r.artist && !$("#artist").value) $("#artist").value = r.artist;
      const same = r.youtube_url && r.youtube_url.includes(videoIdOf(location.href));
      $("#known").textContent = same
        ? `資料集已經有「${r.title}」，而且就是這支影片。送出只會更新升降 / 歌單設定。`
        : r.processed
          ? `資料集已經有「${r.title}」（別的影片版本）。送出會改用這支影片重做伴奏。`
          : `資料集有「${r.title}」的紀錄但還沒處理好，送出會用這支影片處理。`;
    });
  }

  $(".fab").onclick = () => (panel.classList.contains("open") ? panel.classList.remove("open") : open());
  $("#cancel").onclick = () => panel.classList.remove("open");
  $("#down").onclick = () => setKey(key - 1);
  $("#up").onclick = () => setKey(key + 1);

  $("#save").onclick = () => {
    const payload = {
      url: location.href,
      title: $("#title").value.trim(),
      artist: $("#artist").value.trim(),
      key_offset: key,
      video_key: videoKey,
      gender_variant: $("#gender").value || null,
      favorite: $("#fav").checked,
      sing: $("#sing").checked,
      comment: $("#comment").value.trim(),
    };
    if (!payload.title) return status("歌名必填", "err");
    $("#save").disabled = true;
    status("送出中…");
    chrome.runtime.sendMessage({ type: "save", payload }, (r) => {
      $("#save").disabled = false;
      if (!r) return status("擴充功能沒有回應，重新整理頁面再試", "err");
      if (r.offline) return status("收歌站沒開：在 D:\\MandpopDataset 執行 python pipeline.py inbox", "err");
      if (!r.ok) return status(r.error, "err");
      status(`✓ 已存：${r.line}　背景處理中，進度看 http://127.0.0.1:8767/` +
             (r.doc_conflict ? `\n⚠ ${r.doc_conflict}` : ""), r.doc_conflict ? "err" : "ok");
    });
  };

  chrome.runtime.onMessage.addListener((m) => { if (m.type === "toggle" && isWatch()) $(".fab").click(); });

  const isWatch = () => location.pathname === "/watch";
  function sync() {
    host.style.display = isWatch() ? "" : "none";
    if (!isWatch()) panel.classList.remove("open");
  }
  document.documentElement.appendChild(host);
  document.addEventListener("yt-navigate-finish", () => { panel.classList.remove("open"); sync(); });
  sync();
})();
