import asyncio

import httpx
import pytest

from app.crawler.gutenberg_provider import (
    GUTENBERG_CATALOG_URL,
    GUTENBERG_LICENSE,
    GutenbergFetchError,
    GutenbergProvider,
    parse_catalog_csv,
)
from app.crawler.libgen_live import LibgenCrawler, make_work_id
from app.crawler.remote_catalog_refresh import RemoteCatalogRefresher
from app.db.dao import CatalogDAO
from app.db.engine import DatabaseEngine
from app.db.remote_catalog import RemoteCatalogDAO
from app.models.catalog import SearchResultItem


@pytest.fixture
def catalog(tmp_path):
    engine = DatabaseEngine(db_path=tmp_path / "remote-catalog.sqlite")
    CatalogDAO(engine=engine)
    return RemoteCatalogDAO(engine=engine), CatalogDAO(engine=engine)


def remote_item(md5: str, title: str):
    return {
        "md5": md5,
        "title": title,
        "authors_display": "遠端作者",
        "publication_year": 2026,
        "language": "zh",
        "format": "pdf_born_digital",
        "extension": "pdf",
        "size_bytes": 1234,
        "mirror_links": [f"https://example.test/{md5}"],
        "source": "libgen",
    }


def test_same_md5_upsert_twice_keeps_one_distinct_item(catalog):
    remote, _ = catalog
    item = remote_item("a" * 32, "第一版書名")

    assert remote.upsert_batch("cat_471", "python", [item]) == (1, 0)
    assert remote.upsert_batch("cat_471", "python", [{**item, "title": "新版書名"}]) == (0, 1)

    total, items = remote.query_browseable("cat_471", page=1, page_size=20)
    assert total == 1
    assert len(items) == 1
    assert items[0]["title"] == "新版書名"


def test_catalog_survives_dao_reopen_and_repeat_upsert_is_idempotent(tmp_path):
    db_path = tmp_path / "restart-persistence.sqlite"
    first_engine = DatabaseEngine(db_path=db_path)
    CatalogDAO(engine=first_engine)
    first_remote = RemoteCatalogDAO(engine=first_engine)
    item = remote_item("9" * 32, "重啟後仍存在")
    assert first_remote.upsert_batch("cat_471", "python", [item]) == (1, 0)

    reopened_engine = DatabaseEngine(db_path=db_path)
    reopened_remote = RemoteCatalogDAO(engine=reopened_engine)
    assert reopened_remote.upsert_batch("cat_471", "python", [item]) == (0, 1)
    total, items = reopened_remote.query_browseable("cat_471", page=1, page_size=20)

    assert total == 1
    assert [entry["md5"] for entry in items] == ["9" * 32]


def test_parent_count_is_distinct_union_across_child_categories(catalog):
    remote, local = catalog
    shared = remote_item("b" * 32, "跨分類同一本")
    unique = remote_item("c" * 32, "另一本文獻")

    remote.upsert_batch("cat_471", "programming", [shared])
    remote.upsert_batch("cat_472", "machine learning", [shared, unique])

    total, items = remote.query_browseable("cat_400", page=1, page_size=20)
    assert total == 2
    assert len(items) == 2
    assert local.get_category("cat_400").works_count == 2
    tree_parent = next(node for node in local.get_category_tree() if node.category_id == "cat_400")
    assert tree_parent.works_count == 2


def test_second_batch_absence_does_not_delete_first_batch(catalog):
    remote, _ = catalog
    first = remote_item("d" * 32, "先前看見")
    second = remote_item("e" * 32, "後來新增")

    remote.upsert_batch("cat_471", "python", [first])
    remote.upsert_batch("cat_471", "python", [second])

    total, items = remote.query_browseable("cat_471", page=1, page_size=20)
    assert total == 2
    assert {item["md5"] for item in items} == {"d" * 32, "e" * 32}


