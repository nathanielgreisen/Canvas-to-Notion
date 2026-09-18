const DEFAULTS = { canvasOrigin: "", bridgeUrl: "http://127.0.0.1:8765", bridgeSecret: "" };

async function settings() { return { ...DEFAULTS, ...(await chrome.storage.local.get(DEFAULTS)) }; }
function canvasOrigin(value) {
  const u = new URL(value);
  if (u.protocol !== "https:" || u.pathname !== "/" || u.search || u.hash) throw new Error("Canvas origin must be an HTTPS origin without a path.");
  return u.origin;
}
function bridgeUrl(value) {
  const u = new URL(value);
  if (u.protocol !== "http:" || u.hostname !== "127.0.0.1") throw new Error("Bridge URL must use http://127.0.0.1 only.");
  return u.origin;
}
async function bridgeFetch(path, opts = {}) {
  const s = await settings();
  const url = bridgeUrl(s.bridgeUrl) + path;
  if (!s.bridgeSecret) throw new Error("Set the bridge secret in extension Options.");
  const response = await fetch(url, { ...opts, headers: { "Content-Type": "application/json", "X-Bridge-Secret": s.bridgeSecret, ...(opts.headers || {}) } });
  if (!response.ok) throw new Error(`Local bridge HTTP ${response.status}`);
  return response.status === 204 ? null : response.json();
}
async function registerCanvasScript(origin) {
  const id = "canvas-read-only-worker";
  try { await chrome.scripting.unregisterContentScripts({ ids: [id] }); } catch (_) { /* no prior registration */ }
  await chrome.scripting.registerContentScripts([{ id, matches: [`${origin}/*`], js: ["canvas_content.js"], runAt: "document_idle", persistAcrossSessions: true }]);
}
async function canvasTab(origin) {
  const tabs = await chrome.tabs.query({ url: `${origin}/*` });
  if (tabs.length) return tabs[0];
  const tab = await chrome.tabs.create({ url: origin + "/", active: false });
  await new Promise(resolve => {
    const timer = setTimeout(() => { chrome.tabs.onUpdated.removeListener(done); resolve(); }, 10000);
    function done(tabId, change) {
      if (tabId === tab.id && change.status === "complete") { clearTimeout(timer); chrome.tabs.onUpdated.removeListener(done); resolve(); }
    }
    chrome.tabs.onUpdated.addListener(done);
  });
  return tab;
}
async function sendCanvasGet(tabId, message) {
  try {
    return await chrome.tabs.sendMessage(tabId, message);
  } catch (_) {
    // Dynamic registrations apply on navigation. Inject once for an already-open
    // tab (and again after a reload) before retrying the message.
    await chrome.scripting.executeScript({ target: { tabId, frameIds: [0] }, files: ["canvas_content.js"] });
    return chrome.tabs.sendMessage(tabId, message);
  }
}
async function dispatch(job) {
  if (job.method !== "GET") throw new Error("Extension rejects non-GET Canvas jobs.");
  const s = await settings(); const origin = canvasOrigin(s.canvasOrigin);
  const target = new URL(job.url);
  if (target.origin !== origin || target.protocol !== "https:") throw new Error("Extension rejected a non-configured Canvas URL.");
  const tab = await canvasTab(origin);
  const response = await sendCanvasGet(tab.id, { type: "canvas-get", url: target.href });
  await bridgeFetch(`/v1/jobs/${encodeURIComponent(job.job_id)}/result`, { method: "POST", body: JSON.stringify(response) });
}
let polling = false;
async function pollLoop() {
  if (polling) return;
  polling = true;
  try {
    const s = await settings();
    if (!s.canvasOrigin || !s.bridgeSecret) return;
    // Keep one authenticated long-poll open. Every queued Canvas request is
    // collected immediately after the preceding one—there is no alarm delay.
    while (true) {
      const job = await bridgeFetch("/v1/jobs/next");
      if (job && job.job_id) await dispatch(job);
    }
  } catch (error) { console.warn("Canvas bridge poll failed:", error.message); }
  finally { polling = false; }
}
chrome.runtime.onInstalled.addListener(() => { chrome.alarms.create("poll", { periodInMinutes: 0.5 }); pollLoop(); });
chrome.runtime.onStartup.addListener(pollLoop);
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === "poll") pollLoop(); });
chrome.runtime.onMessage.addListener((message, _sender, respond) => {
  if (message.type === "options-saved") registerCanvasScript(message.origin).then(() => { pollLoop(); respond({ ok: true }); }).catch(error => respond({ error: error.message }));
  return true;
});
