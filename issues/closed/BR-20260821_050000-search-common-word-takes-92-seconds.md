# BR-20260821_050000 — 搜尋常見英文字確定性耗時 92 秒，且會癱瘓全站

- **Status**: CLOSED — verified & effective
- **Closed**: 2026-08-21 by ses_fe7b5cbadffeSlxj0dv1Z740O4（dispatcher 獨立重跑六格判準）
- **Fix**: `a31e9258581bd5fe98141ab66cdc379def4d4347` — snippet 改 `instr`+`substr` 視窗
- **Result**: 92.143s → 0.095s（**970×**，dispatcher 獨立三次重跑 0.0949/0.0978/0.0976）
- **Severity**: CRITICAL — 使用者可感知、確定性、單指令即可觸發
- **Owner**: ses_fe7b5cbadffeSlxj0dv1Z740O4（值星官）
- **Family**: G-perf-search
- **Reported**: 2026-08-21 by ses_fe7b5cbadffeSlxj0dv1Z740O4（源自 handler ses_fdf8fc2c4 的量測交件，dispatcher 獨立重跑坐實）

---

## 一、症狀（使用者視角）

在搜尋框輸入一個**常見英文字**（例：`the`），按下搜尋，**畫面轉圈 92 秒**才出結果。

不需要負載、不需要爬蟲在跑、不需要多開分頁。**單一 curl、零併發即可重現。**

```bash
curl -s -o /dev/null -w 'http=%{http_code} t=%{time_total}\n' --max-time 240 \
  'http://127.0.0.1:8088/api/search?q=the&page_size=20'
# dispatcher 獨立實測：http=200 t=92.143385
# handler   獨立實測：http=200 t=95.153384
```

---

## 二、關鍵事實：慢的不是端點，是「命中筆數」

三個負控制組全部 4ms 級，證明 `/api/search` 本身沒問題：

| 查詢 | HTTP | 耗時 | 說明 |
|---|---|---|---|
| `/api/collections` | 200 | **0.004772** | 對照：其他端點正常 |
| `q=zzzznomatch` | 200 | **0.004391** | 零命中 → 快 |
| `q=`（空） | 200 | **0.004062** | 空查詢 → 快 |
| `q=quantum` | 200 | 0.515330 | 少命中 → 中等 |
| **`q=the`** | 200 | **92.143385** | **高命中 → 92 秒** |
| `/api/health`（事後） | 200 | 0.002057 | 事後恢復正常 |

**與 `page_size` / `page` 無關**（handler 實測 `page_size=100` → 93.45s、`page=5` → 93.90s）。分頁救不了它。

---

## 三、機制（`app/db/search.py`）— **99.99% 集中在單一因子**

全部由 dispatcher 獨立在原始碼確認（控制組：檔案 181 行、負控制組 bogus token = 0，工具有鑑別力）：

1. **`src:13` `work_fts` 用 `tokenize='trigram'`** — `the` 是極高頻 trigram，對整個庫近似全掃。
2. **`src:112` `count_sql = SELECT COUNT(*) FROM work w WHERE {where_sql}`** — 每次搜尋先跑一次**完整計數**，`LIMIT`/`OFFSET` 完全幫不上忙。
3. **`src:127-135` snippet 是逐列相關子查詢**：
   ```sql
   CASE WHEN ? != '' THEN (
       SELECT snippet(work_fts, 3, '<mark>', '</mark>', '...', 20)
       FROM work_fts WHERE work_fts.work_id = w.work_id AND work_fts MATCH ?
       LIMIT 1
   ) ELSE NULL END as snippet
   ```
   **每一列都重跑一次 `MATCH`。**
4. **`src:137-139` 三個 `LEFT JOIN`**（`manifestation` / `file_object` / `reading_state`）+ 外層 `ORDER BY` 無索引欄位。

### 三之二、實測占比（2026-08-21 補，**推翻本節原本的「四個因子疊乘」寫法**）

原文寫「四個因子疊乘」並標明未坐實占比。拆解後證實**那個框架是錯的**：

