# BR-20260820_210000 — `async def` 路由與 worker 在事件迴圈執行緒上做同步 I/O（族群性缺陷）

- **Status**: **PARTIAL** — A+B 節已修並驗證（commit `2920ef6`，2026-08-20）；**C/D/E/F 節仍在**。
  處置紀錄與更正見下方「## 處置紀錄（A+B 節）」。
- **Owner**: ses_fe7b5cbadffeSlxj0dv1Z740O4（值星官）
- **Family**: `event-loop-blocking`
- **Severity**: 高（使用者可感知：任何一格阻塞 loop，全站所有請求整條延後，包含完全不碰 DB 的端點）
- **Filed**: 2026-08-20

## **Related**

- `issues/closed/BR-20260820_200000-crawler-search-blocks-event-loop.md` — **同族，且本 BR 是它的復發**。
  依據：**同一條執行路徑**（`crawler_routes.py:48 live_search`）、**同一種失效類別**（`async def` 內同步 I/O 阻塞事件迴圈）。
  BR-200000 的修復（commit `2e7d665`）只把 `_annotate_local_status` 搬進 threadpool，**`:55` 的
  `await crawler.search(q)` 沒搬**——而它內部仍有同步 SQLite 讀。修的是那條路徑上的其中一格，不是那一類。
- `issues/BR-20260820_160000-live-sqlite-on-nfs-latency-undecided-and-multihost-risk.md` — **候選機制供給者**。
  依據：BR-160000 的兩個候選機制（鎖爭用累計 / NFS `timeo=600,hard` I/O 阻塞）都需要「某個東西長時間佔住
  執行緒」才會表現成 20-30s 的 HTTP 延遲。本 BR 的 C 節（下載迴圈同步 `f.write` 到 NFS）是**先前未被列入
  候選清單**的第三個機制候選。

## 這一類缺陷為什麼特別危險

`async def` 路由的 body 直接跑在事件迴圈執行緒上。一旦裡面有同步 I/O，**卡住的不是那一條路由，是整個
process 的所有請求**——包括根本不進 route 的 404 路徑。

本 repo 已實測坐實（值星官的隔離 FastAPI 探針，`73de0aa` 驗收期間）：

```
async def 內 time.sleep(1.5) 期間
  同步 def 路由     1252.4ms   BLOCKED    ← 「走 threadpool」不免疫
  404 路徑          1251.4ms   BLOCKED    ← 連不進 route 的都被卡住
baseline             25.8ms /    1.0ms
CONTROL 直接打 blocker            1501.6ms
```

機制：「`def` 路由走 threadpool」講的是**函式體**在 threadpool 執行；但請求得先經事件迴圈完成 ASGI 接收、
路由匹配、派送。事件迴圈被佔住時派送輪不到——請求卡在**進入 threadpool 之前**。

## A. BR-200000 的修復不完整（同一條路徑）

`app/api/crawler_routes.py:55`：

```python
raw_results = await crawler.search(q)          # ← 沒進 threadpool
await run_in_threadpool(_annotate_local_status, raw_results, dao, worker)   # ← :60 已修
```

`crawler.search` 的內部鏈（逐格已讀，非推測）：

```
libgen_live.py:270  async def search
  └ libgen_live.py:274  async def _execute_single_search
      └ :277  for mirror in self.active_mirrors           ← property
          └ :41   self.dao.get_active_libgen_mirror_urls()
              └ dao.py:930  def （同步）                   ← 同步 SQLite 讀，在 loop 執行緒上
      └ :290 / :292  _parse_libgen_is_html / _parse_libgen_li_html
                     BeautifulSoup 全文解析（CPU-bound），同樣在 loop 上
```

每個 candidate query 各觸發一次同步 SQLite 讀；每個鏡像回應各觸發一次 BeautifulSoup 全文解析。

## B. `category_routes.py:40 get_category_works` — `live_search` 的雙胞胎，完全未修

```python
async def get_category_works(...):
    cat = dao.get_category(category_id)                    # :49  同步 DB
    total, local_items = dao.get_category_works(...)       # :53  同步 DB
    ...
    raw_cloud = await crawler.search(cloud_query)          # :80  同 A 節整條鏈
    for cr in raw_cloud[:15]:
        local_wid = dao.find_work_by_hash(cr_md5)          # :89  最多 15 次逐筆 DB 往返
```