def non_libgen_item(source: str, source_native_id: str, title: str):
    """md5=NULL 的非 libgen item（DD-1/DD-2 反向控制組用）。"""
    return {
        "md5": None,
        "source": source,
        "source_native_id": source_native_id,
        "title": title,
        "authors_display": "遠端作者",
        "publication_year": 2026,
        "language": "en",
        "format": "epub",
        "extension": "epub",
        "size_bytes": 5678,
        "mirror_links": [],
    }


def test_null_md5_different_sources_are_not_collapsed(catalog):
    """tasks.md 1.3 反向控制組①：2 筆 md5=NULL、不同 (source, source_native_id)

    的 item，去重總數必須是 2，不得互撞。這是本次要修的核心缺陷——SQLite
    對多筆 NULL 不觸發 UNIQUE 衝突，若 identity 仍以 md5 為鍵，這兩筆會
    被靜默判定為同一本書。
    """
    remote, _ = catalog
    first = non_libgen_item("gutenberg", "1001", "Gutenberg 版《書 A》")
    second = non_libgen_item("openstax", "42", "OpenStax 版《書 B》")

    added1, updated1 = remote.upsert_batch("cat_471", "python", [first])
    added2, updated2 = remote.upsert_batch("cat_471", "python", [second])
    assert (added1, updated1) == (1, 0)
    assert (added2, updated2) == (1, 0)

    total, items = remote.query_browseable("cat_471", page=1, page_size=20)
    assert total == 2
    assert {item["title"] for item in items} == {
        "Gutenberg 版《書 A》",
        "OpenStax 版《書 B》",
    }


def test_same_composite_key_upsert_twice_keeps_one(catalog):
    """tasks.md 1.3 反向控制組②：同一個 (source, source_native_id) 兩次

    upsert，去重總數必須是 1（正確識別為同一本書並更新，而非新增第二筆）。
    """
    remote, _ = catalog
    item = non_libgen_item("gutenberg", "2701", "白鯨記 第一版")

    assert remote.upsert_batch("cat_471", "python", [item]) == (1, 0)
    assert remote.upsert_batch(
        "cat_471", "python", [{**item, "title": "白鯨記 修訂版"}]
    ) == (0, 1)

    total, items = remote.query_browseable("cat_471", page=1, page_size=20)
    assert total == 1
    assert len(items) == 1
    assert items[0]["title"] == "白鯨記 修訂版"


def test_missing_source_native_id_and_md5_is_rejected_not_run(catalog):
    """errors.md identity.upsert = not-run：呼叫端未帶 source_native_id 且無

    md5 可回退時，本筆必須被拒絕不寫入，不得靜默塞入一筆猜測資料。
    """
    remote, _ = catalog
    broken = non_libgen_item("gutenberg", "", "缺 ID 的書")
    broken["md5"] = None

    added, updated = remote.upsert_batch("cat_471", "python", [broken])
    assert (added, updated) == (0, 0)

    total, items = remote.query_browseable("cat_471", page=1, page_size=20)
    assert total == 0
    assert items == []


def test_failed_refresh_keeps_persisted_items(catalog):
    remote, _ = catalog
    persisted = [
        remote_item("f" * 32, "離線仍可瀏覽"),
        remote_item("6" * 32, "短刷新也不可遺失"),
    ]
    remote.upsert_batch("cat_471", "python", persisted)
    refresh_id = remote.begin_refresh("cat_471", "python")

    remote.finish_refresh(
        refresh_id,
        success=False,
        pages_fetched=0,
        items_seen=0,
        items_added=0,
        items_updated=0,
        error_message="offline",
    )

    total, items = remote.query_browseable("cat_471", page=1, page_size=20)
    status = remote.get_status("cat_471")
    assert total == 2
    assert {entry["md5"] for entry in items} == {"f" * 32, "6" * 32}
    assert status["status"] == "failed"
    assert status["last_success_at"] is None
    assert status["error"] == "offline"
    assert status["accumulated_total"] == 2


