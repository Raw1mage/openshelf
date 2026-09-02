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
- 嚴格純圖示原則（Pure Icon UI）：按鈕無文字標籤，全面支援滑鼠懸停（`title`）提示。
- **搜尋結果書卡極簡排版 (Header-Anchored Dropdown)**：書卡頂部首行並排狀態指標（`💾` 磁碟圖示或公網選框）與「`⋯`」操作按鈕，所有次要操作（`📖` 閱讀、`⭐` 收藏、`📥` 下載原檔、`ℹ️` 詳情）收納於下拉選單中，徹底釋放卡片空間。
- 系統設定彈窗（`⚙️`）：提供 Chrome 擴充套件安裝指引與 File System Access API 本機資料夾同步設定。
- 內嵌 PDF.js 與 EPUB.js 閱讀器，自動同步閱讀頁碼進度。
- **手機沉浸式無遮擋閱覽與單擊翻頁系統 (Mobile Immersive & 1-Tap Turning Engine)**：
  - **自動隱藏與按一下立刻翻頁 (1-Tap Turning & 2s Auto-Hide)**：閱覽過程中 Header 與底部進度條自動滑動淡出（`transform: translateY(-100% / 160%)`），保證 100% 純淨無遮擋文字視野；兩側翻頁通道（`#zoneLeft`, `#zoneRight`）與螢幕左右兩側（左側 35% 上一頁 / 右側 35% 下一頁）永遠維持點擊感測（`pointer-events: auto !important`），**單擊按一下立刻翻頁，絕不被禁用**。
  - **全螢幕手勢與中央喚醒**：支援水平快速滑動（Swipe Left/Right）翻頁；點擊螢幕中央 30% 或雙擊任何位置即可喚醒控制列，無觸控操作滿 2 秒自動再次平滑隱藏。
  - **雙指多點觸控縮放 (Multi-Touch Pinch-to-Zoom)**：支援手勢流暢即時縮放頁面比例（`touch-action: pan-x pan-y pinch-zoom`），手機端開啟預設自動適配螢幕寬度（Fit-Width）。

### 3.8 `DockerHotReloadRuntime` (`docker-compose.yml`, `Dockerfile`)
- **原始碼即時外掛 (Host Bind-Mount)**：`docker-compose.yml` 將本機 `./app` 直接掛載至容器 `/app/app`，Docker 映像檔僅封裝底層 Linux 套件與 Python 固態依賴。
- **Uvicorn 自動熱重載 (WatchFiles Auto-Reload)**：後端 Python 邏輯、REST 路由與靜態資產（HTML/CSS/JS）於外部修改後即時熱生效，徹底免除繁瑣之 `docker compose build` 耗時流程。

### 3.9 `PersonalCollections` & `PortableBookmarkBridge` (`app/api/collection_routes.py`, `app/static/js/app.js`)
- 個人化書單管理（Local-First & Portable）：
  - **預設無感 Local-First**：單機版使用者打開即用，書單資料完整儲存於 Client 端（零伺服器負擔、零帳號隔離負擔、零安裝門檻）。
  - **跨裝置可攜與標準書籤匯出/匯入 (Portable Netscape Bookmarks)**：
    - **一鍵匯出標準書籤檔 (`.html`)**：產生全球通用 Netscape Bookmark 規格之 HTML 書籤檔，可在 Chrome、Safari、Edge、Firefox 點選「匯入書籤」直接還原完整分類資料架構與書籍閱讀超連結，或隨時同步至 Google Drive、OneDrive、iCloud「我的最愛」。
    - **書籤與備份還原 (`.html` / `.json`)**：支援將導出的 Netscape HTML 書籤檔或 JSON 資料庫備份一鍵匯入解析並還原至書單。
  - **Chrome 擴充套件模式 (可選增強)**：提供選配的 Chrome Extension 與原生書籤欄進行實體雙向即時同步。

