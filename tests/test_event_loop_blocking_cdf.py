"""BR-20260820_210000 C/D/F 節回歸測試 —— `async def` 路徑不得在事件迴圈執行緒上做同步 I/O。

判準刻意不用「grep 原始碼有無 to_thread」（缺席態與失敗態共用輸出的形狀），
改成行為判準：斷言同步 I/O 確實發生在**非事件迴圈執行緒**上。
每個測試都自帶控制組，證明偵測器在該壞掉時真的會失敗。
"""
import asyncio
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.api.routes import get_pipeline
from app.api import settings_routes as SR
from app.crawler.libgen_live import LibgenCrawler
from app.crawler.validator import MirrorValidator


# ==========================================================================
# C 節：upload_book → pipeline.ingest_bytes（落檔 + PyMuPDF 抽取 + 多次 DB 寫）
# ==========================================================================

class _SpyPipeline:
    """記錄 ingest_bytes 是在哪個執行緒被呼叫的。"""

    def __init__(self):
        self.calls = 0
        self.thread_ids = []
        self.kwargs = []

    def ingest_bytes(self, data, filename, custom_title=None, custom_author=None):
        self.calls += 1
        self.thread_ids.append(threading.get_ident())
        self.kwargs.append(
            {"len": len(data), "filename": filename,
             "custom_title": custom_title, "custom_author": custom_author}
        )
        return {
            "work_id": "wk_probe", "title": custom_title or "probe",
            "title_provenance": "user_edited", "work_type": "book",
            "language": "en", "authors_display": custom_author,
            "publication_year": None, "availability_tier": 0,
            "created_at": "2026-08-20T00:00:00+00:00",
            "updated_at": "2026-08-20T00:00:00+00:00",
            "identifiers": [], "manifestations": [], "reading_state": None,
        }


def test_upload_book_offloads_ingest_bytes_from_event_loop():
    """C 節：ingest_bytes 必須在非事件迴圈執行緒上執行。

    控制組（同一個測試內）：TestClient 的 portal 執行緒 id 由一條 async 路徑取得——
    若受測執行緒 id 與它相同，代表 threadpool hop 沒生效，斷言會抓到。
    """
    spy = _SpyPipeline()
    app.dependency_overrides[get_pipeline] = lambda: spy

    loop_thread_holder = {}

    @app.get("/__probe_loop_thread__")
    async def _probe():                                   # pragma: no cover - 測試輔助
        loop_thread_holder["tid"] = threading.get_ident()
        return {"ok": True}

    try:
        with TestClient(app) as client:
            # 控制組：取得事件迴圈執行緒 id，並證明它真的取得到
            r0 = client.get("/__probe_loop_thread__")
            assert r0.status_code == 200
            loop_tid = loop_thread_holder.get("tid")
            assert loop_tid is not None, "控制組失效：取不到 loop 執行緒 id，偵測器無鑑別力"

            r = client.post(
                "/api/upload",
                files={"file": ("probe.pdf", b"%PDF-1.4 probe bytes", "application/pdf")},
                data={"custom_title": "probe title"},
            )

        assert r.status_code == 200
        assert spy.calls == 1, "ingest_bytes 必須被實際呼叫到（不得靠短路規避）"
        assert spy.thread_ids[0] != loop_tid, (
            "C 節未修復：pipeline.ingest_bytes 仍在事件迴圈執行緒上執行"
        )
        # 參數必須原樣傳遞——搬進 threadpool 不得順手改語意
        assert spy.kwargs[0]["filename"] == "probe.pdf"
        assert spy.kwargs[0]["custom_title"] == "probe title"
        assert spy.kwargs[0]["custom_author"] is None
        assert spy.kwargs[0]["len"] == len(b"%PDF-1.4 probe bytes")
    finally:
        app.dependency_overrides.pop(get_pipeline, None)
        app.router.routes = [
            rt for rt in app.router.routes
            if getattr(rt, "path", None) != "/__probe_loop_thread__"
        ]


def test_upload_book_rejects_empty_file_before_threadpool():
    """空檔仍必須在 400 就擋掉——搬進 threadpool 不得吞掉既有的前置驗證。"""
    spy = _SpyPipeline()
    app.dependency_overrides[get_pipeline] = lambda: spy
    try:
        with TestClient(app) as client:
            r = client.post("/api/upload", files={"file": ("empty.pdf", b"", "application/pdf")})
        assert r.status_code == 400
        assert spy.calls == 0, "空檔不得進入 ingest_bytes"
    finally:
        app.dependency_overrides.pop(get_pipeline, None)


