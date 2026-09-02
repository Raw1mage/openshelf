import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.db.engine import DatabaseEngine
from app.models.catalog import license_for_source


class RemoteCatalogDAO:
    def __init__(self, engine: Optional[DatabaseEngine] = None):
        self.engine = engine or DatabaseEngine()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def begin_refresh(self, category_id: str, query_term: str) -> str:
        refresh_id = str(uuid.uuid4())
        now = self._now()
        with self.engine.session() as conn:
            previous = conn.execute(
                "SELECT last_success_at FROM remote_catalog_refresh "
                "WHERE category_id = ? AND last_success_at IS NOT NULL "
                "ORDER BY started_at DESC LIMIT 1",
                (category_id,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO remote_catalog_refresh
                    (refresh_id, category_id, status, query_term, started_at, last_success_at)
                VALUES (?, ?, 'refreshing', ?, ?, ?)
                """,
                (refresh_id, category_id, query_term, now,
                 previous["last_success_at"] if previous else None),
            )
        return refresh_id

    def finish_refresh(
        self,
        refresh_id: str,
        *,
        success: bool,
        pages_fetched: int,
        items_seen: int,
        items_added: int,
        items_updated: int,
        cursor: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        now = self._now()
        with self.engine.session() as conn:
            conn.execute(
                """
                UPDATE remote_catalog_refresh SET
                    status = ?, completed_at = ?,
                    last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END,
                    pages_fetched = ?, items_seen = ?, items_added = ?, items_updated = ?,
                    cursor = ?, error_message = ?
                WHERE refresh_id = ?
                """,
                ('fresh' if success else 'failed', now, success, now, pages_fetched,
                 items_seen, items_added, items_updated, cursor, error_message, refresh_id),
            )

    def get_status(self, category_id: str, stale_after_seconds: int = 86400) -> Dict[str, Any]:
        with self.engine.session() as conn:
            row = conn.execute(
                "SELECT * FROM remote_catalog_refresh WHERE category_id = ? "
                "ORDER BY started_at DESC LIMIT 1",
                (category_id,),
            ).fetchone()
            accumulated_total = conn.execute(
                """
                WITH RECURSIVE scope(category_id) AS (
                    SELECT category_id FROM category WHERE category_id = ?
                    UNION ALL
                    SELECT c.category_id FROM category c
                    JOIN scope s ON c.parent_id = s.category_id
                )
                SELECT COUNT(DISTINCT rcc.catalog_id) AS total
                FROM remote_catalog_category rcc
                JOIN scope s ON s.category_id = rcc.category_id
                """,
                (category_id,),
            ).fetchone()["total"]
        if not row:
            return {
                "status": "never_refreshed",
                "last_success_at": None,
                "error": None,
                "accumulated_total": accumulated_total,
                "pages_fetched": 0,
            }
        data = dict(row)
        status = data["status"]
        if status == "fresh" and data["last_success_at"]:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(data["last_success_at"])
            if age.total_seconds() > stale_after_seconds:
                status = "stale"
        return {
            "status": status,
            "last_success_at": data["last_success_at"],
            "error": data["error_message"],
            "accumulated_total": accumulated_total,
            "pages_fetched": data["pages_fetched"],
        }

    def upsert_batch(
        self, category_id: str, query_term: str, items: List[Dict[str, Any]]
    ) -> Tuple[int, int]:
        """插入/更新遠端書目，並掛上分類。

        Identity 重構（DD-1/DD-2, aggregator_multi-source-provider）：判斷「這是不是
        同一本書」的鍵是 `(source, source_native_id)`，不再是 `md5`。md5 降級為
        可空的橋接欄位——libgen 目前仍把 md5 當自己的原生 ID（`source_native_id`
        直接沿用 md5 值），故對 libgen 呼叫端行為不變；非 libgen 來源（例如
        Gutenberg）item 需自帶 `source_native_id`，沒有 md5 也能正確去重，
        不再落入「多筆 md5=NULL 互不衝突」的靜默去重失效。

        outcome codomain（errors.md `identity.upsert`）：呼叫端未帶
        `source_native_id` 且沒有 md5 可回退時，本筆視為 `not-run`（拒絕寫入，
        不計入 added/updated），不得靜默吞掉——上游 provider 契約缺失時，
        沉默塞入一筆猜測值只會讓下一輪去重更難排查。
        """
        now = self._now()
        added = 0
        updated = 0
        with self.engine.session() as conn:
            for item in items:
                source = str(item.get("source") or "libgen").strip()
                md5_raw = str(item.get("md5") or "").strip().lower()
                md5 = md5_raw if len(md5_raw) == 32 else None
                # 非 libgen 呼叫端須自帶 source_native_id；libgen 呼叫端目前尚未
                # 傳這個欄位（見 libgen_live.py 的 item dict），故此處以 md5 回退，
                # 對既有呼叫端零行為差異。
                source_native_id = str(item.get("source_native_id") or md5 or "").strip()
                if not source_native_id:
                    # identity.upsert = not-run：上游未帶可用的原生 ID，提前拒絕
                    # 不寫入（errors.md 已宣告此狀態），不得落成一筆猜測資料。
                    continue
                existing = conn.execute(
                    "SELECT catalog_id FROM remote_catalog_item WHERE source = ? AND source_native_id = ?",
                    (source, source_native_id),
                ).fetchone()
                catalog_id = existing["catalog_id"] if existing else f"rc_{uuid.uuid4().hex[:20]}"
                conn.execute(
                    """
                    INSERT INTO remote_catalog_item
                        (catalog_id, source, source_native_id, md5, title, authors_display,
                         publication_year, language, format, extension, size_bytes,
                         first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, source_native_id) DO UPDATE SET
                        title = CASE
                            WHEN excluded.title = '未知書名' THEN remote_catalog_item.title
                            ELSE excluded.title
                        END,
                        md5 = COALESCE(excluded.md5, remote_catalog_item.md5),
                        authors_display = COALESCE(excluded.authors_display, remote_catalog_item.authors_display),
                        publication_year = COALESCE(excluded.publication_year, remote_catalog_item.publication_year),
                        language = COALESCE(excluded.language, remote_catalog_item.language),
                        format = COALESCE(excluded.format, remote_catalog_item.format),
                        extension = COALESCE(excluded.extension, remote_catalog_item.extension),
                        size_bytes = COALESCE(excluded.size_bytes, remote_catalog_item.size_bytes),
                        last_seen_at = excluded.last_seen_at
                    """,
                    (catalog_id, source, source_native_id, md5, item.get("title") or "未知書名",
                     item.get("authors_display"), item.get("publication_year"),
                     item.get("language"), item.get("format"), item.get("extension"),
                     item.get("size_bytes"), now, now),
                )
                added += 0 if existing else 1
                updated += 1 if existing else 0
                links = item.get("mirror_links") or []
                external_url = links[0] if links else None
                # source_key 沿用 source_native_id（對 libgen 等於既有的 md5 行為，
                # 零差異；對非 libgen 來源則是它自己的原生 ID）。
                conn.execute(
                    """
                    INSERT INTO remote_catalog_source
                        (catalog_id, source, source_key, external_url, mirror_links_json,
                         torrent_url, magnet_uri, download_protocol, peers_count,
                         first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, source_key) DO UPDATE SET
                        catalog_id = excluded.catalog_id,
                        external_url = COALESCE(excluded.external_url, remote_catalog_source.external_url),
                        mirror_links_json = CASE
                            WHEN excluded.mirror_links_json = '[]' THEN remote_catalog_source.mirror_links_json
                            ELSE excluded.mirror_links_json
                        END,
                        torrent_url = COALESCE(excluded.torrent_url, remote_catalog_source.torrent_url),
                        magnet_uri = COALESCE(excluded.magnet_uri, remote_catalog_source.magnet_uri),
                        download_protocol = excluded.download_protocol,
                        peers_count = COALESCE(excluded.peers_count, remote_catalog_source.peers_count),
                        last_seen_at = excluded.last_seen_at
                    """,
                    (catalog_id, source, source_native_id, external_url, json.dumps(links),
                     item.get("torrent_url"), item.get("magnet_uri"),
                     item.get("download_protocol") or "http", item.get("peers_count"),
                     now, now),
                )
                conn.execute(
                    """
                    INSERT INTO remote_catalog_category
                        (catalog_id, category_id, query_term, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(catalog_id, category_id) DO UPDATE SET
                        query_term = excluded.query_term,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (catalog_id, category_id, query_term, now, now),
                )
        return added, updated


    def query_browseable(
        self, category_id: str, page: int, page_size: int
    ) -> Tuple[int, List[Dict[str, Any]]]:
        offset = (page - 1) * page_size
        common_sql = """
            WITH RECURSIVE scope(category_id) AS (
                SELECT category_id FROM category WHERE category_id = ?
                UNION ALL
                SELECT c.category_id FROM category c
                JOIN scope s ON c.parent_id = s.category_id
            ), candidates AS (
                SELECT COALESCE('md5:' || lower(i.value), 'local:' || w.work_id) identity,
                       0 priority, w.work_id, w.work_id local_work_id, w.title,
                       w.authors_display, w.publication_year, w.language,
                       COALESCE((SELECT m.format FROM manifestation m
                                 WHERE m.work_id = w.work_id LIMIT 1), 'pdf') format,
                       (SELECT f.size_bytes FROM file_object f JOIN manifestation m
                        ON m.manifestation_id = f.manifestation_id
                        WHERE m.work_id = w.work_id LIMIT 1) size_bytes,
                       lower(i.value) md5, NULL extension, 'http' download_protocol,
                       NULL torrent_url, NULL magnet_uri, NULL peers_count,
                       '[]' mirror_links_json, 'local' source
                FROM work w
                JOIN work_category wc ON wc.work_id = w.work_id
                JOIN scope s ON s.category_id = wc.category_id
                LEFT JOIN identifier i ON i.work_id = w.work_id AND i.scheme = 'md5'
                WHERE w.classification_state = 'classified' OR wc.source = 'manual'
                UNION ALL
                -- identity 回退鏈（DD-1/DD-2）：優先用 md5 與本地 work 對齊
                -- （既有行為，libgen 下載後可判定「已在本地」）；md5 為 NULL
                -- 的非 libgen item 一律回退到 catalog_id——它現在是
                -- (source, source_native_id) 複合鍵映射出的穩定唯一值。
                -- 若沒有這個回退，多筆 md5=NULL 的 row 會共享同一個 NULL
                -- identity，PARTITION BY 把它們併成一組，只有 rn=1 那筆
                -- 存活，其餘被 `WHERE rn = 1` 濾掉——這正是本次要修的
                -- 靜默去重失效在讀路徑上的翻版。
                SELECT COALESCE('md5:' || lower(rci.md5), 'rc:' || rci.catalog_id), 1,
                       COALESCE('libgen_' || lower(rci.md5), rci.catalog_id), NULL, rci.title,
                       rci.authors_display, rci.publication_year, rci.language,
                       rci.format, rci.size_bytes, lower(rci.md5), rci.extension,
                       COALESCE(rcs.download_protocol, 'http'), rcs.torrent_url,
                       rcs.magnet_uri, rcs.peers_count,
                       COALESCE(rcs.mirror_links_json, '[]'), rci.source
                FROM remote_catalog_item rci
                JOIN remote_catalog_category rcc ON rcc.catalog_id = rci.catalog_id
                JOIN scope s ON s.category_id = rcc.category_id
                LEFT JOIN remote_catalog_source rcs ON rcs.catalog_id = rci.catalog_id
            ), ranked AS (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY identity ORDER BY priority) rn
                FROM candidates
            )
        """
        count_sql = common_sql + "SELECT COUNT(*) AS total FROM ranked WHERE rn = 1"
        page_sql = common_sql + """
            SELECT * FROM ranked WHERE rn = 1
            ORDER BY priority, title, identity LIMIT ? OFFSET ?
        """
        with self.engine.session() as conn:
            total = conn.execute(count_sql, (category_id,)).fetchone()["total"]
            rows = conn.execute(page_sql, (category_id, page_size, offset)).fetchall()
        items: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["availability_tier"] = item.pop("priority")
            item["mirror_links"] = json.loads(item.pop("mirror_links_json") or "[]")
            # 授權標示由 source 推導（tasks.md 2.2），不落地到 schema——它是來源
            # 的性質而非逐筆資料，寫進 DB 反而會讓同一來源的舊 rows 停留在
            # 舊授權字串上。未登錄來源回 None（空白），不得套用預設公版。
            item["license"] = license_for_source(item.get("source"))
            item.pop("identity", None)
            item.pop("rn", None)
            items.append(item)
        return total, items