@pytest.mark.asyncio
async def test_search_page_exposes_cursor_without_inventing_provider_total(monkeypatch):
    crawler = LibgenCrawler(mirrors=["https://example.test"])
    observed = []

    async def fake_search_live_page(query, page_size=25, page=1):
        observed.append((query, page_size, page))
        return {"items": [remote_item("1" * 32, "單頁結果")], "raw_row_count": 1}

    monkeypatch.setattr(crawler, "_search_live_page", fake_search_live_page)
    result = await crawler.search_page("python", page=3, page_size=25)

    assert observed == [("python", 25, 3)]
    assert result["cursor"] == "3"
    assert result["provider_total"] is None
    assert "total" not in result


@pytest.mark.asyncio
async def test_refresh_follows_pages_and_reports_accumulated_total(catalog):
    remote, _ = catalog

    class PagedCrawler:
        def __init__(self):
            self.pages = []

        async def search_page(self, query, page=1, page_size=25):
            self.pages.append(page)
            if page == 1:
                return {
                    "items": [remote_item("2" * 32, "第一頁 A"), remote_item("3" * 32, "第一頁 B")],
                    "cursor": "1",
                    "next_page": 2,
                    "provider_total": None,
                }
            return {
                "items": [remote_item("4" * 32, "第二頁")],
                "cursor": "2",
                "next_page": None,
                "provider_total": None,
            }

    crawler = PagedCrawler()
    refresher = RemoteCatalogRefresher(remote, crawler, page_size=2, max_pages=5)
    await refresher.refresh("cat_471", "python")

    total, _ = remote.query_browseable("cat_471", page=1, page_size=20)
    status = remote.get_status("cat_471")
    assert crawler.pages == [1, 2]
    assert total == 3
    assert status["status"] == "fresh"
    assert status["accumulated_total"] == 3
    assert status["pages_fetched"] == 2


@pytest.mark.asyncio
async def test_raw_full_page_continues_after_one_row_is_filtered(catalog, monkeypatch):
    remote, _ = catalog
    crawler = LibgenCrawler(mirrors=["https://example.test"])
    observed_pages = []

    async def fake_search_live_page(query, page_size, page):
        observed_pages.append(page)
        if page == 1:
            return {
                "items": [remote_item(f"{index:032x}", f"第一頁 {index}") for index in range(24)],
                "raw_row_count": 25,
            }
        return {
            "items": [remote_item("8" * 32, "第二頁仍有資料")],
            "raw_row_count": 1,
        }

    monkeypatch.setattr(crawler, "_search_live_page", fake_search_live_page)
    refresher = RemoteCatalogRefresher(remote, crawler, page_size=25, max_pages=5)
    await refresher.refresh("cat_471", "python")

    total, items = remote.query_browseable("cat_471", page=1, page_size=30)
    assert observed_pages == [1, 2]
    assert total == 25
    assert any(item["title"] == "第二頁仍有資料" for item in items)


@pytest.mark.asyncio
async def test_http_exception_marks_refresh_failed_and_preserves_old_rows(catalog):
    remote, _ = catalog
    old_item = remote_item("7" * 32, "舊資料不可清空")
    remote.upsert_batch("cat_471", "python", [old_item])

    class FailingCrawler:
        async def search_page(self, query, page=1, page_size=25):
            request = httpx.Request("GET", "https://example.test/search")
            raise httpx.ConnectError("network unavailable", request=request)

    refresher = RemoteCatalogRefresher(remote, FailingCrawler())
    await refresher.refresh("cat_471", "python")

    total, items = remote.query_browseable("cat_471", page=1, page_size=20)
    status = remote.get_status("cat_471")
    assert total == 1
    assert [item["md5"] for item in items] == ["7" * 32]
    assert status["status"] == "failed"
    assert "network unavailable" in status["error"]


# === Gutenberg provider（tasks.md 2.1-2.5）===

