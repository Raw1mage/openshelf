import tempfile
import shutil
from pathlib import Path
import fitz  # PyMuPDF
import pytest

from app.storage.manager import StorageManager
from app.db.engine import DatabaseEngine
from app.db.dao import CatalogDAO
from app.pipeline.ingest import IngestionPipeline


def create_sample_pdf(content: str = "繁體中文測試內容：這是一本演算法設計與分析的測試電子書。") -> bytes:
    """在記憶體中建立合法的測試 PDF 檔案位元組。"""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 72), content, fontname="china-t")
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def test_ingestion_pipeline_pdf():
    temp_dir = tempfile.mkdtemp()
    try:
        storage = StorageManager(base_dir=temp_dir)
        engine = DatabaseEngine(db_path=storage.get_db_path())
        dao = CatalogDAO(engine=engine)
        pipeline = IngestionPipeline(storage=storage, dao=dao)

        pdf_bytes = create_sample_pdf("演算法測試：快速排序與二元搜尋樹。")
        work_detail = pipeline.ingest_bytes(
            data=pdf_bytes,
            filename="演算法導論.pdf",
            custom_title="演算法導論 (繁體版)",
            custom_author="Thomas Cormen"
        )

        assert work_detail is not None
        assert work_detail.title == "演算法導論 (繁體版)"
        assert work_detail.authors_display == "Thomas Cormen"
        assert work_detail.availability_tier == 0
        assert len(work_detail.identifiers) == 2  # sha256 + md5
        assert len(work_detail.manifestations) == 1
        assert work_detail.manifestations[0].format == "pdf_born_digital"

        # 驗證純文字已落地
        parsed_md = storage.get_parsed_content(work_detail.work_id)
        assert "快速排序" in parsed_md

        # 驗證重複上傳去重 (Deduplication)
        duplicate_detail = pipeline.ingest_bytes(
            data=pdf_bytes,
            filename="演算法導論_副本.pdf"
        )
        assert duplicate_detail.work_id == work_detail.work_id

    finally:
        shutil.rmtree(temp_dir)
