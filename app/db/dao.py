import json
import logging
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Set, Tuple, Dict, Any
from app.db.engine import DatabaseEngine
from app.models.catalog import (
    WorkCreate, WorkDetailRead, WorkRead, IdentifierRead,
    ManifestationRead, FileObjectRead, ReadingStateRead, ReadingProgressUpdate,
    SearchResultItem, CollectionCreate, CollectionUpdate, CollectionRead,
    CollectionDetailRead, CollectionItemRead, CategoryRead, CategoryTreeNode
)
from app.db.categories import DEFAULT_CATEGORY_TREE
from app.classification.result import ClassificationOutcome, ClassificationSource, ClassificationState
from app.classification.rules import RuleClassifier
from app.classification.taxonomy import parent_of

log = logging.getLogger(__name__)


DEFAULT_LIBGEN_MIRRORS: List[Dict[str, Any]] = [
    {
        "url": "https://libgen.li",
        "enabled": True,
        "note": "Libgen.li 系列主要節點",
        "is_default": True,
        "priority": 1,
        "validation_status": "verified",
        "adapter_type": "libgen_li"
    },
    {
        "url": "https://libgen.la",
        "enabled": True,
        "note": "Libgen.li 系列分流節點",
        "is_default": True,
        "priority": 2,
        "validation_status": "verified",
        "adapter_type": "libgen_li"
    },
    # 以下四項實測已死（2026-08-20，見 BR-20260820_111523）。
    # 刻意保留條目而非刪除：使用者在設定頁仍看得到這些網域及其死因，
    # 而不是發現它們無聲無息消失；同時 enabled=False + validation_status="offline"
    # 確保它們不會進入 get_active_libgen_mirror_urls() 的輸出。
    {
        "url": "https://libgen.rocks",
        "enabled": False,
        "note": "已失效：自簽憑證 + Domain Seizure Notice（法院查封，2026-08-20 實測）",
        "is_default": True,
        "priority": 3,
        "validation_status": "offline",
        "adapter_type": "libgen_li"
    },
    {
        "url": "https://libgen.gs",
        "enabled": False,
        "note": "已失效：DNS NXDOMAIN（域名不存在，2026-08-20 實測）",
        "is_default": True,
        "priority": 4,
        "validation_status": "offline",
        "adapter_type": "libgen_li"
    },
    {
        "url": "https://libgen.pm",
        "enabled": False,
        "note": "已失效：DNS NXDOMAIN（域名不存在，2026-08-20 實測）",
        "is_default": True,
        "priority": 5,
        "validation_status": "offline",
        "adapter_type": "libgen_li"
    },
    {
        "url": "https://libgen.is",
        "enabled": True,
        "note": "Libgen.is 傳統經典鏡像（DNS 可解析但 TCP 逾時，存活狀態 UNDECIDABLE）",
        "is_default": True,
        "priority": 6,
        "validation_status": "unverified",
        "adapter_type": "libgen_is"
    },
    {
        "url": "https://libgen.rs",
        "enabled": True,
        "note": "Libgen.rs 官方鏡像（未實測）",
        "is_default": True,
        "priority": 7,
        "validation_status": "unverified",
        "adapter_type": "libgen_is"
    },
    {
        "url": "https://libgen.st",
        "enabled": True,
        "note": "Libgen.st 官方鏡像（未實測）",
        "is_default": True,
        "priority": 8,
        "validation_status": "unverified",
        "adapter_type": "libgen_is"
    },
    {
        "url": "http://library.lol",
        "enabled": False,
        "note": "已失效：HTTP 200 但內容為 Domain Seizure Notice（查封，2026-08-20 實測）",
        "is_default": True,
        "priority": 9,
        "validation_status": "offline",
        "adapter_type": "direct_gateway"
    }
]

# 實測已死的網域（2026-08-20，見 BR-20260820_111523）。
#
# 為何需要這張清單而不是只從 DEFAULT_LIBGEN_MIRRORS 刪掉：
# get_libgen_mirrors() 若讀到 DB 裡已寫入的 libgen_mirrors setting，會原樣回傳
# 那份資料，完全不經過 DEFAULT_LIBGEN_MIRRORS。改常數只能修到「尚未寫入設定」
# 的新安裝；**既存使用者的 DB 裡那四個死鏡像仍標著 verified、仍會被取用**。
# 故在讀取側以黑名單過濾，兩條路徑（常數 / DB）都生效。
#
# 刻意不在讀取時改寫 DB：默默重寫使用者資料是另一種靈異；過濾是可逆的，
# 使用者若確認某網域復活，從這張清單拿掉即可。
KNOWN_DEAD_MIRROR_HOSTS: List[str] = [
    "libgen.rocks",   # 自簽憑證 + <title>Domain Seizure Notice</title>（法院查封）
    "libgen.gs",      # DNS NXDOMAIN
    "libgen.pm",      # DNS NXDOMAIN
    "library.lol",    # HTTP 200 但 body 為 Domain Seizure Notice（查封）
]


def is_known_dead_mirror(url: str) -> bool:
    """判斷一個鏡像 URL 是否屬於實測已死的網域。"""
    low = (url or "").lower()
    return any(host in low for host in KNOWN_DEAD_MIRROR_HOSTS)