`:89` 與 BR-200000 修掉的那個迴圈**形狀完全相同**——同樣是 `for` 迴圈內逐筆 `find_work_by_hash`，只是在另一個
檔案。`2e7d665` 已在 `dao.py:341` 新增 `find_works_by_hashes()` 批次版，此處可直接沿用。

## C. `routes.py:153 upload_book`

```python
async def upload_book(...):
    contents = await file.read()
    work_detail = pipeline.ingest_bytes(...)   # :164
```

`app/pipeline/ingest.py:41 def ingest_bytes` 是**同步 def**，內含落檔、`PDFExtractor.extract`（PyMuPDF，
CPU-bound）、多次 DB 寫。**大 PDF 可鎖住整條 loop 數十秒。**

這是目前發現的**單次阻塞時長最長**的一格，且它的量級（數十秒）與 BR-160000 追的 20-30s 尖峰同數量級。

## D. `settings_routes.py:45 validate_libgen_mirror`

```
:58  dao.get_libgen_mirrors()          同步 DB 讀
:69 / :92  dao.current_iso()
:98  dao.save_libgen_mirrors()         同步 DB 寫
:55  → validator.py:51 dispatch_br() → validator.py:238 file_path.write_text()   同步檔案寫
validator.py:65 / :88  BeautifulSoup 解析
```

## E. worker 側 —— 下載迴圈每 64KB 一次同步寫

`app/crawler/download_worker.py:382-386`：

```python
with open(part_file, mode) as f:
    async for chunk in resp.aiter_bytes(chunk_size=65536):
        ...
        f.write(chunk)          # ← 同步寫，在 async 迴圈裡
```

**整個數百 MB 下載期間持續佔用 loop 執行緒**，且寫入目標在 NFS 上（見 BR-160000）。

`:418 self.pipeline.process_file(final_dest, metadata_override)` 同樣是完整同步入庫（PDF 抽取 + 多次 DB 寫），
直接跑在 `_process_queue` task 裡。

其餘較輕的：`:302` / `:205` `_save_jobs_to_disk()` 同步 JSON 全檔重寫；`:411` `part_file.replace()` 跨裝置時
退化為整檔複製。

## F. 較輕但成本隨 N 成長的

- `crawler_routes.py:130 enqueue_batch_download` — 迴圈 N 次 `enqueue()`，每次 `_save_jobs_to_disk()` 全檔
  JSON 重寫 ⇒ **O(N²) bytes**，全在 loop 上。
- `crawler_routes.py:206 delete_download_job` — `:243 part_file.unlink()` 可能是數百 MB 的 `.part`。
- `settings_routes.py:110 list_dispatched_issues` — 雖是 sync `def`（不阻塞 loop），但 `:116 glob + stat`、
  `:119 read_text` 逐檔讀 `issues/`，佔用 anyio threadpool 名額（預設 40），在 NFS 上尤其。

## 證據取得方式與控制組

```
CONTROL run_in_threadpool|asyncio.to_thread 全 app/ 命中 = 2
        app/api/crawler_routes.py:3   (import)
        app/api/crawler_routes.py:60  (呼叫)
        ← 證明 pattern 抓得到「已修的那格」，其餘 14 個 async 路由是真的沒有 threadpool hop，
          不是 pattern 失效

CONTROL requests.   全 app/ = 0 (rc=1)；同族 httpx. 命中 4 檔
        ← 證明「找 HTTP client」pattern 有效，是 requests 真的不存在
CONTROL time.sleep  全 app/ = 0 (rc=1)；asyncio.sleep 命中 download_worker.py:323
        ← 證明 sleep 家族抓得到
```

`async def` 路由全 repo 共 15 個（含 `main.py:20 lifespan`）；`APIRouter(` 只出現在 `app/api/*.py` +
`app/main.py`，無漏檔。

## 驗收判準

**共通要求（本族已有兩次「修了其中一格就當作修完那一類」的紀錄）**：

