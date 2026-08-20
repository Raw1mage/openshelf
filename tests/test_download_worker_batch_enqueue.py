"""BR-20260820_210000 F 節 — `enqueue_many` 的批次入列成本與語意鎖定。

本檔鎖住兩件事，**缺一不可**：

1. **成本**：N 筆任務只寫一次 `download_jobs.json`。
2. **語意**：批次與逐筆 `enqueue()` 的去重、autostart、回傳值必須完全一致。

⚠ 只鎖成本會讓一個「什麼都不做」的實作通過——存檔 0 次的最快方法就是不建立任何 job。
所以每一條成本斷言都**必須配一條正面證據**（job 真的建出來了、佇列真的有東西）。
這是 handler J 在 A 節踩過的形狀：「修好了」與「這條路徑沒被走到」共用同一個輸出。

⚠ 存檔次數鎖的是**確切數字**不是「變小了」：
    有建立任何新 job → 恰好 1 次
    一筆都沒建立     → 恰好 0 次（與 enqueue() 命中重複時不存檔的行為一致）
"""

import asyncio
import shutil
import tempfile

import pytest

from app.crawler.download_worker import DownloadWorker

# 夠大到讓 O(N²) 與 O(N) 的存檔次數差距無法用巧合解釋（120 vs 1）。
# 與值星官量 F 節現況時用的 N 相同，方便對照。
BATCH_N = 120


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


class _SaveSpy:
    """數 `_save_jobs_to_disk` 被呼叫幾次，並且**仍然真的存檔**。

    不是換成 no-op：no-op 會讓「存檔次數降到 1」與「存檔壞掉了」共用同一個輸出。
    這裡保留真實副作用，只是在旁邊記一筆。
    """

    def __init__(self, real):
        self.real = real
        self.count = 0

    def __call__(self):
        self.count += 1
        return self.real()


@pytest.fixture
def save_spy(worker, monkeypatch):
    spy = _SaveSpy(worker._save_jobs_to_disk)
    monkeypatch.setattr(worker, "_save_jobs_to_disk", spy)
    return spy


