# BR-20260820_124500 — 「加入自訂書單」選單需等十幾秒才顯示內容

Status: **CLOSED（前端已修 ＋ 後端殘留以機制消除結案）** —— 使用者裁示 2026-08-21「重量一次再決定」，重量後尖峰消失。
  ⚠ 本 BR **未**證明那組 20-27s 的成因，它至今 **UNDECIDED**——結案依據是兩個候選成因
  （DB 在 NFS、`q=the` 慢查詢）均已修復且重量不再復現，不是找到了答案。
  重量為 **idle 條件**（collections max=3.9ms / search max=128ms / over2s=0），
  **量不到不等於不存在**。復發時的分形狀重開條件見「⬆ 2026-08-21」節。
Owner: ses_fe7b5cbadffeSlxj0dv1Z740O4（值星官）
Family: frontend-blocking-io
Filed: 2026-08-20 by ses_fe7b5cbadffeSlxj0dv1Z740O4
Reported-by: 使用者（附截圖，選單顯示「載入中...」）
Partial-since: 2026-08-20（前端阻斷級缺陷已修並 commit `76efe94`；後端延遲殘留，見文末「勘誤與殘留」）
Closed: 2026-08-21 by ses_fe7b5cbadffeSlxj0dv1Z740O4

> ⚠️ **本 BR 的原始假說已被實測推翻，全文以下的 D1/D2/D4 分析僅保留作錯誤紀錄。**
> 真正的根因與殘留項在文末「勘誤與殘留」一節。**接手者請先讀那一節。**

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

---

# 勘誤與殘留（2026-08-20 收攏，值星官 ses_fe7b5cbadffeSlxj0dv1Z740O4）

## 一、本 BR 的原始假說錯了，D1/D2/D4 全部不成立

我原本寫「`callExtension()` 每次失敗要靜默耗掉 3000ms，旗標永不回退，於是每次呼叫都固定付 3 秒」。
handler `ses_fe2894298ffeUsqILfc6fgFmlj` 用 stub content script 做了決定性矩陣，**推翻它**：

| extension 狀態 | `isChromeExtensionAvailable` | 選單浮現 | 列數 |
|---|---|---|---|
| `none`（完全沒有） | false | 24,555 ms | 4（後端書單） |
| `silent`（回 PING、其餘不回 → **每次真的等滿 3000ms**） | true | 25,107 ms | 4 |
| `responsive`（全部即時回應） | true | **179 ms** | 2（Chrome 資料夾） |

`silent` 與 `none` 只差 **552ms**，不是十幾秒。**timeout 從來就不是主因。**

根因是 `app.js` 的 `callExtension` 開頭就有守衛：

```js
if (!isChromeExtensionAvailable && action !== "PING") {
  resolve({ success: false, error: "Extension not available" });
  return;                       // ← 立即返回，3000ms setTimeout 根本沒被排上
}
```

extension 不在時 `callExtension` **從不等待**。我寫 BR 時只讀了 `setTimeout` 那段，
沒讀函式開頭的守衛就下了結論——**靜態閱讀下的推論，未經實測就寫成「證據」**。
本 BR 自己在「沒驗證的」一節寫過「未進瀏覽器實測，這是本 BR 最大的未驗證面」，
那句話是對的，而我仍然把推論寫進了標題與「一句話」。

`responsive` 那格之所以 179ms，是因為它在 extension 分支提前 `return`，
**後端路徑從未執行**——這反過來成為「慢的是後端」的旁證。

## 二、handler 找到的真缺陷：選單根本打不開（阻斷級）

**已修，commit `76efe94`。**

`app.js:718` 等 7 處把使用者資料塞進 inline onclick 的 JS 字串字面值，
而 `escapeHtml()` 只轉 `& < > "`，**不轉單引號**：

```js
onclick="openQuickCollection('${item.work_id}', '${escapeHtml(item.title)}')"
```

書庫第一本書叫 `Silberschatz's Operating System Concepts`（實測 ASCII U+0027，
不是排版撇號 U+2019），撇號直接截斷字串字面值 →
`SyntaxError: missing ) after argument list` → **按鈕點了完全沒反應**。

實測不是邊角：公網 25 筆有 2 筆帶撇號（`Beginner's` ×2），本地 2 筆有 1 筆。

修法是新增 `escapeJsArg()`，**先做 JS 字面值跳脫、再做 HTML 屬性跳脫**——
順序不可顛倒，因為 HTML 屬性會先被解碼再交給 JS parser，所以把 `'` 轉成 `&#39;` 沒用。

