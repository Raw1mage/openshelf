# BR-20260820_130000 — 查詢字串含標點或含未索引詞就回 0 筆

Status: OPEN
Owner: ses_fe7b5cbadffeSlxj0dv1Z740O4（值星官）
Family: search-query-semantics
Filed: 2026-08-20 by ses_fe7b5cbadffeSlxj0dv1Z740O4
Reported-by: 使用者（「把條件縮減到一個單字就能找到書了，長條件式會找不到」）

**Related**:
- `BR-20260820_111523-mirror-resolver-dead-mirrors` — **同一種失效類別「缺席態與失敗態共用同一個輸出」**。
  該案在 crawler 層；本案在查詢層：「語法錯誤導致查詢失敗」與「真的沒有這本書」
  都回 `items: []`，使用者與程式都分不出來。
- `BR-20260820_124500-quick-collections-modal-blocking` — 同屬本輪使用者回報的前端可用性事故，
  但**不同根因**（該案是阻塞式 I/O，本案是查詢語意），非同族。

## 一句話

本地檢索（`/api/search`）在查詢字串含 **`.` 或 `,`** 時回 0 筆，
且**兩個端點**在查詢含「未被索引的詞」（如出版社名）時都回 0 筆。
兩者都靜默——回的是 `items: []`，與「查無此書」完全相同。

## 實測（2026-08-20，容器內，逐條同批執行）

| # | 查詢字串 | local | remote |
|---|---|---|---|
| 1 | `Abraham Silberschatz, Peter Baer Galvin, Greg Gagne, Operating System Concept,Wiley.` | **0** | **0** |
| 2 | `Operating System Concept,Wiley.` | **0** | **0** |
| 3 | `Operating` | **8** | 32 |
| 4 | `Operating System Concept` | **7** | 21 |
| 5 | `Operating System Concepts` | **7** | 26 |
| 6 | `Silberschatz` | **7** | 25 |
| 7 | `Operating System Concept.` ← **只多一個句點** | **0** | **21** |
| 8 | `Operating System Concept,Wiley` ← 逗號 | **0** | **21** |
| 9 | `Operating System Concept Wiley` ← **無標點**，只多一個詞 | **0** | **0** |

控制組即第 3–6 列：同一支查詢管道在不含標點時回 7–8 筆，
證明**端點本身是通的**，上述的 0 不是服務故障。

## 兩個獨立缺陷（我最初的判讀只抓到第一個）

### D1 — 標點殺死本地檢索（local-only）

**決定性證據是第 4 列 vs 第 7 列**：查詢字串唯一的差異是結尾多一個 `.`，
本地從 7 筆變 0 筆，**而公網同一條件仍回 21 筆**。

`.` `,` 在 SQLite FTS5 的 MATCH 語法中不是普通字元。未經 escape 直接餵進
`MATCH ?` 會被當語法符號解析，導致查詢失效或拋例外。
**公網不受影響**代表這是本地 FTS5 專有問題，不是共用的前處理層。

推定位置：本地檢索的 FTS5 查詢組裝處（`app/db/` 內，具體位置待 RCA 確認——
本 BR 未讀該層原始碼，見「沒驗證的」）。

### D2 — 未索引詞導致 AND 語意歸零（local + remote 皆中）

**第 9 列不含任何標點**（`Operating System Concept Wiley`），
卻讓 local 與 remote **雙雙歸零**。與第 4 列相比只多了 `Wiley` 一個詞。

`Wiley` 是出版社。若查詢對所有詞取 AND 且出版社不在被索引的欄位裡，
則任何含出版社名的查詢都必然 0 筆——**而使用者複製書目資訊時幾乎一定會帶出版社**。

這解釋了為什麼使用者的第 1、2 條（真實書目格式）完全失效：
它們同時踩到 D1 與 D2。

> ⚠ **本 BR 最初的判讀錯誤，特此記錄**：我先前向使用者回報「不是條件太長，是條件裡有標點」。
> **那只對一半。** 第 9 列無標點仍歸零，證明 D2 獨立存在。
> 記在這裡是因為下一個接手者若只修 D1，使用者貼真實書目時**仍然會查不到**，
> 而測試（用不含出版社的字串）會全綠。

## 使用者感受得到的傷害

從書目資訊複製貼上是最自然的檢索方式（作者、書名、出版社、標點齊全），
而那**恰好是必然回 0 筆的輸入形狀**。使用者必須自行摸索出
「刪掉標點、刪掉出版社、只留書名」才能用，且畫面不會給任何提示。

## 修復方向（未實作）

