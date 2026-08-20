import os
import re
import uuid
import hashlib
import asyncio
import tempfile
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
import httpx

from app.crawler.mirror_resolver import MirrorResolver
from app.pipeline.ingest import IngestionPipeline

log = logging.getLogger(__name__)


class DownloadJob:
    """下載任務實體物件。"""
    def __init__(
        self,
        job_id: str,
        md5: str,
        title: str,
        authors: Optional[str] = None,
        extension: str = "pdf",
        mirror_links: Optional[List[str]] = None,
        publication_year: Optional[int] = None
    ):
        self.job_id = job_id
        self.md5 = md5.lower()
        self.title = title
        self.authors = authors
        self.extension = extension.lower()
        self.mirror_links = mirror_links or []
        # None 代表「不知道」，與 0（已查證確無）不同。缺值一律保持 None，
        # 不得退化成 0 或空字串——那會讓「上游沒給」與「上游給了 0」共用同一個輸出。
        self.publication_year = publication_year
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
            "publication_year": self.publication_year,
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
        # 「關閉整個 worker」與「暫停單一 job」送出的訊號都是 CancelledError，
        # 語意卻相反（前者要結束迴圈、後者要繼續跑）。沒有這個旗標，兩種取消
        # 共用同一個輸入而處理端只實作得了其中一種——迴圈就永遠關不掉
        # （BR-20260820_230000）。
        self._stopping: bool = False
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
        except Exception as e:
            # 存檔失敗不阻斷呼叫端（回退行為不變），但必須出聲——
            # 否則「寫成功」與「寫失敗」共用同一個輸出，重啟後任務靜默消失。
            log.warning(
                "下載任務狀態存檔失敗，本次變更未落地磁碟（重啟後可能遺失）：%s: %s | file=%s",
                type(e).__name__, e, self._jobs_file,
            )

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
                    mirror_links=item.get("mirror_links", []),
                    # 舊格式 jobs.json 沒有這個 key，必須用 .get 而非 ["..."]，
                    # 否則整個 worker 啟動會被一份既有佇列檔炸掉。
                    publication_year=item.get("publication_year")
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
        except Exception as e:
            # 載入失敗不阻斷啟動（回退行為不變），但必須出聲——
            # 否則「檔案裡沒有任務」與「檔案壞了讀不出來」共用同一個輸出。
            log.warning(
                "下載任務佇列載入失敗，既有任務未被還原：%s: %s | file=%s",
                type(e).__name__, e, self._jobs_file,
            )

    def start(self):
        """啟動背景 Worker 監聽循環。

        兩態必須可區分（BR-20260820_143000）：
          - 有 running loop 且成功建立/已在跑 → 靜默（正常路徑）
          - 無 running loop → 背景迴圈**不會**啟動，佇列中的任務會永遠停在
            `queued`。這不是 no-op 的正常情況，必須 log.warning 出聲。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as e:
            log.warning(
                "無 running event loop，下載背景工作未啟動，佇列中的任務將停留在 "
                "queued 不會被執行（%s: %s）。請在 async context 下呼叫 start()。",
                type(e).__name__, e,
            )
            return
        if self._worker_task is None or self._worker_task.done():
            # 重新啟動時必須清掉關閉旗標，否則新迴圈會把第一個 pause 取消
            # 當成 shutdown 而立刻死掉。
            self._stopping = False
            self._worker_task = loop.create_task(self._process_queue())

    async def stop(self, timeout: float = 5.0) -> bool:
        """停止背景 Worker 監聽循環，並等它真的結束（BR-20260820_230000）。

        與 `pause_job` / `delete_job` 的取消**必須可區分**：兩者送出的訊號都是
        `CancelledError`，但語意相反——
          - pause / delete：取消單一 job 的傳輸，迴圈**繼續**跑
          - stop：整個 worker 收攤，迴圈**結束**
        區分靠的是 `self._stopping`：`_process_queue()` / `_run_single_job()` 的
        `except asyncio.CancelledError` 只在旗標為假時吞掉取消，為真則 re-raise。

        回傳值兩態必須可分：
          - True  → 所有 task 都真的結束了
          - False → 逾時仍有 task 存活（此時 log.warning 出聲）
        否則「關掉了」與「關不掉」會共用同一個輸出——那正是本 BR 的失效類別。
        """
        self._stopping = True

        worker_task = self._worker_task

        # 先收 start_job() 建立的單一 job task，再收背景迴圈本身。
        # 同一個 task 可能同時出現在兩邊——`_process_queue` 執行 job 時會把自己
        # 塞進 `_active_tasks`。必須去重，每個 task 只取消一次：
        # 對同一個 task 連續 cancel 兩次，會讓「取消真的穿透了」與「第一次被吞掉、
        # 第二次剛好打在 queue.get() 上才穿透」共用同一個輸出，把吞取消的缺陷蓋掉。
        pending: set = set()
        for task in list(self._active_tasks.values()) + [worker_task]:
            if task is None or task.done() or task in pending:
                continue
            task.cancel()
            pending.add(task)

        stopped = True
        if pending:
            _done, still_running = await asyncio.wait(pending, timeout=timeout)
            if still_running:
                stopped = False
                log.warning(
                    "下載背景工作在 %.1f 秒內未結束（仍有 %d 個 task 存活），"
                    "關閉流程未能乾淨收尾。",
                    timeout, len(still_running),
                )

        self._save_jobs_to_disk()
        return stopped

    def enqueue(
        self,
        md5: str,
        title: str,
        authors: Optional[str] = None,
        extension: str = "pdf",
        mirror_links: Optional[List[str]] = None,
        publication_year: Optional[int] = None,
        autostart: bool = True
    ) -> DownloadJob:
        """將書籍加入下載佇列。

        ⚠ 副作用（BR-20260820_143000 判準 4）：
        `autostart=True`（**預設**）時本方法尾端呼叫 `self.start()`。在有
        running event loop 的環境下（含 `TestClient`），這會立刻
        `create_task(_process_queue())` 並開始對公網鏡像發出真實 HTTP 請求；
        在無 loop 的同步環境下則不會啟動，任務停留在 `queued`
        （此時 `start()` 會 log.warning 出聲）。

        `autostart=False` 時只做「入列」——job 進 `self.jobs`、進 `self.queue`、
        狀態為 `queued`——但**完全不碰背景迴圈**：不呼叫 `start()`、不建立
        `_worker_task`、不發出任何對外請求，也不會因為無 loop 而 log.warning
        （沒有嘗試啟動，就沒有啟動失敗可報）。之後可由呼叫端在自己選定的時機
        呼叫 `start()` 或 `start_job()` 真正開跑。

        兩者的差異必須可觀察，否則「參數生效」與「參數被忽略」會共用同一個
        輸出；`tests/test_download_worker_enqueue_autostart.py` 鎖住這兩個方向。
        """
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
            mirror_links=mirror_links,
            publication_year=publication_year
        )
        self.jobs[job_id] = job
        self._save_jobs_to_disk()
        self.queue.put_nowait(job)
        if autostart:
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
            # 整個 worker 收攤時取消必須穿透（見 stop() 的說明）。
            if self._stopping:
                self._mark_interrupted_by_shutdown(job)
                raise
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

    def _mark_interrupted_by_shutdown(self, job: DownloadJob):
        """worker 收攤時，把進行中的 job 落盤成可辨識的中斷態。

        不用 `paused`：那是「使用者按了暫停」的語意，重啟後不會自動繼續。
        回 `queued` 才能讓 `_load_jobs_from_disk()` 在下次啟動時重新入列（:135）。
        兩者共用 `paused` 的話，「使用者暫停的」與「被關機掉的」會共用同一個輸出。
        """
        if job.job_id not in self.jobs:
            return
        if job.status in ("completed", "failed", "paused"):
            return
        job.status = "queued"
        job.error_message = "服務關閉中斷，將於下次啟動自動繼續"
        job.updated_at = datetime.now(timezone.utc).isoformat()

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
        while not self._stopping:
            job = await self.queue.get()
            if job.status == "paused" or job.job_id not in self.jobs:
                self.queue.task_done()
                continue

            current_task = asyncio.current_task()
            self._active_tasks[job.job_id] = current_task

            try:
                await self._execute_download_with_resume(job)
            except asyncio.CancelledError:
                # 兩種取消共用這個型別，必須靠 `_stopping` 區分（BR-20260820_230000）：
                #   - stop() 收攤整個 worker → re-raise，讓取消穿透、結束 while 迴圈
                #   - pause_job / delete_job 取消單一 job → 吞掉，迴圈繼續跑
                # 拿掉這個分支（一律吞）迴圈就關不掉；拿掉否定分支（一律穿透）pause 會
                # 讓整個背景迴圈陪葬。兩個方向都有測試鎖住。
                # 收尾（pop / 存檔 / task_done）一律留給下面的 finally，這裡只標狀態後
                # re-raise——在這裡重複做一次會讓 `queue.task_done()` 被呼叫兩次而
                # 拋 ValueError，把乾淨的關閉變成噪音。
                if self._stopping:
                    self._mark_interrupted_by_shutdown(job)
                    raise
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
                # TLS 驗證維持開啟：這是真正落檔到使用者硬碟的那條連線。
                # 關閉驗證等於讓中間人決定使用者實際收到的位元組。
                async with httpx.AsyncClient(headers=headers, timeout=60.0, follow_redirects=True) as client:
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
            "authors_display": job.authors or "未知作者",
            "publication_year": job.publication_year
        }
        res = self.pipeline.process_file(final_dest, metadata_override)
        job.work_id = res["work_id"]
        job.status = "completed"
        job.progress_percent = 100
        job.error_message = None
        job.updated_at = datetime.now(timezone.utc).isoformat()
