import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Set, Union
from app.storage.manager import StorageManager


class DatabaseEngine:
    """管理 SQLite 資料庫連線、WAL 模式與 Schema 遷移。"""

    # 已完成 schema 引導的 DB 路徑（process 層級）。
    #
    # 為何需要：每個 API 請求都會 `Depends(get_dao)` -> `CatalogDAO()` -> `DatabaseEngine()`，
    # 而 `init_database()` 的 `executescript(schema.sql)` 是一個**寫入交易**（實測 49ms）。
    # 寫入交易需要 SQLite writer 鎖；一旦有其他寫入者（下載入庫、分類寫入）持有鎖，
    # 這個純粹多餘的重複引導就會卡在 `timeout=30.0` 上，把一個唯讀 API 變成 20+ 秒。
    # 引導是 idempotent 的，重跑不產生任何新結果——只產生鎖爭用。
    _bootstrapped_paths: Set[str] = set()
    _bootstrap_lock = threading.Lock()

    def __init__(self, db_path: Union[str, Path] = None):
        if db_path is None:
            storage = StorageManager()
            self.db_path = storage.get_db_path()
        else:
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._ensure_initialized()

    def _ensure_initialized(self) -> bool:
        """每個 DB 路徑只在本 process 引導一次 schema，回傳是否實際執行。

        回傳 bool 而非 None，是為了讓「跳過」與「執行」可被測試區分——
        兩者若共用同一個輸出，就無法證明這個守衛真的生效。
        """
        key = str(self.db_path)
        with DatabaseEngine._bootstrap_lock:
            # 檔案不存在代表被外部刪除（測試常見），必須重新引導。
            if key in DatabaseEngine._bootstrapped_paths and self.db_path.exists():
                return False
            self.init_database()
            DatabaseEngine._bootstrapped_paths.add(key)
            return True

    def get_connection(self) -> sqlite3.Connection:
        """建立並配置 SQLite 連線。"""
        conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=30.0
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    @contextmanager
    def session(self) -> Generator[sqlite3.Connection, None, None]:
        """資料庫連線與事務 Context Manager。"""
        conn = self.get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_database(self) -> None:
        """載入並執行 DDL 腳本。

        公開語義刻意保持不變：顯式呼叫一律真的執行。
        被跳過的只有**建構子**那條每請求路徑（見 `_ensure_initialized`）。
        """
        schema_path = Path(__file__).parent / "schema.sql"
        if schema_path.exists():
            with open(schema_path, "r", encoding="utf-8") as f:
                ddl = f.read()
            with self.session() as conn:
                conn.executescript(ddl)
