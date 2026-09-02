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
from app.crawler.openlibrary_bridge import (
    OL_FIELDS,
    OL_SEARCH_URL,
    OpenLibraryBridge,
    build_query,
    extract_bridge_fields,
)
from app.crawler.openstax_provider import (
    OPENSTAX_API_URL,
    OPENSTAX_FIELDS,
    OpenStaxFetchError,
    OpenStaxProvider,
    parse_books_payload,
)
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


# === Open Library 橋接層（tasks.md 3.1-3.3）===

# 2026-09-02 實測回應形狀（依 `q=isbn:9780553213119` 真實回應裁剪）。
OL_HIT_PAYLOAD = {
    "numFound": 1,
    "docs": [
        {
            "key": "/works/OL102749W",
            "title": "Moby Dick",
            "ebook_access": "public",
            "ia": ["mobydickorthewha02701gut"],
            "isbn": ["9780553213119", "0553213113"],
            "oclc": ["12345678"],
            "lccn": ["77012345"],
            "id_project_gutenberg": ["2701"],
        }
    ],
}
# 2026-09-02 實測：`q=title:zzqqxxjjvvwwkk9987654` 回的就是這個形狀。
OL_EMPTY_PAYLOAD = {"numFound": 0, "docs": []}


def _ol_transport(payload, status_code: int = 200, *, capture=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        if isinstance(payload, str):
            return httpx.Response(status_code, text=payload)
        return httpx.Response(status_code, json=payload)

    return httpx.MockTransport(handler)


def _seed_item(remote, title="Moby Dick", native_id="2701"):
    remote.upsert_batch(
        "cat_471",
        "classics",
        [non_libgen_item("gutenberg", native_id, title)],
    )
    pending = remote.list_items_needing_ol_enrichment("cat_471")
    return pending


def test_ol_endpoint_and_fields_match_the_probed_contract():
    """3.1：API 端點與 fields 清單鎖字面值（2026-09-02 實測驗證）。

    實測：`GET https://openlibrary.org/search.json?fields=<下列>&limit=2
    &q=isbn:9780553213119` → HTTP 200、numFound=1。DD-4 明列一次查詢回 6 方
    對映，這一條防止日後有人憑記憶改成別的欄位集而少拿回東西。
    """
    assert OL_SEARCH_URL == "https://openlibrary.org/search.json"
    assert OL_FIELDS == (
        "key,title,ia,ebook_access,isbn,oclc,lccn,id_project_gutenberg"
    )


def test_build_query_prefers_isbn_then_title_and_returns_none_without_clues():
    """3.1：查詢字串組法。回 None = 「沒線索可查」，不是失敗。

    正向控制（有線索必須回非 None）與反向控制（無線索必須回 None）在同一條，
        否則一個恆回 None 的實作也能讓反向斷言單獨通過。
    """
    assert build_query(isbn="978-0-553-21311-9") == "isbn:9780553213119"
    assert build_query(title="Moby Dick") == "title:Moby Dick"
    assert build_query(title="Moby Dick", authors="Melville") == (
        "title:Moby Dick author:Melville"
    )
    assert build_query() is None
    assert build_query(title="未知書名") is None


def test_extract_bridge_fields_takes_first_of_each_array():
    """3.2：OL 的 isbn/oclc/lccn 是**陣列**（DD-1 已記載非一對一）。

    正向控制：有命中時必須抽出 5 格；反向控制：空 docs 必須回空 dict。
    """
    # 正向控制：同一支 list_items_needing_ol_enrichment 對「真的沒 enrich 過」
    # 的 item 必須回非空——否則上面那個 [] 可能只是它永遠回空。
    # （此控制在 test_ol_enrich_empty_... 的 _seed_item 已建立：見該處 len==1 斷言。）
    fields = extract_bridge_fields(OL_HIT_PAYLOAD)
    assert fields == {
        "ol_key": "/works/OL102749W",
        "isbn": "9780553213119",
        "oclc": "12345678",
        "lccn": "77012345",
        "gutenberg_id": "2701",
    }
    assert extract_bridge_fields(OL_EMPTY_PAYLOAD) == {}


@pytest.mark.asyncio
async def test_ol_enrich_ok_writes_bridge_fields_without_touching_identity(catalog):
    """errors.md `ok`：回填至少一項橋接欄位。

    同時鎖定邊界：enrich **不得**動到 Phase 1 的 identity 欄位
    （source / source_native_id）——它們在 enrich 前後必須逐字相同。
    """
    remote, _ = catalog
    pending = _seed_item(remote)
    assert len(pending) == 1

    bridge = OpenLibraryBridge(remote, transport=_ol_transport(OL_HIT_PAYLOAD))
    result = await bridge.enrich_item(pending[0])

    assert result.outcome == "ok"
    assert result.error is None
    assert result.fields_written == 5
    assert result.queried is True

    with remote.engine.session() as conn:
        row = dict(
            conn.execute(
                "SELECT source, source_native_id, ol_key, isbn, oclc, lccn, "
                "gutenberg_id, ol_enriched_at FROM remote_catalog_item"
            ).fetchone()
        )
    assert row["source"] == "gutenberg"
    assert row["source_native_id"] == "2701"
    assert row["ol_key"] == "/works/OL102749W"
    assert row["isbn"] == "9780553213119"
    assert row["oclc"] == "12345678"
    assert row["lccn"] == "77012345"
    assert row["gutenberg_id"] == "2701"
    assert row["ol_enriched_at"] is not None


@pytest.mark.asyncio
async def test_ol_enrich_empty_is_not_failed_and_still_marks_enriched(catalog):
    """errors.md `empty`：查詢成功但 OL 無對應記錄。

    判準①：`empty` 的 error 恆為 None，`failed` 的 error 恆為非 None——
    兩者的 fields_written 都是 0，若只看那個數字就分不出來。
    且 empty 仍記 ol_enriched_at（這是一次**有效**查詢，不該重打）。
    """
    remote, _ = catalog
    pending = _seed_item(remote)

    bridge = OpenLibraryBridge(remote, transport=_ol_transport(OL_EMPTY_PAYLOAD))
    result = await bridge.enrich_item(pending[0])

    assert result.outcome == "empty"
    assert result.error is None
    assert result.fields_written == 0
    assert result.queried is True

    with remote.engine.session() as conn:
        row = dict(
            conn.execute(
                "SELECT ol_key, ol_enriched_at FROM remote_catalog_item"
            ).fetchone()
        )
    assert row["ol_key"] is None
    assert row["ol_enriched_at"] is not None

    # 這一格才是節流鍵真正承重的地方：OL 查過但沒收錄的書，橋接欄位全空，
    # 若 pending 判定看的是「橋接欄位是不是空的」而不是 `ol_enriched_at`，
    # 它就會每一輪被重新列為 pending 而重打 OL——而 OL 明文禁止那種行為。
    # 缺席態（沒查過）與空結果態（查過但沒有）共用同一個輸出的典型病灶。
    assert remote.list_items_needing_ol_enrichment("cat_471") == []


@pytest.mark.asyncio
async def test_ol_enrich_http_error_is_failed_with_non_none_error(catalog):
    """errors.md `failed`（HTTP 錯誤）。與上一條 empty 成對。

    判準①的實質斷言：這裡的 error 非 None、empty 那條的 error 是 None，
    且 outcome 字串不同——兩條一起看才證明兩態真的不共用輸出。
    另：失敗時**不得**蓋 ol_enriched_at，否則一次失敗會被當成一次有效查詢。
    """
    remote, _ = catalog
    pending = _seed_item(remote)

    bridge = OpenLibraryBridge(
        remote, transport=_ol_transport({"error": "boom"}, status_code=500)
    )
    result = await bridge.enrich_item(pending[0])

    assert result.outcome == "failed"
    assert result.error is not None
    assert result.fields_written == 0

    with remote.engine.session() as conn:
        row = dict(
            conn.execute(
                "SELECT ol_enriched_at FROM remote_catalog_item"
            ).fetchone()
        )
    assert row["ol_enriched_at"] is None


@pytest.mark.asyncio
async def test_ol_enrich_invalid_json_is_failed_not_empty(catalog):
    """errors.md `failed`（非法 JSON）。

    這是判準①最容易漏的一格：一份 HTML 錯誤頁若被寬鬆解析，會得到
    「0 筆」而沉默地降級成 empty。此處必須是 failed。
    """
    remote, _ = catalog
    pending = _seed_item(remote)

    bridge = OpenLibraryBridge(
        remote, transport=_ol_transport("<html>not json</html>")
    )
    result = await bridge.enrich_item(pending[0])

    assert result.outcome == "failed"
    assert result.error is not None


@pytest.mark.asyncio
async def test_ol_enrich_timeout_is_failed_with_ol_bridge_timeout_code(catalog):
    """errors.md Error Catalogue `OL_BRIDGE_TIMEOUT`：逾時 → failed，欄位留空。"""
    remote, _ = catalog
    pending = _seed_item(remote)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    bridge = OpenLibraryBridge(remote, transport=httpx.MockTransport(timeout))
    result = await bridge.enrich_item(pending[0])

    assert result.outcome == "failed"
    assert "OL_BRIDGE_TIMEOUT" in result.error


@pytest.mark.asyncio
async def test_ol_enrich_within_ttl_is_not_run_and_makes_no_request(catalog):
    """errors.md `not-run`：節流命中。

    與 `empty` 的區別在此可觀察：兩者 fields_written 都是 0，但 not-run 的
    `queried` 是 False 且**一個請求都沒發出**（requests 計數 0）。
    正向控制在同一條：同一支 transport 對未 enrich 過的 item 必須真的發請求。
    """
    remote, _ = catalog
    pending = _seed_item(remote)
    requests = []
    bridge = OpenLibraryBridge(
        remote,
        transport=_ol_transport(OL_HIT_PAYLOAD, capture=requests),
        min_interval_seconds=0.0,
    )

    first = await bridge.enrich_item(pending[0])
    assert first.outcome == "ok"
    assert len(requests) == 1, "正向控制：未 enrich 過的 item 必須真的打 OL"

    still_pending = remote.list_items_needing_ol_enrichment("cat_471")
    assert still_pending == [], "enrich 後不該再被列為 pending"

    with remote.engine.session() as conn:
        row = dict(
            conn.execute(
                "SELECT catalog_id, title, authors_display, isbn, ol_enriched_at "
                "FROM remote_catalog_item"
            ).fetchone()
        )
    second = await bridge.enrich_item(row)

    assert second.outcome == "not-run"
    assert second.queried is False
    assert len(requests) == 1, "節流命中時絕不得再打 OL"


@pytest.mark.asyncio
async def test_ol_enrich_without_any_clue_is_not_run_and_makes_no_request(catalog):
    """`not-run` 第二種觸發：連可查的線索都沒有，不花請求去確認。"""
    remote, _ = catalog
    requests = []
    bridge = OpenLibraryBridge(
        remote, transport=_ol_transport(OL_HIT_PAYLOAD, capture=requests)
    )

    result = await bridge.enrich_item(
        {"catalog_id": "rc_x", "title": "未知書名", "authors_display": None}
    )

    assert result.outcome == "not-run"
    assert requests == []


@pytest.mark.asyncio
async def test_ol_enrich_expired_ttl_requeries(catalog):
    """節流的反向控制：TTL 過期後必須重查。

    沒有這條，上面的 not-run 測試無法排除「一旦 enrich 過就永遠不再查」。
    """
    remote, _ = catalog
    requests = []
    bridge = OpenLibraryBridge(
        remote,
        transport=_ol_transport(OL_HIT_PAYLOAD, capture=requests),
        ttl_seconds=0,
        min_interval_seconds=0.0,
    )

    result = await bridge.enrich_item(
        {
            "catalog_id": "rc_x",
            "title": "Moby Dick",
            "ol_enriched_at": "2020-01-01T00:00:00+00:00",
        }
    )

    assert result.outcome == "ok"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_ol_request_shape_matches_probed_contract(catalog):
    """3.1：實際發出的請求必須帶 fields / limit / q 三個參數。

    鎖住的是「我實測過的那個形狀」而不是記憶中的形狀。
    """
    remote, _ = catalog
    pending = _seed_item(remote)
    requests = []
    bridge = OpenLibraryBridge(
        remote, transport=_ol_transport(OL_HIT_PAYLOAD, capture=requests)
    )

    await bridge.enrich_item(pending[0])

    assert len(requests) == 1
    url = requests[0].url
    assert str(url).startswith("https://openlibrary.org/search.json")
    assert url.params["fields"] == OL_FIELDS
    assert url.params["limit"] == "1"
    assert url.params["q"] == "title:Moby Dick author:遠端作者"


@pytest.mark.asyncio
async def test_ol_bridge_concurrency_is_bounded_to_one():
    """3.3：以 mock transport 量測併發上界。OL 明文禁 hundreds of requests，

    故上限取 1（比 Gutenberg 的 2 更保守）。正向控制：peak 必須 > 0
    （請求真的進到 transport）；反向控制：peak 必須 <= 1（拿掉 limiter
    會衝到 5）。
    """
    import anyio

    state = {"inflight": 0, "peak": 0}
    gate = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        state["inflight"] += 1
        state["peak"] = max(state["peak"], state["inflight"])
        await gate.wait()
        state["inflight"] -= 1
        return httpx.Response(200, json=OL_HIT_PAYLOAD)

    limiter = anyio.CapacityLimiter(1)

    class _NullDao:
        def mark_ol_enriched(self, catalog_id, fields):
            return len(fields)

    bridges = [
        OpenLibraryBridge(
            _NullDao(),
            transport=httpx.MockTransport(slow),
            limiter=limiter,
            min_interval_seconds=0.0,
        )
        for _ in range(5)
    ]
    tasks = [
        asyncio.create_task(b.enrich_item({"catalog_id": f"rc_{i}", "title": "T"}))
        for i, b in enumerate(bridges)
    ]
    await asyncio.sleep(0.05)
    peak_while_blocked = state["peak"]
    gate.set()
    results = await asyncio.gather(*tasks)

    assert peak_while_blocked > 0, "控制組：必須真的有請求進到 transport"
    assert peak_while_blocked <= 1
    assert state["peak"] <= 1
    assert all(r.outcome == "ok" for r in results)


@pytest.mark.asyncio
async def test_ol_bridge_enforces_minimum_interval_between_requests(catalog):
    """3.3：兩次請求間至少隔 min_interval_seconds（OL 未識別 ~1 req/s）。

    用假時鐘 + 假 sleep 量測，不真的等——斷言的是「有呼叫 sleep 且秒數
    正確」，而非「測試跑得多慢」。
    """
    remote, _ = catalog
    slept = []
    clock = {"t": 100.0}

    async def fake_sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    bridge = OpenLibraryBridge(
        remote,
        transport=_ol_transport(OL_HIT_PAYLOAD),
        min_interval_seconds=1.0,
        sleep=fake_sleep,
        clock=lambda: clock["t"],
    )

    await bridge.enrich_item({"catalog_id": "rc_a", "title": "A"})
    assert slept == [], "首次請求不該等待"
    clock["t"] += 0.25
    await bridge.enrich_item({"catalog_id": "rc_b", "title": "B"})

    assert slept == [0.75], "已過 0.25s，還該等 0.75s 才滿 1s"


@pytest.mark.asyncio
async def test_ol_failure_does_not_block_or_rollback_the_main_write(catalog):
    """技術要求 5：OL 全段掛掉，主寫入必須已完成且 refresh 仍為 fresh。

    這是 DD-4 最重要的不變量：OL 是補充不是前置條件。
    """
    remote, _ = catalog

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("OL down", request=request)

    refresher = RemoteCatalogRefresher(
        remote,
        LibgenCrawler(mirrors=["https://example.test"]),
        gutenberg=GutenbergProvider(
            transport=_csv_transport(CSV_TWO_ROWS),
            max_attempts=1,
            backoff_base_seconds=0.0,
        ),
        ol_bridge=OpenLibraryBridge(
            remote,
            transport=httpx.MockTransport(boom),
            min_interval_seconds=0.0,
        ),
    )

    assert await refresher.refresh_gutenberg("cat_471", "classics") == "ok"

    total, _items = remote.query_browseable("cat_471", page=1, page_size=20)
    status = remote.get_status("cat_471")
    assert total == 2, "OL 失敗不得回滾主寫入"
    assert status["status"] == "fresh"
    assert status["error"] is None


@pytest.mark.asyncio
async def test_refresh_runs_ol_enrich_after_write_not_on_request_path(catalog):
    """3.1/技術要求 2：enrich 掛在刷新流程末段，且真的跑了。

    正向：刷新後橋接欄位已被回填。
    反向：`category_routes.py`（查詢請求路徑）完全不得引用 OpenLibraryBridge——
    這是 design.md "no synchronous call on request path" 的可機械驗證形式。
    """
    from pathlib import Path

    remote, _ = catalog
    refresher = RemoteCatalogRefresher(
        remote,
        LibgenCrawler(mirrors=["https://example.test"]),
        gutenberg=GutenbergProvider(
            transport=_csv_transport(CSV_TWO_ROWS),
            max_attempts=1,
            backoff_base_seconds=0.0,
        ),
        ol_bridge=OpenLibraryBridge(
            remote,
            transport=_ol_transport(OL_HIT_PAYLOAD),
            min_interval_seconds=0.0,
        ),
    )

    await refresher.refresh_gutenberg("cat_471", "classics")

    with remote.engine.session() as conn:
        enriched = conn.execute(
            "SELECT COUNT(*) c FROM remote_catalog_item WHERE ol_key IS NOT NULL"
        ).fetchone()["c"]
    assert enriched == 2, "刷新末段的 enrich 必須真的回填了"

    routes_src = Path("app/api/category_routes.py").read_text(encoding="utf-8")
    assert "OpenLibraryBridge" not in routes_src
    assert "openlibrary_bridge" not in routes_src


@pytest.mark.asyncio
async def test_libgen_refresh_still_works_without_ol_bridge(catalog):
    """邊界：未注入 ol_bridge 時，enrich 是 no-op，Phase 1/2 行為逐字不變。"""
    remote, _ = catalog

    class PagedCrawler:
        async def search_page(self, query, page=1, page_size=25):
            return {
                "items": [remote_item("5" * 32, "libgen 單頁")],
                "cursor": "1",
                "next_page": None,
                "provider_total": None,
            }

    refresher = RemoteCatalogRefresher(remote, PagedCrawler())
    await refresher.refresh("cat_471", "python")

    total, items = remote.query_browseable("cat_471", page=1, page_size=20)
    assert total == 1
    assert remote.get_status("cat_471")["status"] == "fresh"


def test_ol_migration_is_additive_and_keeps_phase1_composite_index(tmp_path):
    """3.2：additive-only migration——舊 DB 補欄後，Phase 1 的複合唯一索引

    必須仍在、舊資料不得遺失。正向控制：驗證 ALTER 前那筆 row 仍可讀到。
    """
    import sqlite3

    db_path = tmp_path / "legacy-ol.sqlite"
    engine = DatabaseEngine(db_path=db_path)
    CatalogDAO(engine=engine)
    remote = RemoteCatalogDAO(engine=engine)
    remote.upsert_batch("cat_471", "python", [remote_item("a" * 32, "舊資料")])

    # 模擬舊 DB：把新增的 OL 欄位拿掉不可行（SQLite 不支援 DROP COLUMN
    # 在舊版），改為直接檢查 migration 幂等性與索引存在。
    dao = CatalogDAO(engine=DatabaseEngine(db_path=db_path))
    applied = dao.apply_column_migrations()
    assert applied == [], "重跑 migration 應為 no-op（幂等）"

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(remote_catalog_item)").fetchall()
        }
        indexes = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='remote_catalog_item'"
            ).fetchall()
        }
        rows = conn.execute(
            "SELECT md5, source, source_native_id FROM remote_catalog_item"
        ).fetchall()

    assert {"ol_key", "isbn", "oclc", "lccn", "gutenberg_id", "ol_enriched_at"} <= cols
    assert {"source", "source_native_id", "md5"} <= cols, "Phase 1 欄位不得消失"
    assert "idx_remote_catalog_item_identity" in indexes
    assert len(rows) == 1
    assert rows[0]["source_native_id"] == "a" * 32


