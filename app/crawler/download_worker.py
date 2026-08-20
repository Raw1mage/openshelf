import os
import re
import uuid
import hashlib
import asyncio
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
import httpx

from app.crawler.mirror_resolver import MirrorResolver
from app.pipeline.ingest import IngestionPipeline


class DownloadJob:
    """下載任務實體物件。"""
    def __init__(
        self,
        job_id: str,
        md5: str,
        title: str,
        authors: Optional[str] = None,
        extension: str = "pdf",
        mirror_links: Optional[List[str]] = None
    ):
        self.job_id = job_id
        self.md5 = md5.lower()
        self.title = title
        self.authors = authors
        self.extension = extension.lower()
        self.mirror_links = mirror_links or []
        self.status = "queued"  # queued, downloading, paused, completed, failed
        self.progress_percent = 0
        self.downloaded_bytes = 0
        self.total_bytes = 0
        self.retry_count = 0
        self.error_message: Optional[str] = None
        self.work_id: Optional[str] = None
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.updated_at = self.created_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "md5": self.md5,
            "title": self.title,
            "authors": self.authors,
            "extension": self.extension,
            "status": self.status,
            "progress_percent": self.progress_percent,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "retry_count": self.retry_count,
            "error_message": self.error_message,
            "work_id": self.work_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at
        }


