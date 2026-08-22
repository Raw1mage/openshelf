from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.engine import DatabaseEngine
from app.db.dao import CatalogDAO
from app.models.catalog import WorkCreate


@pytest.fixture
def dao(tmp_path):
    db_file = tmp_path / "test_categories.db"
    engine = DatabaseEngine(db_path=db_file)
    engine.init_database()
    return CatalogDAO(engine=engine)


def test_category_tree_and_works(dao):
    # 1. 檢驗分類樹種子資料是否完整
    tree = dao.get_category_tree()
    assert len(tree) >= 5
    cat_lit = next((c for c in tree if c.category_id == "cat_800"), None)
    assert cat_lit is not None
    assert len(cat_lit.children) >= 3

    # 2. 建立書籍並檢驗自動分類推導
    w_hp = dao.create_work(WorkCreate(title="哈利波特：消失的密室", authors_display="J.K. Rowling", language="zh"))
    w_py = dao.create_work(WorkCreate(title="流暢的 Python 程式設計", authors_display="Luciano Ramalho", language="zh"))
    w_tintin = dao.create_work(WorkCreate(title="丁丁歷險記：法老的雪茄", authors_display="埃爾熱", language="zh"))

    # 3. 查詢奇幻/文學分類下的藏書
    total_lit, works_lit = dao.get_category_works("cat_800")
    assert total_lit >= 1
    assert any(w.work_id == w_hp for w in works_lit)

    # 4. 查詢科技/程式設計分類下的藏書
    total_tech, works_tech = dao.get_category_works("cat_471")
    assert total_tech >= 1
    assert any(w.work_id == w_py for w in works_tech)

    # 5. 查詢漫畫分類下的藏書
    total_comic, works_comic = dao.get_category_works("cat_090")
    assert total_comic >= 1
    assert any(w.work_id == w_tintin for w in works_comic)


def test_category_cloud_queries():
    from app.db.categories import CATEGORY_CLOUD_SEARCH_QUERIES
    assert "cat_800" in CATEGORY_CLOUD_SEARCH_QUERIES
    assert "cat_471" in CATEGORY_CLOUD_SEARCH_QUERIES
    assert "cat_090" in CATEGORY_CLOUD_SEARCH_QUERIES
    assert "python" in CATEGORY_CLOUD_SEARCH_QUERIES["cat_471"]


def test_category_works_reads_persisted_union_and_schedules_refresh():
    client, refresher = build_category_client()

    with client:
        data = client.get("/api/categories/cat_880/works").json()

    assert refresher.calls == 1
    assert data["catalog_status"]["status"] == "never_refreshed"
    assert data["catalog_status"]["refresh_scheduled"] is True
    assert data["total"] == len(data["items"]) == 3
    assert {item["availability_tier"] for item in data["items"]} == {0, 1}


def test_category_works_distinguishes_empty_catalog_from_failed_refresh():
    empty_client, empty_refresher = build_category_client(cloud_items=[])
    with empty_client:
        empty_data = empty_client.get("/api/categories/cat_880/works").json()

    failed_client, failed_refresher = build_category_client(cloud_error=RuntimeError("offline"))
    with failed_client:
        failed_data = failed_client.get("/api/categories/cat_880/works").json()

    assert empty_refresher.calls == failed_refresher.calls == 1
    assert empty_data["catalog_status"]["status"] == "never_refreshed"
    assert failed_data["catalog_status"]["status"] == "failed"
    assert empty_data["total"] == len(empty_data["items"]) == 2
    assert failed_data["total"] == len(failed_data["items"]) == 2
    assert {item["availability_tier"] for item in failed_data["items"]} == {0}


def build_category_client(cloud_items=None, cloud_error=None):
    from app.api import category_routes
    from app.models.catalog import CategoryRead

    local_items = [
        SimpleNamespace(
            work_id=f"local-{index}", title=f"本地藏書 {index}", authors_display="作者",
            publication_year=2026, language="zh", format="epub", size_bytes=100,
            md5=f"{index:032x}",
        )
        for index in range(2)
    ]
    remote_items = cloud_items if cloud_items is not None else [{
        "md5": "f" * 32, "title": "雲端推薦", "authors_display": "遠端作者",
        "publication_year": 2025, "language": "zh", "format": "pdf_born_digital",
        "size_bytes": 200,
    }]

    class StubDAO:
        def get_category(self, category_id):
            return CategoryRead(
                category_id=category_id, name="奇幻與魔法", slug="fantasy",
                works_count=len(local_items),
            )

        def get_category_works(self, category_id, page=1, page_size=20):
            return len(local_items), local_items

        def find_works_by_hashes(self, hash_values):
            return {}

    class StubRemoteDAO:
        def query_browseable(self, category_id, page, page_size):
            items = [
                {
                    "work_id": item.work_id,
                    "local_work_id": item.work_id,
                    "title": item.title,
                    "authors_display": item.authors_display,
                    "publication_year": item.publication_year,
                    "language": item.language,
                    "format": item.format,
                    "size_bytes": item.size_bytes,
                    "md5": item.md5,
                    "availability_tier": 0,
                }
                for item in local_items
            ]
            if not cloud_error:
                items.extend({**item, "work_id": f"libgen_{item['md5']}", "availability_tier": 1} for item in remote_items)
            return len(items), items

        def get_status(self, category_id):
            return {
                "status": "failed" if cloud_error else "never_refreshed",
                "last_success_at": None,
                "error": str(cloud_error) if cloud_error else None,
                "accumulated_total": 0,
                "pages_fetched": 0,
            }

    class SpyRefresher:
        def __init__(self):
            self.calls = 0

        def schedule(self, category_id, query_term):
            self.calls += 1
            return True

    refresher = SpyRefresher()
    fastapi_app = FastAPI()
    fastapi_app.include_router(category_routes.router)
    fastapi_app.dependency_overrides[category_routes.get_dao] = StubDAO
    fastapi_app.dependency_overrides[category_routes.get_remote_catalog_dao] = StubRemoteDAO
    fastapi_app.dependency_overrides[category_routes.get_refresher] = lambda: refresher
    return TestClient(fastapi_app), refresher
