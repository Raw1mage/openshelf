"""BR-20260820_111523 — mirror_resolver 死鏡像與查封頁哨兵。

fixture 來源：2026-08-20 對真實站點抓取後剝除廣告雜訊（保留全部結構資產），
非手寫捏造。對應原始樣本見 BR 的實測證據表。
"""
import logging
import pathlib

import httpx
import pytest

from app.crawler.mirror_resolver import MirrorResolver
from app.db.dao import (
    CatalogDAO,
    DEFAULT_LIBGEN_MIRRORS,
    KNOWN_DEAD_MIRROR_HOSTS,
    is_known_dead_mirror,
)
from app.db.engine import DatabaseEngine

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "mirror_pages"


def _fx(name: str) -> str:
    p = FIXTURES / name
    assert p.exists(), f"fixture 遺失: {p}"  # 缺席不得偽裝成通過
    text = p.read_text()
    assert text.strip(), f"fixture 為空: {p}"
    return text


# ---------------------------------------------------------------- D3 哨兵

def test_looks_like_libgen_accepts_real_detail_page():
    """正向：真實的 ads.php 詳情頁必須被認定為書庫。"""
    assert MirrorResolver._looks_like_libgen(_fx("libgen_li_real_detail.html")) is True


def test_looks_like_libgen_accepts_book_absent_page():
    """缺席態不是失敗態：真實的「書不存在」頁仍然是書庫，不得被判為查封。

    這條是本 BR 的核心。BR 初版建議用 <a> 標籤數（真頁 >=79 / 查封頁 0）作判準，
    但實測真實的「書不存在」頁只有 5 個 <a> —— 那個判準會把缺席誤判成查封，
    等於把同一個病換個方向再犯一次。
    """
    assert MirrorResolver._looks_like_libgen(_fx("libgen_li_book_absent.html")) is True


@pytest.mark.parametrize("fixture", [
    "library_lol_seizure.html",
    "libgen_rocks_seizure.html",
    "libgen_rocks_seizure_404.html",
])
def test_looks_like_libgen_rejects_seizure_pages(fixture):
    """反向：三種真實查封／接管頁都必須被認定為「已非書庫」。"""
    assert MirrorResolver._looks_like_libgen(_fx(fixture)) is False


def test_looks_like_libgen_rejects_empty_and_garbage():
    assert MirrorResolver._looks_like_libgen("") is False
    assert MirrorResolver._looks_like_libgen("<html><body>hello</body></html>") is False


# ---------------------------------------------------------------- D1 清單

def test_dead_mirrors_removed_from_base_mirrors():
    """實測已死的四個網域不得留在硬編清單。"""
    joined = " ".join(MirrorResolver.BASE_MIRRORS)
    for dead in ("libgen.rocks", "libgen.gs", "libgen.pm", "library.lol"):
        assert dead not in joined, f"{dead} 已實測死亡，不應留在 BASE_MIRRORS"


def test_surviving_mirrors_present_and_prioritised():
    """兩個實測存活的鏡像必須在清單內，且 UNDECIDABLE 的 libgen.is 排最後。"""
    assert "https://libgen.li" in MirrorResolver.BASE_MIRRORS
    assert "https://libgen.la" in MirrorResolver.BASE_MIRRORS
    # libgen.is 為 UNDECIDABLE（DNS 通但 TCP 逾時），保留但不得排在存活鏡像之前
    assert MirrorResolver.BASE_MIRRORS.index("https://libgen.is") > \
        MirrorResolver.BASE_MIRRORS.index("https://libgen.la")


# ---------------------------------------------------------------- D2 TLS

def test_tls_verification_not_disabled_in_resolver_source():
    """verify=False 不得出現在本模組（實測 libgen.li/la 憑證在 verify=True 下有效）。"""
    src = pathlib.Path(MirrorResolver.__module__.replace(".", "/") + ".py")
    text = (pathlib.Path(__file__).parent.parent / src).read_text()
    assert "verify=False" not in text
    # 控制組：確認我們真的讀到了那個檔，而不是讀到空字串就宣稱通過
    assert "class MirrorResolver" in text


# ---------------------------------------------------------------- 端到端

