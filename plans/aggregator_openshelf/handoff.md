# Handoff: aggregator_openshelf

## Execution Contract

- 執行者必須完整交付 Phase 1（基礎核心框架）：包含 Dockerfile、docker-compose.yml（掛載 NAS Volume）、SQLite 3 + FTS5 資料庫管理模組、PyMuPDF 文本提取與 RapidOCR (ONNX CPU) 背景 Worker、FastAPI 後端（適配 Gateway `/libgen/` 前綴）、全繁體中文 Web 介面（含首頁搜尋、多欄位篩選、書目卡片、詳情彈窗與內嵌 PDF.js/EPUB.js 閱讀器），以及完整的自動化與 E2E 測試。
- 完成之判定標準為 `tests/test_e2e.py` 測試通過，且可於 `http://localhost:8000/libgen/`（或對應容器埠）正常載入首頁、完成 PDF/EPUB 入庫、繁簡全文搜尋與線上翻頁閱讀。

## Required Reads

- `plans/aggregator_openshelf/proposal.md` — 專案核心宗旨、背景與已關閉決策。
- `plans/aggregator_openshelf/design.md` — 系統架構圖、IDEF0 元件邊界（A1~A4）與 Gateway 路由設計。
- `plans/aggregator_openshelf/spec.md` — 核心保證條款與驗收情境。
- `plans/aggregator_openshelf/data-schema.json` — Work / Location / Identifier / FileObject / ReadingState 資料綱要。
- `plans/aggregator_openshelf/tasks.md` — Phase 1 具體執行項目與產出工件名稱。

## Stop Gates In Force

- **Gate 1 (Storage Mount)**：容器啟動時若 `/data/raw`、`/data/parsed`、`/data/db` 無法寫入，立即報錯停止。
- **Gate 2 (Gateway Routing)**：靜態資源（CSS/JS）與 API 請求必須完全支援 `/libgen/` 前綴，嚴禁在前端寫死絕對根路徑 `/`。

## Execution-Ready Checklist

- [x] 核心架構與文件模型已收斂並通過設計審查
- [x] Docker Compose 與 NAS 儲存路徑已定義
- [x] Phase 1 各項子任務均具備 machine-verifiable 工件定義
