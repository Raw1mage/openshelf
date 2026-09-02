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


DownloadProtocol = Literal["http", "torrent"]


# 每個來源的授權標示（tasks.md 2.2；design.md Risks「Gutenberg 授權非全球公版」）。
#
# **字面值就是契約**：Project Gutenberg 的 `dcterms:rights` 逐字是
# "Public domain in the USA."——只在美國境內為公版。這裡不得改寫成「公版」
# 或 "public domain"，那會把一個有地域限制的授權暗示成全球通用。
#
# libgen 沒有可宣告的授權（來源本身不提供授權資訊），故值為 None——
# 這與「尚未查到」是不同的意思：前者是來源性質，UI 顯示空白即正確。
SOURCE_LICENSE_LABEL: dict[str, Optional[str]] = {
    "gutenberg": "Public domain in the USA.",
}


def license_for_source(source: Optional[str]) -> Optional[str]:
    """由來源推導授權標示。未登錄的來源回 None（不猜、不套用預設授權）。"""
    if not source:
        return None
    return SOURCE_LICENSE_LABEL.get(source)


class TorrentSourceMixin(BaseModel):
    torrent_url: Optional[str] = None
    magnet_uri: Optional[str] = None
    download_protocol: DownloadProtocol = "http"
    peers_count: Optional[int] = None


class ManifestationCreate(TorrentSourceMixin):
    work_id: str
    version: Optional[str] = "unknown"
    format: Optional[str] = "unknown"
    origin: Literal["local", "external"] = "local"
    license_id: Optional[str] = None
    is_retrievable: int = 1
    external_url: Optional[str] = None


class ManifestationRead(TorrentSourceMixin):
    manifestation_id: str
    work_id: str
    version: Optional[str] = "unknown"
    format: Optional[str] = "unknown"
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
    availability_tier: int = 0


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


class SearchResultItem(TorrentSourceMixin):
    work_id: str
    local_work_id: Optional[str] = None
    title: str
    authors_display: Optional[str] = None
    publication_year: Optional[int] = None
    language: Optional[str] = None
    format: Optional[str] = None
    size_bytes: Optional[int] = None
    md5: Optional[str] = None
    extension: Optional[str] = None
    mirror_links: List[str] = []
    availability_tier: int = 0
    source: Optional[str] = None
    # 逐項授權（tasks.md 2.2）。None = 該來源未宣告授權，不是「公版」。
    license: Optional[str] = None
    snippet: Optional[str] = None
    progress_ratio: Optional[float] = None


class DownloadJob(TorrentSourceMixin):
    """下載任務之序列化模型（API 邊界用）。

    註：`app/crawler/download_worker.py` 內另有同名的執行期物件，
    其雙軌調度行為屬 Phase 3 範圍，本階段不觸碰。
    """
    job_id: str
    work_id: Optional[str] = None
    md5: Optional[str] = None
    title: Optional[str] = None
    authors: Optional[str] = None
    extension: Optional[str] = None
    status: str = "queued"
    progress_percent: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    retry_count: int = 0
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


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


class CatalogRefreshStatus(BaseModel):
    status: Literal["never_refreshed", "fresh", "stale", "refreshing", "failed"]
    last_success_at: Optional[str] = None
    error: Optional[str] = None
    accumulated_total: int = 0
    pages_fetched: int = 0
    refresh_scheduled: bool = False


class CategoryWorksResponse(BaseModel):
    category: CategoryRead
    total: int
    page: int
    page_size: int
    catalog_status: CatalogRefreshStatus
    items: List[SearchResultItem]



# === 自訂 Libgen 來源、鏡像驗證與 BR 發送 (Custom Libgen Mirrors & Pre-flight Validation) ===
class LibgenMirrorItem(BaseModel):
    url: str
    enabled: bool = True
    note: Optional[str] = ""
    is_default: bool = False
    priority: int = 0
    validation_status: Literal["verified", "unverified", "offline", "incompatible_layout"] = "unverified"
    adapter_type: Optional[str] = "unknown"  # 'libgen_li', 'libgen_is', 'direct_gateway', 'unknown'
    last_validated_at: Optional[str] = None
    latency_ms: Optional[float] = None
    sample_records_count: Optional[int] = 0
    br_id: Optional[str] = None
    last_error: Optional[str] = None


class LibgenMirrorsUpdateRequest(BaseModel):
    mirrors: List[LibgenMirrorItem]


class LibgenMirrorValidateRequest(BaseModel):
    url: str
    auto_dispatch_br: bool = True


class LibgenMirrorValidationReport(BaseModel):
    url: str
    is_online: bool
    status_code: Optional[int] = None
    latency_ms: Optional[float] = None
    validation_status: Literal["verified", "unverified", "offline", "incompatible_layout"]
    adapter_type: str
    sample_records_count: int = 0
    error_message: Optional[str] = None
    br_id: Optional[str] = None
    br_path: Optional[str] = None
    dispatched_br: bool = False
