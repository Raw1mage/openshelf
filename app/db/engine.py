import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Union
from app.storage.manager import StorageManager


class DatabaseEngine:
    """管理 SQLite 資料庫連線、WAL 模式與 Schema 遷移。"""

    def __init__(self, db_path: Union[str, Path] = None):
        if db_path is None:
            storage = StorageManager()
            self.db_path = storage.get_db_path()
        else:
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self.init_database()

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
        """載入並執行 DDL 腳本。"""
        schema_path = Path(__file__).parent / "schema.sql"
        if schema_path.exists():
            with open(schema_path, "r", encoding="utf-8") as f:
                ddl = f.read()
            with self.session() as conn:
                conn.executescript(ddl)
