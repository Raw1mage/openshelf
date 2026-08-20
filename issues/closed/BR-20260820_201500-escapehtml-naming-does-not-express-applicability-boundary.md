# BR-20260820_201500 — `escapeHtml()` 命名未表達適用邊界，誘導未來誤用（預防性，非現存缺陷）

Status: OPEN
Owner: ses_fe7b5cbadffeSlxj0dv1Z740O4（值星官）
Family: naming-affordance
Filed: 2026-08-20 by ses_fe7b5cbadffeSlxj0dv1Z740O4
Found-during: 修復 `76efe94` / `af13682`（書單選單撇號截斷）後的回顧

> ## ⚠ 先講一件事：本 BR 是**預防性**的，不是修現存缺陷
>
> 值星官原本要在此處寫「全檔有 10 處把 `escapeHtml` 誤用在 inline onclick」。
> **當場實測推翻了那個記憶——實際誤用是 0 處。**
>
> ```
> onclick 行 + escapeHtml          0 處
> 單引號屬性 + escapeHtml           0 處    ← 真正會壞的形狀，不存在
> JS 字面值 '${escapeHtml(...)}'    0 處
>
> CONTROL onclick 行 + escapeJsArg  11 處   ← 證明 grep 有鑑別力
> CONTROL 不存在的函式名             0 處 rc=1
> ```
>
> `76efe94` 與 `af13682` 已把所有實際誤用點改成 `escapeJsArg`。
> **本 BR 處理的是「名字沒有阻止下一次誤用」，不是「現在有東西壞了」。**
> 接手的 handler 請勿以為有 bug 可修——**若你去找誤用點，你會找不到，那是正確結果。**

**Related**:
- `76efe94`（closed）— 書單選單完全打不開。根因：`escapeHtml` 不轉單引號，
  `Silberschatz's` 截斷 inline onclick 字串，觸發 `SyntaxError`。
- `af13682`（closed）— 同根因殘留三處（`:1236` / `:1653` / `:2903`）。
- **兩案同一個根因，分兩次才修完** —— 這正是本 BR 要處理的：
  第一次修完之後，名字仍然沒有阻止剩下三處被漏掉。

## 一句話

`escapeHtml()`（`app/static/js/app.js:1395`）只轉 `& < > "`，**不轉 `'`**。
它僅適用於 **HTML 文字節點** 與 **雙引號屬性值**，
但名字讀起來像「把任何東西變成安全的 HTML」，**沒有任何地方表達這個邊界**。

同檔的 `escapeJsArg()`（`:1404`）才是 inline `onclick` 內 JS 字串參數的正解，
且它的註解把邊界寫得很清楚——**問題全在 `escapeHtml` 這一側**。

## 證據

### 兩個函式的實際能力

```
app/static/js/app.js:1395  function escapeHtml(str)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")
                                                                   ↑ 有 "，沒有 '

app/static/js/app.js:1404  function escapeJsArg(str)
  先做 JS 字面值跳脫（\\ ' \r \n），再做 HTML 屬性跳脫（& " < >）
  ← 順序不可顛倒，該函式上方註解已寫明
```

### 當前使用分布（實測，非記憶）

```
escapeHtml(  呼叫點總數        54
  其中 雙引號屬性內             7   ← 安全（escapeHtml 有轉 "）
  其中 單引號屬性內             0   ← 會壞的形狀，目前不存在
  其中 JS 字面值內              0   ← 已由 76efe94 / af13682 清乾淨
  其餘 ~47                          ← HTML 文字節點，正確用法

escapeJsArg( 呼叫點總數        12
  其中 onclick 行內            11   ← 正確用法

CONTROL escapeNothingXYZ(      0 rc=1   ← 證明 grep 有鑑別力
```

**那 7 處雙引號屬性目前安全，但它們是最脆的一格**：
任何人把 `title="${escapeHtml(x)}"` 改成 `title='${escapeHtml(x)}'`
（純風格改動，看起來完全無害）就會立刻重現 `76efe94` 的缺陷。
行號：`:1199` `:1210`（兩處同行）`:1656` `:1658` `:1678` `:2766` `:2904`。

### 為什麼名字是根因而非巧合

同一個根因**分兩次才修完**（`76efe94` 修主體，`af13682` 補三處）。
第一次修復後，開發者已經知道這個陷阱了，**仍然漏掉三處**。
名字沒有在任何一個決策點提供阻力——這是 affordance 缺陷，不是粗心。

## 使用者裁示

使用者於 2026-08-20 明確選擇「**現在做**」，並接受
「純機械改名 + 補使用說明註解，風險低但 diff 大」的取捨。

## 修復方向

1. `escapeHtml` → **`escapeHtmlText`**，全檔 54 處呼叫點同步更名。
2. 在新名字的定義上方補註解，明確寫出**三件事**：
   - 適用：HTML 文字節點、雙引號屬性值
   - **不適用**：單引號屬性值、inline `onclick` 內的 JS 字串參數
   - 後者請用 `escapeJsArg`（附交叉引用）
3. **不要改 `escapeJsArg`**——它的名字與註解都已正確。
4. **不要改任何 `escapeHtml` 的行為**。這是純改名，
   一旦改了行為，就從「零風險機械改動」變成「需要完整回歸驗證的行為變更」。

