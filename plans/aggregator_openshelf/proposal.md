# Proposal: aggregator_openshelf — 繁體中文版 Libgen 與自用型全文鏡像系統

## Why

使用者需要一個具備自用型基礎架構的全文聚合系統，能夠：
1. **繁體中文現代化 Libgen 體驗**：仿造 Libgen / Anna's Archive 的核心架構，打造全繁體中文之搜尋、篩選、書目詳情與線上閱讀前端。
2. **擺脫公網來源流亡狀態**：直接相容並匯入 Libgen 官方 Catalog Dump（Non-fiction `updated` / Fiction `fiction`），建立永久穩定的本地離線搜尋引擎。
3. **Gateway 閘道公開服務**：支援註冊於反向代理閘道（Gateway Register）之 `/libgen/` 子路徑下，作為公開檢索站點。
4. **多鏡像與 IPFS 解析**：整合多鏡像節點健康探測與 IPFS Gateway 池，提供高可用之點選下載與選擇性批次鏡像至 NAS。
5. **雙份落地與多用途**：原檔（供閱讀）+ 解析純文字（供 FTS 全文檢索與 RAG 向量檢索），在個人 NAS 上永久保存。

## Original Requirement Wording (Baseline)

- "聽說過 libgen 嗎？我需要開發一個替代系統"
- "目前我還沒有建立個人藏書。而且外網公開的libgen經常在流亡狀態，我覺得第一個要建立的功能是基礎的自用型libgen，並設法從可獲取的來源盡量鏡像備份公開libgen的內容"
- "先建好基礎框架，不要無腦下載打包。建好基礎後，再建立選擇性鏡像機制，讓我可以從公網可獲取來源搜尋書類目錄、或點選符合條件的搜尋結果，並進行批量下載"
- "我還滿好奇這個plan真的有完整設計一個genlib功能的前後端嗎？你有沒有先去拿現有的genlib開源程式來參照"
- "同意。仿造它做一個繁體中文版。並註冊在gateway register的/libgen/成為公開網站"

## Requirement Revision History

- 2026-08-19: initial draft created via plan-init.ts
- 2026-08-19: 使用者四格拍板（方向/規模/藏書/用途）
- 2026-08-19: 使用者需求迭代：先建基礎框架，再建公網目錄搜尋與選擇性批次鏡像機制
- 2026-08-19: 參照 Libgen / Anna's Archive 開源架構，定調打造「繁體中文版 Libgen」並支援 Gateway `/libgen/` 公開站點部署
- 2026-08-20: 使用者需求擴充：實作「個人化書單」與「逛線上書攤（多階層樹狀書目分類與各架藏書自由瀏覽）」

## Effective Requirement Description

使用者親自拍板之核心原則：

1. **產品定位** — 繁體中文版 Libgen / 全文聚合系統。
2. **部署與路由** — Docker 容器化，掛載 NAS 為永久儲存底盤；後端適配反向代理 Gateway 註冊於 `/libgen/` 路徑公開存取。
3. **書目相容** — 支援 Libgen 官方 MySQL dump（數百萬筆書籍與 MD5 映射）之批次/串流轉換匯入。
4. **解析與鏡像** — 建立多鏡像（`libgen.is`, `libgen.li`, `libgen.gs` 等）與 IPFS Gateway 池動態解析器，支援點選直載與批次佇列鏡像至 NAS。
5. **雙份落地與閱讀** — 本地原檔（PDF/EPUB）供內建 Web 閱讀器開啟，解析純文字供 FTS5 全文檢索與 RAG。
6. **個人化書單 (Booklists & Collections)** — 提供自訂書單建立、命名、排序與書籍加入/移出管理，支援多書單分類標籤。
7. **逛線上書攤 (Bookstalls & Tree Browsing)** — 建置多階層樹狀書目分類體系（文學小說、自然科學、應用科學、社會科學、哲學、歷史地理、藝術生活等），免輸入關鍵字即可像在實體圖書館一樣自由漫遊、瀏覽各架位藏書。

## Scope

### IN

- Phase 1: 基礎核心框架 (Core Foundation & Gateway Support)
  - Docker Compose 容器化部署與 NAS Volume 掛載（`/data/raw`, `/data/parsed`, `/data/db`）
  - FastAPI 後端支援 `root_path="/libgen"` 反向代理閘道前綴
  - 統一文件模型（Work + Locations，支援 MD5, SHA256, ISBN, DOI, IPFS CID）
  - 本地原檔匯入、去重與目錄結構管理
  - 文本解析管線：PyMuPDF 文本抽取 + RapidOCR (ONNX CPU) 掃描件支援
  - 全繁體中文 Web UI：首頁搜尋、高級篩選（副檔名、語言、年份）、書目詳情、內嵌 PDF.js / EPUB.js 閱讀器
  - SQLite 3 + FTS5 全文索引（支援中文分詞與三連詞 Trigram）
