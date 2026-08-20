"""BR-20260820_143000 判準 4 — `enqueue(autostart=...)` 的兩態鎖定。

本檔鎖住的核心契約：**「參數生效」與「參數被忽略」不得共用同一個輸出**。

`enqueue()` 原本無條件呼叫 `self.start()`。在有 running event loop 的環境
（含 `TestClient`）下，這會立刻 `create_task(_process_queue())` 並對公網鏡像
發出真實 HTTP 請求——呼叫端沒有任何 opt-out。

因此每一條斷言都必須成對：
  - 只測 `autostart=False` 不啟動 → 一個「永遠不啟動」的壞實作也會通過。
  - 只測 `autostart=True` 會啟動   → 一個「忽略參數、永遠啟動」的壞實作
                                     （也就是修復前的原狀）也會通過。
  - 只測「沒偵測到網路請求」        → 一個壞掉的偵測器也會通過。

所以本檔的每個方向都自帶控制組，包含**偵測器本身**的控制組。
"""

import asyncio
import logging
import shutil
import tempfile

import httpx
import pytest

from app.crawler.download_worker import DownloadWorker

LOGGER_NAME = "app.crawler.download_worker"


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


class _NetworkTripwire:
    """攔在 `httpx.AsyncClient` 建構子上的偵測器。

    `download_worker` 與 `mirror_resolver` 都是在呼叫當下才對 `httpx` 模組取
    `AsyncClient` 屬性，所以替換模組屬性可同時罩住兩條出網路徑。

    建構即記錄，並立刻 raise——真實連線永遠不會發生，但「有沒有人試圖出去」
    這件事被保留下來。若只是回一個假 client，攔截失敗與「本來就沒人要出去」
    會共用同一個輸出。
    """

    def __init__(self, recorder):
        self.recorder = recorder

    def __call__(self, *args, **kwargs):
        self.recorder.append(kwargs.get("headers") or args or "<no-args>")
        raise RuntimeError("network blocked by _NetworkTripwire")


@pytest.fixture
def tripwire(monkeypatch):
    calls: list = []
    monkeypatch.setattr(httpx, "AsyncClient", _NetworkTripwire(calls))
    return calls


def _warnings(caplog) -> list:
    return [r for r in caplog.records
            if r.name == LOGGER_NAME and r.levelno >= logging.WARNING]


async def _shutdown(worker) -> bool:
    """收掉背景迴圈，回傳是否真的結束。

    這裡原本是一個反覆 cancel 200 次的 `_hard_cancel` workaround：
    `_process_queue()` 的 `except asyncio.CancelledError:` 無條件吞掉取消並回到
    迴圈頂端，所以單次 `task.cancel()` + `await task` 會永遠掛住。

    BR-20260820_230000 修復後，worker 自己有了可呼叫的關閉路徑，
    繞道就不再需要了——**這個 workaround 能不能拿掉，本身就是修復是否
    真的生效的檢驗**（判準 4）。包 `wait_for` 是為了讓回歸以斷言失敗而非
    逾時呈現。
    """
    if worker._worker_task is None:
        return True
    return await asyncio.wait_for(worker.stop(), timeout=5.0)


def _run(worker, scenario):
    """跑一個 scenario，並保證離開前一定回收 `worker._worker_task`。

    ⚠ 沒有這層 finally，**失敗的測試會以逾時而非斷言失敗呈現**：
    斷言拋出後 `asyncio.run()` 進入收尾，去取消殘留的 `_worker_task`，
    而一個關不掉的迴圈會把那裡變成永久掛住。逾時（rc=124）與「環境卡住」
    共用同一個輸出，不可以出現在證明它的測試本身。
    """
    async def wrapped():
        try:
            await scenario()
        finally:
            await _shutdown(worker)

    asyncio.run(wrapped())


# ---------------------------------------------------------------------------
# 方向 1 — autostart=False：入列成功，但背景迴圈完全沒被碰
# ---------------------------------------------------------------------------

def test_enqueue_autostart_false_enqueues_but_does_not_start(worker):
    """有 running loop 也不得啟動——這才是本參數存在的理由。"""
    async def scenario():
        # 控制組：確認此 context 下真的有 running loop，
        # 否則本測試會被「無 loop 所以本來就不會啟動」白白通過。
        assert asyncio.get_running_loop() is not None

        job = worker.enqueue(md5="a" * 32, title="不自動啟動", autostart=False)

        assert job.status == "queued"
        assert job.job_id in worker.jobs, "autostart=False 仍必須真的入列"
        assert worker.queue.qsize() == 1, "job 必須進到佇列裡等人來跑"
        assert worker._worker_task is None, \
            "autostart=False 不得建立背景迴圈"

    _run(worker, scenario)


