# openshelf 系統架構文件 (Architecture SSOT)

## 1. 系統定位與目標
`openshelf` 是一套全繁體中文介面、自用型 Libgen / 全文聚合系統。
- **儲存底盤**：Docker 容器化 + NAS Volume 掛載（`/data/raw`, `/data/parsed`, `/data/db`）。
- **網路閘道**：原生支援反向代理 Gateway 註冊於 `/libgen/` 子路徑對外公開服務。
- **核心能力**：
  1. 雙份落地：原始檔案（PDF/EPUB）供閱讀，解析純文字（Markdown/TXT）供 FTS5 全文檢索與未來的 RAG 向量檢索。
  2. 統一二層文件模型：以 `Work`（抽象書目本體）與 `Location`（多實體檔案/線上來源）結構管理。
  3. 掃描件 OCR：PyMuPDF 快速提取文字，文字稀疏時自動調度 RapidOCR (ONNX CPU) 背景解析。
  4. 繁體中文 Web UI：首頁搜尋、格式/語言/年份篩選、書目卡片、元資料詳情與內嵌 PDF.js/EPUB.js 閱讀器與進度記憶。

## 2. 系統架構圖 (System Architecture)

```
+-------------------------------------------------------------------------+
|                  Public Network / Gateway (反向代理閘道)                  |
|                                                                         |
|  Request: https://<domain>/libgen/* ──────────────────────────┐         |
+───────────────────────────────────────────────────────────────┼─────────+
                                                                │
+───────────────────────────────────────────────────────────────┼─────────+
|                              Host / VPS                       ▼         |
|                                                                         |
|  +-------------------------------------------------------------------+  |
|  |                 Docker Container (openshelf-app)                  |  |
|  |                                                                   |  |
|  |   [ 繁體中文 Web UI (Search / Detail / PDF.js / EPUB.js) ]        |  |
|  |                              │ (REST / SSE, root_path=/libgen)   |  |
|  |   [ FastAPI 後端服務 ]                                            |  |
|  |      ├── Catalog & Search Router (FTS5 + Trigram 中文檢索)       |  |
|  |      ├── Ingestion Pipeline (PyMuPDF + RapidOCR Worker)           |  |
|  |      └── StorageManager (安全路徑 + 雜湊計算 + 原子寫入)           |  |
|  |                              │ (SQLite + FTS5)                    |  |
|  |   [ SQLite DB Engine (/data/db/openshelf.sqlite) ]                |  |
|  +──────────────────────────────┼────────────────────────────────────+  |
+─────────────────────────────────┼───────────────────────────────────────+
                                  │ (Docker Volume Mount)
                                  ▼
                   +──────────────────────────────+
                   |          NAS Storage         |
                   |                              |
                   |  /data/raw/       (原檔 PDF) |
                   |  /data/parsed/    (純文字)   |
                   |  /data/db/        (SQLite)   |
                   +──────────────────────────────+
```

## 3. 模組職責分工 (Component Boundaries)

### 3.1 `StorageManager` (`app/storage/manager.py`)
- 管理持久目錄 `/data/raw`、`/data/parsed`、`/data/db`。
- 計算 SHA256 與 MD5 雜湊。
- 提供原子寫入與路徑安全遍歷防護。

### 3.2 `DatabaseEngine` & `CatalogDAO` (`app/db/engine.py`, `app/db/dao.py`)
- SQLite 3 引擎配置 WAL 模式與外鍵約束。
- 管理 `work`, `identifier`, `manifestation`, `file_object`, `reading_state`, `download_job` 資料表。
- 維護 `work_fts` FTS5 虛擬表（tokenize='trigram'）。

### 3.3 `SearchEngine` (`app/db/search.py`)
- 整合 FTS5 全文檢索與結構化篩選（格式、語言、出版年份）。
- 支援中英文 Trigram 搜尋與高亮片段（snippet）。

### 3.4 `IngestionPipeline` (`app/pipeline/ingest.py`)
- 原檔儲存、SHA256/MD5 去重檢測。
- PyMuPDF (`pdf_extractor.py`, `epub_extractor.py`) 抽取文字與元資料。
- 掃描件自動判定並調度 RapidOCR (`ocr_worker.py`) 辨識。
- 雙份落地：寫入純文字庫並同步更新 FTS5 索引。

### 3.5 `LibgenCrawler` & `MirrorResolver` (`app/crawler/libgen_live.py`, `app/crawler/mirror_resolver.py`)
- 即時非同步跨鏡像檢索公網 Libgen 目錄（`libgen.li`, `libgen.is`, `libgen.rocks`, `libgen.la`, `libgen.gs`, `libgen.pm`）。
- 具備智慧引文拆解器（Smart Query Disassembler），自動分離書名、作者與出版商並進行多級容錯級聯檢索。
- 動態解析鏡像頁面以獲取可用之二進位直鏈，支援防盜鏈 Referer 注入與多鏡像自動輪詢。

### 3.6 `DownloadWorker` & `CrawlerRoutes` (`app/crawler/download_worker.py`, `app/api/crawler_routes.py`)
- 非同步背景下載佇列，支援 HTTP Range 斷點續傳、指數退避重試（Exponential Backoff）與 MD5 完整性校驗。
- 自動觸發 `IngestionPipeline` 落地本地原檔庫、抽取 Markdown 並更新 FTS5 索引。
- 提供單本下載、批次下載、手動重試（`/retry`）與任務佇列監控 API。

### 3.7 `WebReaderApp` (`app/main.py`, `app/api/routes.py`, `app/static/`)
- FastAPI REST 路由（支援 `root_path="/libgen"` 與雙重掛載）。
- 繁體中文 Web UI（全自動統一聚合搜尋：本地落地與公網資源並行檢索、多選批次鏡像操作列）。
- 介面用詞全面本地化（去「NAS」化）。
- 下載佇列 Modal 支援一鍵縮小在背景運作，頂部 Header 即時顯示動態動畫與微進度指示器。
- 內嵌 PDF.js 與 EPUB.js 閱讀器，自動同步閱讀頁碼進度。

## 4. 驗證與測試 (Verification)
- 測試套件路徑：`tests/test_storage.py`, `tests/test_pipeline.py`, `tests/test_api.py`, `tests/test_e2e.py`。
- 執行測試指令：`PYTHONPATH=. pytest -v tests/`。
