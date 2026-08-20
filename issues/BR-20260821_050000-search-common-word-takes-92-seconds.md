# BR-20260821_050000 — 搜尋常見英文字確定性耗時 92 秒，且會癱瘓全站

- **Status**: OPEN
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

## 三、機制（`app/db/search.py`，四個因子疊乘）

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

> ⚠ **未逐條坐實相對占比。** 沒有跑 `EXPLAIN QUERY PLAN`，所以「四個因子各佔多少」是機制推論不是實測。修復前應先跑一次以免優化錯地方。

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

## 六、修復方向（未定案，需先跑 EXPLAIN QUERY PLAN）

| 選項 | 動作 | 風險 |
|---|---|---|
| A | snippet 改成**只對本頁 20 列**算（先分頁再 snippet，不要在 `SELECT` 裡逐列子查詢） | 低，語意不變 |
| B | `COUNT(*)` 改成上限估計（`LIMIT 1000` 後回「1000+」）或快取 | 中，改變「總筆數」顯示語意 |
| C | 高頻詞的 trigram 查詢加 `rank` 限制 / 改 tokenizer | 高，影響檢索品質 |
| D | `/api/search` 改 `async def` + `run_in_threadpool` | **不解決慢，只改變排隊位置**；且無助於 GIL |

**D 明確不是解法** —— 它只把排隊從 threadpool 換到別處，92 秒還是 92 秒。

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
