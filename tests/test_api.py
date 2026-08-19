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


@pytest.fixture
def client_and_storage():
    temp_dir = tempfile.mkdtemp()
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
    yield client, storage, dao, pipeline
    app.dependency_overrides.clear()
    shutil.rmtree(temp_dir)


def create_sample_pdf_bytes(title: str, text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 72), f"{title}\n{text}", fontname="china-t")
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def test_api_health(client_and_storage):
    client, _, _, _ = client_and_storage
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_api_upload_and_search(client_and_storage):
    client, storage, dao, _ = client_and_storage

    pdf_bytes = create_sample_pdf_bytes("作業系統概念", "行程排程與記憶體虛擬化技術詳解。")

    # 上傳檔案
    res = client.post(
        "/api/upload",
        files={"file": ("os_book.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"custom_title": "作業系統概念 (繁體中文版)", "custom_author": "Silberschatz"}
    )
    assert res.status_code == 200
    work = res.json()
    work_id = work["work_id"]
    assert work["title"] == "作業系統概念 (繁體中文版)"

    # 關鍵字搜尋測試
    search_res = client.get("/api/search", params={"q": "記憶體虛擬化"})
    assert search_res.status_code == 200
    search_data = search_res.json()
    assert search_data["total"] == 1
    assert search_data["items"][0]["work_id"] == work_id
    assert "記憶體" in search_data["items"][0]["snippet"]

    # 詳情查詢
    detail_res = client.get(f"/api/works/{work_id}")
    assert detail_res.status_code == 200
    assert detail_res.json()["work_id"] == work_id

    # 純文字內容查詢
    content_res = client.get(f"/api/works/{work_id}/content")
    assert content_res.status_code == 200
    assert "行程排程" in content_res.text

    # 閱讀進度更新
    progress_res = client.post(
        f"/api/progress/{work_id}",
        json={"progress_ratio": 0.35, "last_page": 35, "total_pages": 100}
    )
    assert progress_res.status_code == 200
    assert progress_res.json()["status"] == "ok"

    # 原檔串流下載
    file_res = client.get(f"/api/files/{work_id}/raw")
    assert file_res.status_code == 200
    assert len(file_res.content) == len(pdf_bytes)
