# BR-20260820_143000 — DownloadWorker.start() 在無 event loop 時靜默 no-op，有 loop 時真的打公網

Status: **PARTIAL** — 主修復已落地並驗收（`d59dd8d`），但**判準 4 仍未關閉**（見下方殘留節）。
        依本 repo 規約，帶 scoped remainder 的 BR 留頂層，不進 `closed/`。
Owner: ses_fe7b5cbadffeSlxj0dv1Z740O4（值星官）
Fixed-by: `d59dd8d` — handler ses_fe18eab55ffeEQU8vBGV8DrmVd，值星官獨立驗收（2026-08-20）
Family: download-worker-lifecycle
Filed: 2026-08-20 by ses_fe7b5cbadffeSlxj0dv1Z740O4
Reported-by: handler ses_fe27556c4ffeWZLm2DnDItEhNf（修 BR-131500 時撿到，`issues/` 在其禁區故未自行建檔）

**Related**:
- `BR-20260820_111523-mirror-resolver-dead-mirrors`（closed，`16890d7`）— **同一失效類別**：
  「缺席態與失敗態共用同一個輸出」。該案在鏡像健康層（查封頁與缺席頁都回 200），
  本案在背景工作啟動層（沒有 loop 所以不啟動、與啟動失敗，都回同一個靜默 pass）。
- `BR-20260820_131500-download-path-cannot-carry-publication-year`（closed，`478a9e2`）—
  **時序因果**：本缺陷是在修該 BR、為五層改動補測試時被踩到的，
  症狀是 pytest 120 秒 timeout 被砍且零輸出。同一支 `enqueue()` 是兩案的共同執行路徑。

## 一句話

`DownloadWorker.start()` 用 `except RuntimeError: pass` 吞掉「當下沒有 running event loop」，
而 `enqueue()` 尾端**無條件**呼叫它。於是同一支 `enqueue()` 在兩種環境下行為完全相反，
**且兩邊都不出聲**：一般 pytest 下什麼都不做，`TestClient`（有 loop）下會真的開始打公網鏡像下載檔案。

## 現場

`app/crawler/download_worker.py:132-138`

```python
def start(self):
    """啟動背景 Worker 監聽循環。"""
    try:
        loop = asyncio.get_running_loop()
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = loop.create_task(self._process_queue())
    except RuntimeError:
        pass          # ← 「沒有 loop」與「啟動失敗」共用這一個輸出
```

同檔 `:129-130` 的 `_load_jobs_from_disk()` 尾端也有一組 `except Exception: pass`，
是同一種病在同一個檔案裡的第二個實例（本 BR 一併記錄，未修）。

## 證據（handler 的隔離實驗，逐段分組排除）

```
GROUP A（純 DownloadJob，無 worker）        rc=0    7 passed
GROUP B（jobs.json 載入，有 worker）        rc=0    2 passed
GROUP C（HTTP TestClient）                  rc=124  ← hang 在這
第 13 條（enqueue，無 running loop）        rc=0    1 passed   ← 對照組
```

分辨「app 啟動 hang」與「enqueue 觸發真下載」的決定性一步：

```
TestClient(app) 單獨探測 → rc=1（不是 124）
  sqlite3.OperationalError: attempt to write a readonly database
```

**rc=1 ≠ rc=124 ⇒ hang 不是 app 啟動造成的**，而是 `enqueue()` → `start()` → `_process_queue()`
在有 loop 的環境下真的去下載了。

## 為什麼這格重要

1. **任何在 async context 下呼叫 `enqueue` 的測試都會踩到**，而症狀是 **timeout 不是 error**——
   看起來像「測試寫錯了」，實際上是 production code 在測試環境真的發出對外請求。
2. **無 loop 那條路徑同樣危險**：若未來有人在同步 context 呼叫 `enqueue()` 期待任務會開始跑，
   它會安靜地什麼都不做，任務永遠停在 `queued`。沒有任何訊號。

## 目前的處置（非修復）

handler 只在測試 fixture 裡 `monkeypatch.setattr(worker, "start", lambda: None)` 繞過，
**production code 一個字未動**。所以缺陷仍在。

## 驗收判準

1. `start()` 的兩態必須可區分：無 loop → 明確 `log.warning`（帶「背景工作未啟動，任務將停留在 queued」語意）；
   有 loop 且已在跑 → 正常 no-op（可靜默）。**不得共用同一個輸出。**
2. 測試需兩個方向：無 loop 呼叫 → 斷言**有** warning；有 loop 呼叫 → 斷言**無** warning 且 task 被建立。
   只測一邊的話，一個恆記 warning 或恆不記的實作都會通過。
