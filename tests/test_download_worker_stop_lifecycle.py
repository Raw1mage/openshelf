"""BR-20260820_230000 — DownloadWorker 背景迴圈的關閉路徑兩態鎖定。

本檔鎖住的核心契約：**「關閉整個 worker」與「暫停單一 job」不得共用同一個輸出**。

兩者送出的訊號都是 `asyncio.CancelledError`，語意卻相反：

    stop()                 → 迴圈必須**結束**
    pause_job / delete_job → 該 job 標 paused，迴圈必須**繼續**

修復前 `_process_queue()` 只實作了後者（無條件吞掉取消），於是前者永遠關不掉。
所以每個方向都必須成對測，否則：

  - 只測 stop 能關掉 → 一個「取消一律穿透」的壞實作會通過，
    而那個實作會讓使用者按一次暫停就把整個背景迴圈殺掉。
  - 只測 pause 仍正常 → 修復前的原狀（一律吞）會通過。

⚠ 每一條等待都包在 `asyncio.wait_for` 裡。本 BR 的失效形狀是**逾時**而非錯誤，
而逾時（rc=124）與「機器忙」「pytest 掛了」共用同一個輸出——證明「兩態不得共用
同一個輸出」的測試，不可以自己踩同一個病。逾時在這裡一律轉成明確的斷言失敗。
"""

import asyncio
import shutil
import tempfile

import pytest

from app.crawler.download_worker import DownloadWorker

# 迴圈若真的關不掉，這個秒數就是每條測試的成本上限；遠低於 pytest 逾時，
# 所以失敗一定以斷言呈現，不會退化成 rc=124。
STOP_TIMEOUT = 3.0


def _make_worker(temp_dir: str) -> DownloadWorker:
    from app.storage.manager import StorageManager
    from app.db.engine import DatabaseEngine
    from app.db.dao import CatalogDAO
    from app.pipeline.ingest import IngestionPipeline

    storage = StorageManager(base_dir=temp_dir)
    engine = DatabaseEngine(db_path=storage.get_db_path())
    dao = CatalogDAO(engine=engine)
    pipeline = IngestionPipeline(storage=storage, dao=dao)
    return DownloadWorker(pipeline=pipeline)


@pytest.fixture
def worker():
    temp_dir = tempfile.mkdtemp()
    try:
        yield _make_worker(temp_dir)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


class _BlockingDownload:
    """把 `_execute_download_with_resume` 換成一個永遠不會自己結束的 await。

    這是重現本 BR 的必要條件：**佇列空的時候缺陷不會浮現**（見 BR 證據 ②）——
    空佇列時 `_process_queue` 停在 `await queue.get()`，取消直接穿透，一切正常。
    只有「迴圈正卡在一個進行中的 job」時，`:334` 的吞取消才會把迴圈變成關不掉。

    同時它也取代了真實網路：本檔不發出任何對外請求。
    """

    def __init__(self):
        self.entered = asyncio.Event()
        self.calls: list = []

    async def __call__(self, job):
        self.calls.append(job.job_id)
        job.status = "downloading"
        self.entered.set()
        await asyncio.Event().wait()      # 永不完成，直到被取消


async def _stop_or_fail(worker, label: str = "stop()") -> bool:
    """呼叫 `worker.stop()`，並把逾時轉成**明確的斷言失敗**（判準 1）。

    裸的 `asyncio.wait_for` 拋的是 `TimeoutError`，它與「環境卡住」共用同一個
    輸出形狀；要求本檔每一條失敗都要能直接讀出「背景迴圈關不掉」這個結論。
    """
    try:
        return await asyncio.wait_for(worker.stop(), timeout=STOP_TIMEOUT)
    except asyncio.TimeoutError:
        pytest.fail(
            f"{label}：stop() 在 {STOP_TIMEOUT}s 內沒有回來——背景迴圈關不掉。"
            "（這正是 BR-20260820_230000 的缺陷；失敗以斷言呈現而非 rc=124）"
        )


async def _wait_until_in_flight(blocker, label: str):
    """等到背景迴圈真的進到 job 裡面，否則後面測的是空佇列（會白白通過）。"""
    try:
        await asyncio.wait_for(blocker.entered.wait(), timeout=STOP_TIMEOUT)
    except asyncio.TimeoutError:
        pytest.fail(
            f"{label}：背景迴圈在 {STOP_TIMEOUT}s 內沒有進到下載中的 job，"
            "本測試將在空佇列狀態下白白通過（見 BR 證據 ②）"
        )


