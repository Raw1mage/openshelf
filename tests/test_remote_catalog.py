import asyncio

import httpx
import pytest

from app.crawler.libgen_live import LibgenCrawler
from app.crawler.remote_catalog_refresh import RemoteCatalogRefresher
from app.db.dao import CatalogDAO
from app.db.engine import DatabaseEngine
from app.db.remote_catalog import RemoteCatalogDAO


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
