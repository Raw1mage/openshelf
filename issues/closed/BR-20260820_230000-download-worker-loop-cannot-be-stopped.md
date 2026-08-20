# BR-20260820_230000 — DownloadWorker 背景迴圈無法被關閉：吞取消 + 無 shutdown 路徑

Status: **CLOSED** — 六條判準全數達成。修復 `19781d4`，dispatcher 獨立驗收（四組 mutation + 判準 6 容器實測）。
Closed: 2026-08-20 by ses_fe7b5cbadffeSlxj0dv1Z740O4
Owner: ses_fe7b5cbadffeSlxj0dv1Z740O4（值星官）
Family: download-worker-lifecycle
Filed: 2026-08-20 by ses_fe7b5cbadffeSlxj0dv1Z740O4
Found-during: 驗收 `3119fe4`（BR-143000 判準 4）時，handler 回報其一半，值星官獨立勘查補上另一半

**Related**:
- `BR-20260820_143000-download-worker-start-silently-noop-without-event-loop`（PARTIAL → 判準 4 已由 `3119fe4` 關閉）
  — **同一個檔案、同一族**。該案處理的是「背景迴圈該啟動而沒啟動時不出聲」，
  本案是它的鏡像：**「背景迴圈啟動之後關不掉」**。兩案合起來才涵蓋 `_worker_task` 的完整生命週期。
- **發現路徑**：handler `ses_fe15f2ba4ffenRsWHzMk15DmRs` 在為判準 4 寫測試時，
  第一版用天真的 `task.cancel()` + `await task` 收尾而**逾時被砍**（rc=124），
  逐條隔離後定位到吞取消。它交回這格但未建檔（位置在其禁區外，且它不確定我要不要當缺陷處理）。
  值星官獨立驗證時發現**還有第二半**（無 shutdown 路徑），兩者合併為本 BR。

---

## 一句話

`DownloadWorker._worker_task` 一旦啟動就**沒有任何機制能停下它**——`_process_queue()` 吞掉
`CancelledError` 後回到迴圈頂端，而 `lifespan` 只有 `start()` 沒有對應的 shutdown，
`DownloadWorker` 也**根本沒有 stop / shutdown / close 方法**。

---

## 證據

### ① `_process_queue()` 吞取消（`app/crawler/download_worker.py:321-345`）

```python
async def _process_queue(self):
    while True:                                    # :323
        job = await self.queue.get()
        ...
        try:
            await self._execute_download_with_resume(job)
        except asyncio.CancelledError:             # :334  ← ★
            # 任務被手動暫停或刪除
            if job.job_id in self.jobs and job.status != "paused":
                job.status = "paused"
                job.error_message = "已暫停"
        except Exception as e:
            ...
```

`except asyncio.CancelledError` **不 re-raise**，於是取消被消化掉，控制流回到 `while True` 頂端，
停在 `await self.queue.get()` 等下一個 job。**單次 `task.cancel()` 無法結束這個迴圈。**

**這個 except 本身不是錯的**——它的意圖是「使用者按了暫停/刪除」，那時取消的是
`_execute_download_with_resume` 這個內層 await，語意正確。**錯的是它無法區分兩種取消**：

| 取消來源 | 期望行為 | 實際行為 |
|---|---|---|
| `pause_job` / `delete_job` 取消單一 job | 標記 paused，**繼續**跑迴圈 | ✅ 正確 |
| 關閉整個 worker（shutdown / 測試收尾） | **結束**迴圈 | ❌ 被當成暫停，迴圈繼續 |

**兩種取消共用同一個輸入訊號**（都是 `CancelledError`），而處理端只實作了其中一種的語意。

### ② 佇列空 vs 不空，行為完全不同（這是它一直沒被發現的原因）

```
佇列空    _process_queue 停在 await self.queue.get()
          → cancel 直接穿透，task 正常結束            ← 既有測試都在這個狀態
佇列不空  _process_queue 在 await _execute_download...
          → cancel 被 :334 吞掉，回到 while 頂端      ← 永遠關不掉
```

既有的 `tests/test_download_worker_start_lifecycle.py`（上一包新增）用的是**空佇列**收尾，
所以完全正常。缺陷只在「enqueue 之後再想關掉」時才浮現——那正是 `3119fe4` 那包的場景。

