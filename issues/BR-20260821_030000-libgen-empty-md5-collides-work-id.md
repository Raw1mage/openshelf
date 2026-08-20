# BR-20260821_030000 — 空 md5 的公網項目全部共用同一個 work_id `libgen_`，且互撞在前端與後端都無聲

- **Status**: **PARTIAL** — 主修復已 landed 並經 dispatcher 獨立驗收（`3597e2e`），但四格殘留未消除，故不進 `closed/`
- **Fixed-by**: `3597e2e`（handler `ses_fdfe0c00fffea02pCkU7S4EE3x`，dispatcher 驗收 2026-08-21）

## 處置（使用者裁示）

**使用者原話：「下載不到的書就不要顯示搜尋結果」** ⇒ 採**修法 A（收緊放行條件）**，
並因此**關掉 BR-20260821_010000** 的路線（兩張方向相反，已一起裁決）。

```
:361  if not md5_val and not clean_title:   →   if not md5_val:      (li, 9 欄式)
:437  if not md5_val and not title:         →   if not md5_val:      (is, 10 欄式)
```

**`work_id` 未動**：收緊後 `md5_val` 保證非空，`f"libgen_{md5_val}"` 不再可能產出字面值，
互撞從源頭消失。DB 全表掃描確認**不需要 migration**（見下方）。

**丟棄留痕**（handler 主動加，dispatcher 採納）：兩個 parser 各加 `dropped_no_md5` 計數器
＋ `log.debug`。理由是判準①——parser 回 `[]` 可能是 (a) 這批 row 全無 md5、(b) parser 壞了、
(c) 搜尋本來就沒結果。**改動前 (a) 不存在，改動後 (a) 變成常態路徑**，於是靜默地併進
另外兩個。log 讓 (a) 可辨識，也讓零樣本的 is 適配器日後有數字可看。

## 殘留（PARTIAL 的理由，四格皆未消除）

1. **「使用者現在搜尋看不到那些書了」只有單元層證據，無線上實測**。且依現有母體
   （50/50 全含 md5）也**造不出**會被丟棄的真實項目——要驗證只能餵構造的 HTML，那就回到單元層。
2. **is 適配器的 fixture 是依 `:411`/`:428` 程式碼結構自行構造，非真實 HTML**。若真實 is 鏡像
   的第 10 欄以後結構與構造的不同，測試會通過但線上行為未知。
3. **`validator.py:115/138` 的連帶影響只有推論**。它用
   `len(records) > 0 and any(r.get("md5") for r in records)` 判定鏡像是否 verified。
   原本「全部 row 無 md5」的鏡像拿到 `records 非空但 any() 為 False` → step 4；
   現在拿到 `records 為空` → 同樣 step 4。**終點相同，但走的分支不同**——未實測。
4. **未查快取層**是否有序列化過的搜尋結果會繞過 parser 供給既有項目。

## 已驗收的部分（dispatcher 獨立重做，非採信 handler 自報）

- 全套件 `259 passed, 19 skipped` rc=0（基線 246+19）；新測試 13 passed；控制組 rc=4
- mutation 三格，各帶指紋、跑完還原，`sha256` 三次皆對回 `7823cd4e88b5e2ad…`
  - **M1** li 閘還原 ⇒ 殺 **5 條，全是 `test_li_*` + `[li]`**
  - **M2** is 閘還原 ⇒ 殺 **4 條，全是 `test_is_*` + `[is]`**
  - **M3** 兩處同時 ⇒ 殺 **9 條，恰為 M1∪M2**，無交集無遺漏
  - 每格皆驗「刻意沒變」：`workid=2 counter_init=2 counter_inc=2 log_li=1 log_is=1
    colguard9=1 colguard10=1`
  - **既有 246 條在三格 mutation 下一條都沒死** ⇒ 先前這個閘壞了不會有人知道
- **DB 掃描**（dispatcher 獨立重做，六表）：`work` / `identifier` / `manifestation` /
  `download_job` / `collection_item` 的 exact `'libgen_'` 與 prefix `'libgen_'` **全 0**；
  控制組 non-null = 42/84/42/0/5；樣本全是 `wk_` 前綴 ⇒ **不需要 migration**
- fixture 自我驗證（用 BeautifulSoup 數 `<td>`，斷言 li=9 / is=10）——沒有它，
  「被新閘過濾」與「在 `:331`/`:411` 欄數守衛就被丟掉」共用同一個輸出
- 範圍：`mirror_resolver.py` / `app.js` / `crawler_routes.py` / `docker-compose.yml` /
  `issues/` 的 diff 皆 0；控制組 `libgen_live.py` numstat = `26 2`

## handler 推翻 dispatcher 一格，已採納

