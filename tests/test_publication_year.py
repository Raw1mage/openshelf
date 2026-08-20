"""BR-20260820_130500 — 出版年份在解析層與寫入層各被靜默丟棄一次。

本檔的每一組斷言都刻意成對出現：**能解的必須解出正確值，真的沒有的必須仍是 None**。
只測前者的話，一個恆回 2020 的實作也會全綠——那正是本 BR 要修的病（缺席態與
失敗態共用同一個輸出），不能拿同一個病去驗它。
"""

import logging
import tempfile
import shutil
from pathlib import Path

import pytest

from app.crawler.libgen_live import LibgenCrawler
from app.pipeline.ingest import IngestionPipeline
from app.storage.manager import StorageManager
from app.db.engine import DatabaseEngine
from app.db.dao import CatalogDAO


# ---------------------------------------------------------------------------
# 缺陷 A — 公網 parser 的年份解析
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        # 正向：libgen 現行完整日期格式（本 BR 的直接病灶）
        ("1972 June 01", 1972),
        ("1989 March 04", 1989),
        ("2015 December 31", 2015),
        # 正向：舊版光禿禿四位數（修好後不得回歸）
        ("1987", 1987),
        ("2015", 2015),
        # 正向：前後空白
        ("  1998  ", 1998),
        # 負向：來源真的沒有 —— 必須仍是 None，不得為了變綠塞預設值
        ("", None),
        ("   ", None),
        ("n/a", None),
        ("N/A", None),
        ("-", None),
        ("unknown", None),
        (None, None),
        # 負向：有字串但取不出年份（版型變更 / 非預期格式）
        ("June", None),
        ("forthcoming", None),
        # 負向：不在合理年份區間的純數字，不得誤判為年份
        ("42", None),
        ("999", None),
        ("99999", None),
    ],
)
def test_parse_publication_year_positive_and_negative(raw, expected):
    assert LibgenCrawler.parse_publication_year(raw) == expected


def test_parse_publication_year_is_not_a_constant():
    """控制組：證明解析器真的在讀輸入，而不是恆回同一個值。"""
    values = {
        LibgenCrawler.parse_publication_year(s)
        for s in ("1972 June 01", "1987", "2015 December 31", "n/a")
    }
    assert values == {1972, 1987, 2015, None}


def test_unparseable_year_is_logged_but_missing_year_is_not(caplog):
    """驗收判準 2：解析失敗與來源真的沒有，必須在 log 層可區分。"""
    with caplog.at_level(logging.DEBUG, logger="app.crawler.libgen_live"):
        caplog.clear()
        assert LibgenCrawler.parse_publication_year("forthcoming") is None
        unparseable_records = [r for r in caplog.records if "forthcoming" in r.getMessage()]

        caplog.clear()
        assert LibgenCrawler.parse_publication_year("n/a") is None
        placeholder_records = list(caplog.records)

    # 有字串但取不出來 -> 留下帶原始字串的痕跡
    assert len(unparseable_records) == 1
    # 來源真的沒有 -> 不是異常，不該吵
    assert placeholder_records == []


def _libgen_is_html(year_cell: str) -> str:
    return f"""
    <table class="c">
        <tr>
            <th>ID</th><th>Author(s)</th><th>Title</th><th>Publisher</th><th>Year</th>
            <th>Pages</th><th>Language</th><th>Size</th><th>Extension</th><th>Mirrors</th>
        </tr>
        <tr>
            <td>1001</td>
            <td>Silberschatz, Abraham</td>
            <td><a href="book.php?id=1001"><b>Operating System Concepts</b></a></td>
            <td>Wiley</td>
            <td>{year_cell}</td>
            <td>944</td>
            <td>English</td>
            <td>25 Mb</td>
            <td>pdf</td>
            <td><a href="http://library.lol/main/8095d81a62e1a56e6c2133e41c20941b">library.lol</a></td>
        </tr>
    </table>
    """


@pytest.mark.parametrize(
    "year_cell, expected",
    [("1972 June 01", 1972), ("1989 March 04", 1989), ("1987", 1987), ("", None), ("n/a", None)],
)
def test_parse_libgen_is_html_year(year_cell, expected):
    """驗收判準 1：五種輸入逐一斷言，走真正的 HTML 解析路徑。"""
    crawler = LibgenCrawler()
    results = crawler._parse_libgen_is_html(_libgen_is_html(year_cell), "https://libgen.is")
    assert len(results) == 1, "控制組：這份 HTML 必須解得出恰好一筆，否則年份斷言無意義"
    assert results[0]["title"].startswith("Operating System Concepts")
    assert results[0]["publication_year"] == expected


