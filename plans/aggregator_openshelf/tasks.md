# Tasks: aggregator_openshelf

> **Every `- [x]` must name a machine-verifiable artifact.** Ticking a box is a
> claim made by whoever ticked it; it carries no evidence on its own. Close each
> task with an `artifact:` field so the gate can check the claim.

## 1. 容器化與儲存層 (Containerization & Storage Layer)

- [x] 1.1 建立 Dockerfile 與 docker-compose.yml，定義 NAS Volume 掛載（`/data/raw`, `/data/parsed`, `/data/db`）與 Gateway `/libgen/` 環境變數 — artifact: docker-compose.yml, Dockerfile
- [x] 1.2 建立 StorageManager 模組，負責掛載目錄自檢、原子寫入、SHA256/MD5 指紋計算與安全路徑校驗 — artifact: app/storage/manager.py

## 2. 資料庫與統一文件模型 (Database & Unified Document Model)

- [x] 2.1 建立 SQLite 資料庫連線管理（WAL 模式、FTS5 Trigram 支援）與 DDL 初始化腳本 — artifact: app/db/engine.py, app/db/schema.sql
- [x] 2.2 實作 Work、Identifier (MD5/ISBN/DOI)、Manifestation、FileObject、ReadingState、DownloadJob 之 Pydantic 模型與 DAO 操作 — artifact: app/models/catalog.py, app/db/dao.py
- [x] 2.3 實作 FTS5 全文索引觸發器與多條件檢索查詢（關鍵字、繁簡中文、標題、作者、語言、來源分級） — artifact: app/db/search.py

## 3. 解析與 OCR 管線 (Extraction & RapidOCR Pipeline)

- [x] 3.1 實作 PyMuPDF 文本提取器，支援原生 PDF 與 EPUB 之章節/段落文字擷取並轉為 Markdown — artifact: app/pipeline/pdf_extractor.py, app/pipeline/epub_extractor.py
- [x] 3.2 實作掃描件啟發式判定邏輯（文字稀疏度檢測）與 RapidOCR (ONNX CPU) 背景解析 Worker — artifact: app/pipeline/ocr_worker.py
- [x] 3.3 實作入庫工作流（Ingestion Pipeline）：原檔落地 → 提取純文字 → 寫入 `/data/parsed/` → 更新 FTS5 索引 — artifact: app/pipeline/ingest.py

## 4. Web API 與繁體中文前端 (FastAPI & Traditional Chinese UI)

- [x] 4.1 實作 FastAPI 後端 REST 路由（`/api/works`, `/api/works/{id}`, `/api/files/{id}/raw`, `/api/progress`, `/api/upload`），配置 `root_path="/libgen"` — artifact: app/api/routes.py, app/main.py
- [x] 4.2 建立全繁體中文 Web UI 搜尋介面（Libgen 風格首頁、多欄位篩選、書目卡片、詳情彈窗） — artifact: app/static/index.html, app/static/js/app.js, app/static/css/style.css
- [x] 4.3 整合 PDF.js 與 EPUB.js 內嵌閱讀器，實作全螢幕閱讀、翻頁、字體調整與進度自動回傳 — artifact: app/static/reader.html, app/static/js/reader.js

## 6. 公網爬蟲與多鏡像解析 (Live Crawler & Mirror Resolver)

- [x] 6.1 實作公網即時爬蟲模組，支援非同步並行檢索 Libgen 與 Anna's Archive 鏡像，提取書目中繼資料與 MD5 — artifact: app/crawler/libgen_live.py
- [x] 6.2 實作動態鏡像直鏈解析器與 IPFS Gateway 池探測（MD5 → library.lol / libgen / IPFS 可用下載直鏈） — artifact: app/crawler/mirror_resolver.py

## 7. 選擇性鏡像與批次下載佇列 (Selective Mirroring & Batch Queue)

- [x] 7.1 實作非同步下載佇列工作者（DownloadWorker），具備串流下載、MD5 校驗、自動觸發 IngestionPipeline 落地本地並升級 Tier — artifact: app/crawler/download_worker.py
- [x] 7.2 實作公網檢索與批次下載 REST API 路由（`/api/crawler/search`, `/api/crawler/download`, `/api/crawler/queue`） — artifact: app/api/crawler_routes.py
- [x] 7.3 實作 HTTP Range 斷點續傳、指數退避重試與 503 鏡像容錯機制，支援大檔案中斷自動恢復與手動重試 API — artifact: app/crawler/download_worker.py

