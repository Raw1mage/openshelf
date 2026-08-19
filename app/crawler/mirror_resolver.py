import re
from typing import List, Optional
import httpx
from bs4 import BeautifulSoup


class MirrorResolver:
    """動態解析 Libgen / IPFS 鏡像頁面並探測可用之直鏈下載 URL。"""

    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

    BASE_MIRRORS = [
        "https://libgen.li",
        "https://libgen.la",
        "https://libgen.rocks",
        "https://libgen.gs",
        "https://libgen.pm",
        "http://library.lol"
    ]

    async def resolve_download_url(self, md5: str, candidate_mirrors: Optional[List[str]] = None) -> Optional[str]:
        """給定 MD5 或候選頁面，非同步解析出可用的直鏈下載 URL。"""
        md5 = md5.strip().lower()
        if not md5:
            return None

        async with httpx.AsyncClient(
            headers={"User-Agent": self.USER_AGENT},
            timeout=10.0,
            verify=False,
            follow_redirects=True
        ) as client:
            # 1. 優先嘗試各個 Libgen 活躍鏡像家族
            for base in self.BASE_MIRRORS:
                if "library.lol" in base:
                    direct_url = await self._resolve_from_library_lol(client, f"{base}/main/{md5}")
                else:
                    direct_url = await self._resolve_from_libgen_li(client, f"{base}/ads.php?md5={md5}", base)
                
                if direct_url:
                    return direct_url

            # 2. 嘗試 candidate_mirrors 中的指定連結
            if candidate_mirrors:
                for mirror_url in candidate_mirrors:
                    if "ads.php" in mirror_url:
                        base = mirror_url.split("/ads.php")[0]
                        direct_url = await self._resolve_from_libgen_li(client, mirror_url, base)
                        if direct_url:
                            return direct_url
                    elif "library.lol" in mirror_url:
                        direct_url = await self._resolve_from_library_lol(client, mirror_url)
                        if direct_url:
                            return direct_url

        return None

    async def _resolve_from_libgen_li(self, client: httpx.AsyncClient, page_url: str, base_url: str) -> Optional[str]:
        """從 libgen.li/ads.php 解析 get.php?md5=...&key=... 直鏈。"""
        try:
            resp = await client.get(page_url)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for a in soup.find_all("a"):
                    href = a.get("href", "")
                    if "get.php?md5=" in href:
                        if not href.startswith("http"):
                            href = f"{base_url}/" + href.lstrip("/")
                        return href
        except Exception:
            pass
        return None

    async def _resolve_from_library_lol(self, client: httpx.AsyncClient, page_url: str) -> Optional[str]:
        """從 library.lol 解析主下載與 IPFS 鏡像。"""
        try:
            resp = await client.get(page_url)
            if resp.status_code == 200:
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
        except Exception:
            pass
        return None
