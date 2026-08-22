import asyncio
from typing import Dict, Optional

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
    ):
        self.dao = dao
        self.crawler = crawler
        self.page_size = page_size
        self.max_pages = max_pages

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