### 3.10 `Bookstalls` & `PersistentRemoteCatalog` (`app/db/remote_catalog.py`, `app/crawler/remote_catalog_refresh.py`, `app/api/category_routes.py`, `app/static/js/app.js`)
- 多階層樹狀分類與線上書攤（`🏪`）：
  - 預設注入標準中文圖書多階層樹狀分類；本地藏書與遠端可逛書目分開持久化，遠端資料不得污染本地 `work`。
  - 分類 API 先從 SQLite 遠端 catalog 即時分頁回應，再以單分類去重的背景 task 刷新來源；開啟書攤不等待外部網路。
  - 刷新採只增不減的 upsert：以穩定書目識別去重，保存首次／最近發現時間與來源；某次搜尋缺席或網路失敗不得刪除已累積書目。
  - crawler 以來源原始列數判斷下一頁，避免無 MD5 row 被過濾後提早終止；全鏡像失敗必須記為 `failed`，不得與合法空頁共用輸出。
  - API `total` 是分類完整子樹內本地藏書＋遠端 catalog 的穩定 ID 去重聯集；書卡使用 `page`／`page_size` 分頁，單頁長度不得冒充聯集總數。
  - `catalog_status.accumulated_total` 專指遠端 catalog 的 `COUNT(DISTINCT catalog_id)`，在 `never_refreshed|failed|fresh` 皆回持久化全集；失敗狀態保留錯誤訊號並允許後續立即重試。
  - request generation + `AbortController` 防止較早分類或分頁請求晚回後覆蓋目前書卡、徽章與 tooltip。
  - **全純圖示化書卡動作區 (Pure Icon UI)**：所有標籤與按鈕全面去除文字，線上閱讀採用眼睛符號「`👁️`」正方形按鈕，收書採用「`📥`」，格式改為精緻圖示（`📕` 原生 PDF、`📷` 掃描 PDF、`📗` EPUB），搭配原生 Mouseover Tooltip 呈現說明文字；次要功能（`⭐`、`💾`、`ℹ️`）收納於「`⋯`」下拉選單中。

#### 3.10.1 多來源 Identity 與授權模型 (Multi-Source Identity & Licensing)

> 來源：`aggregator_multi-source-provider` Phase 1-4（commit `65e945d8` / `e167f395` / `049c32cf` / `4d471c8b`）。

- **來源層主鍵是複合鍵 `(source, source_native_id)`，不是 `md5`**（DD-1）：
  - `md5` 降級為**可空、非唯一**的橋接欄位，僅供 libgen 既有資料與 `MirrorResolver` 下載路徑使用（DD-2）。
  - 理由：**沒有任何免費識別符能單獨當跨來源主鍵**——ISBN/OCLC/LCCN 皆回陣列、非一對一。
  - **反向控制組是這個設計的存在理由**：SQLite 對多筆 `md5=NULL` **不**觸發 UNIQUE 衝突，若維持 `md5 UNIQUE` 而讓新來源填 NULL，去重會**靜默失效**（缺席態與失敗態共用輸出）。
- **Migration 一律 additive-only**：`app/db/schema.sql` 內聯欄位供新 DB bootstrap，`app/db/dao.py` 的 `_COLUMN_MIGRATIONS["remote_catalog_item"]` 供舊 DB `ALTER`；新增欄位一律可空，**不得刪欄位、不得改既有欄位語意、不得動複合唯一索引**。複合索引建在 `_POST_MIGRATION_INDEXES`（必須等 `source_native_id` 回填跑完才能建）。
- **`work_id` 生成收歛為共用函式** `app/crawler/libgen_live.py` 的 `make_work_id(source, source_native_id)`；libgen 輸出字串逐字不變（向下相容）。
- **三 provider 並存，調度層互不干涉**（`app/crawler/remote_catalog_refresh.py`，具名參數 `gutenberg=` / `openstax=`）：

  | provider | 來源 | `source_native_id` | 失敗語意 |
  |---|---|---|---|
  | `libgen` | 公網鏡像即時檢索 | 既有 `md5` | 全鏡像失敗記 `failed`，不與合法空頁共用輸出 |
  | `gutenberg` | `cache/epub/feeds/pg_catalog.csv` | Gutenberg `Text#` | **例外分離**（`GutenbergFetchError`）——主目錄抓取失敗**應該**中止該次刷新 |
  | `openstax` | CMS JSON API（`?type=books.Book`） | CMS 數字 `id`（非 slug） | **例外分離**（同 Gutenberg） |

