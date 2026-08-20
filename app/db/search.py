import sqlite3
import re
from typing import List, Optional
from app.db.engine import DatabaseEngine
from app.models.catalog import SearchResultItem, SearchResponse


# 可構成查詢詞的字元：英數、非 ASCII（中日韓）、以及會出現在詞內部的符號
# （`C++`、`.NET`、`R&D`、`O'Reilly`、`state-of-the-art`）。
# 其餘字元（逗號、句點、括號、引號…）一律視為分隔符。
_TERM_RE = re.compile(r"[0-9A-Za-z\u0080-\uffff+#&._'\-]+")

# work_fts 使用 tokenize='trigram'，少於 3 個字元的詞在索引中不存在，
# 拿它當條件必然歸零（實測 `"C"` / `"8t"` / `"Ja"` 對確實含該子字串的列皆回 0）。
_TRIGRAM_MIN_LEN = 3


def build_fts_query(raw_query: str) -> Optional[str]:
    """把使用者輸入轉成 FTS5 安全的 MATCH 運算式。

    語意決策（AND，明確擇一）：
      多個詞之間取 **AND**——每個詞都必須出現，但不要求連續、不要求順序。
      這正是原缺陷所在：舊實作把整串輸入包成單一 phrase（`"..."`），
      而 FTS5 的 phrase 要求 token 序列連續且順序一致，於是標點與詞序
      都變成必須逐字命中的條件（`"Concept Operating"` 對確實含這兩個詞的
      列回 0）。改為逐詞 quote 後以 AND 相接即可解除該限制。

      不選 OR 的理由：OR 會讓任一詞命中就回傳，長查詢幾乎必然命中全庫，
      使「真的不存在的書」與「查詢過寬」再度共用同一個輸出。AND 保留精確度，
      且實測不會因出版社名而歸零（publisher 並非獨立欄位，其字樣存在於
      已被索引的 content 欄）。

    每個詞各自包成 phrase 並將內部 `"` escape 成 `""`，使 FTS5 語法字元
    （`*` `(` `)` `:` `^` `-` 及 AND/OR/NOT/NEAR 等關鍵字）一律被當成字面值，
    不會被解析成運算子、也不會拋語法例外。
    回傳 None 表示輸入不含任何可查詢的詞（例如只打了 `*` 或 `(`）。
    呼叫端必須把 None 當成「查不到」而非「不加條件」——後者會退化成回傳全庫。
    """
    terms: List[str] = []
    for raw_term in _TERM_RE.findall(raw_query or ""):
        # 去掉僅出現在詞首尾的標點（`Concept.` -> `Concept`），
        # 但保留詞內部的符號（`C++`、`.NET` 的 `.` 在詞首故會被保留判斷）
        term = raw_term.strip("._'-")
        if len(term) < _TRIGRAM_MIN_LEN:
            continue
        terms.append('"' + term.replace('"', '""') + '"')

    if not terms:
        return None

    return " AND ".join(terms)


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

        # 把使用者輸入轉成 FTS5 安全運算式。fts_expr 為 None 代表輸入不含
        # 任何可查詢的詞（例如只打了 `*`），此時必須回 0 筆而不是不加條件。
        fts_expr = build_fts_query(cleaned_query) if cleaned_query else None

        if cleaned_query:
            if fts_expr is None:
                # 有輸入但無可查詢的詞 -> 明確回 0 筆，
                # 不可省略條件（省略會退化成回傳全庫）。
                where_clauses.append("1 = 0")
            else:
                # 使用 FTS5 全文比對（逐詞 phrase，以 AND 相接）
                where_clauses.append("w.work_id IN (SELECT work_id FROM work_fts WHERE work_fts MATCH ?)")
                params.append(fts_expr)

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
            # snippet 子查詢與 WHERE 條件必須使用同一個 FTS 運算式，否則
            # 兩者語意會不一致（舊實作兩處各自把整串輸入包成 phrase）。
            # fts_expr 為 None 時傳空字串，CASE 的 `? != ''` 會讓 snippet 直接為 NULL。
            snippet_flag = fts_expr if fts_expr else ""
            query_params = [snippet_flag, snippet_flag] + params + [page_size, offset]
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
