# BR-20260820_124500 — 「加入自訂書單」選單需等十幾秒才顯示內容

Status: OPEN
Owner: ses_fe7b5cbadffeSlxj0dv1Z740O4（值星官）
Family: frontend-blocking-io
Filed: 2026-08-20 by ses_fe7b5cbadffeSlxj0dv1Z740O4
Reported-by: 使用者（附截圖，選單顯示「載入中...」）

**Related**:
- `BR-20260820_111523-mirror-resolver-dead-mirrors` — **同一種失效類別「缺席態與失敗態共用同一個輸出」**，
  該案在 crawler 層（查封頁與查無此書都回 None），本案在前端 extension 橋接層
  （extension 不存在、沒回應、回錯誤，三者都收斂成一個靜默的 3 秒 timeout）。
- 檢索阻塞事故（handler `ses_fe2894298ffeUsqILfc6fgFmlj` 處理中）— **同一條執行路徑**：
  兩者都是 `app/static/js/app.js` 的「等一個慢來源，期間畫面只有 loading」，
  且兩者都用空的 catch 吞掉失敗。本案是該病灶在 collections modal 的第二個實例。

## 一句話

點「加入自訂書單」後選單空白約十幾秒才浮現。**後端不是瓶頸**（實測 0.26s），
瓶頸在前端對 Chrome extension 的同步等待——`callExtension()` 每次失敗要靜默耗掉 **3000ms**，
而 `isChromeExtensionAvailable` 一旦被設為 true 就**永不回退**，於是 extension 死掉後
每次呼叫都固定付 3 秒，且畫面上看不出任何原因。

## 後端已排除（實測，非推論）

```
GET /api/collections                      http=200  time=0.261
GET /api/collections（第二次，測快取）      http=200  time=0.266
GET /api/collections/col_favorites        http=200  time=0.268
collections count = 4
  col_favorites          我的最愛
  col_9222f3d4d8024d99   科幻經典
  col_a9cfca019b8c4c24   宇宙天文探秘
  col_1e44822709e04f13   Raw
```

四個 collection、每個端點 0.26 秒。**十幾秒不可能來自後端。**

## 前端證據

> ⚠ **行號以 2026-08-20 12:40 的工作樹為準，且該檔正被另一顆 handler 即時編輯。**
> 同一輪調查內 `function callExtension` 從 1404 行漂移到 1524 行（+120）。
> 下一個接手者請以**符號名**定位，不要信任本文行號。

### D1 — `callExtension()` 的 3 秒靜默 timeout

```js
function callExtension(action, payload = {}) {
  return new Promise((resolve) => {
    if (!isChromeExtensionAvailable && action !== "PING") {
      resolve({ success: false, error: "Extension not available" });
      return;
    }
    ...
    window.postMessage({ source: "CMS_WEB_APP", action, requestId, payload }, "*");

    setTimeout(() => {
      if (extensionCallbacks.has(requestId)) {
        extensionCallbacks.delete(requestId);
        resolve({ success: false, error: "Timeout" });   // ← 靜默 resolve，不 reject
      }
    }, 3000);
  });
}
```

**它 resolve 而不 reject**，所以呼叫端的 `try/catch` 抓不到，只會拿到一個 falsy 結果然後往下走。
「extension 沒安裝」「extension 裝了但沒回應」「extension 回了錯誤」三態
在呼叫端**完全無法區分**——這正是本 repo 已經記錄過一次的失效類別。

### D2 — `isChromeExtensionAvailable` 是單向閂，永不回退

全檔對該旗標只有**一處寫入為 true**（PING 回應時），**沒有任何一處寫回 false**。
所以只要曾經偵測到 extension（分頁重載前、extension 被停用前、擴充崩潰前），
旗標就永遠是 true，之後每一次 `callExtension` 都會走完整的 3 秒 timeout。

**這格是「十幾秒」與「3 秒」之間的橋**：單次呼叫只有 3 秒，但旗標不回退使得
路徑上每一個 extension 呼叫都各自付一次。

