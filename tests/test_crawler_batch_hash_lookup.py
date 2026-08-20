"""BR-20260820_200000 — 批次 hash 查詢與逐筆版本的等價性。

涵蓋驗收判準 5 要求的兩個邊界：空清單、全部未命中。
"""
import pytest

from app.db.dao import CatalogDAO
from app.db.engine import DatabaseEngine
from app.models.catalog import WorkCreate


@pytest.fixture
def dao(tmp_path):
    db = tmp_path / "db" / "t.sqlite"
    engine = DatabaseEngine(db_path=db)
    engine.init_database()
    return CatalogDAO(engine=engine)


def _seed(dao, title, md5, sha256):
    wid = dao.create_work(WorkCreate(title=title, authors_display="t"))
    with dao.engine.session() as conn:
        conn.execute(
            "INSERT INTO manifestation (manifestation_id, work_id) VALUES (?, ?)",
            (f"m-{md5}", wid),
        )
        conn.execute(
            "INSERT INTO file_object (file_id, manifestation_id, role, local_path, "
            "sha256, md5, size_bytes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"f-{md5}", f"m-{md5}", "raw", f"/x/{md5}", sha256, md5, 1),
        )
    return wid


def test_batch_matches_per_item_on_hits(dao):
    """命中案例：批次結果必須與逐筆版本逐鍵相同。"""
    w1 = _seed(dao, "book-1", "a" * 32, "1" * 64)
    w2 = _seed(dao, "book-2", "b" * 32, "2" * 64)

    hashes = ["a" * 32, "b" * 32, "c" * 32]
    batch = dao.find_works_by_hashes(hashes)
    per_item = {h: dao.find_work_by_hash(h) for h in hashes}

    assert batch.get("a" * 32) == w1
    assert batch.get("b" * 32) == w2
    # 未命中者不出現在鍵中，.get() 回 None，語意與逐筆版一致
    assert batch.get("c" * 32) is None

    for h in hashes:
        assert batch.get(h) == per_item[h], f"batch/per-item 不一致於 {h}"


def test_batch_empty_list(dao):
    """邊界一：空清單。不得炸、不得打 DB、回空 dict。"""
    assert dao.find_works_by_hashes([]) == {}


def test_batch_all_miss(dao):
    """邊界二：全部未命中。回空 dict，且與逐筆版一致。"""
    _seed(dao, "book-1", "a" * 32, "1" * 64)

    hashes = ["d" * 32, "e" * 32, "f" * 32]
    batch = dao.find_works_by_hashes(hashes)
    per_item = {h: dao.find_work_by_hash(h) for h in hashes}

    assert batch == {}
    for h in hashes:
        assert batch.get(h) is None
        assert per_item[h] is None


def test_batch_finds_by_sha256_too(dao):
    """逐筆版查 sha256 OR md5；批次版必須保留同一語意。"""
    w1 = _seed(dao, "book-1", "a" * 32, "1" * 64)

    batch = dao.find_works_by_hashes(["1" * 64])
    assert batch.get("1" * 64) == w1
    assert dao.find_work_by_hash("1" * 64) == w1


def test_batch_dedups_repeated_hash(dao):
    """重複 hash 不得造成重複或遺漏。"""
    w1 = _seed(dao, "book-1", "a" * 32, "1" * 64)

    batch = dao.find_works_by_hashes(["a" * 32, "a" * 32, "a" * 32])
    assert batch == {"a" * 32: w1}
