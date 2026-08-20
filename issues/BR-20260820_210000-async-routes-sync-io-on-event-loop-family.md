# BR-20260820_210000 — `async def` 路由與 worker 在事件迴圈執行緒上做同步 I/O（族群性缺陷）

- **Status**: **PARTIAL** — A/B/C/D/F 節已修並驗證；**E 節仍在**（使用者裁示暫緩，見下方）。
  - A+B `2920ef6` / C+D+F(部分) `054838c` / F 節 worker 層 `4aa378a` / F 節接線 `2391ae0`
  - **E 節未修是決策不是漏修**：它動 `download_worker.py` 每 64KB 一次同步 `f.write`，
    而那是 BR-160000 觀察期的第三個候選機制——修它會抹掉診斷訊號。
    使用者裁示（2026-08-20）：先讓 BR-160000 觀察期跑完再動 E 節。
  處置紀錄與更正見下方「## 處置紀錄（A+B 節）」與「## 處置紀錄（F 節 O(N²)）」。
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

### C/D/F 已處置（commit `054838c`）；E 節仍未派

handler `ses_fe108fa63ffeZMgdEurLDyvEgB`，dispatcher 獨立驗收。

```
C  app/api/routes.py src:165   ingest_bytes → run_in_threadpool
                               app/pipeline/ingest.py 零改動
D  app/crawler/validator.py    兩處 _parse_libgen_*_html + 兩處 dispatch_br → asyncio.to_thread
   app/api/settings_routes.py src:59/:102  dao.get/save_libgen_mirrors → run_in_threadpool
F  app/api/settings_routes.py src:117  glob+stat → os.scandir；read_text → readline
   enqueue_batch_download / delete_download_job  未修（見下方分岔）
```

回歸 190 passed rc=0（baseline 185 + 新檔 5 條），rc 獨立取。
禁區 13 個 pathspec 全 0，控制組三個改過的檔各回 1、不可能的 pathspec 回 0。

#### handler 推翻我三格，全部坐實

**① F 節分岔的選項 2 不是「O(N²) 仍在」而是確定性失效——我的派工單給了一個會壞掉的選項。**

我寫「只在 route 層把整個迴圈包進 threadpool → 移出 loop，但 O(N²) 仍在」。
dispatcher 獨立重做探針（下載器已 stub，不打公網）：

```
CONTROL on-loop    _worker_task=CREATED  executed=['job_79f0947ecb72']  status=completed
SUBJECT in-thread  _worker_task=None     executed=[]                    status 停在 queued
VERDICT_DIFFERS = True    SUBJECT_JOB_NEVER_RAN = True
stderr：「無 running event loop，下載背景工作未啟動…將停留在 queued 不會被執行」
```

機制：`download_worker.py src:262-265` 把三件事綁在 `enqueue` 尾端——
`_save_jobs_to_disk()`（要移走的同步 I/O）／`queue.put_nowait()`（asyncio.Queue，非執行緒安全）／
`start()`（`src:159-160` 呼叫 `asyncio.get_running_loop()`）。threadpool 工作執行緒沒有 running loop，
`start()` 走進 `except RuntimeError` → log.warning → return。
**包 threadpool 會把 `autostart=True` 偷偷變成 `autostart=False`**，正是 BR-143000 鎖住的那兩態。

**② D 節的主阻塞源不是我列的 DB，是 validator 的解析。**

```
PRE  （HEAD b04d584）              max_hb = 525.4ms
POST （只修 route 層 DB 存取）      max_hb = 556.7ms   ← 沒有改善
POST2（再修 validator 解析+落檔）   max_hb =  47.1ms   ← 真正的修復
```

與本 BR「處置紀錄 ①」承認的 A 節誤導**完全同型**：我把 `dao.get/save` 列在清單最前面、
`validator.py` 的 BeautifulSoup 列在最後一行，讀起來像前者是主因。
**若只修 DB 那兩格，這一節等於沒修，而 route 層的測試會全綠。**

**③ `list_dispatched_issues` 加 threadpool hop 是零收益。** 它已經是 sync def，本來就跑在
threadpool，包一層只是換一個 token，佔用數不變。正解是縮短單次佔用時間（scandir + readline）。

#### 我推翻 handler 一格：`tp_waiting` 測的是探針批次大小，不是壓力搬家

handler 引用 `tp_waiting=20 @ 60 併發` 當「threadpool 確實排隊了」的證據。
dispatcher 實測七組 N，`waiting == max(0, N-40)` **全部成立**：

