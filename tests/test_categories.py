import pytest
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
