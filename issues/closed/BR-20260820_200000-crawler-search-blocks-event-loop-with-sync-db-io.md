# BR-20260820_200000 — `/api/crawler/search` 在 `async def` 內做同步 DB I/O，爬蟲期間阻塞整個事件迴圈

Status: OPEN
Owner: ses_fe7b5cbadffeSlxj0dv1Z740O4（值星官）
Family: async-blocking-io
Filed: 2026-08-20 by ses_fe7b5cbadffeSlxj0dv1Z740O4
Found-during: handler `ses_fe1e42061ffelY9nWWmlTw3GdZ` 追 BR-124500 後端延遲時的過程發現，
  值星官獨立複核 file:line 後建檔

**Related**:
- `BR-20260820_124500-quick-collections-modal-blocking`（PARTIAL）—
  症狀相鄰但**根因不同**。該案是 DB 層鎖爭用，本案是**事件迴圈層**。
  兩者的分水嶺就是本 repo 一直在用的那個判準：
  不碰 DB 的端點（404）也慢 ⇒ 事件迴圈；只有碰 DB 的慢 ⇒ DB 層。
  **本案是前者。**
- `73de0aa`（引導快取修復）— **大幅減輕但未消除本案**。
  `CatalogDAO()` 建構從 232ms 降到 0.1ms，25 次迴圈從 ~5.8s 降到 ~2.5ms，
  但「在事件迴圈執行緒上做同步 SQLite I/O」這個結構問題原封不動。

## 一句話

`app/api/crawler_routes.py:47` 的 `live_search` 宣告為 `async def`，
但函式體內 `:60` 對公網爬蟲回傳的**每一筆結果**同步呼叫 `dao.find_work_by_hash(md5)`。
`async def` 路由跑在**事件迴圈執行緒**上，任何同步阻塞都會卡住**整個 process 的所有請求**——
包括那些完全不碰 DB 的端點。

## 證據

### 程式碼事實

```
app/api/crawler_routes.py:47   async def live_search(...)
app/api/crawler_routes.py:60       local_wid = dao.find_work_by_hash(md5)   ← 同步 DB I/O
                                   （在 :57 的 for item in raw_results 迴圈內）

grep -n 'run_in_threadpool\|to_thread' app/api/crawler_routes.py
  → rc=1（完全沒有）

CONTROL  app/api/*.py 全域計數
  async def = 14      同步 def = 32      ← 證明 grep 有鑑別力，
                                            且本 repo 確實混用兩種寫法
```

FastAPI 的分派規則：**`def` 路由自動丟進 threadpool，`async def` 路由直接在事件迴圈上跑。**
本檔 11 個路由全部宣告為 `async def`（`:47/:85/:105/:131/:137/:146/:153/:162/:171/:181/:190`），
其中只有 `:60` 這一處碰 `dao` —— 也就是說**只有 `live_search` 有這個缺陷**，範圍是收斂的。

### 實測：不碰 DB 的端點也被拖慢

handler 在建分水嶺儀器時量到（`73de0aa` 尚未生效的條件下）：

```
爬蟲在飛時（/api/crawler/search 實測 3.59s，25 筆結果）
  L0_404      total=1.296s    ← ★ 這個端點根本不進任何 route
  L1_health   total=0.002s       同刻正常
```

**`L0_404` 是一個不存在的路徑，不碰 DB、不進 route handler。**
它慢到 1.296s 只可能有一個解釋：**事件迴圈本身被佔住了。**

