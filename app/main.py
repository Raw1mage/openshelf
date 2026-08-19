import os
from pathlib import Path
from fastapi import FastAPI, APIRouter
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router as api_router
from app.api.crawler_routes import router as crawler_router
from app.api.collection_routes import router as collection_router
from app.api.category_routes import router as category_router
from app.storage.manager import StorageManager
from app.db.engine import DatabaseEngine
from app.api.crawler_routes import get_worker

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    """系統啟動與關閉生命週期管理。"""
    storage = StorageManager()
    storage.ensure_directories()
    engine = DatabaseEngine()
    engine.init_database()
    worker = get_worker()
    worker.start()
    yield

app = FastAPI(
    title="openshelf — 繁體中文版 Libgen 與全文聚合系統",
    description="自用型離線書目檢索、選擇性鏡像與雙份落地全文閱讀系統",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 註冊 API 路由（支援 /api 與 /libgen/api 雙重掛載）
app.include_router(api_router)
app.include_router(crawler_router)
app.include_router(collection_router)
app.include_router(category_router)

libgen_router = APIRouter(prefix="/libgen")
libgen_router.include_router(api_router)
libgen_router.include_router(crawler_router)
libgen_router.include_router(collection_router)
libgen_router.include_router(category_router)
app.include_router(libgen_router)

# 掛載前端靜態目錄（支援 /static 與 /libgen/static 雙重掛載）
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
app.mount("/libgen/static", StaticFiles(directory=str(static_dir)), name="libgen_static")


@app.get("/", include_in_schema=False)
@app.get("/libgen", include_in_schema=False)
@app.get("/libgen/", include_in_schema=False)
def serve_home():
    """首頁（Libgen 繁體中文搜尋介面）。"""
    index_file = static_dir / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {"message": "openshelf backend running"}


@app.get("/reader", include_in_schema=False)
@app.get("/libgen/reader", include_in_schema=False)
@app.get("/libgen/reader/", include_in_schema=False)
def serve_reader():
    """內嵌 PDF.js / EPUB.js 閱讀器介面。"""
    reader_file = static_dir / "reader.html"
    if reader_file.exists():
        return FileResponse(str(reader_file))
    return {"message": "reader page not found"}