## 驗收判準

1. **改名必須完整，且要能證明完整**：
   - 改完後 `grep -c 'escapeHtml(' app/static/js/app.js` 應為 **0**
   - `grep -c 'escapeHtmlText(' app/static/js/app.js` 應為 **55**（54 呼叫 + 1 定義）
   - **兩個數字都要報，且要附一個必然非空的控制組**證明 grep 有鑑別力。
     只報「改完了」不算。
   - ⚠ 注意 `escapeHtml(` 是 `escapeHtmlText(` 的**前綴**——
     用 `grep -c 'escapeHtml('` 會**同時命中新名字**。
     需用能區分兩者的 pattern（例如 `escapeHtml(` 後不接 `Text`），
     **並證明該 pattern 真的能區分**（拿一行新名字餵給它，應該不命中）。
     這一格是本包最容易靜默失敗的地方。
2. **行為不得改變**：`escapeHtmlText` 的 replace 鏈需與原 `escapeHtml` **逐字相同**。
   附 `git diff` 證明該函式體零改動（只有名字那一行變）。
3. **端到端驗證，且要涵蓋那個會壞的字串**：
   - 容器 API 打得到的頁面上，確認含撇號的書名（如 `Silberschatz's ...`）
     在**書單選單**與**檢索結果**兩處都能正常顯示且選單可開啟。
   - **必須實際在瀏覽器層驗證，不能只看 JS 檔改對了。**
     `76efe94` 的症狀是「選單完全打不開」——那是 runtime `SyntaxError`，
     靜態檢查看不出來。
4. `pytest` 不得下降（當前基線 **150 passed**，`.venv/bin/python -m pytest`）。
   `rc` 需獨立一行取，**不得接管線**。

## Boundaries（授權邊界）

**可碰**：
```
app/static/js/app.js       ← 唯一的實作檔
```

**禁區**：
```
app/api/                ← 另一顆 handler 正在改 crawler_routes.py
app/db/                 ← 同上（dao.py），且 BR-160000 待決
app/models/  app/pipeline/  app/crawler/
app/static/css/  app/static/index.html   ← 除非改名真的需要動，需先回報
tests/
issues/  plans/  docs/
requirements.txt  Dockerfile  docker-compose.yml  extension/
```

**特別注意**：`app/api/crawler_routes.py` 與 `app/db/dao.py` 由另一顆 handler
（`crawler_routes` 事件迴圈阻塞包）持有，**檔案集已證明不交集**，請勿越界。

## 環境事實（會咬人的）

- `docker compose` 的 service 名是 **`openshelf`**，不是 `app`。
  叫錯回 rc=1，**與「grep 沒找到」共用同一個退出碼**。
- **bind-mount + `--reload`**：`./app:/app/app`，你每次存檔就是線上程式碼。
  **本包尤其危險**：改名若分多次存檔，中間會有「定義已改、呼叫點未改」的瞬態，
  線上前端會直接 `ReferenceError` 而整頁失效。
  **改名必須一次寫入落地**（單次 `edit` 用 `replaceAll`，不要拆成 54 次）。
- 前端是**原生 JS 無建置步驟**，瀏覽器直接載入 `app.js`。
  沒有編譯器會在改名不完整時報錯——**只有 runtime 會**，而且是在使用者面前。
- pytest 必須用 **`.venv/bin/python -m pytest`**；系統 `python3` 缺 `fitz`。
- `.specbase/events.sqlite` 恆為 M，是背景程序寫的，**永不納入你的範圍證據**。

## 判準（整段適用，違反即退回）

**缺席態與失敗態不得共用同一個輸出。**

每個 grep / 測試 / 量測都要帶**控制組**證明工具有鑑別力——
回空同時是「沒有」與「pattern 打錯」的答案。

**嚴禁用 `|` 管線取 `$?`**（會取到管線末端指令的退出碼）。
值星官本人踩過：`grep -n "publisher" schema.sql | head -5` 的 `rc=0`
是 `head` 的退出碼，導致在 BR 裡寫下一個不存在的欄位。
多條件驗證一律**分行獨立執行**。

**推翻我是本包的合法產出。** 上面的行號、計數、修復方向若有反證，
附證據推翻它——那比照做更有價值。
**本 BR 開頭那格就是值星官自己被實測推翻的紀錄。**

## 沒驗證的

- **未檢查 `app/static/` 下其他檔案是否也定義或使用 `escapeHtml`**
  （只掃了 `app.js`）。若有，改名需一併處理，**但那會擴大範圍，先回報**。
- **未檢查 `extension/` 目錄**（瀏覽器擴充功能）是否有同名函式或共用程式碼。
- **未驗證那 7 處雙引號屬性的資料來源是否可能含 `"`**——
  若含，`escapeHtml` 有轉 `"`，安全；此處只是未逐一確認資料形狀。
- **未評估是否該讓 `escapeHtmlText` 在偵測到單引號情境時主動出聲**
  （例如開發模式下 `console.warn`）。那是更強的防護但屬行為變更，
  超出「純機械改名」的範圍，**不在本包**。