1. **D1**：FTS5 查詢前對使用者輸入做 escape。
   最穩妥是把每個詞包成 `"..."` 片語（FTS5 內雙引號 escape 為 `""`），
   而非嘗試剝除標點——剝除會誤傷 `C++`、`.NET`、`R&D` 這類本身含符號的詞。
2. **D2**：決定 AND / OR 語意。
   建議：全詞 AND 回 0 筆時**自動降級**重試（去掉最後一個詞、或改 OR 並依命中數排序），
   並在 UI 明示「已放寬條件」。**不要靜默降級**——那會變成另一個「兩態共用一個輸出」。
3. **兩者共通**：查詢失敗（語法錯誤 / 例外）與查無結果**必須回不同的東西**。
   目前都是 `items: []`。至少讓 API 回一個 `query_status` 欄位。

## 驗收判準

- [ ] 上表第 1、2 列（使用者的真實輸入）能回非 0 筆
- [ ] **負控制組**：一個確實不存在的書名仍回 0 筆（證明不是把所有查詢都放寬成命中）
- [ ] 第 7 列（`Operating System Concept.`）與第 4 列回**相同筆數**
- [ ] 含 `C++` / `.NET` 的查詢不因 escape 實作而損壞
- [ ] 查詢語法失敗時，API 回傳可與「查無結果」區分的訊號

## 沒驗證的

- **未讀任何原始碼**。全部結論來自黑箱端點量測。
  D1 歸因於「FTS5 未 escape」是**推論**，未在 `app/db/` 確認實際查詢組裝方式。
- **未確認 remote 端在第 9 列歸零的原因**是否與 local 同一個機制
  （remote 走的是 libgen 站點查詢，可能是上游行為而非本地缺陷）。
- 未測其他標點（`:` `;` `-` `'` `&` `/`）的行為。
- 未測中文查詢字串（全形標點可能是另一組行為）。
- 未測 `page_size` / 分頁與本問題的交互作用。

## 排程備註

**與 `app/static/js/app.js` 的兩張前端 BR 無檔案交集**（本案在 `app/db/` 與 API 層），
可與前端工作包並行派工。但**不得與 `handler ses_fe29bb665ffeDEhHsHdW0rFuSi` 並行**——
該 handler 正在寫 `app/db/dao.py`。需等它交件並 commit 後再派。

---

# 根因已定位（2026-08-20 追加，值星官 ses_fe7b5cbadffeSlxj0dv1Z740O4）

## 缺陷 A — 整串查詢被包成單一 FTS5 phrase

`app/db/search.py:34`：

```python
escaped_q = f'"{cleaned_query}"'      # ← 整串使用者輸入外面套一對引號
params.append(escaped_q)
```

`app/db/search.py:96` 的 snippet 子查詢用同一個表達式，所以同時中招。

FTS5 的 `"..."` 是 **phrase 運算子**：它要求引號內的 token 序列**連續且順序一致**地出現。
於是使用者輸入的每一個字元（含標點、含詞序）都變成必須逐字命中的條件。

**決定性控制組**（記憶體內 FTS5 表，`tokenize='trigram'`，單列
`'Operating System Concepts with Java 8th Edition'`，排除線上資料變因）：

| MATCH 表達式 | 命中 |
|---|---|
| `"Operating System Concept"` | **1** |
| `"Operating System Concept."` | **0** ← 只多一個句點 |
| `"Concept Operating"` | **0** ← **只顛倒詞序，無任何標點** |
| `Operating System Concept`（不包引號） | **1** |
| `"Operating" "Concept"` | **1** ← 拆成多個 phrase |
| `Operating AND Concept` | **1** |
| `"Operating System Concept,Wiley"` | **0** |
| CONTROL `SELECT count(*)` | **1**（表真的有資料） |
| CONTROL `"Operating"` | **1**（MATCH 管道是通的） |

**`"Concept Operating"` → 0 是判準關鍵**：它沒有任何標點，只是詞序不同。
所以 BR 原本寫的「標點導致失敗」只是症狀的一半——**根因是 phrase 語意，標點只是最容易觸發它的形式**。

## ~~缺陷 B — `publisher` 不在 FTS 索引內~~ —— **本段是錯的，已撤回**

