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
import anyio
import anyio.to_thread
import httpx

from app.crawler.mirror_resolver import MirrorResolver
from app.pipeline.ingest import IngestionPipeline

log = logging.getLogger(__name__)


# worker 側檔案 I/O 專用的執行緒頃額（BR-20260820_210000 E 節 + BR-20260821_040000）。
#
# **為何不用 `run_in_threadpool` / 預設執行緒池**：預設池只有 40 個 token
# （實測 `current_default_thread_limiter().total_tokens == 40`），而那 40 個同時是
# **每一個 HTTP 請求的 sync 相依項與 sync 路由**在用的（FastAPI 將 sync 依賴一律
# 丟進預設池，`fastapi/dependencies/utils.py:676`）。下載寫入落在
# `hard,timeo=600` 的 NFS 上，一次 stall 就是 60 秒級的持有；若共用預設池，
# 幾個同時進行的下載就能把整個站的請求路徑餓死——**把 event loop 阻塞換成
# threadpool 排隊，使用者看到的症狀完全相同**，而那正是本族上一包已記下的風險。
#
# 專用 limiter 與預設池**完全隔離**（實測：3 個工作佔住專用 limiter 時，
# `default.borrowed_tokens == 0`）。上限 4 是刻意的：下載是順序大檔寫入，
# 再多並行只會讓 NFS 隨機化、並不提升吐吐量。
#
# module-scope 建立（無 running loop 時）已實測可行，且同一個物件可跨多個
# 獨立 event loop 重用（測試每條各自 `asyncio.run` 仍正常）。
_FILE_IO_LIMITER = anyio.CapacityLimiter(4)