值星官獨立重放（node，模擬 HTML 屬性解碼後餵給 JS parser）：

```
OLD (escapeHtml)  → PARSE_FAIL: missing ) after argument list
NEW (escapeJsArg) → PARSE_OK，且 roundtrip 值與原標題逐字元相等（ROUNDTRIP_EXACT: true）
```

**這解釋了使用者截圖裡的「載入中...」為何不會前進**——它不是慢，是那次點擊
根本沒有觸發任何載入；畫面上的是上一次殘留或初始態。

## 三、真正的殘留：那 20-27 秒在後端，本 BR 未修 → **2026-08-21 已重量，尖峰消失，見本節末尾**

handler 用四輪量測定案，值星官複核其證據鏈：

```
PerformanceResourceTiming   QUEUED 1ms / TTFB 26,665ms / download 1ms   ← 排除連線排隊
A/B 儀器對照（不註冊 page.on）  12,006 / 20,049 / 27,593 ms              ← 排除 Playwright 儀器
同刻 browser vs host curl    7,734ms vs 7,878ms                        ← 一致，定案在後端
```

**與 `/api/search` 偶發 4.5s 形狀相同**（同一支 API 忽快忽慢、host 與 browser 同步慢），
很可能同一根因。

### `--reload` 假說：實測未能重現，不是結論

handler 列了線索：`docker compose logs` 顯示 handler C 工作期間有 7 次 `WatchFiles ... Reloading`，
時間吻合但未證明因果。值星官做了決定性實驗（`touch app/db/search.py` 強制觸發 reload，
前後各取樣）：

```
reload 前 5 次   0.267 / 0.315 / 0.242 / 0.251 / 0.247 s
reload 後 10 次  0.255 / 0.250 / 1.999 / 0.241 / 0.222 / 0.252 / 0.243 / 0.276 / 0.220 / 0.232 s
CONTROL          docker compose logs 確認 "WatchFiles detected changes in 'app/db/search.py'. Reloading..." 命中 1
```

reload 確實製造了一次 **2.0s** 的尖峰（post3），但**沒有重現 20-27s**。

**這是 UNDECIDABLE，不是排除**：reload 尖峰的數量級（2s）比觀察到的（20-27s）小一個數量級，
但我的取樣是在單一 host、無並發負載、無 crawler 在跑的條件下做的。
handler 觀察到 20-27s 的當下有瀏覽器、輪詢、可能還有公網爬蟲在跑。
**「我沒重現出來」不等於「它不是成因」。**

### 後續應查的方向（未做）

1. 在**有負載**的條件下重做上述實驗（同時跑 `/api/crawler/search` 再打 `/api/collections`）。
2. 查 SQLite 連線是否為單一共用且無 pool——若是，任何長查詢都會排隊在同一把鎖後面，
   而 `crawler/jobs` 輪詢之所以不受影響可能只是因為它讀的是別的東西。
3. 這格若查實，`BR-20260820_124500` 與 `/api/search` 偶發 4.5s 應合併成一張後端 BR。

### ⚠ 本節的事實基礎已過期（dispatcher，2026-08-21）

**上方那組 20-27s 是 `c663041`（08-20 13:52）量的。而 DB 從 NFS 搬到本地 ext4 是
`dec5b44`（08-20 17:58）——晚四小時。本節從未在搬移後重量。**

```
git log -1 --format='%h %ad' --date=format:'%m-%d %H:%M' dec5b44
  → dec5b44 08-20 17:58   fix(infra): SQLite DB 從 NAS(NFS) 搬到本地 ext4
git log -1 --format='%h %ad' --date=format:'%m-%d %H:%M' -- <本檔>
  → c663041 08-20 13:52
```

dispatcher 於 2026-08-21 量到 idle 下 `/api/collections` median **4ms** / max 6ms（20/20 rc=0），
**但那不能銷帳**——尖峰是偶發、有負載時發生的，而「我沒量到」與「它不存在」共用同一個輸出。

**本節與 `BR-20260820_160000` 判準 8 是同一個觀察對象**（同一支 API、同樣忽快忽慢、
host 與 browser 同步慢），已合併判定，見該 BR 的「觀察期紀錄」節。
已派 handler `ses_fdf8fc2c4ffeHyJI2iGo3sB5b8` 在**有負載條件下**重做，
上方第 1、2 點正是該派工單交給 handler 推翻 dispatcher 的其中兩格。

**在該 handler 交件前，不要照上方的 20-27s 數字做任何判斷。**