def _run(coro_factory):
    asyncio.run(coro_factory())


# ---------------------------------------------------------------------------
# 方向 1 — stop()：進行中的 job 也必須關得掉
# ---------------------------------------------------------------------------

def test_stop_ends_worker_loop_with_job_in_flight(worker, monkeypatch):
    """本 BR 的正面證據：enqueue 之後 stop() 必須真的把迴圈關掉。

    修復前這條會失敗——`_process_queue()` 吞掉取消回到 `while` 頂端，
    `stop()` 等不到 task 結束，回傳 False 且 `_worker_task.done()` 為 False。
    """
    blocker = _BlockingDownload()
    monkeypatch.setattr(worker, "_execute_download_with_resume", blocker)

    async def scenario():
        worker.enqueue(md5="a" * 32, title="進行中的下載")
        task = worker._worker_task
        assert task is not None, "控制組：背景迴圈必須真的被建立，否則本測試無標的"

        await _wait_until_in_flight(blocker, "stop 方向")
        assert not task.done(), "控制組：此刻迴圈必須還活著"

        stopped = await _stop_or_fail(worker, "stop 方向")

        assert stopped is True, "stop() 必須回報它真的收乾淨了"
        assert task.done(), "背景迴圈的 task 必須真的結束"

    _run(scenario)


def test_stop_marks_in_flight_job_resumable(worker, monkeypatch):
    """關閉時進行中的 job 必須落盤成可辨識的中斷態。

    不得標成 `paused`：那是「使用者按了暫停」的語意，重啟後不會自動繼續。
    兩者共用 `paused` 的話，「使用者暫停的」與「被關機掉的」會共用同一個輸出。
    """
    blocker = _BlockingDownload()
    monkeypatch.setattr(worker, "_execute_download_with_resume", blocker)

    async def scenario():
        job = worker.enqueue(md5="b" * 32, title="關機時進行中")
        await _wait_until_in_flight(blocker, "中斷態")

        await _stop_or_fail(worker, "中斷態")

        assert job.status == "queued", \
            f"中斷的 job 必須回 queued 才會被下次啟動重新入列，實得 {job.status!r}"
        assert job.status != "paused", "不得與『使用者按暫停』共用同一個狀態"
        assert job.error_message and "關閉" in job.error_message, \
            f"必須說明原因，實得 {job.error_message!r}"

    _run(scenario)


def test_stop_on_empty_queue_is_also_clean(worker):
    """控制組：空佇列本來就關得掉（修復前也通過）。

    保留它是為了標示邊界——**只有這一條通過不代表缺陷被修好**，
    它正是 BR 證據 ② 所說「缺陷一直沒被發現」的那個狀態。
    """
    async def scenario():
        worker.start()
        task = worker._worker_task
        assert task is not None

        stopped = await _stop_or_fail(worker, "空佇列")
        assert stopped is True
        assert task.done()


def test_stop_is_idempotent(worker):
    """重複 stop() 不得拋出——lifespan 的 finally 可能與其他路徑重疊。"""
    async def scenario():
        worker.start()
        assert await _stop_or_fail(worker, "第一次 stop") is True
        assert await _stop_or_fail(worker, "第二次 stop") is True

    _run(scenario)


def test_stop_without_ever_starting_does_not_raise(worker):
    """從未 start() 過就 stop()：必須安靜成功，不得炸掉關閉流程。"""
    async def scenario():
        assert worker._worker_task is None, "控制組：確認這條路徑上真的沒有 task"
        assert await _stop_or_fail(worker, "未啟動即關閉") is True

    _run(scenario)


# ---------------------------------------------------------------------------
# 方向 2 — pause_job()：既有語意不得被誤傷（判準 2）
# ---------------------------------------------------------------------------