```
A1 FTS MATCH only (work_id list)                  0.016s  -> 14 rows
A2 count_sql (full COUNT)                         0.009s  -> 14
A3 select_sql WITHOUT snippet                     0.009s  -> 14
A4 select_sql WITH snippet (as app)              97.643s  -> 14
A5 snippet for ONE work_id                        0.005s
A6 CONTROL q=zzzznomatch (must be fast)           0.001s  ->  0
```

| 因子 | 編號 | 實測占比 |
|---|---|---|
| **snippet 逐列子查詢** | 3 | **99.99%**（97.634s / 97.643s）|
| count_sql 全表 COUNT | 2 | 0.009s |
| trigram 全掃 | 1 | 0.016s |
| 三個 LEFT JOIN + ORDER BY | 4 | 含在 A3 的 0.009s 內 |

`EXPLAIN QUERY PLAN` 指同一格：`CORRELATED SCALAR SUBQUERY 1` 下掛
`SCAN work_fts VIRTUAL TABLE INDEX 0:M4`。

**⇒ 因子 1、2、4 加起來不到 0.04 秒。針對它們的任何優化都是白費。**

---

## 四、放大路徑：它會癱瘓全站（含不碰 DB 的端點）

`/api/search`（`app/api/routes.py:37`）與 `/api/health`（`src:31`）都是 **`def`（sync）**，FastAPI 會把它們丟進 anyio threadpool。實測上限：

```
anyio to_thread.current_default_thread_limiter().total_tokens = 40
```
（dispatcher 獨立重跑，與 handler 一致）

**一個 `q=the` 請求佔住一個 token 92 秒。** 於是嚴重度隨併發分級（handler 四組實驗，含鑑別力控制組）：

| 條件 | 觀察到的形狀 |
|---|---|
| idle（RUN1，60s） | 三支探針 p50 3-4ms，max ~50ms，**0 次 >20s** |
| 併發 8，慢查詢（RUN2） | 只有 search 慢（p50 103s），其餘端點**完全正常**（8 < 40 tokens，沒排隊） |
| 併發 60，快查詢（RUN4，**鑑別力控制組**） | 三支同時退化，collections p50 **3ms → 620ms**（兩個數量級）⇒ 探針確實抓得到退化 |
| 併發 40，慢查詢（RUN6） | **全站停擺 >120s**，連 `async def` 的 `/api/crawler/jobs` 都 timeout |

> ⚠ **`async def` 的端點也吃 threadpool token** —— 這格是後來才查明的，見 §五之二。
> 所以 RUN4 的 jobs（p50 204ms）與 RUN6 的 jobs（timeout）**不需要動用 GIL 就能解釋**：
> 它的相依 `get_worker` 是 sync def，飽和時它連相依解析都排不進去。
> GIL 爭用可能仍有貢獻，但**不是必要條件**，原本寫成必要是過度歸因。

---

## 五、這格與 BR-160000 的關係（**歸因已於 2026-08-21 撤回，見五之二**）

BR-160000 原本把 30.7s 尖峰歸因為「SQLite 鎖爭用累計」。本 BR 一度改寫為
**「單一慢查詢佔住 threadpool token」**，依據是歷史形狀看似逐格對上：

```
歷史觀察（BR-160000）
  L2_collections  30.704   尖峰   ← sync def，需 token
  L3_search       30.845   尖峰   ← sync def，需 token
  L1_jobs          0.005   正常   ← async def，「不需 token」 ← ✗ 這一格是錯的
  L0_404           0.002   正常   ← 不需 token
```

**上表第三列的判準是錯的，整個歸因隨之撤回。** 詳見下節。

### 五之二、撤回：threadpool 飽和**不能**解釋歷史 30.7s

由 handler `ses_fdf8fc2c4`（提出原歸因的同一方）主動撤回，dispatcher 獨立驗證機制後採納。

**錯在哪**：原判準表寫「`async def` ⇒ 不需 token」，但 **FastAPI 的相依解析發生在 body 之前，
且 sync 相依項一律走 threadpool，與端點本身是否 async 無關**：

```
crawler_routes.py:179   async def list_download_jobs(worker = Depends(get_worker))
crawler_routes.py:22    def get_worker() -> DownloadWorker:        ← sync def
fastapi/dependencies/utils.py:673-676
    elif _is_coroutine_callable(use_sub_dependant.call):
        solved = await call(**solved_result.values)
    else:
        solved = await run_in_threadpool(call, **solved_result.values)   ← 走這條
```