class DownloadWorker:
    """管理非同步鏡像下載佇列、暫停/繼續、刪除、HTTP Range 斷點續傳與自動入庫。"""

    def __init__(self, pipeline: IngestionPipeline = None, resolver: MirrorResolver = None):
        self.pipeline = pipeline or IngestionPipeline()
        self.resolver = resolver or MirrorResolver(dao=self.pipeline.dao if hasattr(self.pipeline, "dao") else None)
        self.jobs: Dict[str, DownloadJob] = {}
        self.queue: asyncio.Queue[DownloadJob] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._active_tasks: Dict[str, asyncio.Task] = {}
        self._jobs_file = self.pipeline.storage.db_dir / "download_jobs.json"
        self._load_jobs_from_disk()

    def _get_part_path(self, job: DownloadJob) -> Path:
        return self.pipeline.storage.raw_dir / f"{job.job_id}_{job.md5}.part"

    def _save_jobs_to_disk(self):
        """將任務狀態持久化寫入磁碟 JSON 檔。"""
        try:
            import json
            data = [j.to_dict() for j in self.jobs.values()]
            tmp_file = self._jobs_file.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp_file.replace(self._jobs_file)
        except Exception:
            pass

    def _load_jobs_from_disk(self):
        """從磁碟載入歷史與未完成任務。"""
        if not self._jobs_file.exists():
            return
        try:
            import json
            with open(self._jobs_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                job = DownloadJob(
                    job_id=item["job_id"],
                    md5=item["md5"],
                    title=item["title"],
                    authors=item.get("authors"),
                    extension=item.get("extension", "pdf"),
                    mirror_links=item.get("mirror_links", [])
                )
                job.status = item.get("status", "queued")
                job.progress_percent = item.get("progress_percent", 0)
                job.downloaded_bytes = item.get("downloaded_bytes", 0)
                job.total_bytes = item.get("total_bytes", 0)
                job.retry_count = item.get("retry_count", 0)
                job.error_message = item.get("error_message")
                job.work_id = item.get("work_id")
                job.created_at = item.get("created_at", datetime.now(timezone.utc).isoformat())
                job.updated_at = item.get("updated_at", job.created_at)
                self.jobs[job.job_id] = job
                
                # 若之前處於排隊或下載中，重新入列繼續下載；若 paused 則保持 paused
                if job.status in ("queued", "downloading"):
                    job.status = "queued"
                    self.queue.put_nowait(job)
        except Exception:
            pass

    def start(self):
        """啟動背景 Worker 監聽循環。"""
        try:
            loop = asyncio.get_running_loop()
            if self._worker_task is None or self._worker_task.done():
                self._worker_task = loop.create_task(self._process_queue())
        except RuntimeError:
            pass

    def enqueue(
        self,
        md5: str,
        title: str,
        authors: Optional[str] = None,
        extension: str = "pdf",
        mirror_links: Optional[List[str]] = None
    ) -> DownloadJob:
        """將書籍加入下載佇列。"""
        for j in self.jobs.values():
            if j.md5 == md5.lower() and j.status in ("queued", "downloading", "paused", "completed"):
                return j

        job_id = f"job_{uuid.uuid4().hex[:12]}"
        job = DownloadJob(
            job_id=job_id,
            md5=md5,
            title=title,
            authors=authors,
            extension=extension,
            mirror_links=mirror_links
        )
        self.jobs[job_id] = job
        self._save_jobs_to_disk()
        self.queue.put_nowait(job)
        self.start()
        return job

    def start_job(self, job_id: str) -> Optional[DownloadJob]:
        """主動啟動特定任務的下載（立即執行）。"""
        job = self.jobs.get(job_id)
        if not job or job.status in ("downloading", "completed"):
            return job
        job.status = "queued"
        job.error_message = None
        self._save_jobs_to_disk()
        
        try:
            loop = asyncio.get_running_loop()
            if job_id not in self._active_tasks or self._active_tasks[job_id].done():
                task = loop.create_task(self._run_single_job(job))
                self._active_tasks[job_id] = task
        except RuntimeError:
            self.queue.put_nowait(job)
            self.start()
        return job

    async def _run_single_job(self, job: DownloadJob):
        """直接執行單一任務下載與入庫。"""
        try:
            await self._execute_download_with_resume(job)
        except asyncio.CancelledError:
            if job.job_id in self.jobs and job.status != "paused":
                job.status = "paused"
                job.error_message = "已暫停"
        except Exception as e:
            job.status = "failed"
            job.error_message = str(e)
            job.updated_at = datetime.now(timezone.utc).isoformat()
        finally:
            self._active_tasks.pop(job.job_id, None)
            self._save_jobs_to_disk()

    def pause_job(self, job_id: str) -> Optional[DownloadJob]:
        """暫停正在排隊或下載中的任務。"""
        job = self.jobs.get(job_id)
        if not job or job.status in ("completed", "failed"):
            return job
        job.status = "paused"
        job.error_message = "已暫停下載"
        job.updated_at = datetime.now(timezone.utc).isoformat()
        
        # 若正在執行下載協程，取消當前傳輸 task
        task = self._active_tasks.get(job_id)
        if task and not task.done():
            task.cancel()

        self._save_jobs_to_disk()
        return job

    def resume_job(self, job_id: str) -> Optional[DownloadJob]:
        """繼續已暫停的任務（立即啟動）。"""
        return self.start_job(job_id)

    def delete_job(self, job_id: str) -> bool:
        """刪除任務（排隊中、下載中、已暫停或已完成均可刪除），並清除臨時斷點檔案。"""
        job = self.jobs.get(job_id)
        if not job:
            return False

        # 若正在下載，先取消 task
        task = self._active_tasks.get(job_id)
        if task and not task.done():
            task.cancel()

        # 刪除臨時斷點檔案
        part_file = self._get_part_path(job)
        if part_file.exists():
            try:
                part_file.unlink()
            except Exception:
                pass

        del self.jobs[job_id]
        self._save_jobs_to_disk()
        return True

    def retry_job(self, job_id: str) -> Optional[DownloadJob]:
        """手動重試失敗的下載任務（立即啟動）。"""
        job = self.jobs.get(job_id)
        if not job:
            return None
        job.status = "queued"
        job.error_message = None
        job.retry_count = 0
        job.updated_at = datetime.now(timezone.utc).isoformat()
        return self.start_job(job_id)

    def clear_completed(self) -> int:
        """清理所有已完成狀態的下載任務記錄。"""
        completed_ids = [jid for jid, j in self.jobs.items() if j.status == "completed"]
        for jid in completed_ids:
            del self.jobs[jid]
        if completed_ids:
            self._save_jobs_to_disk()
        return len(completed_ids)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        job = self.jobs.get(job_id)
        return job.to_dict() if job else None

    def list_jobs(self) -> List[Dict[str, Any]]:
        return [j.to_dict() for j in reversed(list(self.jobs.values()))]

    async def _process_queue(self):
        """依序取出佇列中的任務執行下載與落地。"""
        while True:
            job = await self.queue.get()
            if job.status == "paused" or job.job_id not in self.jobs:
                self.queue.task_done()
                continue

            current_task = asyncio.current_task()
            self._active_tasks[job.job_id] = current_task

            try:
                await self._execute_download_with_resume(job)
            except asyncio.CancelledError:
                # 任務被手動暫停或刪除
                if job.job_id in self.jobs and job.status != "paused":
                    job.status = "paused"
                    job.error_message = "已暫停"
            except Exception as e:
                job.status = "failed"
                job.error_message = str(e)
                job.updated_at = datetime.now(timezone.utc).isoformat()
            finally:
                self._active_tasks.pop(job.job_id, None)
                self._save_jobs_to_disk()
                self.queue.task_done()

    async def _execute_download_with_resume(self, job: DownloadJob):
        """具備斷點續傳 (HTTP Range) 與指數退避重試的穩健下載器。"""
        job.status = "downloading"
        job.updated_at = datetime.now(timezone.utc).isoformat()

        part_file = self._get_part_path(job)
        max_attempts = 6
        last_error = None

        for attempt in range(max_attempts):
            if job.status == "paused" or job.job_id not in self.jobs:
                return

            job.retry_count = attempt
            if attempt > 0:
                backoff_sec = min(2 ** (attempt - 1), 6)
                job.error_message = f"連線中斷，正在自動斷點續傳 (重試 {attempt}/{max_attempts})..."
                job.updated_at = datetime.now(timezone.utc).isoformat()
                await asyncio.sleep(backoff_sec)

            # 1. 解析可用直鏈
            try:
                direct_url = await self.resolver.resolve_download_url(job.md5, job.mirror_links)
            except Exception as e:
                last_error = e
                continue

            if not direct_url:
                last_error = RuntimeError("無法從可用鏡像節點取得有效直鏈下載 URL")
                continue

            # 2. 構建續傳請求標頭 (攜帶 Referer 與 Range)
            headers = {
                "User-Agent": self.resolver.USER_AGENT,
                "Referer": f"https://libgen.li/ads.php?md5={job.md5}",
                "Accept": "*/*"
            }
            start_byte = part_file.stat().st_size if part_file.exists() else 0
            if start_byte > 0:
                headers["Range"] = f"bytes={start_byte}-"

            try:
                async with httpx.AsyncClient(headers=headers, timeout=60.0, follow_redirects=True, verify=False) as client:
                    async with client.stream("GET", direct_url) as resp:
                        if resp.status_code in (503, 429, 502, 504):
                            last_error = RuntimeError(f"鏡像節點繁忙回應 HTTP {resp.status_code}")
                            continue

                        if resp.status_code == 416:
                            # 416 Range Not Satisfiable: 檔案已經下載完整
                            if part_file.exists() and part_file.stat().st_size > 0:
                                break
                            else:
                                start_byte = 0
                                continue

                        if resp.status_code not in (200, 206):
                            last_error = RuntimeError(f"鏡像伺服器回應異常 HTTP {resp.status_code}")
                            continue

                        # 若伺服器回傳 200 (不支援 Range 續傳)，重置檔案
                        mode = "wb"
                        if resp.status_code == 206 and start_byte > 0:
                            mode = "ab"
                            content_range = resp.headers.get("content-range", "")
                            range_match = re.search(r"/(\d+)", content_range)
                            if range_match:
                                job.total_bytes = int(range_match.group(1))
                        else:
                            start_byte = 0
                            c_len = resp.headers.get("content-length")
                            if c_len and c_len.isdigit():
                                job.total_bytes = int(c_len)

                        downloaded = start_byte
                        with open(part_file, mode) as f:
                            async for chunk in resp.aiter_bytes(chunk_size=65536):
                                if job.status == "paused" or job.job_id not in self.jobs:
                                    return
                                f.write(chunk)
                                downloaded += len(chunk)
                                job.downloaded_bytes = downloaded
                                if job.total_bytes > 0:
                                    job.progress_percent = int((downloaded / job.total_bytes) * 100)
                                job.updated_at = datetime.now(timezone.utc).isoformat()

                        # 下載完成
                        break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_error = e
                continue
        else:
            raise last_error or RuntimeError("下載超過重試上限")

        if not part_file.exists() or part_file.stat().st_size == 0:
            raise RuntimeError("下載檔案為空")

        # 3. 落地本地與觸發 IngestionPipeline
        filename = f"{job.title}.{job.extension}"
        final_dest = self.pipeline.storage.get_raw_path(job.md5, job.extension)

        # 移動暫存檔為正式原檔
        part_file.replace(final_dest)

        metadata_override = {
            "title": job.title,
            "authors_display": job.authors or "未知作者"
        }
        res = self.pipeline.process_file(final_dest, metadata_override)
        job.work_id = res["work_id"]
        job.status = "completed"
        job.progress_percent = 100
        job.error_message = None
        job.updated_at = datetime.now(timezone.utc).isoformat()