def _items(n: int, prefix: str = "a"):
    """n 筆合法且互不重複的入列項。"""
    return [
        {"md5": f"{prefix}{i:031x}", "title": f"批次書 {i}"}
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# 方向 1 — 成本：N 筆只寫一次（判準 1）
# ---------------------------------------------------------------------------

def test_enqueue_many_saves_exactly_once(worker, save_spy):
    """N=120 筆 → 存檔恰好 1 次，且 120 筆真的都建出來了。

    後半句是「修好了 vs 沒被走到」的分辨器：一個直接 return 的實作
    存檔 0 次，會讓只鎖次數的測試更漂亮地通過。
    """
    jobs = worker.enqueue_many(_items(BATCH_N), autostart=False)

    assert save_spy.count == 1, \
        f"批次入列必須恰好存檔 1 次，實得 {save_spy.count} 次"

    # 正面證據：工作真的做了
    assert len(jobs) == BATCH_N, f"必須回傳 {BATCH_N} 個 job，實得 {len(jobs)}"
    assert len(worker.jobs) == BATCH_N, \
        f"{BATCH_N} 筆必須真的進到 self.jobs，實得 {len(worker.jobs)}"
    assert worker.queue.qsize() == BATCH_N, \
        f"{BATCH_N} 筆必須真的進到佇列，實得 {worker.queue.qsize()}"
    assert all(j.status == "queued" for j in jobs)
    assert len({j.job_id for j in jobs}) == BATCH_N, "job_id 不得重複"


def test_逐筆_enqueue_saves_N_times_control(worker, save_spy):
    """控制組——證明 spy 真的在數，而且逐筆路徑確實是 N 次。

    沒有這一條，方向 1 的 `count == 1` 可能只是因為 spy 壞了／根本沒掛上。
    這同時也是 F 節 O(N²) 的正面證據：逐筆呼叫就是 N 次整份重寫。
    """
    n = 10
    for item in _items(n):
        worker.enqueue(md5=item["md5"], title=item["title"], autostart=False)

    assert save_spy.count == n, \
        f"控制組：逐筆 enqueue 應存檔 {n} 次（每筆一次），實得 {save_spy.count}"
    assert len(worker.jobs) == n


def test_enqueue_many_saves_zero_times_when_nothing_created(worker, save_spy):
    """一筆都沒建立時存檔 0 次——與 `enqueue()` 命中重複時不存檔一致。

    這條把「恰好 1」釘成可解釋的數字：1 是因為**有東西要寫**，
    不是因為實作裡寫死了一個 1。
    """
    # 先建一批（用掉 1 次存檔）
    first = worker.enqueue_many(_items(3), autostart=False)
    assert save_spy.count == 1
    assert len(first) == 3

    # 同一批再送一次：全部命中重複，不該再寫
    again = worker.enqueue_many(_items(3), autostart=False)

    assert save_spy.count == 1, \
        f"全部重複時不得再存檔，實得總計 {save_spy.count} 次"
    assert len(worker.jobs) == 3, "重複入列不得增加 job 數"
    assert [j.job_id for j in again] == [j.job_id for j in first], \
        "命中重複必須回傳既有的同一批 job（與 enqueue() 行為一致）"


def test_enqueue_many_empty_list_saves_zero_and_returns_empty(worker, save_spy):
    """空清單：不存檔、不啟動、不拋出。"""
    jobs = worker.enqueue_many([], autostart=True)

    assert jobs == []
    assert save_spy.count == 0, f"空清單不得存檔，實得 {save_spy.count}"
    assert worker._worker_task is None, "空清單不得啟動背景迴圈"
    assert worker.queue.qsize() == 0


# ---------------------------------------------------------------------------
# 方向 2 — 語意：與逐筆 enqueue() 必須一致
# ---------------------------------------------------------------------------

def test_enqueue_many_dedup_matches_single_enqueue(worker):
    """已存在的 md5：批次與逐筆都回傳既有 job，不建新的。"""
    existing = worker.enqueue(md5="f" * 32, title="先來的", autostart=False)

    out = worker.enqueue_many(
        [{"md5": "F" * 32, "title": "大寫的同一本"}], autostart=False
    )

    assert len(out) == 1
    assert out[0].job_id == existing.job_id, \
        "md5 比對必須大小寫不敏感，且回傳既有 job（與 enqueue() 一致）"
    assert len(worker.jobs) == 1, "不得建立第二個 job"


def test_enqueue_many_dedups_within_the_same_batch(worker, save_spy):
    """同一批次內出現兩次相同 md5：只建一個。

    逐筆 enqueue() 沒有這個破口（前一筆已寫進 self.jobs），批次若只查
    「進入方法前的快照」就會漏掉——同一個 request 送出兩個相同 md5 的 job。
    """
    dup = "c" * 32
    out = worker.enqueue_many(
        [{"md5": dup, "title": "第一次"}, {"md5": dup, "title": "第二次"}],
        autostart=False,
    )

    assert len(out) == 2, "回傳長度必須與輸入等長（呼叫端靠位置對應）"
    assert out[0].job_id == out[1].job_id, "同一批內的重複必須指向同一個 job"
    assert len(worker.jobs) == 1, f"只該建立 1 個 job，實得 {len(worker.jobs)}"
    assert worker.queue.qsize() == 1, "佇列裡不得有兩份"
    assert save_spy.count == 1


def test_enqueue_many_preserves_all_fields(worker):
    """欄位必須完整帶進去——特別是 publication_year 的 None 語意。"""
    out = worker.enqueue_many([{
        "md5": "d" * 32,
        "title": "完整欄位",
        "authors": "某作者",
        "extension": "EPUB",
        "mirror_links": ["http://example.invalid/x"],
        "publication_year": 1999,
    }], autostart=False)

    job = out[0]
    assert job.title == "完整欄位"
    assert job.authors == "某作者"
    assert job.extension == "epub", "extension 必須正規化為小寫（與 DownloadJob 一致）"
    assert job.mirror_links == ["http://example.invalid/x"]
    assert job.publication_year == 1999


def test_enqueue_many_missing_year_stays_none_not_zero(worker):
    """缺 publication_year 必須保持 None，不得退化成 0。

    `DownloadJob` 的註解明寫：None（不知道）與 0（已查證確無）不同。
    """
    out = worker.enqueue_many(
        [{"md5": "e" * 32, "title": "沒有年份"}], autostart=False
    )
    assert out[0].publication_year is None


def test_enqueue_many_raises_on_missing_md5(worker, save_spy):
    """缺 md5 必須拋出，不得靜默略過。

    靜默略過會讓「送 N 筆回 N-1 筆」沒有任何訊號——那正是本 repo 反覆出現的
    「缺席態與失敗態共用同一個輸出」。
    """
    with pytest.raises(ValueError) as ei:
        worker.enqueue_many(
            [{"md5": "a" * 32, "title": "好的"}, {"title": "壞的，沒有 md5"}],
            autostart=False,
        )

    assert "items[1]" in str(ei.value), \
        f"錯誤訊息必須指出是第幾筆才能定位，實得：{ei.value}"


# ---------------------------------------------------------------------------
# 方向 3 — autostart 兩態（判準 2）
# ---------------------------------------------------------------------------

def test_enqueue_many_autostart_false_does_not_start(worker):
    """autostart=False：有 running loop 也不得啟動背景迴圈。"""
    async def scenario():
        assert asyncio.get_running_loop() is not None, \
            "控制組：此 context 必須真的有 running loop"

        worker.enqueue_many(_items(3), autostart=False)

        assert worker._worker_task is None, "autostart=False 不得建立背景迴圈"
        assert worker.queue.qsize() == 3, "但仍必須真的入列"

    asyncio.run(scenario())


def test_enqueue_many_autostart_true_starts_once(worker):
    """autostart=True：背景迴圈必須被建立，且只建一個。

    沒有這一條，一個「永遠不啟動」的實作會讓上一條通過。
    """
    async def scenario():
        worker.enqueue_many(_items(5), autostart=True)

        task = worker._worker_task
        assert task is not None, "autostart=True 必須建立背景迴圈"
        assert isinstance(task, asyncio.Task)

        assert await asyncio.wait_for(worker.stop(), timeout=5.0) is True
        assert task.done()

    asyncio.run(scenario())


def test_enqueue_many_default_autostart_matches_true(worker):
    """省略參數＝autostart=True（與 enqueue() 的預設一致）。"""
    async def scenario():
        worker.enqueue_many(_items(2))
        assert worker._worker_task is not None, "預設必須啟動（行為與 enqueue() 對齊）"

        assert await asyncio.wait_for(worker.stop(), timeout=5.0) is True

    asyncio.run(scenario())