派工單建議「is 適配器零樣本、風險不對稱，也許該更保守（只加 log 不丟棄）」。
handler 指出**風險方向被算反了**：現行 `and` 的 is 適配器，一旦使用者在設定頁把某個
is 鏡像驗證成 `verified`，**第一批結果就可能全部帶著 `work_id="libgen_"` 進 UI**
——那是現況風險，不是改動引入的。且「li 的書消失、is 的書留著但點了才失敗」這種
分歧本身就是使用者要消滅的病。log 計數已同時滿足「先觀察」，不需要讓錯誤項目進 UI
來換取觀察資料。

## 待使用者裁決的一格（handler 提出，dispatcher 同意後送）

**丟棄 vs 標記為不可下載**。使用者字面要的是「不要顯示」，但他真正想要的可能是
「不要點了才發現失敗」。標記路線**不能單獨成立**：留在結果裡的項目 `work_id` 仍是
`"libgen_"`，`app.js:474/697/759/1003` 四條互撞路徑一條都沒解，必須同時做修法 B
（複合 key）——那是另一張工作單的規模。**且目前觸發機率為 0，兩者對使用者的差別也是 0**。
- **Owner**: ses_fe7b5cbadffeSlxj0dv1Z740O4（openshelf 值星官）
- **Severity**: 中（無使用者受害實例，但一旦發生是靜默錯配而非報錯）
- **Filed**: 2026-08-21
- **Family**: 缺席態與失敗態共用同一個輸出（本 repo 第六次）
- **Related**:
  - `BR-20260821_010000-md5-gate-blocks-usable-mirror-links.md` — **同一格資料的另一面**。010000 說「md5 空但 mirror_links 可用時，`mirror_resolver.py:140` 不該早退」；本張說「md5 空的那些項目，在進入 resolver 之前就已經在 parser 產出端互相踩踏」。兩張引用**同一行** `libgen_live.py:361` 的放行條件，是同一條執行路徑上的前後兩段。
  - `BR-20260820_235500-public-results-always-tagged-born-digital.md` — **同一個 parser 迴圈、相鄰行**（`:364` 格式標籤失真 vs `:361/:370` work_id 互撞），且同為「parser 對缺欄位的處置寫死」的形狀。建檔時使用者裁示 235500 建檔不修；本張是否同處置待決。

---

## 現象

`app/crawler/libgen_live.py` 的兩個適配器都用 md5 組 `work_id`：

```
src:370   "work_id": f"libgen_{md5_val}",     ← _parse_libgen_li_html（9 欄式）
src:446   "work_id": f"libgen_{md5_val}",     ← _parse_libgen_is_html（10 欄式）
```

而**兩個適配器的放行條件都容許 `md5_val` 為空**：

```
src:361   if not md5_val and not clean_title:      ← li 適配器
              continue
src:437   if not md5_val and not title:            ← is 適配器
              continue
```

是 `and` 不是 `or`。**只要標題非空，md5 為空的 row 就會被放行**，於是 `work_id` 變成字面值 `"libgen_"` —— 所有這類項目共用同一個字串。

⚠ 上一輪我只標了 `:361/:370`（li 適配器）。**實測命中兩處**，is 適配器 `:437/:446` 是完全同型的第二份，修法必須同時涵蓋。

---

## 為什麼互撞在使用者眼前是無聲的

互撞的傷害不在「兩張卡片長一樣」，在於**下游多條路徑用 md5/work_id 當 key，而空值在每一條上都被安靜地跳過或錯配**：

| 位置 | 程式碼 | 空 md5 時的行為 |
|---|---|---|
| `app/static/js/app.js:474,478-480` | `localMd5s.has(md5)`，`if (md5 && ...)` | `md5` 為空 ⇒ 短路，**永遠不會被判為「已在本地」**，即使真的已收錄也重複顯示 |
| `app/static/js/app.js:697-698` | `if (!md5Key) continue;` | 空 md5 的卡片**不進差量比對基準**，輪詢時它的 DOM 永遠不會被更新 |
| `app/static/js/app.js:759,778` | `cachedJobsByMd5.get(md5Key)` | 用 `""` 當 key 去查佇列 ⇒ 所有空 md5 卡片**共享同一個查詢結果** |
| `app/static/js/app.js:859-861` | `item.md5 ? <checkbox> : <span>🌐</span>` | **批次收書 checkbox 不渲染**（這是唯一擋住的一格） |
| `app/static/js/app.js:864` | `id="btn-dl-${item.md5}"` + `triggerSingleDownload('${item.md5}')` | **單本下載按鈕照樣渲染**，且所有空 md5 卡片的按鈕 **DOM id 全部是 `btn-dl-`**（互撞） |
| `app/static/js/app.js:1003` | `currentResults.find(r => r.md5 === md5)` | 點下去時用 `""` 比對，**命中第一筆空 md5 的項目，不一定是使用者點的那一本** |
| `app/api/category_routes.py:118` | `cr.get("work_id", f"libgen_{cr_md5}")` | 拿到互撞的 `"libgen_"` 當識別碼往下傳 |