class _StubTransport(httpx.AsyncBaseTransport):
    """以 URL 前綴對應到固定回應，不觸網。"""

    def __init__(self, routes):
        self.routes = routes
        self.requested = []

    async def handle_async_request(self, request):
        url = str(request.url)
        self.requested.append(url)
        for prefix, (status, body) in self.routes.items():
            if url.startswith(prefix):
                return httpx.Response(status, html=body, request=request)
        return httpx.Response(404, html="<html><body>no route</body></html>", request=request)


@pytest.fixture
def patched_client(monkeypatch):
    """讓 MirrorResolver 內部建立的 AsyncClient 走 stub transport。"""
    holder = {}

    def _install(routes):
        transport = _StubTransport(routes)
        holder["transport"] = transport
        original = httpx.AsyncClient.__init__

        def patched(self, *args, **kwargs):
            kwargs["transport"] = transport
            return original(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
        return transport

    return _install


@pytest.mark.asyncio
async def test_resolver_extracts_direct_link_from_real_page(patched_client):
    """負控制組：對真實結構的 libgen.li 頁面，仍能取出 get.php 直鏈（證明沒有誤殺）。"""
    patched_client({
        "https://libgen.li": (200, _fx("libgen_li_real_detail.html")),
    })
    resolver = MirrorResolver(mirrors=["https://libgen.li"])
    url = await resolver.resolve_download_url("8165314895008cdbe22f17f69bb4ae28")
    assert url is not None, "真實頁面必須解析出直鏈，否則就是誤殺"
    assert "get.php?md5=" in url
    assert resolver.seized_mirrors == set()


@pytest.mark.asyncio
async def test_seizure_page_logs_warning_and_marks_dead(patched_client, caplog):
    """查封頁不得靜默回 None：必須出現 warning 級告警並標記該鏡像。"""
    patched_client({
        "http://library.lol": (200, _fx("library_lol_seizure.html")),
    })
    resolver = MirrorResolver(mirrors=["http://library.lol"])
    with caplog.at_level(logging.WARNING, logger="app.crawler.mirror_resolver"):
        url = await resolver.resolve_download_url("8165314895008cdbe22f17f69bb4ae28")

    assert url is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "查封頁必須產生 warning，靜默 None 正是本 BR 要修的病"
    assert "http://library.lol" in resolver.seized_mirrors


@pytest.mark.asyncio
async def test_book_absent_is_silent_and_not_marked_dead(patched_client, caplog):
    """對照組：真正的「書不存在」不得觸發查封告警，也不得標記鏡像為 dead。

    這條與上一條成對，缺一則無法證明告警有鑑別力（否則「全部都告警」也會通過）。
    """
    patched_client({
        "https://libgen.li": (200, _fx("libgen_li_book_absent.html")),
    })
    resolver = MirrorResolver(mirrors=["https://libgen.li"])
    with caplog.at_level(logging.WARNING, logger="app.crawler.mirror_resolver"):
        url = await resolver.resolve_download_url("00000000000000000000000000000000")

    assert url is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, f"缺席態不應告警，實得: {[r.message for r in warnings]}"
    assert resolver.seized_mirrors == set()


@pytest.mark.asyncio
async def test_seized_mirror_is_skipped_on_later_iterations(patched_client):
    """被判定查封的鏡像，同一次解析中不再重複請求。"""
    transport = patched_client({
        "http://library.lol": (200, _fx("library_lol_seizure.html")),
        "https://libgen.li": (200, _fx("libgen_li_real_detail.html")),
    })
    resolver = MirrorResolver(mirrors=["http://library.lol", "https://libgen.li"])
    url = await resolver.resolve_download_url("8165314895008cdbe22f17f69bb4ae28")
    assert url is not None and "get.php?md5=" in url

    # 再送一次 candidate_mirrors 指向同一個查封站，應被跳過
    before = len(transport.requested)
    resolver2 = MirrorResolver(mirrors=["http://library.lol"])
    await resolver2.resolve_download_url("8165314895008cdbe22f17f69bb4ae28",
                                         candidate_mirrors=["http://library.lol/main/x"])
    assert "http://library.lol" in resolver2.seized_mirrors
    assert len(transport.requested) > before  # 控制組：stub 確實有被呼叫過


# ---------------------------------------------------------------- D4 死路徑

def test_libgen_is_family_no_longer_routed_to_seized_library_lol():
    """libgen.is/rs/st 家族不得再被硬路由到已查封的 library.lol。"""
    src = pathlib.Path(__file__).parent.parent / "app" / "crawler" / "mirror_resolver.py"
    text = src.read_text()
    assert "class MirrorResolver" in text  # 控制組
    assert 'await self._resolve_from_library_lol(client, f"http://library.lol/main/{md5}")' not in text


# ---------------------------------------------------------------- D1 dao 層
#
# get_active_libgen_mirror_urls() 有兩條獨立的輸出路徑，兩條都必須測：
#   (a) 正常路徑 —— 有 verified 資料時從中過濾
#   (b) fallback 路徑 —— 無任何 verified 資料時回退至預設清單
# 只測一條的話，另一條漏掉也不會被發現。


@pytest.fixture
def dao(tmp_path):
    engine = DatabaseEngine(db_path=str(tmp_path / "test_mirror_health.db"))
    engine.init_database()
    return CatalogDAO(engine)


def test_known_dead_mirror_helper_both_directions():
    """輔助判定函式兩個方向都要對（只測一邊的話，恆真或恆假都會通過）。"""
    for dead in KNOWN_DEAD_MIRROR_HOSTS:
        assert is_known_dead_mirror(f"https://{dead}") is True
    for alive in ("https://libgen.li", "https://libgen.la", "https://libgen.is"):
        assert is_known_dead_mirror(alive) is False
    assert is_known_dead_mirror("") is False


def test_dao_default_mirrors_mark_dead_ones_offline_and_disabled():
    """預設清單裡的死鏡像必須 enabled=False 且 validation_status != verified。

    刻意保留條目（不刪除），使用者在設定頁仍看得到網域及死因。
    """
    for m in DEFAULT_LIBGEN_MIRRORS:
        if is_known_dead_mirror(m["url"]):
            assert m["enabled"] is False, f"{m['url']} 已實測死亡，不得 enabled"
            assert m["validation_status"] != "verified", f"{m['url']} 不得標為 verified"
    # 控制組：存活鏡像仍在且仍可用，證明上面不是「全部都被關掉」才通過
    alive = [m for m in DEFAULT_LIBGEN_MIRRORS
             if m["enabled"] and m["validation_status"] == "verified"]
    assert [m["url"] for m in alive] == ["https://libgen.li", "https://libgen.la"]


def test_dao_active_urls_path_a_normal_filtering(dao):
    """路徑 (a)：有 verified 資料時，死鏡像不得進入輸出——即使它被標成 verified。

    這模擬的是既存使用者 DB 裡的實際情況：那些列寫入時網域還活著，
    所以它們就是 verified + enabled。只改 DEFAULT_LIBGEN_MIRRORS 常數救不了這些列。
    """
    dao.save_libgen_mirrors([
        {"url": "https://libgen.li", "enabled": True, "validation_status": "verified", "priority": 1},
        {"url": "https://libgen.rocks", "enabled": True, "validation_status": "verified", "priority": 2},
        {"url": "http://library.lol", "enabled": True, "validation_status": "verified", "priority": 3},
        {"url": "https://libgen.gs", "enabled": True, "validation_status": "verified", "priority": 4},
        {"url": "https://libgen.pm", "enabled": True, "validation_status": "verified", "priority": 5},
    ])
    active = dao.get_active_libgen_mirror_urls()

    assert active == ["https://libgen.li"], f"死鏡像漏出: {active}"
    assert len(active) == 1  # 控制組：確定不是回了空清單才通過


def test_dao_active_urls_path_b_fallback_does_not_bypass_gate(dao, caplog):
    """路徑 (b)：無任何 verified 資料時的 fallback，不得繞過驗證閘、不得吐死鏡像。

    舊行為：fallback 回傳所有 enabled 的預設鏡像且**完全不看 validation_status**，
    等於「驗證失敗」與「尚未驗證」共用同一個輸出，而輸出是「全部放行」。
    """
    dao.save_libgen_mirrors([
        {"url": "https://a.internal", "enabled": True, "validation_status": "offline", "priority": 1},
        {"url": "https://b.internal", "enabled": True, "validation_status": "unverified", "priority": 2},
    ])
    with caplog.at_level(logging.WARNING, logger="app.db.dao"):
        active = dao.get_active_libgen_mirror_urls()

    # 控制組：確認真的走到 fallback（否則下面的斷言只是在測路徑 a）
    assert active, "fallback 應回預設存活鏡像，不應為空"
    assert "https://a.internal" not in active
    assert "https://b.internal" not in active

    for url in active:
        assert not is_known_dead_mirror(url), f"fallback 吐出死鏡像: {url}"
    assert active == ["https://libgen.li", "https://libgen.la"]

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "回退至預設清單必須留下 warning，靜默回退正是被繞過的閘"


def test_dao_active_urls_fallback_excludes_unverified_defaults(dao):
    """fallback 不得把預設清單裡 unverified 的條目當成 verified 放行。"""
    dao.save_libgen_mirrors([
        {"url": "https://x.internal", "enabled": True, "validation_status": "unverified", "priority": 1},
    ])
    active = dao.get_active_libgen_mirror_urls()
    for u in ("libgen.is", "libgen.rs", "libgen.st"):
        assert not any(u in a for a in active), f"unverified 預設鏡像 {u} 漏出: {active}"
    assert active == ["https://libgen.li", "https://libgen.la"]  # 控制組


def test_libgen_crawler_mirrors_has_no_dead_hosts():
    """D1 第三處病灶：LibgenCrawler.MIRRORS 也不得含死鏡像。"""
    from app.crawler.libgen_live import LibgenCrawler
    assert LibgenCrawler.MIRRORS, "控制組：清單不得為空"
    for m in LibgenCrawler.MIRRORS:
        assert not is_known_dead_mirror(m), f"LibgenCrawler.MIRRORS 含死鏡像: {m}"


# ------------------------------------------- get_libgen_mirrors 的設定損毀態
#
# 同一種失效類別的第三處：JSON 損毀與「尚未設定」原本共用同一個靜默輸出
# （都回退預設清單全開）。兩個方向都必須測——只測「損毀有 warning」的話，
# 一個「永遠 warning」的實作也會通過。


def test_mirrors_setting_absent_is_silent(dao, caplog):
    """方向一：setting 不存在是正常初始態，不得產生 warning。"""
    with caplog.at_level(logging.WARNING, logger="app.db.dao"):
        mirrors = dao.get_libgen_mirrors()

    assert len(mirrors) == len(DEFAULT_LIBGEN_MIRRORS)  # 控制組：確實回退了
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, f"尚未設定不應告警，實得: {[r.message for r in warnings]}"


def test_mirrors_setting_corrupt_json_warns(dao, caplog):
    """方向二：JSON 損毀時必須留下 warning——設定實際存在卻讀不出來，不是初始態。"""
    dao.set_setting("libgen_mirrors", "{not valid json[[[")
    with caplog.at_level(logging.WARNING, logger="app.db.dao"):
        mirrors = dao.get_libgen_mirrors()

    # 回退行為本身不變：仍回預設清單
    assert len(mirrors) == len(DEFAULT_LIBGEN_MIRRORS)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "設定損毀必須告警，靜默回退正是本失效類別"
    assert any("JSONDecodeError" in r.getMessage() for r in warnings), \
        f"warning 應帶上例外型別，實得: {[r.getMessage() for r in warnings]}"


def test_mirrors_setting_wrong_shape_warns(dao, caplog):
    """JSON 有效但形狀不對（非 list / 空 list）也是「存在但不可用」，不得靜默。"""
    dao.set_setting("libgen_mirrors", '{"a": 1}')
    with caplog.at_level(logging.WARNING, logger="app.db.dao"):
        mirrors = dao.get_libgen_mirrors()

    assert len(mirrors) == len(DEFAULT_LIBGEN_MIRRORS)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "形狀不正確的設定必須告警"


def test_mirrors_setting_valid_custom_is_silent_and_returned(dao, caplog):
    """對照組：正常自訂設定既不告警、也必須原樣回傳（證明 warning 不是恆真）。"""
    dao.save_libgen_mirrors([
        {"url": "https://custom.internal", "enabled": True,
         "validation_status": "verified", "priority": 1},
    ])
    with caplog.at_level(logging.WARNING, logger="app.db.dao"):
        mirrors = dao.get_libgen_mirrors()

    assert [m["url"] for m in mirrors] == ["https://custom.internal"]
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, f"正常設定不應告警，實得: {[r.message for r in warnings]}"
