# [BR-20260820_103115-search-cards-flush] 搜尋結果卡片每 2.5 秒遭全量 DOM Flush 導致勾選狀態被清空

- **回報日期**: 2026-08-20T10:31:15+08:00
- **問題模組**: `Frontend / Search Results & Queue Polling` (`app/static/js/app.js`)
- **嚴重等級**: `High` (影響批次收書操作與基本卡片互動)
- **問題狀態**: `Confirmed / RCA Completed`

---

## 1. 問題症狀 (Symptom)
在首頁搜尋書目時，結果列表中的卡片每隔約 2.5 秒會出現明顯的重新渲染刷新（DOM Flush）。若使用者正在勾選卡片進行批次鏡像收書，勾選狀態會瞬間被清空歸零；若正在點擊「⋯」下拉選單或檢視按鈕，選單會被關閉且焦點遺失。

---

## 2. 根本原因分析 (Root Cause Analysis, RCA)

### (1) 觸發路徑與程式碼定位
1. **背景輪詢計時器**:
   - `app/static/js/app.js` 行 982–989：
     ```javascript
     function startQueuePolling() {
       if (queuePollInterval) clearInterval(queuePollInterval);
       queuePollInterval = setInterval(async () => {
         try {
           await refreshQueueModal();
         } catch (e) {}
       }, 2500); // 每 2.5 秒輪詢一次
     }
     ```
2. **無條件全量重繪 DOM**:
   - `app/static/js/app.js` 行 894–896 (`refreshQueueModal` 內部)：
     ```javascript
     // 若首頁當前正在呈現檢索結果，即時同步更新各卡片按鈕與狀態
     if (currentResults && currentResults.length > 0 && document.getElementById("resultsHeader").style.display !== "none") {
       applySortAndRender(); // 💥 每次輪詢直接觸發全列表 DOM 銷毀與重繪
     }
     ```
3. **狀態遺失機制**:
   - `applySortAndRender()` 會呼叫 `renderResults()`，以 `innerHTML = ...` 重新組裝所有 `.book-card`。
   - 由於目前前端尚未建立全域 `selectedWorkIds` / `selectedMd5s` Set 來持久化使用者的勾選狀態，導致每一次重新賦值 `innerHTML` 時，DOM 上的所有 Checkbox 直接恢復預設未勾選狀態。

---

## 3. 影響範圍 (Impact Assessment)
1. **批次鏡像收書失效**: 使用者無法在 2.5 秒內完成多本書籍的勾選操作。
2. **UI 閃爍與效能消耗**: 每次輪詢無差別重建所有卡片節點，造成瀏覽器不必要的 Reflow/Repaint 與事件監聽器重複綁定。
3. **互動中斷**: 下拉操作選單在輪詢瞬間被重置關閉。

---

## 4. 建議修復方案 (Remediation Plan)

### 方案 A: 狀態保留（State Persistence）
- 於 `app.js` 維護全域 `const selectedWorkIds = new Set();`。
- Checkbox `change` 事件時增刪 ID，`renderResults()` 時依據 Set 動態補上 `checked` 屬性。

### 方案 B: 差量原地更新（Fine-grained In-place Patching，推薦）
- **移除無條件 `applySortAndRender()`**：`refreshQueueModal()` 不再直接重繪整個結果列表。
- **僅針對下載狀態變更的卡片局部更新**：比對 `cachedJobsByMd5` 的狀態差異，僅在特定 MD5 下載進度或入庫完成時，透過 `document.querySelector(`[data-md5="${md5}"]`)` 局部置換該卡片的按鈕狀態（如轉為「📖 直讀」），其餘卡片保持靜態不刷新。

---
*Logged by Antigravity Bug Tracker System.*
