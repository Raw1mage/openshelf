from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Query, Depends
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.crawler.libgen_live import LibgenCrawler
from app.crawler.download_worker import DownloadWorker
from app.db.dao import CatalogDAO

router = APIRouter(prefix="/api/crawler")

# 延遲單例
_crawler = None
_worker = None

def get_crawler() -> LibgenCrawler:
    global _crawler
    if _crawler is None:
        _crawler = LibgenCrawler(dao=CatalogDAO())
    return _crawler

def get_worker() -> DownloadWorker:
    global _worker
    if _worker is None:
        _worker = DownloadWorker()
    return _worker

def get_dao():
    return CatalogDAO()


class DownloadRequestItem(BaseModel):
    md5: str
    title: str
    authors: Optional[str] = None
    extension: Optional[str] = "pdf"
    mirror_links: Optional[List[str]] = []
    # None 代表「不知道」。舊前端（或任何不帶這個欄位的 client）送來的 payload
    # 必須仍能通過驗證，所以這裡是 Optional 而非必填。
    publication_year: Optional[int] = None


class BatchDownloadRequest(BaseModel):
    items: List[DownloadRequestItem]


@router.get("/search")
async def live_search(
    q: str = Query(..., description="公網搜尋關鍵字"),
    crawler: LibgenCrawler = Depends(get_crawler),
    dao: CatalogDAO = Depends(get_dao),
    worker: DownloadWorker = Depends(get_worker)
):
    """即時檢索公網 Libgen 書庫並標註本地落地與佇列中狀態。"""
    raw_results = await crawler.search(q)

    # 標註工作含同步 SQLite I/O。本路由是 async def，直接跑在事件迴圈執行緒上，
    # 因此整段丟進 threadpool——否則爬蟲期間會卡住整個 process 的所有請求，
    # 包括完全不碰 DB 的端點（見 BR-20260820_200000）。
    await run_in_threadpool(_annotate_local_status, raw_results, dao, worker)

    return {
        "query": q,
        "total": len(raw_results),
        "items": raw_results
    }


def _annotate_local_status(
    raw_results: List[Dict[str, Any]],
    dao: CatalogDAO,
    worker: DownloadWorker
) -> None:
    """就地標註本地落地狀態與佇列狀態。純同步，預期在 threadpool 內執行。

    本地命中改為單次批次查詢（原本是每筆一次 DB 往返）。
    """
    md5s = []
    for item in raw_results:
        md5 = (item.get("md5") or "").lower()
        if md5:
            md5s.append(md5)

    local_map = dao.find_works_by_hashes(md5s) if md5s else {}

    for item in raw_results:
        md5 = (item.get("md5") or "").lower()
        if not md5:
            continue

        local_wid = local_map.get(md5)
        if local_wid:
            item["availability_tier"] = 0
            item["local_work_id"] = local_wid
            continue

        # 檢查是否在當前下載佇列中
        for j in worker.jobs.values():
            if j.md5 == md5:
                item["queue_status"] = j.status
                item["queue_progress"] = j.progress_percent
                item["queue_job_id"] = j.job_id
                if j.status == "completed" and j.work_id:
                    item["availability_tier"] = 0
                    item["local_work_id"] = j.work_id
                break


@router.post("/download")
async def enqueue_single_download(
    req: DownloadRequestItem,
    worker: DownloadWorker = Depends(get_worker)
):
    """將單一公網書籍加入下載與落地佇列。"""
    if not req.md5:
        raise HTTPException(status_code=400, detail="必須提供書籍之 MD5 指紋")

    job = worker.enqueue(
        md5=req.md5,
        title=req.title,
        authors=req.authors,
        extension=req.extension or "pdf",
        mirror_links=req.mirror_links,
        publication_year=req.publication_year
    )
    return job.to_dict()


@router.post("/batch-download")
async def enqueue_batch_download(
    req: BatchDownloadRequest,
    worker: DownloadWorker = Depends(get_worker)
):
    """批次加入多本書籍至下載佇列。"""
    enqueued_jobs = []
    for item in req.items:
        if item.md5:
            job = worker.enqueue(
                md5=item.md5,
                title=item.title,
                authors=item.authors,
                extension=item.extension or "pdf",
                mirror_links=item.mirror_links,
                publication_year=item.publication_year
            )
            enqueued_jobs.append(job.to_dict())

    return {
        "status": "ok",
        "enqueued_count": len(enqueued_jobs),
        "jobs": enqueued_jobs
    }


@router.get("/jobs")
async def list_download_jobs(worker: DownloadWorker = Depends(get_worker)):
    """檢視下載佇列所有任務狀態。"""
    return worker.list_jobs()


@router.get("/jobs/{job_id}")
async def get_job_status(job_id: str, worker: DownloadWorker = Depends(get_worker)):
    """查詢特定下載任務狀態。"""
    job = worker.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="找不到指定任務")
    return job


@router.post("/jobs/clear-completed")
async def clear_completed_jobs(worker: DownloadWorker = Depends(get_worker)):
    """清除佇列中所有已完成的下載記錄。"""
    count = worker.clear_completed()
    return {"cleared_count": count, "jobs": worker.list_jobs()}


@router.post("/jobs/{job_id}/start")
async def start_download_job(job_id: str, worker: DownloadWorker = Depends(get_worker)):
    """主動啟動特定任務的下載（立即執行）。"""
    job = worker.start_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="找不到指定任務")
    return job.to_dict()


@router.post("/jobs/{job_id}/pause")
async def pause_download_job(job_id: str, worker: DownloadWorker = Depends(get_worker)):
    """暫停正在排隊或下載中的任務。"""
    job = worker.pause_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="找不到指定任務")
    return job.to_dict()


@router.post("/jobs/{job_id}/resume")
async def resume_download_job(job_id: str, worker: DownloadWorker = Depends(get_worker)):
    """繼續已暫停的任務。"""
    job = worker.resume_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="找不到指定任務")
    return job.to_dict()


@router.delete("/jobs/{job_id}")
@router.post("/jobs/{job_id}/delete")
async def delete_download_job(job_id: str, worker: DownloadWorker = Depends(get_worker)):
    """刪除特定任務並清理臨時檔案。"""
    success = worker.delete_job(job_id)
    if not success:
        raise HTTPException(status_code=404, detail="找不到指定任務")
    return {"status": "ok", "deleted_job_id": job_id}


@router.post("/jobs/{job_id}/retry")
async def retry_download_job(job_id: str, worker: DownloadWorker = Depends(get_worker)):
    """手動重試失敗的下載任務。"""
    job = worker.retry_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="找不到指定任務")
    return job.to_dict()
