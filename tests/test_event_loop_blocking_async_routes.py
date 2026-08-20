"""BR-20260820_210000 A/B 節回歸測試 —— `async def` 路徑不得在事件迴圈執行緒上做同步 I/O。

判準刻意不用「grep 原始碼有無 to_thread」（那是缺席態與失敗態共用輸出的形狀），
改成**行為判準**：在 async 情境下呼叫，斷言同步 I/O 確實發生在**非事件迴圈執行緒**上。
每個測試都附控制組，證明偵測器在該壞掉時真的會失敗。
"""
import asyncio
import threading

import pytest

from app.crawler.libgen_live import LibgenCrawler


# --------------------------------------------------------------------------
# A 節：crawler.search 內部的同步 SQLite 讀與 BeautifulSoup 解析
# --------------------------------------------------------------------------

class _RecordingDAO:
    """記錄 get_active_libgen_mirror_urls 是在哪個執行緒被呼叫的。"""

    def __init__(self):
        self.calls = 0
        self.thread_ids = []

    def get_active_libgen_mirror_urls(self, adapter_types=None):
        self.calls += 1
        self.thread_ids.append(threading.get_ident())
        return ["https://libgen.li"]


@pytest.mark.asyncio
async def test_active_mirrors_db_read_leaves_event_loop_thread():
    """A 節：DB 讀必須發生在非事件迴圈執行緒。

    控制組（同一個測試內）：同步 property 直接呼叫時**必定**落在 loop 執行緒上——
    若兩者的執行緒 id 相同，代表 to_thread 沒生效，斷言會抓到。
    """
    dao = _RecordingDAO()
    crawler = LibgenCrawler(dao=dao)
    loop_thread_id = threading.get_ident()

    # 控制組：同步路徑 —— 證明偵測器（比對 thread id）真的能區分兩種情況
    _ = crawler.active_mirrors
    assert dao.calls == 1
    assert dao.thread_ids[-1] == loop_thread_id, (
        "控制組失效：同步 property 竟不在 loop 執行緒上，偵測器無鑑別力"
    )

    # 受測路徑：async 版必須換執行緒
    mirrors = await crawler._resolve_active_mirrors_async()
    assert mirrors == ["https://libgen.li"]
    assert dao.calls == 2, "DB 讀路徑必須被實際執行到（不得靠快取規避）"
    assert dao.thread_ids[-1] != loop_thread_id, (
        "A 節未修復：active_mirrors 的同步 SQLite 讀仍在事件迴圈執行緒上"
    )


@pytest.mark.asyncio
async def test_execute_single_search_offloads_db_and_parse():
    """A 節整條鏈：_execute_single_search 期間，DB 讀與 HTML 解析都不得在 loop 執行緒。"""
    dao = _RecordingDAO()
    crawler = LibgenCrawler(dao=dao)
    loop_thread_id = threading.get_ident()

    parse_threads = []
    original_parse = crawler._parse_libgen_li_html

    def _spy_parse(html, base_url):
        parse_threads.append(threading.get_ident())
        return original_parse(html, base_url)

    crawler._parse_libgen_li_html = _spy_parse

    html = (
        "<table id='tablelibgen'><tr><th>Title</th></tr><tr>"
        "<td><a href='/edition.php?id=1'>Some Book</a></td><td>Author</td><td>Pub</td>"
        "<td>2011</td><td>English</td><td>100</td><td>1.0 Mb</td><td>pdf</td>"
        "<td><a href='/ads.php?md5=%s'>[1]</a></td>" % ("a" * 32) +
        "</tr></table>"
    )

    class _Resp:
        status_code = 200
        text = html

    class _Client:
        async def get(self, url):
            return _Resp()

    results = await crawler._execute_single_search(_Client(), "anything", 25)

    assert results, "解析必須真的產出結果，否則下面的執行緒斷言是空轉"
    assert dao.calls == 1, "鏡像清單的 DB 讀路徑必須被實際走到"
    assert dao.thread_ids[-1] != loop_thread_id, (
        "A 節未修復：_execute_single_search 的 DB 讀仍在事件迴圈執行緒上"
    )
    assert parse_threads, "控制組失效：解析根本沒被呼叫"
    assert parse_threads[-1] != loop_thread_id, (
        "A 節未修復：BeautifulSoup 解析仍在事件迴圈執行緒上"
    )


# --------------------------------------------------------------------------
# B 節：category_routes 的本地落地檢查改批次 + 走 threadpool
# --------------------------------------------------------------------------

