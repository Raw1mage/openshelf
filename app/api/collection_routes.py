from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from app.db.dao import CatalogDAO
from app.models.catalog import (
    CollectionCreate, CollectionUpdate, CollectionRead,
    CollectionDetailRead, CollectionItemAdd
)

router = APIRouter(prefix="/collections", tags=["Collections"])


def get_dao() -> CatalogDAO:
    return CatalogDAO()


@router.get("", response_model=List[CollectionRead])
def list_collections(dao: CatalogDAO = Depends(get_dao)):
    """列出所有自訂個人書單。"""
    return dao.list_collections()


@router.post("", response_model=dict, status_code=201)
def create_collection(col: CollectionCreate, dao: CatalogDAO = Depends(get_dao)):
    """建立新的自訂書單。"""
    if not col.name.strip():
        raise HTTPException(status_code=400, detail="書單名稱不可為空")
    cid = dao.create_collection(col)
    return {"status": "ok", "collection_id": cid, "message": "書單建立成功"}


@router.get("/{collection_id}", response_model=CollectionDetailRead)
def get_collection(collection_id: str, dao: CatalogDAO = Depends(get_dao)):
    """取得單一書單詳情及內含的所有書籍。"""
    col = dao.get_collection(collection_id)
    if not col:
        raise HTTPException(status_code=404, detail="找不到該書單")
    return col


@router.put("/{collection_id}", response_model=dict)
def update_collection(collection_id: str, update: CollectionUpdate, dao: CatalogDAO = Depends(get_dao)):
    """修改書單名稱、描述或圖示。"""
    success = dao.update_collection(collection_id, update)
    if not success:
        raise HTTPException(status_code=400, detail="更新失敗或無欄位變更")
    return {"status": "ok", "message": "書單更新成功"}


@router.delete("/{collection_id}", response_model=dict)
def delete_collection(collection_id: str, dao: CatalogDAO = Depends(get_dao)):
    """刪除自訂書單（系統預設書單不可刪除）。"""
    success = dao.delete_collection(collection_id)
    if not success:
        raise HTTPException(status_code=400, detail="系統預設書單不可刪除或書單不存在")
    return {"status": "ok", "message": "書單已刪除"}


@router.post("/{collection_id}/items", response_model=dict)
def add_item_to_collection(
    collection_id: str,
    item: CollectionItemAdd,
    dao: CatalogDAO = Depends(get_dao)
):
    """將書籍加入書單。"""
    col = dao.get_collection(collection_id)
    if not col:
        raise HTTPException(status_code=404, detail="找不到該書單")
    dao.add_work_to_collection(collection_id, item.work_id, item.notes)
    return {"status": "ok", "message": "已成功加入書單"}


@router.delete("/{collection_id}/items/{work_id}", response_model=dict)
def remove_item_from_collection(
    collection_id: str,
    work_id: str,
    dao: CatalogDAO = Depends(get_dao)
):
    """從書單中移除書籍。"""
    success = dao.remove_work_from_collection(collection_id, work_id)
    return {"status": "ok", "removed": success}


@router.get("/work/{work_id}/status", response_model=List[str])
def get_work_collections(work_id: str, dao: CatalogDAO = Depends(get_dao)):
    """查詢某本書籍已加入哪些書單（回傳 collection_id 陣列）。"""
    return dao.get_work_collections(work_id)