async def _run_file_io(func, *args):
    """將一個同步檔案 I/O 呼叫移出 event loop 執行緒，走專用頃額。

    `abandon_on_cancel` 保持預設（False）：取消時等執行緒把手邊這一步做完。
    這與改造前的行為一致（同步呼叫本來就不可中斷），且避免「取消後執行緒仍在
    寫一個已被刪除的檔案」這種難以重現的競合。
    """
    return await anyio.to_thread.run_sync(func, *args, limiter=_FILE_IO_LIMITER)


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
        publication_year: Optional[int] = None,
        collection_ids: Optional[List[str]] = None
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
        # 使用者在佇列當下表達的「這本書要進哪些書單」意圖（FR-20260820_234500 R1/R2）。
        # 指定發生時 `work_id` 通常還是 None（下載完成才產生），所以這裡存的是
        # **意圖**，不是既成事實；真正寫進 DB 由 `_apply_collections()` 負責。
        self.collection_ids: List[str] = list(collection_ids or [])
        # 歸戶結果的**失敗態**專用格。三態必須互斥可辨，不得共用同一個輸出：
        #   collection_ids == []   且 error is None → 使用者根本沒指定（缺席態）
        #   collection_ids != []   且 error is None → 已指定；未完成＝待歸戶，已完成＝歸戶成功
        #   error is not None                      → 已指定但寫入失敗，字串帶「是哪幾個 cid 失敗」
        # 少了這一格，「書單被刪導致寫不進去」會與「使用者沒指定」在 to_dict()、
        # 存檔與 UI 上完全一樣——那正是本 repo 已重複踩過三次的失效類別
        # （mirror-resolver 回 None / extension 三態收斂成一個 timeout /
        #  publication_year 缺值退化成 0）。
        self.collection_sync_error: Optional[str] = None
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
            # 這兩個 key 必須同時出現在 to_dict() 與 _load_jobs_from_disk()。
            # 只寫其一 = 「存了但重啟後消失」，而且不寫往返測試就看不出來
            # （BR-20260820_131500 的 publication_year 是同一條鏈上的前例）。
            "collection_ids": list(self.collection_ids),
            "collection_sync_error": self.collection_sync_error,
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

    @staticmethod
    def _part_size(part_file: Path) -> int:
        """回傳 `.part` 檔目前大小；不存在回 **-1**。

        兩態刻意用不同的值而非都回 0：「檔案不存在」與「檔案存在但是空的」
        在斷點續傳邏輯裡意義不同（前者要從頭下載，後者代表上一次建立了檔但一個
        byte 都沒拿到），共用同一個輸出會讓兩種情境不可區分。

        **一次 `stat(2)` 取代原本的 `exists()` + `stat()`**。原寫法在 NFS 上是
        兩次 RPC 往返，且兩次之間檔案可能被刪（TOCTOU）——直接 stat 並接
        `FileNotFoundError` 兩個問題一起消掉。
        """
        try:
            return part_file.stat().st_size
        except FileNotFoundError:
            return -1

    @staticmethod
    def _remove_part_file(part_file: Path) -> bool:
        """刪除 `.part` 檔，回傳是否真的刪到了一個檔。

        同樣用一次 `unlink(2)` 取代 `exists()` + `unlink()` 兩次 NFS RPC。
        回傳 bool 而非 None，是為了讓「檔本來就不在」與「真的刪了一個數百 MB 的檔」
        可被區分——原寫法的 `except Exception: pass` 把兩者與「權限不足刪不掉」
        三態全部壓成同一個輸出。
        """
        try:
            part_file.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as e:
            # 回退行為不變（不阻斷刪 job），但必須出聲：數百 MB 的殘留檔靜默堆在
            # NAS 上是真實成本，而原本的 `except Exception: pass` 永遠不會有人知道。
            log.warning(
                "下載斷點檔刪除失敗，檔案可能殘留於儲存區：%s: %s | file=%s",
                type(e).__name__, e, part_file,
            )
            return False

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
                    publication_year=item.get("publication_year"),
                    # 同上：舊格式沒有這個 key。`or []` 而非 `.get(k, [])` 是因為
                    # 舊檔也可能寫進 null（to_dict 時是空 list，但手改過的檔不保證）。
                    collection_ids=item.get("collection_ids") or []
                )
                job.status = item.get("status", "queued")
                job.progress_percent = item.get("progress_percent", 0)
                job.downloaded_bytes = item.get("downloaded_bytes", 0)
                job.total_bytes = item.get("total_bytes", 0)
                job.retry_count = item.get("retry_count", 0)
                job.error_message = item.get("error_message")
                # 歸戶失敗訊號也必須跟著往返，否則重啟後一個歸戶失敗的 job 會
                # 復原成「看起來完全正常」——失敗態静默退化成成功態。
                job.collection_sync_error = item.get("collection_sync_error")
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

    # 「這個 md5 已經有 job 佔位了」的唯一判準。單筆 `enqueue` 與批次 `enqueue_many`
    # **必須共用這一份定義**——兩條入口各自抄一份的話，去重規則會悄悄分岔，
    # 同一個輸入從不同入口得到不同結果，而且分岔本身不會有任何訊號。
    _ACTIVE_STATUSES = ("queued", "downloading", "paused", "completed")

    def enqueue_many(
        self,
        items: List[Dict[str, Any]],
        autostart: bool = True
    ) -> List[DownloadJob]:
        """批次入列：N 筆任務只寫一次 `download_jobs.json`。

        與逐筆呼叫 `enqueue()` 的差異**只在成本，不在語意**：去重規則、`autostart`
        行為、回傳的 job 物件都相同。

        **成本差異（本方法存在的唯一理由，BR-20260820_210000 F 節）**——
        `enqueue()` 每一筆都做兩件 O(N) 的事：
          (a) `src:247` 線性掃 `self.jobs` 找重複
          (b) `src:262` 整份重寫 JSON（**全部** job，不是新增那筆）
        逐筆呼叫 N 次 ⇒ 掃描 O(N²) + 寫入 O(N²)。**兩個都是**，修一個不夠。
        本方法把去重索引建一次、檔案寫一次，兩者同時降為 O(N)。

        **存檔次數是可解釋的確切數字，不是「變少了」**：
          - 有建立任何新 job → **恰好 1 次**
          - 一筆都沒建立（全部重複／空清單）→ **0 次**
        後者與 `enqueue()` 命中重複時提前返回、同樣不存檔的行為一致。
        「修好了」與「這條路徑根本沒被走到」都會讓次數變小，所以測試鎖的是
        確切數字**加上**「job 真的建出來了」的正面證據，不是只鎖次數。

        `items` 每筆是 dict，key 與 `enqueue()` 的參數同名。**缺 `md5` 一律拋
        `ValueError`**，不靜默略過——略過會讓「送 120 筆回 119 筆」沒有任何訊號。
        """
        # 去重索引建一次，而不是每筆掃一次 `self.jobs`——這是 O(N²) 的另一半，
        # 只修存檔次數的話它會留在原地。
        existing_by_md5: Dict[str, DownloadJob] = {}
        for j in self.jobs.values():
            if j.status in self._ACTIVE_STATUSES:
                existing_by_md5.setdefault(j.md5, j)

        results: List[DownloadJob] = []
        created: List[DownloadJob] = []

        for idx, item in enumerate(items):
            md5 = str(item.get("md5") or "").lower()
            if not md5:
                raise ValueError(
                    f"enqueue_many: items[{idx}] 缺少 md5，無法建立下載任務。"
                    "（靜默略過會讓「送 N 筆回 N-1 筆」沒有任何訊號）"
                )

            existing = existing_by_md5.get(md5)
            if existing is not None:
                results.append(existing)
                continue

            job = DownloadJob(
                job_id=f"job_{uuid.uuid4().hex[:12]}",
                md5=md5,
                title=item.get("title"),
                authors=item.get("authors"),
                extension=item.get("extension") or "pdf",
                mirror_links=item.get("mirror_links"),
                publication_year=item.get("publication_year"),
                collection_ids=item.get("collection_ids"),
            )
            self.jobs[job.job_id] = job
            # 同一批次內的重複也要擋：同一個 md5 在一個 request 裡出現兩次時，
            # 只查「進入本方法前的快照」會漏掉——逐筆 enqueue() 沒有這個破口，
            # 因為前一筆已經寫進 self.jobs 了。
            existing_by_md5[md5] = job
            created.append(job)
            results.append(job)

        if created:
            # 存檔一次。順序與 `enqueue()` 逐筆時相同：先落盤、再入列、最後啟動。
            self._save_jobs_to_disk()
            for job in created:
                self.queue.put_nowait(job)
            if autostart:
                self.start()

        return results

    def enqueue(
        self,
        md5: str,
        title: str,
        authors: Optional[str] = None,
        extension: str = "pdf",
        mirror_links: Optional[List[str]] = None,
        publication_year: Optional[int] = None,
        collection_ids: Optional[List[str]] = None,
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

        實作上這是 `enqueue_many([...一筆...])` 的單筆包裝——**刻意不另抄一份**
        去重與存檔邏輯：兩份會分岔，而分岔不會有訊號。
        """
        return self.enqueue_many(
            [{
                "md5": md5,
                "title": title,
                "authors": authors,
                "extension": extension,
                "mirror_links": mirror_links,
                "publication_year": publication_year,
                "collection_ids": collection_ids,
            }],
            autostart=autostart,
        )[0]

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
        """刪除任務（排隊中、下載中、已暫停或已完成均可刪除），並清除臨時斷點檔案。

        ⚠ **本方法必須在 event loop 執行緒上呼叫**（它會 `task.cancel()`，而
        asyncio Task 的方法不是 thread-safe）。需要在非 loop 執行緒上刪除時，
        呼叫 `adelete_job()`——它把數百 MB `.part` 檔的 NFS `unlink` 移出 loop，
        但把 `task.cancel()` 留在 loop 上。見 `adelete_job` 的說明。
        """
        job = self.jobs.get(job_id)
        if not job:
            return False

        # 若正在下載，先取消 task
        task = self._active_tasks.get(job_id)
        if task and not task.done():
            task.cancel()

        # 刪除臨時斷點檔案
        self._remove_part_file(self._get_part_path(job))

        del self.jobs[job_id]
        self._save_jobs_to_disk()
        return True

    async def adelete_job(self, job_id: str) -> bool:
        """`delete_job` 的 async 版：把 NFS `unlink` 移出 event loop 執行緒。

        **為何不是簡單地把整個 `delete_job` 丟進 threadpool**
        （這是本方法存在的全部理由，不是風格偏好）：
        `delete_job` 內含 `task.cancel()`，而 **asyncio Task/Future 的方法不是
        thread-safe**。實測（Python 3.12）：

            一般模式   跨執行緒 t.cancel()  -> 碰巧生效，cancelled=True
            debug 模式  跨執行緒 t.cancel()  -> RuntimeError: Non-thread-safe
                                                  operation invoked on an event loop
                                                  other than the current one
                                                  **cancelled=False（取消沒生效）**
            CONTROL   同 loop 上  t.cancel()  -> 兩種模式都乾淨成功

        一般模式下它「看起來會動」正是最危險的地方：測試會全綠，而實際上那是
        未定義行為——取消訊號在錯誤的執行緒上進入 loop 的內部狀態機。
        「下載真的停了」與「取消沒生效但沒人知道」會共用同一個輸出。

        所以這裡拆成兩段：取消與字典操作留在 loop（便宜、純記憶體），
        **只有真正會卡住的 `unlink` 進執行緒**（數百 MB 檔案在
        `hard,timeo=600` 的 NFS 上）。

        回傳值與 `delete_job` 完全一致：True = 真的刪了一個 job，
        False = 找不到該 job。**檔案刪不成功不會變成 False**（同原行為），
        但會由 `_remove_part_file` log.warning 出聲。
        """
        job = self.jobs.get(job_id)
        if not job:
            return False

        # 必須在 loop 執行緒上，見上方實測。
        task = self._active_tasks.get(job_id)
        if task and not task.done():
            task.cancel()

        # 唯一真正會阻塞的一格：數百 MB 的 .part 檔在 NFS 上 unlink。
        await _run_file_io(self._remove_part_file, self._get_part_path(job))

        # 上面 await 期間 job 可能已被別的路徑刪掉（例如使用者連點兩次）。
        # 用 pop 而非 `del`：重複刪除回 False，不拋 KeyError。
        if self.jobs.pop(job_id, None) is None:
            return False
        await _run_file_io(self._save_jobs_to_disk)
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

    def _known_collection_ids(self) -> set:
        """回傳目前存在的所有 collection_id 集合。

        刻意用 `dao.list_collections()`（**一次** DB 往返、不載 items）而不是對每個
        cid 呼叫 `dao.get_collection(cid)`（每個 cid **一次**往返，且會連同該書單的
        所有 items 一起載入）。驗證只需要 id 集合，載 items 是白工；`collection_ids`
        是複數，迴圈內查 DB 會讓成本隨選取數線性增長。
        """
        return {c.collection_id for c in self.pipeline.dao.list_collections()}

    def assign_collections(self, job_id: str, collection_ids: List[str]) -> Optional[DownloadJob]:
        """指定某個 job 完成後要歸入哪些書單（FR-20260820_234500 R1/R2/R4）。

        兩種時機、同一個入口，但**行為不同且必須可區分**：
          - job 尚未完成（`work_id is None`）→ 只記下意圖並落盤，等下載完成由
            `_apply_collections()` 真正寫入。
          - job 已完成（`work_id` 存在）→ 立刻寫入 DB，即時生效（R4）。
        呼叫端要靠 `job.work_id is not None` 判斷發生了哪一種；兩者共用同一個回應的話，
        前端無法分辨「已寫進 DB」與「只記下意圖」。

        不存在的 collection_id 一律 `ValueError`**不靜默略過**（AC5）——略過會讓
        「指定了三個書單」與「指定了三個但只有兩個是真的」共用同一個輸出。
        """
        job = self.jobs.get(job_id)
        if job is None:
            return None

        requested = [str(c) for c in (collection_ids or [])]
        known = self._known_collection_ids()
        missing = [cid for cid in requested if cid not in known]
        if missing:
            raise ValueError(
                f"指定的書單不存在：{', '.join(missing)}。"
                f"（目前可用書單共 {len(known)} 個；靜默略過會讓「指定成功」與"
                "「指定到一個已被刪除的書單」共用同一個輸出）"
            )

        job.collection_ids = requested
        # 重新指定＝重新嘗試，舊的失敗訊號必須清掉，否則一個已修好的 job 會永遠
        # 掛著上一次的錯誤字串。
        job.collection_sync_error = None
        job.updated_at = datetime.now(timezone.utc).isoformat()

        # R4：已完成的 job 立即生效，不必重跑下載。
        if job.status == "completed" and job.work_id:
            self._apply_collections(job)

        self._save_jobs_to_disk()
        return job

    def _apply_collections(self, job: DownloadJob) -> None:
        """把 job 上記錄的書單意圖真正寫進資料庫。

        缺席態與失敗態**不得共用輸出**（本 repo 已重複踩過三次的失效類別）：
          - 沒指定任何書單 → 直接返回，不 log、不寫 `collection_sync_error`。
            「什麼都沒發生」本來就該是無聲的。
          - 指定了但寫入失敗 → `collection_sync_error` 記下**是哪幾個 cid 失敗**
            並 `log.warning` 出聲。只寫「有錯」不夠：那會讓「三個全失敗」與
            「三個裡失敗一個」共用同一個輸出。
        """
        if not job.collection_ids:
            return

        if not job.work_id:
            # 這是呼叫端的錯誤（work_id 還沒產生就叫這個方法），不是使用者的指定失敗。
            # 必須出聲，否則歸戶會靜默不發生。
            log.warning(
                "job %s 尚無 work_id 卻被要求歸戶，本次未寫入任何書單（指定的書單：%s）",
                job.job_id, ", ".join(job.collection_ids),
            )
            job.collection_sync_error = "尚未產生 work_id，無法寫入書單"
            return

        failed: List[str] = []
        for cid in job.collection_ids:
            try:
                self.pipeline.dao.add_work_to_collection(cid, job.work_id)
            except Exception as e:
                failed.append(f"{cid}({type(e).__name__}: {e})")

        if failed:
            job.collection_sync_error = (
                f"{len(failed)}/{len(job.collection_ids)} 個書單寫入失敗：{'; '.join(failed)}"
            )
            log.warning(
                "job %s 歸戶部分失敗，work_id=%s：%s",
                job.job_id, job.work_id, job.collection_sync_error,
            )
        else:
            job.collection_sync_error = None

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
            # 一次 stat(2) 完成「在不在 + 多大」，並且移出 loop——原寫法是
            # `exists()` + `stat()` 兩次 NFS RPC 往返，都在 event loop 執行緒上。
            part_size = await _run_file_io(self._part_size, part_file)
            start_byte = part_size if part_size > 0 else 0
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
                            if await _run_file_io(self._part_size, part_file) > 0:
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
                        # 開檔、每一次寫入、關檔全部走專用執行緒頃額（E 節主體）。
                        #
                        # 刻意保持**逐 chunk** 進出執行緒，而不是把整段下載包進去：
                        # 後者會讓一個數分鐘的下載**長期佔住一個 token**，把「阻塞 loop」
                        # 換成「阻塞執行緒池」——那只是把問題搬家。逐 chunk 時每次只持有
                        # 一次 64KB 寫入的時間，多個下載可以在 4 個 token 上交錯進行。
                        # 代價是每 64KB 一次執行緒往返（~數十微秒），遠小於 NFS 寫入本身。
                        f = await _run_file_io(open, part_file, mode)
                        try:
                            async for chunk in resp.aiter_bytes(chunk_size=65536):
                                if job.status == "paused" or job.job_id not in self.jobs:
                                    return
                                await _run_file_io(f.write, chunk)
                                downloaded += len(chunk)
                                job.downloaded_bytes = downloaded
                                if job.total_bytes > 0:
                                    job.progress_percent = int((downloaded / job.total_bytes) * 100)
                                job.updated_at = datetime.now(timezone.utc).isoformat()
                        finally:
                            # 不論正常結束、`return`（暫停/刪除）還是例外，檔案都必須關。
                            # 這重現了原本 `with open(...)` 的保證；拿掉就會在暫停路徑上漏 fd。
                            await _run_file_io(f.close)

                        # 下載完成
                        break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_error = e
                continue
        else:
            raise last_error or RuntimeError("下載超過重試上限")

        # 同樣一次 stat(2)：-1（不存在）與 0（存在但空）兩態在這裡的處置相同，
        # 但 `_part_size` 仍把它們分開回傳，因為斷點續傳那邊需要區分。
        if await _run_file_io(self._part_size, part_file) <= 0:
            raise RuntimeError("下載檔案為空")

        # 3. 落地本地與觸發 IngestionPipeline
        filename = f"{job.title}.{job.extension}"
        final_dest = self.pipeline.storage.get_raw_path(job.md5, job.extension)

        # 移動暫存檔為正式原檔。同一個 NFS 挂載內這是一次 rename(2)（便宜），
        # 但若 raw_dir 日後被搬到別的檔案系統，`Path.replace` 會退化成整檔複製
        # （數百 MB）——兩種情境共用這一行，所以一律移出 loop。
        await _run_file_io(part_file.replace, final_dest)

        metadata_override = {
            "title": job.title,
            "authors_display": job.authors or "未知作者",
            "publication_year": job.publication_year
        }
        # 本檔單次阻塞最久的一格：`process_file` 同步做完 compute_file_hashes（全檔讀 NFS）
        # → PDFExtractor.extract / OCR（CPU-bound 數十秒）→ convert_to_pdf 寫 NFS
        # → save_parsed_markdown 寫 NFS → 9 次 SQLite 寫。全程在 event loop 執行緒上。
        res = await _run_file_io(self.pipeline.process_file, final_dest, metadata_override)
        job.work_id = res["work_id"]
        job.status = "completed"
        job.progress_percent = 100
        job.error_message = None
        job.updated_at = datetime.now(timezone.utc).isoformat()

        # 落地後自動歸戶（FR-20260820_234500 R3）。
        # AC6（下載失敗的 job 不得產生任何書單寫入）由**控制流**保證而非由 if 保證：
        # 上面任何一步失敗都會 raise，執行根本走不到這一行。用 `if job.status == "failed"`
        # 去擋是比較弱的寫法——那要求失敗路徑一定有人記得把狀態標對。
        self._apply_collections(job)