任何宣稱「某個 `async def` 路由已無同步 I/O」的證據，**必須附控制組**證明檢查工具在該壞掉時真的會非空。
`grep` 回空同時是「沒有」與「pattern 打錯」的答案。

**⚠ 不得使用的判準**：`L1_health` 對照組。BR-200000 原本用它推導「`def` 路由免疫」，該假說已被實測推翻
（見上方隔離探針數據）。`def` 路由與 404 路徑**都會**被 loop 阻塞拖慢，所以「修復前後 L1_health 都正常」
**不是**有效的驗收條件——照字面執行會把真實有效的修復判為不合格。

**有效的分水嶺**：量 **loop lag** 本身（在事件迴圈上跑一個固定週期的 heartbeat task，記錄它實際被喚醒的
間隔），而不是量任何一條 HTTP 路由。loop 被阻塞時 heartbeat 的間隔會直接拉長，且該訊號不與 threadpool
飽和、DB 鎖、NFS I/O 共用輸出。

**逐節判準**：

1. **A 節** — `crawler.search` 的同步 SQLite 讀與 BeautifulSoup 解析必須移出 loop 執行緒。
   控制組：修復後 `active_mirrors` 的 DB 讀路徑仍需被實際執行到（不可用「快取住就不讀了」規避，
   那會讓「修好了」與「這條路徑沒被走到」共用同一個輸出）。
2. **B 節** — `category_routes.py:89` 改用 `dao.py:341 find_works_by_hashes()` 批次版。
   控制組：回傳結果必須**同時包含**已落地與未落地兩類（否則恆真/恆假的實作也會通過）。
3. **C 節** — `upload_book` 的 `ingest_bytes` 必須走 threadpool。
   控制組：上傳一個真實 PDF，量 loop lag 在上傳期間是否仍在毫秒級。
4. **E 節** — 下載迴圈的 `f.write` 與 `process_file`。
   **注意**：這格可能與 BR-160000 的候選機制重疊，修它之前先確認不會遮蔽 BR-160000 的診斷訊號。
5. **回歸** — pytest 基線 155 passed（`3a5a8b6`..`f8f5618` 後），`rc` 獨立一行取，不接管線。

## 沒驗證的

---

## 處置紀錄（A+B 節）

**commit `2920ef6`** — handler `ses_fe1437b14ffeef47UKuGuwbm2h`，
dispatcher `ses_fe7b5cbadffeSlxj0dv1Z740O4` 獨立驗收。

### 修了什麼

```
A 節  app/crawler/libgen_live.py
      active_mirrors property 保留同步語意（validator + 三個測試檔直接呼叫）
      新增 _resolve_active_mirrors_async()，asyncio.to_thread 包住
      _execute_single_search 改走 async 版
      兩個 _parse_libgen_*_html 呼叫改 await asyncio.to_thread(...)

B 節  app/api/category_routes.py
      get_category / get_category_works 改 run_in_threadpool
      15 次逐筆 find_work_by_hash 改單次 find_works_by_hashes 批次
```

### loop lag（dispatcher 獨立重做，heartbeat 10ms）

```
CONTROL time.sleep(0.60) ON LOOP     max_hb=610.3ms   ← 偵測器有鑑別力
CONTROL idle                         max_hb= 10.3ms   ← 底線

A1 sync active_mirrors 200x           79.2ms → 11.8ms（= idle 底線）
   CONTROL DB 呼叫 before=200 after=200  ← 未被快取規避
A2 sync BeautifulSoup 6x             744.6ms → 57.2ms
   CONTROL units=399 兩邊皆同           ← 非空轉
B  40x(15x 逐筆) → 批次              241.9ms → 15.0ms   work 231.7 → 37.8ms
   真實 80MB DB：hit=5 miss=20 兩類皆非空，per-key 與逐筆版等價
   空清單邊界：回 dict len=0，不拋錯
```

### 本 BR 三處被實測推翻，已更正

**① 風險排序應對調。** A 節的主要阻塞源是 **BeautifulSoup 不是 SQLite**：

```
同步 SQLite 讀      0.40 ms/次
BeautifulSoup 解析  124   ms/次      ← 310 倍
```

