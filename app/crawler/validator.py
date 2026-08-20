import asyncio
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
import httpx
from bs4 import BeautifulSoup

from app.models.catalog import LibgenMirrorValidationReport
from app.crawler.libgen_live import LibgenCrawler


class MirrorValidator:
    """Libgen 鏡像來源上線前預檢驗證器（Pre-flight Validator）與自動 BR 發送系統。"""

    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    TEST_QUERY = "Python"
    TEST_MD5 = "00000000000000000000000000000000"

    def __init__(self, issues_dir: Optional[Path] = None):
        self.issues_dir = issues_dir or (Path(__file__).parent.parent.parent / "issues")
        self.issues_dir.mkdir(parents=True, exist_ok=True)
        self.crawler = LibgenCrawler()

    async def validate_mirror(self, raw_url: str, auto_dispatch_br: bool = True) -> LibgenMirrorValidationReport:
        """
        執行多階段預檢驗證：
        1. 連線與延遲探測 (Connectivity Probe)
        2. 結構適配器相容性測試 (Scraper Adapter Parsing Test)
        3. 若結構不相容且來源在線，自動發送 Bug Report (Auto BR Dispatch)
        """
        url = raw_url.strip().rstrip("/")
        if not url.startswith("http://") and not url.startswith("https://"):
            url = f"https://{url}"

        start_time = time.time()

        # TLS 驗證維持開啟：本類專職是「審查一個鏡像能不能信任」，
        # 若在此關閉驗證，一個自簽憑證的被接管網域會直接通過預檢、
        # 被標成 verified 並進入正式鏡像清單。
        async with httpx.AsyncClient(
            headers={"User-Agent": self.USER_AGENT},
            timeout=8.0,
            follow_redirects=True
        ) as client:
            # 1. 探測 Gateway (如 library.lol)
            if "library.lol" in url or "lol" in url or "gateway" in url:
                report = await self._test_gateway_mirror(client, url, start_time)
                if report.validation_status == "incompatible_layout" and auto_dispatch_br:
                    br_id, br_path = await asyncio.to_thread(
                        self.dispatch_br, url, report.status_code, "Gateway 結構變更或無效", report.error_message or "")
                    report.br_id = br_id
                    report.br_path = str(br_path)
                    report.dispatched_br = True
                return report

            # 2. 探測 Libgen.li 系列適配器
            li_search_url = f"{url}/index.php?req={self.TEST_QUERY}"
            try:
                t0 = time.time()
                resp = await client.get(li_search_url)
                latency_ms = round((time.time() - t0) * 1000, 1)

                if resp.status_code == 200:
                    # BeautifulSoup 全文解析是 CPU-bound。本方法被 async def 路由
                    # （settings_routes.py validate_libgen_mirror）await，所以直接呼叫
                    # 會落在事件迴圈執行緒上，卡住全 process 的請求
                    # （BR-20260820_210000 D 節；實測單次驗證 max_hb 達 556.7ms）。
                    records = await asyncio.to_thread(self.crawler._parse_libgen_li_html, resp.text, url)
                    if len(records) > 0 and any(r.get("md5") for r in records):
                        return LibgenMirrorValidationReport(
                            url=url,
                            is_online=True,
                            status_code=resp.status_code,
                            latency_ms=latency_ms,
                            validation_status="verified",
                            adapter_type="libgen_li",
                            sample_records_count=len(records),
                            error_message=None
                        )
            except Exception as e:
                pass

            # 3. 探測 Libgen.is 系列傳統適配器
            is_search_url = f"{url}/search.php?req={self.TEST_QUERY}&open=0&res=25&view=simple&phrase=1&column=def"
            try:
                t0 = time.time()
                resp = await client.get(is_search_url)
                latency_ms = round((time.time() - t0) * 1000, 1)

                if resp.status_code == 200:
                    records = await asyncio.to_thread(self.crawler._parse_libgen_is_html, resp.text, url)
                    if len(records) > 0 and any(r.get("md5") for r in records):
                        return LibgenMirrorValidationReport(
                            url=url,
                            is_online=True,
                            status_code=resp.status_code,
                            latency_ms=latency_ms,
                            validation_status="verified",
                            adapter_type="libgen_is",
                            sample_records_count=len(records),
                            error_message=None
                        )
            except Exception as e:
                pass

            # 4. 探測首頁連線（確認是否僅是 DOM 結構無法解析還是完全斷線）
            try:
                t0 = time.time()
                home_resp = await client.get(url)
                latency_ms = round((time.time() - t0) * 1000, 1)

                if home_resp.status_code < 400:
                    # 來源在線，但所有已知爬蟲適配器皆無法解析表格
                    snippet = home_resp.text[:800].strip()
                    error_msg = f"HTTP {home_resp.status_code} 連線成功，但現有適配器 (libgen_li / libgen_is) 均無法解析書目表格，可能結構變更或有反爬機制。"
                    
                    report = LibgenMirrorValidationReport(
                        url=url,
                        is_online=True,
                        status_code=home_resp.status_code,
                        latency_ms=latency_ms,
                        validation_status="incompatible_layout",
                        adapter_type="unknown",
                        sample_records_count=0,
                        error_message=error_msg
                    )

                    if auto_dispatch_br:
                        # dispatch_br 內含同步 file_path.write_text()（:238）。
                        br_id, br_path = await asyncio.to_thread(
                            self.dispatch_br, url, home_resp.status_code, snippet, error_msg)
                        report.br_id = br_id
                        report.br_path = str(br_path)
                        report.dispatched_br = True

                    return report
                else:
                    return LibgenMirrorValidationReport(
                        url=url,
                        is_online=False,
                        status_code=home_resp.status_code,
                        latency_ms=latency_ms,
                        validation_status="offline",
                        adapter_type="unknown",
                        sample_records_count=0,
                        error_message=f"來源伺服器回應異常狀態碼 HTTP {home_resp.status_code}"
                    )
            except Exception as net_err:
                latency_ms = round((time.time() - start_time) * 1000, 1)
                return LibgenMirrorValidationReport(
                    url=url,
                    is_online=False,
                    status_code=None,
                    latency_ms=latency_ms,
                    validation_status="offline",
                    adapter_type="unknown",
                    sample_records_count=0,
                    error_message=f"連線失敗或逾時: {str(net_err)}"
                )

    async def _test_gateway_mirror(self, client: httpx.AsyncClient, url: str, start_time: float) -> LibgenMirrorValidationReport:
        """探測下載 Gateway (例如 library.lol)。"""
        try:
            t0 = time.time()
            resp = await client.get(url)
            latency_ms = round((time.time() - t0) * 1000, 1)

            if resp.status_code < 400:
                return LibgenMirrorValidationReport(
                    url=url,
                    is_online=True,
                    status_code=resp.status_code,
                    latency_ms=latency_ms,
                    validation_status="verified",
                    adapter_type="direct_gateway",
                    sample_records_count=1,
                    error_message=None
                )
            else:
                return LibgenMirrorValidationReport(
                    url=url,
                    is_online=False,
                    status_code=resp.status_code,
                    latency_ms=latency_ms,
                    validation_status="offline",
                    adapter_type="direct_gateway",
                    sample_records_count=0,
                    error_message=f"Gateway 回應 HTTP {resp.status_code}"
                )
        except Exception as e:
            return LibgenMirrorValidationReport(
                url=url,
                is_online=False,
                status_code=None,
                latency_ms=round((time.time() - start_time) * 1000, 1),
                validation_status="offline",
                adapter_type="direct_gateway",
                sample_records_count=0,
                error_message=f"Gateway 連線逾時: {str(e)}"
            )

    def dispatch_br(self, url: str, status_code: Optional[int], html_snippet: str, failure_reason: str) -> Tuple[str, Path]:
        """自動生成結構化 Bug Report (BR) 檔案並寫入 repo 之 issues/ 目錄。"""
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y%m%d_%H%M%S")
        clean_domain = re.sub(r"[^a-zA-Z0-9]", "_", url.replace("https://", "").replace("http://", ""))
        br_id = f"BR-{date_str}-{clean_domain}"
        file_path = self.issues_dir / f"{br_id}.md"

        safe_html = html_snippet[:1200].replace("```", "'''")

        content = f"""# [{br_id}] Libgen 鏡像解析適配器缺失 / 結構不相容報告

- **回報日期**: {now.isoformat()}
- **目標鏡像**: `{url}`
- **HTTP 狀態碼**: `{status_code or 'Unknown'}`
- **驗證狀態**: `incompatible_layout`
- **問題分類**: `Crawler Adapter Incompatibility`

---

## 1. 問題症狀 (Symptom)
系統對自訂或更新之 Libgen 來源 `{url}` 執行上線前預檢驗證（Pre-flight Validation）時，伺服器連線正常（HTTP {status_code}），但現有之 `libgen_li`（9欄式）與 `libgen_is`（10欄式）爬蟲適配器均無法成功提取出有效的書籍 MD5 與中繼資料表格。

## 2. 失敗原因 (Failure Reason)
```text
{failure_reason}
```

## 3. 回應 HTML 結構切片 (DOM Signature Snippet)
```html
{safe_html}
```

## 4. 處置與建議處置 (Next Actions)
1. **防護隔離**: 該鏡像已自動被標記為 `incompatible_layout` 並暫停參與正式搜尋與下載佇列，避免污染檢索品質。
2. **適配器開發**: 開發團隊 / 維護 Agent 需檢視上方 HTML 特徵，於 `app/crawler/libgen_live.py` 或 `app/crawler/validator.py` 新增針對該網域的專屬 BeautifulSoup 解析函數。
3. **重新驗證**: 適配器部署完成後，於系統設定齒輪頁再次點選「⚡ 驗證」，通過後即可自動恢復正式上線。

---
*Generated automatically by openshelf Pre-flight Mirror Validator.*
"""
        file_path.write_text(content, encoding="utf-8")
        return br_id, file_path
