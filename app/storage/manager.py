import hashlib
import os
from pathlib import Path
from typing import Tuple, Union


class StorageManager:
    """管理 NAS / 容器本地持久儲存區路徑、雜湊指紋計算與原子檔案寫入。"""

    def __init__(self, base_dir: Union[str, Path] = None):
        if base_dir is None:
            base_dir = os.getenv("DATA_DIR", "./data")
        self.base_dir = Path(base_dir).resolve()
        self.raw_dir = self.base_dir / "raw"
        self.parsed_dir = self.base_dir / "parsed"
        self.db_dir = self.base_dir / "db"
        self.ensure_directories()

    def ensure_directories(self) -> None:
        """建立必要的目錄結構。"""
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.parsed_dir.mkdir(parents=True, exist_ok=True)
        self.db_dir.mkdir(parents=True, exist_ok=True)

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
