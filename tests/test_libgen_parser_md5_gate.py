"""md5 放行閘：無 md5 的 row 不得進入搜尋結果。

使用者裁示（2026-08-21）：「下載不到的書就不要顯示搜尋結果」。
關閉 BR-20260821_010000（改採相反方向）與 BR-20260821_030000（work_id 互撞）。

本檔每一組斷言都自帶控制組，理由寫在各測試的 docstring：
「這批 row 被過濾」「parser 整個壞掉回空 list」「fixture 欄數不對在欄數守衛就被丟掉」
三者在外部共用同一個輸出（空 list / 少一筆），必須靠正向控制組與 log 計數拆開。
"""

import logging

import pytest

from app.crawler.libgen_live import LibgenCrawler

PARSER_LOGGER = "app.crawler.libgen_live"

GOOD_MD5 = "8095d81a62e1a56e6c2133e41c20941b"
GOOD_MD5_B = "1111111111111111111111111111abcd"


# --------------------------------------------------------------------------
# fixture builders —— 欄數固定，避免落入 len(cols) 守衛而誤判為「新閘生效」
# li 適配器需 >= 9 欄（md5 來自 cols[8]）；is 適配器需 >= 10 欄（md5 來自 cols[9:]）
# --------------------------------------------------------------------------

def _li_row(title: str, mirror_cell: str) -> str:
    """li 適配器單列：恰好 9 個 <td>。"""
    return f"""
        <tr>
            <td><a href="/edition.php?id=1">{title}</a></td>
            <td>Silberschatz, Abraham</td>
            <td>Wiley</td>
            <td>1987</td>
            <td>English</td>
            <td>944</td>
            <td>25 Mb</td>
            <td>pdf</td>
            <td>{mirror_cell}</td>
        </tr>
    """


def _li_table(*rows: str) -> str:
    body = "".join(rows)
    return f"""
    <table id="tablelibgen">
        <tr><th>Title</th><th>Author</th><th>Publisher</th><th>Year</th><th>Language</th>
            <th>Pages</th><th>Size</th><th>Ext</th><th>Mirrors</th></tr>
        {body}
    </table>
    """


def _is_row(title: str, mirror_cell: str) -> str:
    """is 適配器單列：恰好 10 個 <td>（第 10 欄即 cols[9]）。"""
    return f"""
        <tr>
            <td>1001</td>
            <td>Knuth, Donald E.</td>
            <td><a href="book.php?id=1001"><b>{title}</b></a></td>
            <td>Addison-Wesley</td>
            <td>1997</td>
            <td>672</td>
            <td>English</td>
            <td>25 Mb</td>
            <td>pdf</td>
            <td>{mirror_cell}</td>
        </tr>
    """


def _is_table(*rows: str) -> str:
    body = "".join(rows)
    return f"""
    <table class="c">
        <tr><th>ID</th><th>Author(s)</th><th>Title</th><th>Publisher</th><th>Year</th>
            <th>Pages</th><th>Language</th><th>Size</th><th>Extension</th><th>Mirrors</th></tr>
        {body}
    </table>
    """


WITH_MD5 = f'<a href="/get.php?md5={GOOD_MD5}">get</a>'
WITH_MD5_B = f'<a href="/get.php?md5={GOOD_MD5_B}">get</a>'
# 有可用鏡像連結但 href 不含 32-hex —— 正是 BR-010000/030000 爭議的那一格
NO_MD5_BUT_LINKED = '<a href="/file.php?id=774851">file</a>'
NO_LINK_AT_ALL = "&nbsp;"


