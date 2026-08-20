"""FR-20260820_234500 補測 — 掛載點的**執行**證據與 HTTP 層的 `applied=true` 分支。

本檔補的是交件時明列的兩格未量項，兩格的共同病灶是同一個：
**「程式碼寫在正確位置」與「那行真的會被執行」共用了同一個輸出。**

1. **R3 掛載點執行證據**（`test_execution_path_*`）
   原本只有靜態位置證據（`src.index(work_id=) < src.index(_apply_collections)`）。
   那證明的是「這行寫在正確的位置」，不是「這行會被走到」——一個被 `return`
   提前跳出、或被包在永遠為假的分支裡的呼叫，靜態斷言完全看不出來。
   本檔起一個 **127.0.0.1 上的真 HTTP server**，讓 `_execute_download_with_resume()`
   的真身從頭跑到尾（重試迴圈 → Range 標頭 → 串流落檔 → part 檔 replace →
   process_file → work_id 指派 → 歸戶），全程不碰公網。

2. **R4 `applied=true` 分支**（`test_http_*`）
   交件時只驗過 `applied=false` 那半邊（全庫沒有 completed job）。
   「兩個分支回應不同」是設計，「它真的會走不同分支」要證明。
   `applied=false` 與 `applied=true` 若因某個 bug 恆回同一個值，
   只測一邊永遠看不出來——所以本檔**同一個測試裡同時取兩邊**做對照。

⚠ 為什麼不用 mock 掉 `_execute_download_with_resume`：
    mock 掉它就等於把要驗的東西換成自己寫的替身，剩下的斷言只是在驗替身。
    這裡只替換**外部世界**（下載 URL 指向本機、process_file 建真 work 列），
    被測函式本體一行都沒有換。
"""

import json
import shutil
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app.api import crawler_routes
from app.crawler.download_worker import DownloadWorker
from app.main import app

# 內容刻意不是空的：`_execute_download_with_resume` 有一條
# 「下載檔案為空 → raise」的檢查，空 body 會讓測試死在那裡而非死在歸戶。
PAYLOAD = b"%PDF-1.4\n% openshelf local fixture\n" + b"x" * 2048


def _make_worker(temp_dir: str) -> DownloadWorker:
    from app.db.dao import CatalogDAO
    from app.db.engine import DatabaseEngine
    from app.pipeline.ingest import IngestionPipeline
    from app.storage.manager import StorageManager

    storage = StorageManager(base_dir=temp_dir)
    engine = DatabaseEngine(db_path=storage.get_db_path())
    dao = CatalogDAO(engine=engine)
    return DownloadWorker(pipeline=IngestionPipeline(storage=storage, dao=dao))


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def worker(temp_dir):
    return _make_worker(temp_dir)


def _new_collection(worker, name: str) -> str:
    from app.models.catalog import CollectionCreate
    return worker.pipeline.dao.create_collection(CollectionCreate(name=name))


def _new_work(worker, title: str) -> str:
    """建一筆**真的** work 列（`collection_item` 對 `work` 有 FK 約束）。"""
    from app.models.catalog import WorkCreate
    return worker.pipeline.dao.create_work(WorkCreate(title=title, language="zh"))


# ---------------------------------------------------------------------------
# 本機 HTTP server：取代公網鏡像，讓下載器真身跑得完
# ---------------------------------------------------------------------------

class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler 的介面)
        body = PAYLOAD
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 靜音，避免污染 pytest 輸出
        pass


@pytest.fixture
def local_http_url():
    """起一個真的 HTTP server 在 127.0.0.1 的隨機埠上。

    用真 socket 而不是 mock httpx：被測函式裡的 Range 標頭組裝、狀態碼分支、
    `aiter_bytes` 串流落檔全都要真的跑過，mock 掉 client 就等於把這些跳過。
    """
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}/fixture.pdf"
    finally:
        srv.shutdown()
        srv.server_close()