> ### ⚠ 上面這段括號解釋已被實測推翻（2026-08-20，commit `2e7d665` 驗收時）
>
> 原文寫「`L1_health` 同刻正常，**因為**它是 `def` 而非 `async def`，走 threadpool」。
> **那個「因為」不成立——`def` 路由並不免疫事件迴圈阻塞。**
>
> 由 handler `ses_fe1b0e9ccffe2E7ROmNmwyZbJH` 提出，值星官以**隔離 FastAPI 探針**
> 獨立坐實（純 FastAPI，不碰 openshelf 程式碼，只測 ASGI 分派行為本身）：
>
> ```
> async def blocker(): time.sleep(1.5)      ← 佔住事件迴圈
> def sync_route():    return {"ok": True}  ← FastAPI 丟 threadpool
>
> BASELINE（無阻塞）
>   /sync_route      25.8 ms  kind=ok
>   /notfound_404     1.0 ms  kind=http404
> CONTROL 直接打 blocker  1501.6 ms         ← 證明探針有鑑別力
>
> 阻塞期間同刻取樣
>   /sync_route    1252.4 ms  kind=ok        BLOCKED
>   /notfound_404  1251.4 ms  kind=http404   BLOCKED
>
> VERDICT_def_route_immune = False
> VERDICT_404_immune       = False
> ```
>
> **機制**：「走 threadpool」講的是**函式體**在 threadpool 執行；
> 但請求要先經過事件迴圈完成 ASGI 接收、路由匹配、以及把工作**派送**進 threadpool。
> 事件迴圈被同步程式碼佔住時，這個派送根本輪不到——**請求卡在進入 threadpool 之前**。
>
> **對上方那組舊數據的處置**：`L0_404=1.296s` 與 `L1_health=0.002s` 的分離無法在
> 隔離儀器中重現。不宣稱該組數據為假（條件不同：真公網爬蟲、`73de0aa` 前、
> 原始儀器已不存在），但**它不能支撐「`def` 路由免疫」這個推論**。
>
> **實務後果：影響面比本 BR 原本估計的更大**——連 `def` 路由與根本不進 route 的
> 404 都被拖慢，所以修好的收益也更大。
>
> **值星官的自我記錄**：驗證這格時，我第一版探針用 `-1.0` 當失敗值，於是
> 「404 免疫」與「404 根本沒量到」共用同一個輸出，**而那個假結論恰好支持我原本的假說**。
> 改成三態（`ok` / `http404` / `connfail`）並加控制組後才拿到真數字。
> **這是本包第二次在觀察者自己的儀器上踩到本 repo 的核心判準。**

## 為什麼這格重要

1. **使用者可感知，且症狀會誤導**：公網檢索期間**全站卡頓**——
   書單、搜尋、任何頁面都變慢，而使用者只是在做一件事（搜尋公網）。
   看起來像「整個系統很慢」，實際是單一路由佔住事件迴圈。
2. **會隨資料量惡化**：迴圈次數 = 爬蟲回傳筆數。目前 25 筆，
   若未來提高每頁筆數或多鏡像聚合，阻塞時間線性成長。
3. **`73de0aa` 讓它變得更難發現**：修復後單次 `CatalogDAO()` 只要 0.1ms，
   總阻塞從 ~5.8s 降到 ~2.5ms —— **症狀幾乎消失但結構缺陷還在**。
   一旦 DB 變慢（見 BR-160000 的 NFS 尖峰）或筆數變多，它會再次浮現，
   而下一個人會從零開始查。這正是「修掉症狀、留下病灶」的形狀。

## 修復方向（不是規定，handler 可推翻）

三個候選，各有取捨：

| | 做法 | 取捨 |
|---|---|---|
| **1** | `async def live_search` → `def live_search` | 最小改動。FastAPI 自動丟 threadpool。但 `:56` 有 `await crawler.search(q)`，改 `def` 後這個 await 無處可放 |
| **2** | 保留 `async def`，把整個標註迴圈包進 `run_in_threadpool()` | 精準。await 保留在 async 層，同步 DB 工作丟 threadpool |
| **3** | 一次查詢取代 N 次查詢 | `find_work_by_hash` 改成批次版本（`WHERE hash IN (...)`），順帶把 25 次 DB 往返降成 1 次 |

**2 與 3 可疊加，且 3 獨立於本 BR 也有價值。** 由 handler 判斷。

## 驗收判準

1. **必須證明事件迴圈不再被阻塞**，且該證明要有鑑別力：
   - 在 `/api/crawler/search` 飛行期間，同刻打 **`L0_404`**（不存在的路徑，不碰 DB、不進 route）。
   - **修復前後同條件對照**：修復前必須能重現 `L0_404` 的尖峰，
     修復後該尖峰消失。**只量修復後不算**——「沒量到」與「不存在」共用同一個輸出。
   - 若修復前重現不出來（`73de0aa` 已大幅減輕），**必須人為放大**：
     例如暫時讓 `find_work_by_hash` sleep，或把結果筆數拉高，
     製造出可觀測的阻塞後再驗修復。**重現不了就直說，不要交一份「修復後很快」當證據。**