# --------------------------------------------------------------------------
# fixture 自我驗證：先證明 fixture 本身走得到新閘，而不是卡在欄數守衛
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "builder, row_builder, expected_cols",
    [(_li_table, _li_row, 9), (_is_table, _is_row, 10)],
)
def test_fixture_has_enough_columns_to_reach_the_md5_gate(builder, row_builder, expected_cols):
    """控制組（拆開「被新閘過濾」與「根本沒被 parser 看到」）。

    li 的 :331 `len(cols) < 9` 與 is 的 :411 `len(cols) < 10` 會在 md5 閘之前
    丟掉欄數不足的 row。若 fixture 欄數不對，後面所有「結果為空」的斷言都會
    在舊行為下也成立 —— 那時驗到的是欄數守衛，不是本次改動。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(builder(row_builder("T", WITH_MD5)), "html.parser")
    data_rows = soup.find_all("tr")[1:]
    assert len(data_rows) == 1
    assert len(data_rows[0].find_all("td")) == expected_cols


# --------------------------------------------------------------------------
# li 適配器
# --------------------------------------------------------------------------

def test_li_drops_row_without_md5_but_keeps_normal_row():
    """主斷言 + 正向控制組。

    正向控制組（`return []` 的實作會在此掛掉）：同一份 HTML 裡的正常 row
    必須照常產出，且欄位完整。缺了它，「過濾成功」與「parser 全壞」同一個輸出。
    """
    crawler = LibgenCrawler()
    html = _li_table(
        _li_row("Has Md5", WITH_MD5),
        _li_row("No Md5 But Has Mirror Link", NO_MD5_BUT_LINKED),
        _li_row("No Md5 No Link", NO_LINK_AT_ALL),
    )

    results = crawler._parse_libgen_li_html(html, "https://libgen.li")

    # 正向控制組：正常 row 照常產出（證明 parser 沒壞、沒回空 list）
    assert len(results) == 1, f"預期恰好保留 1 筆，實得 {len(results)}：{[r['title'] for r in results]}"
    kept = results[0]
    assert kept["title"] == "Has Md5"
    assert kept["md5"] == GOOD_MD5
    assert kept["work_id"] == f"libgen_{GOOD_MD5}"
    assert kept["mirror_links"] == [f"https://libgen.li/get.php?md5={GOOD_MD5}"]

    # 主斷言：無 md5 的兩筆都不在結果裡（標題非空也不行 —— 舊的 `and` 會放行）
    titles = {r["title"] for r in results}
    assert "No Md5 But Has Mirror Link" not in titles
    assert "No Md5 No Link" not in titles


def test_li_never_emits_empty_md5_or_colliding_work_id():
    """BR-20260821_030000 復發防護：不得再出現 work_id == "libgen_"。"""
    crawler = LibgenCrawler()
    html = _li_table(
        _li_row("Ghost A", NO_MD5_BUT_LINKED),
        _li_row("Ghost B", NO_MD5_BUT_LINKED),
        _li_row("Real", WITH_MD5),
    )

    results = crawler._parse_libgen_li_html(html, "https://libgen.li")

    assert len(results) == 1, "控制組：正常 row 必須存活，否則以下斷言在空 list 上恆真"
    assert all(r["md5"] for r in results)
    assert all(r["work_id"] != "libgen_" for r in results)
    work_ids = [r["work_id"] for r in results]
    assert len(work_ids) == len(set(work_ids)), "work_id 必須互不相同"


def test_li_two_valid_rows_get_distinct_work_ids():
    """控制組：兩筆各自有 md5 時必須都保留且 work_id 相異。

    若沒有這條，一個「只回第一筆」的壞實作也能通過上面的唯一性斷言。
    """
    crawler = LibgenCrawler()
    html = _li_table(_li_row("Book A", WITH_MD5), _li_row("Book B", WITH_MD5_B))

    results = crawler._parse_libgen_li_html(html, "https://libgen.li")

    assert len(results) == 2
    assert {r["work_id"] for r in results} == {f"libgen_{GOOD_MD5}", f"libgen_{GOOD_MD5_B}"}


def test_li_drop_is_logged_and_column_guard_drop_is_not(caplog):
    """拆開「被 md5 閘丟掉」與「被欄數守衛丟掉」——兩者都讓結果少一筆。

    只有 md5 閘會累計 dropped_no_md5 並在 debug log 留痕；欄數不足的 row 在更早
    的守衛被丟棄，不入計數。沒有這條，兩種丟棄在外部共用同一個輸出。
    """
    crawler = LibgenCrawler()

    with caplog.at_level(logging.DEBUG, logger=PARSER_LOGGER):
        caplog.clear()
        crawler._parse_libgen_li_html(
            _li_table(_li_row("Real", WITH_MD5), _li_row("Ghost", NO_MD5_BUT_LINKED)),
            "https://libgen.li",
        )
        md5_drop_records = [r for r in caplog.records if "no md5" in r.getMessage()]

        caplog.clear()
        short_row = "<tr><td>only</td><td>three</td><td>cols</td></tr>"
        crawler._parse_libgen_li_html(
            _li_table(_li_row("Real", WITH_MD5), short_row), "https://libgen.li"
        )
        short_row_records = [r for r in caplog.records if "no md5" in r.getMessage()]

    assert len(md5_drop_records) == 1, "md5 閘丟棄必須留痕"
    assert "dropped 1 row(s)" in md5_drop_records[0].getMessage()
    assert "libgen_li" in md5_drop_records[0].getMessage()
    assert short_row_records == [], "欄數守衛的丟棄不屬 md5 閘，不得混入同一計數"


def test_li_all_rows_missing_md5_yields_empty_but_logged(caplog):
    """全數無 md5 時結果為空 —— 但必須與「沒有任何 row」可區分。"""
    crawler = LibgenCrawler()

    with caplog.at_level(logging.DEBUG, logger=PARSER_LOGGER):
        caplog.clear()
        empty_res = crawler._parse_libgen_li_html(
            _li_table(_li_row("G1", NO_MD5_BUT_LINKED), _li_row("G2", NO_MD5_BUT_LINKED)),
            "https://libgen.li",
        )
        dropped_msgs = [r.getMessage() for r in caplog.records if "no md5" in r.getMessage()]

        caplog.clear()
        no_row_res = crawler._parse_libgen_li_html(_li_table(), "https://libgen.li")
        no_row_msgs = [r.getMessage() for r in caplog.records if "no md5" in r.getMessage()]

    assert empty_res == []
    assert no_row_res == []
    # 兩個空 list，靠 log 分辨成因
    assert len(dropped_msgs) == 1 and "dropped 2 row(s)" in dropped_msgs[0]
    assert no_row_msgs == [], "沒有任何 row 時不得謊報丟棄"


# --------------------------------------------------------------------------
# is 適配器（零實測樣本，fixture 依 :411/:428 的結構自行構造）
# --------------------------------------------------------------------------

def test_is_drops_row_without_md5_but_keeps_normal_row():
    """is 適配器同型檢查，含正向控制組。"""
    crawler = LibgenCrawler()
    html = _is_table(
        _is_row("Has Md5", f'<a href="http://library.lol/main/{GOOD_MD5}">lol</a>'),
        _is_row("No Md5 But Has Mirror Link", '<a href="http://library.lol/file.php?id=42">lol</a>'),
    )

    results = crawler._parse_libgen_is_html(html, "https://libgen.is")

    assert len(results) == 1, f"預期恰好保留 1 筆，實得 {len(results)}"
    kept = results[0]
    assert kept["title"] == "Has Md5"
    assert kept["md5"] == GOOD_MD5
    assert kept["work_id"] == f"libgen_{GOOD_MD5}"
    assert kept["size_bytes"] == 25 * 1024 * 1024  # 控制組：其他欄位未被改動波及


def test_is_never_emits_empty_md5_or_colliding_work_id():
    crawler = LibgenCrawler()
    html = _is_table(
        _is_row("Ghost A", '<a href="http://library.lol/file.php?id=1">a</a>'),
        _is_row("Ghost B", '<a href="http://library.lol/file.php?id=2">b</a>'),
        _is_row("Real", f'<a href="http://library.lol/main/{GOOD_MD5}">lol</a>'),
    )

    results = crawler._parse_libgen_is_html(html, "https://libgen.is")

    assert len(results) == 1, "控制組：正常 row 必須存活"
    assert all(r["md5"] for r in results)
    assert all(r["work_id"] != "libgen_" for r in results)


def test_is_two_valid_rows_get_distinct_work_ids():
    crawler = LibgenCrawler()
    html = _is_table(
        _is_row("A", f'<a href="http://library.lol/main/{GOOD_MD5}">a</a>'),
        _is_row("B", f'<a href="http://library.lol/main/{GOOD_MD5_B}">b</a>'),
    )

    results = crawler._parse_libgen_is_html(html, "https://libgen.is")

    assert len(results) == 2
    assert {r["work_id"] for r in results} == {f"libgen_{GOOD_MD5}", f"libgen_{GOOD_MD5_B}"}


def test_is_drop_is_logged_and_column_guard_drop_is_not(caplog):
    """is 適配器：md5 閘丟棄留痕；欄數不足（<10）的丟棄不入計數。"""
    crawler = LibgenCrawler()
    good = _is_row("Real", f'<a href="http://library.lol/main/{GOOD_MD5}">lol</a>')

    with caplog.at_level(logging.DEBUG, logger=PARSER_LOGGER):
        caplog.clear()
        crawler._parse_libgen_is_html(
            _is_table(good, _is_row("Ghost", '<a href="http://library.lol/file.php?id=9">g</a>')),
            "https://libgen.is",
        )
        md5_drop_records = [r for r in caplog.records if "no md5" in r.getMessage()]

        caplog.clear()
        short_row = "<tr><td>a</td><td>b</td><td>c</td></tr>"
        crawler._parse_libgen_is_html(_is_table(good, short_row), "https://libgen.is")
        short_row_records = [r for r in caplog.records if "no md5" in r.getMessage()]

    assert len(md5_drop_records) == 1
    assert "dropped 1 row(s)" in md5_drop_records[0].getMessage()
    assert "libgen_is" in md5_drop_records[0].getMessage()
    assert short_row_records == []


# --------------------------------------------------------------------------
# 下游契約：進得了結果的項目，一定走得完下載主鍵這一關
# --------------------------------------------------------------------------

@pytest.mark.parametrize("adapter", ["li", "is"])
def test_every_emitted_item_has_a_resolvable_download_key(adapter):
    """把 parser 的輸出與 mirror_resolver 的入口條件綁在一起。

    `mirror_resolver.resolve_download_url` 在 `not md5` 時直接回 None（:140）。
    本測試斷言 parser 不再產出任何會撞上該早退的項目 —— 這正是使用者裁示
    「下載不到的書就不要顯示搜尋結果」的機器可驗版本。
    """
    crawler = LibgenCrawler()
    if adapter == "li":
        html = _li_table(_li_row("Real", WITH_MD5), _li_row("Ghost", NO_MD5_BUT_LINKED))
        results = crawler._parse_libgen_li_html(html, "https://libgen.li")
    else:
        html = _is_table(
            _is_row("Real", f'<a href="http://library.lol/main/{GOOD_MD5}">lol</a>'),
            _is_row("Ghost", '<a href="http://library.lol/file.php?id=9">g</a>'),
        )
        results = crawler._parse_libgen_is_html(html, "https://libgen.is")

    assert results, "控制組：必須有存活項目，否則以下 all() 在空 list 上恆真"
    for item in results:
        md5 = (item["md5"] or "").strip().lower()
        assert md5, f"{item['title']} 沒有 md5，會在 mirror_resolver:140 早退"
        assert len(md5) == 32