def test_local_http_fixture_is_actually_reachable(local_http_url):
    """控制組：先證明這個 fixture server 真的在服務。

    沒有這一條的話，下面那個「下載成功」測試若因 server 沒起來而失敗，
    會被誤讀成「歸戶邏輯壞了」——外部世界壞掉與被測邏輯壞掉共用同一個輸出。
    """
    import urllib.request

    with urllib.request.urlopen(local_http_url, timeout=5) as resp:
        assert resp.status == 200
        assert resp.read() == PAYLOAD

    # 反向控制：關掉的埠必須連不上，證明上面那個 200 不是任何東西都會回 200
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()
    with pytest.raises(Exception):
        urllib.request.urlopen(f"http://127.0.0.1:{dead_port}/", timeout=2)


# ---------------------------------------------------------------------------
# ① R3 掛載點的**執行**證據
#
# 只替換「外部世界」兩格：
#   - resolver.resolve_download_url → 指向本機 fixture server（取代公網鏡像）
#   - pipeline.process_file        → 建一筆真的 work 列並回 work_id
#     （真身會呼叫 fitz 抽取文字，我們的 fixture 不是合法 PDF；替換它是為了
#      讓「抽取失敗」不與「歸戶沒被呼叫」共用同一個輸出。work 列是**真的**，
#      不是假 id——collection_item 對 work 有 FK 約束。）
# 被測函式 `_execute_download_with_resume` 本體一行都沒有換。
# ---------------------------------------------------------------------------

def _rig_worker_for_local_download(worker, url: str, work_id: str):
    """把 worker 的外部依賴接到本機，回傳一個記錄 process_file 是否被呼叫的 dict。"""
    calls = {"process_file": 0}

    async def _fake_resolve(md5, mirror_links=None):
        return url

    def _fake_process_file(file_path, metadata_override=None):
        calls["process_file"] += 1
        # 落地檔必須真的存在且非空，否則「下載器沒把檔寫出來」會被這層蓋掉
        assert file_path.exists(), f"process_file 收到不存在的檔案：{file_path}"
        assert file_path.stat().st_size == len(PAYLOAD), (
            f"落地檔大小不符：{file_path.stat().st_size} != {len(PAYLOAD)}"
        )
        return {"work_id": work_id}

    worker.resolver.resolve_download_url = _fake_resolve
    worker.pipeline.process_file = _fake_process_file
    return calls


@pytest.mark.asyncio
async def test_execution_path_reaches_apply_collections(worker, local_http_url):
    """R3 掛載點執行證據：走完真身後，書單真的被寫進 DB。

    這條與原本的靜態位置斷言（`src.index(...)`）互補而非重複：
    靜態證明「寫在正確的位置」，本條證明「那一行真的被走到」。
    一個被提前 return 跳過的呼叫，靜態斷言完全看不出來。
    """
    cid = _new_collection(worker, "執行路徑歸戶")
    wid = _new_work(worker, "真的被下載到的書")
    calls = _rig_worker_for_local_download(worker, local_http_url, wid)

    job = worker.enqueue(
        md5="e" * 32, title="真的被下載到的書",
        collection_ids=[cid], autostart=False,
    )

    await worker._execute_download_with_resume(job)

    # 前置條件：下載器真的跑完了（否則下面的斷言在驗一個沒發生的事）
    assert calls["process_file"] == 1, "process_file 沒被呼叫，下載器根本沒跑到落地那步"
    assert job.status == "completed", f"job 沒進 completed：{job.status} / {job.error_message}"
    assert job.work_id == wid

    # 本條的標的：歸戶真的發生了
    assert worker.pipeline.dao.get_work_collections(wid) == [cid], (
        "work_id 已產生但書單沒寫進去 —— 掛載點沒有被執行"
    )
    assert job.collection_sync_error is None


@pytest.mark.asyncio
async def test_execution_path_without_collections_writes_nothing(worker, local_http_url):
    """缺席態對照：同一條執行路徑、沒指定書單時不得產生任何寫入。

    沒有這條，上面那條的「寫進去了」可能來自某個把所有 work 都塞進預設書單的
    實作——「歸戶正確」與「無差別亂寫」會共用同一個輸出。
    """
    wid = _new_work(worker, "沒指定書單的書")
    calls = _rig_worker_for_local_download(worker, local_http_url, wid)

    job = worker.enqueue(md5="f" * 32, title="沒指定書單的書", autostart=False)
    await worker._execute_download_with_resume(job)

    assert calls["process_file"] == 1
    assert job.status == "completed"
    assert worker.pipeline.dao.get_work_collections(wid) == [], (
        "沒指定書單卻被寫進了某個書單"
    )
    assert job.collection_sync_error is None, "缺席態不得產生錯誤訊號"


