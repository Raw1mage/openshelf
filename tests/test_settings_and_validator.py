import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

from app.main import app
from app.db.engine import DatabaseEngine
from app.db.dao import CatalogDAO, DEFAULT_LIBGEN_MIRRORS
from app.crawler.validator import MirrorValidator
from app.crawler.libgen_live import LibgenCrawler
from app.crawler.mirror_resolver import MirrorResolver


SAMPLE_LIBGEN_LI_HTML = """
<!DOCTYPE html>
<html>
<body>
<table id="tablelibgen">
  <thead><tr><th>Title</th><th>Authors</th><th>Publisher</th><th>Year</th><th>Lang</th><th>Pages</th><th>Size</th><th>Ext</th><th>Mirrors</th></tr></thead>
  <tbody>
    <tr>
      <td><a href="/book/index.php?md5=3b50c0e86b2de48805f190e210ca385a">Fluent Python: Clear, Concise, and Effective Programming</a></td>
      <td>Luciano Ramalho</td>
      <td>O'Reilly Media</td>
      <td>2021</td>
      <td>English</td>
      <td>1012</td>
      <td>15.2 Mb</td>
      <td>epub</td>
      <td><a href="ads.php?md5=3b50c0e86b2de48805f190e210ca385a">GET</a></td>
    </tr>
  </tbody>
</table>
</body>
</html>
"""

SAMPLE_LIBGEN_IS_HTML = """
<!DOCTYPE html>
<html>
<body>
<table>
  <tr><th>ID</th><th>Author</th><th>Title</th><th>Publisher</th><th>Year</th><th>Pages</th><th>Language</th><th>Size</th><th>Extension</th><th>Mirrors</th></tr>
  <tr>
    <td>1001</td>
    <td>Mark Lutz</td>
    <td><a href="book/index.php?md5=7d5a8df2d2b51ad889158c3a9d9b4b0e">Learning Python</a></td>
    <td>O'Reilly Media</td>
    <td>2013</td>
    <td>1600</td>
    <td>English</td>
    <td>22.5 Mb</td>
    <td>pdf</td>
    <td><a href="http://library.lol/main/7d5a8df2d2b51ad889158c3a9d9b4b0e">Mirror 1</a></td>
  </tr>
</table>
</body>
</html>
"""

SAMPLE_UNKNOWN_HTML = """
<!DOCTYPE html>
<html>
<head><title>Cloudflare Anti-DDoS Challenge</title></head>
<body>
<div>Please complete the security check to continue...</div>
</body>
</html>
"""


@pytest.fixture
def test_dao(tmp_path):
    db_file = tmp_path / "test_catalog.db"
    engine = DatabaseEngine(db_path=str(db_file))
    engine.init_database()
    return CatalogDAO(engine)


def test_dao_libgen_mirrors_lifecycle(test_dao):
    # 1. 預設鏡像清單驗證
    mirrors = test_dao.get_libgen_mirrors()
    assert len(mirrors) == len(DEFAULT_LIBGEN_MIRRORS)
    assert any(m["url"] == "https://libgen.li" for m in mirrors)

    # 2. 儲存自訂鏡像
    custom_mirrors = [
        {"url": "https://custom.libgen.internal", "enabled": True, "note": "私有節點", "validation_status": "verified", "priority": 1},
        {"url": "https://broken.libgen.internal", "enabled": True, "note": "壞掉節點", "validation_status": "incompatible_layout", "priority": 2},
        {"url": "https://offline.libgen.internal", "enabled": True, "note": "離線節點", "validation_status": "offline", "priority": 3},
        {"url": "https://unverified.libgen.internal", "enabled": True, "note": "未驗證節點", "validation_status": "unverified", "priority": 4},
        {"url": "https://disabled.libgen.internal", "enabled": False, "note": "停用節點", "validation_status": "verified", "priority": 5},
    ]
    test_dao.save_libgen_mirrors(custom_mirrors)

    loaded = test_dao.get_libgen_mirrors()
    assert len(loaded) == 5

    # 3. 測試 active 鏡像過濾（只有 enabled=True 且 validation_status="verified" 者能參與系統運作）
    active_urls = test_dao.get_active_libgen_mirror_urls()
    assert active_urls == ["https://custom.libgen.internal"]

    # 4. 恢復預設
    defaults = test_dao.reset_libgen_mirrors()
    assert len(defaults) == len(DEFAULT_LIBGEN_MIRRORS)


@pytest.mark.asyncio
async def test_validator_libgen_li_success(tmp_path):
    validator = MirrorValidator(issues_dir=tmp_path / "issues")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = SAMPLE_LIBGEN_LI_HTML

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        report = await validator.validate_mirror("https://libgen.li", auto_dispatch_br=True)

    assert report.validation_status == "verified"
    assert report.adapter_type == "libgen_li"
    assert report.sample_records_count == 1
    assert report.dispatched_br is False


@pytest.mark.asyncio
async def test_validator_libgen_is_success(tmp_path):
    validator = MirrorValidator(issues_dir=tmp_path / "issues")

    # 第一次 (li_search_url) 404，第二次 (is_search_url) 200
    async def mock_get(url, *args, **kwargs):
        resp = MagicMock()
        if "search.php" in url:
            resp.status_code = 200
            resp.text = SAMPLE_LIBGEN_IS_HTML
        else:
            resp.status_code = 404
            resp.text = "Not Found"
        return resp

    with patch("httpx.AsyncClient.get", side_effect=mock_get):
        report = await validator.validate_mirror("https://libgen.is", auto_dispatch_br=True)

    assert report.validation_status == "verified"
    assert report.adapter_type == "libgen_is"
    assert report.sample_records_count == 1
    assert report.dispatched_br is False


@pytest.mark.asyncio
async def test_validator_incompatible_layout_auto_dispatch_br(tmp_path):
    issues_dir = tmp_path / "issues"
    validator = MirrorValidator(issues_dir=issues_dir)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = SAMPLE_UNKNOWN_HTML

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        report = await validator.validate_mirror("https://new-unsupported-mirror.org", auto_dispatch_br=True)

    assert report.validation_status == "incompatible_layout"
    assert report.adapter_type == "unknown"
    assert report.dispatched_br is True
    assert report.br_id is not None
    assert report.br_path is not None

    # 驗證 BR 檔案確實已寫入磁碟且包含 DOM 片段與診斷資訊
    br_file = Path(report.br_path)
    assert br_file.exists()
    content = br_file.read_text(encoding="utf-8")
    assert "https://new-unsupported-mirror.org" in content
    assert "Cloudflare Anti-DDoS Challenge" in content
    assert "incompatible_layout" in content


def test_settings_api_endpoints(test_dao, tmp_path):
    from app.api.settings_routes import get_dao, get_validator
    issues_dir = tmp_path / "issues"
    validator = MirrorValidator(issues_dir=issues_dir)

    app.dependency_overrides[get_dao] = lambda: test_dao
    app.dependency_overrides[get_validator] = lambda: validator

    try:
        client = TestClient(app)

        # 1. 取得鏡像清單
        res = client.get("/api/settings/libgen-mirrors")
        assert res.status_code == 200
        mirrors = res.json()
        assert len(mirrors) >= 8

        # 2. 恢復預設鏡像清單
        res_reset = client.post("/api/settings/libgen-mirrors/reset")
        assert res_reset.status_code == 200

        # 3. 測試問題列表 API
        res_issues = client.get("/api/settings/libgen-mirrors/issues")
        assert res_issues.status_code == 200
        assert "issues" in res_issues.json()
    finally:
        app.dependency_overrides.clear()
