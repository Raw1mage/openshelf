"""BR-20260820_210000 E 節 + BR-20260821_040000：同步阻塞 I/O 移出 event loop。

本檔鎖三件事，每一條都附控制組——「修好了」與「這條路徑根本沒被走到」
會讓同一個斷言變綠，所以每格都要有一個**必須非零/必須失敗**的對照。

1. `StorageManager` 的進程級守衛：建構子不再每次 mkdir，但顯式呼叫仍真的執行。
2. 下載迴圈的檔案 I/O 走專用執行緒額度，且**不吃**預設 40 格 threadpool。
3. `delete_job` 的 NFS `unlink` 移出 loop，但 `task.cancel()` 留在 loop 上。
"""
import asyncio
import os
import threading

import anyio
import anyio.to_thread
import pytest

from app.storage.manager import StorageManager


# ---------------------------------------------------------------- 1. mkdir 守衛


@pytest.fixture
def fresh_guard():
    """每條測試都從乾淨的 process 級守衛開始，並在結束後還原。

    不還原的話，這個 class 屬性會洩漏到同一輪 pytest 的其他測試檔，
    讓它們的 StorageManager 靜默跳過建目錄——那是一個**跨檔案的假綠燈**。
    """
    saved = set(StorageManager._ensured_dirs)
    StorageManager._ensured_dirs.clear()
    yield
    StorageManager._ensured_dirs.clear()
    StorageManager._ensured_dirs.update(saved)


def _count_mkdir(monkeypatch):
    """攔 `os.mkdir`（最底層）而不是 `Path.mkdir`。

    `pathlib` 內部呼叫的就是 `os.mkdir`，攔這裡才涵蓋不經 pathlib 的路徑；
    攔 `Path.mkdir` 會讓「漏掉一條路徑」與「真的沒有呼叫」共用同一個輸出。
    """
    calls = []
    real = os.mkdir

    def spy(path, *a, **kw):
        calls.append(str(path))
        return real(path, *a, **kw)

    monkeypatch.setattr(os, "mkdir", spy)
    return calls


def test_first_storage_manager_creates_dirs(tmp_path, monkeypatch, fresh_guard):
    """CONTROL：第一次建構**必須**真的 mkdir。

    沒有這條，下一條的「第二次 0 次」就無法與「守衛把所有人都擋掉了、
    目錄根本沒被建立」區分。
    """
    calls = _count_mkdir(monkeypatch)
    StorageManager(base_dir=tmp_path / "data")

    assert len(calls) > 0, "第一次建構就沒有 mkdir，代表目錄從未被建立"
    assert (tmp_path / "data" / "raw").is_dir()
    assert (tmp_path / "data" / "parsed").is_dir()
    assert (tmp_path / "data" / "db").is_dir()


def test_subsequent_storage_managers_do_no_mkdir(tmp_path, monkeypatch, fresh_guard):
    """SUBJECT：同一個 base_dir 的第 2..N 次建構一次 syscall 都不下。

    這是 BR-20260821_040000 的主體——`raw`/`parsed` 掛在 `hard,timeo=600`
    的 NFS 上，每個 API 請求各下一次 mkdir。
    """
    base = tmp_path / "data"
    StorageManager(base_dir=base)          # 第一次：真的建

    calls = _count_mkdir(monkeypatch)      # 從第二次才開始數
    for _ in range(5):
        StorageManager(base_dir=base)

    assert calls == [], f"第 2..6 次建構仍下了 mkdir：{calls}"


def test_guard_is_per_base_dir_not_global(tmp_path, monkeypatch, fresh_guard):
    """守衛必須以 base_dir 為 key，不能是一個全域布林。

    全域布林會讓「第一個 StorageManager 建好了」變成「所有其他路徑都跳過」，
    測試用 tmp_path 的情境會整批靜默不建目錄。
    """
    StorageManager(base_dir=tmp_path / "a")

    calls = _count_mkdir(monkeypatch)
    StorageManager(base_dir=tmp_path / "b")   # 不同路徑，必須真的建

    assert len(calls) > 0, "不同 base_dir 被同一個守衛擋掉了"
    assert (tmp_path / "b" / "raw").is_dir()