**所以 `/api/crawler/jobs` 也搶那 40 個 token。**

dispatcher 獨立驗證（**第一次驗錯，重驗才拿到真值——記錄於此以免下一個人重蹈**）：

```
第一次：regex 抓 ^(async )?def get_\w+  →  PROVIDER_TOTAL=22, ASYNC_PROVIDERS=2
        ✗ 把「路由處理函式」誤當 provider（get_category_works / get_job_status
          是 @router.get 的 handler，不是 Depends 目標）

重驗：先收集所有 Depends(X) 實際引用的名字，再查那些名字的 def 種類
        NAMES_USED_IN_Depends = [get_crawler, get_dao, get_pipeline,
                                 get_search, get_storage, get_validator, get_worker]
        TRUE_ASYNC_PROVIDERS = 0        ← handler 說對了
        CONTROL_depended_names = 7      ← 有鑑別力，不是恆回空
```

**證偽的關鍵是 handler 自己的數據**：

| 實驗 | jobs 表現 | 意義 |
|---|---|---|
| idle（RUN1） | 3ms | 基線 |
| RUN4（60 併發快查詢） | p50 **204ms**、max 973ms | 飽和時 jobs **確實**被卡 |
| RUN6（40 併發慢查詢） | **120s timeout** | 飽和時 jobs **完全**卡死 |

**若歷史那次真是 threadpool 飽和，`L1_jobs` 也該被卡住；歷史觀察是 0.005s。⇒ 不符，假說排除。**

### 五之三、那歷史 30.7s 是什麼？回到原判

`L1` 需要 token 卻快、`L2`/`L3` 需要 token 卻慢 ⇒ **差別不在拿不拿得到 token，在拿到之後做什麼**：

```
L1_jobs   拿到 token → worker.list_jobs() 讀記憶體 dict  → 0.005s
L2/L3     拿到 token → 走 SQLite                        → 30.7s
```

差異落在 **DB 路徑本身**，不在排隊。而那組量測時 **DB 還在 NFS**
（`git show 73de0aa:docker-compose.yml:19`），`dec5b44` 已將其搬到本地 ext4。

**現行判定**：歷史 30.7s 的成因**仍未確定**。threadpool 飽和假說已排除；
剩餘主要候選為「DB 當時位於 NFS」（已被 `dec5b44` 消除）。

⇒ **本 BR 與歷史 30.7s 很可能是兩個不同的缺陷，只是形狀相似。** 兩者的連結已斷開。

### 五之四、什麼**沒有**因此動搖（避免矯枉過正）

| 宣稱 | 狀態 | 依據 |
|---|---|---|
| `q=the` 確定性 92-95 秒 | **不動** | 直接量測，非歸因（dispatcher 92.143385s） |
| 三個負控制組 4ms ⇒ 慢的是命中筆數 | **不動** | 直接量測 |
| threadpool = 40 tokens | **不動** | 雙方各自獨立量到 40 |
| 飽和造成全站退化（RUN4）／停擺（RUN6） | **不動** | 直接量測 |
| `search.py` 四個機制因子 | **不動** | dispatcher 逐條在原始碼確認 |
| WAL 下 writer 鎖不擋 reader | **不動** | 負面實測 |
| **「threadpool 飽和解釋歷史 30.7s」** | **撤回** | 本節 |
| BR-040000 每請求 `os.mkdir` | **成立且加強** | 7 個 provider **全部**為 sync def |

**本 BR 的 CRITICAL 判定完全不受影響** —— 那是量出來的，不是推出來的。

### 附帶：writer 鎖假說有負面實測證據

handler 對線上 DB 持有 writer 鎖 12 秒（純 `BEGIN IMMEDIATE` + `ROLLBACK`，不寫任何列），同時段三支探針 **p50 3ms、0 次 >1s，完全沒有影響**。因為 `app/db/engine.py:56` 設了 `PRAGMA journal_mode = WAL`，**WAL 下 reader 不會被 writer 擋住**。