class CatalogDAO:
    """封裝 SQLite 資料存取邏輯。"""

    # 對既有資料庫補上的新增欄位（table -> [(column, DDL type/default)]）。
    # schema.sql 走 executescript 無法搭載 ALTER TABLE（第二次啟動會 duplicate column 中斷），
    # 故新增欄位對舊 DB 的補齊在此以 PRAGMA table_info 實測後条件式執行。
    _COLUMN_MIGRATIONS: Dict[str, List[Tuple[str, str]]] = {
        "manifestation": [
            ("torrent_url", "TEXT"),
            ("magnet_uri", "TEXT"),
            ("download_protocol", "TEXT NOT NULL DEFAULT 'http'"),
            ("peers_count", "INTEGER"),
        ],
        "download_job": [
            ("torrent_url", "TEXT"),
            ("magnet_uri", "TEXT"),
            ("download_protocol", "TEXT NOT NULL DEFAULT 'http'"),
            ("peers_count", "INTEGER"),
        ],
        # feature_smart-book-classification：分類 provenance 與可判定狀態。
        #
        # 對既有 DB，work 的預設值刻意是 'pending' 而不是 'classified'：
        # 既存的 42 筆全部由舊 infer_categories_for_work() 產生，其中包含已知
        # 錯誤的 cat_800+cat_850 fallback。把它們標成 classified 等於宣稱那些
        # 錯誤分類可信；標成 pending 才會被回填命令撿起來重判。
        "work": [
            ("classification_state", "TEXT NOT NULL DEFAULT 'pending'"),
            ("classified_at", "TEXT"),
            ("classification_error", "TEXT"),
        ],
        "work_category": [
            ("source", "TEXT NOT NULL DEFAULT 'legacy'"),
            ("model", "TEXT"),
            ("prompt_version", "TEXT"),
            ("assigned_at", "TEXT"),
        ],
    }

    # 已完成一次性引導的 DB 路徑（process 層級）。
    #
    # 為何需要：`Depends(get_dao)` 讓**每個 API 請求**都新建一次 CatalogDAO，
    # 於是 `apply_column_migrations()` 與 `seed_categories_if_needed()` 也每請求各跑一次。
    # 這兩者都是**寫入路徑**（ALTER / CREATE INDEX / INSERT OR IGNORE），需要 SQLite writer 鎖。
    # 實測：writer 鎖被下載入庫持有時，`seed_categories_if_needed()` 單次阻塞 18.2 秒
    # （同一時刻 `DatabaseEngine()` 只要 69ms），這就是 /api/collections 偶發 20-27s 的根因。
    #
    # 這兩個步驟本質是**啟動期引導**而非請求期工作：schema 遷移與種子資料在 process
    # 生命週期內只需成立一次。跳過重跑不改變任何可觀察結果，只消除鎖爭用。
    _bootstrapped_paths: Set[str] = set()
    _bootstrap_lock = threading.Lock()

    def __init__(self, engine: DatabaseEngine = None, bootstrap: bool = True):
        """`bootstrap=False` 供離線唯讀工具使用（dry-run 回填、稽核腳本）。

        引導本身是**寫入路徑**（ALTER / CREATE INDEX / INSERT）。一個宣稱唯讀的
        呼叫端若仍走引導，它對錯誤路徑或 0-byte 檔案的第一個動作就是把它變成
        一個看起來正常的空 DB——而那正是「指錯 DB」最該被聽見的時刻。
        """
        self.engine = engine or DatabaseEngine()
        if bootstrap:
            self._ensure_bootstrapped()

    def _ensure_bootstrapped(self) -> bool:
        """每個 DB 路徑只在本 process 執行一次遷移與種子，回傳是否實際執行。

        回傳 bool 而非 None，是為了讓「跳過」與「執行」可被測試區分——
        兩者若共用同一個輸出，就無法證明這個守衛真的生效。
        """
        db_path = Path(self.engine.db_path)
        key = str(db_path)
        with CatalogDAO._bootstrap_lock:
            # 檔案不存在代表被外部刪除（測試常見），必須重新引導。
            if key in CatalogDAO._bootstrapped_paths and db_path.exists():
                return False
            self.apply_column_migrations()
            self.seed_categories_if_needed()
            CatalogDAO._bootstrapped_paths.add(key)
            return True

    # 依賴新增欄位的索引：必須在 ALTER 補完欄位之後才能建立，
    # 否則對舊 DB 會以 "no such column" 中斷啟動。
    _POST_MIGRATION_INDEXES: List[str] = [
        "CREATE INDEX IF NOT EXISTS idx_manifestation_protocol ON manifestation(download_protocol)",
        "CREATE INDEX IF NOT EXISTS idx_download_job_protocol ON download_job(download_protocol)",
        "CREATE INDEX IF NOT EXISTS idx_work_classification_state ON work(classification_state)",
        "CREATE INDEX IF NOT EXISTS idx_work_category_source ON work_category(source)",
    ]

    def apply_column_migrations(self) -> List[str]:
        """對既有 DB 補上缺少的欄位，回傳實際執行的 ALTER 清單（已是最新則為空）。

        回傳值刻意不是 bool：「無需遷移」與「遷移失敗」不得共用同一個輸出——
        失敗會拋出例外，成功則回傳可檢驗的欄位名清單。
        """
        applied: List[str] = []
        with self.engine.session() as conn:
            for table, columns in self._COLUMN_MIGRATIONS.items():
                table_exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,)
                ).fetchone()
                if not table_exists:
                    continue
                existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                for col_name, col_ddl in columns:
                    if col_name in existing:
                        continue
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_ddl}")
                    applied.append(f"{table}.{col_name}")

            # 欄位補齊後才建立相依索引（IF NOT EXISTS，可重複執行）
            for index_ddl in self._POST_MIGRATION_INDEXES:
                conn.execute(index_ddl)
        return applied

    def seed_categories_if_needed(self) -> None:
        """初次啟動時自動寫入分類樹與預設書單種子資料。"""
        with self.engine.session() as conn:
            # 建立預設分類樹
            count = conn.execute("SELECT COUNT(*) AS c FROM category").fetchone()["c"]
            if count == 0:
                for parent in DEFAULT_CATEGORY_TREE:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO category (category_id, parent_id, name, slug, icon, level, sort_order)
                        VALUES (?, NULL, ?, ?, ?, ?, ?)
                        """,
                        (parent["id"], parent["name"], parent["slug"], parent["icon"], parent["level"], parent["sort_order"])
                    )
                    for child in parent.get("children", []):
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO category (category_id, parent_id, name, slug, icon, level, sort_order)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (child["id"], parent["id"], child["name"], child["slug"], child["icon"], child["level"], child["sort_order"])
                        )

            # 建立預設「⭐ 我的最愛」自訂書單
            col_count = conn.execute("SELECT COUNT(*) AS c FROM collection WHERE is_system = 1").fetchone()["c"]
            if col_count == 0:
                now = self.current_iso()
                conn.execute(
                    """
                    INSERT OR IGNORE INTO collection (collection_id, name, description, icon, is_system, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                    """,
                    ("col_favorites", "我的最愛", "預設精選書單", "⭐", now, now)
                )

            # 刻意不再於此處對既有 Work 做「自動分類補寫」。
            #
            # 這段原本每次 bootstrap 都跑一次 infer_categories_for_work()，而該函式
            # 的零命中 fallback 會把任何判不出的書塞進 cat_800+cat_850——線上
            # cat_850 的 13 筆全是作業系統與電腦架構書就是這條路徑反覆寫出來的。
            # 移除 fallback 後這段就只剩「用規則補寫」的功能，而那件事現在由
            # script/backfill_classification.py 負責（可 dry-run、可重跑、有 provenance）。
            # 保留一個沉默的自動寫入點只會讓回填結果被下次啟動悄悄覆蓋回去。

    @staticmethod
    def generate_id(prefix: str) -> str:
        """產生具前綴的唯一 ID。"""
        return f"{prefix}_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def current_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def create_work(self, work_data: WorkCreate, work_id: Optional[str] = None) -> str:
        """建立 Work 紀錄。"""
        wid = work_id or self.generate_id("wk")
        now = self.current_iso()

        with self.engine.session() as conn:
            conn.execute(
                """
                INSERT INTO work (
                    work_id, title, title_provenance, work_type, language,
                    publication_year, authors_display, availability_tier,
                    relevance_authority, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    wid, work_data.title, work_data.title_provenance, work_data.work_type,
                    work_data.language, work_data.publication_year, work_data.authors_display,
                    work_data.availability_tier, 1.0, now, now
                )
            )
            # 初始化 reading_state
            conn.execute(
                """
                INSERT OR IGNORE INTO reading_state (work_id, added_at)
                VALUES (?, ?)
                """,
                (wid, now)
            )
            # 入庫路徑只跑**零網路的規則層**。模型層不得在此呼叫：這條路徑
            # 兩個呼叫端（routes.py:169 的預設 threadpool、download_worker.py:1086 的
            # CapacityLimiter(4)）都是**共用池**，遠端 latency 會外溢到別人身上。
            # 規則零命中時寫 pending，由回填命令消化。詳見
            # app/classification/service.py 末尾的實測註解。
            outcome = RuleClassifier().classify(work_data.title, work_data.authors_display)
            self._write_classification(conn, wid, outcome, now)
        return wid

    def find_work_by_hash(self, hash_val: str) -> Optional[str]:
        """根據 SHA-256 或 MD5 查詢既有之 work_id。"""
        with self.engine.session() as conn:
            row = conn.execute(
                """
                SELECT m.work_id FROM file_object f
                JOIN manifestation m ON f.manifestation_id = m.manifestation_id
                WHERE f.sha256 = ? OR f.md5 = ?
                LIMIT 1
                """,
                (hash_val, hash_val)
            ).fetchone()
            if row:
                return row["work_id"]
            
            # 從 identifier 表查
            row_id = conn.execute(
                """
                SELECT work_id FROM identifier
                WHERE (scheme = 'sha256' OR scheme = 'md5') AND value = ?
                LIMIT 1
                """,
                (hash_val,)
            ).fetchone()
            if row_id:
                return row_id["work_id"]
        return None

    def find_works_by_hashes(self, hash_vals: List[str]) -> Dict[str, str]:
        """批次版 find_work_by_hash：一次查詢取代 N 次往返。

        回傳 {hash_val: work_id}，僅含命中者；未命中的 hash 不會出現在鍵中
        （呼叫端用 .get() 取得 None，語意與逐筆版的 Optional 回傳一致）。

        與逐筆版的等價性由 tests/test_crawler_batch_hash_lookup.py 保證，
        含空清單與全部未命中兩個邊界。
        """
        if not hash_vals:
            return {}

        # 去重但保留原始值（呼叫端傳進來的大小寫由呼叫端負責正規化，
        # 此處不再 lower()，以免與逐筆版產生行為差異）
        uniq = list(dict.fromkeys(hash_vals))
        found: Dict[str, str] = {}

        # SQLite 的變數上限預設 999（新版 32766）。分塊以免筆數成長後炸掉。
        CHUNK = 300
        with self.engine.session() as conn:
            for i in range(0, len(uniq), CHUNK):
                chunk = uniq[i:i + CHUNK]
                marks = ",".join("?" * len(chunk))

                # 第一段：file_object 的 sha256 / md5，對應逐筆版的第一個查詢
                rows = conn.execute(
                    f"""
                    SELECT f.sha256, f.md5, m.work_id FROM file_object f
                    JOIN manifestation m ON f.manifestation_id = m.manifestation_id
                    WHERE f.sha256 IN ({marks}) OR f.md5 IN ({marks})
                    """,
                    chunk + chunk
                ).fetchall()
                for r in rows:
                    for col in ("sha256", "md5"):
                        v = r[col]
                        if v and v in uniq and v not in found:
                            found[v] = r["work_id"]

                # 第二段：identifier 表，對應逐筆版的 fallback 查詢。
                # 逐筆版只在第一段未命中時才查，故此處僅補未命中者。
                missing = [h for h in chunk if h not in found]
                if missing:
                    marks2 = ",".join("?" * len(missing))
                    rows_id = conn.execute(
                        f"""
                        SELECT value, work_id FROM identifier
                        WHERE (scheme = 'sha256' OR scheme = 'md5')
                          AND value IN ({marks2})
                        """,
                        missing
                    ).fetchall()
                    for r in rows_id:
                        if r["value"] not in found:
                            found[r["value"]] = r["work_id"]

        return found

    def add_identifier(self, work_id: str, scheme: str, value: str, confidence: str = "asserted") -> None:
        """新增識別碼（如 MD5, ISBN, DOI）。"""
        with self.engine.session() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO identifier (work_id, scheme, value, confidence)
                VALUES (?, ?, ?, ?)
                """,
                (work_id, scheme.lower(), value.strip(), confidence)
            )

    def add_manifestation(
        self,
        work_id: str,
        format_type: str = "unknown",
        version: str = "unknown",
        origin: str = "local",
        external_url: Optional[str] = None,
        torrent_url: Optional[str] = None,
        magnet_uri: Optional[str] = None,
        download_protocol: str = "http",
        peers_count: Optional[int] = None
    ) -> str:
        """新增 Manifestation 實體（含 Torrent / Magnet 來源屬性）。"""
        mid = self.generate_id("mf")
        with self.engine.session() as conn:
            conn.execute(
                """
                INSERT INTO manifestation (
                    manifestation_id, work_id, version, format, origin, is_retrievable, external_url,
                    torrent_url, magnet_uri, download_protocol, peers_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mid, work_id, version, format_type, origin, 1, external_url,
                    torrent_url, magnet_uri, download_protocol or "http", peers_count
                )
            )
        return mid

    def update_manifestation_torrent_source(
        self,
        manifestation_id: str,
        torrent_url: Optional[str] = None,
        magnet_uri: Optional[str] = None,
        download_protocol: Optional[str] = None,
        peers_count: Optional[int] = None
    ) -> bool:
        """回寫已知 Manifestation 之 Torrent / Magnet 來源（僅更新非 None 欄位）。"""
        fields = {
            "torrent_url": torrent_url,
            "magnet_uri": magnet_uri,
            "download_protocol": download_protocol,
            "peers_count": peers_count,
        }
        updates = {k: v for k, v in fields.items() if v is not None}
        if not updates:
            return False
        set_sql = ", ".join(f"{k} = ?" for k in updates)
        with self.engine.session() as conn:
            cur = conn.execute(
                f"UPDATE manifestation SET {set_sql} WHERE manifestation_id = ?",
                list(updates.values()) + [manifestation_id]
            )
            return cur.rowcount > 0

    def get_torrent_sources_for_work(self, work_id: str) -> List[Dict[str, Any]]:
        """取出某 Work 底下具備 Torrent 或 Magnet 來源的 Manifestation。"""
        with self.engine.session() as conn:
            rows = conn.execute(
                """
                SELECT manifestation_id, work_id, format, torrent_url, magnet_uri,
                       download_protocol, peers_count
                FROM manifestation
                WHERE work_id = ?
                  AND (torrent_url IS NOT NULL OR magnet_uri IS NOT NULL)
                """,
                (work_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def add_file_object(
        self,
        manifestation_id: str,
        role: str,
        local_path: str,
        sha256: str,
        size_bytes: int,
        md5: Optional[str] = None,
        produced_by: Optional[str] = None
    ) -> str:
        """新增實體檔案紀錄。"""
        fid = self.generate_id("fo")
        now = self.current_iso()
        with self.engine.session() as conn:
            conn.execute(
                """
                INSERT INTO file_object (
                    file_id, manifestation_id, role, local_path, sha256, md5,
                    size_bytes, produced_by, produced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (fid, manifestation_id, role, local_path, sha256, md5, size_bytes, produced_by, now)
            )
        return fid

    def update_work_tier(self, work_id: str, tier: int) -> None:
        """更新可用等級 (0=本地已解析, 1=本地未解析, etc.)。"""
        with self.engine.session() as conn:
            conn.execute(
                "UPDATE work SET availability_tier = ?, updated_at = ? WHERE work_id = ?",
                (tier, self.current_iso(), work_id)
            )

    def update_fts_index(self, work_id: str, title: str, authors: Optional[str], content: str) -> None:
        """更新或新增 FTS5 全文索引。"""
        with self.engine.session() as conn:
            # 刪除舊索引
            conn.execute("DELETE FROM work_fts WHERE work_id = ?", (work_id,))
            # 插入新索引
            conn.execute(
                "INSERT INTO work_fts (work_id, title, authors_display, content) VALUES (?, ?, ?, ?)",
                (work_id, title or "", authors or "", content or "")
            )

    def get_work_detail(self, work_id: str) -> Optional[WorkDetailRead]:
        """取得單一 Work 之完整巢狀元資料。"""
        with self.engine.session() as conn:
            w_row = conn.execute("SELECT * FROM work WHERE work_id = ?", (work_id,)).fetchone()
            if not w_row:
                return None

            # 取得 identifiers
            id_rows = conn.execute("SELECT * FROM identifier WHERE work_id = ?", (work_id,)).fetchall()
            identifiers = [IdentifierRead(**dict(r)) for r in id_rows]

            # 取得 manifestations 及 files
            mf_rows = conn.execute("SELECT * FROM manifestation WHERE work_id = ?", (work_id,)).fetchall()
            manifestations = []
            for mf_row in mf_rows:
                mf_id = mf_row["manifestation_id"]
                file_rows = conn.execute("SELECT * FROM file_object WHERE manifestation_id = ?", (mf_id,)).fetchall()
                files = [FileObjectRead(**dict(fr)) for fr in file_rows]
                mf_dict = dict(mf_row)
                mf_dict["files"] = files
                manifestations.append(ManifestationRead(**mf_dict))

            # 取得 reading_state
            rs_row = conn.execute("SELECT * FROM reading_state WHERE work_id = ?", (work_id,)).fetchone()
            reading_state = ReadingStateRead(**dict(rs_row)) if rs_row else None

            w_dict = dict(w_row)
            w_dict["identifiers"] = identifiers
            w_dict["manifestations"] = manifestations
            w_dict["reading_state"] = reading_state

            return WorkDetailRead(**w_dict)

    def update_reading_progress(self, work_id: str, progress: ReadingProgressUpdate) -> None:
        """更新閱讀進度。"""
        now = self.current_iso()
        with self.engine.session() as conn:
            conn.execute(
                """
                INSERT INTO reading_state (
                    work_id, progress_ratio, last_page, total_pages, last_opened_at, added_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(work_id) DO UPDATE SET
                    progress_ratio = excluded.progress_ratio,
                    last_page = excluded.last_page,
                    total_pages = excluded.total_pages,
                    last_opened_at = excluded.last_opened_at
                """,
                (work_id, progress.progress_ratio, progress.last_page, progress.total_pages, now, now)
            )

    # === 個人化書單 (Collection) DAO 實作 ===
    def create_collection(self, col: CollectionCreate, is_system: int = 0) -> str:
        """建立新書單。"""
        cid = self.generate_id("col")
        now = self.current_iso()
        with self.engine.session() as conn:
            conn.execute(
                """
                INSERT INTO collection (collection_id, name, description, icon, is_system, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (cid, col.name, col.description, col.icon or "📚", is_system, now, now)
            )
        return cid

    def list_collections(self) -> List[CollectionRead]:
        """列出所有自訂書單及內含書籍數量。"""
        with self.engine.session() as conn:
            rows = conn.execute(
                """
                SELECT c.*, COUNT(ci.work_id) AS items_count
                FROM collection c
                LEFT JOIN collection_item ci ON c.collection_id = ci.collection_id
                GROUP BY c.collection_id
                ORDER BY c.is_system DESC, c.created_at ASC
                """
            ).fetchall()
            return [CollectionRead(**dict(r)) for r in rows]

    def get_collection(self, collection_id: str) -> Optional[CollectionDetailRead]:
        """取得單一書單詳情及內含的所有書籍。"""
        with self.engine.session() as conn:
            col_row = conn.execute(
                """
                SELECT c.*, COUNT(ci.work_id) AS items_count
                FROM collection c
                LEFT JOIN collection_item ci ON c.collection_id = ci.collection_id
                WHERE c.collection_id = ?
                GROUP BY c.collection_id
                """,
                (collection_id,)
            ).fetchone()
            if not col_row:
                return None

            item_rows = conn.execute(
                """
                SELECT 
                    ci.collection_id, ci.work_id, ci.added_at, ci.notes, ci.sort_order,
                    w.title, w.authors_display, w.publication_year, w.language, w.availability_tier,
                    rs.progress_ratio,
                    (SELECT m.format FROM manifestation m WHERE m.work_id = w.work_id LIMIT 1) AS format,
                    (SELECT f.size_bytes FROM file_object f 
                     JOIN manifestation m ON f.manifestation_id = m.manifestation_id 
                     WHERE m.work_id = w.work_id LIMIT 1) AS size_bytes,
                    (SELECT id.value FROM identifier id WHERE id.work_id = w.work_id AND id.scheme = 'md5' LIMIT 1) AS md5
                FROM collection_item ci
                JOIN work w ON ci.work_id = w.work_id
                LEFT JOIN reading_state rs ON w.work_id = rs.work_id
                WHERE ci.collection_id = ?
                ORDER BY ci.sort_order ASC, ci.added_at DESC
                """,
                (collection_id,)
            ).fetchall()

            items = []
            for ir in item_rows:
                ir_dict = dict(ir)
                work_item = SearchResultItem(
                    work_id=ir_dict["work_id"],
                    title=ir_dict["title"],
                    authors_display=ir_dict["authors_display"],
                    publication_year=ir_dict["publication_year"],
                    language=ir_dict["language"],
                    format=ir_dict["format"] or "pdf",
                    size_bytes=ir_dict["size_bytes"],
                    md5=ir_dict["md5"],
                    availability_tier=ir_dict["availability_tier"],
                    progress_ratio=ir_dict["progress_ratio"]
                )
                items.append(CollectionItemRead(
                    collection_id=ir_dict["collection_id"],
                    work_id=ir_dict["work_id"],
                    added_at=ir_dict["added_at"],
                    notes=ir_dict["notes"],
                    sort_order=ir_dict["sort_order"],
                    work=work_item
                ))

            col_dict = dict(col_row)
            col_dict["items"] = items
            return CollectionDetailRead(**col_dict)

    def update_collection(self, collection_id: str, update: CollectionUpdate) -> bool:
        """更新書單名稱、描述或圖示。"""
        now = self.current_iso()
        fields = []
        params = []
        if update.name is not None:
            fields.append("name = ?")
            params.append(update.name)
        if update.description is not None:
            fields.append("description = ?")
            params.append(update.description)
        if update.icon is not None:
            fields.append("icon = ?")
            params.append(update.icon)

        if not fields:
            return False

        fields.append("updated_at = ?")
        params.append(now)
        params.append(collection_id)

        with self.engine.session() as conn:
            cursor = conn.execute(
                f"UPDATE collection SET {', '.join(fields)} WHERE collection_id = ?",
                tuple(params)
            )
            return cursor.rowcount > 0

    def delete_collection(self, collection_id: str) -> bool:
        """刪除書單（系統預設書單不可刪除）。"""
        with self.engine.session() as conn:
            cursor = conn.execute(
                "DELETE FROM collection WHERE collection_id = ? AND is_system = 0",
                (collection_id,)
            )
            return cursor.rowcount > 0

    def add_work_to_collection(self, collection_id: str, work_id: str, notes: Optional[str] = None) -> bool:
        """將書籍加入書單。"""
        now = self.current_iso()
        with self.engine.session() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO collection_item (collection_id, work_id, added_at, notes, sort_order)
                VALUES (?, ?, ?, ?, 0)
                """,
                (collection_id, work_id, now, notes)
            )
        return True

    def remove_work_from_collection(self, collection_id: str, work_id: str) -> bool:
        """從書單中移除書籍。"""
        with self.engine.session() as conn:
            cursor = conn.execute(
                "DELETE FROM collection_item WHERE collection_id = ? AND work_id = ?",
                (collection_id, work_id)
            )
            return cursor.rowcount > 0

    def get_work_collections(self, work_id: str) -> List[str]:
        """查詢某書籍被加入的所有書單 ID。"""
        with self.engine.session() as conn:
            rows = conn.execute(
                "SELECT collection_id FROM collection_item WHERE work_id = ?",
                (work_id,)
            ).fetchall()
            return [r["collection_id"] for r in rows]

    # === 多階層分類體系與線上書攤 (Category & Shelf) DAO 實作 ===

    # 三條讀路徑（樹統計 / 分類詳情 / 架位查詢）共用的「可信分類」子查詢。
    #
    # 為何必須只有一份：三條各自寫一次 WHERE 就是三份會漂移的副本，而漂移的
    # 症狀是「書架上看得到這本、分類徽章的數字卻不算它」——只有使用者會發現。
    #
    # 判準：一個 work_category 列只有在下列任一成立時才對使用者可見——
    #   1. 該 Work 的 classification_state = 'classified'：自動分類已判定且可信。
    #      回填前的 legacy 列全部是 'pending'，於是那些被舊 fallback 塞進 cat_850
    #      的作業系統書**從一開始就不出現在書攤上**，而不是等回填跑完才消失。
    #   2. 該列 source = 'manual'：使用者手動指定的分類永遠可見，與自動狀態無關。
    #
    # 帶兩個 ? 參數，依序為 (CLASSIFIED, MANUAL)。
    _VISIBLE_WORK_CATEGORY_SQL = """
        SELECT wc.work_id AS work_id, wc.category_id AS category_id
        FROM work_category wc
        JOIN work w_cls ON w_cls.work_id = wc.work_id
        WHERE w_cls.classification_state = ? OR wc.source = ?
    """

    @property
    def _visible_params(self) -> Tuple[str, str]:
        return (ClassificationState.CLASSIFIED, ClassificationSource.MANUAL)

    def get_category_tree(self) -> List[CategoryTreeNode]:
        """取得多階層分類樹結構與各節點藏書數量。

        每一層的 works_count 都是該子樹的 **DISTINCT work_id** 數，不是子節點
        計數的加總。同一本書命中兩個 sibling 葉節點（例：既是 cat_471 也是
        cat_472）時，加總會把它算兩次，使父分類徽章大於點進去實際看到的本數。
        """
        with self.engine.session() as conn:
            cat_rows = conn.execute(
                "SELECT * FROM category ORDER BY level ASC, sort_order ASC"
            ).fetchall()
            visible_rows = conn.execute(
                f"""
                SELECT vwc.category_id,
                       COALESCE('md5:' || lower(i.value), 'local:' || vwc.work_id) AS identity
                FROM ({self._VISIBLE_WORK_CATEGORY_SQL}) vwc
                LEFT JOIN identifier i
                  ON i.work_id = vwc.work_id AND i.scheme = 'md5'
                """,
                self._visible_params,
            ).fetchall()
            remote_rows = conn.execute(
                """
                SELECT rcc.category_id, 'md5:' || lower(rci.md5) AS identity
                FROM remote_catalog_category rcc
                JOIN remote_catalog_item rci ON rci.catalog_id = rcc.catalog_id
                """
            ).fetchall()

        direct: Dict[str, Set[str]] = {}
        for r in [*visible_rows, *remote_rows]:
            direct.setdefault(r["category_id"], set()).add(r["identity"])

        node_map: Dict[str, CategoryTreeNode] = {}
        roots: List[CategoryTreeNode] = []

        for r in cat_rows:
            r_dict = dict(r)
            node = CategoryTreeNode(
                category_id=r_dict["category_id"],
                parent_id=r_dict["parent_id"],
                name=r_dict["name"],
                slug=r_dict["slug"],
                icon=r_dict["icon"],
                level=r_dict["level"],
                sort_order=r_dict["sort_order"],
                works_count=0,
                children=[]
            )
            node_map[node.category_id] = node

        for node in node_map.values():
            if node.parent_id and node.parent_id in node_map:
                node_map[node.parent_id].children.append(node)
            elif not node.parent_id:
                roots.append(node)

        for root in roots:
            self._rollup_distinct_works(root, direct)

        return roots

    def _rollup_distinct_works(
        self, node: CategoryTreeNode, direct: Dict[str, Set[str]]
    ) -> Set[str]:
        """自底向上求子樹的 work_id 聯集，把基數寫回節點。

        回傳集合而非數字，是因為「父的本數」不是「子的本數之和」——
        必須先聯集再取基數，否則跨 sibling 的重複會被算兩次。
        """
        works: Set[str] = set(direct.get(node.category_id, ()))
        for child in node.children:
            works |= self._rollup_distinct_works(child, direct)
        node.works_count = len(works)
        return works

    def _category_scope_ids(self, conn: sqlite3.Connection, category_id: str) -> List[str]:
        """取得自身與完整子樹，供列表與 distinct union 統計共用。"""
        rows = conn.execute(
            """
            WITH RECURSIVE subtree(category_id) AS (
                SELECT category_id FROM category WHERE category_id = ?
                UNION ALL
                SELECT c.category_id FROM category c
                JOIN subtree s ON c.parent_id = s.category_id
            )
            SELECT category_id FROM subtree
            """,
            (category_id,),
        ).fetchall()
        return [row["category_id"] for row in rows]

    def get_category(self, category_id: str) -> Optional[CategoryRead]:
        """取得單一分類節點（works_count 與架位查詢同範圍、同可信判準）。"""
        with self.engine.session() as conn:
            row = conn.execute(
                "SELECT * FROM category WHERE category_id = ?", (category_id,)
            ).fetchone()
            if not row:
                return None

            cat_ids = self._category_scope_ids(conn, category_id)
            placeholders = ",".join("?" for _ in cat_ids)
            count_row = conn.execute(
                f"""
                SELECT COUNT(DISTINCT identity) AS works_count FROM (
                    SELECT COALESCE('md5:' || lower(i.value), 'local:' || vwc.work_id) AS identity
                    FROM ({self._VISIBLE_WORK_CATEGORY_SQL}) vwc
                    LEFT JOIN identifier i ON i.work_id = vwc.work_id AND i.scheme = 'md5'
                    WHERE vwc.category_id IN ({placeholders})
                    UNION
                    SELECT 'md5:' || lower(rci.md5) AS identity
                    FROM remote_catalog_category rcc
                    JOIN remote_catalog_item rci ON rci.catalog_id = rcc.catalog_id
                    WHERE rcc.category_id IN ({placeholders})
                )
                """,
                tuple(self._visible_params) + tuple(cat_ids) + tuple(cat_ids),
            ).fetchone()

            data = dict(row)
            data["works_count"] = count_row["works_count"]
            return CategoryRead(**data)

    # === 智慧分類 provenance 與狀態 (Classification) ===

    def _write_classification(
        self,
        conn: sqlite3.Connection,
        work_id: str,
        outcome: ClassificationOutcome,
        now: Optional[str] = None,
    ) -> None:
        """在**已開啟的交易內**原子替換某 Work 的自動分類與狀態。

        「原子」在此有具體所指：刪除舊的自動分類（source in rule/llm/legacy）
        與寫入新分類、更新狀態三者同屬一個 transaction。分開做會出現一個中間
        態——舊分類已刪、新分類未寫——那一瞬間書會從架上消失。

        失敗態（error/disabled）**不動既有分類**：保留原資料供重試，只更新
        狀態欄。這是刻意的：把已知可能正確的 rule 分類因為一次網路逾時就清掉，
        等於用「判不出」覆蓋「判得出」。

        **manual 列完全不受本方法影響**：既不被 DELETE、也不被 UPSERT 改寫。
        自動分類器對使用者手動指定的分類沒有寫入權——否則回填一次就静默
        吃掉人工修正，而且沒有任何輸出會顯示這件事發生過。
        """
        now = now or self.current_iso()

        conn.execute(
            "UPDATE work SET classification_state = ?, classification_error = ?, "
            "classified_at = ?, updated_at = ? WHERE work_id = ?",
            (
                outcome.state,
                outcome.error,
                now if outcome.is_classified else None,
                now,
                work_id,
            ),
        )

        if outcome.state in (ClassificationState.ERROR, ClassificationState.DISABLED):
            return

        # classified / unclassified / pending 都代表「這次判定有結論」，
        # 舊的自動分類一律讓位。unclassified 與 pending 的結論就是「沒有分類」。
        # 只刪 AUTOMATIC（rule/llm/legacy）；manual 不在列。
        auto_marks = ",".join("?" * len(ClassificationSource.AUTOMATIC))
        conn.execute(
            f"DELETE FROM work_category WHERE work_id = ? AND source IN ({auto_marks})",
            (work_id,) + tuple(ClassificationSource.AUTOMATIC),
        )

        if not outcome.is_classified:
            return

        # 葉節點 + 其父節點都寫入。
        #
        # 為何不是「只寫葉節點」（推翻 design.md 的葉節點-only 契約）：
        # `get_category()` (:800) 的 works_count 只 COUNT 自身 category_id、不含
        # 子代，而該值經 CategoryWorksResponse.category 回給前端。只寫葉節點會
        # 讓所有父分類的計數變 0。父 row 的 source 沿用同一個 provenance，
        # 故回填時一併被上面的 DELETE 清掉，不會殘留。
        rows = []
        for cid in outcome.category_ids:
            rows.append(cid)
            parent = parent_of(cid)
            if parent != cid:
                rows.append(parent)

        for cid in dict.fromkeys(rows):
            # UPSERT 的 DO UPDATE 帶 WHERE：只有衝突列本身是自動來源時才改寫。
            #
            # 上面的 DELETE 已清掉所有自動列，所以這裡還會衝突的只剩一種可能：
            # 同一個 (work_id, category_id) 上存在 manual 列。若沒有 WHERE，這行會
            # 把使用者手動指定的 source 改成 rule/llm，provenance 就消失了。
            # 衝突且為 manual 時這行退化成 no-op，manual 列原封不動保留。
            auto_marks_upsert = ",".join("?" * len(ClassificationSource.AUTOMATIC))
            conn.execute(
                f"""
                INSERT INTO work_category
                    (work_id, category_id, confidence, source, model, prompt_version, assigned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(work_id, category_id) DO UPDATE SET
                    confidence = excluded.confidence,
                    source = excluded.source,
                    model = excluded.model,
                    prompt_version = excluded.prompt_version,
                    assigned_at = excluded.assigned_at
                WHERE work_category.source IN ({auto_marks_upsert})
                """,
                (
                    work_id, cid, outcome.confidence, outcome.source,
                    outcome.model, outcome.prompt_version, now,
                ) + tuple(ClassificationSource.AUTOMATIC),
            )

    def apply_classification(self, work_id: str, outcome: ClassificationOutcome) -> None:
        """對單一 Work 套用分類結果（自帶短 transaction）。"""
        with self.engine.session() as conn:
            self._write_classification(conn, work_id, outcome)

    def get_work_classification_input(self, work_id: str) -> Optional[Dict[str, Any]]:
        """取出分類器需要的欄位。找不到回 None（呼叫端負責分辨）。"""
        with self.engine.session() as conn:
            row = conn.execute(
                "SELECT work_id, title, authors_display, language, work_type, "
                "classification_state FROM work WHERE work_id = ?",
                (work_id,),
            ).fetchone()
            return dict(row) if row else None

    # 回填的預設候選：**所有非 classified 的狀態**。
    #
    # unclassified / disabled 也在內（使用者裁決）：它們代表「用當時的 API 設定
    # 與當時的模型判不出」，而那兩者都會變——補上 API key、換更強的模型之後，
    # 這些書必須能被重試。把它們排除在預設之外，等於讓一次暫時性的環境缺陷
    # 變成永久判決，而且使用者從介面上看不出還有這批書可救。
    DEFAULT_BACKFILL_STATES: Tuple[str, ...] = tuple(
        s for s in ClassificationState.ALL if s != ClassificationState.CLASSIFIED
    )

    def list_works_for_classification(
        self, states: Optional[List[str]] = None, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """列出待分類的 Work。預設抓**所有非 classified**的狀態。

        已 classified 的不入選（幂等）；其餘四種狀態都是「還沒有可信分類」，
        都應在環境改善後可重試。
        """
        states = list(states) if states else list(self.DEFAULT_BACKFILL_STATES)
        marks = ",".join("?" * len(states))
        sql = (
            "SELECT work_id, title, authors_display, language, work_type, "
            f"classification_state FROM work WHERE classification_state IN ({marks}) "
            "ORDER BY created_at ASC"
        )
        params: List[Any] = list(states)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.engine.session() as conn:
            return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]

    def get_classification_stats(self) -> Dict[str, int]:
        """各 classification_state 的筆數。零筆的狀態也會出現（值為 0）。

        缺席的 key 與 0 不得共用同一個輸出：呼叫端要能分辨「這個狀態沒有書」
        與「這個狀態名打錯了」。
        """
        stats = {s: 0 for s in ClassificationState.ALL}
        with self.engine.session() as conn:
            for r in conn.execute(
                "SELECT classification_state AS s, COUNT(*) AS c FROM work GROUP BY 1"
            ).fetchall():
                stats[r["s"]] = r["c"]
        return stats

    def get_work_categories_detail(self, work_id: str) -> List[Dict[str, Any]]:
        """某 Work 的所有分類關聯（含 provenance），供稽核與測試使用。"""
        with self.engine.session() as conn:
            rows = conn.execute(
                "SELECT category_id, confidence, source, model, prompt_version, assigned_at "
                "FROM work_category WHERE work_id = ? ORDER BY category_id",
                (work_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_category_works(
        self,
        category_id: str,
        page: int = 1,
        page_size: int = 20
    ) -> Tuple[int, List[SearchResultItem]]:
        """取得特定分類架位（含其子分類）下的所有藏書。

        只回傳**可信分類**（見 `_VISIBLE_WORK_CATEGORY_SQL`）：回填前那些被舊
        fallback 塞進 cat_850 的作業系統書不得出現在書攤上。與 `get_category`
        的徽章數字共用同一個判準與同一個範圍，否則徽章寫 1、點進去是空的。
        """
        with self.engine.session() as conn:
            cat_ids = self._category_scope_ids(conn, category_id)
            placeholders = ",".join("?" for _ in cat_ids)
            visible_params = tuple(self._visible_params)

            # 計算總數
            count_sql = f"""
                SELECT COUNT(DISTINCT vwc.work_id) AS total
                FROM ({self._VISIBLE_WORK_CATEGORY_SQL}) vwc
                WHERE vwc.category_id IN ({placeholders})
            """
            total = conn.execute(
                count_sql, visible_params + tuple(cat_ids)
            ).fetchone()["total"]

            # 分頁查詢
            offset = (page - 1) * page_size
            query_sql = f"""
                SELECT DISTINCT
                    w.work_id, w.title, w.authors_display, w.publication_year, w.language, w.availability_tier,
                    rs.progress_ratio,
                    (SELECT m.format FROM manifestation m WHERE m.work_id = w.work_id LIMIT 1) AS format,
                    (SELECT f.size_bytes FROM file_object f 
                     JOIN manifestation m ON f.manifestation_id = m.manifestation_id 
                     WHERE m.work_id = w.work_id LIMIT 1) AS size_bytes,
                    (SELECT id.value FROM identifier id WHERE id.work_id = w.work_id AND id.scheme = 'md5' LIMIT 1) AS md5
                FROM work w
                JOIN ({self._VISIBLE_WORK_CATEGORY_SQL}) vwc ON w.work_id = vwc.work_id
                LEFT JOIN reading_state rs ON w.work_id = rs.work_id
                WHERE vwc.category_id IN ({placeholders})
                ORDER BY w.created_at DESC
                LIMIT ? OFFSET ?
            """
            params = visible_params + tuple(cat_ids) + (page_size, offset)
            rows = conn.execute(query_sql, params).fetchall()

            items = [
                SearchResultItem(
                    work_id=r["work_id"],
                    title=r["title"],
                    authors_display=r["authors_display"],
                    publication_year=r["publication_year"],
                    language=r["language"],
                    format=r["format"] or "pdf",
                    size_bytes=r["size_bytes"],
                    md5=r["md5"],
                    availability_tier=r["availability_tier"],
                    progress_ratio=r["progress_ratio"]
                )
                for r in rows
            ]
            return total, items

    # === 系統設定與自訂 Libgen 鏡像管理 (System Settings & Custom Libgen Mirrors) ===

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """讀取系統設定值。"""
        with self.engine.session() as conn:
            row = conn.execute("SELECT value FROM system_setting WHERE key = ?", (key,)).fetchone()
            if row:
                return row["value"]
            return default

    def set_setting(self, key: str, value: str) -> None:
        """寫入或更新系統設定值。"""
        now = self.current_iso()
        with self.engine.session() as conn:
            conn.execute(
                """
                INSERT INTO system_setting (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now)
            )

    def get_libgen_mirrors(self) -> List[Dict[str, Any]]:
        """讀取已設定之 Libgen 鏡像清單（若未設定則回傳預設清單）。"""
        raw = self.get_setting("libgen_mirrors")
        if not raw:
            # 尚未設定：正常初始態，靜默回退。
            return [dict(m) for m in DEFAULT_LIBGEN_MIRRORS]
        try:
            import json
            data = json.loads(raw)
            if isinstance(data, list) and len(data) > 0:
                return data
            # 解析成功但形狀不對（非 list 或空 list）——使用者資料實際存在卻不可用，
            # 與「尚未設定」是完全不同的狀態，不得共用同一個靜默輸出。
            log.warning(
                "libgen_mirrors 設定內容形狀不正確（type=%s len=%s），已回退至預設清單",
                type(data).__name__,
                len(data) if hasattr(data, "__len__") else "n/a",
            )
        except Exception as exc:
            # JSON 損毀：使用者的鏡像設定實際存在但讀不出來。靜默回退會讓
            # 「設定損毀」與「尚未設定」長得一模一樣，兩者都變成「預設清單全開」。
            log.warning(
                "libgen_mirrors 設定解析失敗，已回退至預設清單：%s: %s",
                type(exc).__name__, exc,
            )
        return [dict(m) for m in DEFAULT_LIBGEN_MIRRORS]

    def save_libgen_mirrors(self, mirrors: List[Dict[str, Any]]) -> None:
        """保存 Libgen 鏡像清單。"""
        import json
        self.set_setting("libgen_mirrors", json.dumps(mirrors, ensure_ascii=False))

    def reset_libgen_mirrors(self) -> List[Dict[str, Any]]:
        """重設回預設 Libgen 鏡像清單。"""
        import json
        defaults = [dict(m) for m in DEFAULT_LIBGEN_MIRRORS]
        self.set_setting("libgen_mirrors", json.dumps(defaults, ensure_ascii=False))
        return defaults

    def get_active_libgen_mirror_urls(self, adapter_types: Optional[List[str]] = None) -> List[str]:
        """
        取得「已啟用」且「通過預檢驗證 (verified)」的有效鏡像 URL 陣列，供爬蟲與下載解析器正式呼叫。
        未經驗證或解析不相容 (incompatible_layout) 者將被隔離阻擋。
        """
        mirrors = self.get_libgen_mirrors()
        active = []
        for m in sorted(mirrors, key=lambda x: x.get("priority", 999)):
            if not m.get("enabled", True):
                continue
            # 只有驗證通過者可正式參與
            status = m.get("validation_status", "unverified")
            if status != "verified":
                continue
            if adapter_types and m.get("adapter_type") not in adapter_types:
                continue
            url = m.get("url", "").rstrip("/")
            if not url:
                continue
            # 既存 DB 裡的死鏡像可能仍標著 verified（寫入時它們還活著），
            # 在此過濾才能同時覆蓋常數路徑與 DB 路徑。
            if is_known_dead_mirror(url):
                log.debug("跳過實測已死的鏡像（見 BR-20260820_111523）: %s", url)
                continue
            active.append(url)

        if not active:
            # 安全防線：若無任何通過驗證之鏡像。
            #
            # 此路徑先前回傳「所有 enabled 的預設鏡像」且**完全不看
            # validation_status**，等於繞過驗證閘：「驗證失敗」與「尚未驗證」
            # 共用同一個輸出，而且那個輸出是「全部放行」。改為仍套用
            # verified 與死鏡像過濾，並在真的走到這裡時大聲記錄。
            active = [
                m["url"].rstrip("/")
                for m in DEFAULT_LIBGEN_MIRRORS
                if m.get("enabled", True)
                and m.get("validation_status") == "verified"
                and not is_known_dead_mirror(m.get("url", ""))
            ]
            log.warning(
                "無任何通過驗證的鏡像，已回退至預設清單中已驗證且非已知死亡的鏡像：%s",
                active or "（空，無可用鏡像）",
            )
        return active

