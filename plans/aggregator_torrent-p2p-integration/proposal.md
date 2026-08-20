# Proposal: aggregator_torrent-p2p-integration

## Why

目前 OpenShelf 的公網圖書檢索與下載主要依賴 HTTP 鏡像（如 `libgen.li`, `libgen.rocks`, `library.lol`）。然而公網環境存在以下挑戰：
1. **HTTP 鏡像容易遭受封鎖或失效**：第三方 HTTP 鏡像常因頻寬超載、網域封鎖或防盜鏈改版而暫時癱瘓。
2. **冷門/絕版大檔書籍缺失**：部分大容量（>50MB）或年代久遠的圖書在 HTTP 鏡像上容易遺失，但社群的 BitTorrent / Magnet 網路中仍由熱心節點做種保種。
3. **使用者體驗不能割裂**：使用者不應該需要安裝外部肥大的 BT 客戶端（如 qBittorrent）或手動匯出種子；系統應在背景自動調度 HTTP 與 P2P 協定，達成「點擊即收書、自動故障轉移（Failover）」的零摩擦極致體驗。

## What Changes

1. **資料模型擴充**：`SearchResultItem` 與 `Manifestation` 支援 `torrent_url` 與 `magnet_uri`。
2. **搜尋與爬蟲升級**：`LibgenCrawler` 檢索時自動解析並提取種子直鏈或 Magnet URI。
3. **內嵌輕量 P2P 下載引擎**：建立非同步 `P2PDownloadEngine`（支援 Magnet 解析、Tracker 宣告、DHT 與單檔選擇性 Piece 抓取）。
4. **雙軌調度器 (`DownloadWorker`)**：實作 HTTP 優先 ➔ HTTP 失敗時自動切換 P2P 下載之狀態機。
5. **落地管線與前端整合**：完檔後自動送入 `IngestionPipeline` 抽取文本與 FTS5 索引；前端佇列即時顯示 Peer 數，齒輪設定支援自訂 Tracker 池與頻寬上限。

## Capabilities

- **New Capabilities**:
  - `torrent-search-extraction`: 搜尋時自動提取種子與 Magnet 資訊。
  - `embedded-p2p-downloader`: 非同步 BitTorrent 下載與單檔抽取。
  - `auto-failover-download`: HTTP 失敗自動無感轉移至 P2P 下載。
  - `p2p-settings-management`: 齒輪設定面板支援 Tracker 伺服器池與連線速率限制。

- **Modified Capabilities**:
  - `download-worker`: 升級為支援 HTTP 與 P2P 雙軌智慧調度。
  - `queue-modal`: 支援呈現 P2P 任務狀態與 Peer 數量。

## Impact

- **Affected Systems**: `app/crawler/`, `app/models/`, `app/db/`, `app/static/`
- **Dependencies**: 輕量 Python P2P 庫（如 `aiobt` / `torrentool` / `torf` / `bencode.py`）
- **Performance**: P2P 下載在背景非同步執行，記憶體開銷低（< 50MB），不影響前端檢索與閱讀體驗。
