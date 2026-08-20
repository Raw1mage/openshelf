"""Phase 1 迴歸測試：資料模型 / Schema 遷移 / Torrent-Magnet 解析。

對應 plans/aggregator_torrent-p2p-integration Task 1.1 ~ 1.3。
Phase 2+（P2P 引擎、雙軌調度）不在本檔範圍。
"""
import sqlite3

import pytest

from app.crawler.libgen_live import LibgenCrawler
from app.models.catalog import (
    SearchResultItem,
    ManifestationCreate,
    ManifestationRead,
    DownloadJob,
)

TORRENT_FIELDS = ("torrent_url", "magnet_uri", "download_protocol", "peers_count")


# === Task 1.1: 資料模型擴充 ===
@pytest.mark.parametrize(
    "model_cls", [SearchResultItem, ManifestationCreate, ManifestationRead, DownloadJob]
)
def test_models_expose_torrent_fields(model_cls):
    fields = model_cls.model_fields
    missing = [f for f in TORRENT_FIELDS if f not in fields]
    assert missing == [], f"{model_cls.__name__} 缺少欄位: {missing}"


def test_torrent_field_defaults_are_unknown_not_zero():
    """peers_count 預設須為 None（未知），不得是 0（已查詢且確無 Peer）。"""
    item = SearchResultItem(work_id="w1", title="t")
    assert item.torrent_url is None
    assert item.magnet_uri is None
    assert item.download_protocol == "http"
    assert item.peers_count is None