def _libgen_li_html(year_cell: str) -> str:
    return f"""
    <table id="tablelibgen">
        <tr><th>Title</th><th>Author</th><th>Publisher</th><th>Year</th><th>Language</th>
            <th>Pages</th><th>Size</th><th>Ext</th><th>Mirrors</th></tr>
        <tr>
            <td><a href="/edition.php?id=1">Operating System Concepts</a></td>
            <td>Silberschatz, Abraham</td>
            <td>Wiley</td>
            <td>{year_cell}</td>
            <td>English</td>
            <td>944</td>
            <td>25 Mb</td>
            <td>pdf</td>
            <td><a href="/get.php?md5=8095d81a62e1a56e6c2133e41c20941b">get</a></td>
        </tr>
    </table>
    """


@pytest.mark.parametrize(
    "year_cell, expected",
    [("1972 June 01", 1972), ("1989 March 04", 1989), ("1987", 1987), ("", None), ("n/a", None)],
)
def test_parse_libgen_li_html_year(year_cell, expected):
    crawler = LibgenCrawler()
    results = crawler._parse_libgen_li_html(_libgen_li_html(year_cell), "https://libgen.li")
    assert len(results) == 1, "控制組：這份 HTML 必須解得出恰好一筆，否則年份斷言無意義"
    assert results[0]["title"].startswith("Operating System Concepts")
    assert results[0]["publication_year"] == expected


# ---------------------------------------------------------------------------
# 缺陷 B — 本地 ingest 的年份寫入
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        (1987, 1987),
        ("1987", 1987),
        ("1972 June 01", 1972),
        (None, None),
        ("", None),
        ("n/a", None),
        (42, None),
        (99999, None),
        (True, None),
    ],
)
def test_coerce_publication_year(raw, expected):
    assert IngestionPipeline._coerce_publication_year(raw) == expected


def _make_pipeline(temp_dir: str) -> IngestionPipeline:
    storage = StorageManager(base_dir=temp_dir)
    engine = DatabaseEngine(db_path=storage.get_db_path())
    dao = CatalogDAO(engine=engine)
    return IngestionPipeline(storage=storage, dao=dao)


def _sample_pdf(content: str) -> bytes:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 72), content, fontname="china-t")
    data = doc.tobytes()
    doc.close()
    return data


def test_process_file_persists_publication_year_and_keeps_none_when_absent():
    """驗收判準 3：上游給值時寫得進去、上游是 None 時仍寫 None 不炸。"""
    temp_dir = tempfile.mkdtemp()
    try:
        pipeline = _make_pipeline(temp_dir)

        # (a) 上游給值 -> 必須落庫
        with_year = Path(temp_dir) / "with_year.pdf"
        with_year.write_bytes(_sample_pdf("有年份的測試書籍內容。"))
        res_a = pipeline.process_file(
            with_year,
            {"title": "有年份的書", "authors_display": "Someone", "publication_year": "1972 June 01"},
        )
        detail_a = pipeline.dao.get_work_detail(res_a["work_id"])
        assert detail_a.publication_year == 1972

        # (b) 上游沒有 -> 必須仍是 None（不得塞預設值）
        no_year = Path(temp_dir) / "no_year.pdf"
        no_year.write_bytes(_sample_pdf("沒有年份的測試書籍內容，內容需不同以免去重。"))
        res_b = pipeline.process_file(
            no_year, {"title": "沒年份的書", "authors_display": "Someone"}
        )
        detail_b = pipeline.dao.get_work_detail(res_b["work_id"])
        assert detail_b.publication_year is None

        # 控制組：兩筆都真的入了庫且是不同的 work，否則上面兩條斷言可能只是巧合
        assert res_a["work_id"] != res_b["work_id"]
        assert detail_a.title == "有年份的書"
        assert detail_b.title == "沒年份的書"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_ingest_bytes_keeps_publication_year_none_without_metadata():
    """ingest_bytes 沒有年份來源時必須寫 None 而非炸掉或塞預設。"""
    temp_dir = tempfile.mkdtemp()
    try:
        pipeline = _make_pipeline(temp_dir)
        detail = pipeline.ingest_bytes(
            data=_sample_pdf("上傳路徑的測試書籍內容。"),
            filename="上傳測試.pdf",
            custom_title="上傳測試書",
            custom_author="Uploader",
        )
        assert detail.publication_year is None
        # 控制組：這筆確實走完了入庫流程
        assert detail.title == "上傳測試書"
        assert detail.work_id is not None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
