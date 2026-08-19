from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends, Query
from app.db.dao import CatalogDAO
from app.models.catalog import CategoryRead, CategoryTreeNode, CategoryWorksResponse

router = APIRouter(prefix="/categories", tags=["Categories"])


def get_dao() -> CatalogDAO:
    return CatalogDAO()


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
def get_category_works(
    category_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    dao: CatalogDAO = Depends(get_dao)
):
    """取得特定分類（及子分類）下的所有藏書（線上書架漫遊）。"""
    cat = dao.get_category(category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="找不到該分類")
    total, items = dao.get_category_works(category_id, page=page, page_size=page_size)
    return CategoryWorksResponse(
        category=cat,
        total=total,
        page=page,
        page_size=page_size,
        items=items
    )
