"""BR-20260820_130000 — 查詢字串含標點 / 詞序不同就回 0 筆。

根因是 FTS5 **phrase 語意**（`"..."` 要求 token 序列連續且順序一致），
不是標點本身——標點只是最容易觸發它的形式。

本檔的斷言分三組，缺一不可：
  1. 正向：BR 控制組表的每一列（含純詞序案例 `Concept Operating`）。
  2. **負向**：真的不存在的書仍須回 0。缺這格就無法區分
     「修好了」與「把所有查詢都變成命中」——一個 `MATCH '*'` 的實作
     會讓第 1 組全綠而第 2 組紅。
  3. 語法字元：`C++` / `"` / `(` / `*` / `AND` / `NEAR` 不得拋例外或 500。
"""

import sqlite3

import pytest

from app.db.search import build_fts_query


ROW_TITLE = "Operating System Concepts with Java 8th Edition"
ROW_AUTHORS = "Abraham Silberschatz, Peter Baer Galvin, Greg Gagne"
# content 欄在正式庫中存放 PDF 抽取全文，出版社字樣出現在此而非獨立欄位。
# 實測線上庫 `"Wiley"` 命中 6 筆，即經由本欄。
ROW_CONTENT = (
    "OPERATING SYSTEM CONCEPTS with JAVA  ABRAHAM SILBERSCHATZ  Yale University  "
    "PETER BAER GALVIN  GREG GAGNE  Eighth Edition  John Wiley & Sons, Inc."
)


