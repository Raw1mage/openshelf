import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.db.engine import DatabaseEngine


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
        now = self._now()
        added = 0
        updated = 0
        with self.engine.session() as conn:
            for item in items:
                md5 = str(item.get("md5") or "").strip().lower()
                if len(md5) != 32:
                    continue
                catalog_id = f"md5:{md5}"
                exists = conn.execute(
                    "SELECT 1 FROM remote_catalog_item WHERE catalog_id = ?", (catalog_id,)
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO remote_catalog_item
                        (catalog_id, md5, title, authors_display, publication_year,
                         language, format, extension, size_bytes, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(catalog_id) DO UPDATE SET
                        title = CASE
                            WHEN excluded.title = '未知書名' THEN remote_catalog_item.title
                            ELSE excluded.title
                        END,
                        authors_display = COALESCE(excluded.authors_display, remote_catalog_item.authors_display),
                        publication_year = COALESCE(excluded.publication_year, remote_catalog_item.publication_year),
                        language = COALESCE(excluded.language, remote_catalog_item.language),
                        format = COALESCE(excluded.format, remote_catalog_item.format),
                        extension = COALESCE(excluded.extension, remote_catalog_item.extension),
                        size_bytes = COALESCE(excluded.size_bytes, remote_catalog_item.size_bytes),
                        last_seen_at = excluded.last_seen_at
                    """,
                    (catalog_id, md5, item.get("title") or "未知書名",
                     item.get("authors_display"), item.get("publication_year"),
                     item.get("language"), item.get("format"), item.get("extension"),
                     item.get("size_bytes"), now, now),
                )
                added += 0 if exists else 1
                updated += 1 if exists else 0
                source = item.get("source") or "libgen"
                links = item.get("mirror_links") or []
                external_url = links[0] if links else None
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
                    (catalog_id, source, md5, external_url, json.dumps(links),
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
                       '[]' mirror_links_json
                FROM work w
                JOIN work_category wc ON wc.work_id = w.work_id
                JOIN scope s ON s.category_id = wc.category_id
                LEFT JOIN identifier i ON i.work_id = w.work_id AND i.scheme = 'md5'
                WHERE w.classification_state = 'classified' OR wc.source = 'manual'
                UNION ALL
                SELECT 'md5:' || lower(rci.md5), 1,
                       'libgen_' || lower(rci.md5), NULL, rci.title,
                       rci.authors_display, rci.publication_year, rci.language,
                       rci.format, rci.size_bytes, lower(rci.md5), rci.extension,
                       COALESCE(rcs.download_protocol, 'http'), rcs.torrent_url,
                       rcs.magnet_uri, rcs.peers_count,
                       COALESCE(rcs.mirror_links_json, '[]')
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
            item.pop("identity", None)
            item.pop("rn", None)
            items.append(item)
        return total, items
