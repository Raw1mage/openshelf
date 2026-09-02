"""OpenStax provider（DD-3 延伸；tasks.md 4.1-4.2）。

第三個 `remote_catalog_item` 來源。與前兩個來源的**關鍵差異是授權模型**：

- Gutenberg：整個來源固定一句 `"Public domain in the USA."`（`SOURCE_LICENSE_LABEL`）。
- OpenStax：**逐本不同**。2026-09-02 實測 129 本 → 3 種值：
  `Creative Commons Attribution-NonCommercial-ShareAlike License` 72 本、
  `Creative Commons Attribution License` 46 本、**`null` 11 本**。

那 11 本 null 是**來源未宣告**，不是抓取缺陷。寫 NULL 才是正確的——套用任何
預設值都等於替出版方做了它沒做的聲明。design.md 明寫「不得套用全域授權假設」。

**API 契約（2026-09-02 實測，非憑記憶）**

    GET https://openstax.org/apps/cms/api/v2/pages/
        ?type=books.Book&limit=100&offset=<n>&fields=<逗號分隔欄位>

實測結果：
- 無 `fields=`：HTTP 200、`meta.total_count:129`，但 items 只有
  `id` / `meta` / `title` 三個 key，**沒有 `license_name`**。
- `fields=title,license_name`：**HTTP 200**，且真的帶回 `license_name`。
- `fields=zzz_not_a_field`：**HTTP 400** `{"message": "unknown fields: zzz_not_a_field"}`。
- `fields=*`：HTTP 200、77 個欄位、3.6 MB。
- `limit=100&offset=100`：HTTP 200、回 29 筆（129 - 100），分頁成立。

⚠ **派工單記載的「加 `&fields=` 會 400，必須取得整份 payload」與實測不符。**
400 只發生在 `fields=` 帶了 API 不認得的**欄位名**，不是「帶 fields 就 400」。
而且反過來——不帶 `fields=` 根本拿不到 `license_name`，本任務的核心要求
（逐本授權）在不帶 `fields=` 的情況下**做不到**。故此處明確使用 `fields=`，
並把欄位清單收斂在 `OPENSTAX_FIELDS` 一個常數裡：欄位名寫錯會在第一次請求
就 400 炸掉（fail fast），而不是安靜地少一欄。

`fields=*` 是能拿到 license_name 的另一條路，但要付 3.6 MB / 77 欄的代價去換
5 個欄位，且未來新增欄位會靜默改變 payload 大小。故不採用。

**Outcome codomain**（errors.md `openstax_provider.refresh`）：

- `ok`     —— 分頁拉完且產出 >= 1 本（`OpenStaxFetchResult.outcome`）。
- `empty`  —— API 回 200 但 0 本。
- `failed` —— 下載/解析失敗、HTTP 非 2xx ⇒ `raise OpenStaxFetchError`。
- `not-run` / `indeterminate` 見 errors.md（前者由 refresher 排放，後者為 n/a）。

判準①：**沿用 Gutenberg 的「例外 vs 回傳值」型別分離**，不用 OL 的欄位互斥。
理由是同一條判準的兩側：OpenStax 抓取失敗**應該**中止該次刷新（它是主資料
來源，失敗代表這批書目沒拿到），OL 失敗**不應該**（它只是補充欄位）。判準
不是「哪個寫法好看」，是「失敗要不要阻斷主流程」——這裡的答案與 Gutenberg
相同，故做法也相同。
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import anyio
import httpx

from app.crawler.libgen_live import make_work_id

log = logging.getLogger(__name__)


OPENSTAX_SOURCE = "openstax"

OPENSTAX_API_URL = "https://openstax.org/apps/cms/api/v2/pages/"

# 只取需要的欄位。**每一個名字都必須是 API 認得的**——未知欄位名會讓整個請求
# 400（實測 `{"message": "unknown fields: zzz_not_a_field"}`），這是 fail fast
# 而非缺陷：欄位打錯會在第一次請求就炸，不會安靜地少一欄。
OPENSTAX_FIELDS = "title,license_name,book_state,is_ally_content,webview_link"

# 129 本書、每頁 100 筆 = 2 次請求。OpenStax 沒有明文 rate limit 警告，量本身
# 也極小；上限 2 比照 Gutenberg（形狀源自 download_worker.py:39）。
_HTTP_LIMITER = anyio.CapacityLimiter(2)

OPENSTAX_PAGE_SIZE = 100

# 硬上限：129 本 → 2 頁。設 20 頁是為了讓 API 若哪天回一個永遠有 next 的
# 壞分頁時，迴圈會停而不是無限打下去。
OPENSTAX_MAX_PAGES = 20


class OpenStaxFetchError(RuntimeError):
    """errors.md `OPENSTAX_FETCH_FAILED`。

    與 Gutenberg 同構：失敗走例外而不是回空 list。若失敗也回空 list，
    呼叫端就無法把「拓到了但 0 本」與「根本沒拓到」分開。
    """

    code = "OPENSTAX_FETCH_FAILED"


@dataclass
class OpenStaxFetchResult:
    """一次全量抓取的結果。`outcome` 只會是 `"ok"` 或 `"empty"`。"""

    outcome: str
    items: List[Dict[str, Any]] = field(default_factory=list)
    pages_fetched: int = 0
    total_count: Optional[int] = None


def parse_books_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把一頁 books.Book 回應解析成 `upsert_batch()` 吃的 item dict。

    實測形狀（2026-09-02）：
    `{"meta": {"total_count": 129}, "items": [{"id": 873, "title": "...",
      "license_name": "...", "meta": {"slug": "...", "html_url": "..."}}]}`

    `source_native_id` 取 **`id`**（數字）而非 slug：實測 129 本的 id 與 slug
    各自都唯一（129/129），但 slug 含非 ASCII（`cálculo-volumen-1`）且是
    可重命名的展示層識別符；id 是 CMS 主鍵，更適合當 identity。

    **`license_name` 一律逐本帶出，未宣告就是 None**——不在這裡填預設值。
    """
    items: List[Dict[str, Any]] = []
    for row in payload.get("items") or []:
        native_id = row.get("id")
        if native_id in (None, ""):
            continue
        native_id = str(native_id)
        meta = row.get("meta") or {}
        slug = (meta.get("slug") or "").strip() or None
        title = (row.get("title") or "").strip()
        # 未宣告授權 → None。空字串也歸 None，否則 "" 與 null 會在下游
        # 讀起來不同但意思相同（兩個值一個意思 = 另一種輸出共用）。
        license_name = (row.get("license_name") or "").strip() or None
        external_url = (meta.get("html_url") or "").strip() or None
        if not external_url and slug:
            external_url = f"https://openstax.org/details/books/{slug}"
        items.append(
            {
                "source": OPENSTAX_SOURCE,
                "source_native_id": native_id,
                # OpenStax 不提供 md5（同 Gutenberg）。去重靠複合鍵。
                "md5": None,
                "title": title or "未知書名",
                "authors_display": None,
                "publication_year": None,
                "language": (meta.get("locale") or "").strip() or None,
                "format": "pdf",
                "extension": "pdf",
                "size_bytes": None,
                "license_name": license_name,
                "work_id": make_work_id(OPENSTAX_SOURCE, native_id),
                "mirror_links": [external_url] if external_url else [],
                "download_protocol": "http",
            }
        )
    return items


