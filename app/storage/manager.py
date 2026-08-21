import fnmatch
import hashlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Set, Tuple, Union

log = logging.getLogger(__name__)

# 孤兒 `.tmp_*` 判定閾值：mtime 早於 `now - 這個秒數` 才刪。
#
# **推導（不是「感覺 24 小時差不多」）**：
#
# 先確定「合法寫入最久能多久」的上界。實測容器內對 `/data/parsed`（nfs4）
# 每 4 MiB + fsync 耗 0.442s ⇒ ~9 MiB/s。`save_raw_bytes` 不做 fsync，
# 實際更快，所以 9 MiB/s 是保守下界。以單一電子書 2 GiB 這個誇張上界估，
# 也只要 ~4 分鐘。
#
# **但寫入耗時其實不是這裡的約束**——實測 mtime 在寫入過程中**持續前進**
# （5 次取樣：+0.000 / +0.442 / +0.884 / +1.326 / +1.767s，
# `MTIME_ADVANCES_DURING_WRITE=True`）。也就是說 mtime 對「正在寫的檔」
# 是一個**心跳**而不是 `open()` 當下的固定戳記：只要寫入還在推進，
# 該檔的 mtime 就不可能落到閾值以外。所以合法寫入不論多久都不會被誤判。
#
# 真正的約束是另一個：**活著但 mtime 凍結**能多久。那只發生在 NFS RPC 卡住時
# （hard mount 會無限重試）。這個時間沒有理論上界，只能靠margin 涵蓋。
#
# 取 6 小時：對 4 分鐘的寫入上界有 ~90x 餘裕，也涵蓋數小時級的 NFS 斷線。
#
# **兩個方向的代價不對稱，所以偏長**：
#   閾值太長 → 孤兒多留幾小時。它本來就已經永久留著了，無害。
#   閾值太短 → 刪掉還活著的寫入端的檔案。**資料遺失**。
# 不對稱 ⇒ 往長的一邊偏。
ORPHAN_TMP_THRESHOLD_SECONDS = 6 * 60 * 60

# 節流間隔：距上次掃描未滿這個秒數就整個跳過（連列舉都不做）。
#
# **推導**：孤兒只由異常終止產生（SIGKILL / OOM / 容器被砍 / NFS 中斷），
# 是稀有事件，且回收沒有任何延遲需求——晚 15 分鐘回收與立刻回收沒有差別。
# 另一邊，掃描成本隨目錄線性成長（實測 42/47 檔 0.5ms；線性外推 10k 檔
# ~120ms、100k 檔 ~1.2s）。既然沒有延遲需求，就把頻率壓到成本可忽略為止。
# 15 分鐘 ⇒ 即使目錄長到 100k 檔，每小時也只付 4 × 1.2s。
SWEEP_INTERVAL_SECONDS = 15 * 60

# 掃描時符合此 glob 的檔案才是候選。與 `save_*` 寫出的 `.tmp_{pid}` 對應。
ORPHAN_TMP_GLOB = "*.tmp_*"


