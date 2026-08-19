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
                words = part.split()
                if len(words) >= 2 or any(kw in part.lower() for kw in ["system", "concept", "guide", "handbook", "principle", "introduction", "science", "programming", "java", "python", "data"]):
                    title_candidates.append(part)
                else:
                    author_candidates.append(part)

            for title in title_candidates:
                candidates.append(title)
                for author in author_candidates:
                    last_name = author.split()[-1]
                    candidates.append(f"{last_name} {title}")
                    candidates.append(f"{title} {last_name}")

        # 原始查詢作為兜底
        candidates.append(raw)

        # 去重且保持順序
        seen = set()
        unique_cands = []
        for c in candidates:
            c_clean = re.sub(r"\s+", " ", c).strip()
            if c_clean and c_clean.lower() not in seen:
                seen.add(c_clean.lower())
                unique_cands.append(c_clean)

        return unique_cands

    async def search(self, query: str, max_results: int = 25) -> List[Dict[str, Any]]:
        """向存活鏡像發送搜尋請求，若原字串無結果則自動觸發智慧級聯分解。"""
        if not query.strip():
            return []

        queries_to_try = self.generate_smart_queries(query)
        aggregated_results: Dict[str, Dict[str, Any]] = {}

        async with httpx.AsyncClient(
            headers={"User-Agent": self.USER_AGENT},
            timeout=5.0,
            verify=False,
            follow_redirects=True
        ) as client:
            for q in queries_to_try:
                results = await self._execute_single_search(client, q, max_results)
                if results:
                    for item in results:
                        md5 = item.get("md5")
                        if md5 and md5 not in aggregated_results:
                            aggregated_results[md5] = item
                    if len(aggregated_results) >= 4:
                        break

        all_items = list(aggregated_results.values())
        raw_words = set(re.findall(r"\w+", query.lower()))
        
        def score_item(it: Dict[str, Any]) -> int:
            text = f"{it.get('title', '')} {it.get('authors_display', '')} {it.get('publisher', '')}".lower()
            return sum(1 for w in raw_words if w in text)

        all_items.sort(key=score_item, reverse=True)
        return all_items[:max_results]

    async def _execute_single_search(self, client: httpx.AsyncClient, query_term: str, max_results: int) -> List[Dict[str, Any]]:
        """針對單一查詢詞向存活鏡像發起 HTTP 檢索。"""
        encoded_query = urllib.parse.quote(query_term)
        for mirror in self.MIRRORS:
            search_url = f"{mirror}/index.php?req={encoded_query}"
            try:
                resp = await client.get(search_url)
                if resp.status_code == 200 and "table" in resp.text:
                    results = self._parse_libgen_li_html(resp.text, mirror)
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
                "source": "libgen"
            })

        return items