def test_enqueue_autostart_false_is_silent(worker, caplog):
    """autostart=False 不得出聲。

    `start()` 在無 loop 時會 log.warning。若 `autostart=False` 誤實作成
    「照樣呼叫 start()」，在同步 context 下會留下那則 warning——
    這條就是那個誤實作的探針。
    """
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    caplog.clear()
    worker.enqueue(md5="b" * 32, title="同步無 loop 且不自動啟動", autostart=False)

    recs = _warnings(caplog)
    assert recs == [], \
        f"沒有嘗試啟動就沒有啟動失敗可報，實得：{[r.getMessage() for r in recs]}"


# ---------------------------------------------------------------------------
# 方向 2 — 預設（autostart=True）：行為必須與加參數前逐字相同
# ---------------------------------------------------------------------------

def test_enqueue_default_still_starts_worker(worker):
    """不傳參數＝維持原行為：有 loop 時 `_worker_task` 真的被建立。

    沒有這一條，一個「永遠不啟動」的實作也會讓方向 1 全數通過。
    """
    async def scenario():
        job = worker.enqueue(md5="c" * 32, title="預設啟動")

        assert job.status == "queued"
        task = worker._worker_task
        assert task is not None, "預設路徑必須建立背景 task（行為零變更）"
        assert isinstance(task, asyncio.Task)

        assert await _shutdown(worker), "背景 task 必須能被收掉"
        assert task.done(), "收掉之後 task 必須真的結束"

    _run(worker, scenario)


def test_enqueue_explicit_true_matches_default(worker):
    """顯式 `autostart=True` 與省略參數必須是同一條路徑。"""
    async def scenario():
        worker.enqueue(md5="d" * 32, title="顯式 True", autostart=True)
        task = worker._worker_task
        assert task is not None

        assert await _shutdown(worker), "背景 task 必須能被收掉"
        assert task.done(), "收掉之後 task 必須真的結束"

    _run(worker, scenario)


def test_enqueue_default_in_sync_context_still_warns(worker, caplog):
    """無 loop 的同步 context 下，預設路徑仍必須沿用既有的出聲行為。

    這是「預設行為零變更」的另一面：上一包（BR 判準 1-3）建立的 warning 契約
    不得因為本次加參數而被削弱。
    """
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    caplog.clear()
    worker.enqueue(md5="e" * 32, title="同步預設")

    assert len(_warnings(caplog)) >= 1, \
        "預設路徑在無 loop 下仍必須出聲（與加參數前相同）"


# ---------------------------------------------------------------------------
# 方向 3 — 真的不發對外 HTTP（判準 4 的字面要求），偵測器自帶控制組
# ---------------------------------------------------------------------------

def test_enqueue_autostart_false_issues_no_outbound_http(worker, tripwire):
    """autostart=False：讓 loop 實際轉一段時間，不得有任何出網嘗試。"""
    async def scenario():
        worker.enqueue(md5="f" * 32, title="不出網", autostart=False)
        # 給 loop 真的跑一段時間：若有被偷偷排程的 task，這裡足夠讓它跑到出網那步。
        for _ in range(20):
            await asyncio.sleep(0.01)

        assert tripwire == [], \
            f"autostart=False 不得發出任何對外請求，實得 {len(tripwire)} 次：{tripwire}"
        assert worker._worker_task is None

    _run(worker, scenario)


def test_tripwire_actually_detects_outbound_http(worker, tripwire):
    """偵測器的控制組——沒有這一條，上一條等於什麼都沒證明。

    同一個 worker、同一個偵測器，改走 `autostart=True`：偵測器**必須**跳。
    這同時也是「預設行為確實會出網」的正面證據，也就是本 BR 記載的那個副作用。
    """
    async def scenario():
        worker.enqueue(md5="0" * 32, title="會出網", autostart=True)
        assert worker._worker_task is not None

        for _ in range(200):          # 最多 2 秒
            if tripwire:
                break
            await asyncio.sleep(0.01)

        await _shutdown(worker)

        assert tripwire, \
            "偵測器未在預設路徑上跳——它抓不到網路請求，上一條測試無效"

    _run(worker, scenario)
