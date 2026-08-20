# Handoff: aggregator_torrent-p2p-integration

## Execution Contract

- **交付目標**：完成 Torrent / Magnet 搜尋解析、內嵌輕量 P2P 下載引擎、DownloadWorker 雙軌智慧調度（HTTP 優先 ➔ P2P 自動 Failover）與前端/齒輪設定整合。
- **完成定義 (Definition of Done)**：
  1. 檢索時能成功解析並存儲 `torrent_url` 與 `magnet_uri`。
  2. `P2PDownloadEngine` 能根據 Magnet/Torrent 連結獨立完成檔案下載與雜湊校驗。
  3. `DownloadWorker` 在 HTTP 404/5xx 失敗時能自動無縫切換至 P2P 下載，並在完檔後自動觸發 `IngestionPipeline` 落地 `/data/raw` 與更新 FTS5 索引。
  4. 齒輪設定面板支援自訂 Tracker 節點池與頻寬限制。
  5. 全套單元測試與整合測試（`pytest`）100% 通過。

## Required Reads

- [proposal.md](proposal.md) — 專案願景與範圍定義
- [design.md](design.md) — 雙軌調度與 P2P 下載架構設計
- [tasks.md](tasks.md) — 5 階段詳細實作任務清單
- [app/crawler/libgen_live.py](../../app/crawler/libgen_live.py) — 既有爬蟲與解析邏輯
- [app/crawler/download_worker.py](../../app/crawler/download_worker.py) — 既有非同步下載佇列
- [app/pipeline/ingest.py](../../app/pipeline/ingest.py) — 文本抽取與 FTS5 索引管線

## Stop Gates In Force

- **Gate 1**: 資料模型擴充與 Torrent/Magnet 解析驗證通過（Phase 1 完成）。
- **Gate 2**: P2P 下載引擎與單檔提取獨立測試通過（Phase 2 完成）。
- **Gate 3**: 雙軌調度與 Ingestion Pipeline 整合測試通過（Phase 3 完成）。

## Execution-Ready Checklist

- [x] Docker live bind mount 環境運作正常
- [x] 既有 16 項測試全部 PASS
- [x] IDEF0 與 GRAFCET 模型定義完備
