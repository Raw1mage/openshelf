from datetime import datetime
from typing import List, Optional, Literal
from pydantic import BaseModel, Field


class IdentifierBase(BaseModel):
    scheme: str  # 'md5', 'sha256', 'isbn', 'doi', 'ipfs', 'arxiv'
    value: str
    confidence: Literal["asserted", "matched", "guessed"] = "asserted"


class IdentifierRead(IdentifierBase):
    identifier_id: int
    work_id: str


class FileObjectRead(BaseModel):
    file_id: str
    manifestation_id: str
    role: Literal["original", "extracted_text", "ocr_text", "thumbnail"]
    local_path: str
    sha256: str
    md5: Optional[str] = None
    size_bytes: int
    produced_by: Optional[str] = None
    produced_at: Optional[str] = None


class ManifestationRead(BaseModel):
    manifestation_id: str
    work_id: str
    version: Optional[str] = "unknown"
    format: Optional[str] = "unknown"  # 'pdf_born_digital', 'pdf_scanned', 'epub', etc.
    origin: Literal["local", "external"] = "local"
    license_id: Optional[str] = None
    is_retrievable: int = 1
    external_url: Optional[str] = None
    files: List[FileObjectRead] = []


class ReadingStateRead(BaseModel):
    work_id: str
    user_curation_score: Optional[float] = 1.0
    progress_ratio: Optional[float] = 0.0
    last_page: Optional[int] = 1
    total_pages: Optional[int] = 1
    last_opened_at: Optional[str] = None
    added_at: str


class ReadingProgressUpdate(BaseModel):
    progress_ratio: float = Field(..., ge=0.0, le=1.0)
    last_page: int = Field(..., ge=1)
    total_pages: int = Field(..., ge=1)


class WorkBase(BaseModel):
    title: str
    title_provenance: str = "filename_parsed"
    work_type: str = "book"
    language: Optional[str] = None
    publication_year: Optional[int] = None
    authors_display: Optional[str] = None
    availability_tier: int = 0  # 0=已落地已解析, 1=已落地未解析, 2=可取得, 3=僅目錄


class WorkCreate(WorkBase):
    pass


class WorkRead(WorkBase):
    work_id: str
    relevance_authority: Optional[float] = None
    created_at: str
    updated_at: str
    merged_into: Optional[str] = None


class WorkDetailRead(WorkRead):
    identifiers: List[IdentifierRead] = []
    manifestations: List[ManifestationRead] = []
    reading_state: Optional[ReadingStateRead] = None


class SearchResultItem(BaseModel):
    work_id: str
    title: str
    authors_display: Optional[str] = None
    publication_year: Optional[int] = None
    language: Optional[str] = None
    format: Optional[str] = None
    size_bytes: Optional[int] = None
    md5: Optional[str] = None
    availability_tier: int = 0
    snippet: Optional[str] = None
    progress_ratio: Optional[float] = None


class SearchResponse(BaseModel):
    query: str
    total: int
    page: int
    page_size: int
    items: List[SearchResultItem]


# === 個人化書單 (Collections) ===
class CollectionBase(BaseModel):
    name: str
    description: Optional[str] = None
    icon: Optional[str] = "📚"


class CollectionCreate(CollectionBase):
    pass


class CollectionUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None


class CollectionItemAdd(BaseModel):
    work_id: str
    notes: Optional[str] = None


class CollectionItemRead(BaseModel):
    collection_id: str
    work_id: str
    added_at: str
    notes: Optional[str] = None
    sort_order: int = 0
    work: Optional[SearchResultItem] = None


class CollectionRead(CollectionBase):
    collection_id: str
    is_system: int = 0
    created_at: str
    updated_at: str
    items_count: int = 0


class CollectionDetailRead(CollectionRead):
    items: List[CollectionItemRead] = []


# === 多階層分類與線上書攤 (Categories & Bookstalls) ===
class CategoryRead(BaseModel):
    category_id: str
    parent_id: Optional[str] = None
    name: str
    slug: str
    icon: str = "📖"
    level: int = 1
    sort_order: int = 0
    works_count: int = 0


class CategoryTreeNode(CategoryRead):
    children: List["CategoryTreeNode"] = []


class CategoryWorksResponse(BaseModel):
    category: CategoryRead
    total: int
    page: int
    page_size: int
    items: List[SearchResultItem]