最後兩列是核心：**使用者點 A 書的下載按鈕，實際送出的是 B 書的資料**，而系統從頭到尾不會出聲。

`triggerSingleDownload` 送出後的下場已在 BR-010000 定案：`mirror_resolver.py:140` `if not md5: return None` ⇒ 六次重試全空 ⇒ `RuntimeError`，佇列標 failed。所以目前的實際結果是「點了一本錯的書，然後它失敗了」。

---

## 證據（含控制組）

```
=== A. work_id 由 md5 組成的行 ===
src:370: "work_id": f"libgen_{md5_val}",
src:446: "work_id": f"libgen_{md5_val}",
COUNT_A=2
CONTROL   單看 'work_id' 命中 = 2 行 [370, 446]      ← 與 A 一致，證明沒有第三處
CONTROL-NEG 'work_id_ZZZ_not_real' = 0               ← 有鑑別力

=== B. python 層 'libgen_' 全域掃描 ===
files_with_work_id = 11                              ← 控制組，該非零
libgen_ZZZ_not_real = 0                              ← 負控制組
消費端命中：app/api/category_routes.py:118

=== C. app.js 使用面 ===
'work_id' 命中 38 行 / '.md5' 命中 33 行             ← 控制組，該非零
'work_id_ZZZ' = 0                                    ← 負控制組，有鑑別力
```

⚠ **上一輪的證據作廢**：我當時用 pattern `work_id=f"libgen_` 查，命中 0 rc=1，**但控制組也是 0** —— 我漏了 JSON key 的引號（實際字面是 `"work_id": f"libgen_{md5_val}"`）。那組數字無鑑別力，本張的數字是重取的。

---

## 沒量什麼（同等重要）

1. **沒有真實 HTML 樣本證明這條路徑會被走到**。「第 8 欄有 href 但都不含 32-hex」在實務上發生的頻率是 0 還是常態，本張**沒有數字**。這格與 BR-010000 的最弱一環是**同一格**，正由探勘 subagent `ses_fdfff5772ffeQTDKtWtOKNUnBQ` 實測中。
   - 若實測結果是「所有活躍鏡像的第 8 欄 href 都含 32-hex」⇒ 本張與 010000 都應降優先序（邏輯缺陷為真，但無觸發路徑）。
   - 若實測結果是「libgen.li 的 `file.php?id=<id>` 形態常態不含 md5」⇒ 兩張都應升級，因為那代表**某個鏡像的所有結果都會互撞**。
2. **沒有實測點擊互撞**。「點 A 拿到 B」是讀 `app.js:1003` `find(r => r.md5 === md5)` 推論的，**沒有在瀏覽器上造出兩筆空 md5 項目點過**。
3. **沒查 `_parse_libgen_is_html` 的第 9 欄以後結構**（`src:428` `for a in cols[9:]`），只確認它與 li 適配器同型。
4. **沒評估修法對既有本地資料的影響**。若 DB 裡已存在 `work_id="libgen_"` 的列，改 key 生成規則可能需要 migration —— 未查 DB。

---

## 修法選項（未裁決）

**A. 收緊放行條件**：`if not md5_val: continue`（把 `and not clean_title` 改掉）
- 最小改動，一行 × 2 處。
- 代價：與 BR-010000 的方向**相反** —— 010000 主張「md5 空但 mirror_links 可用時應該還是能下載」。若採 A，就等於承認「沒有 md5 的項目一律不呈現」，把 010000 那條路徑永久關掉。
- **兩張 BR 必須一起裁決**，不可各自修。

**B. 改用穩定的複合 key**：`work_id = f"libgen_{md5_val or hash(title+authors+mirror_links[0])}"`
- 保留 010000 的路徑（項目仍呈現、仍可嘗試從 mirror_links 解析）。
- 代價：`work_id` 不再能反推 md5，需查所有假設「`libgen_` 之後就是 md5」的地方（至少 `category_routes.py:118`、`app.js:624/625/653/699`）。

**C. 前端一併擋掉單本下載按鈕**：`app.js:864` 比照 `:859` 的 checkbox 加 `item.md5 ?` 守衛
- 只治「點錯書」這個症狀，不治 work_id 互撞。
- 但它是**唯一能立即消除使用者可感知傷害**的一格，且與 A/B 不衝突。

---

## 復發防護（若決定修）

修法落地後至少要有一條測試鎖住：**餵一份含兩筆「有標題、第 8 欄 href 不含 32-hex」的 HTML 給 `_parse_libgen_li_html`，斷言兩筆的 `work_id` 不相等**（或斷言它們根本不被放行，視採 A 或 B）。

該測試必須自帶控制組：另餵一筆正常含 md5 的 row，斷言它照常產出 `libgen_<md5>` —— 否則「work_id 不重複」在「parser 整個壞掉回空 list」的實作下也會過。
