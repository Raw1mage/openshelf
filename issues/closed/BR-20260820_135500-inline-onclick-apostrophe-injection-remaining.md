# BR-20260820_135500 — inline onclick 的撇號截斷仍存在於三處（同款阻斷級缺陷）

Status: CLOSED
Closed: 2026-08-20 by ses_fe7b5cbadffeSlxj0dv1Z740O4 (fix af13682)
Owner: ses_fe7b5cbadffeSlxj0dv1Z740O4（值星官）
Family: frontend-blocking-io
Filed: 2026-08-20 by ses_fe7b5cbadffeSlxj0dv1Z740O4
Found-by: handler ses_fe2894298ffeUsqILfc6fgFmlj（修 BR-124500 時順手標出，範圍外未修）

**Related**:
- `BR-20260820_124500-quick-collections-modal-blocking` — **同一個缺陷、同一個檔、同一種修法**。
  該案已修 `openQuickCollection` 等 7 個呼叫點（commit `76efe94`，新增 `escapeJsArg()`），
  本案是**同一批呼叫點裡沒被涵蓋的剩餘三處**，因為它們不在書單路徑上。
  該 BR 的 §5「範圍外、已具備修法」就是本 BR 的來源。

## 一句話

`app/static/js/app.js` 仍有三處把使用者資料直接塞進 inline `onclick` 的 JS 字串字面值，
只用 `escapeHtml()` 跳脫——**它不轉單引號**。書名含撇號時（`Silberschatz's ...`、
`Beginner's ...`）會產生 `SyntaxError: missing ) after argument list`，
**按鈕點了完全沒反應，且沒有任何錯誤訊息**。

## 證據（值星官獨立 grep，2026-08-20 13:45 工作樹）

```
app.js:1236  onclick="saveSingleBookToLocalDisk('${j.work_id}', '${escapeHtml(j.title)}', '${j.format || 'pdf'}')"
app.js:1653  onclick="showBrDetailModal('${m.br_id}', '${escapeHtml(m.last_error || '')}')"
app.js:2903  onclick="handleCategoryClick('${node.category_id}', '${escapeHtml(node.name)}', '${node.icon}', '${escapeHtml(currentPath)}')"
```

`escapeHtml()`（app.js 內）只轉四個字元：

```js
str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;")
```

**單引號不在其中。**

決定性重放（node，模擬 HTML 屬性解碼後餵給 JS parser，值星官親跑）：

```
標題 "Silberschatz's Operating System Concepts"（ASCII U+0027）
  escapeHtml  → PARSE_FAIL: missing ) after argument list
  escapeJsArg → PARSE_OK，roundtrip 值逐字元相等（ROUNDTRIP_EXACT: true）
```

## 使用者感受得到的傷害

- **`:1236` 最嚴重**：那是下載佇列裡的「📥 下載保存至本機硬碟」按鈕。
  書名帶撇號時**整顆按鈕失效**，使用者無法把已下載的書存到本機，且畫面上毫無異狀。
- `:2903` 是分類樹節點點擊。分類名或路徑含撇號時整個節點點不動。
- `:1653` 是 BR 詳情彈窗，`m.last_error` 是**錯誤訊息字串**——
  錯誤訊息裡出現撇號的機率極高（`can't`、`doesn't`），
  等於「系統出錯時，看錯誤詳情的按鈕也一起壞掉」。

實測不是邊角：公網 25 筆有 2 筆帶撇號（`Beginner's` ×2），本地 2 筆有 1 筆。

## 另有四處已改為 `escapeJsArg` 但未實測

commit `76efe94` 已把下列四處改成 `escapeJsArg`，但 handler 回報**沒有實際觸發到它們**
（需要既有書單資料才點得到）。改法與已驗證的 `openQuickCollection` 完全相同，
但依判準「未量測就是未量測」，在此列出：

```
app.js:2239  removeChromeBookmark
app.js:2292  renameCollectionPrompt
app.js:2293  deleteCollectionPrompt
app.js:2316  removeBookFromCollection
```

## 修復方向

把三處的 `escapeHtml(...)` 換成 `escapeJsArg(...)`。函式已存在（`76efe94` 引入），
**不需要新寫**。注意 `escapeJsArg` 的內部順序不可顛倒——
先做 JS 字面值跳脫（`\` `'` `\r` `\n`），再做 HTML 屬性跳脫（`&` `"` `<` `>`），
因為 HTML 屬性會先被瀏覽器解碼再交給 JS parser。

**更根本的方向（建議一併評估，但不強制）**：inline `onclick` + 字串內插本身就是這個病的溫床。
改用 `data-*` 屬性 + `addEventListener` 事件委派可以讓這類缺陷在結構上不可能發生。
那是較大的重構，可另議。

## 驗收判準

- [ ] 三處全部改用 `escapeJsArg`；`grep -n "onclick=.*escapeHtml(" app/static/js/app.js` **rc=1**，
      且**控制組** `grep -c "escapeJsArg(" app/static/js/app.js` 非零（證明 grep 有鑑別力）
- [ ] `:1236` 實測：找一本標題帶撇號的已下載書，點「📥」按鈕**能觸發下載**，
      `pageerrors` 為空陣列（修前應為 `["missing ) after argument list"]`）
- [ ] **負控制組**：無撇號的書，同一顆按鈕修前修後都正常（證明沒改壞既有路徑）
- [ ] 四處已改但未實測的呼叫點，至少實測到 `removeBookFromCollection`（最容易造出資料）
- [ ] `node --check` rc=0，且**控制組**對一個故意寫壞的副本 rc=1

## 沒驗證的

- **未實測任何一處**。本 BR 全部證據為 grep + node 重放，未進瀏覽器點擊。
  handler B 在 BR-124500 已用 Playwright 驗證過**同款修法**在 `openQuickCollection` 上有效
  （`null → 26,088ms`、`pageerrors` 清空），故修法本身可信，但**這三處各自的觸發路徑未跑過**。
- 未盤查 `app.js` 以外的檔案是否有同款寫法（`extension/` 目錄未查）。
- 未評估 `data-*` + 事件委派重構的工作量。

## 排程備註

**與 `BR-20260820_131500`（下載鏈帶不動年份）同檔衝突**——後者的第一層也是 `app.js`。
兩者不可並行派工。本 BR 較小，可在 131500 交件後由同一顆 handler 續作。
