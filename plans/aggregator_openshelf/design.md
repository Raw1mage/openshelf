# Design: aggregator_openshelf

## Context

使用者需要一套繁體中文版、具備 Libgen / Anna's Archive 核心架構的全文聚合系統，並能以反向代理 Gateway 註冊在 `/libgen/` 作為公開服務。系統既能離線匯入百萬級 Libgen 書目 Dump，又能動態解析 IPFS / 存活鏡像下載原檔，並將已落地之書籍自動抽取純文字存入 NAS，支援線上即時閱讀與全文檢索。

## Goals / Non-Goals

**Goals**
- 建立基於 Docker Compose + NAS Volume 掛載的單機容器架構，支援 Gateway `/libgen/` 反向代理前綴。
- 打造全繁體中文介面，提供現代化搜尋、格式/語言/年份篩選、書目詳情與內嵌閱讀器。
- 實作「二層文件模型（Work + Locations）」，完全兼容 Libgen 的 MD5 指紋體系。
- 實作 Libgen MySQL Dump 串流轉換工具，將 `updated.sql.gz` 與 `fiction.sql.gz` 直接匯入本地 SQLite。
- 建立多鏡像節點（`libgen.is`, `libgen.li` 等）與 IPFS Gateway 池動態健康探測與下載解析器。
- 雙份落地管線：原檔（PDF/EPUB）保留供閱讀，抽取乾淨純文字（Markdown/TXT）供 FTS5 全文檢索與 RAG。

**Non-Goals**
- 不提供公網多租戶商用計費與密碼註冊。
- 不做無腦 50TB+ 歷史種子全量下載。

## System Architecture

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
|  |      ├── Libgen Dump Ingester (Streaming MySQL -> SQLite)         |  |
|  |      └── Dynamic Mirror & IPFS Resolver (健康檢查 + 鏈結解析)      |  |
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

## Component Boundaries (Derived from IDEF0 Activities)

### 1. `StorageManager` & `LibgenDumpIngester` (對應 IDEF0 A1: 原檔匯入與儲存)
- **責任**：
  1. 管理 NAS 掛載路徑（`/data/raw`、`/data/parsed`、`/data/db`）、計算檔案 SHA256/MD5 指紋、執行原子寫入與安全路徑校驗。
  2. 串流解析 Libgen 官方 MySQL Dump（`updated.sql.gz` / `fiction.sql.gz`），免解壓直接串流轉入 SQLite。
- **邊界**：負責檔案系統 I/O 與底層 Dump 檔案串流。

### 2. `ExtractionPipeline` (對應 IDEF0 A2: 文本抽取與 OCR 解析)
- **責任**：
  1. 輸入本地原檔（PDF / EPUB）。
  2. 使用 PyMuPDF 擷取原生文字層。
  3. 啟發式判定掃描件（若每頁平均可辨識字符 < 30 字），自動調度 RapidOCR (ONNX CPU) 背景解析圖片文字。
  4. 輸出標準化 Markdown / TXT 檔至 `/data/parsed/` 並更新 FTS5 索引。

### 3. `CatalogStore` (對應 IDEF0 A3: 書目建模與 FTS5 索引)
- **責任**：封裝 SQLite 3 + FTS5 操作，管理 Work 與 Locations 兩層實體資料，支援中文分詞與 Trigram 索引。
- **提供介面**：`create_work()`, `add_location()`, `search_works(query, filters)`, `get_work_detail()`, `update_reading_progress()`。

### 4. `WebReaderApp` & `DynamicMirrorResolver` (對應 IDEF0 A4: Web 檢索與閱讀服務)
- **責任**：
  1. 提供搜尋、書目檢視、上傳與閱讀進度 REST API，後端配置 `root_path="/libgen"` 適配 Gateway。
  2. 提供全繁體中文 Web 前端，整合 PDF.js 與 EPUB.js，支援全螢幕閱讀、翻頁與進度記憶。
  3. 動態偵測可用之 Libgen 鏡像節點與 IPFS Gateway 池（Cloudflare, Pinata, dweb.link, libgen.is/li/gs），提供即時下載直鏈解析。

### 5. `MirrorQueueManager` (選擇性鏡像與批次下載管理器 — Phase 2/3 骨架)
- **責任**：管理外部來源解析（IPFS CID, HTTP, Magnet）與非同步批次下載任務佇列。

## Decisions

- **DD-1（儲存分離）**：應用邏輯於 Docker 容器執行，持久資料全數透過 Volume Mount 掛載至外部 NAS。
- **DD-2（二層文件模型）**：以 Work 作為書目主實體，以 Location 記錄實體儲存（本地路徑）或遠端來源（IPFS/HTTP/DOI/ISBN/MD5）。
- **DD-3（雙份落地）**：原檔（保留排版/圖片）供閱讀，純文字（3-5% 空間）供全文檢索與 RAG。
- **DD-4（CPU 友善 OCR）**：採用 RapidOCR (ONNX Runtime CPU)，非同步背景執行，不阻塞 Web 主行程。
- **DD-5（閘道與繁體中文）**：全站 UI 繁體中文化，FastAPI 適配反向代理 Gateway `/libgen/` 前綴與 Header 傳遞。

## Risks / Trade-offs

- **Risk: NAS 網路掛載延遲與 SQLite 鎖定**
  - *Mitigation*: SQLite 啟用 WAL (Write-Ahead Logging) 模式；資料庫目錄確保為本地低延遲路徑或 NAS 支援鎖定之掛載點（NFS/SMB 設定或 local volume override）。
- **Risk: 掃描件 OCR 耗用 CPU**
  - *Mitigation*: 限制 OCR 背景 Worker Concurrency = 1~2，並設 process priority (nice)，優先保證 Web API 響應。

## Critical Files

- `docker-compose.yml` — 容器定義與 NAS volume 映射。
- `app/models/schema.py` — Work 與 Location 資料模型定義。
- `app/db/catalog.py` — SQLite + FTS5 資料庫管理與搜尋。
- `app/pipeline/extractor.py` — PyMuPDF 文本提取與 RapidOCR 整合。
- `app/main.py` — FastAPI 進入點與路由。
- `app/static/index.html` — Web UI 搜尋與檢索介面。
- `app/static/reader.html` — 內嵌 PDF.js / EPUB.js 閱讀器。
