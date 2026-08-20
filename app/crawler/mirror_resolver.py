import logging
from typing import List, Optional, Any, Set
import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


class MirrorResolver:
    """動態解析 Libgen / IPFS 鏡像頁面並探測可用之直鏈下載 URL。"""

    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

    # 鏡像清單為快照，站點是活的。以下順序反映 2026-08-20 的實測存活狀態：
    #   libgen.li / libgen.la  → HTTP 200 真書庫，且 TLS 憑證在 verify=True 下有效
    #   libgen.is              → DNS 可解析但 TCP 逾時，UNDECIDABLE（分不出站點下線與
    #                            本地網路阻擋），故保留但降至最後順位
    # 已移除（實測死亡，非推測）：
    #   libgen.rocks  自簽憑證 + <title>Domain Seizure Notice</title>（法院查封）
    #   libgen.gs     DNS NXDOMAIN
    #   libgen.pm     DNS NXDOMAIN
    #   library.lol   HTTP 200 但 body 為 Domain Seizure Notice（查封）
    BASE_MIRRORS = [
        "https://libgen.li",
        "https://libgen.la",
        "https://libgen.is",
    ]

    # 判定「這個回應仍然是書庫」所需命中的結構標記數下限。
    # 實測分數（2026-08-20）：真詳情頁 5、真「書不存在」頁 3、三種查封／代管頁皆 0。
    LIBGEN_MARKER_THRESHOLD = 2

    # 連線層逾時獨立於整體逾時：死站台不應各自吃滿 10 秒。
    CONNECT_TIMEOUT = 5.0
    READ_TIMEOUT = 10.0

    def __init__(self, mirrors: Optional[List[str]] = None, dao: Optional[Any] = None):
        self._custom_mirrors = mirrors
        self.dao = dao
        # 本次解析過程中被判定為「已非書庫」的鏡像，供呼叫端事後查詢／告警彙整。
        self.seized_mirrors: Set[str] = set()

    @property
    def active_mirrors(self) -> List[str]:
        if self._custom_mirrors:
            return self._custom_mirrors
        if self.dao:
            try:
                verified = self.dao.get_active_libgen_mirror_urls()
                if verified:
                    return verified
            except Exception as exc:
                log.debug("dao.get_active_libgen_mirror_urls 失敗，回退內建清單: %s: %s",
                          type(exc).__name__, exc)
        return self.BASE_MIRRORS

    @staticmethod
    def _looks_like_libgen(html: str) -> bool:
        """判斷一段 HTML 是否仍然來自 Libgen 書庫本體。

        設計要點（此函式存在的理由）：
        呼叫端原本無法區分兩種都回 None 的情況——
          (a) 這本書在這個鏡像上沒有  → 正常，換下一個鏡像
          (b) 這個網域已被查封／接管  → 異常，該告警
        兩者都是 HTTP 200 且解析不到目標元素。

        判準採「站點自有資產與路由」的結構特徵，不採字串比對：
        查封頁換一套文案即可繞過字串比對，但它無法在不變回書庫的前提下
        重新提供 Libgen 自己的 CSS/JS 資產與 md5 路由。

        ⚠ 刻意不採「<a> 標籤數」作為判準（BR 初版建議）：實測真實的
        「書不存在」頁只有 5 個 <a>，用 anchor 數門檻會把缺席態誤判為查封態
        ——那正是本函式要消除的病，只是換了個方向。
        """
        if not html:
            return False

        soup = BeautifulSoup(html, "html.parser")

        # 收集全文件的資產與連結目標（不限 <a>，查封頁連 <script> 都沒有）
        refs: List[str] = []
        for tag, attr in (("link", "href"), ("script", "src"), ("a", "href"),
                          ("img", "src"), ("form", "action")):
            for el in soup.find_all(tag):
                val = el.get(attr) or ""
                if val:
                    refs.append(val.lower())
        joined = " ".join(refs)

        markers = 0

        # 1-3. Libgen 自有的靜態資產路徑（真詳情頁與真「書不存在」頁都會帶）
        if "dark-mode" in joined:
            markers += 1
        if "font.min.css" in joined:
            markers += 1
        if "favicon.ico" in joined:
            markers += 1

        # 4. Libgen 自有路由（搜尋／詳情／取檔）
        if any(route in joined for route in ("index.php", "ads.php", "get.php", "book.php", "md5=")):
            markers += 1

        # 5. library.lol 型 gateway 的下載區塊與 IPFS 出口
        if soup.find("div", id="download") is not None:
            markers += 1
        if "ipfs" in joined:
            markers += 1

        # 6. 詳情頁的書目表格
        if soup.find("table") is not None:
            markers += 1

        return markers >= MirrorResolver.LIBGEN_MARKER_THRESHOLD

    def _guard_is_library(self, html: str, page_url: str, base_url: str) -> bool:
        """對回應內容做哨兵檢查；命中查封／接管特徵時大聲記錄並回 False。"""
        if MirrorResolver._looks_like_libgen(html):
            return True

        title = ""
        try:
            soup = BeautifulSoup(html, "html.parser")
            if soup.title and soup.title.string:
                title = soup.title.string.strip()[:120]
        except Exception as exc:
            log.debug("查封哨兵解析標題失敗: %s: %s", type(exc).__name__, exc)

        self.seized_mirrors.add(base_url)
        log.warning(
            "鏡像回應 HTTP 200 但內容已不是 Libgen 書庫（疑似網域查封或被接管），"
            "已標記為 dead 並跳過：mirror=%s url=%s bytes=%d title=%r",
            base_url, page_url, len(html or ""), title,
        )
        return False

    async def resolve_download_url(self, md5: str, candidate_mirrors: Optional[List[str]] = None) -> Optional[str]:
        """給定 MD5 或候選頁面，非同步解析出可用的直鏈下載 URL。"""
        md5 = md5.strip().lower()
        if not md5:
            return None

        async with httpx.AsyncClient(
            headers={"User-Agent": self.USER_AGENT},
            timeout=httpx.Timeout(self.READ_TIMEOUT, connect=self.CONNECT_TIMEOUT),
            follow_redirects=True
        ) as client:
            # 1. 優先嘗試各個通過預檢驗證的活躍鏡像家族
            for base in self.active_mirrors:
                if base in self.seized_mirrors:
                    continue
                if "library.lol" in base:
                    # library.lol 於 2026-08-20 實測已被查封；此分支僅在 dao 或
                    # custom_mirrors 明確供應該網域時才會走到，保留是為了讓哨兵
                    # 能對它發出告警，而不是靜默回 None。
                    direct_url = await self._resolve_from_library_lol(client, f"{base}/main/{md5}", base)
                else:
                    # libgen.is/rs/st 家族原本被硬路由到 library.lol（已查封 → 恆回
                    # None）。改為與其他鏡像一致走 ads.php；此路徑對 libgen.is
                    # 尚未實測（該站 TCP 逾時，UNDECIDABLE），但嚴格優於一條
                    # 可證明恆失敗的分支。
                    direct_url = await self._resolve_from_libgen_li(client, f"{base}/ads.php?md5={md5}", base)

                if direct_url:
                    return direct_url

            # 2. 嘗試 candidate_mirrors 中的指定連結
            if candidate_mirrors:
                for mirror_url in candidate_mirrors:
                    if "ads.php" in mirror_url:
                        base = mirror_url.split("/ads.php")[0]
                        if base in self.seized_mirrors:
                            continue
                        direct_url = await self._resolve_from_libgen_li(client, mirror_url, base)
                        if direct_url:
                            return direct_url
                    elif "library.lol" in mirror_url:
                        direct_url = await self._resolve_from_library_lol(client, mirror_url, "library.lol")
                        if direct_url:
                            return direct_url

        return None

    async def _resolve_from_libgen_li(self, client: httpx.AsyncClient, page_url: str, base_url: str) -> Optional[str]:
        """從 libgen.li/ads.php 解析 get.php?md5=...&key=... 直鏈。"""
        try:
            resp = await client.get(page_url)
            if resp.status_code != 200:
                log.debug("鏡像回應非 200：url=%s status=%s", page_url, resp.status_code)
                return None
            if not self._guard_is_library(resp.text, page_url, base_url):
                return None
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a"):
                href = a.get("href", "")
                if "get.php?md5=" in href:
                    if not href.startswith("http"):
                        href = f"{base_url}/" + href.lstrip("/")
                    return href
            log.debug("鏡像頁面有效但查無此書（缺席，非失敗）：url=%s", page_url)
        except Exception as exc:
            log.debug("鏡像請求失敗：url=%s %s: %s", page_url, type(exc).__name__, exc)
        return None

    async def _resolve_from_library_lol(self, client: httpx.AsyncClient, page_url: str,
                                        base_url: str = "library.lol") -> Optional[str]:
        """從 library.lol 解析主下載與 IPFS 鏡像。"""
        try:
            resp = await client.get(page_url)
            if resp.status_code != 200:
                log.debug("Gateway 回應非 200：url=%s status=%s", page_url, resp.status_code)
                return None
            if not self._guard_is_library(resp.text, page_url, base_url):
                return None
            soup = BeautifulSoup(resp.text, "html.parser")
            download_div = soup.find("div", id="download")
            if download_div:
                get_link = download_div.find("a")
                if get_link and get_link.get("href"):
                    target_href = get_link["href"]
                    if target_href.startswith("http"):
                        return target_href

            for a in soup.find_all("a"):
                href = a.get("href", "")
                if "ipfs.io" in href or "cloudflare-ipfs" in href or "pinata" in href:
                    return href
            log.debug("Gateway 頁面有效但查無此書（缺席，非失敗）：url=%s", page_url)
        except Exception as exc:
            log.debug("Gateway 請求失敗：url=%s %s: %s", page_url, type(exc).__name__, exc)
        return None
