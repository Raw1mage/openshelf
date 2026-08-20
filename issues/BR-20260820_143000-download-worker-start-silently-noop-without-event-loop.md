# BR-20260820_143000 — DownloadWorker.start() 在無 event loop 時靜默 no-op，有 loop 時真的打公網

Status: OPEN
Owner: ses_fe7b5cbadffeSlxj0dv1Z740O4（值星官）
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
5. 完整 pytest 不得下降（當前基線 **150 passed**，`.venv/bin/python -m pytest`）。

## 下一個 session 的 checklist

- [ ] `.venv/bin/python -m pytest` 取基線，`rc` 獨立一行取，**不接管線**（`cmd | tail` 會取到 tail 的 `$?`）
- [ ] 讀 `app/crawler/download_worker.py:100-150`，確認 `start()` 與 `_load_jobs_from_disk()` 現況
- [ ] 修 `start()` 兩態可區分 + `_load_jobs_from_disk()` 的靜默 except
- [ ] 兩個方向的測試（見判準 2）
- [ ] 不得動 `app/db/`、`app/static/`、`plans/`、`requirements.txt`、`Dockerfile`

## 沒驗證的

- **未量 production runtime 下 log level 是否真的會輸出 warning**。app 的 logging 設定未查。
  同一格在 BR-124320 的 Q5 也是未驗證面。
- **未確認是否有其他呼叫點依賴「無 loop 時靜默 pass」這個行為**。改成 warning 前需 grep `\.start()` 全域。