- Phase 2: Libgen Catalog 匯入與多來源解析 (Libgen Catalog & Dynamic Resolver)
  - Libgen 官方 MySQL Dump 串流轉換工具（`updated.sql.gz` / `fiction.sql.gz` 轉入本地 SQLite）
  - 多鏡像節點健康探測與 IPFS Gateway 池動態解析器（MD5 → 可用下載直鏈）
- Phase 3: 選擇性鏡像與批次下載佇列 (Selective Mirroring & Batch Queue)
  - 搜尋結果多選與批次下載管理
  - 非同步背景下載、MD5 校驗與自動落地 NAS、觸發文字解析
- Phase 4: 語意檢索與 RAG 支援 (Semantic Search & RAG API)
  - 純文字切塊、向量化與 RAG 檢索介面
- Phase 5: 個人化書單與收藏庫 (Personal Booklists & Custom Collections)
  - 自訂書單資料結構（`collection`, `collection_item`）與完整 CRUD DAO/API
  - 前端純圖示快速收藏按鈕（`⭐` / `🔖`）、書單管理抽屜與獨立書單檢視
  - Chrome 擴充套件原生書籤同步橋樑（Local-First，零伺服器負擔）
- Phase 6: 多階層樹狀分類與線上書攤 (Multi-Level Tree Categories & Bookstall Shelf Browsing)
  - 多階層分類結構（`category`, `work_category`）與中繼資料自動分類對應
  - 線上書攤（Bookstall / Shelf View）前端視覺化介面，支援樹狀節點展開折疊、層級導航與瀑布流/書架排版自由瀏覽各架位藏書
- Phase 7: 線上書攤漸進式雲端探索 (On-Demand Category Cloud Discovery & Hybrid Shelf)
  - 捨棄暴力窮舉，採用「有觸及再展開」之懶加載探測機制
  - 點擊架位時動態向 Libgen 公網探測該領域經典熱門書目並快取
  - 混合書架體驗：已落地標記 `💾 本地`（直讀 `📖`），雲端標記 `🌐 公網`（一鍵收書 `📥`）
- Phase 8: 手機行動端 RWD 全版獨立頁深度重構 (Mobile-First Full-Screen Refactoring)
  - 手機螢幕（< 768px）所有 Modal 浮窗全面轉換為全版獨立頁面（100vw × 100dvh）
  - 統一提供「⬅️ 返回上一頁」按鈕，解決多層巢狀框架與滾動條問題
  - 書攤與書單採用「兩階段下鑽（Two-Stage Drill-Down）」架構，手機端流暢切換列表與架位內容
  - 解決窄螢幕爆框、擠塞與換行問題，過濾標籤列支援原生橫向平滑滾動

### OUT

- 任何未授權受版權內容的直接集中式散布（透過 IPFS 與第三方鏡像導流/解析）
- 多租戶商業計費與公開註冊系統
- 無腦全量拉取 50TB+ 歷史種子包

## Non-Goals

- 不做通用多媒體串流伺服器（非影音/漫畫專用伺服器）。
- 不做去中心化種子發布節點（專注於客戶端鏡像抓取、檢索與本地閱讀）。

## Constraints

### C1 — 儲存與部署架構
- 系統運行於單台主機/VPS 容器中，必須透過 Docker Volume Mount 掛載外部 NAS 做永久儲存。
- 必須透過反向代理閘道（Gateway）以 `/libgen/` 路徑公開，靜態資源與 API 路由必須完全支援子路徑。

### C2 — CPU 運算資源限制
- 主機無專用 GPU，OCR 必須採用輕量 CPU ONNX 推理（RapidOCR），解析工作必須排入背景非同步佇列以避免阻塞 Web UI。

### C3 — 多標識符與無 DOI 去重
- 書籍與論文來源多樣，缺乏統一主鍵；必須以內部 Work ID 為核心，並建立 MD5 / SHA256 / ISBN / DOI / 標題作者 SimHash 的多級索引。

## What Changes

- 新建獨立專案 `openshelf`，包含後端 API（FastAPI）、資料庫（SQLite+FTS5）、解析器模組、Libgen Dump 匯入工具與全繁體中文前端 Web 介面。

## Capabilities

### New Capabilities