def test_pause_job_marks_paused_and_keeps_loop_running(worker, monkeypatch):
    """**本檔最重要的一條。**

    `:334` 的 `except CancelledError` 存在是有理由的：使用者按暫停/刪除時，
    取消的是內層那個 job，迴圈必須繼續服務下一個任務。

    沒有這一條，一個「取消一律穿透」的壞實作會讓方向 1 全數通過——
    而那個實作的實際後果是：使用者按一次暫停，整個下載背景迴圈就死了。
    """
    blocker = _BlockingDownload()
    monkeypatch.setattr(worker, "_execute_download_with_resume", blocker)

    async def scenario():
        job = worker.enqueue(md5="c" * 32, title="要被暫停的")
        task = worker._worker_task
        await _wait_until_in_flight(blocker, "pause 方向")

        worker.pause_job(job.job_id)

        # 讓取消真的被投遞並處理完
        for _ in range(50):
            if job.status == "paused" and not blocker.entered.is_set():
                break
            await asyncio.sleep(0.01)

        assert job.status == "paused", f"pause_job 必須標記 paused，實得 {job.status!r}"
        assert not task.done(), \
            "迴圈必須仍在跑——單一 job 的取消不得讓整個背景 worker 陪葬"

        # 更強的證據：迴圈不只「還沒 done」，而是真的還會服務下一個任務。
        blocker.entered.clear()
        worker.enqueue(md5="d" * 32, title="暫停之後的下一個")
        await _wait_until_in_flight(blocker, "暫停後的下一個任務")

        assert len(blocker.calls) == 2, \
            f"迴圈必須真的取用了第二個 job，實得 calls={blocker.calls}"
        assert not task.done(), "服務第二個任務時迴圈仍必須活著"

        await _stop_or_fail(worker, "pause 後收尾")

    _run(scenario)


def test_stop_after_pause_still_works(worker, monkeypatch):
    """暫停過之後仍必須關得掉——兩條路徑共用 `_stopping`，不得互相污染。"""
    blocker = _BlockingDownload()
    monkeypatch.setattr(worker, "_execute_download_with_resume", blocker)

    async def scenario():
        job = worker.enqueue(md5="e" * 32, title="先暫停再關閉")
        task = worker._worker_task
        await _wait_until_in_flight(blocker, "先暫停再關閉")

        worker.pause_job(job.job_id)
        for _ in range(50):
            if job.status == "paused":
                break
            await asyncio.sleep(0.01)

        blocker.entered.clear()
        worker.enqueue(md5="f" * 32, title="第二個進行中")
        await _wait_until_in_flight(blocker, "第二個進行中")

        stopped = await _stop_or_fail(worker, "先暫停再關閉")
        assert stopped is True
        assert task.done()


def test_start_after_stop_clears_stopping_flag(worker, monkeypatch):
    """關閉後重新啟動：`_stopping` 必須被清掉。

    沒有這一條，一個「stop 之後旗標永遠為真」的實作會通過方向 1，
    但重啟後第一次 pause 就會把新迴圈立刻殺掉（取消被當成 shutdown）。
    """
    blocker = _BlockingDownload()
    monkeypatch.setattr(worker, "_execute_download_with_resume", blocker)

    async def scenario():
        worker.start()
        await _stop_or_fail(worker, "重啟前的關閉")
        assert worker._stopping is True, "控制組：stop() 後旗標確實為真"

        worker.start()
        assert worker._stopping is False, "重新啟動必須清掉關閉旗標"

        job = worker.enqueue(md5="1" * 32, title="重啟後的任務")
        await _wait_until_in_flight(blocker, "重啟後")

        task = worker._worker_task
        worker.pause_job(job.job_id)
        for _ in range(50):
            if job.status == "paused":
                break
            await asyncio.sleep(0.01)

        assert job.status == "paused"
        assert not task.done(), "重啟後的迴圈同樣不得被單一 job 的暫停殺掉"

        await _stop_or_fail(worker, "重啟後的關閉")

    _run(scenario)


# ---------------------------------------------------------------------------
# 方向 3 — lifespan 真的有接上（判準：只修 ① 沒有人會去取消它）
# ---------------------------------------------------------------------------

def test_lifespan_stops_worker_on_shutdown(monkeypatch):
    """`app.main.lifespan` 的 `yield` 之後必須呼叫 `worker.stop()`。

    只修 `_process_queue` 而沒有呼叫端，等於沒修——兩者共用「服務關不乾淨」
    這一個輸出（BR 證據 ④）。
    """
    import app.main as main_module

    calls: list = []

    class _FakeWorker:
        def start(self):
            calls.append("start")

        async def stop(self, timeout: float = 5.0):
            calls.append("stop")
            return True

    monkeypatch.setattr(main_module, "get_worker", lambda: _FakeWorker())
    monkeypatch.setattr(main_module.StorageManager, "ensure_directories", lambda self: None)
    monkeypatch.setattr(main_module.DatabaseEngine, "init_database", lambda self: None)

    async def scenario():
        async with main_module.lifespan(main_module.app):
            assert calls == ["start"], \
                f"控制組：進入 lifespan 後應只有 start，實得 {calls}"
        assert calls == ["start", "stop"], \
            f"離開 lifespan 必須呼叫 worker.stop()，實得 {calls}"

    asyncio.run(scenario())