> 這**不等於**「候選 1 已排除」。它只窄到一句：**「writer 鎖阻擋唯讀請求」這個特定分支，在 WAL 下有負面實測證據。**

---

## 六、修復方向（**已定案並實作，見六之二**）

> ⚠ **下表是本 BR 建檔當下的原始猜測，其中 A 與 B 已被實測證偽。**
> 保留它是為了讓下一個讀者看到「為什麼那兩條看起來合理卻不會生效」——
> 直接刪掉的話，下一個人會再想出同一個選項 A。

| 選項 | 動作 | 風險 |
|---|---|---|
| ~~A~~ | ~~snippet 改成只對本頁 20 列算~~ **✗ 不會生效，見六之二** | — |
| ~~B~~ | ~~`COUNT(*)` 改成上限估計~~ **✗ 白費，count 只佔 0.009s** | — |
| C | 高頻詞的 trigram 查詢加 `rank` 限制 / 改 tokenizer | 高，影響檢索品質 |
| D | `/api/search` 改 `async def` + `run_in_threadpool` | **不解決慢，只改變排隊位置**；且無助於 GIL |

**D 明確不是解法** —— 它只把排隊從 threadpool 換到別處，92 秒還是 92 秒。
修復後的實測進一步坐實：**根因消失後 40 併發下 threadpool 完全不飽和（>1s 次數 = 0），
排隊問題自動消失，不需要動 D。**

### 六之二、⚠ 選項 A 為什麼不會生效（**這格比修法本身更容易失傳**）

**成本與命中列數無關，與文件大小線性相關。** dispatcher 獨立實測：

```
HIT_ROWS=14   ⇒ N=14 < 20 ⇒「只取本頁 20 列」這個上限根本不會生效

per-doc snippet cost（逐個 work 量，含 content 長度）
  wk_c58e351110304a00       1131 chars    0.004s
  wk_46586dcb0be5447f     238747 chars    0.005s
  wk_189094b2c6f84ad0     328820 chars    0.058s
  wk_c07ec58fb975496e     311654 chars    0.937s
  wk_78ec573c5013465f    1953622 chars    4.885s
  wk_8c6d31cc16734fcd    2177753 chars    5.864s
  wk_45205a19040640dc    2201780 chars    6.583s
  wk_59625e5e6086441b    2459854 chars    8.088s
  wk_9b1556deec6c44f5    2615284 chars    9.503s
  wk_0e5c93bcf7634d06    2389233 chars    9.801s
  wk_587aa2ce8bb64e46    2191496 chars    9.833s
  wk_0389cd70dae24383    2615284 chars   10.441s
  wk_34e8385cf2334f9c    3873836 chars   17.061s
  wk_97feae4764f4456c    3873836 chars   19.002s   ← 單一文件就 19 秒
TOTAL_CHARS=27232330
DOCS_OVER_1S=10 / 14
CONTROL_rows_nonempty=True
```

**決定性佐證**（handler 實測）——把「逐列各一次查詢」換成「單次掃描一併取回」，
即呼叫次數從 14 降到 1：

```
PLAN_A_per_row_loop   = 97.3s    （14 次呼叫）
PLAN_B_single_scan    = 97.262s  （1 次呼叫）
```

**呼叫次數降 14 倍，時間完全沒變。** 成本不在呼叫次數，在 `snippet()` 每次都要
重新掃描整份 content 定位 token 偏移。

> **這格對讀者的實際傷害**：照選項 A 實作的人會做完、測不出加速、然後**懷疑自己實作錯了**。
> 失敗的形狀（做完了、沒變快）不會指向「建議本身錯了」。

### 六之三、實際採用的修法

SQL 端 `instr` + `substr` 取固定視窗。三個候選的實測：

| 方案 | 耗時 | 傳輸量 | 判定 |
|---|---|---|---|
| FTS5 `snippet()` | 97.6s | — | 現況 |
| Python 端全文處理 | 0.230s | **27.2M 字元** | 快，但記憶體風險 |
| **SQL `instr`+`substr`** | **0.078s** | **840 字元** | **採用** |

**未選 Python 全文版**，即使它也快 424 倍：要把 27.2MB 拉進 process 記憶體，
`page_size=100` 時達數十 MB，40 併發下是新的癱瘓路徑。
**修一個效能缺陷時引入另一個，不是可接受的交換。**

