from pathlib import Path
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Query

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

    # 同步回寫至資料庫中的鏡像清單
    mirrors = dao.get_libgen_mirrors()
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

    dao.save_libgen_mirrors(mirrors)
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
    if not issues_dir.exists():
        return {"total": 0, "issues": []}

    files = sorted(issues_dir.glob("BR-*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
    issue_list = []
    for f in files:
        content = f.read_text(encoding="utf-8")
        title_line = content.split("\n")[0] if content else f.name
        issue_list.append({
            "br_id": f.stem,
            "title": title_line.replace("# ", "").strip(),
            "file_name": f.name,
            "updated_at": f.stat().st_mtime,
            "path": str(f)
        })

    return {"total": len(issue_list), "issues": issue_list}
