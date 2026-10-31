// Network hop lives here: the service worker is exempt from CORS / Private
// Network Access via host_permissions, a youtube.com content script is not.
const INBOX = "http://127.0.0.1:8767";

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg.type !== "save") return false;
  fetch(`${INBOX}/api/youtube`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(msg.payload),
  })
    .then(async (r) => {
      const j = await r.json().catch(() => ({}));
      reply(r.ok ? { ok: true, ...j } : { ok: false, error: j.error || `HTTP ${r.status}` });
    })
    .catch(() => reply({ ok: false, offline: true }));
  return true; // async reply
});

chrome.action.onClicked.addListener((tab) => {
  if (tab.id != null) chrome.tabs.sendMessage(tab.id, { type: "toggle" }).catch(() => {});
});

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg.type !== "guess") return false;
  fetch(`${INBOX}/api/guess?text=${encodeURIComponent(msg.text)}`)
    .then((r) => r.json())
    .then((j) => reply({ ok: true, ...j }))
    .catch(() => reply({ ok: false, offline: true }));
  return true;
});