CSV_HEADER = "Text#,Type,Issued,Title,Language,Authors,Subjects,LoCC,Bookshelves\n"
CSV_TWO_ROWS = CSV_HEADER + (
    "1,Text,1971-12-01,The Declaration of Independence,en,\"Jefferson, Thomas\",Politics,E201,Politics\n"
    "2701,Text,2001-06-01,Moby Dick,en,\"Melville, Herman\",Whaling,PS,Adventure\n"
)
# 合法但 0 筆的 catalog：只有表頭。這是 `empty`，**不是** failed。
CSV_HEADER_ONLY = CSV_HEADER


def _csv_transport(body: str, status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=body)

    return httpx.MockTransport(handler)


def test_gutenberg_catalog_url_is_the_official_documented_path():
    """2026-09-02 實證：此 URL 回 HTTP 200 + content-type text/csv（21,196,613 bytes）。

    同 host 的不存在路徑回 404（負向控制），故 200 不是「什麼都回 200」的假象。
    此測試鎖住字面值，避免日後被憑記憶改成猜測值。
    """
    assert GUTENBERG_CATALOG_URL == (
        "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv"
    )


def test_parse_catalog_csv_produces_source_scoped_identity_without_md5():
    """2.1 正向控制組：解析器對真的有東西的 CSV 必須回非空。

    這是判準①要求的正控制——沒有它，下面 `empty` 那條測試回 0 筆時，
    無法分辨是「CSV 真的沒東西」還是「解析器根本壞了永遠回 0」。
    """
    items = parse_catalog_csv(CSV_TWO_ROWS)

    assert len(items) == 2
    assert [item["source_native_id"] for item in items] == ["1", "2701"]
    assert {item["source"] for item in items} == {"gutenberg"}
    assert all(item["md5"] is None for item in items)
    # 1.4 的共用函式必須被重用，不得在 provider 內重刻一份格式。
    assert items[1]["work_id"] == make_work_id("gutenberg", "2701")
    assert items[1]["publication_year"] == 2001


def test_parse_catalog_csv_on_header_only_is_empty_not_an_error():
    """與上一條配對：同一支解析器對「只有表頭」回 0 筆且不丟例外。

    上一條證明它有東西時會回非空 ⇒ 這裡的 0 筆是真的 0 筆，不是壞掉。
    """
    assert parse_catalog_csv(CSV_HEADER_ONLY) == []


def test_gutenberg_license_is_usa_scoped_literal_not_a_generic_public_domain():
    """2.2：授權字面值必須逐字是 `Public domain in the USA.`。

    反向斷言同樣重要——若有人把它改成「公版」或 "public domain"，
    那會把一個有地域限制的授權暗示成全球通用，此測試必須紅。
    """
    assert GUTENBERG_LICENSE == "Public domain in the USA."
    assert GUTENBERG_LICENSE not in ("public domain", "Public Domain", "公版")
    assert "USA" in GUTENBERG_LICENSE


def test_api_model_exposes_gutenberg_license_and_leaves_libgen_blank(catalog):
    """2.2：授權必須在 API 回應可見（`SearchResultItem` 建得起來且帶字面值）。

    對照組是 libgen item：它沒有可宣告的授權，欄位必須是 None（空白），
    不得被套用預設公版——否則「未宣告」與「已確認公版」共用同一個輸出。
    """
    remote, _ = catalog
    remote.upsert_batch(
        "cat_471", "classics", parse_catalog_csv(CSV_TWO_ROWS)
    )
    remote.upsert_batch("cat_471", "classics", [remote_item("a" * 32, "libgen 書")])

    _, stored = remote.query_browseable("cat_471", page=1, page_size=20)
    api_items = [SearchResultItem(**row) for row in stored]
    by_source = {item.source: item for item in api_items}

    assert by_source["gutenberg"].license == "Public domain in the USA."
    assert by_source["libgen"].license is None


