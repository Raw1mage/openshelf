# Errors: aggregator_torrent-p2p-integration

## Error Catalogue

| Error Code | Description | Handling |
| :--- | :--- | :--- |
| `ERR_P2P_NO_PEERS` | 宣告 Tracker 與 DHT 後未找到任何活躍 Peers | 指數退避重試宣告，或標記任務失敗 |
| `ERR_P2P_INVALID_MAGNET` | Magnet URI 格式異常或缺少 info_hash | 拋出 400 Bad Request 並記錄日誌 |
| `ERR_P2P_PIECE_CORRUPTED` | 下載之 Piece 雜湊與種子 metadata 不符 | 丟棄該 Piece 並重新向其他 Peer 請求 |
| `ERR_P2P_CHECKSUM_MISMATCH` | 完整檔案 MD5 與書目資料不符 | 隔離暫存檔並重試下載 |