- **Open Library 是橋接層而非 provider**（`app/crawler/openlibrary_bridge.py`，DD-4）：
  - **寫入時一次性 enrich，絕不掛在任何 API GET 的同步路徑上**——`category_routes.py` 對本模組**零引用**（由原碼掃描測試鎖定）。理由：官方明文禁 bulk harvest 與「hundreds of single-book requests」。
  - **失敗不得阻斷主寫入**，故這裡採**欄位互斥**（`OLEnrichResult` 的 `error`/`fields_written`/`queried` 三欄組合）而非例外分離——**這是與上表兩個 provider 相反的刻意取捨**，判準是「失敗要不要阻斷主流程」，不是寫法偏好。
  - `ol_enriched_at` 在 **empty 時仍蓋**（那是一次有效查詢）、**failed 時不蓋**（否則一次失敗會永不重試）。
- **兩層授權模型（逐本優先、來源層回退）**，讀路徑在 `app/db/remote_catalog.py:query_browseable()`：

  ```
  item["license"] = rci.license_name  or  license_for_source(rci.source)
                    ↑逐筆資料（OpenStax）    ↑來源性質（Gutenberg）
  兩者皆無 ⇒ None（空白）
  ```

  - **兩種授權在資料性質上不同，不得壓縮成同一個機制**：Gutenberg 的 `"Public domain in the USA."` 是**來源的性質**（全庫同一句，存於 `app/models/catalog.py` 的 `SOURCE_LICENSE_LABEL` 字面值 SSOT）；OpenStax 的是**逐筆資料**（同一來源 3 種值，必須隨 row 存於 `license_name` 可空欄）。
  - **來源未宣告授權必須寫 NULL，不得套用任何預設值**（OpenStax 129 本中實測 11 本未宣告）——套預設值等於替出版方做了它沒做的聲明。libgen 的 license 同理為 `None`（來源未宣告 ≠ 已確認公版）。
  - Gutenberg 的授權是「美國公版」**非全球公版**，UI/API 必須可見且不得與一般公版混同。

### 3.11 `SmartBookClassification` (`app/classification/`, `app/db/dao.py`, `script/backfill_classification.py`)
- 採規則優先、模型補判的兩階段分類：入庫路徑只跑零網路規則層；零命中或多類衝突標記為 `pending`，由可重跑回填命令呼叫 OpenAI-compatible endpoint。
- 模型輸出只接受 taxonomy 既有葉節點、最多兩類；非法 JSON、未知／父節點 ID、429/5xx 或逾時均不使用預設類別。
- `work.classification_state` 區分 `pending|classified|unclassified|error|disabled`；`work_category.source` 保存 `rule|llm|legacy|manual` provenance。自動回填只替換自動來源，不覆寫人工分類。
- 書攤分類樹、分類詳情與作品列表共用可信分類查詢：只顯示 `classified` 或 `manual` 關聯，避免舊 fallback 在回填前繼續對使用者可見。
- 回填 CLI 預設唯讀 dry-run，開工前驗證正確 DB schema；分類／寫入例外與 `error|disabled` 有效判定失敗均回非零，但逐本隔離、保留狀態供後續重試。