**實測**（handler 的第一版測試）：

```
bash tool terminated command after exceeding timeout 120000 ms
逐條隔離 → test_tripwire_actually_detects_outbound_http rc=124
```

### ③ 沒有任何 shutdown 路徑（值星官獨立勘查，handler 未提）

```
app/main.py:20-28   lifespan：
                      worker = get_worker()
                      worker.start()
                      yield
                                      ← yield 之後什麼都沒有

grep -n 'def stop\|def shutdown\|def close' app/crawler/download_worker.py
  → rc=1（不存在）
  CONTROL: grep -c 'def ' 同一檔案 = 19    ← 證明 pattern 有鑑別力，不是 grep 壞掉

grep -rn '_worker_task' app/ --include='*.py'
  → :78 宣告 / :163 檢查 / :164 建立 / :187 docstring
    全部落在 download_worker.py 內，外部零引用
  CONTROL: grep -rn '_zzz_no_such_attr' → rc=1

DownloadWorker() 單例    app/api/crawler_routes.py:25
```

**即使呼叫端想關，它也沒有可呼叫的方法。**

### ④ 兩半是同一個缺陷

```
只修 ①（不吞 shutdown 的取消）  →  仍然沒有人會去取消它
只修 ③（加 stop() + lifespan）  →  呼叫 cancel 之後仍然關不掉
```

必須一起修，否則任一半都不會產生可觀察的行為改變——**「修好了」與「沒修」共用同一個輸出**。

---

## 為什麼這格重要

1. **uvicorn 優雅關閉會被拖住或直接被砍。** 目前靠 `--reload` 與容器重啟強制終止，
   下載中的 job 沒有機會落盤標記狀態。`docker compose stop` 的預設 10s 寬限期到了就是 SIGKILL。
2. **測試層已經實際受害。** 任何「enqueue 後想收乾淨」的測試都得自己實作
   `_hard_cancel`（反覆 cancel 直到 `done()`）。`3119fe4` 已在
   `tests/test_download_worker_enqueue_autostart.py:84-121` 留下這個 workaround 與完整註解，
   但那是**測試層繞道，不是修復**。
3. **失效形狀是逾時，不是錯誤。** rc=124 與「機器忙」「pytest 掛了」「網路慢」共用同一個輸出。
   handler 的第一版測試就是這樣被誤導的——**證明「兩態不得共用同一個輸出」的測試，
   自己踩了同一個病**。這是本 repo 第 N 次遇到同一個失效類別，且這次踩到的是為它寫的測試本身。

---

## 修復方向（未定案，需實作時判斷）

**① 區分兩種取消。** 候選做法：

| | 做法 | 代價 |
|---|---|---|
| A | 加 `self._stopping: bool` 旗標，`:334` 的 except 內檢查，為真則 `raise` | 最小改動；但旗標與取消是兩個獨立訊號，可能不同步 |
| B | 只在**內層** await 包 try（取消 `_execute_download_with_resume` 用單獨的 task），外層迴圈的取消自然穿透 | 語意最乾淨；改動較大 |
| C | `except CancelledError` 內判斷 `asyncio.current_task().cancelling()`（Py 3.11+） | 標準做法；需確認 Python 版本 |

**② 加 `async def stop()`** — 取消 `_worker_task`、等它真的結束、標記進行中的 job 狀態、落盤。

**③ `lifespan` 的 `yield` 之後呼叫 `await worker.stop()`。**

---

## 驗收判準

1. **必須有一條測試證明「enqueue 之後 stop() 能真的關掉」**，且該測試在修復前會失敗。
   **失敗必須以斷言呈現，不得以逾時（rc=124）呈現**——逾時與環境問題共用同一個輸出。
   建議做法：測試自帶 `asyncio.wait_for(..., timeout=N)` 把逾時轉成明確的斷言失敗。

2. **必須證明「暫停單一 job」的既有語意未被破壞。** 這是本修復最容易誤傷的一格：
   `:334` 的 except 存在是有理由的。需要一條測試覆蓋
   「job 進行中 → `pause_job` → 該 job 標記 paused **且迴圈仍在跑**」。
   **只測 stop 能關掉而不測 pause 仍正常，會讓「取消一律穿透」的壞實作通過。**