# ==========================================================================
# D 節：validate_libgen_mirror 的 DB 存取 + validator 的解析與 BR 落檔
# ==========================================================================

class _ThreadRecordingDAO:
    def __init__(self):
        self.read_threads = []
        self.write_threads = []
        self.saved = None

    def get_libgen_mirrors(self):
        self.read_threads.append(threading.get_ident())
        return [{"url": "https://probe.example.org", "enabled": True, "note": "",
                 "is_default": False, "priority": 1}]

    def save_libgen_mirrors(self, mirrors):
        self.write_threads.append(threading.get_ident())
        self.saved = mirrors

    @staticmethod
    def current_iso():
        return "2026-08-20T00:00:00+00:00"


class _StubValidator:
    """回傳固定 report，讓本測試專注在路由層的 DB 存取執行緒。"""

    def __init__(self, report):
        self._report = report

    async def validate_mirror(self, url, auto_dispatch_br=True):
        return self._report


def test_validate_mirror_route_offloads_db_access():
    """D 節：路由層的 get/save_libgen_mirrors 必須離開事件迴圈執行緒。"""
    from app.models.catalog import LibgenMirrorValidationReport

    dao = _ThreadRecordingDAO()
    report = LibgenMirrorValidationReport(
        url="https://probe.example.org", is_online=True, status_code=200,
        latency_ms=1.0, validation_status="verified", adapter_type="libgen_li",
        sample_records_count=3, error_message=None,
    )
    app.dependency_overrides[SR.get_dao] = lambda: dao
    app.dependency_overrides[SR.get_validator] = lambda: _StubValidator(report)

    loop_thread_holder = {}

    @app.get("/__probe_loop_thread_d__")
    async def _probe():                                   # pragma: no cover - 測試輔助
        loop_thread_holder["tid"] = threading.get_ident()
        return {"ok": True}

    try:
        with TestClient(app) as client:
            r0 = client.get("/__probe_loop_thread_d__")
            assert r0.status_code == 200
            loop_tid = loop_thread_holder.get("tid")
            assert loop_tid is not None, "控制組失效：取不到 loop 執行緒 id"

            r = client.post(
                "/api/settings/libgen-mirrors/validate",
                json={"url": "https://probe.example.org", "auto_dispatch_br": False},
            )

        assert r.status_code == 200
        assert len(dao.read_threads) == 1, "DB 讀路徑必須被實際執行到"
        assert len(dao.write_threads) == 1, "DB 寫路徑必須被實際執行到"
        assert dao.read_threads[0] != loop_tid, "D 節未修復：get_libgen_mirrors 仍在 loop 執行緒上"
        assert dao.write_threads[0] != loop_tid, "D 節未修復：save_libgen_mirrors 仍在 loop 執行緒上"
        # 回寫語意不得改變：既有 mirror 應被就地更新為 verified/enabled
        assert dao.saved is not None and len(dao.saved) == 1
        assert dao.saved[0]["validation_status"] == "verified"
        assert dao.saved[0]["enabled"] is True
    finally:
        app.dependency_overrides.pop(SR.get_dao, None)
        app.dependency_overrides.pop(SR.get_validator, None)
        app.router.routes = [
            rt for rt in app.router.routes
            if getattr(rt, "path", None) != "/__probe_loop_thread_d__"
        ]


