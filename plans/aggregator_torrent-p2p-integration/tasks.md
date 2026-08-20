# Tasks: aggregator_torrent-p2p-integration

## Tasks

### Phase 1: 資料模型與 Torrent/Magnet 搜尋解析擴充
- [ ] Task 1.1: 擴充 `app/models/catalog.py`（在 `SearchResultItem`, `ManifestationCreate`, `DownloadJob` 中加入 `torrent_url`, `magnet_uri`, `download_protocol`, `peers_count`）。
- [ ] Task 1.2: 擴充 `app/db/schema.sql` 與 `app/db/dao.py` 支援 Torrent 相關屬性之讀寫與持久化。
- [ ] Task 1.3: 升級 `app/crawler/libgen_live.py` 與 HTML 解析適配器，在檢索時自動提取種子直鏈或 Magnet URI。

### Phase 2: 輕量內嵌式 P2P 下載引擎實作
- [ ] Task 2.1: 建立 `app/crawler/torrent_engine.py`，封裝非同步 BitTorrent / Magnet 下載模組（支援 Magnet 解析、Tracker 宣告、DHT 與 Peer 握手）。
- [ ] Task 2.2: 實作單檔選擇性 Piece 抓取（Selective Download）與 MD5 / SHA256 完整性校驗。
- [ ] Task 2.3: 實作完檔後背景做種定時器（Seeding Timer）與資源釋放機制。

### Phase 3: `DownloadWorker` 雙軌智慧調度與自動 Failover
- [ ] Task 3.1: 升級 `DownloadWorker`（`app/crawler/download_worker.py`），實作 HTTP 優先 ➔ 失敗自動轉移 P2P 的狀態機。
- [ ] Task 3.2: 統一下載進度回報通道，整合至 `/api/crawler/jobs` REST 端點。
- [ ] Task 3.3: 確保 P2P 下載完成之原檔無縫送入 `IngestionPipeline`，自動抽取 Markdown 與更新 FTS5 索引。

### Phase 4: 前端任務佇列與齒輪設定介面擴充
- [ ] Task 4.1: 更新 `app/static/js/app.js` 與 `app/static/index.html`，在下載佇列彈窗（Queue Modal）中清晰顯示下載協定（`HTTP` / `P2P`）與 Peer 數量。
- [ ] Task 4.2: 齒輪設定面板擴充「P2P / Torrent 下載偏好」（Tracker 伺服器池、頻寬上限、DHT 開關）。

### Phase 5: 自動化測試、品質驗證與文件同步
- [ ] Task 5.1: 撰寫 `tests/test_torrent_engine.py` 單元測試（驗證 Magnet 解析、Tracker 宣告與 Piece 下載）。
- [ ] Task 5.2: 撰寫 `tests/test_hybrid_download.py` 整合測試（驗證 HTTP ➔ P2P 自動 Failover 流程與 Ingestion Pipeline 連動）。
- [ ] Task 5.3: 同步更新 `docs/ARCHITECTURE.md` 與 `README.md`，納入 P2P 雙軌架構說明。