@pytest.mark.asyncio
async def test_execution_path_download_failure_writes_nothing(worker):
    """AC6 的執行路徑版：下載失敗時，歸戶那行根本走不到。

    刻意不起 fixture server，讓 resolver 回一個連不上的位址。
    這條驗的是「控制流保證」而不是「if 判斷保證」：函式 raise 之後，
    `_apply_collections` 那一行在字面上就不會被執行到。
    """
    cid = _new_collection(worker, "失敗不該寫入")
    wid = _new_work(worker, "下載失敗的書")

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()

    async def _resolve_to_dead_port(md5, mirror_links=None):
        return f"http://127.0.0.1:{dead_port}/nope.pdf"

    called = {"process_file": 0}

    def _should_not_run(file_path, metadata_override=None):
        called["process_file"] += 1
        raise AssertionError("下載失敗卻走到了 process_file")

    worker.resolver.resolve_download_url = _resolve_to_dead_port
    worker.pipeline.process_file = _should_not_run

    job = worker.enqueue(
        md5="0a" + "1" * 30, title="下載失敗的書",
        collection_ids=[cid], autostart=False,
    )
    job.work_id = wid  # 最惡劣的情況：work_id 已經在了，只有控制流能擋住寫入

    with pytest.raises(Exception):
        await worker._execute_download_with_resume(job)

    assert called["process_file"] == 0
    assert worker.pipeline.dao.get_work_collections(wid) == [], (
        "下載失敗卻寫進了書單 —— AC6 被違反"
    )


# ---------------------------------------------------------------------------
# ② R4 `applied=true` 分支的 HTTP 層證據（AC3 + AC4）
#
# 交件時只驗過 `applied=false` 那半邊（全庫沒有 completed job）。
# 「兩個分支回應不同」是設計，「它真的會走不同分支」要證明——
# 若因某個 bug 恆回同一個值，只測一邊永遠看不出來。
# ---------------------------------------------------------------------------

@pytest.fixture
def worker_and_client(temp_dir):
    """TestClient + 共用同一個 temp DB 的 worker 與 collection routes。

    ⚠ `collection_routes.get_dao()` 預設會 `CatalogDAO()` —— 指向**正式** DB。
    不一起 override 的話，測試會在 A 庫寫入、到 B 庫查詢，於是「歸戶沒發生」
    與「查錯庫了」共用同一個 404——本檔要消滅的正是這種歧義。
    （這一格第一次跑時真的發生了：`GET /api/collections/{cid}` 回 404。）
    """
    from app.api import collection_routes

    worker = _make_worker(temp_dir)
    # autostart 會起背景 task 去打真實公網；本節只驗 HTTP 層行為。
    worker.start = lambda: None
    app.dependency_overrides[crawler_routes.get_worker] = lambda: worker
    app.dependency_overrides[collection_routes.get_dao] = lambda: worker.pipeline.dao
    try:
        with TestClient(app) as client:
            yield worker, client
    finally:
        app.dependency_overrides.pop(crawler_routes.get_worker, None)
        app.dependency_overrides.pop(collection_routes.get_dao, None)


