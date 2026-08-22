from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.concurrency import run_in_threadpool
from app.db.dao import CatalogDAO
from app.crawler.libgen_live import LibgenCrawler
from app.models.catalog import CategoryRead, CategoryTreeNode, CategoryWorksResponse, SearchResultItem
from app.db.categories import CATEGORY_CLOUD_SEARCH_QUERIES

router = APIRouter(prefix="/api/categories", tags=["Categories"])

_crawler = None


def get_dao() -> CatalogDAO:
    return CatalogDAO()


def get_crawler() -> LibgenCrawler:
    global _crawler
    if _crawler is None:
        _crawler = LibgenCrawler(dao=CatalogDAO())
    return _crawler


@router.get("/tree", response_model=List[CategoryTreeNode])
def get_category_tree(dao: CatalogDAO = Depends(get_dao)):
    """取得多階層樹狀分類目錄（含各節點總藏書統計）。"""
    return dao.get_category_tree()


@router.get("/{category_id}", response_model=CategoryRead)
def get_category(category_id: str, dao: CatalogDAO = Depends(get_dao)):
    """取得單一分類詳情。"""
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
    crawler: LibgenCrawler = Depends(get_crawler)
):
    """取得特定分類（及子分類）下所有可逛書目；可由呼叫端停用雲端探索。

    本路由是 async def，body 直接跑在事件迴圈執行緒上。所有同步 SQLite 讀一律
    丟進 threadpool——否則整個 process 的所有請求（含不碰 DB 的端點）都會被卡住。
    見 BR-20260820_210000 B 節。
    """
    cat = await run_in_threadpool(dao.get_category, category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="找不到該分類")

    total, local_items = await run_in_threadpool(
        dao.get_category_works, category_id, page=page, page_size=page_size
    )

    items: List[SearchResultItem] = []
    seen_md5s = set()

    for w in local_items:
        md5_clean = (w.md5 or "").lower()
        if md5_clean:
            seen_md5s.add(md5_clean)
        items.append(SearchResultItem(
            work_id=w.work_id,
            local_work_id=w.work_id,
            title=w.title,
            authors_display=w.authors_display,
            publication_year=w.publication_year,
            language=w.language,
            format=w.format,
            size_bytes=w.size_bytes,
            md5=w.md5,
            availability_tier=0,  # 本地落地典藏
            snippet=""
        ))

    cloud_status = "not_requested"

    # 若開啟雲端探測且本地藏書較少或在第一頁，動態向 Libgen 探測該領域精選雲端書目
    if include_cloud and (len(items) < page_size or page == 1):
        cloud_status = "success"
        cloud_items: List[SearchResultItem] = []
        cloud_query = CATEGORY_CLOUD_SEARCH_QUERIES.get(category_id) or cat.name
        try:
            raw_cloud = await crawler.search(cloud_query)
            cloud_slice = raw_cloud[:15]

            # 本地落地檢查改為單次批次查詢（原本是每筆一次 DB 往返，最多 15 次），
            # 並整段丟進 threadpool。與 crawler_routes._annotate_local_status 同一形狀，
            # 沿用 dao.find_works_by_hashes()（BR-20260820_210000 B 節）。
            lookup_md5s = []
            for cr in cloud_slice:
                m = (cr.get("md5") or "").lower()
                if m and m not in seen_md5s:
                    lookup_md5s.append(m)

            local_map = (
                await run_in_threadpool(dao.find_works_by_hashes, lookup_md5s)
                if lookup_md5s
                else {}
            )

            for cr in cloud_slice:
                cr_md5 = (cr.get("md5") or "").lower()
                if cr_md5 and cr_md5 in seen_md5s:
                    continue
                if cr_md5:
                    seen_md5s.add(cr_md5)

                # 檢查是否已在本地落地
                local_wid = local_map.get(cr_md5) if cr_md5 else None
                tier = 0 if local_wid else 1

                cloud_items.append(SearchResultItem(
                    work_id=local_wid or cr.get("work_id", f"libgen_{cr_md5}"),
                    local_work_id=local_wid,
                    title=cr.get("title", "未知書名"),
                    authors_display=cr.get("authors_display", "未知作者"),
                    publication_year=cr.get("publication_year"),
                    language=cr.get("language", "en"),
                    format=cr.get("format", "pdf_born_digital"),
                    size_bytes=cr.get("size_bytes", 0),
                    md5=cr_md5,
                    availability_tier=tier,
                    snippet=""
                ))
            items.extend(cloud_items)
        except Exception:
            cloud_status = "failed"

    return CategoryWorksResponse(
        category=cat,
        total=len(items) if include_cloud else total,
        page=page,
        page_size=page_size,
        cloud_status=cloud_status,
        items=items
    )
