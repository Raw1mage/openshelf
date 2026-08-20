# Specification: aggregator_torrent-p2p-integration

## Purpose

為 OpenShelf 提供 BitTorrent / Magnet (P2P) 搜尋與非同步下載能力，並透過雙軌調度器達成 HTTP 失效自動轉移至 P2P 下載的零摩擦體驗。

## Requirements

### Requirement: Torrent & Magnet 搜尋結果解析
系統在檢索 Libgen 或相容鏡像時，必須解析書目卡片中的 `.torrent` 檔案網址與 `magnet:?xt=urn:btih:...` URI 並存入 `Manifestation`。

#### Scenario: 成功提取書目 Magnet 與 Torrent
- **GIVEN** 使用者搜尋某本書籍，公網鏡像返回包含 Magnet 鏈結之 HTML
- **WHEN** `LibgenCrawler` 執行 DOM 解析
- **THEN** 提取之 `magnet_uri` 與 `torrent_url` 正確賦值並存入資料庫

### Requirement: 內嵌非同步 P2P 下載引擎
系統必須具備非同步 BitTorrent 下載模組，支援 Magnet 解析、Tracker 宣告、DHT 與單檔選擇性抓取。

#### Scenario: 透過 Magnet 下載單本圖書
- **GIVEN** 下載任務指定 `magnet:?xt=urn:btih:...`
- **WHEN** `P2PDownloadEngine` 啟動並連接 Peers
- **THEN** 成功抓取目標單檔 Pieces 並完成 MD5 校驗落地

### Requirement: DownloadWorker 雙軌智慧調度與 Failover
點擊下載時預設優先嘗試 HTTP 直鏈，若 HTTP 失敗或逾時則自動切換至 P2P 下載。

#### Scenario: HTTP 404 自動轉移 P2P
- **GIVEN** 書籍具備 HTTP 與 Magnet 兩種來源
- **WHEN** HTTP 鏡像返回 404 Not Found
- **THEN** 調度器自動轉移至 P2P 引擎繼續下載，完檔後自動觸發 IngestionPipeline

## Acceptance Checks

1. 檢索時能成功解析並存儲 `torrent_url` 與 `magnet_uri`。
2. `P2PDownloadEngine` 能根據 Magnet 連結完成單檔下載與校驗。
3. `DownloadWorker` 在 HTTP 404 時能自動無縫切換至 P2P 下載並完成 FTS5 索引。
