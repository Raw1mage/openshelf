import sqlite3
from typing import List, Optional
from app.db.engine import DatabaseEngine
from app.models.catalog import SearchResultItem, SearchResponse


class SearchEngine:
    """提供基於 SQLite FTS5 Trigram 與結構化欄位的複合搜尋引擎。"""

    def __init__(self, engine: DatabaseEngine = None):
        self.engine = engine or DatabaseEngine()

    def search(
        self,
        query: str = "",
        format_filter: Optional[str] = None,
        language_filter: Optional[str] = None,
        year_min: Optional[int] = None,
        year_max: Optional[int] = None,
        page: int = 1,
        page_size: int = 20
    ) -> SearchResponse:
        """執行複合全文檢索與條件篩選。"""
        offset = (page - 1) * page_size
        cleaned_query = query.strip()

        params = []
        where_clauses = ["w.merged_into IS NULL"]

        if cleaned_query:
            # 使用 FTS5 全文比對
            where_clauses.append("w.work_id IN (SELECT work_id FROM work_fts WHERE work_fts MATCH ?)")
            # 處理 FTS5 trigram 查詢語法（包裝引號以支援中文片語）
            escaped_q = f'"{cleaned_query}"'
            params.append(escaped_q)

        if format_filter and format_filter != "all":
            where_clauses.append(
                "w.work_id IN (SELECT work_id FROM manifestation WHERE format = ?)"
            )
            params.append(format_filter)

        if language_filter and language_filter != "all":
            where_clauses.append("w.language = ?")
            params.append(language_filter)

        if year_min is not None:
            where_clauses.append("w.publication_year >= ?")
            params.append(year_min)

        if year_max is not None:
            where_clauses.append("w.publication_year <= ?")
            params.append(year_max)

        where_sql = " AND ".join(where_clauses)

        # 計算總數
        count_sql = f"SELECT COUNT(*) as total FROM work w WHERE {where_sql}"

        # 查詢列表（關聯首個實體檔案之 format/size/md5/progress）
        select_sql = f"""
            SELECT 
                w.work_id,
                w.title,
                w.authors_display,
                w.publication_year,
                w.language,
                w.availability_tier,
                m.format as format,
                f.size_bytes as size_bytes,
                f.md5 as md5,
                r.progress_ratio as progress_ratio,
                CASE 
                    WHEN ? != '' THEN (
                        SELECT snippet(work_fts, 3, '<mark>', '</mark>', '...', 20)
                        FROM work_fts 
                        WHERE work_fts.work_id = w.work_id AND work_fts MATCH ?
                        LIMIT 1
                    )
                    ELSE NULL 
                END as snippet
            FROM work w
            LEFT JOIN manifestation m ON w.work_id = m.work_id AND m.origin = 'local'
            LEFT JOIN file_object f ON m.manifestation_id = f.manifestation_id AND f.role = 'original'
            LEFT JOIN reading_state r ON w.work_id = r.work_id
            WHERE {where_sql}
            ORDER BY w.availability_tier ASC, w.publication_year DESC, w.created_at DESC
            LIMIT ? OFFSET ?
        """

        with self.engine.session() as conn:
            # 統計總筆數
            total = conn.execute(count_sql, params).fetchone()["total"]

            # 執行分頁查詢
            query_params = [cleaned_query, f'"{cleaned_query}"' if cleaned_query else ""] + params + [page_size, offset]
            rows = conn.execute(select_sql, query_params).fetchall()

            items = []
            for row in rows:
                items.append(
                    SearchResultItem(
                        work_id=row["work_id"],
                        title=row["title"],
                        authors_display=row["authors_display"],
                        publication_year=row["publication_year"],
                        language=row["language"],
                        format=row["format"],
                        size_bytes=row["size_bytes"],
                        md5=row["md5"],
                        availability_tier=row["availability_tier"],
                        snippet=row["snippet"],
                        progress_ratio=row["progress_ratio"]
                    )
                )

        return SearchResponse(
            query=query,
            total=total,
            page=page,
            page_size=page_size,
            items=items
        )
