# Observability: aggregator_openshelf

## Events

- `ingest.started` — 檔案開始上傳或掃描入庫時觸發，包含 file_name 與 size_bytes。
- `ingest.extracted` — PyMuPDF 文本抽取完成，記錄耗時 (ms) 與抽取字數。
- `ingest.ocr_triggered` — 掃描件觸發 RapidOCR 解析，記錄總頁數。
- `ingest.completed` — 檔案雙份落地並完成 FTS5 索引，包含 work_id 與 sha256。
- `search.query` — 使用者發起全文檢索，記錄查詢詞與 FTS5 耗時 (ms)。
- `reader.progress_sync` — 使用者閱讀翻頁同步進度，記錄 work_id 與頁碼百分比。

## Metrics

- `openshelf_total_works_count` — 系統中 Work 總數（區分已落地與遠端書目）。
- `openshelf_storage_used_bytes` — NAS 掛載目錄使用的磁碟空間總量。
- `openshelf_ingest_duration_seconds` — 單本檔案入庫與解析端對端耗時長條圖。
- `openshelf_search_latency_seconds` — FTS5 搜尋延遲長條圖。