3. `_load_jobs_from_disk()` 的 `except Exception: pass` 一併改為帶例外型別與訊息的 `log.warning`，
   **回退行為不得改變**（載入失敗仍不阻斷啟動）。
4. 需有一條測試證明「在 async context 下 enqueue **不會**真的發出對外網路請求」——
   或明確記錄這是 by-design 並提供 opt-out。此格若無解，至少要在 `enqueue()` docstring 寫明副作用。
5. 完整 pytest 不得下降（~~當前基線 **150 passed**~~ — **實測為 155 passed**，
   該舊值於 2026-08-20 被 handler 推翻、值星官獨立重跑坐實；`.venv/bin/python -m pytest`）。

## 下一個 session 的 checklist

- [ ] `.venv/bin/python -m pytest` 取基線，`rc` 獨立一行取，**不接管線**（`cmd | tail` 會取到 tail 的 `$?`）
- [ ] 讀 `app/crawler/download_worker.py:100-150`，確認 `start()` 與 `_load_jobs_from_disk()` 現況
- [ ] 修 `start()` 兩態可區分 + `_load_jobs_from_disk()` 的靜默 except
- [ ] 兩個方向的測試（見判準 2）
- [ ] 不得動 `app/db/`、`app/static/`、`plans/`、`requirements.txt`、`Dockerfile`

## 沒驗證的

✅ **下列兩格已於 2026-08-20 補齊（值星官派的 explore 勘查員 + 值星官抽驗），不再是未知：**

- **log level 確實會輸出**。載入 `uvicorn.config.LOGGING_CONFIG` 後，
  `logging.getLogger("app.crawler.download_worker")` 的 effective level = `WARNING`、`propagate=True`、
  無自有 handler；`lg.warning(...)` 實測落到 stderr（`captured_stderr='PROBE_WARNING_VISIBLE\n'`）。
  專案本身無任何 `basicConfig` / `dictConfig`（grep rc=1，控制組 `getLogger` 在 `dao.py` 命中）。
  ⇒ **判準 1 的 `log.warning` 在 uvicorn 下看得到。**
- **全域 `.start()` 命中 3 處，沒有任何一處依賴「靜默 pass」語意**（值星官獨立重跑）：
  ```
  app/main.py:27                  worker.start()
  app/crawler/download_worker.py:169   self.start()   # enqueue 尾端，無條件
  app/crawler/download_worker.py:188   self.start()   # start_job 的 except RuntimeError 分支
  ```
  ⇒ **改成 warning 安全。**

## 補充現場（2026-08-20 重驗，行號已位移）

BR 上方引的是 `:132-138`，**當前工作樹是 `:133-140`**（差 1 行，內容逐字相同）。
`git diff --stat HEAD -- app/crawler/download_worker.py` 無輸出、rc=0；該檔最後一次被改是 `478a9e2`。

**兩態實測（直呼 `DownloadWorker.start`，未實例化、未碰磁碟）**：

```
NOLOOP: return=None worker_task=None exception=None warnings=0 logs=''
LOOP  : return=None worker_task_type=Task created=True logs='Using selector: EpollSelector\n'
```

兩態的**回傳值相同（都是 `None`）**，唯一差別是 `_worker_task` 這個私有屬性——
呼叫端拿不到任何區分訊號。

**控制組**：
1. `asyncio.get_running_loop()` 無 loop 時確實 raise `RuntimeError: no running event loop`；
   有 loop 時回 `_UnixSelectorEventLoop` ⇒ 證明 `except RuntimeError` 抓的就是這條。
2. logging 通道有鑑別力：同一組 handler 在 LOOP 分支錄到 `'Using selector: EpollSelector'`。
   **buffer 不是壞的，NOLOOP 的 `''` 是真的沒有任何輸出。**
3. `grep -c "log.warning\|logger.warning"` 對 `download_worker.py` = **0**（rc=1）；
   同 pattern 對 `dao.py` = **3**（rc=0）⇒ 專案確實有 warning 慣例，該檔一行都沒有是**缺席**。

**繞過措施仍在**：`tests/test_download_path_year.py:214`
`monkeypatch.setattr(worker, "start", lambda: None)`，docstring（`:204-207`）明寫
「TestClient 底下有 running loop，於是會 create_task 真的去打公網鏡像——測試會靜默挂住
（零輸出，與『跑很久』無法區分）」。production code 一個字未動。

**仍未量的**：
- 未重現 GROUP C 的 rc=124 hang（未執行會打公網的測試）。那組數據仍是原始回報的轉述。
- `download_worker.py:92-93` `_save_jobs_to_disk` 尾端另有同型 `except Exception: pass`（本 BR 原未記）。

