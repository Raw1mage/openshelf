import tempfile
import shutil
import io
import fitz
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.api.routes import get_storage, get_dao, get_search, get_pipeline
from app.storage.manager import StorageManager
from app.db.engine import DatabaseEngine
from app.db.dao import CatalogDAO
from app.db.search import SearchEngine
from app.pipeline.ingest import IngestionPipeline


def make_pdf(title: str, text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    # 支援中英文字型
    font = "china-t" if any("\u4e00" <= c <= "\u9fff" for c in title + text) else "helv"
    page.insert_text((50, 72), f"{title}\n{text}", fontname=font)
    data = doc.tobytes()
    doc.close()
    return data


def test_full_e2e_flow():
    temp_dir = tempfile.mkdtemp()
    try:
        storage = StorageManager(base_dir=temp_dir)
        engine = DatabaseEngine(db_path=storage.get_db_path())
        dao = CatalogDAO(engine=engine)
        search_engine = SearchEngine(engine=engine)
        pipeline = IngestionPipeline(storage=storage, dao=dao)

        app.dependency_overrides[get_storage] = lambda: storage
        app.dependency_overrides[get_dao] = lambda: dao
        app.dependency_overrides[get_search] = lambda: search_engine
        app.dependency_overrides[get_pipeline] = lambda: pipeline

        client = TestClient(app)

        # 1. 驗證靜態頁面可訪問
        home_res = client.get("/")
        assert home_res.status_code == 200
        assert "CMS圖書館" in home_res.text

        reader_res = client.get("/reader")
        assert reader_res.status_code == 200

        # 2. 匯入第一本書（繁體中文）
        pdf1 = make_pdf("計算機結構", "管線化技術與分支預測器架構深度剖析。")
        res1 = client.post(
            "/api/upload",
            files={"file": ("ca.pdf", io.BytesIO(pdf1), "application/pdf")},
            data={"custom_title": "計算機結構：量化研究方法", "custom_author": "John L. Hennessy"}
        )
        assert res1.status_code == 200
        w1_id = res1.json()["work_id"]

        # 3. 匯入第二本書（英文）
        pdf2 = make_pdf("Designing Data-Intensive Applications", "Reliability, scalability, and maintainability in distributed systems.")
        res2 = client.post(
            "/api/upload",
            files={"file": ("ddia.pdf", io.BytesIO(pdf2), "application/pdf")},
            data={"custom_title": "Designing Data-Intensive Applications", "custom_author": "Martin Kleppmann"}
        )
        assert res2.status_code == 200
        w2_id = res2.json()["work_id"]

        # 4. 繁體中文關鍵字搜尋
        search_res = client.get("/api/search", params={"q": "管線化"})
        assert search_res.status_code == 200
        hits = search_res.json()["items"]
        assert len(hits) == 1
        assert hits[0]["work_id"] == w1_id

        # 5. 英文關鍵字搜尋
        search_res_en = client.get("/api/search", params={"q": "distributed"})
        assert search_res_en.status_code == 200
        hits_en = search_res_en.json()["items"]
        assert len(hits_en) == 1
        assert hits_en[0]["work_id"] == w2_id

        # 6. 閱讀進度記憶測試
        client.post(
            f"/api/progress/{w1_id}",
            json={"progress_ratio": 0.5, "last_page": 5, "total_pages": 10}
        )
        detail1 = client.get(f"/api/works/{w1_id}").json()
        assert detail1["reading_state"]["progress_ratio"] == 0.5
        assert detail1["reading_state"]["last_page"] == 5

    finally:
        app.dependency_overrides.clear()
        shutil.rmtree(temp_dir)
