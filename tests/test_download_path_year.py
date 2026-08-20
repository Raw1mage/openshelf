"""BR-20260820_131500 — 下載路徑必須把出版年份從搜尋帶到入庫。

上游（BR-20260820_130500）已修好「解析端能取出年份」與「ingest 端能寫入年份」兩頭，
本檔驗證中段五層真的帶得動這個值，且**缺值時仍是 None 而非 0 或空字串**。

語意約束（沿用上一包，不得在此包被推翻）：
    None = 不知道（上游沒給 / 解析不出來）
    int  = 已知的年份
0 不是「沒有年份」的表示法——若哪一層把 None 退化成 0，「上游沒給」與
「上游給了 0」就再次共用同一個輸出，正是本族 BR 要消滅的病。
"""

import json
import tempfile
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.crawler.download_worker import DownloadWorker, DownloadJob
from app.api.crawler_routes import DownloadRequestItem, BatchDownloadRequest


# ---------------------------------------------------------------------------
# 層 4/5 — DownloadJob 攜帶與序列化
# ---------------------------------------------------------------------------

def test_download_job_carries_publication_year():
    job = DownloadJob(
        job_id="job_a", md5="A" * 32, title="有年份的書",
        authors="Someone", extension="pdf", publication_year=1972,
    )
    assert job.publication_year == 1972
    assert job.to_dict()["publication_year"] == 1972


def test_download_job_without_year_is_none_not_zero():
    """負向：缺值必須是 None。0 會與「已查證確無」混淆。"""
    job = DownloadJob(job_id="job_b", md5="B" * 32, title="沒年份的書")
    assert job.publication_year is None
    d = job.to_dict()
    assert "publication_year" in d, "欄位必須存在，否則存檔再讀回會靜默掉值"
    assert d["publication_year"] is None
    assert d["publication_year"] != 0


def test_to_dict_roundtrip_preserves_year():
    """存檔再讀回不得掉值——to_dict 有輸出但 _load 沒讀，值會靜默消失。"""
    original = DownloadJob(
        job_id="job_c", md5="C" * 32, title="來回測試", publication_year=1989
    )
    revived = DownloadJob(
        job_id=original.to_dict()["job_id"],
        md5=original.to_dict()["md5"],
        title=original.to_dict()["title"],
        publication_year=original.to_dict()["publication_year"],
    )
    assert revived.publication_year == 1989


# ---------------------------------------------------------------------------
# 判準 4 — 舊格式 jobs.json 不得炸掉 worker 啟動
# ---------------------------------------------------------------------------

def _worker_with_jobs_file(temp_dir: str, payload: list) -> DownloadWorker:
    from app.storage.manager import StorageManager
    from app.db.engine import DatabaseEngine
    from app.db.dao import CatalogDAO
    from app.pipeline.ingest import IngestionPipeline

    storage = StorageManager(base_dir=temp_dir)
    engine = DatabaseEngine(db_path=storage.get_db_path())
    dao = CatalogDAO(engine=engine)
    pipeline = IngestionPipeline(storage=storage, dao=dao)

    worker = DownloadWorker(pipeline=pipeline)
    worker._jobs_file.parent.mkdir(parents=True, exist_ok=True)
    worker._jobs_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    worker.jobs.clear()
    worker._load_jobs_from_disk()
    return worker


_LEGACY_JOB = {
    "job_id": "job_legacy",
    "md5": "d" * 32,
    "title": "舊格式任務",
    "authors": "舊作者",
    "extension": "pdf",
    "mirror_links": [],
    "status": "completed",
    "progress_percent": 100,
}


def test_legacy_jobs_json_without_year_still_loads():
    """磁碟上既有的 jobs.json 沒有 publication_year 這個 key。

    若 _load_jobs_from_disk 寫成 item["publication_year"] 會 KeyError 炸掉整個
    worker 啟動——那是把一次欄位新增變成一次服務中斷。
    """
    temp_dir = tempfile.mkdtemp()
    try:
        assert "publication_year" not in _LEGACY_JOB, "控制組：這份 fixture 必須真的是舊格式"
        worker = _worker_with_jobs_file(temp_dir, [_LEGACY_JOB])
        assert "job_legacy" in worker.jobs, "舊格式任務必須仍載入得進來"
        loaded = worker.jobs["job_legacy"]
        assert loaded.title == "舊格式任務"
        assert loaded.publication_year is None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_new_jobs_json_with_year_loads_the_value():
    """控制組：證明上一條的 None 是「舊格式沒有」而非「這條路徑根本不讀」。"""
    temp_dir = tempfile.mkdtemp()
    try:
        modern = dict(_LEGACY_JOB, job_id="job_modern", publication_year=1987)
        worker = _worker_with_jobs_file(temp_dir, [modern])
        assert worker.jobs["job_modern"].publication_year == 1987
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 層 2 — API 請求模型
# ---------------------------------------------------------------------------

def test_request_model_accepts_year_and_defaults_to_none():
    with_year = DownloadRequestItem(md5="e" * 32, title="T", publication_year=1972)
    assert with_year.publication_year == 1972

    # 舊 client 不送這個欄位時必須仍能通過驗證（否則升級前端前所有下載都會 422）
    legacy = DownloadRequestItem(md5="f" * 32, title="T")
    assert legacy.publication_year is None


