"""Project Gutenberg catalog provider（DD-3；tasks.md 2.1 / 2.5）。

第一個非 libgen 的 `remote_catalog_item` 來源，用途是**驗證 Phase 1 的多來源
identity 抽象是否真的成立**：本 provider 產出的 item 完全沒有 md5，只靠
`(source="gutenberg", source_native_id=<Gutenberg Text#>)` 去重。

**授權不是全球公版**（design.md Risks）：PG 的 `dcterms:rights` 逐字是
`"Public domain in the USA."`，只在美國境內為公版。故此處不得用「公版 /
public domain」這類泛稱，字面值集中定義在 `app.models.catalog
.SOURCE_LICENSE_LABEL`，read path 與 provider 共用同一份 SSOT。

**Outcome codomain**（errors.md `gutenberg_provider.refresh`，本檔負責其中兩態）：

- `ok`    —— CSV 取得且產出 >= 1 筆 item（`GutenbergFetchResult.outcome`）。
- `empty` —— CSV 取得成功但產出 0 筆（同上，值不同）。
- `failed` / `not-run` 由 `remote_catalog_refresh.py` 排放，見該檔。

`empty` 與 `failed` **不得共用同一個輸出**（errors.md 判準①）：下載/解析失敗
一律 `raise GutenbergFetchError`，成功但 0 筆一律 `return outcome="empty"`。
兩者型別就不同，呼叫端不可能把其中一個誤讀成另一個——這正是本包要修的
「缺席態與失敗態共用輸出」病灶在新 provider 上的預防。
"""

import asyncio
import csv
import io
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import anyio
import httpx

from app.crawler.libgen_live import make_work_id
from app.models.catalog import SOURCE_LICENSE_LABEL

log = logging.getLogger(__name__)


GUTENBERG_SOURCE = "gutenberg"

# 官方 catalog CSV（design.md DD-3：robot/harvest 官方三例外之一）。
# 2026-09-02 實證：HTTP/2 200、content-type: text/csv、content-length
# 21,196,613、last-modified Sun, 30 Aug 2026。同 host 的不存在路徑回 404
# （負向控制），故 200 不是「什麼都回 200」的假象。
GUTENBERG_CATALOG_URL = "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv"

GUTENBERG_LICENSE = SOURCE_LICENSE_LABEL[GUTENBERG_SOURCE]

# 保守節流（tasks.md 2.5）。PG 沒有公布 req/s 數字，只定性說明「濫抓會封 IP」，
# 故比照 `download_worker.py:39` 的 `anyio.CapacityLimiter` 形狀取一個明顯保守
# 的併發上限，並搭配指數退避。上限 2 是刻意的：catalog 是單一大檔，併發再高
# 也不會更快，只會更接近被封的門檻。
_HTTP_LIMITER = anyio.CapacityLimiter(2)


class GutenbergFetchError(RuntimeError):
    """errors.md `GUTENBERG_FETCH_FAILED`：CSV 下載/解析失敗。

    **刻意用例外而不是回傳空 list**：若失敗也回空 list，呼叫端就無法把
    「抓到了但 0 筆」與「根本沒抓到」分開——那是本包明列要防的缺陷。
    """

    code = "GUTENBERG_FETCH_FAILED"


@dataclass
class GutenbergFetchResult:
    """一次 catalog 抓取的完整結果。

    `outcome` 只會是 `"ok"` 或 `"empty"`；`failed` 走例外，不佔用這個欄位。
    """

    outcome: str
    items: List[Dict[str, Any]] = field(default_factory=list)
    rows_scanned: int = 0
    attempts: int = 0