## 8. 前端爬書與鏡像收書 UI (Frontend Live Search & Batch Mirroring)

- [x] 8.1 前端整合統一聚合搜尋、單本「📥 鏡像收書」按鈕、多選「⚡ 批次鏡像」浮動操作列與下載進度抽屜 — artifact: app/static/index.html, app/static/js/app.js
- [x] 8.2 撰寫爬蟲、鏡像解析與下載佇列之單元與整合測試 — artifact: tests/test_crawler.py
- [x] 8.3 前端全面去除「NAS」字樣改為「本地」，下載佇列 Modal 支援最小化為 Header 動態微進度圖示，並提供失敗任務重試按鈕 — artifact: app/static/index.html, app/static/js/app.js, app/static/css/style.css

## 9. 個人化書單 (Personal Booklists & Collections)

- [x] 9.1 資料庫層：在 `schema.sql` 與 `dao.py` 新增 `collection`、`collection_item` 資料表與 CRUD 操作方法 — artifact: app/db/schema.sql, app/db/dao.py, app/models/catalog.py
- [x] 9.2 API 路由層：實作 `/api/collections` 系列 REST 端點（建立/清單/詳情/更新/刪除/加退書籍） — artifact: app/api/collection_routes.py, app/main.py
- [x] 9.3 前端 UI 層：書籍卡片新增純圖示收藏按鈕（`⭐`），頁首新增「我的書單」純圖示按鈕（`📚`）與書單管理彈窗/檢視介面 — artifact: app/static/index.html, app/static/js/app.js, app/static/css/style.css

## 10. 多階層樹狀分類與線上書攤 (Multi-Level Tree Categories & Bookstall Shelf Browsing)

- [x] 10.1 資料庫與預設分類體系：在 `schema.sql` 與 `dao.py` 建立 `category`、`work_category` 結構，預先注入標準中文圖書多階層樹狀分類（總類、哲學、宗教、自然科學、應用科學、社會科學、歷史地理、語言文學、藝術生活等）與自動分類推導 — artifact: app/db/schema.sql, app/db/dao.py, app/db/categories.py
- [x] 10.2 API 路由層：實作 `/api/categories/tree` 與 `/api/categories/{id}/works` 分類導航與架位書籍檢索端點 — artifact: app/api/category_routes.py, app/main.py
- [x] 10.3 前端 UI 層：頁首新增「逛書攤」純圖示按鈕（`🏪`），實作左側多階層可折疊樹狀書目導航面板與右側沉浸式書架展示區 — artifact: app/static/index.html, app/static/js/app.js, app/static/css/style.css

## 11. 線上書攤漸進式雲端探索與混合書架 (On-Demand Category Cloud Discovery & Hybrid Shelf)

- [x] 11.1 後端動態探測：在 `category_routes.py` 與 `dao.py` 實作「有觸及再展開」之領域關鍵字雲端探測與快取機制，架位藏書不足時自動向 Libgen 請求精選推薦並落地快取 — artifact: app/api/category_routes.py, app/db/dao.py, app/crawler/search.py
- [x] 11.2 混合書架 UI：線上書攤書架同時展示 `💾 本地已收錄`（點擊 `📖` 直讀）與 `🌐 雲端精選`（點擊 `📥` 一鍵鏡像落地），並提供來源切換篩選 — artifact: app/static/js/app.js, app/static/index.html

## 12. 手機行動端 RWD 全版獨立頁深度重構 (Mobile-First Full-Screen Single Pages & Back Buttons)

- [x] 12.1 CSS 行動端全面重構：在 `style.css` 建立完整 `@media (max-width: 768px)` 樣式，所有 Modal 浮窗改為 100vw × 100dvh 全螢幕獨立頁面，去除巢狀邊框與多重滾動條 — artifact: app/static/css/style.css
- [x] 12.2 獨立頁導航與返回按鈕：在所有 Modal 標頭左側加入「⬅️ 返回上一頁」按鈕，書攤與書單在手機端實作「兩階段下鑽（分類列表 ➔ 架位書籍）」無縫切換 — artifact: app/static/index.html, app/static/js/app.js
- [x] 12.3 窄螢幕防爆框與排版最佳化：優化手機頂部 Header 圖示佈局、搜尋過濾標籤列支援橫向滑動（touch scroll）、書籍卡片在手機端直向流暢堆疊 — artifact: app/static/css/style.css, app/static/index.html



