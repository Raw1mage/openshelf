from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from app.crawler.libgen_live import LibgenCrawler
from app.crawler.remote_catalog_refresh import RemoteCatalogRefresher
from app.db.categories import CATEGORY_CLOUD_SEARCH_QUERIES
from app.db.dao import CatalogDAO
from app.db.remote_catalog import RemoteCatalogDAO
from app.models.catalog import (
    CatalogRefreshStatus,
    CategoryRead,
    CategoryTreeNode,
    CategoryWorksResponse,
    SearchResultItem,
)

router = APIRouter(prefix="/api/categories", tags=["Categories"])

_crawler = None


def get_dao() -> CatalogDAO:
    return CatalogDAO()


def get_remote_catalog_dao() -> RemoteCatalogDAO:
    return RemoteCatalogDAO()


def get_crawler() -> LibgenCrawler:
    global _crawler
    if _crawler is None:
        _crawler = LibgenCrawler(dao=CatalogDAO())
    return _crawler


def get_refresher(
    remote_dao: RemoteCatalogDAO = Depends(get_remote_catalog_dao),
    crawler: LibgenCrawler = Depends(get_crawler),
) -> RemoteCatalogRefresher:
    return RemoteCatalogRefresher(remote_dao, crawler)


@router.get("/tree", response_model=List[CategoryTreeNode])
def get_category_tree(dao: CatalogDAO = Depends(get_dao)):
    return dao.get_category_tree()


@router.get("/{category_id}", response_model=CategoryRead)
def get_category(category_id: str, dao: CatalogDAO = Depends(get_dao)):
    cat = dao.get_category(category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="找不到該分類")
    return cat


@router.get("/{category_id}/works", response_model=CategoryWorksResponse)
async def get_category_works(
    category_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    include_cloud: bool = Query(True),
    dao: CatalogDAO = Depends(get_dao),
    remote_dao: RemoteCatalogDAO = Depends(get_remote_catalog_dao),
    refresher: RemoteCatalogRefresher = Depends(get_refresher),
    crawler: LibgenCrawler = Depends(get_crawler),
):
    """先讀 SQLite 即時回應；需要時僅排入背景刷新，不等待外網。"""
    cat = await run_in_threadpool(dao.get_category, category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="找不到該分類")

    if not hasattr(remote_dao, "query_browseable"):
        local_total, local_items = await run_in_threadpool(
            dao.get_category_works, category_id, page, page_size
        )
        stored_items = list(local_items)
        if include_cloud:
            query_term = CATEGORY_CLOUD_SEARCH_QUERIES.get(category_id) or cat.name
            cloud_items = await crawler.search(query_term, max_results=page_size)
            hashes = [item.get("md5", "").lower() for item in cloud_items if item.get("md5")]
            landed = await run_in_threadpool(dao.find_works_by_hashes, hashes)
            for raw_item in cloud_items:
                item = dict(raw_item)
                md5 = item.get("md5", "").lower()
                local_work_id = landed.get(md5)
                item.update(
                    availability_tier=0 if local_work_id else 1,
                    local_work_id=local_work_id,
                    work_id=local_work_id or f"libgen_{md5}",
                )
                stored_items.append(item)
        total = local_total + max(0, len(stored_items) - len(local_items))
        items = [SearchResultItem(**item) for item in stored_items]
        return CategoryWorksResponse(
            category=cat.model_copy(update={"works_count": total}),
            total=total,
            page=page,
            page_size=page_size,
            catalog_status=CatalogRefreshStatus(
                status="never_refreshed",
                accumulated_total=total,
                pages_fetched=0,
                refresh_scheduled=False,
            ),
            items=items,
        )

    total, stored_items = await run_in_threadpool(
        remote_dao.query_browseable, category_id, page, page_size
    )
    status = await run_in_threadpool(remote_dao.get_status, category_id)

    refresh_scheduled = False
    if include_cloud and status["status"] in {"never_refreshed", "stale", "failed"}:
        query_term = CATEGORY_CLOUD_SEARCH_QUERIES.get(category_id) or cat.name
        refresh_scheduled = refresher.schedule(category_id, query_term)

    items = [SearchResultItem(**item) for item in stored_items]
    return CategoryWorksResponse(
        category=cat.model_copy(update={"works_count": total}),
        total=total,
        page=page,
        page_size=page_size,
        catalog_status=CatalogRefreshStatus(
            **status,
            refresh_scheduled=refresh_scheduled,
        ),
        items=items,
    )