- **繁體中文 Libgen 搜尋體驗**：提供現代化、直覺之繁體中文圖書檢索介面。
- **公網書目離線搜尋**：支援百萬級 Libgen 書目元資料快速檢索。
- **動態多鏡像解析**：自動切換存活之 Libgen 鏡像節點與 IPFS Gateway。
- **選擇性批次鏡像**：搜尋後一鍵勾選加入下載佇列，由後端向可用鏡像/IPFS 抓取並驗證落地。
- **雙份落地與即時閱讀**：原檔永久保存在 NAS，純文字供全文搜尋，內建 Web 閱讀器。

### Modified Capabilities

- 無（新建專案）。

## Impact

本專案為全新建立之繁體中文版 Libgen 全文聚合系統，架構獨立運行於 Docker 容器中，透過 Volume Mount 掛載至外部 NAS，並透過反向代理閘道之 `/libgen/` 路徑對外服務。不影響主機既有其他系統，亦無跨系統相依破壞性風險。

## Decisions (Closed)

- **DD-1（儲存架構）**：採用 Docker 容器化 + Volume Mount NAS 作為永久儲存空間，主機負責運算，NAS 負責儲存。
- **DD-2（二層文件模型）**：採用二層結構（Work 抽象實體 + Locations 實體檔案/遠端連結），以 MD5 作為 Libgen 檔案核心指紋。
- **DD-3（去重策略）**：本地原檔以 SHA256/MD5 精確去重；公網書目以 ISBN/DOI/MD5 優先，無標識符者以標題+作者 SimHash 比對。
- **DD-4（解析與 OCR）**：Python PyMuPDF 優先提取文字層；字數不足時觸發 RapidOCR (CPU ONNX) 處理掃描頁。
- **DD-5（閘道與多語系）**：前端全量繁體中文化，後端支援 `root_path="/libgen"` 反向代理閘道前綴。FS Gateway、HTTP 鏡像、BitTorrent Magnet）
- Phase 3: 選擇性鏡像與批次下載佇列 (Selective Mirroring & Batch Queue)
  - 搜尋結果多選與批次下載管理
  - 非同步背景下載、MD5 校驗與自動落地 NAS、觸發文字解析
- Phase 4: 語意檢索與 RAG 支援 (Semantic Search & RAG API)
  - 純文字切塊、向量化與 RAG 檢索介面

### OUT

- 任何未授權受版權內容的公開轉發與對外散布服務
- 多租戶商業計費與公開註冊系統
- 無腦全量拉取 50TB+ 歷史種子包

## Non-Goals

- 不做通用多媒體串流伺服器（非影音/漫畫專用伺服器）。
- 不做去中心化種子發布節點（專注於客戶端鏡像抓取與本地個人檢索）。

## Constraints

### C1 — 儲存與部署架構
- 系統運行於單台主機/VPS 容器中，必須透過 Docker Volume Mount 掛載外部 NAS 做永久儲存。

### C2 — CPU 運算資源限制
- 主機無專用 GPU，OCR 必須採用輕量 CPU ONNX 推理（RapidOCR），解析工作必須排入背景非同步佇列以避免阻塞 Web UI。

### C3 — 多標識符與無 DOI 去重
- 書籍與論文來源多樣，缺乏統一主鍵；必須以內部 Work ID 為核心，並建立 MD5 / SHA256 / ISBN / DOI / 標題作者 SimHash 的多級索引。

## What Changes

- 新建獨立專案 `openshelf`，包含後端 API（FastAPI）、資料庫（SQLite+FTS5）、解析器模組與前端 Web 介面。

## Capabilities

### New Capabilities

- **公網書目離線搜尋**：支援數百萬級書目元資料快速檢索。
- **選擇性批次鏡像**：搜尋後一鍵勾選加入下載佇列，由後端向可用鏡像/IPFS 抓取並驗證落地。
- **雙份落地與即時閱讀**：原檔永久保存在 NAS，純文字供全文搜尋，內建 Web 閱讀器。

### Modified Capabilities

- 無（新建專案）。

## Impact

本專案為全新建立之自用型全文聚合系統，架構獨立運行於 Docker 容器中，透過 Volume Mount 掛載至外部 NAS。不影響主機既有其他系統，亦無跨系統相依破壞性風險。

## Decisions (Closed)

- **DD-1（儲存架構）**：採用 Docker 容器化 + Volume Mount NAS 作為永久儲存空間，主機負責運算，NAS 負責儲存。
- **DD-2（文件模型）**：採用二層結構（Work 抽象實體 + Locations 實體檔案/遠端連結）。
- **DD-3（去重策略）**：本地原檔以 SHA256/MD5 精確去重；公網書目以 ISBN/DOI 優先，無標識符者以標題+作者 SimHash 比對。
- **DD-4（解析與 OCR）**：Python PyMuPDF 優先提取文字層；字數不足時觸發 RapidOCR (CPU ONNX) 處理掃描頁。