class OpenStaxProvider:
    """分頁拉完 OpenStax CMS API 的 books.Book。"""

    def __init__(
        self,
        *,
        api_url: str = OPENSTAX_API_URL,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        page_size: int = OPENSTAX_PAGE_SIZE,
        max_pages: int = OPENSTAX_MAX_PAGES,
        max_attempts: int = 3,
        backoff_base_seconds: float = 1.0,
        timeout_seconds: float = 60.0,
        sleep: Optional[Callable[[float], Any]] = None,
        limiter: Optional[anyio.CapacityLimiter] = None,
    ):
        self.api_url = api_url
        self.transport = transport
        self.page_size = page_size
        self.max_pages = max_pages
        self.max_attempts = max(1, max_attempts)
        self.backoff_base_seconds = backoff_base_seconds
        self.timeout_seconds = timeout_seconds
        self._sleep = sleep or asyncio.sleep
        self.limiter = limiter or _HTTP_LIMITER

    async def _get_page(self, offset: int) -> Dict[str, Any]:
        """拿一頁。任何失敗一律 raise，**絕不**回空 dict。

        `fields=` 是必需的（不帶就拿不到 license_name，見模組 docstring）。
        若 `OPENSTAX_FIELDS` 寫了 API 不認得的名字，這裡會在 400 上 fail fast。
        """
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                async with self.limiter:
                    async with httpx.AsyncClient(
                        transport=self.transport,
                        timeout=self.timeout_seconds,
                        headers={"User-Agent": "OpenShelf/0.1 (+catalog sync)"},
                        follow_redirects=True,
                    ) as client:
                        response = await client.get(
                            self.api_url,
                            params={
                                "type": "books.Book",
                                "limit": self.page_size,
                                "offset": offset,
                                "fields": OPENSTAX_FIELDS,
                            },
                        )
                # 明確 gate HTTP status：400 的 body 是
                # `{"message": "unknown fields: ..."}`，它是合法 JSON。若不 gate，
                # 下面的 `payload.get("items")` 會安靜地拿到空並回報 empty——
                # 那正是把 failed 降級成 empty。
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # noqa: BLE001 - 轉譯為單一具名失敗態
                last_error = exc
                log.warning(
                    "OpenStax fetch attempt %s/%s failed (offset=%s): %s",
                    attempt,
                    self.max_attempts,
                    offset,
                    exc,
                )
                if attempt < self.max_attempts:
                    await self._sleep(self.backoff_base_seconds * (2 ** (attempt - 1)))
        raise OpenStaxFetchError(
            f"{OpenStaxFetchError.code}: {self.api_url} offset={offset} failed "
            f"after {self.max_attempts} attempts ({last_error})"
        ) from last_error

    async def fetch_books(self) -> OpenStaxFetchResult:
        """分頁拉完全部 books.Book。回 `ok` 或 `empty`；失敗走例外。"""
        collected: List[Dict[str, Any]] = []
        pages = 0
        total_count: Optional[int] = None
        offset = 0
        while pages < self.max_pages:
            payload = await self._get_page(offset)
            pages += 1
            meta = payload.get("meta") or {}
            if total_count is None:
                raw_total = meta.get("total_count")
                total_count = int(raw_total) if raw_total is not None else None
            try:
                batch = parse_books_payload(payload)
            except Exception as exc:  # noqa: BLE001
                raise OpenStaxFetchError(
                    f"{OpenStaxFetchError.code}: payload parse failed ({exc})"
                ) from exc
            raw_rows = len(payload.get("items") or [])
            collected.extend(batch)
            # 終止條件用**原始筆數**而非解析後筆數：若某頁全數被過濾（例如
            # 全部缺 id），用解析後筆數會讓分頁提前停下而静默漏掉後面的頁。
            if raw_rows < self.page_size:
                break
            offset += self.page_size
            if total_count is not None and offset >= total_count:
                break
        return OpenStaxFetchResult(
            outcome="ok" if collected else "empty",
            items=collected,
            pages_fetched=pages,
            total_count=total_count,
        )