上方 A 節把「同步 SQLite 讀」寫在鏈的最前面、解析寫在後面，**讀起來像前者是主因**。
那是排版誤導，不是機制描述錯誤——兩格都真的在 loop 上，只是量級差 310 倍。

**② 「沒驗證的」第 2 項（index）方向反了。** 原文擔心「沒查是否有 index，
可能讓 A 節收益比預期小很多」。實測 index 齊備：

```
file_object   idx_file_object_md5 / idx_file_object_sha256
identifier    idx_identifier_lookup
EXPLAIN       MULTI-INDEX OR + SEARCH USING INDEX + COVERING INDEX，無 table scan
CONTROL       PRAGMA index_list(zzz_no_such_table) len=0
CONTROL       SELECT ... WHERE size_bytes > 1  →  SCAN file_object
```

第二個控制組是關鍵：**只證明「真表有 index」不夠，還要證明這個探針在該回
SCAN 時真的會回 SCAN**，否則「全走 index」與「EXPLAIN 壞了」共用同一個輸出。

結論仍是「收益比預期小」，但理由完全不同——不是 index 缺失，是那格 DB 讀本來就便宜。

**③ A 節的修復落點不在 `crawler_routes.py:55`。** 該行 `await crawler.search(q)`
本身是正確的 async 呼叫，缺陷在被它呼叫的 `libgen_live.py:277/290/292`。
`crawler_routes.py` 本包**零改動**（`git diff --quiet` rc=0，
控制組對 `libgen_live.py` rc=1 證明該檢查有鑑別力）。

### 派工單缺陷（記在 dispatcher 頭上）

dispatcher 的禁區清單把 `tests/test_download_worker_enqueue_autostart.py`
列給本 handler 當「不可碰」，卻**同時列給另一顆 handler 當「可碰」**
（BR-230000 判準 4 要求它拿掉 `_hard_cancel`）。於是禁區掃描誤報。

已用內容釘死歸屬：本 handler 的符號在該 diff 命中 0（rc=1），
控制組同 grep 對其自身 diff 命中 13（rc=0）。**兩顆 handler 的檔案集切分無誤，
錯的是 dispatcher 的清單。**

### C/D/E/F 節為何未派

```
E 節   動 app/crawler/download_worker.py，與 BR-230000 的檔案集交集非空
       （comm -12 = 1，控制組 comm -12 set vs 自己 = 3 證明有鑑別力）
       且 E 節的同步 f.write 到 NFS 是 BR-160000 的第三個候選機制
       —— 現在動它會抹掉觀察期的診斷訊號（本 BR 判準 4 自己寫的）
C/D/F  範圍收斂：族群性缺陷一次吞六節會讓驗收無法歸因
```

### 派 C/D/F 節時必須加的判準（handler 的自陳，dispatcher 採納）

**anyio threadpool 飽和（預設 40 名額）尚未被量過。** A+B 節的修復把壓力
**從 loop 移到 threadpool**；C/D/E 節若照做會加倍。原「沒驗證的」第 4 項
仍然成立，而本次修復**增加**了那條路徑的負載。

下一包必須把它納入判準——否則會把 loop 阻塞換成 threadpool 排隊，
而**兩者對使用者呈現的症狀相同**（請求變慢），卻需要完全不同的修法。

1. **沒量任何阻塞的實際時長**。上述風險分級是依「同步呼叫是否落在 loop 執行緒」判定的**結構性**分級，
   不是實測毫秒數。C 節與 E 節標為最高風險是**假說**，未用 profiler 或 loop-lag 監測證實它們就是
   BR-160000 那個 20-30s 尖峰的成因。
2. **沒查 `dao.py` 各方法的實際查詢成本**（是否有 index）。只確認它們是 sync `def` 與行號。
3. **沒讀 `app/pipeline/ingest.py` 全文**，只確認 `ingest_bytes:41` / `process_file:128` 是 sync def。
4. **沒查 anyio threadpool 是否曾實際飽和**（40 名額）。這是與 loop 阻塞不同的第二條路徑，未量。
5. **沒有實際觸發任何一格**。全部是靜態讀取 + 一次隔離探針（那個探針證明的是「阻塞會傳播」這個機制，
   不是「這些特定的點會阻塞多久」）。
