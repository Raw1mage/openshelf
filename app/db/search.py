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
    terms = [
        '"' + term.replace('"', '""') + '"'
        for term in extract_query_terms(raw_query)
    ]

    if not terms:
        return None

    return " AND ".join(terms)


def extract_query_terms(raw_query: str) -> List[str]:
    """抽出可查詢的原始詞（未加引號），供 snippet 高亮使用。

    與 `build_fts_query` 共用同一組切詞規則，兩者不可各自實作——否則
    「WHERE 命中的詞」與「被高亮的詞」會不一致，而那會讓「這一列沒有高亮」
    同時代表「該列其實沒命中」與「兩邊規則不一致」兩件事。
    """
    out: List[str] = []
    for raw_term in _TERM_RE.findall(raw_query or ""):
        # 去掉僅出現在詞首尾的標點（`Concept.` -> `Concept`），
        # 但保留詞內部的符號（`C++`、`.NET` 的 `.` 在詞首故會被保留判斷）
        term = raw_term.strip("._'-")
        if len(term) < _TRIGRAM_MIN_LEN:
            continue
        out.append(term)
    return out


# snippet 視窗：命中處向前取 30 字，總長 120 字。
_SNIPPET_CONTEXT_BEFORE = 30
_SNIPPET_WINDOW = 120


def _build_snippet_map(conn, work_ids: List[str], terms: List[str]) -> dict:
    """為本頁的 work_id 取回 snippet 片段，並就地標記 `<mark>`。

    為何不用 FTS5 內建的 `snippet()`：它必須重新掃描整份 content 才能定位
    token 偏移，成本與**文件大小**線性相關而非與命中數相關。
    實測（BR-20260821_050000）：14 筆命中、共 27.2M 字元全文 -> **97.6 秒**，
    其中單一 3.87M 字元的文件就占 17.7 秒；而單列小文件只要 0.005 秒。
    改用 `instr` + `substr` 只回傳命中處前後的固定視窗，同一組資料
    實測 **0.078 秒**（1250x），且僅傳輸 840 字元而非 27.2M。

    只對**本頁**的 work_id 取，因此成本不隨總命中數成長。

    回傳 `{work_id: snippet}`；未命中的 work_id **不出現在鍵中**（而非映到 None），
    呼叫端用 `.get()` 取值即可，兩種情形對外都是 `None`。
    """
    if not work_ids or not terms:
        return {}

    used = terms[:5]
    branches = []
    params: List = []
    for term in used:
        branches.append(
            "CASE WHEN instr(lower(content), lower(?)) > 0 "
            "THEN substr(content, MAX(1, instr(lower(content), lower(?)) - ?), ?) "
            "END"
        )
        params.extend([term, term, _SNIPPET_CONTEXT_BEFORE, _SNIPPET_WINDOW])

    # COALESCE 至少需要兩個引數，單一分支時不可包。
    frag_expr = branches[0] if len(branches) == 1 else "COALESCE(%s)" % ", ".join(branches)
    id_placeholders = ",".join("?" for _ in work_ids)
    sql = "SELECT work_id, %s AS frag FROM work_fts WHERE work_id IN (%s)" % (
        frag_expr, id_placeholders
    )

    # 長詞優先，避免 `the` 先被包後破壞 `theory` 的標記。
    pattern = re.compile(
        "|".join(re.escape(t) for t in sorted(used, key=len, reverse=True)),
        re.IGNORECASE,
    )

    out = {}
    for row in conn.execute(sql, params + list(work_ids)):
        frag = row["frag"]
        if not frag:
            continue
        out[row["work_id"]] = "..." + pattern.sub(
            lambda m: "<mark>%s</mark>" % m.group(0), frag
        ) + "..."
    return out


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
                r.progress_ratio as progress_ratio
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
            query_params = params + [page_size, offset]
            rows = conn.execute(select_sql, query_params).fetchall()

            # snippet 在分頁**之後**另外取，只對本頁的 work_id。
            # 舊實作把它寫成 SELECT 列中的相關子查詢，於是每一列都要重跑一次
            # 全文掃描（BR-20260821_050000）。掃描成本跟文件大小走，不跟命中數走，
            # 所以分頁救不了它——實測 page_size=20 與 page_size=100 同樣是 92 秒。
            snippets = _build_snippet_map(
                conn,
                [row["work_id"] for row in rows],
                extract_query_terms(cleaned_query) if fts_expr else [],
            )

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
                        snippet=snippets.get(row["work_id"]),
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