## 四、handler 順帶修的（在授權邊界內，且正是本 BR 的原始目標）

等待/空/失敗三態現在可區分（原本三者共用同一行靜態「載入書單中...」）：

| case | `data-quick-state` | 畫面 |
|---|---|---|
| A `/api/collections` 永不 resolve | `loading` | `載入書單中… 已等待 N 秒`（每秒遞增） |
| B 回 `[]` | `empty` | `📖 尚未建立任何個人書單` |
| C 回 500 | `error` | `⚠️ 載入書單失敗 書單清單 HTTP 500` |

`ALL_THREE_DISTINCT: True`。另加 `!res.ok → throw`（原本 500 會走到 `res.json()`
才炸成 `Unexpected token`）與 `quickCollectionReqId` 競態守衛。

## 五、範圍外、已具備修法、建議另開一包

同樣的撇號漏洞仍存在於三處**不在書單路徑上**的呼叫點（值星官獨立 grep 確認）：

```
app.js:1236  saveSingleBookToLocalDisk('${j.work_id}', '${escapeHtml(j.title)}', ...)
app.js:1653  showBrDetailModal('${m.br_id}', '${escapeHtml(m.last_error || '')}')
app.js:2903  handleCategoryClick('${node.category_id}', '${escapeHtml(node.name)}', ...)
```

另外 4 個已改為 `escapeJsArg` 但**未實測**的呼叫點（需既有書單資料才觸發得到）：
`removeChromeBookmark` / `renameCollectionPrompt` / `deleteCollectionPrompt` /
`removeBookFromCollection`。改法與已驗證的 `openQuickCollection` 完全相同。

## ⬆ 2026-08-21：§三 殘留重量（使用者裁示「重量一次再決定」）

### 為什麼要重量：這個殘留的事實基礎已被改變過兩次

§三那組 20-27s 是 `c663041`（08-20 13:52）量的。在那之後：

```
dec5b44  08-20 17:58   DB 從 NFS 搬到本地 ext4
 a31e925  08-21 早      /api/search?q=the 從 92s 降到 0.1s（snippet 逐列子查詢 → instr+substr）
```

**兩次都改變了那組量測的前提，而殘留從未重量。**
一張事實基礎已知過期的 BR 掛在頂層，下一個 cold-context 的人會對著 stale 的數字推論。

### 量到什麼（dispatcher 親手，2026-08-21）

```
collections  n=20  med=3.4ms    max=3.9ms    over20s=0  over2s=0  codes={200}
search_the   n=20  med=118.5ms  max=128.4ms  over20s=0  over2s=0  codes={200}
CONTROL_404  n=3   max=1.8ms    codes={404}            ★控制組有鑑別力
```

**原 20-27s ⇒ 現 max 3.9ms，差四個數量級。**

### ⚠ 這組量測證不了的（實事求是地標出來）

1. **這是 idle 量測。** 原始回報發生在使用者真實操作瀏覽器時，而本次是空載連發 curl。
   **量不到不等於不存在**——尤其原始尖峰本來就是偶發的。
2. **未證明因果。** 兩個候選成因（DB 在 NFS、`q=the` 慢查詢）都已修，
   但**沒有直接證據**證明那組 20-27s 就是這兩者之一造成的。
3. **這與 BR-20260820_160000 是同一種結案方式**：以**機制消除**結案，不是以**找到成因**結案。

### 復發時的重開條件（寫給下一個人）

若使用者再回報「加入自訂書單」選單慢，**先分形狀再決定開哪張**：

```
前端選單根本打不開 / 點了無反應      ⇒ 本 BR 的 §二形狀，重開本張
選單打得開但內容等很久，且 host curl 同刻也慢  ⇒ 後端，先量三支探針：
    collections/search 尖峰而 jobs 同刻正常   ⇒ BR-20260820_160000 的形狀
    三支同時尖峰                              ⇒ 新機制，開新張，不要採舊帳
```

## 六、為何本 BR 曾是 PARTIAL（已於 2026-08-21 結案）

~~前端阻斷級缺陷已修並已 commit（`76efe94`），**但使用者原始回報的「十幾秒」尚未消除**——
它在後端，而後端不在本包授權內。依規約，帶明確殘留的 BR 留在頂層不進 `closed/`。~~

**上段刻意劃掉不刪**：它記錄了本 BR 曾經為何是 PARTIAL。後端那個殘留已於
2026-08-21 重量（見上節），尖峰在 idle 下完全消失。兩個候選成因均已修復，
**以機制消除結案**，並留下復發時的分形狀重開條件。