class _CountingDAO:
    """category route 需要的最小 DAO 介面，計數兩種查法。"""

    def __init__(self, landed):
        self.landed = landed
        self.per_item_calls = 0
        self.batch_calls = 0
        self.batch_thread_ids = []

    # -- category 相關 --
    def get_category(self, category_id):
        from app.models.catalog import CategoryRead
        return CategoryRead(
            category_id=category_id, name="Test", slug="test", parent_id=None,
        )

    def get_category_works(self, category_id, page=1, page_size=20):
        return 0, []

    # -- hash 查詢 --
    def find_work_by_hash(self, hash_val):
        self.per_item_calls += 1
        return self.landed.get(hash_val)

    def find_works_by_hashes(self, hash_vals):
        self.batch_calls += 1
        self.batch_thread_ids.append(threading.get_ident())
        return {h: self.landed[h] for h in hash_vals if h in self.landed}


class _StubCrawler:
    def __init__(self, items):
        self.items = items

    async def search(self, query, max_results=25):
        return self.items


@pytest.mark.asyncio
async def test_category_works_uses_batch_lookup_not_per_item():
    """B 節：15 筆雲端結果只准一次批次查詢，且必須離開 loop 執行緒。

    控制組：landed 同時含已落地與未落地兩類（恆真/恆假的實作會被 tier 斷言抓到）。
    """
    from app.api.category_routes import get_category_works

    md5s = ["%032x" % i for i in range(15)]
    landed = {md5s[0]: "W-1", md5s[3]: "W-2"}      # 2 筆已落地
    assert len(landed) > 0 and len(landed) < len(md5s), (
        "控制組失效：輸入必須同時含已落地與未落地兩類"
    )

    cloud = [
        {"md5": m, "title": "T%d" % i, "authors_display": "A",
         "publication_year": 2000, "language": "en",
         "format": "pdf_born_digital", "size_bytes": 1}
        for i, m in enumerate(md5s)
    ]

    dao = _CountingDAO(landed)
    loop_thread_id = threading.get_ident()

    resp = await get_category_works(
        category_id="c1", page=1, page_size=20, include_cloud=True,
        dao=dao, crawler=_StubCrawler(cloud),
    )

    assert dao.batch_calls == 1, (
        "B 節未修復：應為單次批次查詢，實際 batch_calls=%d" % dao.batch_calls
    )
    assert dao.per_item_calls == 0, (
        "B 節未修復：仍有逐筆 find_work_by_hash 往返 %d 次" % dao.per_item_calls
    )
    assert dao.batch_thread_ids[-1] != loop_thread_id, (
        "B 節未修復：批次 DB 查詢仍在事件迴圈執行緒上"
    )

    # 結果正確性：兩類都必須出現，否則恆真/恆假實作也會通過
    tiers = {}
    for it in resp.items:
        tiers.setdefault(it.availability_tier, []).append(it.md5)
    assert set(tiers) == {0, 1}, (
        "回傳必須同時含已落地(tier=0)與未落地(tier=1)，實際=%s" % sorted(tiers)
    )
    assert sorted(tiers[0]) == sorted(landed), "已落地那類的 md5 對不上"
    assert len(tiers[1]) == len(md5s) - len(landed)


@pytest.mark.asyncio
async def test_category_works_sync_db_reads_leave_event_loop_thread():
    """B 節：:49 get_category 與 :53 get_category_works 兩處同步 DB 讀也要離開 loop。"""
    from app.api.category_routes import get_category_works

    seen = {}
    loop_thread_id = threading.get_ident()

    class _ThreadProbeDAO(_CountingDAO):
        def get_category(self, category_id):
            seen["get_category"] = threading.get_ident()
            return super().get_category(category_id)

        def get_category_works(self, category_id, page=1, page_size=20):
            seen["get_category_works"] = threading.get_ident()
            return super().get_category_works(category_id, page=page, page_size=page_size)

    dao = _ThreadProbeDAO({})
    await get_category_works(
        category_id="c1", page=1, page_size=20, include_cloud=False,
        dao=dao, crawler=_StubCrawler([]),
    )

    assert set(seen) == {"get_category", "get_category_works"}, (
        "控制組失效：兩個 DB 方法沒有全部被呼叫到，實際=%s" % sorted(seen)
    )
    for name, tid in seen.items():
        assert tid != loop_thread_id, (
            "B 節未修復：%s 的同步 DB 讀仍在事件迴圈執行緒上" % name
        )