---

## 殘留節（PARTIAL 的理由，2026-08-20 值星官驗收後寫入）

### 已關閉的（`d59dd8d`，值星官獨立驗收）

判準 1、2、3、5 全數達成：

- **判準 1**（兩態可區分）— `start()` `:146-164`，`try` 縮到只包 `asyncio.get_running_loop()`，
  `except RuntimeError` 改 `log.warning` + `return`。訊息明寫後果與例外型別。
- **判準 2**（兩個方向的測試）— `tests/test_download_worker_start_lifecycle.py`，8 條，
  每個「必須出聲」都配一條「必須靜默」的控制組。
- **判準 3**（`_load_jobs_from_disk` 的靜默 except）— 已改，回退行為一字未改。
  **順帶處理了本 BR 原未記的第三個實例** `:95-101` `_save_jobs_to_disk`。
- **判準 5**（pytest 不下降）— 155 → 163，零下降。

**值星官獨立驗收數據**（未採信 handler 自報）：

```
基線 --ignore 新測試檔        155 passed  rc=0
  CONTROL --ignore 指向不存在的檔  163 passed   ← 證明 --ignore 真的在濾，非空轉
完整                          163 passed  rc=0   （155 + 8）

MUTATION 把 start() 改回 except RuntimeError: return
  → 2 failed / 6 passed  rc=1
    死的正是 test_start_without_event_loop_warns
            test_enqueue_without_event_loop_warns
  ⇒ 測試真的鎖得住，不是恆綠

還原 diff rc=0；CONTROL diff 對 mutant 副本 rc=1   ← 證明 diff 有鑑別力
MUTANT 標記已清 rc=1；CONTROL 同 grep 對副本 rc=0
還原後完整                    163 passed  rc=0
禁區 12 個路徑全空；CONTROL 同語法對已改檔非空
```

**handler 額外收窄的一格（本 BR 原未記）**：原寫法把 `loop.create_task(...)` 也包在同一個
`try` 裡，所以 `create_task` 自己拋的 `RuntimeError`（例如 loop 已關閉）會被同一個 `except`
一起吞掉。**這個 `except` 同時吃掉了第二種真失敗**，本 BR 只描述了「無 loop」那一態。
現已收窄，該條會正常往上拋。

### ⚠ 未關閉的 —— 判準 4（本 BR 留頂層的唯一理由）

> **判準 4 原文**：需有一條測試證明「在 async context 下 enqueue **不會**真的發出對外網路
> 請求」——或明確記錄這是 by-design 並提供 opt-out。

**現況：只做了最低替代**，即在 `enqueue()` docstring 揭露副作用（`:175-186`）。
**缺陷本身仍在**：`enqueue()` 尾端無條件呼叫 `self.start()`，在有 running loop 的環境
（含 `TestClient`）下會立刻 `create_task(_process_queue())` 並開始對公網鏡像發出真實 HTTP
請求。目前**沒有 opt-out 參數**。

**為何本包沒做**：關閉這格需要動 `enqueue()` 的語意或新增 opt-out 參數（例如
`autostart: bool = True`），那是**產品決策不是實作細節**——派工單已明列為範圍外，
handler 依指示未自行決定。

**下一包的選項（需使用者拍板）**：

| | 做法 | 代價 |
|---|---|---|
| A | `enqueue(..., autostart: bool = True)`，測試傳 `False` | 公開 API 增加一個參數；既有呼叫端不受影響 |
| B | 把 `start()` 從 `enqueue()` 尾端移除，改由呼叫端顯式啟動 | 破壞既有語意，三個呼叫端都要改（`main.py:27`、`:169`、`:188`） |
| C | 維持現狀，只靠 docstring 揭露 | 缺陷留著；任何在 async context 下呼叫 `enqueue` 的測試仍會踩到，且症狀是 timeout 不是 error |

**現行繞過措施仍是必要的、不得移除**：`tests/test_download_path_year.py:214`
`monkeypatch.setattr(worker, "start", lambda: None)`。它擋的是「有 loop 時真的去下載」
這條路徑——本修復完全沒碰它（本修復只讓「無 loop 時」出聲），**所以它沒有變成多餘**。

### 歸檔條件

判準 4 以 A / B / C 任一方式關閉後，本 BR 方可 `git mv` 進 `issues/closed/`。
在那之前維持 **PARTIAL** 留頂層——**修好一半就歸檔，會讓「已驗證的部分」與
「未涵蓋的部分」共用同一個狀態**，而那正是本 BR 記載的失效類別本身。