### 3.12 `MobileResponsiveUI` (`app/static/css/style.css`, `app/static/js/app.js`)
- 手機行動端 RWD 深度優化（`@media (max-width: 768px)`）：
  - **全版獨立頁（Full-Screen Single Page Sheets）**：所有彈窗在手機上均為 100vw × 100dvh 全螢幕獨立頁，去除四周間距與外框圓角。
  - **兩階段下鑽導航（Two-Stage Drill-Down）**：線上書攤與個人書單在手機端採用「列表 ➔ 詳情」互斥切換架構。
  - **頂部與底欄最佳化**：各頁面頂部配置標準「⬅️」返回按鈕，搜尋過濾標籤列支援手機原生水平平滑滾動（Touch Scroll）。

### 3.13 `AnchoredModalDialogSystem` (`app/static/js/modal-dialog.js`, `app/static/css/style.css`)
- **無原生 Message Box 規範**：全站禁止呼叫瀏覽器原生 `alert()`、`confirm()`、`prompt()`，一律由自訂 Promise-based Modal / Popover 接管。
- **觸發點附著定位 (Trigger-Anchored Popover)**：
  - 依據觸發節點（Anchor Element）動態計算視窗座標，預設展開於觸發按鈕正下方（空間不足時自動向上翻轉），帶有指示箭頭並具備視窗邊界防溢出（Viewport Clamping）。
  - 手機版（螢幕寬度 $\le 640\text{px}$）或無錨點時自動降級為中央浮動 Modal 呈現。
- **鍵盤與無障礙支援**：支援 `Enter` 快速確認、`Escape` 快速取消/關閉，並具備自動 Focus 與預設文字選取功能。

### 3.14 `StealthScrollbarSystem` (`app/static/css/style.css`)
- **低調隱藏式捲軸**：全站各欄位、側邊欄、彈窗與內容區採用深色同底色自訂捲軸（Webkit Scrollbar + CSS `scrollbar-width: thin; scrollbar-color: rgba(...) transparent;`），平時極度低調不搶視覺，懸停時微亮，徹底去除原生粗糙的亮色捲軸。

### 3.15 `CustomLibgenMirrorsAndPreflightValidator` (`app/crawler/validator.py`, `app/api/settings_routes.py`, `app/db/dao.py`)
- 自訂 Libgen 來源、鏡像管理與上線前預檢驗證管線（Pre-flight Validation & Scraper Adapters）：
  - **上線前強制預檢 (Pre-flight Validation Pipeline)**：任何新增或更新之 Libgen 鏡像來源，必須先經由 `MirrorValidator` 進行連線延遲探測與爬取適配器抽樣測試（提取書籍標題、MD5 等特徵），通過驗證（`verified`）後才正式被納入爬蟲檢索與直鏈解析輪替池，徹底阻絕無效或惡意來源污染系統。
  - **多適配器相容分流 (Scraper Adapters)**：支援 `libgen_li`（9 欄式）、`libgen_is`（10 欄式）與 `direct_gateway`（如 `library.lol`）多種解析器架構，動態自動適配。
  - **自動 BR 派發機制 (Auto Bug Report Dispatch)**：若目標鏡像連線正常（HTTP 200/300）但現有適配器均無法解析書目表格（因改版、DOM 變更或反爬機制），驗證器將自動產生結構化 Bug Report 檔案寫入本專案 `issues/BR-<TIMESTAMP>-<DOMAIN>.md`，包含失敗原因、HTTP 狀態與 DOM Signature 切片，並將該鏡像隔離標記為 `incompatible_layout`，方便維護人員快速開發專屬適配器。
  - **齒輪設定頁面 (Web UI)**：提供直觀的鏡像管理面板，支援一鍵批量預檢驗證、測速延遲標籤、啟用/停用開關、優先級排序、刪除與恢復原廠預設清單。

## 4. 驗證與測試 (Verification)
- 測試套件包含 `tests/test_smart_classification.py`，覆蓋規則邊界、模型契約、provenance、可信分類讀路徑、唯讀 dry-run、CLI 退出碼與 mutation 控制。
- 完整測試指令：`.venv/bin/python -m pytest -q`；需現役服務的 E2E 測試維持 opt-in。