### D3 — modal 載入路徑先打 extension 才回退後端

載入書單清單的函式（`callExtension("GET_TREE")`，約 2714 行）：
extension 分支優先，失敗才走 `Promise.all([/api/collections, /api/collections/work/<id>/status])`。
後端那段只要 0.26 秒，卻被排在 3 秒之後。

### D4 — 儲存路徑是**序列**迴圈，N 個書單 = N × 3 秒

```js
for (const cb of checkboxes) {
  ...
  await callExtension("ADD_BOOKMARK", { ... });     // ← 迴圈內 await
}
```

對照組是同一函式的後端分支，它用 `promises.push(...)` + `await Promise.all(promises)` —— **並行**。
extension 分支卻是逐一 await。以現有 4 個書單計算：**4 × 3000ms = 12 秒**，
與使用者回報的「十幾秒」數量級吻合。

> ⚠ **此為假設，未實測**。使用者描述的是「選單浮現」（載入路徑 D3），
> 而 12 秒的算式來自儲存路徑（D4）。兩者都成立但不是同一條路。
> 需要瀏覽器實測才能判定使用者實際遇到的是哪一條，或兩條都遇到。

## 使用者感受得到的傷害

每次加入書單都要空等十幾秒，畫面只顯示「載入中...」，
**沒有任何訊息說明它在等什麼、或等的東西已經不在了**。
使用者無從得知這與 Chrome extension 有關，也無法自行繞過。

## 修復方向（未實作）

1. **D2 優先**：`callExtension` timeout 時把 `isChromeExtensionAvailable` 設回 false，
   後續呼叫立即短路。**單向閂改成雙向**——這一格最便宜且效果最大。
2. **D1**：把三態分開。timeout / not-available / error-response 要能被呼叫端區分，
   並在 UI 上呈現（例如 badge 從 🟢 變 ⚪ 並附「擴充套件已離線，改用本地模式」）。
3. **D3**：改成 extension 與後端**並行**發動，先回來的先渲染
   （與檢索阻塞事故同一個處方，可共用實作）。
4. **D4**：extension 分支改用 `Promise.all`，與後端分支一致。
5. timeout 常數 3000ms 抽成具名常數並下調（extension 是同頁 postMessage，
   正常回應是毫秒級；3 秒對「活著的 extension」毫無必要，只服務於「死掉的 extension」）。

## 驗收判準

- [ ] extension 不存在 / 已離線時，選單在 **1 秒內**顯示內容（走後端）
- [ ] **負控制組**：extension 正常時，仍能正確顯示 Chrome 書籤資料夾（證明沒有誤殺 extension 路徑）
- [ ] extension 離線時 UI 有可見提示，非靜默降級
- [ ] `isChromeExtensionAvailable` 在全檔有 **≥1 處**寫回 false（目前為 0 處）
- [ ] 儲存 4 個書單的耗時不隨書單數線性增長

## 沒驗證的

- **未進瀏覽器實測**。全部為後端計時 + 靜態閱讀，未用 Playwright 量過真實的
  「點擊 → 內容浮現」耗時，因此**無法證明使用者遇到的就是這條路徑**。
  這是本 BR 最大的未驗證面。
- 未確認使用者環境中 extension 的實際狀態（有裝但離線？沒裝？裝了但版本不符？）。
  三種情況觸發的路徑不同。
- `/api/collections/work/<id>/status` 端點未單獨計時（只測了 `/api/collections` 與 detail）。
- 本 BR 撰寫期間 `app/static/js/app.js` 正被 handler `ses_fe2894298ffeUsqILfc6fgFmlj`
  即時編輯，**上述行號與部分程式碼片段可能已經不是當前狀態**。

## 排程備註

**不要與檢索阻塞事故並行派工**——兩者同檔（`app/static/js/app.js`）、
同病灶（阻塞式等待 + 空 catch），並行會撞車。
建議在 `ses_fe2894298ffeUsqILfc6fgFmlj` 交件驗收後，由同一顆 handler 續作，
它屆時已有該檔的完整脈絡。