@pytest.mark.asyncio
async def test_gutenberg_fetch_failure_raises_and_is_not_an_empty_result():
    """2.3 判準①（失敗態）：下載失敗必須 raise，不得回一個空結果。

    與下一條 `empty` 測試成對——兩者若共用輸出，這兩條中必有一條紅。
    """

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unavailable", request=request)

    provider = GutenbergProvider(
        transport=httpx.MockTransport(boom),
        max_attempts=2,
        backoff_base_seconds=0.0,
    )
    with pytest.raises(GutenbergFetchError) as excinfo:
        await provider.fetch_catalog()
    assert "GUTENBERG_FETCH_FAILED" in str(excinfo.value)


@pytest.mark.asyncio
async def test_gutenberg_http_404_is_failed_not_silently_parsed_as_empty():
    """HTTP status 未 gate 時，一份 404 HTML 會被 csv 模組安靜解析成 0 筆，

    於是 `failed` 被降級成 `empty`。此測試鎖住那條 gate。
    """
    provider = GutenbergProvider(
        transport=_csv_transport("<html>not found</html>", status_code=404),
        max_attempts=1,
        backoff_base_seconds=0.0,
    )
    with pytest.raises(GutenbergFetchError):
        await provider.fetch_catalog()


@pytest.mark.asyncio
async def test_gutenberg_zero_rows_is_empty_outcome_not_failure():
    """2.3 判準①（缺席態）：CSV 抓到但 0 筆 ⇒ `empty`，且**不**丟例外。"""
    provider = GutenbergProvider(
        transport=_csv_transport(CSV_HEADER_ONLY),
        max_attempts=1,
        backoff_base_seconds=0.0,
    )
    result = await provider.fetch_catalog()

    assert result.outcome == "empty"
    assert result.items == []


@pytest.mark.asyncio
async def test_gutenberg_non_empty_catalog_is_ok_outcome():
    """正向控制組：同一支 provider 對有資料的 CSV 回 `ok` + 非空 items。

    沒有這一條，上面的 `empty` 可能只是 provider 永遠回空。
    """
    provider = GutenbergProvider(
        transport=_csv_transport(CSV_TWO_ROWS),
        max_attempts=1,
        backoff_base_seconds=0.0,
    )
    result = await provider.fetch_catalog()

    assert result.outcome == "ok"
    assert len(result.items) == 2


@pytest.mark.asyncio
async def test_refresher_records_failed_and_empty_with_distinguishable_rows(catalog):
    """2.3：`failed` 與 `empty` 在 refresh row 上必須可分辨。

    failed ⇒ status='failed' 且 error 含 GUTENBERG_FETCH_FAILED；
    empty  ⇒ status='fresh' 且 error 為 None。兩者若共用輸出，這條必紅。
    """
    remote, _ = catalog

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unavailable", request=request)

    failing = RemoteCatalogRefresher(
        remote,
        LibgenCrawler(mirrors=["https://example.test"]),
        gutenberg=GutenbergProvider(
            transport=httpx.MockTransport(boom),
            max_attempts=1,
            backoff_base_seconds=0.0,
        ),
    )
    assert await failing.refresh_gutenberg("cat_471", "classics") == "failed"
    failed_status = remote.get_status("cat_471")

    empty = RemoteCatalogRefresher(
        remote,
        LibgenCrawler(mirrors=["https://example.test"]),
        gutenberg=GutenbergProvider(
            transport=_csv_transport(CSV_HEADER_ONLY),
            max_attempts=1,
            backoff_base_seconds=0.0,
        ),
    )
    assert await empty.refresh_gutenberg("cat_471", "classics") == "empty"
    empty_status = remote.get_status("cat_471")

    assert failed_status["status"] == "failed"
    assert "GUTENBERG_FETCH_FAILED" in failed_status["error"]
    assert empty_status["status"] == "fresh"
    assert empty_status["error"] is None
    assert failed_status["status"] != empty_status["status"]


