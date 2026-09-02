"""Open Library 橋接層（DD-4/DD-5；tasks.md 3.1-3.3）。

**這不是查詢時的資料來源，是寫入時的一次性 enrich。** OL 官方明文禁止 bulk
harvest 與「hundreds of single-book requests」，未識別的 rate limit 約 1 req/s。
故此模組的每一個設計決定都是為了「少打、不重複打、打失敗也不影響主流程」：

- 只在 `RemoteCatalogRefresher` 的刷新流程**末段**觸發，`upsert_batch()` 早已
  完成（書目已上架），OL 掛掉不 rollback 也不延遲上架。
- **絕不掛在任何 API GET 請求的同步路徑上**（design.md: no synchronous call
  on request path）。`category_routes.py` 完全不引用本模組。
- `CapacityLimiter(1)` —— 比 Gutenberg 的 2 更保守，因為 OL 的警告更明確。
- 同一 catalog_id 在 `ttl_seconds` 內已 enrich 過 ⇒ `not-run`，不重打。

**API 格式（2026-09-02 實測查證，非憑記憶）**：

    GET https://openlibrary.org/search.json
        ?fields=key,title,ia,ebook_access,isbn,oclc,lccn,id_project_gutenberg
        &limit=<n>&q=<lucene 語法查詢>

實測結果：
- `q=isbn:9780553213119` → HTTP 200、`numFound:1`、docs[0] 帶 ia/ebook_access。
- `q=title:moby dick` → HTTP 200、`numFound:1283`（證明不同查詢真的回不同東西，
  不是固定回應）。
- `q=title:zzqqxxjjvvwwkk9987654` → HTTP 200、`numFound:0`、`docs:[]`
  （**真正的 empty 長這樣**）。
- ⚠ `q=isbn:0000000000000` → HTTP 200、`numFound:1`，且該 doc 的 isbn 陣列
  **真的含這串**（`/works/OL45733017W`）。所以「明顯造假的 ISBN」不是可靠的
  empty 判準——OL 收錄了帶該 ISBN 的資料。這一格是實測推翻直覺的地方，記在
  此處以免日後有人用假 ISBN 當空查詢的測試依據。

**Outcome codomain**（errors.md `openlibrary_bridge.enrich`，五態全部在本檔排放）：

- `ok`       —— 回填至少一項橋接欄位（回傳 `OLEnrichResult(outcome="ok")`）。
- `empty`    —— 查詢成功但 OL 無對應記錄（`numFound == 0` 或 docs 全無可用欄位）。
- `failed`   —— HTTP 錯誤、非法 JSON、逾時（`OL_BRIDGE_TIMEOUT`）。
- `not-run`  —— 節流命中，本次跳過。
- `indeterminate` —— errors.md 已宣告 n/a（依 httpx timeout 最終轉 failed 或 ok）。

判準①：`failed` 與 `empty` **不共用輸出**。`failed` 一定帶非 None 的 `error`
字串且 `fields_written == 0`；`empty` 的 `error` 恆為 None。呼叫端不需要（也
不可能）靠「回傳是不是空的」去猜是哪一種。

**與 Phase 2 的取捨差異（刻意的）**：Gutenberg 用「例外 vs 回傳值」分離
failed/empty，因為那裡的失敗**應該**中止該次刷新。這裡不行——OL 失敗必須被
吞掉不阻斷主流程（技術要求 5），若用例外就得在每個呼叫點包 try，反而讓
「吞掉」變成呼叫端的責任而非模組的保證。故改用**同型別但欄位互斥**的
`OLEnrichResult`，三態各自有可斷言的欄位組合，測試逐條鎖定。
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import anyio
import httpx

log = logging.getLogger(__name__)


OL_SEARCH_URL = "https://openlibrary.org/search.json"

# DD-4 明列的 6 方對映欄位。一次查詢全部帶回，避免為了補欄位而二次請求。
OL_FIELDS = "key,title,ia,ebook_access,isbn,oclc,lccn,id_project_gutenberg"

# OL 的警告比 Gutenberg 更明確（"hundreds of single-book requests" 被點名），
# 故併發上限取 1——形狀比照 download_worker.py:39，數值更保守。
_OL_LIMITER = anyio.CapacityLimiter(1)

# 同一 catalog_id 的重查間隔預設值。橋接欄位變動極慢（ISBN/OCLC/LCCN 是
# 書目事實不是庫存狀態），一天一次遠比需要的還頻繁。
DEFAULT_TTL_SECONDS = 86400


@dataclass
class OLEnrichResult:
    """一次 enrich 的結果。三態由**欄位組合**區分，不共用輸出：

    | outcome  | error       | fields_written | queried |
    | -------- | ----------- | -------------- | ------- |
    | ok       | None        | >= 1           | True    |
    | empty    | None        | 0              | True    |
    | failed   | 非 None 字串 | 0              | True    |
    | not-run  | None        | 0              | False   |

    `queried` 是分開 `not-run` 與 `empty` 的那一格——兩者的 fields_written
    都是 0，若只看那個數字就無法分辨「跳過了」與「查了但 OL 沒有」。
    `error` 是分開 `failed` 與 `empty` 的那一格。
    """

    outcome: str
    fields_written: int = 0
    queried: bool = False
    error: Optional[str] = None
    fields: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.fields is None:
            self.fields = {}


def build_query(
    *,
    isbn: Optional[str] = None,
    title: Optional[str] = None,
    authors: Optional[str] = None,
) -> Optional[str]:
    """組 OL 的 lucene 查詢字串。沒有任何可用線索時回 None。

    優先序：ISBN（唯一性最高）> title+author > title。回 None 代表
    「這筆沒有可查的東西」，呼叫端應視為 not-run 而非 failed——
    沒有線索不是 OL 的錯，也不該花一次請求去確認。
    """
    if isbn:
        cleaned = "".join(ch for ch in isbn if ch.isalnum())
        if cleaned:
            return f"isbn:{cleaned}"
    title = (title or "").strip()
    if not title or title == "未知書名":
        return None
    authors = (authors or "").strip()
    if authors:
        return f"title:{title} author:{authors}"
    return f"title:{title}"


def extract_bridge_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    """從 OL 回應抽出橋接欄位。無命中時回空 dict。

    OL 的 isbn/oclc/lccn 都是**陣列**（DD-1 已記載：非一對一，所以它們
    不能當跨來源主鍵）。此處取第一個值存成橋接用，不假裝它具唯一性。
    """
    docs: List[Dict[str, Any]] = payload.get("docs") or []
    if not docs:
        return {}
    doc = docs[0]

    def first(key: str) -> Optional[str]:
        value = doc.get(key)
        if isinstance(value, list):
            return str(value[0]) if value else None
        return str(value) if value not in (None, "") else None

    fields = {
        "ol_key": first("key"),
        "isbn": first("isbn"),
        "oclc": first("oclc"),
        "lccn": first("lccn"),
        "gutenberg_id": first("id_project_gutenberg"),
    }
    return {k: v for k, v in fields.items() if v}


class OpenLibraryBridge:
    """寫入時的一次性 enrich。**絕不在查詢請求路徑上呼叫**。"""

    def __init__(
        self,
        dao: Any,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        timeout_seconds: float = 20.0,
        min_interval_seconds: float = 1.0,
        limiter: Optional[anyio.CapacityLimiter] = None,
        sleep: Optional[Callable[[float], Any]] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.dao = dao
        self.transport = transport
        self.ttl_seconds = ttl_seconds
        self.timeout_seconds = timeout_seconds
        # OL 未識別 rate limit 約 1 req/s（DD-4）。兩次請求間至少隔這麼久。
        self.min_interval_seconds = min_interval_seconds
        self.limiter = limiter or _OL_LIMITER
        self._sleep = sleep or asyncio.sleep
        self._clock = clock or time.monotonic
        self._last_request_at: Optional[float] = None

    async def _throttled_get(self, query: str) -> httpx.Response:
        """單一併發 + 最小間隔的 GET。任何失敗直接往上拋，由 enrich 包成 failed。"""
        async with self.limiter:
            if self._last_request_at is not None:
                elapsed = self._clock() - self._last_request_at
                remaining = self.min_interval_seconds - elapsed
                if remaining > 0:
                    await self._sleep(remaining)
            self._last_request_at = self._clock()
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout_seconds,
                headers={"User-Agent": "OpenShelf/0.1 (+catalog bridge)"},
                follow_redirects=True,
            ) as client:
                return await client.get(
                    OL_SEARCH_URL,
                    params={"fields": OL_FIELDS, "limit": 1, "q": query},
                )

    async def enrich_item(self, item: Dict[str, Any]) -> OLEnrichResult:
        """對單一 catalog item 做一次 enrich。**不拋例外**。

        技術要求 5：OL 失敗不得阻斷主流程。故所有錯誤在此轉譯成
        `outcome="failed"` 的回傳值，呼叫端不需要包 try——把「吞掉」做成
        模組的保證而不是呼叫端的責任。這與 Phase 2 的 Gutenberg 選了相反
        的做法，是因為那邊的失敗**應該**中止該次刷新，這邊不應該。
        """
        catalog_id = item.get("catalog_id")

        # not-run ①：節流命中（TTL 內已 enrich 過）。
        enriched_at = item.get("ol_enriched_at")
        if enriched_at and not self._is_stale(enriched_at):
            return OLEnrichResult(outcome="not-run", queried=False)

        query = build_query(
            isbn=item.get("isbn"),
            title=item.get("title"),
            authors=item.get("authors_display"),
        )
        # not-run ②：沒有任何可查的線索，不花請求去確認。
        if not query:
            return OLEnrichResult(outcome="not-run", queried=False)

        try:
            response = await self._throttled_get(query)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            # errors.md OL_BRIDGE_TIMEOUT：log warning、欄位保留空白、不阻斷寫入。
            log.warning("OL_BRIDGE_TIMEOUT: %s (%s)", query, exc)
            return OLEnrichResult(
                outcome="failed", queried=True, error=f"OL_BRIDGE_TIMEOUT: {exc}"
            )
        except json.JSONDecodeError as exc:
            log.warning("OL bridge got invalid JSON for %s: %s", query, exc)
            return OLEnrichResult(
                outcome="failed", queried=True, error=f"invalid JSON: {exc}"
            )
        except Exception as exc:  # noqa: BLE001 - 一律吞掉，不阻斷主寫入
            log.warning("OL bridge request failed for %s: %s", query, exc)
            return OLEnrichResult(outcome="failed", queried=True, error=str(exc))

        fields = extract_bridge_fields(payload)
        if not fields:
            # empty：查詢成功但 OL 無對應記錄。error 恆為 None，這就是它與
            # failed 的分界——兩者的 fields_written 都是 0，但 error 不同。
            # 仍記下 enriched_at：這是一次**有效的**查詢，不該下一輪再打。
            if catalog_id is not None:
                await asyncio.to_thread(self.dao.mark_ol_enriched, catalog_id, {})
            return OLEnrichResult(outcome="empty", queried=True)

        if catalog_id is not None:
            await asyncio.to_thread(self.dao.mark_ol_enriched, catalog_id, fields)
        return OLEnrichResult(
            outcome="ok",
            fields_written=len(fields),
            queried=True,
            fields=fields,
        )

    def _is_stale(self, enriched_at: str) -> bool:
        """TTL 判定。無法解析的時間戳一律視為 stale（寫壞了就重查，不是永不重查）。"""
        from datetime import datetime, timezone

        try:
            then = datetime.fromisoformat(enriched_at)
        except (TypeError, ValueError):
            return True
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - then).total_seconds()
        return age > self.ttl_seconds

    async def enrich_category(
        self, category_id: str, *, max_items: int = 25
    ) -> Dict[str, int]:
        """對一個分類內尚未 enrich 的 item 逐筆跑一次，回傳各 outcome 的計數。

        回傳 dict 而非單一狀態：一批裡十分常見 ok/empty/failed 同時發生，
        塵縮成一個值就是把三態塵回去。`max_items` 是硬上限，OL 明文禁
        「hundreds of single-book requests」。
        """
        pending = await asyncio.to_thread(
            self.dao.list_items_needing_ol_enrichment, category_id, max_items
        )
        counts = {"ok": 0, "empty": 0, "failed": 0, "not-run": 0}
        for item in pending:
            result = await self.enrich_item(item)
            counts[result.outcome] = counts.get(result.outcome, 0) + 1
        return counts
