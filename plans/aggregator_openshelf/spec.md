# Spec: aggregator_openshelf

## Purpose

本規格書規範 openshelf 系統之核心保證：在 Docker 容器與 NAS 儲存環境下，提供統一二層文件模型、雙份落地（原檔與純文字解析）、全文檢索與 Web 閱讀功能，並為後續公網目錄檢索與選擇性鏡像批次下載提供穩固地基。

## Requirements

### Requirement: 儲存與容器化部署 (Storage & Deployment)

系統 SHALL 透過 Docker Compose 部署，並將所有持久化資料（原檔、純文字、資料庫）掛載至外部指定儲存路徑（NAS Volume）。

#### Scenario: 容器初始化與路徑掛載
- **WHEN** 執行 `docker compose up -d`
- **THEN** 容器成功啟動，自動建立 `/data/raw`、`/data/parsed`、`/data/db` 目錄結構，並初始化 SQLite 資料庫與 FTS5 虛擬表。

---

### Requirement: 統一文件模型 (Unified Document Model)

系統 SHALL 以 `Work` 作為抽象書目實體，並以 `Location` 表達檔案實體（本地 NAS 檔案）或遠端來源（IPFS CID, HTTP, DOI, ISBN）。

#### Scenario: 本地書籍入庫
- **WHEN** 匯入本地 PDF/EPUB 檔案
- **THEN** 系統計算 SHA256/MD5，建立一筆 `Work` 紀錄與對應之本地 `Location` 紀錄，狀態標記為 `available_local`。

#### Scenario: 多來源表示
- **WHEN** 某書籍同時擁有本地 EPUB 檔案與遠端 IPFS/DOI 來源
- **THEN** 該 `Work` 關聯兩個 `Location` 項目，搜尋時僅呈現單一書籍卡片，詳情頁提供所有可用版本切換。

---

### Requirement: 雙份落地與解析管線 (Dual-Storage & Extraction Pipeline)

系統 SHALL 在儲存原始檔案之同時，將其完整解析為乾淨純文字（Markdown / TXT），並非同步寫入 `/data/parsed/` 及 FTS5 索引。

#### Scenario: 原生 PDF / EPUB 抽取
- **WHEN** 匯入具備文字層的 PDF 或 EPUB
- **THEN** PyMuPDF 於 2 秒內抽取完整章節文字，生成 Markdown 存檔並更新全文檢索索引。

#### Scenario: 掃描件 PDF 之 OCR 降級處理
- **WHEN** 匯入無文字層之掃描 PDF（每頁平均文字 < 30 字）
- **THEN** 系統判定為掃描件，將解析工作派發至 RapidOCR 背景 Worker，逐頁辨識文字後合併存入 `/data/parsed/` 並更新 FTS5 索引。

---

### Requirement: 反向代理閘道適配 (Gateway Subpath Routing)

系統 SHALL 支援在反向代理閘道之 `/libgen/` 子路徑下運行，所有靜態資源載入、API 請求與 Swagger 文檔均能正確解析路徑。

#### Scenario: 透過 `/libgen/` 子路徑訪問 Web 介面與 API
- **WHEN** 瀏覽器透過 `https://<domain>/libgen/` 訪問系統
- **THEN** 頁面所有 CSS/JS 資源與 API 請求均帶有正確之 `/libgen/` 前綴，無 404 或路徑錯誤。

---

### Requirement: 全繁體中文圖書檢索與閱讀 (Traditional Chinese UI & Reader)

系統 SHALL 提供全繁體中文之 Web 操作介面，包含搜尋框、格式/語言/年份篩選、書目詳情與內嵌閱讀器。

#### Scenario: 繁體中文搜尋與篩選
- **WHEN** 使用者輸入繁體中文字詞並指定語言或格式篩選
- **THEN** 系統透過 FTS5 Trigram/分詞精確返回匹配之繁簡書目列表，介面元素全數以標準繁體中文呈現。

---

## Acceptance Checks

- [ ] `docker compose up` 能在乾淨環境下一次成功啟動後端與 Web 介面。
- [ ] 透過 `/libgen/` 子路徑訪問時，靜態首頁與 API 能正常運作且無路徑斷鏈。
- [ ] 放入原生 PDF 檔案，5 秒內完成入庫、純文字抽取與 FTS5 索引建立。
- [ ] 放入掃描件 PDF 檔案，背景 Worker 能自動完成 OCR 並建立全文索引。
- [ ] Web 介面能以繁體中文透過關鍵字精確搜尋到原書內文片段。
- [ ] Web 閱讀器能正確開啟 PDF/EPUB 並記憶當前閱讀頁碼。