@pytest.mark.asyncio
async def test_refresher_without_gutenberg_provider_is_not_run(catalog):
    """errors.md `not-run`：未設定 provider ⇒ 本輪跳過，且不留下任何 refresh row。

    與 `empty` 的差別在這裡是可觀察的：not-run 連 refresh 紀錄都沒有
    （status 仍為 never_refreshed），empty 則有一筆 fresh。
    """
    remote, _ = catalog
    refresher = RemoteCatalogRefresher(
        remote, LibgenCrawler(mirrors=["https://example.test"])
    )

    assert await refresher.refresh_gutenberg("cat_471", "classics") == "not-run"
    assert remote.get_status("cat_471")["status"] == "never_refreshed"


@pytest.mark.asyncio
async def test_gutenberg_and_libgen_items_coexist_in_one_category(catalog):
    """2.4 端到端：兩個來源的 item 同時出現在同一分類的去重總數中。

    libgen 1 筆 + gutenberg 2 筆 = 3，且沒有任何一筆因 md5=NULL 被吃掉。
    """
    remote, _ = catalog
    refresher = RemoteCatalogRefresher(
        remote,
        LibgenCrawler(mirrors=["https://example.test"]),
        gutenberg=GutenbergProvider(
            transport=_csv_transport(CSV_TWO_ROWS),
            max_attempts=1,
            backoff_base_seconds=0.0,
        ),
    )
    remote.upsert_batch("cat_471", "classics", [remote_item("b" * 32, "libgen 那本")])

    assert await refresher.refresh_gutenberg("cat_471", "classics") == "ok"

    total, items = remote.query_browseable("cat_471", page=1, page_size=20)
    assert total == 3
    assert {item["source"] for item in items} == {"libgen", "gutenberg"}
    assert {item["title"] for item in items} == {
        "libgen 那本",
        "The Declaration of Independence",
        "Moby Dick",
    }


@pytest.mark.asyncio
async def test_gutenberg_refresh_is_idempotent_on_repeat(catalog):
    """2.4：同一份 catalog 刷兩次，總數仍為 2（複合鍵去重成立，md5 全 NULL）。"""
    remote, _ = catalog
    refresher = RemoteCatalogRefresher(
        remote,
        LibgenCrawler(mirrors=["https://example.test"]),
        gutenberg=GutenbergProvider(
            transport=_csv_transport(CSV_TWO_ROWS),
            max_attempts=1,
            backoff_base_seconds=0.0,
        ),
    )

    await refresher.refresh_gutenberg("cat_471", "classics")
    await refresher.refresh_gutenberg("cat_471", "classics")

    total, _items = remote.query_browseable("cat_471", page=1, page_size=20)
    assert total == 2


@pytest.mark.asyncio
async def test_gutenberg_concurrency_is_bounded_by_capacity_limiter():
    """2.5：以 mock transport 量測併發上界，證明「有節流」而非只是寫了個 limiter。

    正向控制組：limiter 容量 2、同時發 6 個請求 ⇒ 觀察到的同時在途數必須 <= 2。
    若把 limiter 拿掉，peak 會衝到 6，這條會紅——這就是它的判別力。
    """
    import anyio

    state = {"inflight": 0, "peak": 0}
    gate = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        state["inflight"] += 1
        state["peak"] = max(state["peak"], state["inflight"])
        await gate.wait()
        state["inflight"] -= 1
        return httpx.Response(200, text=CSV_TWO_ROWS)

    limiter = anyio.CapacityLimiter(2)
    providers = [
        GutenbergProvider(
            transport=httpx.MockTransport(slow),
            max_attempts=1,
            backoff_base_seconds=0.0,
            limiter=limiter,
        )
        for _ in range(6)
    ]
    tasks = [asyncio.create_task(p.fetch_catalog()) for p in providers]
    await asyncio.sleep(0.05)
    observed_peak_while_blocked = state["peak"]
    gate.set()
    results = await asyncio.gather(*tasks)

    assert observed_peak_while_blocked > 0, "控制組：必須真的有請求進到 transport"
    assert observed_peak_while_blocked <= 2
    assert state["peak"] <= 2
    assert all(r.outcome == "ok" for r in results)


