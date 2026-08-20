import logging
import os
from pathlib import Path
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from app.db.dao import CatalogDAO
from app.db.engine import DatabaseEngine
from app.models.catalog import (
    LibgenMirrorItem,
    LibgenMirrorsUpdateRequest,
    LibgenMirrorValidateRequest,
    LibgenMirrorValidationReport
)
from app.crawler.validator import MirrorValidator

router = APIRouter(prefix="/api/settings", tags=["Settings"])

log = logging.getLogger(__name__)

def get_dao() -> CatalogDAO:
    return CatalogDAO()


def get_validator() -> MirrorValidator:
    return MirrorValidator()



@router.get("/libgen-mirrors", response_model=List[LibgenMirrorItem])
def get_libgen_mirrors(dao: CatalogDAO = Depends(get_dao)):
    """取得目前所有 Libgen 來源鏡像設定清單（包含驗證狀態與適配器分類）。"""
    mirrors_raw = dao.get_libgen_mirrors()
    return [LibgenMirrorItem(**m) for m in mirrors_raw]


@router.post("/libgen-mirrors", response_model=List[LibgenMirrorItem])
def update_libgen_mirrors(
    req: LibgenMirrorsUpdateRequest,
    dao: CatalogDAO = Depends(get_dao)
):
    """更新並保存 Libgen 鏡像清單（排序、啟用/停用、新增或刪除）。"""
    mirrors_dicts = [m.model_dump() for m in req.mirrors]
    dao.save_libgen_mirrors(mirrors_dicts)
    return [LibgenMirrorItem(**m) for m in dao.get_libgen_mirrors()]


@router.post("/libgen-mirrors/validate", response_model=LibgenMirrorValidationReport)
async def validate_libgen_mirror(
    req: LibgenMirrorValidateRequest,
    dao: CatalogDAO = Depends(get_dao),
    validator: MirrorValidator = Depends(get_validator)
):
    """
    執行單一鏡像來源之上線前預檢驗證（Pre-flight Validation）。
    若連線正常但爬蟲適配器結構無法解析，將自動產生 BR 檔案並記錄。
    驗證通過後自動將狀態寫入鏡像資料庫。
    """
    report = await validator.validate_mirror(req.url, auto_dispatch_br=req.auto_dispatch_br)

    # 同步回寫至資料庫中的鏡像清單。
    # 本路由是 async def，兩次 DB 存取直接呼叫會落在事件迴圈執行緒上
    # （BR-20260820_210000 D 節）。dao.current_iso() 是純計算，不需搬。
    mirrors = await run_in_threadpool(dao.get_libgen_mirrors)
    target_url = req.url.strip().rstrip("/")
    updated = False

    for m in mirrors:
        cur_url = m.get("url", "").strip().rstrip("/")
        if cur_url.lower() == target_url.lower() or cur_url.replace("https://", "").replace("http://", "") == target_url.replace("https://", "").replace("http://", ""):
            m["validation_status"] = report.validation_status
            m["adapter_type"] = report.adapter_type
            m["latency_ms"] = report.latency_ms
            m["sample_records_count"] = report.sample_records_count
            m["last_validated_at"] = dao.current_iso()
            m["br_id"] = report.br_id
            m["last_error"] = report.error_message
            # 若驗證不通過或無法解析，自動取消啟用正式運作
            if report.validation_status != "verified":
                m["enabled"] = False
            else:
                m["enabled"] = True
            updated = True
            break

    if not updated:
        # 若為全新自訂來源，自動附加並標註
        new_mirror = {
            "url": report.url,
            "enabled": (report.validation_status == "verified"),
            "note": "自訂鏡像來源",
            "is_default": False,
            "priority": len(mirrors) + 1,
            "validation_status": report.validation_status,
            "adapter_type": report.adapter_type,
            "latency_ms": report.latency_ms,
            "sample_records_count": report.sample_records_count,
            "last_validated_at": dao.current_iso(),
            "br_id": report.br_id,
            "last_error": report.error_message
        }
        mirrors.append(new_mirror)

    await run_in_threadpool(dao.save_libgen_mirrors, mirrors)
    return report


@router.post("/libgen-mirrors/reset", response_model=List[LibgenMirrorItem])
def reset_libgen_mirrors(dao: CatalogDAO = Depends(get_dao)):
    """重設回系統預設之 Libgen 鏡像清單。"""
    defaults = dao.reset_libgen_mirrors()
    return [LibgenMirrorItem(**m) for m in defaults]


@router.get("/libgen-mirrors/issues")
def list_dispatched_issues():
    """列出目前 issues/ 目錄下自動產生之所有 Libgen 鏡像解析問題報告 (BR)。"""
    issues_dir = Path(__file__).parent.parent.parent / "issues"

    # BR-20260821_020000：來源目錄不可用時，絕不可與「目錄在、但真的沒有 BR」
    # 共用同一個輸出。兩者的 total 都會是 0，所以差異必須由一個獨立欄位承載。
    #
    # 命名刻意用 source_available 而非 mount_ok：本函式唯一觀察得到的事實是
    # 「這個路徑不是一個可用目錄」，它**無法**分辨成因（容器沒掛載 / 路徑解析
    # 改變 / 權限不足 / 該位置是一個檔案）。叫 mount_ok 等於用欄位名斷言一個
    # 本檢查證明不了的成因，那本身就是新的一次「兩態共用一個輸出」。
    # 成因留給 log 與 source_path 讓人去判讀，欄位只陳述可觀察到的事實。
    if not issues_dir.is_dir():
        log.error(
            "BR 清單來源目錄不可用，讀取端回傳空清單（total=0）："
            "resolved_path=%s path_exists=%s。"
            "可能成因：容器未掛載 ./issues、路徑解析改變、權限不足、"
            "或該位置存在但不是目錄——本檢查無法分辨是哪一種。",
            issues_dir,
            issues_dir.exists(),
        )
        return {
            "total": 0,
            "issues": [],
            "source_available": False,
            "source_path": str(issues_dir),
        }

    # 本函式是同步 def（跑在 anyio threadpool，不阻塞事件迴圈），但它佔用的是
    # 全 app 共用的 40 個 threadpool 名額——BR-20260820_210000 A+B 節的修復已經
    # 把壓力從 loop 移到這個池子。所以這裡的修法不是再加一次 threadpool hop
    # （那只是換一個 token，佔用數不變），而是**縮短單次佔用的時間**：
    #   1. scandir 的 DirEntry 自帶 stat 快取 → 原本每檔 stat 兩次，現在一次
    #   2. 只要第一行標題，改 readline() → 不再把整份 BR 全文讀進記憶體
    entries = []
    with os.scandir(issues_dir) as it:
        for entry in it:
            if not entry.name.startswith("BR-") or not entry.name.endswith(".md"):
                continue
            if not entry.is_file():
                continue
            entries.append((entry.stat().st_mtime, entry.name, entry.path))

    entries.sort(key=lambda t: t[0], reverse=True)

    issue_list = []
    for mtime, name, path in entries:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                title_line = fh.readline()
        except OSError:
            title_line = ""
        issue_list.append({
            "br_id": name[:-3],
            "title": (title_line or name).replace("# ", "").strip(),
            "file_name": name,
            "updated_at": mtime,
            "path": path
        })

    return {
        "total": len(issue_list),
        "issues": issue_list,
        "source_available": True,
        "source_path": str(issues_dir),
    }