def test_http_applied_true_vs_false_are_different_branches(worker_and_client):
    """AC4：同一個端點、兩種 job 狀態，回應必須不同。

    兩邊**同一個測試裡取**，因為這條鍵的不是「某一邊的值對不對」，
    而是「兩邊不得共用同一個輸出」。分開寫成兩個測試的話，一個恆回
    `applied=false` 的壞實作只會死一條，而那一條的訊息不會指向真正的病因。
    """
    worker, client = worker_and_client
    cid = _new_collection(worker, "HTTP 分支對照")
    wid = _new_work(worker, "已完成的書")

    pending_job = worker.enqueue(md5="a1" + "0" * 30, title="還沒下完的書", autostart=False)
    done_job = worker.enqueue(md5="a2" + "0" * 30, title="已完成的書", autostart=False)
    done_job.status = "completed"
    done_job.work_id = wid

    r_pending = client.post(
        f"/api/crawler/jobs/{pending_job.job_id}/collections",
        json={"collection_ids": [cid]},
    )
    r_done = client.post(
        f"/api/crawler/jobs/{done_job.job_id}/collections",
        json={"collection_ids": [cid]},
    )

    assert r_pending.status_code == 200, r_pending.text
    assert r_done.status_code == 200, r_done.text
    p, d = r_pending.json(), r_done.json()

    # 這才是本條的標的：兩個分支真的不同
    assert p["applied"] is False and p["pending"] is True, p
    assert d["applied"] is True and d["pending"] is False, d
    assert p["work_id"] is None
    assert d["work_id"] == wid
    assert p["applied"] != d["applied"], "兩個分支回了同一個值，共用了輸出"


def test_http_applied_true_actually_writes_to_collection(worker_and_client):
    """AC3：`applied=true` 不只是一個旗標，書真的進了書單。

    三條證據都走 HTTP，不直接讀 dao：本格要驗的就是「使用者那邊看得到」。
    """
    worker, client = worker_and_client
    cid = _new_collection(worker, "AC3 HTTP 實測")
    wid = _new_work(worker, "會被寫進書單的書")

    # 前置對照：一開始書單是空的（否則「本來就在裡面」會冒充成「寫進去了」）
    before = client.get(f"/api/collections/{cid}")
    assert before.status_code == 200, before.text
    assert before.json()["items"] == [], before.json()

    job = worker.enqueue(md5="a3" + "0" * 30, title="會被寫進書單的書", autostart=False)
    job.status = "completed"
    job.work_id = wid

    r = client.post(f"/api/crawler/jobs/{job.job_id}/collections", json={"collection_ids": [cid]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] is True, body
    assert body["work_id"] == wid
    assert body["collection_sync_error"] is None, body

    # AC3：書單 items 真的多了那本書
    after = client.get(f"/api/collections/{cid}")
    assert after.status_code == 200, after.text
    assert [it["work_id"] for it in after.json()["items"]] == [wid], after.json()

    # AC4：反向查詢也看得到
    status = client.get(f"/api/collections/work/{wid}/status")
    assert status.status_code == 200, status.text
    assert cid in status.json(), status.json()


def test_http_missing_collection_id_fails_loud(worker_and_client):
    """AC5 的 HTTP 層：不存在的 cid 回 422 且指名，不得靜默略過。

    控制組：同一個 job 用合法 cid 必須回 200——否則那個 422 可能只是
    「這個端點壞了」，與「它正確地拒絕了壞輸入」共用同一個輸出。
    """
    worker, client = worker_and_client
    good_cid = _new_collection(worker, "AC5 控制組")
    job = worker.enqueue(md5="a4" + "0" * 30, title="AC5", autostart=False)

    bad = client.post(
        f"/api/crawler/jobs/{job.job_id}/collections",
        json={"collection_ids": ["col_does_not_exist_zzz"]},
    )
    assert bad.status_code == 422, bad.text
    assert "col_does_not_exist_zzz" in bad.json()["detail"], bad.json()

    good = client.post(
        f"/api/crawler/jobs/{job.job_id}/collections",
        json={"collection_ids": [good_cid]},
    )
    assert good.status_code == 200, good.text

    # 失敗的那次不得留下半完成的狀態
    assert worker.jobs[job.job_id].collection_ids == [good_cid]


def test_http_unknown_job_is_404_not_422(worker_and_client):
    """「job 不存在」與「書單不存在」必須是不同狀態碼。

    兩者共用同一個碼的話，前端無法分辨該重新載佇列還是該重載書單清單。
    """
    worker, client = worker_and_client
    cid = _new_collection(worker, "404 對照")
    r = client.post("/api/crawler/jobs/job_never_existed/collections",
                    json={"collection_ids": [cid]})
    assert r.status_code == 404, r.text
