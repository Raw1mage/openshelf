"""BR-20260820_143000 — DownloadWorker 靜默失效的兩態鎖定。

本檔鎖住的核心契約：**缺席態與失敗態不得共用同一個輸出**。

`start()` 原本用 `except RuntimeError: pass` 吞掉「當下沒有 running event loop」，
於是同一支 `enqueue()` 在兩種環境下行為完全相反且兩邊都不出聲：
  - 無 loop（一般 pytest）→ 背景迴圈根本沒起來，任務永遠停在 queued
  - 有 loop（TestClient）→ 真的 create_task 去打公網鏡像

測試必須**兩個方向都測**：只斷言「無 loop 有 warning」的話，一個恆記 warning 的實作
會通過；只斷言「有 loop 無 warning」的話，一個恆不記的實作（也就是修復前的原狀）
會通過。兩條合起來才有鑑別力。
"""

import asyncio
import logging
import shutil
import tempfile

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


def _warnings(caplog) -> list:
    return [r for r in caplog.records
            if r.name == LOGGER_NAME and r.levelno >= logging.WARNING]


# ---------------------------------------------------------------------------
# 方向 1 — 無 running loop：必須出聲
# ---------------------------------------------------------------------------

def test_start_without_event_loop_warns(worker, caplog):
    """無 loop 呼叫 start()：背景迴圈不會啟動，這件事必須有訊號。"""
    # 控制組：確認此 context 下真的沒有 running loop，否則本測試在測別的東西
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()

    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    caplog.clear()
    worker.start()

    recs = _warnings(caplog)
    assert len(recs) == 1, f"無 loop 必須且只需出聲一次，實得 {len(recs)} 筆：{[r.getMessage() for r in recs]}"
    msg = recs[0].getMessage()
    assert "queued" in msg, f"訊息必須說明後果（任務停留在 queued），實得：{msg!r}"
    assert worker._worker_task is None, "無 loop 時不得留下半啟動的 task"


def test_enqueue_without_event_loop_warns(worker, caplog):
    """`enqueue()` 尾端無條件呼叫 start()，所以同樣的靜默會沿這條路徑傳播。

    這是使用者真正會走的入口：同步 context 下丟一個任務進來，
    修復前它會安靜地什麼都不做。
    """
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    caplog.clear()
    job = worker.enqueue(md5="a" * 32, title="無 loop 入列")

    assert job.status == "queued"
    assert len(_warnings(caplog)) >= 1, "enqueue 在無 loop 下什麼都不會執行，必須出聲"


# ---------------------------------------------------------------------------
# 方向 2 — 有 running loop：必須靜默，且 task 真的被建立
# ---------------------------------------------------------------------------

def test_start_with_event_loop_is_silent_and_creates_task(worker, caplog):
    """有 loop 的正常路徑：不得出聲，且 `_worker_task` 真的被建立。

    沒有這一條，一個「無論如何都記 warning」的實作也會讓方向 1 通過。
    """
    async def scenario():
        caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
        caplog.clear()
        worker.start()

        recs = _warnings(caplog)
        assert recs == [], f"正常啟動路徑不得出聲，實得：{[r.getMessage() for r in recs]}"

        task = worker._worker_task
        assert task is not None, "有 loop 時必須真的建立背景 task"
        assert isinstance(task, asyncio.Task)
        assert not task.done()

        # 佇列是空的，_process_queue 會停在 await queue.get()，不會發出任何網路請求。
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_start_twice_with_event_loop_does_not_duplicate_task(worker):
    """重入保護：已有存活的 task 時再呼叫 start() 不得再建一個。"""
    async def scenario():
        worker.start()
        first = worker._worker_task
        worker.start()
        assert worker._worker_task is first, "不得重複建立背景迴圈"

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 同族第二、三個實例 — 持久化的靜默 except
# ---------------------------------------------------------------------------

def test_load_jobs_from_disk_warns_on_corrupt_file(worker, caplog):
    """壞掉的 jobs.json：載入失敗仍不阻斷（回退行為不變），但必須出聲。"""
    worker._jobs_file.parent.mkdir(parents=True, exist_ok=True)
    worker._jobs_file.write_text("{ this is not valid json", encoding="utf-8")

    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    caplog.clear()
    worker._load_jobs_from_disk()   # 不得拋出例外

    recs = _warnings(caplog)
    assert len(recs) >= 1, "讀不出來與檔案裡沒有任務不得共用同一個輸出"
    assert "JSONDecodeError" in recs[0].getMessage(), \
        f"訊息必須帶例外型別才能定位，實得：{recs[0].getMessage()!r}"


def test_load_jobs_from_disk_is_silent_on_valid_file(worker, caplog):
    """控制組：健康的 jobs.json 不得出聲。

    沒有這一條，一個「無條件記 warning」的實作也會讓上一條通過。
    """
    import json
    worker._jobs_file.parent.mkdir(parents=True, exist_ok=True)
    worker._jobs_file.write_text(
        json.dumps([{"job_id": "job_ok", "md5": "b" * 32, "title": "健康任務",
                     "status": "completed"}]),
        encoding="utf-8",
    )

    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    caplog.clear()
    worker.jobs.clear()
    worker._load_jobs_from_disk()

    assert _warnings(caplog) == [], "正常載入不得出聲"
    assert "job_ok" in worker.jobs, "控制組：這份 fixture 必須真的載得進來"


def test_save_jobs_to_disk_warns_on_write_failure(worker, caplog, monkeypatch):
    """存檔失敗仍不阻斷呼叫端（回退行為不變），但必須出聲。"""
    def _boom(*args, **kwargs):
        raise PermissionError("disk is read-only")

    monkeypatch.setattr("builtins.open", _boom)

    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    caplog.clear()
    worker._save_jobs_to_disk()   # 不得拋出例外

    recs = _warnings(caplog)
    assert len(recs) >= 1, "寫成功與寫失敗不得共用同一個輸出"
    assert "PermissionError" in recs[0].getMessage()


def test_save_jobs_to_disk_is_silent_on_success(worker, caplog):
    """控制組：正常存檔不得出聲。"""
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    caplog.clear()
    worker._save_jobs_to_disk()

    assert _warnings(caplog) == [], "正常存檔不得出聲"
    assert worker._jobs_file.exists(), "控制組：這條路徑必須真的寫出檔案"
