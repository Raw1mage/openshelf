// CMS圖書館 Chrome Extension - Content Script Bridge

// 在網頁端標註擴充套件已注入
window.__CMS_EXTENSION_ACTIVE__ = true;

// 監聽網頁發送的 CustomEvent 或 postMessage
window.addEventListener("message", async (event) => {
  // 只接收來自同 window 的訊息
  if (event.source !== window) return;
  const data = event.data;
  if (!data || data.source !== "CMS_WEB_APP") return;

  const { action, requestId, payload } = data;

  try {
    const response = await chrome.runtime.sendMessage({ action, payload });
    window.postMessage({
      source: "CMS_EXTENSION",
      action: `${action}_RESPONSE`,
      requestId: requestId,
      response: response
    }, "*");
  } catch (err) {
    window.postMessage({
      source: "CMS_EXTENSION",
      action: `${action}_RESPONSE`,
      requestId: requestId,
      response: { success: false, error: err.message }
    }, "*");
  }
});

// 發送擴充套件就緒信號給前端網頁
window.postMessage({
  source: "CMS_EXTENSION",
  action: "READY",
  version: "1.0.0"
}, "*");
