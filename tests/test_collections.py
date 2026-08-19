import pytest
from app.db.engine import DatabaseEngine
from app.db.dao import CatalogDAO
from app.models.catalog import WorkCreate, CollectionCreate, CollectionUpdate


@pytest.fixture
def dao(tmp_path):
    db_file = tmp_path / "test_collections.db"
    engine = DatabaseEngine(db_path=db_file)
    engine.init_database()
    return CatalogDAO(engine=engine)


def test_collection_lifecycle(dao):
    # 1. 預設書單確認
    cols = dao.list_collections()
    assert len(cols) >= 1
    assert any(c.collection_id == "col_favorites" for c in cols)

    # 2. 建立自訂書單
    col_id = dao.create_collection(CollectionCreate(name="科幻精選", description="個人科幻愛好", icon="🚀"))
    assert col_id.startswith("col_")

    # 3. 建立書籍
    w1 = dao.create_work(WorkCreate(title="三體", authors_display="劉慈欣", language="zh", publication_year=2008))
    w2 = dao.create_work(WorkCreate(title="銀河帝國", authors_display="艾席莫夫", language="zh", publication_year=1951))

    # 4. 加入書籍至書單
    dao.add_work_to_collection(col_id, w1, notes="第一部必讀")
    dao.add_work_to_collection(col_id, w2, notes="基地系列")

    # 5. 檢驗書單內容
    detail = dao.get_collection(col_id)
    assert detail is not None
    assert detail.name == "科幻精選"
    assert len(detail.items) == 2
    assert {it.work_id for it in detail.items} == {w1, w2}

    # 6. 查詢書籍所屬書單
    in_cols = dao.get_work_collections(w1)
    assert col_id in in_cols

    # 7. 更新書單名稱
    dao.update_collection(col_id, CollectionUpdate(name="硬科幻與太空歌劇"))
    detail2 = dao.get_collection(col_id)
    assert detail2.name == "硬科幻與太空歌劇"

    # 8. 移除單本書籍
    dao.remove_work_from_collection(col_id, w1)
    detail3 = dao.get_collection(col_id)
    assert len(detail3.items) == 1
    assert detail3.items[0].work_id == w2

    # 9. 刪除自訂書單
    deleted = dao.delete_collection(col_id)
    assert deleted is True
    assert dao.get_collection(col_id) is None
