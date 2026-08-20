"""FR-20260820_234500 — 下載佇列中直接指定書籍歸屬的自訂書單。

本檔鎖住五件事，缺一不可：

1. **參數鏈五處都通**：`DownloadRequestItem` → `enqueue()` → `DownloadJob.__init__`
   → `to_dict()` → `_load_jobs_from_disk()`。漏掉後兩處任一 = 「存了但重啟後消失」，
   而且**不寫真往返測試就看不出來**（BR-20260820_131500 的 publication_year 是同一條鏈上的前例）。
2. **落地後自動歸戶**（R3）：work_id 產生後真的寫進 collection。
3. **fail loud**（AC5）：指定不存在的 collection_id 必須明確報錯，不得靜默略過。
4. **失敗的 job 不產生任何書單寫入**（AC6），且該斷言必須**有正向對照**——
   否則「0 次寫入」與「這條路徑根本沒被走到」共用同一個輸出。
5. **三態互斥**：沒指定 / 指定了待歸戶 / 指定了但寫入失敗，三者不得共用同一個輸出。
   這是本 repo 已重複踩過三次的失效類別（mirror-resolver 回 None、
   extension 三態收斂成一個 timeout、publication_year 缺值退化成 0）。
"""

import json
import shutil
import tempfile

import pytest

from app.crawler.download_worker import DownloadWorker, DownloadJob


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
    """建一筆**真的** work 列。

    不可用自編的假 work_id：`collection_item` 對 `work` 有 FOREIGN KEY 約束
    （app/db/schema.sql:126），假 id 會拋 IntegrityError，於是「歸戶邏輯壞掉」與
    「測試資料不合法」共用同一個輸出——正是本檔要消滋的那種歧義。
    """
    from app.models.catalog import WorkCreate
    return worker.pipeline.dao.create_work(WorkCreate(title=title, language="zh"))


# ---------------------------------------------------------------------------
# T1 — DownloadJob 攜帶 collection_ids 並序列化
# ---------------------------------------------------------------------------

def test_job_carries_collection_ids():
    job = DownloadJob(
        job_id="job_t1", md5="A" * 32, title="有書單的書",
        collection_ids=["col_favorites", "col_x"],
    )
    assert job.collection_ids == ["col_favorites", "col_x"]
    d = job.to_dict()
    assert d["collection_ids"] == ["col_favorites", "col_x"]


def test_job_without_collection_ids_is_empty_list_not_missing():
    """缺席態：沒指定就是空 list，且 key 必須存在。

    key 不存在的話，存檔再讀回會靜默掉值——與 publication_year 那案同一個病。
    """
    job = DownloadJob(job_id="job_t1b", md5="B" * 32, title="沒書單的書")
    assert job.collection_ids == []
    d = job.to_dict()
    assert "collection_ids" in d, "欄位必須存在，否則存檔再讀回會靜默掉值"
    assert d["collection_ids"] == []
    assert "collection_sync_error" in d
    assert d["collection_sync_error"] is None


# ---------------------------------------------------------------------------
# T2 — 真往返（R2，最容易漏的一環）
#
# 不是手工重建物件，而是：worker A 存檔 → **全新的 worker B** 指向同一個
# db_dir 讀回。模擬的就是服務重啟。
# ---------------------------------------------------------------------------

def test_collection_ids_survive_save_and_reload(temp_dir):
    worker_a = _make_worker(temp_dir)
    cid = _new_collection(worker_a, "重啟往返測試")
    job = worker_a.enqueue(
        md5="c" * 32, title="往返的書",
        collection_ids=[cid], autostart=False,
    )
    job_id = job.job_id

    # 確認真的落盤了（不是只存在記憶體）
    on_disk = json.loads(worker_a._jobs_file.read_text(encoding="utf-8"))
    assert any(j["job_id"] == job_id and j["collection_ids"] == [cid] for j in on_disk), (
        f"download_jobs.json 裡找不到帶 collection_ids 的記錄：{on_disk}"
    )

    # 重啟：全新 worker 實例從同一個目錄讀回
    worker_b = _make_worker(temp_dir)
    revived = worker_b.jobs.get(job_id)
    assert revived is not None, "重啟後 job 本身就不見了，往返測試失去意義"
    assert revived.collection_ids == [cid], (
        "重啟後 collection_ids 消失——to_dict() 有寫但 _load_jobs_from_disk() 沒讀回，"
        "這正是「存了但重啟後消失」那個缺陷"
    )