class StorageManager:
    """管理 NAS / 容器本地持久儲存區路徑、雜湊指紋計算與原子檔案寫入。"""

    # 已完成目錄引導的 base_dir（process 層級）。
    #
    # 為何需要（BR-20260821_040000）：`StorageManager` **不是** singleton——
    # `routes.py:18 get_storage()` 每個 `Depends` 新建一個，而
    # `engine.py:24 DatabaseEngine.__init__` 在 `db_path is None` 時也新建一個，
    # 於是 `Depends(get_dao)` / `Depends(get_search)` 也各自建一個。
    # 建構子無條件呼叫 `ensure_directories()` ⇒ **每一個 API 請求都對
    # `/data/raw` 與 `/data/parsed` 各下一次 `os.mkdir(2)`**，而那兩個目錄掛在
    # `hard,timeo=600` 的 NFS 上（`docker inspect` 實測）。連 `q=zzzznomatch`
    # 那種完全不碰檔案的搜尋也中招。
    #
    # `exist_ok=True` **不會**省掉 syscall：`/usr/lib/python3.12/pathlib.py`
    # 是先無條件 `os.mkdir(2)` 再 `except OSError` 吞 `EEXIST`。
    #
    # 執行位置是 threadpool（sync 相依項由 `fastapi/dependencies/utils.py:676`
    # 一律 `run_in_threadpool`），所以風險形狀是 **anyio threadpool 飽和**
    # （實測 `total_tokens=40`）而非 event loop 卡死——NFS 一次 stall，40 個
    # 並發請求的相依項解析全部堵在 `os.mkdir` 上，之後所有 sync 路由一起排隊。
    # 表徵與 event-loop 卡死幾乎不可區分，但根因與修法都不同。
    #
    # 與 `DatabaseEngine._bootstrapped_paths`（`engine.py:19`）刻意同形：那個守衛
    # 擋的是 `init_database`，**擋不到這裡**——兩個守衛管的是不同的 syscall。
    _ensured_dirs: Set[str] = set()
    _ensure_lock = threading.Lock()

    # 節流狀態：{目錄絕對路徑: 上次掃描的 monotonic 秒數}。
    #
    # **為何是 class 層級而不是 instance 層級**：`StorageManager` 不是 singleton
    # （見上方 `_ensured_dirs` 的註解——每個 `Depends` 都新建一個）。掛在 instance
    # 上等於每次寫入都拿到一個全新的空 dict ⇒ 節流永遠不生效，退化成「每次寫入都
    # 掃」。這正是 `_ensured_dirs` 必須是 class 層級的同一個理由。
    #
    # **為何用 `time.monotonic()` 而不是 `time.time()`**：節流量的是「距上次掃描
    # 過了多久」，是時間間隔。`time.time()` 會被 NTP 校正與手動改時鐘往回撥，
    # 一次回撥就可能讓節流卡死數小時（或反之整個失效）。`monotonic` 不受影響。
    # 注意這與孤兒判準用的時鐘**刻意不同**：那裡比對的是檔案 mtime，屬於掛鐘
    # 時間，只能用 `time.time()`。兩者量的是不同的東西，用同一個時鐘才是錯的。
    _last_sweep: Dict[str, float] = {}
    _sweep_lock = threading.Lock()

    def __init__(self, base_dir: Union[str, Path] = None):
        if base_dir is None:
            base_dir = os.getenv("DATA_DIR", "./data")
        self.base_dir = Path(base_dir).resolve()
        self.raw_dir = self.base_dir / "raw"
        self.parsed_dir = self.base_dir / "parsed"
        self.db_dir = self.base_dir / "db"
        # 下載暫存區（BR-20260821_040000 機制②，選項 C）。
        #
        # **為何掛在 `db_dir` 底下而不是 `base_dir / "staging"`**：實測
        # （`docker inspect` + 容器內 `df -T`）只有三個路徑被 bind-mount，
        # `base_dir`（`/data`）本身**沒有**掛載——它是容器的 overlay 層：
        #
        #     /data/raw     nfs4     ← NAS
        #     /data/parsed  nfs4     ← NAS
        #     /data/db      ext4     ← host ./data/db，唯一的本地持久掛載
        #     /data（本身）  overlay  ← 容器層，rebuild 即消失
        #
        # 所以 `/data/staging` 會落在 **overlay**，容器一 rebuild 就整個消失，
        # 而 `.part` 檔正是斷點續傳（HTTP Range）跨重啟續傳的依據
        # （`_load_jobs_from_disk` 把 `downloading` 復原成 `queued` 重新入列，
        # 再由 `_part_size` 讀既有 `.part` 決定 Range 起點）。放 overlay 會讓
        # 「跨重啟續傳」靜默退化成「每次重啟整檔重下」——功能還在、行為變了、
        # 沒有任何錯誤訊號。
        #
        # `db_dir` 底下是**在不改 `docker-compose.yml` 的前提下**唯一同時滿足
        # 「本地非 NFS」＋「跨容器重建持久」的位置。語意上把數百 MB 的下載暫存
        # 放進 `db/` 並不漂亮；正解是新增一條 `./data/staging:/data/staging`
        # 掛載，但那要改 compose。`OPENSHELF_STAGING_DIR` 就是那個逃生口：
        # 掛載一旦補上，設這個 env 即可搬離，不需要改這裡的程式碼。
        staging_override = os.getenv("OPENSHELF_STAGING_DIR")
        self.staging_dir = (
            Path(staging_override).resolve() if staging_override else self.db_dir / "staging"
        )
        self._ensure_directories_once()

    def _ensure_directories_once(self) -> bool:
        """每個 base_dir 只在本 process 建立一次目錄，回傳是否實際執行。

        回傳 bool 而非 None，是為了讓「跳過」與「執行」可被測試區分——
        兩者若共用同一個輸出，就無法證明這個守衛真的生效
        （同 `DatabaseEngine._ensure_initialized` 的理由）。

        **刻意不做 `exists()` 複查**：那會用 3 次 `stat(2)` 換掉 3 次 `mkdir(2)`，
        在 NFS 上仍是每請求的 RPC 往返，等於沒修。目錄若被外部刪除，寫入端
        （`save_raw_bytes` / `save_parsed_markdown`）會拋 `FileNotFoundError`
        **大聲失敗**——那比 `mkdir(exist_ok=True)` 靜默把「掛載掉了」重建成
        一個容器內的空目錄要好，後者正是 BR-20260820_223000
        （`dispatch_br` 寫進 ephemeral 目錄）踩過的坑。
        需要強制重建時呼叫公開的 `ensure_directories()`。
        """
        key = str(self.base_dir)
        with StorageManager._ensure_lock:
            if key in StorageManager._ensured_dirs:
                return False
            self.ensure_directories()
            StorageManager._ensured_dirs.add(key)
            return True

    def ensure_directories(self) -> None:
        """建立必要的目錄結構。

        公開語義刻意保持不變：**顯式呼叫一律真的執行**（`main.py:23` 的
        lifespan 啟動引導依賴這一點）。被跳過的只有**建構子**那條每請求路徑
        （見 `_ensure_directories_once`）。
        """
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.parsed_dir.mkdir(parents=True, exist_ok=True)
        self.db_dir.mkdir(parents=True, exist_ok=True)
        # 暫存區在本地 ext4 上，mkdir 是一次本地 syscall（不是 NFS RPC），
        # 所以放在這條既有的引導路徑上不會重新引入 BR-20260821_040000 機制①
        # 的每請求 NFS mkdir 成本。
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        StorageManager._ensured_dirs.add(str(self.base_dir))

    @staticmethod
    def compute_file_hashes(file_path: Path) -> Tuple[str, str, int]:
        """計算檔案的 SHA-256、MD5 雜湊與位元組大小。"""
        sha256_hash = hashlib.sha256()
        md5_hash = hashlib.md5()
        size_bytes = 0

        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                sha256_hash.update(chunk)
                md5_hash.update(chunk)
                size_bytes += len(chunk)

        return sha256_hash.hexdigest(), md5_hash.hexdigest(), size_bytes

    @staticmethod
    def compute_bytes_hashes(data: bytes) -> Tuple[str, str, int]:
        """計算位元組資料的 SHA-256、MD5 雜湊與長度。"""
        sha256 = hashlib.sha256(data).hexdigest()
        md5 = hashlib.md5(data).hexdigest()
        return sha256, md5, len(data)

    def sweep_orphan_tmp(
        self,
        directory: Path,
        threshold_seconds: float = ORPHAN_TMP_THRESHOLD_SECONDS,
        now: Optional[float] = None,
    ) -> Dict[str, int]:
        """回收 `directory` 下超過閾值的 `.tmp_*` 孤兒檔，回傳掃除統計。

        **回傳 dict 而不是單一刪除數**：`removed=0` 有兩種意思——「沒有孤兒」與
        「掃不到任何檔（目錄不存在 / 權限錯 / glob 寫錯）」。共用同一個輸出就無法
        判斷這支掃除到底有沒有在工作。`scanned` 就是那個控制組：
        `scanned>0, removed=0` 是「有看到檔但都還新」，`scanned=0` 是「根本沒看到候選」。

        當下線上孤兒數為 0，所以這支掃除上線後很可能**永遠** `removed=0`。
        一支永遠不工作的掃除可以永遠通過測試——這正是 `scanned` 是必要而非裝飾的原因。

        **判準是 mtime，刻意不是 pid（請勿「優化」成讀 pid）**：

        1. **pid 會重用**。容器重啟後 pid 從 1 開始，很容易撞到同一個號碼
           ⇒ 死掉的孤兒被誤判成「還活著」而永不刪除。
        2. **更嚴重：那是容器內的 pid，卻寫在 NAS 的檔名上**。NAS 可被多台主機、
           多個容器掛載，pid 命名空間**不是全域唯一的** ⇒ 可能把別台主機正在寫的檔
           當孤兒刪掉。

        **為何 mtime 在這裡安全（實測，非推論）**：mtime 在寫入過程中**持續前進**
        （容器內對 nfs4 的 `/data/parsed` 取樣 5 次：+0.000 / +0.442 / +0.884 /
        +1.326 / +1.767s）。所以 mtime 對「正在寫的檔」是一個**心跳**，而不是
        `open()` 當下的固定戳記：只要寫入還在推進，該檔就不可能落到閾值外。
        這比「一次性寫入所以很快」更強：即使未來有人把 `data` 改成串流餵入、
        寫入耗時從秒級變成小時級，mtime 判準**仍然正確**。

        **為何不能與 `download_worker.sweep_orphan_parts` 合併**：那支掃的是
        `/data/db/staging` 的 `*.part`，判準是「有沒有 job 引用」——`.part` 有真值
        來源（job 清單），且是串流下載的續傳檔，可以合法地存在很久 ⇒ 對它用 mtime
        會誤刪活著的續傳。`.tmp_*` 反過來：沒有任何真值來源，但寫入有 mtime 心跳
        ⇒ 只有 mtime 可用。**兩者判準恰好互斥，這是它們不能合併的根本原因。**

        失敗一律吞下並記 log：清理是盡力而為，絕不能讓一次清理失敗導致使用者的入庫失敗。
        """
        # 掛鐘時間（非 monotonic）：要跟檔案 mtime 同一個基準才能相減。
        now_ts = time.time() if now is None else now
        cutoff = now_ts - threshold_seconds
        scanned = 0
        removed = 0
        kept = 0

        # **為何是 `os.scandir` 而不是 `directory.glob()`**（實測，寫本檔時被測試抓到）：
        # `Path('/nonexistent').glob('*.tmp_*')` 回 `[]` 且**不拋任何例外**。
        # 於是「NFS 掛載掉了 / 目錄被刪 / 權限不足」會與「目錄好好的，只是沒有孤兒」
        # 產生**一模一樣的 `scanned=0`**——這正是判準①禁止的「缺席態與失敗態共用同一個
        # 輸出」。`os.scandir` 對同一個輸入拋 `FileNotFoundError(errno=2)`，是大聲的。
        # 設計要求④要求 `scanned` 能當控制組，而它只有在列舉失敗真的發得出聲時才成立。
        try:
            with os.scandir(directory) as entries:
                candidates = [
                    Path(e.path) for e in entries
                    if e.is_file() and fnmatch.fnmatch(e.name, ORPHAN_TMP_GLOB)
                ]
        except OSError as exc:
            # 目錄不存在 / NFS 不可達 / 權限不足。不能阻斷主流程，但必須出聲。
            log.warning("孤兒暫存檔掃除無法列舉目錄 %s: %s", directory, exc)
            return {"scanned": 0, "removed": 0, "kept": 0}

        for candidate in candidates:
            scanned += 1
            try:
                if candidate.stat().st_mtime >= cutoff:
                    kept += 1
                    continue
                candidate.unlink()
                removed += 1
                log.info("已回收孤兒暫存檔 %s", candidate)
            except OSError as exc:
                # 檔案在 glob 與 unlink 之間消失（另一個執行緒剛 rename 或剛掃掉）、
                # NFS 抖動、權限不足——全部當成 kept，下一輪再試。
                kept += 1
                log.warning("孤兒暫存檔回收失敗 %s: %s", candidate, exc)

        return {"scanned": scanned, "removed": removed, "kept": kept}

    def _maybe_sweep(self, directory: Path) -> Optional[Dict[str, int]]:
        """節流閘：距上次掃描未滿間隔就回 `None`（連 glob 都不做）。

        回傳 `Optional[Dict]` 而非 `Dict`：「被節流跳過」與「真的掃了但一個檔也沒看到」
        必須可區分——兩者若共用 `{"scanned": 0, ...}`，就無法證明節流真的生效
        （同 `_ensure_directories_once` 回 bool 的理由）。

        **執行緒安全**：兩個入口都在執行緒上（`routes.py:168 run_in_threadpool`、
        `download_worker.py:1064 _run_file_io`，`_FILE_IO_LIMITER = CapacityLimiter(4)`）
        ⇒ 最多 4 個執行緒可能同時進來。時間戳的**讀取與寫入包在同一把鎖裡**，且在
        釋放鎖之前先佔位——否則四個執行緒會同時通過檢查、同時掃描。這不需要完美互斥
        （最壞情況多掃一次，成本 0.5ms），但鎖只護時間戳、**不包住掃描本體**：把數百
        毫秒的 NFS glob 拿在鎖裡會讓其他三個執行緒一起阻塞，那正是 BR-20260821_040000
        機制①的 threadpool 飽和形狀。
        """
        key = str(directory)
        # 節流量的是「距上次過了多久」，是時間間隔，故用 monotonic：`time.time()` 會被
        # NTP 校正與手動改時鐘往回撥，一次回撥就可能讓節流卡死數小時（或反之整個失效）。
        # 這與孤兒判準用的時鐘**刻意不同**：那裡比對的是檔案 mtime，屬於掛鐘時間，
        # 只能用 `time.time()`。兩者量的是不同的東西，用同一個時鐘才是錯的。
        now_mono = time.monotonic()
        with StorageManager._sweep_lock:
            last = StorageManager._last_sweep.get(key)
            if last is not None and (now_mono - last) < SWEEP_INTERVAL_SECONDS:
                return None
            # 先佔位再釋鎖：避免四個執行緒同時通過檢查。
            StorageManager._last_sweep[key] = now_mono
        return self.sweep_orphan_tmp(directory)

    def save_raw_bytes(self, data: bytes, extension: str) -> Tuple[str, str, str, int]:
        """將位元組資料原子儲存至 /data/raw/{sha256}.{ext}，回傳 (相對路徑, sha256, md5, size_bytes)。"""
        sha256, md5, size_bytes = self.compute_bytes_hashes(data)
        ext = extension.lstrip(".").lower()
        rel_path = f"raw/{sha256}.{ext}"
        target_path = self.base_dir / rel_path

        if not target_path.exists():
            tmp_path = target_path.with_suffix(f".tmp_{os.getpid()}")
            with open(tmp_path, "wb") as f:
                f.write(data)
            tmp_path.replace(target_path)

        # piggyback lazy sweeper（BR-20260821_060000）：掛在**寫入成功之後**，因為這時該目錄的
        # dentry / attribute cache 是熱的（實測 warm glob 0.48ms ≈ 冷 0.76ms）。
        # **刻意不掛在 `ensure_directories()` / `__init__`**：那條路徑每個 API 請求都走
        # （`Depends()` → `DatabaseEngine()` → `StorageManager()`），BR-20260821_040000
        # 機制①就是被那條路徑咬的，剛修好，不要再加東西上去。
        #
        # 放在 `return` 前而非 `if` 區塊內：去重命中（`target_path` 已存在）時不寫檔，
        # 但那也是一次「碰過這個目錄」的機會，cache 同樣是熱的，沒理由放掉。
        self._maybe_sweep(self.raw_dir)

        return rel_path, sha256, md5, size_bytes

    def save_parsed_markdown(self, work_id: str, markdown_content: str) -> str:
        """將抽取的 Markdown 純文字儲存至 /data/parsed/{work_id}.md，回傳相對路徑。"""
        rel_path = f"parsed/{work_id}.md"
        target_path = self.base_dir / rel_path
        tmp_path = target_path.with_suffix(f".tmp_{os.getpid()}")

        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(markdown_content)
        tmp_path.replace(target_path)

        # 同 `save_raw_bytes`：寫入成功之後、cache 熱的時候掃。
        self._maybe_sweep(self.parsed_dir)

        return rel_path

    def resolve_path(self, rel_path: str) -> Path:
        """將儲存庫相對路徑轉換為安全絕對路徑。"""
        full_path = (self.base_dir / rel_path).resolve()
        if not str(full_path).startswith(str(self.base_dir)):
            raise ValueError(f"不合法的路徑遍歷存取: {rel_path}")
        return full_path

    def get_parsed_content(self, work_id: str) -> str:
        """讀取已解析的純文字內容。"""
        path = self.resolve_path(f"parsed/{work_id}.md")
        if not path.exists():
            return ""
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def get_raw_path(self, hash_or_name: str, extension: str = "pdf") -> Path:
        """取得原始檔案在 raw 目錄下的目標路徑。"""
        ext = extension.lstrip(".").lower()
        return self.raw_dir / f"{hash_or_name}.{ext}"

    def get_db_path(self) -> Path:
        """取得 SQLite 資料庫檔案路徑。"""
        return self.db_dir / "openshelf.sqlite"
