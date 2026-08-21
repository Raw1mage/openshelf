import hashlib
import os
import threading
from pathlib import Path
from typing import Set, Tuple, Union


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

        return rel_path, sha256, md5, size_bytes

    def save_parsed_markdown(self, work_id: str, markdown_content: str) -> str:
        """將抽取的 Markdown 純文字儲存至 /data/parsed/{work_id}.md，回傳相對路徑。"""
        rel_path = f"parsed/{work_id}.md"
        target_path = self.base_dir / rel_path
        tmp_path = target_path.with_suffix(f".tmp_{os.getpid()}")

        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(markdown_content)
        tmp_path.replace(target_path)

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