改動（`app/db/search.py`，+90/-21）：

1. 抽出 `extract_query_terms()`，與 `build_fts_query()` **共用同一組切詞規則**
   —— 否則「WHERE 命中的詞」與「被高亮的詞」會不一致，那會讓「這列沒高亮」
   同時代表兩件不同的事（判準①的形狀）
2. 移除 SELECT 中的 snippet 相關子查詢
3. 新增 `_build_snippet_map()`：**分頁之後**只對本頁 work_id 取 `instr`+`substr` 視窗，
   Python 端 regex 包 `<mark>`
4. 長詞優先排序，避免 `the` 先被包後破壞 `theory` 的標記

**snippet 語意變更（已裁示接受）**：舊的是 FTS5 token-aware（`'...'` 分隔、20 token），
新的是字元視窗（前 30 字 + 共 120 字）。**內容不逐字相同，但都含 `<mark>`、都是合理摘要。**
dispatcher 裁示：snippet 是**摘要**不是**資料**，契約是「讓使用者看到命中脈絡」，
不是「逐字重現某個特定演算法的輸出」。判準 3/4 刻意只比 `work_id` 集合與 `<mark>` 存在。

---

## 七、驗收判準

1. `q=the&page_size=20` 從 92s 降到 **< 2s**（附前後對照與 3 次重跑）。
2. 三個負控制組（`zzzznomatch` / 空查詢 / `collections`）**仍然 < 50ms** —— 證明沒有靠關掉功能換速度。
3. 搜尋結果的**內容**不變：同一個 query 修復前後回傳的 `work_id` 集合一致（附集合比對，不是只比筆數）。
4. snippet 仍然有 `<mark>` 標記（若走選項 A，要證明沒把 snippet 弄丟）。
5. 併發 40 個 `q=the` 時，`/api/health` 仍 < 1s（RUN6 的複驗）。
6. 全套件 `259 passed`（skip 27 全部是 e2e opt-in，屬預期）。

---

## 八、尚未檢定的

1. **沒有自發重現。** 所有尖峰都是誘發的。「正常使用中此刻會不會自己發生」未證明 —— 但觸發門檻極低（一個常見字就夠）。
2. **不知道歷史那次 30.7s 當下的併發數。** 機制解釋與形狀相符，但沒有直接證據證明當時真有 40 個 sync 請求在排隊。**這是本判定最弱的一格。**
3. `EXPLAIN QUERY PLAN` 未跑（見 §三）。
4. raw_dir / NFS 假說（BR-20260821_040000）**完全沒測**，仍是未檢定的理論風險。

---

## 九、Related

- `BR-20260820_160000-live-sqlite-on-nfs-latency-undecided-and-multihost-risk.md` — **同一個觀察對象**（那組 30.704s / 30.845s 尖峰）。本 BR 提供其候選 1 的改寫依據。判準 8 由本 BR 承接。
- `BR-20260820_124500-quick-collections-modal-blocking.md` — §三 的 20-27s 量測是同一個現象的另一次觀察；其事實基礎已標過期（量測時間 `c663041` 早於 DB 搬遷 `dec5b44` 四小時）。
- `BR-20260820_210000-async-routes-sync-io-on-event-loop-family.md` — 同族（事件迴圈阻塞）。E 節 `download_worker.py:707` 的 `f.write` **不再是唯一診斷訊號**，因為真正的訊號是 `/api/search`，故 E 節現在可動。
- `BR-20260821_040000-raw-and-parsed-dirs-still-on-nfs-after-db-migration.md` — 同族但**不同機制**。本 BR 的歷史形狀（`L1_jobs` 同刻正常）**推翻**了 NFS 阻塞 event loop 作為歷史尖峰的解釋；BR-040000 描述的是一個尚未被觀察到的第二機制。

---

## 十、證據落點

- handler 的量測工具：`script/latency_probe.py`（三支探針真並行，各自 daemon thread）
- raw jsonl：`/run/user/1000/openshelf-spike-probe/`
- dispatcher 獨立複驗：本文件 §二、§三、§四的 threadpool token 數