@pytest.fixture()
def fts_conn():
    """記憶體內 FTS5 表，欄位與 tokenizer 均對齊正式 schema。

    欄位刻意與 `app/db/schema.sql` 的 work_fts 一致
    （title / authors_display / content，tokenize='trigram'）。
    只放 title 的簡化 double 會失真：出版社字樣實際存在於 content 欄，
    少了它會讓測試對 AND 語意得出與線上相反的結論。

    刻意不依賴 repo 內或線上的 sqlite 檔：BR 已記載
    `data/db/openshelf.sqlite` 的 work 表為 0 rows，拿它驗會得到
    「查無資料」而誤判成修好或修壞。
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE work_fts USING fts5("
        "title, authors_display, content, tokenize='trigram')"
    )
    conn.execute(
        "INSERT INTO work_fts(title, authors_display, content) VALUES (?, ?, ?)",
        (ROW_TITLE, ROW_AUTHORS, ROW_CONTENT),
    )
    conn.commit()
    yield conn
    conn.close()


def match_count(conn, user_query):
    """走與正式程式碼相同的建構層，回傳命中數。"""
    expr = build_fts_query(user_query)
    if expr is None:
        # 無可查詢的詞 -> 正式程式碼會走 `1 = 0`，等價於 0 筆
        return 0
    return conn.execute(
        "SELECT count(*) FROM work_fts WHERE work_fts MATCH ?", (expr,)
    ).fetchone()[0]


# --- 控制組：證明測試裝置本身是通的 -------------------------------------

def test_control_table_really_has_data(fts_conn):
    """若這條紅了，底下所有『回 0』的斷言都不具意義。"""
    assert fts_conn.execute("SELECT count(*) FROM work_fts").fetchone()[0] == 1


def test_control_publisher_lives_in_content_column(fts_conn):
    """釘住 fixture 的保真度：出版社字樣必須經由 content 欄可被檢索。

    這條同時證偽了 BR 對「缺陷 B」的描述——`work` 表並無 publisher 欄
    （`grep -n publisher app/db/schema.sql` rc=1，控制組 `title` 為 4 筆），
    出版社字樣本來就在已被索引的 content 內。
    """
    assert fts_conn.execute(
        "SELECT count(*) FROM work_fts WHERE work_fts MATCH ?", ('"Wiley"',)
    ).fetchone()[0] == 1


def test_control_raw_phrase_still_exhibits_the_bug(fts_conn):
    """釘住根因本身：舊實作的表達式在無標點、僅詞序不同時仍回 0。

    這條是「缺席態與失敗態不得共用同一個輸出」的守門員——
    它證明本檔量到的差異來自修復，而不是來自測試資料湊巧。
    """
    old_style = '"Concept Operating"'  # 舊實作：整串包成單一 phrase
    assert fts_conn.execute(
        "SELECT count(*) FROM work_fts WHERE work_fts MATCH ?", (old_style,)
    ).fetchone()[0] == 0


# --- 第 1 組：BR 控制組表的每一列 ---------------------------------------

@pytest.mark.parametrize(
    "user_query, why",
    [
        ("Operating System Concept", "基準：多詞、無標點"),
        ("Operating System Concept.", "只多一個句點（BR 第 7 列）"),
        ("Operating System Concept,Wiley", "逗號 + 未出現在 title 的詞"),
        ("Concept Operating", "純詞序顛倒，無任何標點 —— 判準關鍵"),
        ("Operating", "單一詞"),
        ("operating system concept", "大小寫不同"),
        ("Operating   System    Concept", "多重空白"),
        ("Operating System Concept, Wiley.", "混合標點"),
        (
            "Abraham Silberschatz, Peter Baer Galvin, Operating System Concept.",
            "使用者的真實書目貼上形狀（BR 第 1 列）",
        ),
    ],
)
def test_queries_with_punctuation_or_reordering_still_match(fts_conn, user_query, why):
    assert match_count(fts_conn, user_query) == 1, f"應命中：{why}"


# --- 第 2 組：負向（強制，不是選配） ------------------------------------

@pytest.mark.parametrize(
    "user_query",
    [
        "zzzzz_no_such_book_qqq",
        "Nonexistent Title That Is Definitely Absent",
        "Operating System Concept zzzzz_no_such_book_qqq",  # AND：一詞不中即不中
    ],
)
def test_absent_books_still_return_zero(fts_conn, user_query):
    """若這組紅了，代表修復把所有查詢都放寬成命中——那不是修好。"""
    assert match_count(fts_conn, user_query) == 0


def test_and_semantics_every_term_must_be_present(fts_conn):
    """明確釘住多詞語意為 AND 而非 OR。

    OR 會讓上面那條『一詞不中』的查詢命中，使
    「查無此書」與「查詢過寬」再度共用同一個輸出。
    """
    expr = build_fts_query("Operating Nonexistentword")
    assert expr is not None
    assert " AND " in expr
    assert match_count(fts_conn, "Operating Nonexistentword") == 0


def test_and_semantics_documented_tradeoff(fts_conn):
    """AND 的代價，明寫成斷言而非藏在註解裡。

    只要查詢中有**任一**詞在所有被索引欄位皆不存在，整串即回 0。
    此處以出版社為例：`Wiley` 存在於 content 故仍命中；
    但若換成真的不存在的出版社，AND 會讓整串歸零——
    這是精確度換來的已知取捨，不是回歸。
    """
    assert match_count(fts_conn, "Operating System Concept Wiley") == 1
    assert match_count(fts_conn, "Operating System Concept Packt") == 0


# --- 第 3 組：語法字元不得使查詢失敗 ------------------------------------

@pytest.mark.parametrize(
    "user_query",
    ['"', "(", ")", "*", "AND", "OR", "NOT", "NEAR", "C++", ".NET", "^", ":", "-",
     '"unterminated', "((()))", "* * *", "R&D", "O'Reilly", ""],
)
def test_syntax_characters_never_raise(fts_conn, user_query):
    """不得拋例外（在 API 層即 500）。回 0 筆是可接受的，炸掉不是。"""
    count = match_count(fts_conn, user_query)
    assert isinstance(count, int)
    assert count >= 0


def test_quote_is_escaped_not_injected():
    """使用者輸入的 `"` 必須被 escape 成 `""`，不得逃逸成 phrase 運算子。"""
    expr = build_fts_query('say "hello" now')
    assert expr is not None
    # 每個詞各自成 phrase，內部引號 escape 後仍是合法運算式
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
    conn.execute("SELECT count(*) FROM t WHERE t MATCH ?", (expr,)).fetchone()


def test_terms_shorter_than_trigram_are_dropped():
    """trigram 索引不含 <3 字元的詞，留著它們會讓 AND 必然歸零。"""
    # `C++` strip 後為 `C++`（3 字元）會保留；`of` 應被丟棄
    expr = build_fts_query("Operating of System")
    assert expr is not None
    assert '"of"' not in expr
    assert '"Operating"' in expr and '"System"' in expr


def test_query_with_only_unusable_terms_returns_none():
    """只打了語法字元 -> None，呼叫端必須據此回 0 筆而非回全庫。"""
    assert build_fts_query("*") is None
    assert build_fts_query("(") is None
    assert build_fts_query("") is None