def test_request_model_accepts_explicit_null():
    """前端送的是 `?? null`，明確的 null 必須被接受而非 422。"""
    item = DownloadRequestItem.model_validate(
        {"md5": "0" * 32, "title": "T", "publication_year": None}
    )
    assert item.publication_year is None


def test_batch_request_carries_year_per_item():
    req = BatchDownloadRequest.model_validate(
        {
            "items": [
                {"md5": "1" * 32, "title": "有", "publication_year": 1995},
                {"md5": "2" * 32, "title": "無"},
            ]
        }
    )
    assert [i.publication_year for i in req.items] == [1995, None]


# ---------------------------------------------------------------------------
# 層 2→3 — enqueue 真的把值交給 job（含批次那條，最容易漏）
# ---------------------------------------------------------------------------

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


def test_enqueue_passes_year_through_and_keeps_none():
    temp_dir = tempfile.mkdtemp()
    try:
        worker = _make_worker(temp_dir)

        with_year = worker.enqueue(
            md5="3" * 32, title="有年份", extension="pdf", publication_year=1972
        )
        without = worker.enqueue(md5="4" * 32, title="沒年份", extension="pdf")

        assert with_year.publication_year == 1972
        assert without.publication_year is None
        # 控制組：兩者確實是不同的 job，否則上面兩條可能在斷言同一個物件
        assert with_year.job_id != without.job_id
        assert worker.get_job(with_year.job_id)["publication_year"] == 1972
        assert worker.get_job(without.job_id)["publication_year"] is None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 層 1→5 全鏈（HTTP 邊界，不含真實網路下載）
# ---------------------------------------------------------------------------

@pytest.fixture
def client_and_worker(monkeypatch):
    """HTTP 邊界測試：只驗欄位傳遞契約，不觸發真實下載。

    `enqueue()` 尾端無條件呼叫 `self.start()`；TestClient 底下有 running loop，
    於是會 `create_task(_process_queue())` 真的去打公網鏡像——測試會靜默掛住
    （零輸出，與「跑很久」無法區分）。這裡把背景迴圈停掉，讓斷言對準本包要證明的
    「值有沒有從 HTTP 傳到 job」，而不是網路。
    """
    import app.api.crawler_routes as cr
    from app.main import app as fastapi_app

    temp_dir = tempfile.mkdtemp()
    worker = _make_worker(temp_dir)
    monkeypatch.setattr(worker, "start", lambda: None)

    original = cr._worker
    cr._worker = worker
    try:
        yield TestClient(fastapi_app), worker
    finally:
        cr._worker = original
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_http_single_download_carries_year(client_and_worker):
    client, worker = client_and_worker
    res = client.post(
        "/api/crawler/download",
        json={"md5": "5" * 32, "title": "HTTP 有年份", "publication_year": 1972,
              "extension": "pdf", "mirror_links": []},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["publication_year"] == 1972
    assert worker.jobs[body["job_id"]].publication_year == 1972


def test_http_single_download_without_year_is_none(client_and_worker):
    """負向 + 回歸：舊 payload 不得 422，且不得被塞成 0。"""
    client, worker = client_and_worker
    res = client.post(
        "/api/crawler/download",
        json={"md5": "6" * 32, "title": "HTTP 沒年份", "extension": "pdf", "mirror_links": []},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["publication_year"] is None
    assert body["publication_year"] != 0


def test_http_batch_download_carries_year_per_item(client_and_worker):
    """批次那條是第二個 enqueue 呼叫點，最容易在改動時漏掉。"""
    client, worker = client_and_worker
    res = client.post(
        "/api/crawler/batch-download",
        json={
            "items": [
                {"md5": "7" * 32, "title": "批次有", "publication_year": 1989,
                 "extension": "pdf", "mirror_links": []},
                {"md5": "8" * 32, "title": "批次無", "extension": "pdf", "mirror_links": []},
            ]
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["enqueued_count"] == 2, "控制組：兩筆都必須真的入列，否則下面的斷言無意義"
    years = [j["publication_year"] for j in body["jobs"]]
    assert years == [1989, None]


# ---------------------------------------------------------------------------
# 層 5→ingest — metadata_override 的接縫
# ---------------------------------------------------------------------------

def test_job_metadata_override_shape_matches_ingest_contract():
    """download_worker 交給 ingest 的 dict 必須用 ingest 讀得懂的 key。

    這一格若寫成 `year` 或 `pub_year`，ingest 端的 metadata_override.get(
    "publication_year") 會永遠拿到 None——五層全接好了卻仍不通，而且不報錯。
    """
    from app.pipeline.ingest import IngestionPipeline

    job = DownloadJob(job_id="job_x", md5="9" * 32, title="接縫測試", publication_year=1972)
    metadata_override = {
        "title": job.title,
        "authors_display": job.authors or "未知作者",
        "publication_year": job.publication_year,
    }
    coerced = IngestionPipeline._coerce_publication_year(
        metadata_override.get("publication_year")
    )
    assert coerced == 1972

    empty_job = DownloadJob(job_id="job_y", md5="a" * 32, title="接縫負向")
    assert IngestionPipeline._coerce_publication_year(
        {"publication_year": empty_job.publication_year}.get("publication_year")
    ) is None