# === Task 1.2: Schema 與 DAO 持久化 ===
def _legacy_db(tmp_path):
    """建立一個「舊版 schema」的 DB（無 torrent 欄位）並塞入既有資料。"""
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE work (
            work_id TEXT PRIMARY KEY, title TEXT NOT NULL,
            title_provenance TEXT NOT NULL DEFAULT 'filename_parsed',
            work_type TEXT NOT NULL DEFAULT 'unknown', language TEXT,
            publication_year INTEGER, authors_display TEXT,
            availability_tier INTEGER NOT NULL DEFAULT 0, relevance_authority REAL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, merged_into TEXT
        );
        CREATE TABLE manifestation (
            manifestation_id TEXT PRIMARY KEY, work_id TEXT NOT NULL,
            version TEXT DEFAULT 'unknown', format TEXT DEFAULT 'unknown',
            origin TEXT NOT NULL DEFAULT 'local', license_id TEXT,
            is_retrievable INTEGER NOT NULL DEFAULT 1, external_url TEXT
        );
        INSERT INTO work (work_id,title,title_provenance,work_type,availability_tier,created_at,updated_at)
             VALUES ('wk_legacy','Legacy Book','filename_parsed','book',0,'2026-01-01','2026-01-01');
        INSERT INTO manifestation (manifestation_id,work_id,version,format,origin,is_retrievable,external_url)
             VALUES ('mf_legacy','wk_legacy','v1','epub','external',1,'https://old.example/x.epub');
        """
    )
    conn.commit()
    conn.close()
    return path


def test_legacy_db_really_lacks_column_before_migration(tmp_path):
    """控制組：證明遷移前該欄位確實不存在，避免『通過』其實是假陽性。"""
    path = _legacy_db(tmp_path)
    conn = sqlite3.connect(str(path))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT magnet_uri FROM manifestation")
    conn.close()


def test_migration_adds_columns_and_preserves_existing_rows(tmp_path):
    from app.db.engine import DatabaseEngine
    from app.db.dao import CatalogDAO

    path = _legacy_db(tmp_path)
    CatalogDAO(engine=DatabaseEngine(db_path=str(path)))

    conn = sqlite3.connect(str(path))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(manifestation)")}
    assert set(TORRENT_FIELDS).issubset(cols)

    row = conn.execute(
        "SELECT external_url, magnet_uri, download_protocol "
        "FROM manifestation WHERE manifestation_id='mf_legacy'"
    ).fetchone()
    assert row == ("https://old.example/x.epub", None, "http")
    assert conn.execute("SELECT COUNT(*) FROM work").fetchone()[0] == 1
    conn.close()


def test_migration_is_idempotent(tmp_path):
    from app.db.engine import DatabaseEngine
    from app.db.dao import CatalogDAO

    path = _legacy_db(tmp_path)
    dao = CatalogDAO(engine=DatabaseEngine(db_path=str(path)))
    assert dao.apply_column_migrations() == []
    assert dao.apply_column_migrations() == []


def test_dao_roundtrip_torrent_source(tmp_path):
    from app.storage.manager import StorageManager
    from app.db.engine import DatabaseEngine
    from app.db.dao import CatalogDAO
    from app.models.catalog import WorkCreate

    storage = StorageManager(base_dir=tmp_path)
    dao = CatalogDAO(engine=DatabaseEngine(db_path=storage.get_db_path()))

    wid = dao.create_work(WorkCreate(title="P2P Book"))
    magnet = "magnet:?xt=urn:btih:3f99647c7584e05a0ab08155686a68e2&dn=book.epub"
    mid = dao.add_manifestation(
        work_id=wid,
        format_type="epub",
        origin="external",
        torrent_url="https://libgen.li/t/1.torrent",
        magnet_uri=magnet,
        download_protocol="torrent",
        peers_count=12,
    )

    sources = dao.get_torrent_sources_for_work(wid)
    assert len(sources) == 1
    assert sources[0]["magnet_uri"] == magnet
    assert sources[0]["download_protocol"] == "torrent"
    assert sources[0]["peers_count"] == 12

    assert dao.update_manifestation_torrent_source(mid, peers_count=30) is True
    assert dao.get_torrent_sources_for_work(wid)[0]["peers_count"] == 30

    detail = dao.get_work_detail(wid)
    assert detail.manifestations[0].magnet_uri == magnet


# === Task 1.3: Torrent / Magnet 解析 ===
def test_parse_magnet_uri_matches_plan_vector():
    """對應 plans/.../test-vectors.json 的 parse_magnet_uri 向量。"""
    got = LibgenCrawler.parse_magnet_uri(
        "magnet:?xt=urn:btih:3f99647c7584e05a0ab08155686a68e2"
        "&dn=book.epub&tr=udp://tracker.opentrackr.org:1337"
    )
    assert got == {
        "info_hash": "3f99647c7584e05a0ab08155686a68e2",
        "display_name": "book.epub",
        "trackers": ["udp://tracker.opentrackr.org:1337"],
    }


@pytest.mark.parametrize(
    "bad", ["", "https://libgen.li/x.torrent", "not-a-magnet", None]
)
def test_parse_magnet_uri_rejects_non_magnet(bad):
    """非 magnet 一律 info_hash=None，不與『magnet 但無 hash』共用輸出。"""
    assert LibgenCrawler.parse_magnet_uri(bad)["info_hash"] is None


LIBGEN_IS_ROW = """
<table class="c">
<tr><th>ID</th><th>Author(s)</th><th>Title</th><th>Publisher</th><th>Year</th>
<th>Pages</th><th>Language</th><th>Size</th><th>Extension</th><th>Mirrors</th></tr>
<tr>
<td>1001</td><td>Knuth, Donald E.</td>
<td><a href="book.php?id=1001"><b>TAOCP</b></a></td>
<td>Addison-Wesley</td><td>1997</td><td>672</td><td>English</td><td>25 Mb</td><td>pdf</td>
<td><a href="http://library.lol/main/8095d81a62e1a56e6c2133e41c20941b">lol</a></td>
{extra}
</tr></table>
"""

MAGNET_CELL = (
    '<td><a href="magnet:?xt=urn:btih:3f99647c7584e05a0ab08155686a68e2'
    '&amp;dn=taocp.pdf&amp;tr=udp://tracker.opentrackr.org:1337">magnet</a></td>'
    '<td><a href="/torrents/fiction/1001.torrent">torrent</a></td>'
)


def test_parse_libgen_is_extracts_magnet_and_torrent():
    crawler = LibgenCrawler()
    item = crawler._parse_libgen_is_html(
        LIBGEN_IS_ROW.format(extra=MAGNET_CELL), "https://libgen.is"
    )[0]
    assert item["magnet_uri"].startswith("magnet:?xt=urn:btih:")
    assert item["torrent_url"] == "https://libgen.is/torrents/fiction/1001.torrent"
    assert item["download_protocol"] == "torrent"
    # 既有欄位不得被破壞
    assert item["md5"] == "8095d81a62e1a56e6c2133e41c20941b"
    assert item["size_bytes"] == 25 * 1024 * 1024


def test_parse_libgen_is_without_torrent_stays_http():
    """控制組：無 P2P 來源時協定須維持 http，欄位為 None。"""
    crawler = LibgenCrawler()
    item = crawler._parse_libgen_is_html(
        LIBGEN_IS_ROW.format(extra=""), "https://libgen.is"
    )[0]
    assert item["magnet_uri"] is None
    assert item["torrent_url"] is None
    assert item["download_protocol"] == "http"


def test_parse_libgen_li_extracts_bare_text_magnet():
    """部分 libgen.li 版型以純文字呈現 magnet，需由保底路徑掃出。"""
    crawler = LibgenCrawler()
    html = """
    <table id="tablelibgen">
    <tr><th>Title</th><th>Author</th><th>Publisher</th><th>Year</th><th>Lang</th>
    <th>Pages</th><th>Size</th><th>Ext</th><th>Mirrors</th></tr>
    <tr>
    <td><a href="edition.php?id=9">Deep Learning</a></td><td>Goodfellow</td><td>MIT</td>
    <td>2016</td><td>English</td><td>800</td><td>15 Mb</td><td>pdf</td>
    <td><a href="/ads.php?md5=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">mirror</a>
        magnet:?xt=urn:btih:deadbeefcafebabe1234567890abcdef&amp;dn=dl.pdf</td>
    </tr></table>
    """
    item = crawler._parse_libgen_li_html(html, "https://libgen.li")[0]
    assert item["magnet_uri"] == (
        "magnet:?xt=urn:btih:deadbeefcafebabe1234567890abcdef&dn=dl.pdf"
    )
    assert item["download_protocol"] == "torrent"