def test_collection_sync_error_survives_reload(temp_dir):
    """失敗訊號也必須跟著往返。

    掉了的話，重啟後一個歸戶失敗的 job 會復原成「看起來完全正常」——
    失敗態靜默退化成成功態。
    """
    worker_a = _make_worker(temp_dir)
    cid = _new_collection(worker_a, "失敗訊號往返")
    job = worker_a.enqueue(md5="d" * 32, title="帶錯的書",
                           collection_ids=[cid], autostart=False)
    job.collection_sync_error = "1/1 個書單寫入失敗：col_gone(測試用)"
    worker_a._save_jobs_to_disk()

    worker_b = _make_worker(temp_dir)
    revived = worker_b.jobs[job.job_id]
    assert revived.collection_sync_error == "1/1 個書單寫入失敗：col_gone(測試用)"


def test_reload_tolerates_legacy_json_without_the_key(temp_dir):
    """相容性（與上面兩條不同，這條測的不是往返）：

    舊格式 jobs.json 沒有這兩個 key，**不得把整個 worker 啟動炸掉**。
    """
    worker_a = _make_worker(temp_dir)
    legacy = [{
        "job_id": "job_legacy", "md5": "e" * 32, "title": "舊格式的書",
        "status": "paused", "progress_percent": 0,
    }]
    worker_a._jobs_file.write_text(json.dumps(legacy), encoding="utf-8")

    worker_b = _make_worker(temp_dir)
    revived = worker_b.jobs.get("job_legacy")
    assert revived is not None, "舊格式 jobs.json 把啟動炸掉了"
    assert revived.collection_ids == []
    assert revived.collection_sync_error is None


# ---------------------------------------------------------------------------
# T3 — work_id 產生後真的寫進 collection（R3）
# ---------------------------------------------------------------------------

def test_apply_collections_writes_to_db(worker):
    cid = _new_collection(worker, "歸戶目的地")
    wid = _new_work(worker, "已完成的書")
    job = worker.enqueue(md5="f" * 32, title="已完成的書",
                         collection_ids=[cid], autostart=False)
    job.work_id = wid
    job.status = "completed"

    worker._apply_collections(job)

    assert worker.pipeline.dao.get_work_collections(wid) == [cid]
    assert job.collection_sync_error is None


def test_completion_path_invokes_apply_collections():
    """控制流證據：`_apply_collections` 換在 work_id 產生之後。

    AC6（失敗的 job 不得寫入）靠的就是這個位置——它在 raise 之後才執行，
    所以失敗路徑根本走不到。位置被挪到上面或搼進另一個分支，這條會紅。
    """
    import inspect
    src = inspect.getsource(DownloadWorker._execute_download_with_resume)
    assert src.count("self._apply_collections(job)") == 1, "完成路徑上找不到歸戶呼叫"
    assert src.index('job.work_id = res["work_id"]') < src.index("self._apply_collections(job)"), (
        "歸戶呼叫跑在 work_id 產生之前，那時 work_id 還是 None"
    )


# ---------------------------------------------------------------------------
# T4 — fail loud（AC5）：指定不存在的 collection_id
# ---------------------------------------------------------------------------

def test_assign_unknown_collection_raises(worker):
    job = worker.enqueue(md5="a1" + "0" * 30, title="測 fail loud", autostart=False)

    with pytest.raises(ValueError) as exc:
        worker.assign_collections(job.job_id, ["col_does_not_exist"])

    assert "col_does_not_exist" in str(exc.value), "錯誤訊息必須指名是哪一個 cid，不能只說「有錯」"
    assert worker.jobs[job.job_id].collection_ids == [], "fail loud 後不得留下部分寫入的意圖"


