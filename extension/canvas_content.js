/* Runs only after the user grants the configured HTTPS Canvas host permission. */
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type !== "canvas-get") return;
  (async () => {
    const target = new URL(message.url);
    if (target.origin !== location.origin || target.protocol !== "https:") throw new Error("Rejected request outside this Canvas origin.");
    const response = await fetch(target.href, { method: "GET", credentials: "include", redirect: "error", headers: { "Accept": "application/json" } });
    const text = await response.text();
    if (text.length > 2_000_000) throw new Error("Canvas response exceeds extension safety limit.");
    let body;
    try { body = JSON.parse(text); } catch (_) { throw new Error("Canvas returned non-JSON; sign in again if needed."); }
    sendResponse({ status: response.status, headers: { link: response.headers.get("link") || "", content_type: response.headers.get("content-type") || "" }, body });
  })().catch(error => sendResponse({ status: 599, headers: {}, body: { error: String(error.message || error) } }));
  return true;
});