@pytest.mark.asyncio
async def test_validator_parse_and_br_write_leave_event_loop_thread(tmp_path, monkeypatch):
    """D 節：validator 內的 BeautifulSoup 解析與 BR 落檔必須離開 loop 執行緒。

    這一格是 D 節的主阻塞源——實測單次 validate 的 loop lag 由 556.7ms 降到 47.1ms，
    修 route 層的 DB 存取完全沒有動到它（DB 那格只有 0.x ms 量級）。
    """
    import httpx
    import app.crawler.validator as vmod

    loop_tid = threading.get_ident()
    parse_threads = []
    write_threads = []

    rows = "".join(
        "<tr>" + "".join(f"<td>c{i}-{c}</td>" for c in range(9)) + "</tr>"
        for i in range(5)
    )
    html = f"<html><body><table id='tablelibgen'><tbody>{rows}</tbody></table></body></html>"

    class _T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, html=html, request=request)

    class _Shim:
        def AsyncClient(self, *a, **kw):
            kw["transport"] = _T()
            return httpx.AsyncClient(*a, **kw)

        def __getattr__(self, name):
            return getattr(httpx, name)

    monkeypatch.setattr(vmod, "httpx", _Shim())

    validator = MirrorValidator(issues_dir=tmp_path / "issues")

    real_parse_li = validator.crawler._parse_libgen_li_html
    real_parse_is = validator.crawler._parse_libgen_is_html
    real_dispatch = validator.dispatch_br

    def spy_li(*a, **kw):
        parse_threads.append(threading.get_ident())
        return real_parse_li(*a, **kw)

    def spy_is(*a, **kw):
        parse_threads.append(threading.get_ident())
        return real_parse_is(*a, **kw)

    def spy_dispatch(*a, **kw):
        write_threads.append(threading.get_ident())
        return real_dispatch(*a, **kw)

    validator.crawler._parse_libgen_li_html = spy_li
    validator.crawler._parse_libgen_is_html = spy_is
    validator.dispatch_br = spy_dispatch

    # 控制組：同步直接呼叫必定落在 loop 執行緒——證明比對 thread id 有鑑別力
    _ = spy_li(html, "https://probe.example.org")
    assert parse_threads[-1] == loop_tid, "控制組失效：同步呼叫竟不在 loop 執行緒上"
    parse_threads.clear()

    report = await validator.validate_mirror("https://probe.example.org", auto_dispatch_br=True)

    assert len(parse_threads) >= 1, "解析路徑必須被實際執行到（不得靠短路規避）"
    for tid in parse_threads:
        assert tid != loop_tid, "D 節未修復：BeautifulSoup 解析仍在事件迴圈執行緒上"

    # 這份 HTML 沒有 md5，會走到 incompatible_layout 分支並自動發 BR
    assert report.validation_status == "incompatible_layout"
    assert len(write_threads) == 1, "dispatch_br 必須被實際執行到"
    assert write_threads[0] != loop_tid, "D 節未修復：BR 落檔 write_text 仍在事件迴圈執行緒上"
    assert report.dispatched_br is True
    assert (tmp_path / "issues").exists()
    assert len(list((tmp_path / "issues").glob("BR-*.md"))) == 1


# ==========================================================================
# F 節：list_dispatched_issues 的 threadpool 佔用時間
# ==========================================================================

def test_list_dispatched_issues_reads_only_first_line(tmp_path, monkeypatch):
    """F 節：不得為了取標題把整份 BR 全文讀進記憶體。

    本函式是 sync def（不阻塞 loop），但它佔用的是全 app 共用的 40 個
    threadpool 名額。加一次 threadpool hop 只是換一個 token、佔用數不變；
    真正的修法是縮短單次佔用時間——所以判準是「讀取量」而非「執行緒」。
    """
    issues = tmp_path / "issues"
    issues.mkdir()
    big = "x" * 200_000
    (issues / "BR-20260820_a.md").write_text(f"# first title\n{big}", encoding="utf-8")
    (issues / "BR-20260820_b.md").write_text(f"# second title\n{big}", encoding="utf-8")
    (issues / "not-a-br.md").write_text("# ignored\n", encoding="utf-8")

    read_bytes = {"n": 0}
    real_open = open

    class _SpyFile:
        def __init__(self, fh):
            self._fh = fh

        def readline(self, *a, **kw):
            s = self._fh.readline(*a, **kw)
            read_bytes["n"] += len(s)
            return s

        def read(self, *a, **kw):
            s = self._fh.read(*a, **kw)
            read_bytes["n"] += len(s)
            return s

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()
            return False

    def spy_open(path, *a, **kw):
        return _SpyFile(real_open(path, *a, **kw))

    monkeypatch.setattr(SR, "open", spy_open, raising=False)
    monkeypatch.setattr(
        SR, "Path",
        type("_P", (), {"__new__": staticmethod(lambda cls, *a, **kw: tmp_path / "app" / "api" / "x.py")}),
    )

    out = SR.list_dispatched_issues()

    assert out["total"] == 2, "只有 BR-*.md 應被列入（控制組：not-a-br.md 必須被排除）"
    titles = {i["title"] for i in out["issues"]}
    assert titles == {"first title", "second title"}
    # 控制組：確實有讀到東西（否則 read_bytes==0 會與「沒讀全文」共用同一個輸出）
    assert read_bytes["n"] > 0, "控制組失效：一個 byte 都沒讀到，本判準無鑑別力"
    assert read_bytes["n"] < 10_000, (
        f"F 節未修復：讀了 {read_bytes['n']} bytes，代表仍在讀整份 BR 全文"
        f"（兩份檔案各 200KB）"
    )
