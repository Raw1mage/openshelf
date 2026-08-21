import asyncio
import logging
import re
import urllib.parse
from typing import List, Dict, Any, Optional
import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


class LibgenCrawler:
    """即時檢索 Libgen 公網鏡像與解析書目元資料（具備極速響應、智慧引文拆解與容錯級聯檢索）。"""

    # 鏡像清單為快照，站點是活的。已移除實測死亡者（2026-08-20，見
    # BR-20260820_111523）：libgen.rocks（自簽憑證 + 法院查封頁）、
    # libgen.gs（DNS NXDOMAIN）。此處是 D1 同一病灶的第三處，另兩處在
    # mirror_resolver.BASE_MIRRORS 與 dao.DEFAULT_LIBGEN_MIRRORS。
    MIRRORS = [
        "https://libgen.li",
        "https://libgen.la"
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
        """存活鏡像清單。**同步**——內含 DAO 的同步 SQLite 讀。

        任何 `async def` 內的呼叫端必須改走 `_resolve_active_mirrors_async()`，
        否則這一格 DB 讀會落在事件迴圈執行緒上（BR-20260820_210000 A 節）。
        本 property 本身保留同步語意，供既有同步呼叫端（測試、validator）使用。
        """
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

    async def _resolve_active_mirrors_async(self) -> List[str]:
        """在 worker 執行緒上解析存活鏡像清單，使同步 SQLite 讀離開事件迴圈。

        刻意**不**加快取：快取會讓「已修好」與「這條 DB 路徑根本沒被走到」
        共用同一個輸出（BR-20260820_210000 A 節控制組要求）。
        """
        return await asyncio.to_thread(lambda: self.active_mirrors)

    # 出版年份辨識樣式。libgen 現行版型的 Year 欄位可能是光禿禿的四位數（`1987`），
    # 也可能是完整日期（`1972 June 01`）。取第一個落在合理區間的四位數。
    YEAR_RE = re.compile(r"\b(1[0-9]{3}|20[0-9]{2}|21[0-9]{2})\b")

    # 明確表示「來源沒有這個資訊」的佔位字串，與「有字串但解析不出來」區分。
    YEAR_PLACEHOLDERS = {"", "-", "--", "n/a", "na", "none", "null", "unknown", "?"}

    @classmethod
    def parse_publication_year(cls, year_str: Optional[str]) -> Optional[int]:
        """從 libgen 的 Year 欄位取出四位數出版年。

        三種輸入必須可區分，不得共用同一個靜默輸出：
          1. 來源真的沒有（空字串 / `n/a` 之類佔位符）  -> None，不記 log
          2. 有字串但取不出年份（版型變更、非預期格式）-> None，**記 log.debug 帶原始字串**
          3. 有可解析的年份（`1987` / `1972 June 01`）  -> int

        第 2 種是 BR-20260820_130500 的病灶：`str.isdigit()` 把它併進第 1 種，
        於是版型一改就沒有人會知道。
        """
        if year_str is None:
            return None
        normalized = str(year_str).strip()
        if normalized.lower() in cls.YEAR_PLACEHOLDERS:
            return None
        match = cls.YEAR_RE.search(normalized)
        if not match:
            log.debug(
                "libgen year field present but unparseable (possible layout change): %r",
                normalized,
            )
            return None
        return int(match.group(1))

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
        
        # TLS 驗證維持開啟：2026-08-20 實測 libgen.li / libgen.la 憑證在
        # verify=True 下有效；唯一在 verify=True 失敗的 libgen.rocks 是自簽憑證
        # 且內容已是法院查封頁，關閉驗證只會讓查封頁被當成書庫。
        async with httpx.AsyncClient(
            headers={"User-Agent": self.USER_AGENT},
            timeout=8.0,
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
        # 同步 SQLite 讀移出事件迴圈執行緒（BR-20260820_210000 A 節）。
        mirrors = await self._resolve_active_mirrors_async()
        for mirror in mirrors:
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
                    # BeautifulSoup 全文解析是 CPU-bound，同樣移出事件迴圈執行緒
                    # （BR-20260820_210000 A 節）。兩個 _parse_* 方法維持同步 def，
                    # 以免破壞 validator.py 與既有測試的直接呼叫。
                    if is_libgen_is:
                        results = await asyncio.to_thread(self._parse_libgen_is_html, resp.text, mirror)
                    else:
                        results = await asyncio.to_thread(self._parse_libgen_li_html, resp.text, mirror)
                    if results:
                        return results
            except Exception:
                continue
        return []

    # 丟棄留痕的等級門檻。「丟掉幾筆」與「整批都被丟掉」是兩種不同的事，
    # 不得共用同一個等級——理由見 _log_md5_drops。
    def _log_md5_drops(self, adapter: str, dropped: int, kept: int, base_url: str) -> None:
        """記錄本次解析因無 md5 而丟棄的 row 數。

        **等級不是單一值，而是依「這批還剩不剩東西」分流**，因為兩種情形對讀者
        的意義完全不同：

          - `kept > 0`（丟掉一部分）-> INFO。
            這是**正常但值得知道**的事：來源版型裡本來就會混雜少數無下載連結的
            項目，使用者仍拿得到結果。用 DEBUG 會讓它在生產環境永遠看不到
            （本專案生產等級為 INFO）；用 WARNING 則會讓每次正常搜尋都噴警告，
            久了沒有人會再看警告——那是把留痕做成雜訊。

          - `kept == 0 且 dropped > 0`（整批都被丟掉）-> WARNING。
            這一格才是 BR-20260821_030000 的核心：對外的輸出是空的 `[]`，而
            「這批 row 全無 md5」「parser 壞了」「搜尋本來就沒結果」三者共用
            這同一個空 list。它同時是**來源版型可能已變更**的第一個訊號——
            例如鏡像把 md5 從 href 改成 data 屬性，症狀正是全部落進這一格。
            這是需要人介入判斷的情況，不是例行資訊。

        誰會讀這個 log、在什麼情境下讀（這決定了上面的取捨）：
          1. 維護者在使用者回報「搜到的比預期少 / 根本搜不到」時翻 `docker logs`。
             他要能一眼分辨「來源就沒有」與「我們自己過濾掉了」。
          2. 維護者在鏡像清單維護（`/api/settings/libgen-mirrors/validate`）之後，
             想知道某個新鏡像實際被解析成什麼樣子。
          3. 沒有人會在「一切正常」時盯著它——所以 INFO 那一格必須夠稀疏
             （只在真的有丟棄時才發，`dropped == 0` 完全不出聲）。

        `dropped == 0` 時**一個字都不印**：那是絕大多數情況，印了就是純雜訊，
        且會讓「有丟棄」這件事失去對比度。
        """
        if not dropped:
            return

        # `dropped %d row(s)` 是**穩定的可檢形式**，兩個分支必須一字不差地共用。
        # tests/test_libgen_parser_md5_gate.py 兩條斷言（`dropped 1 row(s)` 與
        # `dropped 2 row(s)`）靠它區分丟棄筆數；在中間插入任何字（如 `ALL`）
        # 都會讓那兩條失效。「整批丟棄」靠的是**等級與句尾補述**，不是改寫計數句。
        if kept:
            log.info(
                "%s adapter dropped %d row(s) with no md5 (unresolvable download);"
                " kept %d | mirror=%s",
                adapter, dropped, kept, base_url,
            )
        else:
            log.warning(
                "%s adapter dropped %d row(s) with no md5 (unresolvable download);"
                " kept 0 — the entire batch was dropped and the result set is empty."
                " 反覆出現時優先懷疑該鏡像版型已變更（BR-20260821_030000）。"
                " | mirror=%s",
                adapter, dropped, base_url,
            )

    def _parse_libgen_li_html(self, html_content: str, base_url: str) -> List[Dict[str, Any]]:
        """解析 libgen.li 專屬搜尋結果表格。"""
        soup = BeautifulSoup(html_content, "html.parser")
        items = []

        table = soup.find("table", id="tablelibgen") or soup.find("table")
        if not table:
            return items

        rows = table.find_all("tr")[1:]
        dropped_no_md5 = 0
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
            year = self.parse_publication_year(year_str)
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

            if not md5_val:
                # 使用者裁示（2026-08-21）：「下載不到的書就不要顯示搜尋結果」。
                # md5 是本站唯一的下載主鍵（mirror_resolver.resolve_download_url
                # 在無 md5 時直接回 None），缺它的 row 必然下載失敗；且 work_id 會
                # 全部互撞成字面值 "libgen_"（BR-20260821_030000）。因此在來源端
                # 丟棄，而不是讓它進 UI 之後才失敗。
                # 丟棄必須留痕：否則「這批 row 沒有 md5」與「parser 壞掉回空 list」
                # 與「搜尋本來就沒結果」在外部共用同一個輸出。
                dropped_no_md5 += 1
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

        self._log_md5_drops("libgen_li", dropped_no_md5, len(items), base_url)
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
        dropped_no_md5 = 0
        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 10:
                continue

            authors = cols[1].text.strip()
            title_col = cols[2]
            title = title_col.find("a").text.strip() if title_col.find("a") else title_col.text.strip()
            publisher = cols[3].text.strip()
            year_str = cols[4].text.strip()
            year = self.parse_publication_year(year_str)
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

            if not md5_val:
                # 同 _parse_libgen_li_html：無 md5 即無下載路徑，於來源端丟棄。
                # 本適配器目前無實測樣本（三個 is 鏡像被 dao 的 validation_status
                # 濾掉），故丟棄計數同樣入 log 以便日後觀察。
                dropped_no_md5 += 1
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

        self._log_md5_drops("libgen_is", dropped_no_md5, len(items), base_url)
        return items
