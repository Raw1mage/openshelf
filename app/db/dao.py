import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Tuple, Dict, Any
from app.db.engine import DatabaseEngine
from app.models.catalog import (
    WorkCreate, WorkDetailRead, WorkRead, IdentifierRead,
    ManifestationRead, FileObjectRead, ReadingStateRead, ReadingProgressUpdate,
    SearchResultItem, CollectionCreate, CollectionUpdate, CollectionRead,
    CollectionDetailRead, CollectionItemRead, CategoryRead, CategoryTreeNode
)
from app.db.categories import DEFAULT_CATEGORY_TREE, infer_categories_for_work


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
    {
        "url": "https://libgen.rocks",
        "enabled": True,
        "note": "Libgen.li 系列備用節點",
        "is_default": True,
        "priority": 3,
        "validation_status": "verified",
        "adapter_type": "libgen_li"
    },
    {
        "url": "https://libgen.gs",
        "enabled": True,
        "note": "Libgen.li 系列分流節點",
        "is_default": True,
        "priority": 4,
        "validation_status": "verified",
        "adapter_type": "libgen_li"
    },
    {
        "url": "https://libgen.pm",
        "enabled": True,
        "note": "Libgen.pm 系列分流節點",
        "is_default": True,
        "priority": 5,
        "validation_status": "verified",
        "adapter_type": "libgen_li"
    },
    {
        "url": "https://libgen.is",
        "enabled": True,
        "note": "Libgen.is 傳統經典鏡像",
        "is_default": True,
        "priority": 6,
        "validation_status": "verified",
        "adapter_type": "libgen_is"
    },
    {
        "url": "https://libgen.rs",
        "enabled": True,
        "note": "Libgen.rs 官方鏡像",
        "is_default": True,
        "priority": 7,
        "validation_status": "verified",
        "adapter_type": "libgen_is"
    },
    {
        "url": "https://libgen.st",
        "enabled": True,
        "note": "Libgen.st 官方鏡像",
        "is_default": True,
        "priority": 8,
        "validation_status": "verified",
        "adapter_type": "libgen_is"
    },
    {
        "url": "http://library.lol",
        "enabled": True,
        "note": "Library.lol 直鏈下載 Gateway",
        "is_default": True,
        "priority": 9,
        "validation_status": "verified",
        "adapter_type": "direct_gateway"
    }
]


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
    }

    def __init__(self, engine: DatabaseEngine = None):
        self.engine = engine or DatabaseEngine()
        self.apply_column_migrations()
        self.seed_categories_if_needed()

    # 依賴新增欄位的索引：必須在 ALTER 補完欄位之後才能建立，
    # 否則對舊 DB 會以 "no such column" 中斷啟動。
    _POST_MIGRATION_INDEXES: List[str] = [
        "CREATE INDEX IF NOT EXISTS idx_manifestation_protocol ON manifestation(download_protocol)",
        "CREATE INDEX IF NOT EXISTS idx_download_job_protocol ON download_job(download_protocol)",
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

            # 對現有未分類的書籍進行自動分類關聯
            works = conn.execute("SELECT work_id, title, authors_display FROM work").fetchall()
            for w in works:
                cat_ids = infer_categories_for_work(w["title"], w["authors_display"])
                for cid in cat_ids:
                    conn.execute(
                        "INSERT OR IGNORE INTO work_category (work_id, category_id) VALUES (?, ?)",
                        (w["work_id"], cid)
                    )

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
            # 自動推導並寫入所屬分類
            cat_ids = infer_categories_for_work(work_data.title, work_data.authors_display)
            for cid in cat_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO work_category (work_id, category_id) VALUES (?, ?)",
                    (wid, cid)
                )
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
    def get_category_tree(self) -> List[CategoryTreeNode]:
        """取得多階層分類樹結構與各節點藏書數量。"""
        with self.engine.session() as conn:
            # 取得各分類直接所屬或子分類的書籍數量
            rows = conn.execute(
                """
                SELECT c.*, COUNT(DISTINCT wc.work_id) AS works_count
                FROM category c
                LEFT JOIN work_category wc ON c.category_id = wc.category_id
                GROUP BY c.category_id
                ORDER BY c.level ASC, c.sort_order ASC
                """
            ).fetchall()

            node_map: Dict[str, CategoryTreeNode] = {}
            roots: List[CategoryTreeNode] = []

            for r in rows:
                r_dict = dict(r)
                node = CategoryTreeNode(
                    category_id=r_dict["category_id"],
                    parent_id=r_dict["parent_id"],
                    name=r_dict["name"],
                    slug=r_dict["slug"],
                    icon=r_dict["icon"],
                    level=r_dict["level"],
                    sort_order=r_dict["sort_order"],
                    works_count=r_dict["works_count"],
                    children=[]
                )
                node_map[node.category_id] = node

            for node in node_map.values():
                if node.parent_id and node.parent_id in node_map:
                    node_map[node.parent_id].children.append(node)
                elif not node.parent_id:
                    roots.append(node)

            # 累加父層分類的總藏書數量（包含子分類）
            for root in roots:
                self._rollup_counts(root)

            return roots

    def _rollup_counts(self, node: CategoryTreeNode) -> int:
        child_sum = 0
        for child in node.children:
            child_sum += self._rollup_counts(child)
        node.works_count = max(node.works_count, child_sum)
        return node.works_count

    def get_category(self, category_id: str) -> Optional[CategoryRead]:
        """取得單一分類節點。"""
        with self.engine.session() as conn:
            row = conn.execute(
                """
                SELECT c.*, COUNT(DISTINCT wc.work_id) AS works_count
                FROM category c
                LEFT JOIN work_category wc ON c.category_id = wc.category_id
                WHERE c.category_id = ?
                GROUP BY c.category_id
                """,
                (category_id,)
            ).fetchone()
            if not row:
                return None
            return CategoryRead(**dict(row))

    def get_category_works(
        self,
        category_id: str,
        page: int = 1,
        page_size: int = 20
    ) -> Tuple[int, List[SearchResultItem]]:
        """取得特定分類架位（含其子分類）下的所有藏書。"""
        with self.engine.session() as conn:
            # 取得該分類及其所有子分類 ID
            cat_ids = [category_id]
            child_rows = conn.execute("SELECT category_id FROM category WHERE parent_id = ?", (category_id,)).fetchall()
            cat_ids.extend([cr["category_id"] for cr in child_rows])

            placeholders = ",".join("?" for _ in cat_ids)
            
            # 計算總數
            count_sql = f"""
                SELECT COUNT(DISTINCT w.work_id) AS total
                FROM work w
                JOIN work_category wc ON w.work_id = wc.work_id
                WHERE wc.category_id IN ({placeholders})
            """
            total = conn.execute(count_sql, tuple(cat_ids)).fetchone()["total"]

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
                JOIN work_category wc ON w.work_id = wc.work_id
                LEFT JOIN reading_state rs ON w.work_id = rs.work_id
                WHERE wc.category_id IN ({placeholders})
                ORDER BY w.created_at DESC
                LIMIT ? OFFSET ?
            """
            params = list(cat_ids) + [page_size, offset]
            rows = conn.execute(query_sql, tuple(params)).fetchall()

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
            return [dict(m) for m in DEFAULT_LIBGEN_MIRRORS]
        try:
            import json
            data = json.loads(raw)
            if isinstance(data, list) and len(data) > 0:
                return data
        except Exception:
            pass
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
            if url:
                active.append(url)

        if not active:
            # 安全防線：若無任何通過驗證之自訂鏡像，回傳預設啟用鏡像
            active = [m["url"].rstrip("/") for m in DEFAULT_LIBGEN_MIRRORS if m.get("enabled", True)]
        return active


