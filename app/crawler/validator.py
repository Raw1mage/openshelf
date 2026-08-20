import asyncio
import logging
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

log = logging.getLogger(__name__)


class BRDispatchTargetMissing(RuntimeError):
    """BR 落點目錄在寫入當下不存在——部署契約（掛載）失效的具名訊號。

    存在的理由是「具名」：缺目錄時 `write_text` 丟的是裸 `FileNotFoundError`，
    它與「磁碟上某個無關檔案不見了」共用同一個型別，呼叫端無法只針對本情況處置。
    """


class MirrorValidator:
    """Libgen 鏡像來源上線前預檢驗證器（Pre-flight Validator）與自動 BR 發送系統。"""

    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    TEST_QUERY = "Python"
    TEST_MD5 = "00000000000000000000000000000000"

    def __init__(self, issues_dir: Optional[Path] = None):
        # BR 落點有兩種來源，語意完全不同，不可共用同一條建目錄邏輯：
        #
        #   1. 呼叫端「顯式指定」（測試 fixture、一次性工具）——位置是呼叫端自己
        #      挑的，建出來就是它要的意思，照建、不出聲。
        #   2. 走「預設值」——這是正式部署路徑，而這個目錄是**部署契約**的一部分：
        #      容器內 /app/issues 必須由 docker-compose 的 `./issues:/app/issues`
        #      掛載進來。掛載在，目錄必然已存在（docker 在掛載時就備妥）；掛載被
        #      拿掉，目錄就不存在。
        #
        # 原本這裡對兩者一視同仁地 `mkdir(parents=True, exist_ok=True)`，於是第 2
        # 種情況下「掛載不見了」被靜默補成一個容器 rebuild 即蒸發的 ephemeral 目錄：
        # dispatch_br 寫進去的診斷 BR 全部消失、host 端 issues/ 永遠看不到、前端
        # 清單恆為 total=0——「真的沒有 BR」與「BR 寫到別的地方去了」共用同一個輸出，
        # 零錯誤、零 log（BR-20260820_223000 實測）。
        #
        # 判準用「目錄是否已存在」當掛載存在的代理指標，是因為它正是該次失效形狀
        # 的唯一分界線；它的已知盲點記在 dispatch_br 上方。
        explicit = issues_dir is not None
        self.issues_dir = issues_dir or (Path(__file__).parent.parent.parent / "issues")
        self.issues_dir_is_explicit = explicit
        self.issues_dir_missing = False

        if explicit:
            self.issues_dir.mkdir(parents=True, exist_ok=True)
        else:
            # 不建。建了就等於把「掛載失效」這個事實抹掉，之後沒有任何人能區分。
            self.issues_dir_missing = not self.issues_dir.is_dir()
            if self.issues_dir_missing:
                log.error(
                    "BR 落點目錄不存在且不予自動建立：%s。"
                    " 這是部署契約失效訊號——容器內該路徑應由 docker-compose 的"
                    " `./issues:/app/issues` 掛載提供。缺少掛載時自動建出來的目錄會在"
                    " rebuild 時連同所有自動產生的診斷 BR 一起消失，且不會有任何錯誤，"
                    " 前端 /api/settings/libgen-mirrors/issues 會恆為 total=0"
                    " （BR-20260820_223000）。請確認掛載存在後重啟容器。",
                    self.issues_dir,
                )
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
                    await self._try_dispatch_br(
                        report, url, report.status_code,
                        "Gateway 結構變更或無效", report.error_message or "")
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
                        await self._try_dispatch_br(
                            report, url, home_resp.status_code, snippet, error_msg)

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

    async def _try_dispatch_br(
        self,
        report: LibgenMirrorValidationReport,
        url: str,
        status_code: Optional[int],
        html_snippet: str,
        failure_reason: str,
    ) -> None:
        """嘗試落檔診斷 BR，並把結果如實反映到 report 上。

        取捨（本包的核心判斷，明寫理由）：落點失效時**不讓 validate_mirror 整個炸掉**。
        BR 落檔是「診斷副作用」，鏡像驗證才是使用者要的主功能；讓副作用的失敗升級成
        路由 500，等於因為記不了帳就拒絕做生意。但也**不得靜默吞掉**——那正是本 BR 要
        修的病。所以走第三條路：

          - 例外只在此處被接住，不再往上傳（主功能存活）
          - log.error 出聲（可觀測）
          - `dispatched_br` 維持 False、`br_path` 維持 None（欄位如實，不謊報成功）
          - `error_message` 追加一句，讓失效**沿著使用者看得到的通道**浮上來，
            而不只是留在容器 log 裡等人去翻

        最後一項是關鍵：只寫 log 的話，前端仍然只看到「BR 清單是空的」，跟修好之前
        的症狀一模一樣。要讓「沒有 BR」與「BR 寫不進去」不再共用同一個輸出，訊號
        必須出現在同一個回應裡。

        `report.br_path` 的語意未改變：成功時仍是寫入的檔案路徑字串，失敗時仍是
        既有的預設 None——本函式不會塞任何新形態的值進去。
        """
        try:
            br_id, br_path = await asyncio.to_thread(
                self.dispatch_br, url, status_code, html_snippet, failure_reason)
        except BRDispatchTargetMissing as exc:
            log.error("診斷 BR 落檔失敗（鏡像驗證結果仍照常回傳）：%s", exc)
            note = f"（另注意：診斷 BR 無法寫入，BR 落點目錄不存在——{exc}）"
            report.error_message = f"{report.error_message or ''}{note}"
            report.dispatched_br = False
            return

        report.br_id = br_id
        report.br_path = str(br_path)
        report.dispatched_br = True

    def dispatch_br(self, url: str, status_code: Optional[int], html_snippet: str, failure_reason: str) -> Tuple[str, Path]:
        """自動生成結構化 Bug Report (BR) 檔案並寫入 repo 之 issues/ 目錄。

        落點不存在時 raise `BRDispatchTargetMissing`，**不自動建目錄**。

        為什麼寫入當下要再檢一次，而不是靠 `__init__` 的檢查就好：`__init__` 只
        看得到「建構那一刻」。validator 由 FastAPI 每次請求重新建構（settings_routes
        的 `get_validator`），但掛載仍可能在建構後、寫入前消失，而且更關鍵的是——
        `__init__` 的 log 只是「說了」，若這裡照樣寫下去，寫入行為本身仍然是靜默的。
        兩處都要，因為它們證明的是不同的事：一個是「啟動時契約就已失效」，一個是
        「這一次寫入確實沒有落到持久位置」。

        已知盲點（明寫，不假裝覆蓋）：本檢查只能分辨「目錄不存在」。若掛載存在但
        指向錯的 host 路徑、或掛載被換成另一個 ephemeral volume，目錄照樣存在，
        這裡看不出來——那一格由 tests/test_container_mount_contract.py 的
        compose↔code 一致性鎖負責，不是本函式的職責。
        """
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y%m%d_%H%M%S")
        clean_domain = re.sub(r"[^a-zA-Z0-9]", "_", url.replace("https://", "").replace("http://", ""))
        br_id = f"BR-{date_str}-{clean_domain}"
        file_path = self.issues_dir / f"{br_id}.md"

        if not self.issues_dir.is_dir():
            log.error(
                "BR 落點目錄不存在，放棄寫入 %s。自動建立會讓這份診斷 BR 落進 rebuild"
                " 即消失的 ephemeral 目錄，而呼叫端會收到一個看起來成功的路徑"
                " （BR-20260820_223000）。",
                file_path,
            )
            raise BRDispatchTargetMissing(
                f"BR 落點目錄不存在，拒絕自動建立：{self.issues_dir}。"
                " 預期它由部署掛載提供（容器內為 docker-compose 的"
                " `./issues:/app/issues`）。"
            )

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
