# BR-20260821_030000 — 空 md5 的公網項目全部共用同一個 work_id `libgen_`，且互撞在前端與後端都無聲

- **Status**: **PARTIAL** — 主修復已 landed 並經 dispatcher 獨立驗收（`3597e2e`）；四格殘留已於 2026-08-21 銷三格，但**兩格仍開著**（is 適配器無真實樣本、丟棄留痕在生產路徑不可觀察），故不進 `closed/`
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

## 殘留狀態（四格 → 2026-08-21 銷三格，**兩格仍開著**）

> 以下每一格的證據 dispatcher 都獨立重做過，非採信 handler 自報。

**① 無線上實測 — 已消除（且推翻了 dispatcher 自己的預判）**

dispatcher 在派工單寫「現在造不出會被丟棄的真實項目，這格很可能做不到」。**做得到。**
handler 沒用 `page.route` 騙前端，而是把**上游鏡像**換成受控假鏡像：
docker 私網 + 假鏡像容器（4 row：2 有 md5 / 2 無），真鏡像全部 `enabled=False`
（`ACTIVE_COUNT=1` 確保絕不外連）。**parser / route / DB / 瀏覽器全是真的，只有 HTML 是構造的。**

fixture 自我驗證：BeautifulSoup 數出 `DATA_ROWS=4 cols=9`，2 筆 href 含 32-hex /
2 筆 `file.php?id=`——沒有它，「被新閘丟棄」與「在欄數守衛就被丟掉」共用同一個輸出。

```
                    HTTP  total  KEEP  DROP  work_id相異  字面 "libgen_" 筆數
FIXED   (新閘)      200    2      2     0     2 of 2       0
MUTATED (舊寬鬆閘)  200    4      2     2     3 of 4       2   ← 互撞重現
```

瀏覽器層（真 chromium 打真頁面，非注入）：
FIXED 頁面 `btn-dl-` id 相異 2/2、互撞 0 個；MUTATED 相異 3/4、**互撞 2 個（兩個裸 `btn-dl-`）**。
本 BR 症狀表列的 `app.js:864` DOM id 互撞在瀏覽器裡被直接看見，修復後歸零。
mutation 跑在 `app/` 的 scratch 副本上（`tokenize` 剥掉 COMMENT/STRING 後計數），**repo 內 `app/` 零改動**。

⚠ 這格證明的是「**parser 遇到無 md5 row 時全鏈會怎樣**」，**不是**「真實鏡像現在會不會產出這種 row」。
後者依實測母體（50/50 全含 md5）觸發機率仍為 0。

**② is 適配器無真實樣本 — 仍開著（已證明目前做不到，擋在網路不在程式）**

```
libgen.is  HTTP=000  CURL_RC=28   DNS rc=0 -> 193.218.118.42
libgen.rs  HTTP=000  CURL_RC=28   DNS rc=0 -> 193.218.118.42
libgen.st  HTTP=000  CURL_RC=28   DNS rc=0 -> 193.218.118.42
CONTROL libgen.li  HTTP=200  CURL_RC=0   DNS rc=0 -> 179.43.167.164
```

三者 DNS **都解得到**且指向同一 IP ⇒ **是網路層不可達，不是 DNS NXDOMAIN**（兩種成因不得混為一談）。
控制組 libgen.li 通 ⇒ 網路本身正常，探針有鑑別力。

順帶確認一件事：dispatcher 先前說「`dao.py:941` 的 `verified` 過濾是可翻轉的狀態，
不是結構性死路」——**成立**，但翻轉它也拿不到樣本，因為主機根本連不上。
⇒ `_parse_libgen_is_html` 的 fixture **仍然是依程式碼結構自行構造的**。這格**未被消除**，
只是從「未做」換成「**已證明目前無法消除**」。哪天鏡像回來了要重新驗。

**③ `validator.py:115/138` 連帶影響 — 已消除（分支確實不同，終點確實相同）**

架第二台假鏡像（3 row 全無 md5，fixture 自我驗證 `ROWS_WITH_MD5=0`），兩容器各打 `/validate`：

```
                    validation_status      adapter   sample_records_count
FIXED   (新閘)      incompatible_layout    unknown   0
MUTATED (舊寬鬆閘)  incompatible_layout    unknown   0     ← 終點相同 ✓
CONTROL 有 md5 鏡像   verified/libgen_li               2 筆 / 4 筆  ← 有鑑別力
```

分支不同的直接證據（容器內直接呼叫 parser，非推論）：
```
FIXED  : len_records=0  any_md5=False  gate=False  → 「len()>0 為 False 短路」
MUTATED: len_records=3  any_md5=False  gate=False  → 「len()>0 為 True，由 any() 決定」
```
⇒ handler 的推論**正確**（終點同為 step 4 → `incompatible_layout`），且兩條短路分支都真的被走過一次。

**④ 未查快取層 — 已消除（沒有快取層）**

```
libgen_live.py + crawler_routes.py 內 'cache'          rc=1（0 行）
app/ 全域 'lru_cache|TTLCache|_cache'                 rc=1（0 檔）
CONTROL  app/ 全域 'def '                              rc=0（20 檔）
```
控制組有鑑別力 ⇒ 那兩個 0 不是 pattern 寫錯。**沒有快取層，parser 是搜尋結果的唯一來源。**

## 新發現的揭露缺陷（handler 依判準②回報，dispatcher 獨立坐實）

**上方「丟棄留痕」那格的 `log.debug` 在生產路徑上不可觀察——它的效果目前是 0。**

```
容器內實測（docker exec）：
  app.crawler.libgen_live  EFFECTIVE_LEVEL = 30 WARNING
  ROOT_LEVEL = WARNING
  DEBUG_ENABLED = False   INFO_ENABLED = False   WARNING_ENABLED = True

成因：
  grep -rn 'basicConfig|dictConfig|setLevel' app/    rc=1（0 行）
  CONTROL grep -rn 'getLogger' app/                  rc=0（12 行）  ← 有鑑別力
  ⇒ app 全域沒有任何 logging 設定，root logger 停在預設 WARNING。
  uvicorn 的 --log-level 只設它自己的 logger，不動 root（handler 實測過，加了仍為 0）。
```

**這是本 BR 自己的修復裡長出的同一個病。** 上方「丟棄留痕」節寫的理由是：
否則 (a) 這批 row 全無 md5、(b) parser 壞了、(c) 搜尋本來沒結果 共用同一個輸出。
而那個留痕在生產環境**發不出來**，三態仍然共用同一個輸出。

兩條修法選項（**未裁決**，都要動 production 檔）：
- **(a)** `log.debug` → `log.info`：改動最小，但 root 仍在 WARNING ⇒ **仍然發不出來**。
  除非同時降 root level，否則這個選項是無效的——這一格必須實測，不得推論。
- **(b)** `app/main.py` 加一次 `logging.basicConfig(level=...)`：真的會發出來，
  但**影響全 app 的 log 量**（目前 12 處 `getLogger`），是全局性變更。

這也是 BR-030000 **不能進 `closed/`** 的第二個理由。

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
