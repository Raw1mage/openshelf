import re
import urllib.parse
from typing import List, Dict, Any, Optional
import httpx
from bs4 import BeautifulSoup


class LibgenCrawler:
    """即時檢索 Libgen 公網鏡像與解析書目元資料（具備極速響應、智慧引文拆解與容錯級聯檢索）。"""

    MIRRORS = [
        "https://libgen.li",
        "https://libgen.la",
        "https://libgen.rocks",
        "https://libgen.gs"
    ]

    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

    KNOWN_PUBLISHERS = {
        "wiley", "springer", "o'reilly", "oreilly", "pearson", "mcgraw-hill", 
        "mcgraw hill", "elsevier", "mit press", "cambridge", "oxford", "cengage",
        "routledge", "academic press", "addison-wesley", "addison wesley", "prentice hall"
    }

    def __init__(self, mirrors: Optional[List[str]] = None, dao: Optional[Any] = None):
        self._custom_mirrors = mirrors
        self.dao = dao

    @property
    def active_mirrors(self) -> List[str]:
        if self._custom_mirrors:
            return self._custom_mirrors
        if self.dao:
            try:
                verified_urls = self.dao.get_active_libgen_mirror_urls()
                if verified_urls:
                    return verified_urls
            except Exception:
                pass
        return self.MIRRORS

    # Magnet URI 與 .torrent 直鏈辨識樣式
    MAGNET_RE = re.compile(r"magnet:\?[^\s\"'<>]+", re.IGNORECASE)
    BTIH_RE = re.compile(r"xt=urn:btih:([a-zA-Z0-9]+)", re.IGNORECASE)
    TORRENT_HREF_RE = re.compile(r"\.torrent(\?|$|#)", re.IGNORECASE)

    @staticmethod
    def parse_magnet_uri(magnet: str) -> Dict[str, Any]:
        """解析 Magnet URI，取出 info_hash / display_name / trackers。

        非 magnet 字串一律回傳 info_hash=None（而非空字串），
        使「不是 magnet」與「是 magnet 但無 hash」不共用同一個輸出。
        """
        empty: Dict[str, Any] = {"info_hash": None, "display_name": None, "trackers": []}
        if not magnet or not isinstance(magnet, str):
            return empty
        if not magnet.lower().startswith("magnet:"):
            return empty

        query = magnet[magnet.find("?") + 1:] if "?" in magnet else ""
        params = urllib.parse.parse_qs(query, keep_blank_values=False)

        info_hash = None
        for xt in params.get("xt", []):
            m = LibgenCrawler.BTIH_RE.search(f"xt={xt}")
            if m:
                info_hash = m.group(1).lower()
                break

        display_names = params.get("dn", [])
        trackers = [t for t in params.get("tr", []) if t]

        return {
            "info_hash": info_hash,
            "display_name": display_names[0] if display_names else None,
            "trackers": trackers,
        }

    @classmethod
    def _extract_torrent_sources(cls, row_or_cols: Any, base_url: str = "") -> Dict[str, Any]:
        """自書目列（BeautifulSoup Tag 或 Tag 串列）提取 .torrent 直鏈與 Magnet URI。

        回傳 dict：torrent_url / magnet_uri / download_protocol / peers_count。
        找不到任何 P2P 來源時 download_protocol 維持 'http'（而非空字串）。
        """
        result: Dict[str, Any] = {
            "torrent_url": None,
            "magnet_uri": None,
            "download_protocol": "http",
            "peers_count": None,
        }

        tags = row_or_cols if isinstance(row_or_cols, (list, tuple)) else [row_or_cols]

        anchors = []
        for tag in tags:
            if tag is None:
                continue
            try:
                anchors.extend(tag.find_all("a"))
            except AttributeError:
                continue

        for a in anchors:
            href = a.get("href", "") or ""
            if not href:
                continue

            # 1. Magnet：href 直接是 magnet:，或內嵌於 query string
            if href.lower().startswith("magnet:"):
                if not result["magnet_uri"]:
                    result["magnet_uri"] = href
                continue

            # 2. .torrent 直鏈（相對路徑補上 base_url）
            if cls.TORRENT_HREF_RE.search(href):
                if not result["torrent_url"]:
                    result["torrent_url"] = f"{base_url}{href}" if href.startswith("/") and base_url else href
                continue

        # 3. 保底：整列純文字中掃描裸露的 magnet 連結（部分鏡像以文字呈現）
        if not result["magnet_uri"]:
            for tag in tags:
                if tag is None:
                    continue
                try:
                    text = tag.get_text(" ", strip=True)
                except AttributeError:
                    continue
                m = cls.MAGNET_RE.search(text)
                if m:
                    result["magnet_uri"] = m.group(0)
                    break

        if result["magnet_uri"] or result["torrent_url"]:
            result["download_protocol"] = "torrent"

        return result

    @staticmethod
    def parse_size_to_bytes(size_str: str) -> int:
        """解析如 '12.5 Mb', '800 Kb', '1.2 Gb' 為位元組數。"""
        s = size_str.strip().lower()
        match = re.match(r"([\d\.]+)\s*([a-z]+)", s)
        if not match:
            return 0
        val, unit = float(match.group(1)), match.group(2)
        if "gb" in unit:
            return int(val * 1024 * 1024 * 1024)
        elif "mb" in unit:
            return int(val * 1024 * 1024)
        elif "kb" in unit:
            return int(val * 1024)
        elif "b" in unit:
            return int(val)
        return int(val)

    def generate_smart_queries(self, raw_query: str) -> List[str]:
        """智慧引文拆解器：將複合書目引文（多作者、標題、出版商逗號串）分解為高命中率檢索候選詞。"""
        raw = raw_query.strip()
        candidates = []

        # 1. 拆解逗號與分號
        if any(sep in raw for sep in (",", ";", "/")):
            parts = [p.strip() for p in re.split(r"[,;/]+", raw) if p.strip()]
            filtered_parts = [p for p in parts if p.lower() not in self.KNOWN_PUBLISHERS]
            
            # 若去除出版商後有乾淨短語，優先作為第一候選詞
            if filtered_parts:
                clean_full = " ".join(filtered_parts)
                candidates.append(clean_full)

            title_candidates = []
            author_candidates = []
            
            for part in filtered_parts:
                # 判斷是否像作者名（字數短、或包含逗號姓名結構）
                words = part.split()
                if len(words) <= 3 and not any(c.isdigit() for c in part):
                    author_candidates.append(part)
                else:
                    title_candidates.append(part)

            if title_candidates:
                candidates.append(" ".join(title_candidates))
            if author_candidates and title_candidates:
                candidates.append(f"{title_candidates[0]} {author_candidates[0]}")

        # 2. 去除常見副標題標點（冒號、破折號、括號）
        sub_cleaned = re.split(r"[:\-\(\)\[\]]", raw)[0].strip()
        if sub_cleaned and sub_cleaned != raw and len(sub_cleaned) >= 4:
            candidates.append(sub_cleaned)

        # 3. 原始字串保底
        if raw not in candidates:
            candidates.append(raw)

        # 4. 去重但保留優先序
        seen = set()
        unique_candidates = []
        for c in candidates:
            norm = c.lower().strip()
            if norm and norm not in seen:
                seen.add(norm)
                unique_candidates.append(c)

        return unique_candidates

    async def search_live(self, query: str, max_results: int = 25) -> List[Dict[str, Any]]:
        """執行智慧即時檢索：按優先級依序嘗試智慧候選詞，一旦命中即刻返回。"""
        if not query or not query.strip():
            return []
        smart_candidates = self.generate_smart_queries(query)
        
        async with httpx.AsyncClient(
            headers={"User-Agent": self.USER_AGENT},
            timeout=8.0,
            verify=False,
            follow_redirects=True
        ) as client:
            # 依序執行候選詞檢索
            for term in smart_candidates:
                items = await self._execute_single_search(client, term, max_results)
                if items:
                    return items
            
            return []

    async def search(self, query: str, max_results: int = 25) -> List[Dict[str, Any]]:
        """向存活鏡像發送搜尋請求（相容別名）。"""
        return await self.search_live(query, max_results)

    async def _execute_single_search(self, client: httpx.AsyncClient, query_term: str, max_results: int) -> List[Dict[str, Any]]:
        """針對單一查詢詞向通過預檢驗證之存活鏡像發起 HTTP 檢索，支援 libgen_li 與 libgen_is 多適配器。"""
        encoded_query = urllib.parse.quote(query_term)
        for mirror in self.active_mirrors:
            if "library.lol" in mirror:
                continue
            is_libgen_is = any(k in mirror for k in ("libgen.is", "libgen.rs", "libgen.st"))
            if is_libgen_is:
                search_url = f"{mirror}/search.php?req={encoded_query}&open=0&res=25&view=simple&phrase=1&column=def"
            else:
                search_url = f"{mirror}/index.php?req={encoded_query}"

            try:
                resp = await client.get(search_url)
                if resp.status_code == 200 and "table" in resp.text:
                    if is_libgen_is:
                        results = self._parse_libgen_is_html(resp.text, mirror)
                    else:
                        results = self._parse_libgen_li_html(resp.text, mirror)
                    if results:
                        return results
            except Exception:
                continue
        return []

    def _parse_libgen_li_html(self, html_content: str, base_url: str) -> List[Dict[str, Any]]:
        """解析 libgen.li 專屬搜尋結果表格。"""
        soup = BeautifulSoup(html_content, "html.parser")
        items = []

        table = soup.find("table", id="tablelibgen") or soup.find("table")
        if not table:
            return items

        rows = table.find_all("tr")[1:]
        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 9:
                continue

            title_col = cols[0]
            title_link = title_col.find("a")
            raw_title = title_link.text.strip() if title_link else title_col.text.strip()
            clean_title = re.sub(r"\s+", " ", raw_title.split("\n")[0]).strip()

            authors = cols[1].text.strip()
            publisher = cols[2].text.strip()
            year_str = cols[3].text.strip()
            year = int(year_str) if year_str.isdigit() else None
            language = cols[4].text.strip()
            pages = cols[5].text.strip()
            size_str = cols[6].text.strip()
            size_bytes = self.parse_size_to_bytes(size_str)
            extension = cols[7].text.strip().lower()

            mirror_links = []
            md5_val = ""
            for a in cols[8].find_all("a"):
                href = a.get("href", "")
                if href:
                    if href.startswith("/"):
                        href = f"{base_url}{href}"
                    mirror_links.append(href)
                    md5_match = re.search(r"([a-fA-F0-9]{32})", href)
                    if md5_match and not md5_val:
                        md5_val = md5_match.group(1).lower()

            if not md5_val and not clean_title:
                continue

            format_type = "epub" if extension == "epub" else "pdf_born_digital"

            # 提取 Torrent / Magnet 來源（掃描整列，不限於鏡像欄）
            torrent_src = self._extract_torrent_sources(row, base_url)

            items.append({
                "work_id": f"libgen_{md5_val}",
                "title": clean_title,
                "authors_display": authors or "未知作者",
                "publisher": publisher,
                "publication_year": year,
                "pages": pages,
                "language": language or "en",
                "format": format_type,
                "extension": extension,
                "size_bytes": size_bytes,
                "md5": md5_val,
                "availability_tier": 2,
                "mirror_links": mirror_links,
                "torrent_url": torrent_src["torrent_url"],
                "magnet_uri": torrent_src["magnet_uri"],
                "download_protocol": torrent_src["download_protocol"],
                "peers_count": torrent_src["peers_count"],
                "source": "libgen"
            })

        return items

    def _parse_libgen_is_html(self, html_content: str, base_url: str) -> List[Dict[str, Any]]:
        """解析傳統 libgen.is 表格。"""
        soup = BeautifulSoup(html_content, "html.parser")
        items = []

        tables = soup.find_all("table")
        target_table = None
        for table in tables:
            rows = table.find_all("tr")
            if len(rows) > 1 and "Title" in rows[0].text and "Author" in rows[0].text:
                target_table = table
                break

        if not target_table:
            return items

        rows = target_table.find_all("tr")[1:]
        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 10:
                continue

            authors = cols[1].text.strip()
            title_col = cols[2]
            title = title_col.find("a").text.strip() if title_col.find("a") else title_col.text.strip()
            publisher = cols[3].text.strip()
            year_str = cols[4].text.strip()
            year = int(year_str) if year_str.isdigit() else None
            pages = cols[5].text.strip()
            language = cols[6].text.strip()
            size_str = cols[7].text.strip()
            size_bytes = self.parse_size_to_bytes(size_str)
            extension = cols[8].text.strip().lower()

            mirror_links = []
            md5_val = ""
            for a in cols[9:]:
                link = a.find("a")
                if link and link.get("href"):
                    href = link["href"]
                    mirror_links.append(href)
                    md5_match = re.search(r"([a-fA-F0-9]{32})", href)
                    if md5_match and not md5_val:
                        md5_val = md5_match.group(1).lower()

            if not md5_val and not title:
                continue

            format_type = "epub" if extension == "epub" else "pdf_born_digital"

            # 提取 Torrent / Magnet 來源（掃描整列，不限於鏡像欄）
            torrent_src = self._extract_torrent_sources(row, base_url)

            items.append({
                "work_id": f"libgen_{md5_val}",
                "title": title,
                "authors_display": authors or "未知作者",
                "publisher": publisher,
                "publication_year": year,
                "pages": pages,
                "language": language or "en",
                "format": format_type,
                "extension": extension,
                "size_bytes": size_bytes,
                "md5": md5_val,
                "availability_tier": 2,
                "mirror_links": mirror_links,
                "torrent_url": torrent_src["torrent_url"],
                "magnet_uri": torrent_src["magnet_uri"],
                "download_protocol": torrent_src["download_protocol"],
                "peers_count": torrent_src["peers_count"],
                "source": "libgen"
            })

        return items