3. **mutation 證明測試鎖得住**：把修復還原成吞取消，上述測試必須死，且**以斷言失敗呈現**。

4. **`tests/test_download_worker_enqueue_autostart.py` 的 `_hard_cancel` 應可簡化或移除。**
   若修復到位，那個 workaround 就是多餘的——**它是否能拿掉，本身就是修復是否真的生效的檢驗**。
   若拿不掉，說明缺陷還在。

5. `pytest` 不得下降（**當前基線 170 passed**，`.venv/bin/python -m pytest`，rc 獨立一行取）。

6. **需實測容器優雅關閉**：`docker compose stop openshelf` 應在寬限期內正常結束，
   而非等到 SIGKILL。這格需要進行中的 job 才有意義——
   **在空佇列狀態下測會白白通過**（見證據 ②）。

---

## 沒驗證的

---

## 處置紀錄

**修復 `19781d4`** — handler `ses_fe1449648ffeVlh7BM0VFmG40q`，
dispatcher `ses_fe7b5cbadffeSlxj0dv1Z740O4` 獨立驗收。

### 六條判準

```
1 修復前失敗且以斷言呈現       ✅  mutation C：4 failed rc=1
                                   CONTROL 'Failed:'=4 rc=0 / 'AssertionError'=0 rc=1
                                   / 假 pattern=0 rc=1；rc=1 非 124
2 pause 語意未被破壞           ✅  mutation D（一律穿透）死的正是
                                   test_pause_job_marks_paused_and_keeps_loop_running
                                   斷言證據：Task cancelled ... _process_queue（迴圈陪葬）
3 mutation 鎖得住              ⚠  見下方「判準 3 的缺口」
4 _hard_cancel 可拿掉          ✅  grep rc=1（消失），CONTROL _shutdown=5 rc=0
                                   autostart 測試 7 passed rc=0
5 pytest 不下降                ✅  183 passed rc=0
6 容器優雅關閉                 ✅  見下方「判準 6」
```

### 判準 6（dispatcher 執行，handler 無重啟權限）

容器內起同構 uvicorn（真 lifespan + 真 `worker.stop()`，假 blocking download，
不碰使用者書庫、不打公網），對它發 SIGTERM：

```
修復後   SIGTERM_TO_EXIT 0.122s   STOP_RETURNED=True    STOP_MS=1.0
缺陷態   SIGTERM_TO_EXIT 5.123s   STOP_RETURNED=False   STOP_MS=5005.0
         + log.warning「仍有 1 個 task 存活，關閉流程未能乾淨收尾」

兩者     JOB_IN_FLIGHT=True     ← 關鍵：證據 ② 明說空佇列會白白通過
         EXIT_CODE=-15（SIGTERM）非 -9，未被 SIGKILL
         CLEANED True（探針殘留已清，容器內 test -e rc=1，
                       CONTROL test -e /app/app/main.py rc=0）
```

**42 倍差異，且 `stop()` 的 bool 兩態真的可分。** 缺陷態是在探針內把
`_process_queue` 換成「無守衛 + 一律吞取消」的複製品，**不動磁碟上的
production 檔案**（線上是 bind-mount + `--reload`，改檔會立刻波及使用者）。

另量空佇列基線：`docker compose stop` 2.34s、exit_code=0、
log 有 `Application shutdown complete`。**但那格如證據 ② 所述沒有鑑別力**，
不作為判準 6 的依據。

### 判準 3 的缺口（dispatcher 四組 mutation 的發現）

```
A 只拿掉 while not self._stopping，保留兩處 re-raise    9 passed rc=0
  CONTROL 守衛 count=0 rc=1（突變落地）
  CONTROL if self._stopping count=2 rc=0（只動守衛）
B 只拿掉 _process_queue 的 re-raise，保留守衛           1 failed rc=1
  AssertionError: 'paused' != 'queued'  ← 死的是狀態語意
C 兩處全還原（原始缺陷態）                              4 failed rc=1
D 一律穿透（拿掉 pause 分支）                           1 failed rc=1
每組還原後 diff rc=0、9 passed rc=0
```

**A 全過 ⇒ 沒有任何測試單獨鎖住迴圈守衛。** B 與 C 的差集顯示 re-raise 才是
必要條件；守衛在有 re-raise 的前提下是**冗餘的防禦深度**。

