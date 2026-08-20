#!/usr/bin/env python3
"""把 /api/search?q=the 的 92 秒拆到四個候選因子上。

直接對 DB 下與 app 完全相同的 SQL（複製自 app/db/search.py），
逐段計時 + EXPLAIN QUERY PLAN，回答「哪一格佔最多」。

刻意不經 API：避免佔用 threadpool token 干擾其他量測，
也讓計時不含 FastAPI / pydantic 的序列化開銷（那部分另外量）。
"""
import sqlite3
import sys
import time

DB = "/home/pkcs12/projects/openshelf/data/db/openshelf.sqlite"
FTS_EXPR = '"the"'   # build_fts_query("the") 的輸出
PAGE_SIZE = 20
OFFSET = 0

WHERE_SQL = ("w.merged_into IS NULL AND "
             "w.work_id IN (SELECT work_id FROM work_fts WHERE work_fts MATCH ?)")

COUNT_SQL = "SELECT COUNT(*) as total FROM work w WHERE %s" % WHERE_SQL

SELECT_SQL = """
    SELECT
        w.work_id, w.title, w.authors_display, w.publication_year,
        w.language, w.availability_tier,
        m.format as format, f.size_bytes as size_bytes, f.md5 as md5,
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
    WHERE %s
    ORDER BY w.availability_tier ASC, w.publication_year DESC, w.created_at DESC
    LIMIT ? OFFSET ?
""" % WHERE_SQL

# 無 snippet 版：把 snippet_flag 傳空字串，CASE 走 ELSE NULL 分支。
# 這是唯一「只關掉 snippet、其餘完全相同」的對照組。
SELECT_NO_SNIPPET_PARAMS = ["", ""] + [FTS_EXPR] + [PAGE_SIZE, OFFSET]
SELECT_FULL_PARAMS = [FTS_EXPR, FTS_EXPR] + [FTS_EXPR] + [PAGE_SIZE, OFFSET]


def conn():
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=30.0)
    c.row_factory = sqlite3.Row
    return c


def timed(label, fn):
    t = time.perf_counter()
    try:
        v = fn()
        dt = time.perf_counter() - t
        print("%-46s %8.3fs  -> %s" % (label, dt, v), flush=True)
        return dt
    except Exception as e:
        dt = time.perf_counter() - t
        print("%-46s %8.3fs  -> ERROR %s: %s" % (label, dt, type(e).__name__, e),
              flush=True)
        return dt


def main():
    print("=" * 78)
    print("A. 逐段計時（每段獨立新連線，避免 cache 互相汙染）")
    print("=" * 78)

    # A1 純 FTS MATCH：只解析 trigram 索引，不碰 work 表
    c = conn()
    timed("A1 FTS MATCH only (work_id list)",
          lambda: len(c.execute(
              "SELECT work_id FROM work_fts WHERE work_fts MATCH ?",
              [FTS_EXPR]).fetchall()))
    c.close()

    # A2 count_sql：BR §三 因子 2
    c = conn()
    timed("A2 count_sql (full COUNT)",
          lambda: c.execute(COUNT_SQL, [FTS_EXPR]).fetchone()["total"])
    c.close()

    # A3 select 無 snippet：因子 1+4（FTS + JOIN + ORDER BY）
    c = conn()
    timed("A3 select_sql WITHOUT snippet",
          lambda: len(c.execute(SELECT_SQL, SELECT_NO_SNIPPET_PARAMS).fetchall()))
    c.close()

    # A4 select 含 snippet：全部四個因子
    c = conn()
    timed("A4 select_sql WITH snippet (as app)",
          lambda: len(c.execute(SELECT_SQL, SELECT_FULL_PARAMS).fetchall()))
    c.close()

    # A5 單獨一次 snippet：證明單列成本
    c = conn()
    wid = c.execute("SELECT work_id FROM work_fts WHERE work_fts MATCH ? LIMIT 1",
                    [FTS_EXPR]).fetchone()
    if wid:
        c2 = conn()
        timed("A5 snippet for ONE work_id",
              lambda: c2.execute(
                  "SELECT snippet(work_fts,3,'<mark>','</mark>','...',20) "
                  "FROM work_fts WHERE work_fts.work_id = ? AND work_fts MATCH ? LIMIT 1",
                  [wid["work_id"], FTS_EXPR]).fetchone() is not None)
        c2.close()
    c.close()

    # A6 控制組：零命中查詢走同一條 SQL，必須快
    c = conn()
    timed("A6 CONTROL select_sql q=zzzznomatch (must be fast)",
          lambda: len(c.execute(
              SELECT_SQL,
              ['"zzzznomatchxyz"', '"zzzznomatchxyz"', '"zzzznomatchxyz"',
               PAGE_SIZE, OFFSET]).fetchall()))
    c.close()

    print()
    print("=" * 78)
    print("B. EXPLAIN QUERY PLAN")
    print("=" * 78)
    c = conn()
    for label, sql, params in [
        ("count_sql", COUNT_SQL, [FTS_EXPR]),
        ("select_sql (with snippet)", SELECT_SQL, SELECT_FULL_PARAMS),
    ]:
        print("--- %s ---" % label)
        for row in c.execute("EXPLAIN QUERY PLAN " + sql, params):
            print("   ", " | ".join(str(x) for x in row))
    c.close()


if __name__ == "__main__":
    main()