2. ~~**`L1_health`（`def`，走 threadpool）需同刻取樣當對照組。**
   若修復前後 `L1_health` 都正常，而 `L0_404` 從慢變快，才證明改動命中的是事件迴圈層。~~

   **⚠ 本條已作廢（2026-08-20）。** 它建立在「`def` 路由免疫事件迴圈阻塞」這個
   **已被實測推翻**的前提上（見上方「已被實測推翻」方塊）。
   照字面執行會把真實有效的修復判為不合格——**實測中 `L1_health` 與 `L0_404`
   的 max 逐毫秒相同**（1.267/1.267、1.261/1.261、0.066/0.066、0.040/0.040）。

   **正確的判準**：`L0_404` 與 `L1_health` **都應該**在修復前變慢、修復後變快。
   兩者一起改善才是命中事件迴圈層的證據；若只有其中一個改善，反而要追問為什麼。
   真正的對照維度不是「哪個路由免疫」，而是**修復前後的同條件對照**——
   前提是修復前必須真的能重現尖峰（見判準 1）。
3. 功能不得回歸：`/api/crawler/search` 仍需正確標註 `availability_tier`、
   `local_work_id`、`queue_status` 等欄位。需有一筆**已在本地落地**的書
   與一筆**未落地**的書同時出現在結果中的實測（否則恆真/恆假的實作也會通過）。
4. `pytest` 不得下降（當前基線 **150 passed**，`.venv/bin/python -m pytest`）。
   `rc` 需獨立一行取，**不得接管線**。
5. 若採候選 3（批次查詢），需證明批次版本與逐筆版本結果一致，
   且需涵蓋 **空清單** 與 **全部未命中** 兩個邊界。

## Boundaries（授權邊界）

**可碰**：
```
app/api/crawler_routes.py
app/db/dao.py            （若採候選 3，需加批次查詢方法）
tests/                   （新增測試）
```

**禁區**：
```
app/static/             ← 另一顆 handler 正在改 app.js，一個字都不要碰
app/db/engine.py        ← 73de0aa 剛改過，且 BR-160000 待決
app/db/schema.sql
app/models/
app/pipeline/
app/crawler/
issues/  plans/  docs/
requirements.txt  Dockerfile  docker-compose.yml  extension/
```

**特別注意**：`app/static/js/app.js` 由另一顆 handler（`escapeHtml` 改名包）持有，
**檔案集已證明不交集**，請勿越界。

## 環境事實（會咬人的）

- `docker compose` 的 service 名是 **`openshelf`**，不是 `app`。
  叫錯回 rc=1，**與「grep 沒找到」共用同一個退出碼**。
- **bind-mount + `--reload`**：`./app:/app/app`，你每次存檔就是線上程式碼。
  **import 與使用它的 code 必須同一次寫入落地**，否則 `--reload` 會抓到瞬態 `NameError`
  並讓線上服務短暫 500（上一顆 handler 踩過）。
- pytest 必須用 **`.venv/bin/python -m pytest`**；系統 `python3` 缺 `fitz`，
  會噴 9 errors during collection。
- 線上 DB 在容器內 `/data/db/openshelf.sqlite`（80MB、37 筆 work）。
  repo 內的 `data/db/openshelf.sqlite` **不是那顆**（114KB、`work` 表 0 rows）。
  端到端一律打容器 API：`http://127.0.0.1:8088/...`。
- `.specbase/events.sqlite` 恆為 M，是背景程序寫的，**永不納入你的範圍證據**。

## 判準（整段適用，違反即退回）

**缺席態與失敗態不得共用同一個輸出。**

每個 grep / 測試 / 量測都要帶**控制組**證明工具有鑑別力——
回空同時是「沒有」與「pattern 打錯」的答案。

**嚴禁用 `|` 管線取 `$?`**（會取到管線末端指令的退出碼）。
值星官本人踩過這個坑：`grep -n "publisher" schema.sql | head -5` 的 `rc=0`
是 `head` 的退出碼，導致在 BR 裡寫下一個不存在的欄位。
多條件驗證一律**分行獨立執行**。

**推翻我是本包的合法產出。** 上面的修復方向、根因判斷、甚至 `L0_404` 那格證據，
若你有反證，附證據推翻它——那比照做更有價值。

## 沒驗證的

- **未在 `73de0aa` 生效後重新量 `L0_404` 尖峰。** 那組 1.296s 是修復前的數據。
  修復後是否仍可重現，**未量**——這是本包第一個要回答的問題。
- **未量 `find_work_by_hash` 單次耗時**（只知道 `CatalogDAO()` 建構從 232ms → 0.1ms）。
- **未確認其他 10 個 `async def` 路由是否有間接的同步 I/O**（只 grep 了 `dao.`；
  `worker.jobs.values()` 是記憶體操作，但 `worker` 的其他方法未查）。