這不是修復缺陷（多一層防禦正當），但 handler 宣稱
**「只做 re-raise（mutation A）9 條裡只死 1 條、主測試存活」與實測相反**——
它描述的 mutation A 是「只 re-raise 不加守衛」，而我實測那個組合是**全過**。
方向對（兩處都改才完整）、數字錯（死 0 條不是 1 條）。

**留給後人的一格**：若日後有人「簡化」掉 `while not self._stopping`，
測試不會red。要補鎖就得寫一條「stop 後再 put 一個 job，迴圈不得取用它」的測試。

### handler 推翻本 BR 三格，皆經獨立坐實

**① 「修復方向 ②」說 stop() 要標記進行中的 job 但沒說標成什麼——標 `paused` 是錯的。**
`_load_jobs_from_disk():135` 只對 `queued`/`downloading` 重新入列，標 `paused`
會讓被關機中斷的下載重啟後**永遠不會自動繼續**，且與使用者主動暫停共用同一個輸出。
改標 `queued` + 明確 error_message（`_mark_interrupted_by_shutdown()`）。
mutation B 正是鎖住這格的測試。

**② 「沒驗證的」第 2 項（`_run_single_job:238`）確認有同樣問題，已一併修。**
它雖不在 `while True` 裡，但 `stop()` 必須等它結束；不 re-raise 的話
`asyncio.wait` 會等到那個 task 以「正常完成」收場，`stop()` 回 True 但
shutdown 期間進行中的 job 沒被標記狀態。

**③ 「沒驗證的」第 3 項（`:438`）讀過，無問題，不用動。** 它在
`_execute_download_with_resume` 的重試迴圈內，寫的是無條件 `raise`，
存在理由是擋住下一行 `except Exception` 把 CancelledError 吞進重試。語意正確。

### handler 過程中自陳的儀器缺陷（值得記）

第一版 `stop()` 沒去重 cancel 目標：`_process_queue` 執行 job 時會把自己同時
放進 `_active_tasks` 與 `_worker_task`，於是同一個 task 被 cancel 兩次——
第二次剛好打在 `queue.get()` 上而穿透，**把吞取消的缺陷蓋掉**，
mutation 下 `test_stop_after_pause_still_works` 假性存活。

**「取消真的穿透了」與「第一次被吞掉、第二次剛好打在 queue.get() 上才穿透」
共用同一個輸出。** 已加去重（`:190-196`）。

### 仍未涵蓋（不阻礙結案）

- 真實網路下載被 stop 中斷時，httpx socket 是否乾淨關閉、`.part` 檔是否完整落盤
- `delete_job` 路徑（與 `pause_job` 共用取消機制，推論相同但未實測）
- 多個 job 併發下的 `stop()`（只測到單一 in-flight + 一個排隊）
- 修復對 pause/resume/delete 三個 **HTTP 端點**的影響（測的是 worker 方法層）

以上四項都是**已知範圍邊界**，不是未修的缺陷。本 BR 的核心主張
（迴圈關不掉）已被修復並以 42 倍的容器實測差異坐實。

- **未量真實 uvicorn 關閉時的實際行為。** 上述 ③ 是讀 code 得出的，
  未實測 `docker compose stop` 是否真的被拖到 SIGKILL。
- **未確認 `_run_single_job`（`:234-249`）是否有同樣問題。** 它有結構相同的
  `except asyncio.CancelledError`（`:238`），但它不在 `while True` 裡——
  單次執行完就結束，取消被吞掉的後果較輕。**未實測。**
- **未確認 `:438` 的第三個 `CancelledError` handler。** 只 grep 到位置，未讀 context。
- **未評估 `_active_tasks` 與 `_worker_task` 的互動。** 值星官勘查時懷疑過
  `_process_queue`（`:330`）把 `current_task` 塞進 `_active_tasks[job_id]`
  可能與 `start_job` 的 `_run_single_job`（`:228`）互相覆蓋，
  但實測兩條路徑各自 `pop` 自己的 `job_id`，**未發現交叉污染**。
  這格標為「查過但未發現問題」，不是「未查」。
- **未評估修復對現有 `pause_job` / `resume_job` / `delete_job` 三個 API 端點的影響。**