# === OpenStax provider（tasks.md 4.1-4.2）===

# 2026-09-02 實測回應形狀裁剪。三筆刻意覆蓋三種授權情境：
# CC BY-NC-SA / CC BY / **null（未宣告，實測 129 本中有 11 本）**。
OPENSTAX_PAGE = {
    "meta": {"total_count": 3},
    "items": [
        {
            "id": 873,
            "title": "Additive Manufacturing Essentials",
            "license_name": "Creative Commons Attribution-NonCommercial-ShareAlike License",
            "meta": {
                "slug": "additive-manufacturing-essentials",
                "html_url": "https://openstax.org/details/books/additive-manufacturing-essentials",
                "locale": "en",
            },
        },
        {
            "id": 200,
            "title": "College Physics",
            "license_name": "Creative Commons Attribution License",
            "meta": {
                "slug": "college-physics",
                "html_url": "https://openstax.org/details/books/college-physics",
                "locale": "en",
            },
        },
        {
            "id": 311,
            "title": "C\u00e1lculo volumen 1",
            "license_name": None,
            "meta": {
                "slug": "c\u00e1lculo-volumen-1",
                "html_url": "https://openstax.org/details/books/c\u00e1lculo-volumen-1",
                "locale": "es",
            },
        },
    ],
}
OPENSTAX_EMPTY_PAGE = {"meta": {"total_count": 0}, "items": []}
# 實測：`fields=zzz_not_a_field` → 400 且 body 是合法 JSON。
OPENSTAX_400_BODY = {"message": "unknown fields: zzz_not_a_field"}


