const ids = ["canvasOrigin", "bridgeUrl", "bridgeSecret"];
chrome.storage.local.get(ids, values => ids.forEach(id => document.getElementById(id).value = values[id] || (id === "bridgeUrl" ? "http://127.0.0.1:8765" : "")));
document.getElementById("save").addEventListener("click", async () => {
  const result = document.getElementById("result");
  try {
    const origin = new URL(document.getElementById("canvasOrigin").value).origin;
    if (!origin.startsWith("https://")) throw new Error("Canvas origin must use HTTPS.");
    const bridge = new URL(document.getElementById("bridgeUrl").value);
    if (bridge.protocol !== "http:" || bridge.hostname !== "127.0.0.1") throw new Error("Bridge URL must use 127.0.0.1.");
    const granted = await chrome.permissions.request({ origins: [`${origin}/*`] });
    if (!granted) throw new Error("Chrome host permission was not granted.");
    await chrome.storage.local.set({ canvasOrigin: origin, bridgeUrl: bridge.origin, bridgeSecret: document.getElementById("bridgeSecret").value });
    const response = await chrome.runtime.sendMessage({ type: "options-saved", origin });
    if (response.error) throw new Error(response.error);
    result.textContent = "Saved. The extension can now read only this Canvas origin via GET.";
  } catch (error) { result.textContent = `Not saved: ${error.message}`; }
});