def parse_catalog_csv(text: str, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """把官方 pg_catalog.csv 解析成 `upsert_batch()` 吃的 item dict。

    欄位（2026-09-02 實查表頭逐字）：
    `Text#,Type,Issued,Title,Language,Authors,Subjects,LoCC,Bookshelves`。

    只收 `Type == "Text"`（PG 另有 Sound/Image/Dataset，本書庫不收）。
    `source_native_id` 取 `Text#`——design.md 已明寫同一本書在 PG 可能有多個
    ID，收斂到 FRBR work 層是 Non-Goal，任選其一即可。
    """
    rows = csv.DictReader(io.StringIO(text))
    items: List[Dict[str, Any]] = []
    for row in rows:
        native_id = (row.get("Text#") or "").strip()
        if not native_id or not native_id.isdigit():
            continue
        if (row.get("Type") or "").strip() != "Text":
            continue
        title = (row.get("Title") or "").replace("\r\n", " ").replace("\n", " ").strip()
        issued = (row.get("Issued") or "").strip()
        year: Optional[int] = None
        if len(issued) >= 4 and issued[:4].isdigit():
            year = int(issued[:4])
        items.append(
            {
                "source": GUTENBERG_SOURCE,
                "source_native_id": native_id,
                # md5 刻意留 None：PG 不提供 md5。這一格為 None 是**設計上的正常
                # 狀態**（errors.md identity.upsert 的 indeterminate n/a 列），
                # 不是缺陷，去重靠複合鍵。
                "md5": None,
                "title": title or "未知書名",
                "authors_display": (row.get("Authors") or "").strip() or None,
                "publication_year": year,
                "language": (row.get("Language") or "").strip() or None,
                "format": "epub",
                "extension": "epub",
                "size_bytes": None,
                "license": GUTENBERG_LICENSE,
                "work_id": make_work_id(GUTENBERG_SOURCE, native_id),
                "mirror_links": [
                    f"https://www.gutenberg.org/ebooks/{native_id}.epub.images"
                ],
                "download_protocol": "http",
            }
        )
        if limit is not None and len(items) >= limit:
            break
    return items


class GutenbergProvider:
    """抓取並解析 PG catalog CSV。

    介面刻意**不**模仿 `LibgenCrawler.search_page()`：PG 沒有分頁搜尋，是一份
    全量 CSV。`RemoteCatalogRefresher` 因此以 provider 種類分派（見該檔），
    而不是硬套 libgen 的分頁協定。
    """

    def __init__(
        self,
        *,
        catalog_url: str = GUTENBERG_CATALOG_URL,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        max_attempts: int = 3,
        backoff_base_seconds: float = 1.0,
        timeout_seconds: float = 120.0,
        limit: Optional[int] = None,
        sleep: Optional[Callable[[float], Any]] = None,
        limiter: Optional[anyio.CapacityLimiter] = None,
    ):
        self.catalog_url = catalog_url
        self.transport = transport
        self.max_attempts = max(1, max_attempts)
        self.backoff_base_seconds = backoff_base_seconds
        self.timeout_seconds = timeout_seconds
        self.limit = limit
        self._sleep = sleep or asyncio.sleep
        self.limiter = limiter or _HTTP_LIMITER

    async def _get_catalog_text(self) -> str:
        """下載 CSV 全文。任何失敗一律 raise，**絕不**回空字串。

        指數退避：第 n 次失敗後睡 `backoff_base * 2**(n-1)` 秒（tasks.md 2.5）。
        併發受 `self.limiter` 節流，測試可用它斷言併發上界。
        """
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_attempts + 1):
            self.attempts = attempt
            try:
                async with self.limiter:
                    async with httpx.AsyncClient(
                        transport=self.transport,
                        timeout=self.timeout_seconds,
                        headers={"User-Agent": "OpenShelf/0.1 (+catalog sync)"},
                        follow_redirects=True,
                    ) as client:
                        response = await client.get(self.catalog_url)
                # 明確 gate HTTP status：非 2xx 的 body 是錯誤頁，不是 catalog。
                # 若不 gate，一份 404 HTML 會被 csv 模組安靜解析成 0 筆，於是
                # `failed` 被降級成 `empty`——正是本包禁止的輸出共用。
                response.raise_for_status()
                return response.text
            except Exception as exc:  # noqa: BLE001 - 轉譯為單一具名失敗態
                last_error = exc
                log.warning(
                    "Gutenberg catalog fetch attempt %s/%s failed: %s",
                    attempt,
                    self.max_attempts,
                    exc,
                )
                if attempt < self.max_attempts:
                    await self._sleep(self.backoff_base_seconds * (2 ** (attempt - 1)))
        raise GutenbergFetchError(
            f"{GutenbergFetchError.code}: {self.catalog_url} unreachable "
            f"after {self.max_attempts} attempts ({last_error})"
        ) from last_error

    async def fetch_catalog(self) -> GutenbergFetchResult:
        """回傳 `ok` 或 `empty`；失敗態走 `GutenbergFetchError`。

        判準①：`empty` 與 `failed` 沒有任何共用的輸出通道——前者是回傳值，
        後者是例外，呼叫端不可能靠檢查「是不是空的」而混淆兩者。
        """
        self.attempts = 0
        text = await self._get_catalog_text()
        try:
            items = parse_catalog_csv(text, limit=self.limit)
        except Exception as exc:  # noqa: BLE001
            raise GutenbergFetchError(
                f"{GutenbergFetchError.code}: catalog parse failed ({exc})"
            ) from exc
        return GutenbergFetchResult(
            outcome="ok" if items else "empty",
            items=items,
            rows_scanned=len(items),
            attempts=self.attempts,
        )
