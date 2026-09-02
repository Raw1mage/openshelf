import asyncio
from typing import Dict, Optional

from app.crawler.gutenberg_provider import (
    GUTENBERG_SOURCE,
    GutenbergFetchError,
    GutenbergProvider,
)
from app.crawler.libgen_live import LibgenCrawler
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
    ):
        self.dao = dao
        self.crawler = crawler
        self.page_size = page_size
        self.max_pages = max_pages
        # provider 調度（tasks.md 2.3）：不再假設只有 libgen。刻意用具名參數而非
        # 泛用 provider registry——design.md Non-Goals 明寫「不為未驗證的未來
        # 來源預先抽象」。Gutenberg 是第二個來源，兩個具名欄位就夠；真的接到
        # 第四、第五個時再抽象，那時才知道該抽象成什麼形狀。
        self.gutenberg = gutenberg

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

        added, updated = await asyncio.to_thread(
            self.dao.upsert_batch, category_id, query_term, result.items
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
        # provider 已經把 ok/empty 分開了，這裡逐字沿用，不重新判斷一次——
        # 兩處各自判斷會製造第二個真相來源。
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
                added, updated = await asyncio.to_thread(
                    self.dao.upsert_batch, category_id, query_term, batch
                )
                items_added += added
                items_updated += updated
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
