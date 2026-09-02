import asyncio
import logging
from typing import Dict, Optional

log = logging.getLogger(__name__)

from app.crawler.gutenberg_provider import (
    GUTENBERG_SOURCE,
    GutenbergFetchError,
    GutenbergProvider,
)
from app.crawler.libgen_live import LibgenCrawler
from app.crawler.openlibrary_bridge import OpenLibraryBridge
from app.crawler.openstax_provider import OpenStaxFetchError, OpenStaxProvider
from app.db.remote_catalog import RemoteCatalogDAO


class RemoteCatalogRefresher:
    _tasks: Dict[str, asyncio.Task] = {}

    def __init__(
        self,
        dao: RemoteCatalogDAO,
        crawler: LibgenCrawler,
        *,
        page_size: int = 25,
        max_pages: int = 10,
        gutenberg: Optional[GutenbergProvider] = None,
        openstax: Optional[OpenStaxProvider] = None,
        ol_bridge: Optional[OpenLibraryBridge] = None,
        ol_max_items: int = 25,
    ):
        self.dao = dao
        self.crawler = crawler
        self.page_size = page_size
        self.max_pages = max_pages
        # OL 橋接（DD-4, Phase 3）。掛在**刷新流程末段**，不是查詢路徑——
        # `category_routes.py` 完全不引用 OpenLibraryBridge，那是這條約束
        # 唯一真正的保證（design.md: no synchronous call on request path）。
        self.ol_bridge = ol_bridge
        self.ol_max_items = ol_max_items
        # provider 調度（tasks.md 2.3）：不再假設只有 libgen。刻意用具名參數而非
        # 泛用 provider registry——design.md Non-Goals 明寫「不為未驗證的未來
        # 來源預先抽象」。Gutenberg 是第二個來源，兩個具名欄位就夠；真的接到
        # 第四、第五個時再抽象，那時才知道該抽象成什麼形狀。
        self.gutenberg = gutenberg
        # Phase 4：第三個來源，沿用 Phase 2 的具名參數模式。到這裡已經三個
        # provider，仍不抽泛用 registry——design.md Non-Goals 明寫不為未驗證的
        # 未來來源預先抽象，而且三條路徑的**抓取協定彼此不同**（libgen 分頁
        # 搜尋 / Gutenberg 全量 CSV / OpenStax 分頁 JSON API），硬抽成同一個
        # 介面只會逗出一層假抽象。
        self.openstax = openstax

    async def _enrich_after_write(self, category_id: str) -> Optional[Dict[str, int]]:
        """刷新寫入完成後的一次性 OL enrich。回傳各 outcome 計數，未設定則 None。

        **一定在 upsert 之後、且不影響刷新結果**：書目此刻已上架，OL 整段掛掉
        也只是這裡回一組 failed 計數，refresh row 已經是 fresh 了。`enrich_item()`
        本身不拋例外（見該處注解），故此處不需要 try——但仍保留一層，因為
        `list_items_needing_ol_enrichment` 的 DB 讀是可能失敗的，而那同樣不該
        讓一次成功的刷新變成失敗。
        """
        if self.ol_bridge is None:
            return None
        try:
            return await self.ol_bridge.enrich_category(
                category_id, max_items=self.ol_max_items
            )
        except Exception as exc:  # noqa: BLE001 - enrich 永不阻斷主流程
            log.warning("OL enrich pass failed for %s: %s", category_id, exc)
            return None

    def schedule(self, category_id: str, query_term: str) -> bool:
        key = f"{self.dao.engine.db_path}:{category_id}"
        existing = self._tasks.get(key)
        if existing and not existing.done():
            return False
        task = asyncio.create_task(self.refresh(category_id, query_term))
        self._tasks[key] = task

        def clear_completed(done: asyncio.Task) -> None:
            if self._tasks.get(key) is done:
                self._tasks.pop(key, None)

        task.add_done_callback(clear_completed)
        return True

    async def refresh_gutenberg(self, category_id: str, query_term: str) -> str:
        """Gutenberg provider 的刷新路徑（tasks.md 2.3）。

        **與 libgen 路徑並存，不改寫它**：`refresh()` 逐字保持原行為，本方法是
        另一條獨立的調度路徑。PG 不是分頁搜尋而是一份全量 CSV，硬套 libgen 的
        `search_page()` 協定只會逼出一個假分頁層。

        回傳 errors.md `gutenberg_provider.refresh` 的 outcome 值，四態各自
        獨立、**不共用回傳值也不共用 log 訊息**：

        - `not-run`  —— 未設定 provider（本輪跳過，沒有任何抓取動作發生）。
        - `failed`   —— `GutenbergFetchError`（下載/解析失敗），舊 rows 保留不刪。
        - `empty`    —— CSV 取得成功但 0 筆（異常但不是錯誤，refresh 記為成功）。
        - `ok`       —— 取得且寫入 >= 1 筆。

        判準①：`empty` 與 `failed` 在此處走的是**不同分支、不同 refresh
        status、不同 error_message**——`empty` 的 refresh row 是
        `status='fresh'` 且 `error_message IS NULL`，`failed` 的是
        `status='failed'` 且 error 逐字含 `GUTENBERG_FETCH_FAILED`。測試可以
        分別鎖定這兩組，任一被誤判成另一個都會紅。
        """
        if self.gutenberg is None:
            # not-run：沒有 provider，什麼都沒跑。刻意不開 refresh row——開一筆
            # 空的成功紀錄會讓「沒跑」長得像「跑了沒東西」。
            return "not-run"

        refresh_id = await asyncio.to_thread(
            self.dao.begin_refresh, category_id, query_term
        )
        try:
            result = await self.gutenberg.fetch_catalog()
        except GutenbergFetchError as exc:
            await asyncio.to_thread(
                self.dao.finish_refresh,
                refresh_id,
                success=False,
                pages_fetched=0,
                items_seen=0,
                items_added=0,
                items_updated=0,
                error_message=str(exc),
            )
            return "failed"

        added, updated, rejected = await asyncio.to_thread(
            self.dao.upsert_batch, category_id, query_term, result.items
        )
        if rejected:
            # identity.upsert 的 not-run 在這裡浮上來（VANS 5-A）。provider 契約
            # 缺失是上游缺陷，不是本次刷新失敗，故不改 refresh status；但也不得
            # 讓它完全無聲——items_seen 與 added+updated 的落差在這裡有名字。
            log.warning(
                "gutenberg refresh: %d 筆缺 source_native_id 被拒絕寫入 (category=%s)",
                rejected,
                category_id,
            )
        await asyncio.to_thread(
            self.dao.finish_refresh,
            refresh_id,
            success=True,
            pages_fetched=1,
            items_seen=len(result.items),
            items_added=added,
            items_updated=updated,
        )
        # 寫入完成、refresh row 已定案之後才 enrich（DD-4）。順序是契約的一部分：
        # OL 是寫入時的一次性補充，不是上架的前置條件。
        await self._enrich_after_write(category_id)
        # provider 已經把 ok/empty 分開了，這裡逐字沿用，不重新判斷一次——
        # 兩處各自判斷會製造第二個真相來源。
        return result.outcome

    async def refresh_openstax(self, category_id: str, query_term: str) -> str:
        """OpenStax provider 的刷新路徑（tasks.md 4.1）。

        與 libgen / Gutenberg 兩條路徑**並存**，逐字不改它們。三條的抓取協定
        本來就不同（分頁搜尋 / 全量 CSV / 分頁 JSON API），共用一個介面只會
        逼出假抽象。

        回傳 errors.md `openstax_provider.refresh` 的 outcome 值：

        - `not-run`  —— 未設定 provider（不開 refresh row，「沒跑」不得長得像
                        「跑了沒東西」）。
        - `failed`   —— `OpenStaxFetchError`，舊 rows 保留不刪；refresh row 是
                        `status='failed'` 且 error 逐字含 `OPENSTAX_FETCH_FAILED`。
        - `empty`    —— API 回 200 但 0 本；refresh row 是 `status='fresh'` 且
                        `error_message IS NULL`。
        - `ok`       —— 取得且寫入 >= 1 本。

        判準①：`empty` 與 `failed` 走不同分支、不同 refresh status、不同
        error_message，與 Gutenberg 路徑同構。
        """
        if self.openstax is None:
            return "not-run"

        refresh_id = await asyncio.to_thread(
            self.dao.begin_refresh, category_id, query_term
        )
        try:
            result = await self.openstax.fetch_books()
        except OpenStaxFetchError as exc:
            await asyncio.to_thread(
                self.dao.finish_refresh,
                refresh_id,
                success=False,
                pages_fetched=0,
                items_seen=0,
                items_added=0,
                items_updated=0,
                error_message=str(exc),
            )
            return "failed"

        added, updated, rejected = await asyncio.to_thread(
            self.dao.upsert_batch, category_id, query_term, result.items
        )
        if rejected:
            log.warning(
                "openstax refresh: %d 筆缺 source_native_id 被拒絕寫入 (category=%s)",
                rejected,
                category_id,
            )
        await asyncio.to_thread(
            self.dao.finish_refresh,
            refresh_id,
            success=True,
            pages_fetched=result.pages_fetched,
            items_seen=len(result.items),
            items_added=added,
            items_updated=updated,
        )
        await self._enrich_after_write(category_id)
        return result.outcome

    async def refresh(self, category_id: str, query_term: str) -> None:
        refresh_id = await asyncio.to_thread(
            self.dao.begin_refresh, category_id, query_term
        )
        pages_fetched = 0
        items_seen = 0
        items_added = 0
        items_updated = 0
        cursor: Optional[str] = None
        try:
            page = 1
            while page <= self.max_pages:
                result = await self.crawler.search_page(
                    query_term, page=page, page_size=self.page_size
                )
                batch = result["items"]
                cursor = result["cursor"]
                pages_fetched += 1
                items_seen += len(batch)
                added, updated, rejected = await asyncio.to_thread(
                    self.dao.upsert_batch, category_id, query_term, batch
                )
                items_added += added
                items_updated += updated
                if rejected:
                    log.warning(
                        "libgen refresh: %d 筆缺 source_native_id 被拒絕寫入 "
                        "(category=%s, page=%d)",
                        rejected,
                        category_id,
                        page,
                    )
                next_page = result.get("next_page")
                if next_page is None:
                    break
                page = int(next_page)
            await asyncio.to_thread(
                self.dao.finish_refresh,
                refresh_id,
                success=True,
                pages_fetched=pages_fetched,
                items_seen=items_seen,
                items_added=items_added,
                items_updated=items_updated,
                cursor=cursor,
            )
            # DD-4：libgen 路徑同樣在**寫入完成之後**做一次性 OL enrich。
            # 未注入 ol_bridge 時這是 no-op，故 Phase 2 鎖定的 libgen 行為不變。
            await self._enrich_after_write(category_id)
        except Exception as exc:
            await asyncio.to_thread(
                self.dao.finish_refresh,
                refresh_id,
                success=False,
                pages_fetched=pages_fetched,
                items_seen=items_seen,
                items_added=items_added,
                items_updated=items_updated,
                cursor=cursor,
                error_message=str(exc),
            )