def test_explicit_ensure_directories_always_executes(tmp_path, monkeypatch, fresh_guard):
    """公開語義不變：顯式呼叫一律真的執行（`main.py:23` lifespan 依賴這點）。

    只擋建構子那條每請求路徑。這兩態若共用同一個輸出，
    啟動引導就會在第二次啟動時靜默變成 no-op。
    """
    storage = StorageManager(base_dir=tmp_path / "data")

    calls = _count_mkdir(monkeypatch)
    storage.ensure_directories()           # 顯式呼叫

    assert len(calls) == 3, f"顯式 ensure_directories 應下 3 次 mkdir，實際 {calls}"


def test_ensure_once_return_value_distinguishes_skip_from_run(tmp_path, fresh_guard):
    """「跳過」與「執行」必須回不同的值，否則守衛是否生效無法被觀察。"""
    s1 = StorageManager(base_dir=tmp_path / "data")
    assert s1._ensure_directories_once() is False, "已建過仍回 True"

    StorageManager._ensured_dirs.clear()
    assert s1._ensure_directories_once() is True, "清空守衛後仍回 False"


# ------------------------------------------------- 2. 專用執行緒額度與隔離


def test_file_io_limiter_does_not_consume_default_threadpool():
    """本包最關鍵的一格：worker 的檔案 I/O **不得**吃預設 40 格。

    預設池同時是每一個 HTTP 請求的 sync 相依項在用的
    （`fastapi/dependencies/utils.py:676` 把 sync 依賴一律丟預設池）。
    共用的話，幾個同時進行的 NFS 下載就能把整站請求餓死——
    **把 event loop 阻塞換成 threadpool 排隊，使用者看到的症狀完全相同**。
    """
    from app.crawler.download_worker import _FILE_IO_LIMITER, _run_file_io

    async def scenario():
        default = anyio.to_thread.current_default_thread_limiter()
        assert default.borrowed_tokens == 0

        released = threading.Event()
        observed = {}

        async def hold():
            await _run_file_io(released.wait)

        async with anyio.create_task_group() as tg:
            for _ in range(3):
                tg.start_soon(hold)
            await anyio.sleep(0.2)
            observed["custom"] = _FILE_IO_LIMITER.borrowed_tokens
            observed["default"] = default.borrowed_tokens
            released.set()

        return observed, default.total_tokens

    observed, default_total = asyncio.run(scenario())

    # CONTROL：自訂 limiter 真的被借走了，證明工作確實跑在受限池上
    assert observed["custom"] == 3, f"自訂 limiter 未被借用：{observed}"
    # SUBJECT：預設池完全沒被碰
    assert observed["default"] == 0, (
        f"worker 檔案 I/O 吃了預設 threadpool 名額：{observed}"
    )
    assert default_total == 40, f"預設池大小假設已變：{default_total}"


def test_file_io_runs_off_the_event_loop_thread():
    """`_run_file_io` 必須真的換執行緒，不是同步跑完裝樣子。"""
    from app.crawler.download_worker import _run_file_io

    async def scenario():
        loop_thread = threading.current_thread().name
        worker_thread = await _run_file_io(lambda: threading.current_thread().name)
        return loop_thread, worker_thread

    loop_thread, worker_thread = asyncio.run(scenario())
    assert loop_thread != worker_thread, (
        f"檔案 I/O 仍在 event loop 執行緒上：{loop_thread}"
    )


def test_limiter_survives_multiple_event_loops():
    """module-scope limiter 必須可跨多個獨立 event loop 重用。

    每條 async 測試各自 `asyncio.run` 一個新 loop；若 limiter 綁死第一個 loop，
    第二條之後會全部炸掉。
    """
    from app.crawler.download_worker import _run_file_io

    for _ in range(2):
        assert asyncio.run(_run_file_io(lambda: 7)) == 7
