"""BR-20260820_210000 F 節 — `/api/crawler/batch-download` 端點的批次入列鎖。

`4aa378a` 讓 worker 層有了 `enqueue_many`，但那一版是 **FIXED-UNDEPLOYED**：
route 層仍逐筆呼叫 `enqueue()`，線上一點差異都沒有。本檔鎖的正是那一格——
**端點真的走批次路徑**，而不只是「worker 有一個沒人呼叫的 API」。

⚠ 為什麼不用計時當判準：
    時間會因機器負載浮動，而「這台機器今天比較快」與「改成 O(N) 了」
    共用同一個輸出。這裡鎖的是**存檔次數**——它是離散的、可解釋的確切數字，
    逐筆是 N、批次是 1，沒有中間地帶。

⚠ 為什麼每條成本斷言都配一條正面證據：
    存檔 0 次的最快實作就是「什麼都不做」。只鎖次數會讓一個把整批丟掉的
    實作通過——那是 handler J 在 A 節踩過的形狀：「修好了」與「這條路徑
    沒被走到」共用同一個輸出。所以每一條都同時斷言 job 真的建出來了。
"""

import shutil
import tempfile

import pytest
from fastapi.testclient import TestClient

from app.api import crawler_routes
from app.crawler.download_worker import DownloadWorker
from app.main import app

# 夠大到讓「1 次」與「N 次」無法用巧合解釋，也夠小到測試跑得快。
BATCH_N = 40


def _make_worker(temp_dir: str) -> DownloadWorker:
    from app.db.dao import CatalogDAO
    from app.db.engine import DatabaseEngine
    from app.pipeline.ingest import IngestionPipeline
    from app.storage.manager import StorageManager

    storage = StorageManager(base_dir=temp_dir)
    engine = DatabaseEngine(db_path=storage.get_db_path())
    dao = CatalogDAO(engine=engine)
    return DownloadWorker(pipeline=IngestionPipeline(storage=storage, dao=dao))


class _SaveSpy:
    """數 `_save_jobs_to_disk` 被呼叫幾次，同時保留真實落盤行為。

    不是 mock 掉——若把落盤換成 no-op，就無法分辨「只存一次」與
    「存了但寫壞了」。真正呼叫原函式，只是額外計數。
    """

    def __init__(self, worker: DownloadWorker):
        self.count = 0
        self._orig = worker._save_jobs_to_disk
        worker._save_jobs_to_disk = self._wrapped

    def _wrapped(self, *a, **k):
        self.count += 1
        return self._orig(*a, **k)


@pytest.fixture
def worker_and_client():
    temp_dir = tempfile.mkdtemp()
    worker = _make_worker(temp_dir)
    # autostart 會嘗試建立背景 task 去打真實網路；本檔只驗入列成本，
    # 讓它啟動會讓測試去做真實下載（dispatcher 踩過：探針掛住 120s）。
    worker.start = lambda: None
    app.dependency_overrides[crawler_routes.get_worker] = lambda: worker
    try:
        with TestClient(app) as client:
            yield worker, client
    finally:
        app.dependency_overrides.pop(crawler_routes.get_worker, None)
        shutil.rmtree(temp_dir, ignore_errors=True)


def _items(n: int, offset: int = 0):
    return [
        {
            "md5": f"{i + offset:032x}",
            "title": f"Book {i + offset}",
            "authors": "Tester",
            "extension": "pdf",
            "mirror_links": [],
            "publication_year": 2020,
        }
        for i in range(n)
    ]


def test_batch_endpoint_saves_exactly_once(worker_and_client):
    """主鎖：N 筆只寫一次 download_jobs.json。

    這條若失敗且次數等於 N，代表 route 層被改回逐筆迴圈——
    worker 層的 enqueue_many 仍然完好，但線上又變回 O(N²)，
    且不會有任何錯誤訊號。
    """
    worker, client = worker_and_client
    spy = _SaveSpy(worker)

    res = client.post("/api/crawler/batch-download", json={"items": _items(BATCH_N)})

    assert res.status_code == 200, res.text
    # 正面證據：真的建了 N 個 job，不是把整批丟掉換來的「便宜」。
    assert res.json()["enqueued_count"] == BATCH_N
    assert len(worker.jobs) == BATCH_N
    assert spy.count == 1, (
        f"端點存檔 {spy.count} 次，應為 1。"
        f"若為 {BATCH_N} 則 route 層退回逐筆 enqueue()（O(N²) 復發）。"
    )


def test_per_item_enqueue_would_save_n_times_control(worker_and_client):
    """控制組：證明這支 spy 在該數到 N 時真的會數到 N。

    缺這條，`spy.count == 1` 可能只是因為 spy 根本沒接上——
    「只存一次」與「一次都沒偵測到」會共用同一個輸出。
    """
    worker, _client = worker_and_client
    spy = _SaveSpy(worker)

    for item in _items(10, offset=9000):
        worker.enqueue(autostart=False, **item)

    assert len(worker.jobs) == 10
    assert spy.count == 10, f"spy 沒有正確計數（得到 {spy.count}，應為 10）"


def test_batch_endpoint_still_skips_items_without_md5(worker_and_client):
    """行為鎖：缺 md5 的項仍靜默略過，不是整批 400。

    worker 的 `enqueue_many` 對缺 md5 會拋 ValueError；route 層刻意在
    傳入前過濾，讓 F 節的改動**只改成本不改行為**。
    這條鎖住那個刻意的選擇——若有人拿掉過濾，這裡會變成 500 而非 200。

    ⚠ 「送 N 筆回 N-1 筆沒有任何訊號」本身是既有缺陷，但那要另外決策
    （會影響既有前端）。本測試鎖的是「現況未被本次改動意外變更」，
    不是主張這個行為是對的。
    """
    worker, client = worker_and_client
    payload = _items(3, offset=100)
    payload[1]["md5"] = ""

    res = client.post("/api/crawler/batch-download", json={"items": payload})

    assert res.status_code == 200, res.text
    assert res.json()["enqueued_count"] == 2
    assert len(worker.jobs) == 2


def test_empty_batch_saves_zero_times(worker_and_client):
    """邊界：空批次不該寫檔。

    這條同時證明「1 次」不是一個恆定值——若實作無論如何都存一次，
    這裡會得到 1 而非 0，主鎖的 `== 1` 就失去鑑別力。
    """
    worker, client = worker_and_client
    spy = _SaveSpy(worker)

    res = client.post("/api/crawler/batch-download", json={"items": []})

    assert res.status_code == 200, res.text
    assert res.json()["enqueued_count"] == 0
    assert len(worker.jobs) == 0
    assert spy.count == 0, f"空批次存檔 {spy.count} 次，應為 0"