def test_assign_partially_unknown_is_all_or_nothing(worker):
    """一真一假時也要整組拒絕。

    若只默默寫進真的那個，「指定了兩個」與「指定了兩個但只成功一個」就共用輸出。
    """
    cid_ok = _new_collection(worker, "真的書單")
    job = worker.enqueue(md5="a2" + "0" * 30, title="一真一假", autostart=False)

    with pytest.raises(ValueError):
        worker.assign_collections(job.job_id, [cid_ok, "col_ghost"])

    assert worker.jobs[job.job_id].collection_ids == []


def test_assign_unknown_job_returns_none_not_raise(worker):
    """「job 不存在」與「書單不存在」是不同的失敗，不得共用同一個輸出。

    前者回 None（route 轉 404），後者 raise ValueError（route 轉 422）。
    """
    assert worker.assign_collections("job_nonexistent", []) is None


# ---------------------------------------------------------------------------
# T5 — 失敗的 job 不得產生任何書單寫入（AC6）
#
# ⚠ 「寫入 0 次」與「這條路徑根本沒被走到」共用同一個輸出，
# 所以每一條 0 次斷言都**必須在同一個測試裡配一條正向對照**。
# ---------------------------------------------------------------------------

class _CountingDao:
    """包住真 dao，只數 add_work_to_collection 被呼叫幾次（保留真實副作用）。"""

    def __init__(self, real):
        self._real = real
        self.add_calls = []

    def add_work_to_collection(self, collection_id, work_id, notes=None):
        self.add_calls.append((collection_id, work_id))
        return self._real.add_work_to_collection(collection_id, work_id, notes)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_failed_job_writes_nothing_but_completed_job_does(worker):
    """負向（失敗）與正向（成功）在同一個測試裡對照。

    只寫負向那一半的話，一個「_apply_collections 永遠什麼都不做」的壞實作
    也會讓測試通過——那正是本檔檔頭警告的形狀。
    """
    counting = _CountingDao(worker.pipeline.dao)
    worker.pipeline.dao = counting
    cid = _new_collection(worker, "AC6 對照組")

    # ① 負向：下載失敗的 job（有指定書單，但沒有 work_id）
    failed_job = worker.enqueue(md5="b1" + "0" * 30, title="下載失敗的書",
                                collection_ids=[cid], autostart=False)
    failed_job.status = "failed"
    failed_job.error_message = "無法從可用鏡像節點取得有效直鏈下載 URL"
    worker._apply_collections(failed_job)

    assert counting.add_calls == [], (
        f"下載失敗的 job 竟然寫了書單：{counting.add_calls}"
    )

    # ② 正向對照：同一個 worker、同一個 cid，成功的 job 必須真的寫進去。
    #    這一半證明上面那個空 list 是「真的沒寫」而非「這條路徑壞掉了」。
    ok_job = worker.enqueue(md5="b2" + "0" * 30, title="下載成功的書",
                            collection_ids=[cid], autostart=False)
    ok_job.status = "completed"
    ok_job.work_id = "work_ac6_ok"
    worker._apply_collections(ok_job)

    assert len(counting.add_calls) == 1, (
        "正向對照組沒有寫入——上面的「0 次」不能證明 AC6，"
        "只能證明這條路徑整個壞掉了"
    )
    assert counting.add_calls[0] == (cid, "work_ac6_ok")