def _openstax_transport(payload, status_code: int = 200, *, capture=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        if isinstance(payload, str):
            return httpx.Response(status_code, text=payload)
        return httpx.Response(status_code, json=payload)

    return httpx.MockTransport(handler)


def test_openstax_endpoint_and_fields_match_the_probed_contract():
    """4.1：端點鎖字面值（2026-09-02 實測）。

    **`fields=` 是必需的不是禁止的**：實測不帶 fields 時 items 只有
    id/meta/title，拿不到 license_name；帶 `fields=title,license_name` 回 200
    且真的帶回。400 只發生在**未知欄位名**。此測試鎖住這個實測結論，
    避免日後有人依舊誤傳把 fields 拿掉而静默失去授權欄位。
    """
    assert OPENSTAX_API_URL == "https://openstax.org/apps/cms/api/v2/pages/"
    assert "license_name" in OPENSTAX_FIELDS.split(",")
    assert "title" in OPENSTAX_FIELDS.split(",")


def test_parse_books_payload_keeps_per_book_license_including_null():
    """4.2 核心：逐本 `license_name`，**未宣告就是 None**。

    正向控制：有宣告的兩本各自拿到**不同**的字串（非單一全域值）。
    反向控制：未宣告的那本必須是 None，不得被任何預設值頂上——若兩者
    共用同一個輸出，就等於替出版方做了它沒做的聲明。
    """
    items = parse_books_payload(OPENSTAX_PAGE)

    assert len(items) == 3
    licenses = {i["source_native_id"]: i["license_name"] for i in items}
    assert licenses["873"] == (
        "Creative Commons Attribution-NonCommercial-ShareAlike License"
    )
    assert licenses["200"] == "Creative Commons Attribution License"
    assert licenses["311"] is None
    # 逐本而非全域：兩個有值的必須不同。
    assert licenses["873"] != licenses["200"]
    assert {i["source"] for i in items} == {"openstax"}
    assert all(i["md5"] is None for i in items)
    assert items[0]["work_id"] == make_work_id("openstax", "873")


def test_parse_books_payload_on_empty_items_is_empty_not_an_error():
    """與上一條配對：同一支解析器對 0 筆回空 list 且不丟例外。

    上一條證明它有東西時回 3 筆 ⇒ 這裡的 0 是真的 0，不是解析器壞了。
    """
    assert parse_books_payload(OPENSTAX_EMPTY_PAGE) == []


@pytest.mark.asyncio
async def test_openstax_fetch_ok_and_request_shape_uses_fields_param(catalog):
    """4.1：實際發出的請求必須帶 type/limit/offset/fields 四個參數。"""
    requests = []
    provider = OpenStaxProvider(
        transport=_openstax_transport(OPENSTAX_PAGE, capture=requests),
        page_size=100,
        max_attempts=1,
        backoff_base_seconds=0.0,
    )
    result = await provider.fetch_books()

    assert result.outcome == "ok"
    assert len(result.items) == 3
    assert result.total_count == 3
    assert len(requests) == 1
    params = requests[0].url.params
    assert params["type"] == "books.Book"
    assert params["limit"] == "100"
    assert params["offset"] == "0"
    assert params["fields"] == OPENSTAX_FIELDS


@pytest.mark.asyncio
async def test_openstax_zero_books_is_empty_outcome_not_failure():
    """errors.md `empty`：API 回 200 但 0 本 ⇒ empty，且**不**丟例外。"""
    provider = OpenStaxProvider(
        transport=_openstax_transport(OPENSTAX_EMPTY_PAGE),
        max_attempts=1,
        backoff_base_seconds=0.0,
    )
    result = await provider.fetch_books()

    assert result.outcome == "empty"
    assert result.items == []


@pytest.mark.asyncio
async def test_openstax_connection_failure_raises_not_empty():
    """errors.md `failed`：連線失敗必須 raise，不得回空結果。

    與上一條 empty 成對——兩者若共用輸出，必有一條紅。
    """

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unavailable", request=request)

    provider = OpenStaxProvider(
        transport=httpx.MockTransport(boom),
        max_attempts=2,
        backoff_base_seconds=0.0,
    )
    with pytest.raises(OpenStaxFetchError) as excinfo:
        await provider.fetch_books()
    assert "OPENSTAX_FETCH_FAILED" in str(excinfo.value)


@pytest.mark.asyncio
async def test_openstax_unknown_field_400_is_failed_not_parsed_as_empty():
    """errors.md `OPENSTAX_UNKNOWN_FIELD`：400 的 body 是**合法 JSON**

    （實測 `{"message": "unknown fields: zzz_not_a_field"}`）。若不 gate HTTP
    status，`payload.get("items")` 會安靜拿到空並回報 empty——那正是把
    failed 降級成 empty。此測試鎖住那條 gate。
    """
    provider = OpenStaxProvider(
        transport=_openstax_transport(OPENSTAX_400_BODY, status_code=400),
        max_attempts=1,
        backoff_base_seconds=0.0,
    )
    with pytest.raises(OpenStaxFetchError):
        await provider.fetch_books()


@pytest.mark.asyncio
async def test_openstax_paginates_until_short_page():
    """4.1：分頁。實測 129 本、limit=100 → 第二頁 29 筆後停。

    本測試以 page_size=2 縮小同形狀：第一頁滿 2 筆 → 繼續，第二頁 1 筆
    （不滿）→ 停。正向控制：兩頁都真的被拿了（offset 序列）。
    """
    requests = []
    page1 = {
        "meta": {"total_count": 3},
        "items": OPENSTAX_PAGE["items"][:2],
    }
    page2 = {
        "meta": {"total_count": 3},
        "items": OPENSTAX_PAGE["items"][2:],
    }

    def paged(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json=page1 if offset == 0 else page2)

    provider = OpenStaxProvider(
        transport=httpx.MockTransport(paged),
        page_size=2,
        max_attempts=1,
        backoff_base_seconds=0.0,
    )
    result = await provider.fetch_books()

    assert [r.url.params["offset"] for r in requests] == ["0", "2"]
    assert result.pages_fetched == 2
    assert len(result.items) == 3
    assert result.outcome == "ok"


@pytest.mark.asyncio
async def test_openstax_retries_with_exponential_backoff():
    """4.1：指數退避（3 次嘗試間的 sleep 秒數）。"""
    attempts = {"n": 0}
    slept = []

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("boom", request=request)

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    provider = OpenStaxProvider(
        transport=httpx.MockTransport(flaky),
        max_attempts=3,
        backoff_base_seconds=0.5,
        sleep=fake_sleep,
    )
    with pytest.raises(OpenStaxFetchError):
        await provider.fetch_books()

    assert attempts["n"] == 3
    assert slept == [0.5, 1.0]


@pytest.mark.asyncio
async def test_openstax_recovers_on_second_attempt():
    """退避的正向控制組：第一次失敗、第二次成功 ⇒ ok。"""
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("transient", request=request)
        return httpx.Response(200, json=OPENSTAX_PAGE)

    provider = OpenStaxProvider(
        transport=httpx.MockTransport(flaky),
        max_attempts=3,
        backoff_base_seconds=0.0,
    )
    result = await provider.fetch_books()

    assert calls["n"] == 2
    assert result.outcome == "ok"


@pytest.mark.asyncio
async def test_openstax_concurrency_is_bounded_by_capacity_limiter():
    """4.1 節流：以 mock transport 量測併發上界。

    正向控制：peak > 0（請求真的進到 transport）；反向：peak ≤ 2
    （拿掉 limiter 會衝到 5）。
    """
    import anyio

    state = {"inflight": 0, "peak": 0}
    gate = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        state["inflight"] += 1
        state["peak"] = max(state["peak"], state["inflight"])
        await gate.wait()
        state["inflight"] -= 1
        return httpx.Response(200, json=OPENSTAX_PAGE)

    limiter = anyio.CapacityLimiter(2)
    providers = [
        OpenStaxProvider(
            transport=httpx.MockTransport(slow),
            max_attempts=1,
            backoff_base_seconds=0.0,
            limiter=limiter,
        )
        for _ in range(5)
    ]
    tasks = [asyncio.create_task(p.fetch_books()) for p in providers]
    await asyncio.sleep(0.05)
    peak_while_blocked = state["peak"]
    gate.set()
    results = await asyncio.gather(*tasks)

    assert peak_while_blocked > 0, "控制組：必須真的有請求進到 transport"
    assert peak_while_blocked <= 2
    assert all(r.outcome == "ok" for r in results)


@pytest.mark.asyncio
async def test_openstax_per_book_license_is_visible_in_api_model(catalog):
    """4.2：逐本授權必須在 API 回應可見，且三種情境各自正確。

    同一個分類內混著四種授權情境：
    - OpenStax CC BY-NC-SA（逐本值）
    - OpenStax CC BY（逐本值，與上一個不同）
    - OpenStax 未宣告（None——**不得**被任何預設值頂上）
    - Gutenberg（來源層字面值，Phase 2 行為不得回歸）
    四者共存才能證明「逐本優先 + 來源層回退」兩層都活著。
    """
    remote, _ = catalog
    remote.upsert_batch("cat_471", "textbooks", parse_books_payload(OPENSTAX_PAGE))
    remote.upsert_batch("cat_471", "textbooks", parse_catalog_csv(CSV_TWO_ROWS))

    _, stored = remote.query_browseable("cat_471", page=1, page_size=20)
    api_items = [SearchResultItem(**row) for row in stored]
    by_title = {item.title: item for item in api_items}

    assert by_title["Additive Manufacturing Essentials"].license == (
        "Creative Commons Attribution-NonCommercial-ShareAlike License"
    )
    assert by_title["College Physics"].license == (
        "Creative Commons Attribution License"
    )
    # 未宣告 ⇒ None。這一格是本 phase 的核心反向控制。
    assert by_title["C\u00e1lculo volumen 1"].license is None
    # Gutenberg 的來源層字面值不得因為新增逐本層而壞掉。
    assert by_title["Moby Dick"].license == "Public domain in the USA."


@pytest.mark.asyncio
async def test_refresher_openstax_failed_and_empty_are_distinguishable(catalog):
    """4.1：`failed` 與 `empty` 在 refresh row 上必須可分辨（判準①）。"""
    remote, _ = catalog

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unavailable", request=request)

    failing = RemoteCatalogRefresher(
        remote,
        LibgenCrawler(mirrors=["https://example.test"]),
        openstax=OpenStaxProvider(
            transport=httpx.MockTransport(boom),
            max_attempts=1,
            backoff_base_seconds=0.0,
        ),
    )
    assert await failing.refresh_openstax("cat_471", "textbooks") == "failed"
    failed_status = remote.get_status("cat_471")

    empty = RemoteCatalogRefresher(
        remote,
        LibgenCrawler(mirrors=["https://example.test"]),
        openstax=OpenStaxProvider(
            transport=_openstax_transport(OPENSTAX_EMPTY_PAGE),
            max_attempts=1,
            backoff_base_seconds=0.0,
        ),
    )
    assert await empty.refresh_openstax("cat_471", "textbooks") == "empty"
    empty_status = remote.get_status("cat_471")

    assert failed_status["status"] == "failed"
    assert "OPENSTAX_FETCH_FAILED" in failed_status["error"]
    assert empty_status["status"] == "fresh"
    assert empty_status["error"] is None


@pytest.mark.asyncio
async def test_refresher_without_openstax_provider_is_not_run(catalog):
    """errors.md `not-run`：未設定 provider ⇒ 跳過，不留 refresh row。"""
    remote, _ = catalog
    refresher = RemoteCatalogRefresher(
        remote, LibgenCrawler(mirrors=["https://example.test"])
    )

    assert await refresher.refresh_openstax("cat_471", "textbooks") == "not-run"
    assert remote.get_status("cat_471")["status"] == "never_refreshed"


@pytest.mark.asyncio
async def test_three_sources_coexist_in_one_category(catalog):
    """4.1 端到端：三個來源同時出現在同一分類，去重總數正確。

    libgen 1 + gutenberg 2 + openstax 3 = 6。兩個非 libgen 來源的 md5 全為
    NULL，若複合鍵不成立它們會互撞——這是 Phase 1 抽象在第三個來源上的
    驗證點。
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
        openstax=OpenStaxProvider(
            transport=_openstax_transport(OPENSTAX_PAGE),
            max_attempts=1,
            backoff_base_seconds=0.0,
        ),
    )
    remote.upsert_batch("cat_471", "mixed", [remote_item("b" * 32, "libgen 那本")])

    assert await refresher.refresh_gutenberg("cat_471", "mixed") == "ok"
    assert await refresher.refresh_openstax("cat_471", "mixed") == "ok"

    total, items = remote.query_browseable("cat_471", page=1, page_size=20)
    assert total == 6
    assert {item["source"] for item in items} == {"libgen", "gutenberg", "openstax"}


@pytest.mark.asyncio
async def test_openstax_refresh_is_idempotent_on_repeat(catalog):
    """4.1：同一份 payload 刷兩次，總數仍為 3（複合鍵去重成立）。"""
    remote, _ = catalog
    refresher = RemoteCatalogRefresher(
        remote,
        LibgenCrawler(mirrors=["https://example.test"]),
        openstax=OpenStaxProvider(
            transport=_openstax_transport(OPENSTAX_PAGE),
            max_attempts=1,
            backoff_base_seconds=0.0,
        ),
    )

    await refresher.refresh_openstax("cat_471", "textbooks")
    await refresher.refresh_openstax("cat_471", "textbooks")

    total, _items = remote.query_browseable("cat_471", page=1, page_size=20)
    assert total == 3


def test_license_name_migration_is_additive_and_keeps_phase1_index(tmp_path):
    """4.2：additive-only——新增 license_name 不得動 Phase 1 複合唯一索引。"""
    import sqlite3

    db_path = tmp_path / "legacy-openstax.sqlite"
    engine = DatabaseEngine(db_path=db_path)
    CatalogDAO(engine=engine)
    remote = RemoteCatalogDAO(engine=engine)
    remote.upsert_batch("cat_471", "python", [remote_item("a" * 32, "舊資料")])

    dao = CatalogDAO(engine=DatabaseEngine(db_path=db_path))
    assert dao.apply_column_migrations() == [], "重跑 migration 應為 no-op"

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(remote_catalog_item)").fetchall()
        }
        indexes = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='remote_catalog_item'"
            ).fetchall()
        }
        rows = conn.execute(
            "SELECT md5, source, source_native_id, license_name "
            "FROM remote_catalog_item"
        ).fetchall()

    assert "license_name" in cols
    assert {"source", "source_native_id", "md5"} <= cols, "Phase 1 欄位不得消失"
    assert "idx_remote_catalog_item_identity" in indexes
    assert len(rows) == 1
    assert rows[0]["source_native_id"] == "a" * 32
    # libgen 舊資料的 license_name 為 NULL（來源未宣告），不得被回填任何值。
    assert rows[0]["license_name"] is None


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
