# Errors: aggregator_openshelf

## Error Catalogue

| Code | Condition | Surface | Recovery |
| ---- | --------- | ------- | -------- |
| `ERR_STORAGE_UNWRITABLE` | NAS 掛載路徑 `/data/` 權限不足或磁碟唯讀 | HTTP 500 (API) / 記錄 FATAL 日誌 | 檢查 Docker Volume 掛載路徑之 UID/GID 權限與 NAS 共享設定 |
| `ERR_DUPLICATE_FILE` | 匯入之檔案 SHA256 已存在於資料庫中 | HTTP 409 (API) / 回傳既有 work_id | 前端直接導向既有書籍檢視頁面，不重複寫入磁碟 |
| `ERR_EXTRACTION_FAILED` | PDF 損毀或密碼鎖定無法解析 | Ingestion Error / 標記 manifestation.format='unknown' | 標記檔案需手動修復，不阻塞其他檔案解析 |
| `ERR_OCR_TIMEOUT` | 單頁 OCR 處理逾時（超過 60 秒） | Worker Warning / 記錄失敗頁碼 | 跳過該頁並繼續解析其餘頁面，標記 OCR partial |