```
N     10  40  50  60  60  80  120
work 0.3 0.3 0.3 0.3 0.5 0.3  0.3
wait   0   0  10  20  20  40   80      ← 全部 == max(0, N-40)
CONTROL 60 @ 1ms → waiting=12 < 20     ← 證明探針該低於預測時真的會低
```

**那是算術必然不是量測結果**——它由我丟幾個進去決定，不論修復好壞都一樣。
「有排隊」與「我丟了 60 個進 40 格」共用同一個輸出。

正確指標是等待**時間**：

```
CONTROL 10 併發未飽和      p50=  3.0ms  max=  3.9ms   ← 儀器該近零時真的近零
CONTROL 40 併發剛好滿格    p50=  5.3ms  max= 11.4ms
60x @467.8ms（PRE 成本）   p95=476.5ms  max=477.0ms
60x @ 16.5ms（POST 成本）  p95= 25.0ms  max= 25.1ms   ← 19 倍
```

**壓力沒有搬家成災難，是同向改善**——佔用時間短 28 倍，佇列排空快 19 倍。
handler 的結論方向對，但它引的數字支撐不了那個結論。

#### F 節待裁決（未修，不是漏修）

`enqueue_batch_download` 的 O(N²) 唯一正確修法是讓 worker 提供批次入列 API（存檔一次），
那會動 `download_worker.py` = 禁區 + handler I 持有 `[OWNS download-worker-lifecycle]`。
`delete_download_job` 的 `part_file.unlink()` 同理。**handler 依派工單指示未自行動手。**

現況 N=120 實測 `jobs_file_bytes=57012`、wall≈95ms、loop lag 93.9ms——這個 N 還不痛，但它是 O(N²)。

#### 本節滾出一張新 BR

驗收 F 節時查 `list_dispatched_issues` 回 `total=0` 的原因，發現
**`dispatch_br` 寫進容器 ephemeral 目錄**（`/app/issues` 未掛載，`validator.py src:24` 的
`mkdir(exist_ok=True)` 讓缺席態與失敗態共用同一個輸出）。
→ `BR-20260820_223000-dispatch-br-writes-to-ephemeral-container-dir.md`

#### dispatcher 本輪自己的三個缺陷

1. **commit `054838c` 的訊息有一句不實陳述**：我寫「`/api/settings/libgen-mirrors/issues` 200,
   total>0 且標題非空」，**實際回的是 `TOTAL=0 ITEMS=0`**，而我自己的控制組已經標了
   `N/A-empty`。我把一個沒有鑑別力的結果寫成了證據。不 amend，留在歷史裡並在此更正。
2. **第一版 F 節探針掛住 120s（rc=124）**：控制組那條 `autostart=True` 真的啟動迴圈去打公網
   做指數退避重試，我沒 stub 下載器。清理後 `REAL_PYTHON_STRAYS=0`
   （`pgrep` 一度回 2 是匹配到我自己的 bash 命令列，控制組 `ps` 看得到 4 個 python 證明有鑑別力）。
3. **拿假 404 當證據**：先猜 `/api/settings/dispatched-issues` 回 404 就準備下結論，
   真實路徑是 `/api/settings/libgen-mirrors/issues`。**本班第二次**（前一次是打錯 port 8000）。
   改用 `openapi.json` 查真實路由才確定。

#### E 節的行號已漂移（本 BR 內文未更新）

本 BR 上方 E 節寫 `download_worker.py:382-386`，**實際 `f.write(chunk)` 在 `src:512`、
`process_file` 在 `src:544`**（BR-230000 的修復把檔案撐長了）。派 E 節時以 bytes 為準。

E 節仍未派的原因不變：它的同步 `f.write` 到 NFS 是 BR-160000 的第三個候選機制，
**現在動它會抹掉觀察期的診斷訊號**，需使用者裁決。

### （原記錄）C/D/E/F 節為何未派

```
E 節   動 app/crawler/download_worker.py，與 BR-230000 的檔案集交集非空
       （comm -12 = 1，控制組 comm -12 set vs 自己 = 3 證明有鑑別力）
       且 E 節的同步 f.write 到 NFS 是 BR-160000 的第三個候選機制
       —— 現在動它會抹掉觀察期的診斷訊號（本 BR 判準 4 自己寫的）
C/D/F  範圍收斂：族群性缺陷一次吞六節會讓驗收無法歸因
```

## 處置紀錄（F 節 O(N²)）

**狀態：已修並驗證。** `4aa378a`（worker 層）+ `2391ae0`（route 層接線）。

### handler I 推翻我兩格，皆經 dispatcher 獨立重驗坐實