> **勘誤（2026-08-20，handler `ses_fe2618ae6ffesJJq3LCVuuryGp` 推翻，值星官確認）**
>
> 下方原文說「`work` 表**有** `publisher` 欄（schema.sql 內 grep rc=0）」——**兩個子句都不成立**。
>
> ```
> grep -n "publisher" app/db/schema.sql        → rc=1，零筆
> CONTROL grep -c "title" app/db/schema.sql     → 4        （證明 grep 讀得到檔）
> 線上 PRAGMA table_info(work)  12 欄：has publisher = False，CONTROL has title = True
> 線上 work_fts 欄位：[work_id, title, authors_display, content]
> ```
>
> **根本沒有 `publisher` 這個欄位。** 不是「有欄位但沒進索引」，是從來就不存在。
>
> **我這個 `rc=0` 是怎麼來的**（自我覆核）：我當時寫的是 `grep -n "publisher" app/db/schema.sql | head -5`，
> 那個 `$?` 是 **`head` 的退出碼**，不是 `grep` 的。`head` 永遠回 0。
> 這正是我貼進每一張派工單的判準①裡點名的那個陷阱（`cmd | tail` 取到 tail 的 `$?`）。
> **我自己犯了，而且犯在 BR 裡——這會讓下一個人為一個不存在的欄位設計遷移。**
>
> **`Wiley` 實際是搜得到的**：線上 FTS 命中 **6 筆**，經由已被索引的 `content` 欄
> （PDF 抽取全文含版權頁，snippet 佐證：`"... ABRAHAM SILBERSCHATZ ... John Wiley & Sons"`）。
>
> **這直接解掉了我原本担心的取捨**：我担心「選 AND 的話 `Wiley` 永遠不在索引裡，
> `Operating System Concept Wiley` 仍歸零」——**前提不成立，那個代價不存在**。
> 實打 `Operating System Concept,Wiley` 修後 **6 筆**。
>
> 以下原文保留作為錯誤紀錄，**不得依據它開工**。

<details>
<summary>原文（已證實為錯）</summary>

`app/db/schema.sql:107-113`：

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS work_fts USING fts5(
    work_id UNINDEXED,
    title,
    authors_display,
    content,
    tokenize='trigram'
);
```

`work` 表**有** `publisher` 欄（schema.sql 內 grep rc=0），但它**沒有進 FTS 索引**。
所以使用者查 `Wiley` 這類出版社名時，本地檢索在定義上**不可能命中**——
與「這本書不存在」回同一個 `items: []`。

這解釋了為何 `Operating System Concept,Wiley` 就算修好缺陷 A 也仍可能是 0：
兩個獨立缺陷疊在同一個查詢上。

</details>

## 實打對照（容器 API，修復前）

```
Operating System Concept        local=7
Operating System Concept.       local=0
Operating System Concept,Wiley  local=0
Concept Operating               local=0     ← 無標點，僅詞序
```

## 修復方向（不是指定實作，是劃出必須成立的性質）

1. **不得**把使用者輸入整串包成 phrase。需要一個查詢建構層，把輸入轉成 FTS5 安全的表達式。
2. **必須處理 FTS5 語法字元**（`"` `*` `(` `)` `:` `^` `-` `AND` `OR` `NOT` `NEAR`）——
   使用者打 `C++` 或 `"quoted"` 不得讓查詢炸掉或靜默回 0。
3. **標點應被視為分隔符而非查詢條件**：`Operating System Concept,Wiley.` 應等價於
   `Operating System Concept Wiley` 這組詞。
4. **多詞語意需明確擇一並寫進註解**：AND（全中）還是 OR（任一）？
   若選 AND，`Wiley` 未索引會讓整串歸零——這與缺陷 B 交互，必須一起想。
5. **缺陷 B 的處置需要裁示**：把 `publisher` 加進 `work_fts` 要重建虛擬表 +
   回填既有資料（動 `app/db/schema.sql` 與遷移）。**這格範圍較大，不預設要做。**

## 驗收判準

1. 上方控制組表的**每一列**都要有對應測試，含 `"Concept Operating"` 這種純詞序案例。
2. **負向必測**：真的不存在的書（如 `zzzzz_no_such_book_qqq`）仍須回 0。
   缺這格就無法證明修復不是「把所有查詢都變成命中」。
3. **語法字元不得使查詢失敗**：`C++`、`"`、`(`、`*`、`AND` 單獨輸入皆須回應而非拋例外。
4. 端到端實打容器 API，給出修前修後對照表（至少涵蓋上方四條實打字串）。
5. 既有 7 筆命中的查詢（`Operating System Concept`）修復後**不得減少**。

## 已知驗證陷阱

`data/db/openshelf.sqlite`（repo 內，root:root，`work` 表 0 rows）**不是線上那顆**。
線上是 NAS 掛載 `/nas/openshelf/db`（35 筆）。端到端一律打容器 API。
