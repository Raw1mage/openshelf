# Observability: aggregator_torrent-p2p-integration

## Events

- `[TorrentEngine] Announcing info_hash={hash} to {n} trackers...`
- `[TorrentEngine] Connected to {n} peers (download_speed={kbps} KB/s)`
- `[DownloadWorker] HTTP mirror failed, auto-failover to P2P for work_id={id}`
- `[DownloadWorker] P2P download completed for work_id={id}, verifying checksum...`

## Metrics

- `protocol: "http" | "torrent"`
- `peers_count: int`
- `speed_kbps: float`
- `progress_percent: float`