@pytest.mark.asyncio
async def test_gutenberg_retries_with_exponential_backoff_before_failing():
    """2.5：指數退避——3 次嘗試之間的 sleep 秒數必須是 base * 2**n。"""
    attempts = {"n": 0}
    slept = []

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("boom", request=request)

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    provider = GutenbergProvider(
        transport=httpx.MockTransport(flaky),
        max_attempts=3,
        backoff_base_seconds=0.5,
        sleep=fake_sleep,
    )
    with pytest.raises(GutenbergFetchError):
        await provider.fetch_catalog()

    assert attempts["n"] == 3
    assert slept == [0.5, 1.0]


@pytest.mark.asyncio
async def test_gutenberg_recovers_on_second_attempt(catalog):
    """退避的正向控制組：第一次失敗、第二次成功 ⇒ `ok`。

    沒有這條，上面的 retry 測試無法排除「provider 永遠失敗」。
    """
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("transient", request=request)
        return httpx.Response(200, text=CSV_TWO_ROWS)

    provider = GutenbergProvider(
        transport=httpx.MockTransport(flaky),
        max_attempts=3,
        backoff_base_seconds=0.0,
    )
    result = await provider.fetch_catalog()

    assert calls["n"] == 2
    assert result.outcome == "ok"
    assert result.attempts == 2


@pytest.mark.asyncio
async def test_libgen_refresh_path_is_unchanged_by_provider_dispatch(catalog):
    """2.3 邊界：新增 provider 調度不得改動既有 libgen 刷新路徑的行為。

    帶著 gutenberg provider 建立 refresher 後跑 `refresh()`，libgen 結果
    必須與 Phase 1 完全相同，且 gutenberg provider 一次都沒被呼叫。
    """
    remote, _ = catalog
    touched = {"n": 0}

    def counting(request: httpx.Request) -> httpx.Response:
        touched["n"] += 1
        return httpx.Response(200, text=CSV_TWO_ROWS)

    class PagedCrawler:
        async def search_page(self, query, page=1, page_size=25):
            return {
                "items": [remote_item("5" * 32, "libgen 單頁")],
                "cursor": "1",
                "next_page": None,
                "provider_total": None,
            }

    refresher = RemoteCatalogRefresher(
        remote,
        PagedCrawler(),
        gutenberg=GutenbergProvider(
            transport=httpx.MockTransport(counting),
            max_attempts=1,
            backoff_base_seconds=0.0,
        ),
    )
    await refresher.refresh("cat_471", "python")

    total, items = remote.query_browseable("cat_471", page=1, page_size=20)
    assert total == 1
    assert [item["md5"] for item in items] == ["5" * 32]
    assert remote.get_status("cat_471")["status"] == "fresh"
    assert touched["n"] == 0


@pytest.mark.asyncio
async def test_gutenberg_mirror_links_do_not_match_libgen_download_markers():
    """design.md Critical Files：`mirror_resolver` 靠 URL 含 ads.php/get.php/md5=

    判斷 libgen 站。Gutenberg 直鏈不得誤觸這三個標記（誤判會把 PG 直鏈送進
    libgen 的解析分支）。此處只斷言字串性質，不觸碰 mirror_resolver.py。
    """
    items = parse_catalog_csv(CSV_TWO_ROWS)
    for item in items:
        for link in item["mirror_links"]:
            assert "ads.php" not in link
            assert "get.php" not in link
            assert "md5=" not in link


@pytest.mark.asyncio
async def test_schedule_allows_only_one_refresh_per_category(catalog):
    remote, _ = catalog
    release = asyncio.Event()

    class BlockingCrawler:
        async def search_page(self, query, page=1, page_size=25):
            await release.wait()
            return {"items": [], "cursor": str(page), "next_page": None, "provider_total": None}

    refresher = RemoteCatalogRefresher(remote, BlockingCrawler())
    assert refresher.schedule("cat_471", "python") is True
    assert refresher.schedule("cat_471", "python") is False

    tasks = list(refresher._tasks.values())
    release.set()
    await asyncio.gather(*tasks)