**① O(N²) 是兩個不是一個。** 我的派工單只寫「每筆整份重寫 `download_jobs.json`」，
但 `enqueue()` 每筆還做 `src:247` 的線性掃描找重複。逐筆 N 次 ⇒ **掃描 O(N²) + 寫入 O(N²)**，
只修存檔那半會讓掃描原封不動留著。

**② 「N=120 還不痛」是線性外推，而 O(N²) 不能那樣外推。** dispatcher 獨立重測：

```
   N   per-item    batch   speedup  saves_old  saves_new
  30     11.7ms     1.2ms     9.8x         30          1
 120     96.6ms     2.1ms    45.6x        120          1
 480   1507.3ms     8.2ms   183.5x        480          1   ← 1.5 秒 loop 阻塞
1000   6484.6ms    18.5ms   350.7x       1000          1

CONTROL spy 逐筆 10 筆 → saves=10          證明 spy 真的在數
CONTROL 空批次        → saves=0（非 1）    證明「1 次」不是恆定值
CONTROL 全重複批次    → saves=0, jobs=5    證明去重不是靠不建 job 達成
每組 assert len(jobs)==n                   證明兩邊都真的建了 N 個 job
```

**N=480 是 1.5 秒的事件迴圈阻塞**，使用者勾選整頁搜尋結果就到這個量級。

### handler I 主動標 FIXED-UNDEPLOYED（本包最有價值的一格）

它在交件**第 0 節開宗明義**寫：worker 層 O(N) 可用，但 route 層仍逐筆呼叫，
**線上零改善**。route 層是它的禁區（我派工單明列「回報給我，我另外處理」）。

若它照一般寫法交「批次 API 完成」，我驗 worker 層會全綠，
然後把一個線上零改善的東西當成修好了——**「修好了」與「沒修」在線上會共用同一個輸出**。

dispatcher 獨立驗證它的宣稱：`git diff --name-only -- app/api/crawler_routes.py` 空
（控制組對 `download_worker.py` 非空）；`grep enqueue_many` 只在 worker 自己內部命中
（控制組舊 API 在 route 有兩處呼叫 rc=0）。坐實。

### 接線（dispatcher 自接，`2391ae0`）

`src:136-160` 的 for 迴圈改為一次 `enqueue_many`。**`if item.md5` 的過濾刻意保留**：
現行行為靜默略過缺 md5 的項，而 `enqueue_many` 缺 md5 拋 `ValueError`；
在 route 端過濾讓本次改動**只改成本不改行為**。

新增 `tests/test_batch_download_route_uses_batch_api.py`（4 條），判準用**存檔次數不用計時**
——時間會浮動，「機器今天比較快」與「改成 O(N) 了」共用同一個輸出。

runtime 驗證（不只驗磁碟檔）：容器內 `inspect.getsource(enqueue_batch_download)`
→ `enqueue_many` 在、舊 for 迴圈不在、控制組 bogus 不在。**FIXED-UNDEPLOYED 轉 FIXED。**

### dispatcher 自陳的兩個量測缺陷

1. **第一版效能探針用了不存在的 `data_dir=` 參數** → `TypeError` rc=1。
   至少它**大聲失敗**，沒有靜默給出令人安心的數字。
2. **第一版 mutation A 死 4 條而非 handler 報的 2 條**——不是它報錯，是**我把「移動」做成「新增」**。
   `enqueue()` 已委派給 `enqueue_many()`，多加一行會讓逐筆路徑每筆存 2 次，控制組跟著死。
   **兩個不同的 patch 共用 `mutation A` 這個名字——本族同一失效類別第五次。**
   指紋救了這格：`save calls 8→8` vs 我的版本會變 9。
3. **route 改動的第一版 `assert` 因自己寫的 docstring 命中 pattern 而誤報**
   （`if item.md5` raw 計數=2，code-only=1）。這是 handler K 踩過的同一形狀
   （它是 `dao.current_iso()` 出現在註解裡）。改用 `tokenize` 剝掉 comment/STRING 才分辨得出。

### 遺留（不在本包，已記）

- **`delete_download_job` 的 `part_file.unlink()`**（`download_worker.py src:356`）
  數百 MB `.part` 在 NFS 上 unlink 仍會佔住 loop。handler K 標出、handler I 未動。
- **缺 md5 靜默略過**：送 N 筆回 N-1 筆沒有任何訊號。handler I 傾向改成出聲，
  dispatcher 同意那是真缺陷，但**會影響既有前端，需使用者決策**，故本包維持現狀並用測試鎖住。

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