def test_no_collection_specified_writes_nothing_and_stays_silent(worker):
    """缺席態：沒指定書單 → 不寫入、且**不留失敗訊號**。

    「什麼都沒發生」本來就該是無聲的。這裡若寫了 collection_sync_error，
    就會讓「沒指定」看起來像「指定了但失敗」。
    """
    counting = _CountingDao(worker.pipeline.dao)
    worker.pipeline.dao = counting

    job = worker.enqueue(md5="b3" + "0" * 30, title="沒指定書單的書", autostart=False)
    job.status = "completed"
    job.work_id = "work_no_assign"
    worker._apply_collections(job)

    assert counting.add_calls == []
    assert job.collection_sync_error is None, "缺席態不得留下失敗訊號"


# ---------------------------------------------------------------------------
# T6 — 三態互斥：沒指定 / 指定了待歸戶 / 指定了但寫入失敗
# ---------------------------------------------------------------------------

class _ExplodingDao:
    """add_work_to_collection 一律炸掉，模擬「書單在寫入當下被刪或 DB 寫入失敗」。"""

    def __init__(self, real):
        self._real = real

    def add_work_to_collection(self, collection_id, work_id, notes=None):
        raise RuntimeError(f"FOREIGN KEY constraint failed ({collection_id})")

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_three_states_are_mutually_distinguishable(worker):
    """三態必須在 to_dict() 上就分得出來（UI 與存檔都靠它）。

    這是本 FR 最核心的一條：本 repo 已在同一種失效類別上踩過三次。
    """
    cid = _new_collection(worker, "三態測試")

    # A. 沒指定
    a = worker.enqueue(md5="c1" + "0" * 30, title="A 沒指定", autostart=False)

    # B. 指定了，尚未完成（待歸戶）
    b = worker.enqueue(md5="c2" + "0" * 30, title="B 待歸戶",
                       collection_ids=[cid], autostart=False)

    # C. 指定了，已完成，但寫入失敗
    c = worker.enqueue(md5="c3" + "0" * 30, title="C 寫入失敗",
                       collection_ids=[cid], autostart=False)
    c.status = "completed"
    c.work_id = "work_boom"
    worker.pipeline.dao = _ExplodingDao(worker.pipeline.dao)
    worker._apply_collections(c)

    da, db_, dc = a.to_dict(), b.to_dict(), c.to_dict()

    assert (da["collection_ids"], da["collection_sync_error"]) == ([], None)
    assert (db_["collection_ids"], db_["collection_sync_error"]) == ([cid], None)
    assert dc["collection_ids"] == [cid]
    assert dc["collection_sync_error"] is not None, "寫入失敗得到了與成功一模一樣的輸出"

    signatures = {
        (tuple(d["collection_ids"]), d["collection_sync_error"] is None)
        for d in (da, db_, dc)
    }
    assert len(signatures) == 3, f"三態沒有全部分開，實際只有 {len(signatures)} 種：{signatures}"


def test_sync_error_names_which_cid_failed(worker):
    """部分失敗時必須指名是哪幾個，不能只寫「有錯」。

    否則「全失敗」與「失敗一個」又共用同一個輸出。
    """
    cid1 = _new_collection(worker, "部分失敗 1")
    cid2 = _new_collection(worker, "部分失敗 2")
    wid = _new_work(worker, "一成一敗")
    real = worker.pipeline.dao

    class _HalfFailingDao:
        def add_work_to_collection(self, collection_id, work_id, notes=None):
            if collection_id == cid2:
                raise RuntimeError("disk full")
            return real.add_work_to_collection(collection_id, work_id, notes)

        def __getattr__(self, name):
            return getattr(real, name)

    job = worker.enqueue(md5="c4" + "0" * 30, title="一成一敗",
                         collection_ids=[cid1, cid2], autostart=False)
    job.status = "completed"
    job.work_id = wid
    worker.pipeline.dao = _HalfFailingDao()
    worker._apply_collections(job)

    err = job.collection_sync_error
    assert err is not None
    assert cid2 in err, f"錯誤訊息沒指名失敗的 cid：{err}"
    assert "1/2" in err, f"錯誤訊息沒區分「全失敗」與「失敗一個」：{err}"
    assert real.get_work_collections(wid) == [cid1], "成功的那一個應該真的寫進去了"
