import os
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Form, Depends
from fastapi.responses import FileResponse, PlainTextResponse

from app.storage.manager import StorageManager
from app.db.engine import DatabaseEngine
from app.db.dao import CatalogDAO
from app.db.search import SearchEngine
from app.pipeline.ingest import IngestionPipeline
from app.models.catalog import WorkDetailRead, SearchResponse, ReadingProgressUpdate

router = APIRouter(prefix="/api")

def get_storage():
    return StorageManager()

def get_dao():
    return CatalogDAO()

def get_search():
    return SearchEngine()

def get_pipeline():
    return IngestionPipeline()


@router.get("/health")
def health_check():
    """健康檢查端點。"""
    return {"status": "ok", "service": "openshelf", "version": "1.0.0"}


@router.get("/search", response_model=SearchResponse)
def search_books(
    q: str = Query("", description="搜尋關鍵字（書名、作者、內文）"),
    format: Optional[str] = Query("all", description="格式篩選: all, pdf_born_digital, pdf_scanned, epub"),
    language: Optional[str] = Query("all", description="語言: all, zh, en"),
    year_min: Optional[int] = Query(None, description="最小出版年"),
    year_max: Optional[int] = Query(None, description="最大出版年"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search_engine: SearchEngine = Depends(get_search)
):
    """檢索書籍與全文。"""
    return search_engine.search(
        query=q,
        format_filter=format,
        language_filter=language,
        year_min=year_min,
        year_max=year_max,
        page=page,
        page_size=page_size
    )


@router.get("/works/{work_id}", response_model=WorkDetailRead)
def get_work(work_id: str, dao: CatalogDAO = Depends(get_dao)):
    """取得書籍完整元資料。"""
    detail = dao.get_work_detail(work_id)
    if not detail:
        raise HTTPException(status_code=404, detail="找不到指定書籍")
    return detail


@router.get("/works/{work_id}/content", response_class=PlainTextResponse)
def get_work_parsed_content(
    work_id: str,
    storage: StorageManager = Depends(get_storage),
    dao: CatalogDAO = Depends(get_dao)
):
    """取得抽取之 Markdown 純文字內容（供 RAG 或快速預覽）。"""
    detail = dao.get_work_detail(work_id)
    if not detail:
        raise HTTPException(status_code=404, detail="找不到指定書籍")
    
    content = storage.get_parsed_content(work_id)
    return content


@router.get("/files/{work_id}/raw")
def get_raw_file(
    work_id: str,
    preview: bool = Query(False, description="是否為閱讀器預覽模式（MOBI 自動輸出 PDF）"),
    storage: StorageManager = Depends(get_storage),
    dao: CatalogDAO = Depends(get_dao)
):
    """串流輸出原始檔案（供 Web 閱讀器或下載）。"""
    detail = dao.get_work_detail(work_id)
    if not detail or not detail.manifestations:
        raise HTTPException(status_code=404, detail="找不到指定檔案")

    # 尋找本地原始檔
    target_rel_path = None
    media_type = "application/pdf"
    manifestation_format = "pdf"
    for mf in detail.manifestations:
        if mf.origin == "local":
            for f in mf.files:
                if f.role == "original":
                    target_rel_path = f.local_path
                    manifestation_format = mf.format
                    if mf.format == "epub":
                        media_type = "application/epub+zip"
                    elif mf.format in ("mobi", "azw", "azw3"):
                        media_type = "application/x-mobipocket-ebook"
                    break
        if target_rel_path:
            break

    if not target_rel_path:
        raise HTTPException(status_code=404, detail="該書籍無本地可提供之實體檔案")

    file_path = storage.resolve_path(target_rel_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="實體檔案遺失")

    # 若為 MOBI 且為預覽/線上閱讀模式，提供自動轉換之標準 PDF
    if manifestation_format in ("mobi", "azw", "azw3") or file_path.suffix.lower() in (".mobi", ".azw", ".azw3"):
        converted_pdf = storage.parsed_dir / f"{work_id}.pdf"
        if not converted_pdf.exists():
            from app.pipeline.mobi_extractor import MobiExtractor
            MobiExtractor.convert_to_pdf(file_path, converted_pdf)

        if converted_pdf.exists():
            return FileResponse(
                path=str(converted_pdf),
                media_type="application/pdf",
                filename=f"{detail.title}.pdf"
            )

    filename = f"{detail.title}.{file_path.suffix.lstrip('.')}"
    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=filename
    )


@router.post("/progress/{work_id}")
def update_progress(
    work_id: str,
    progress: ReadingProgressUpdate,
    dao: CatalogDAO = Depends(get_dao)
):
    """更新閱讀進度。"""
    dao.update_reading_progress(work_id, progress)
    return {"status": "ok", "work_id": work_id}


@router.post("/upload", response_model=WorkDetailRead)
async def upload_book(
    file: UploadFile = File(...),
    custom_title: Optional[str] = Form(None),
    custom_author: Optional[str] = Form(None),
    pipeline: IngestionPipeline = Depends(get_pipeline)
):
    """上傳新書籍檔案（PDF / EPUB）並觸發自動解析。"""
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="上傳檔案為空")

    work_detail = pipeline.ingest_bytes(
        data=contents,
        filename=file.filename or "unknown.pdf",
        custom_title=custom_title,
        custom_author=custom_author
    )
    return work_detail
