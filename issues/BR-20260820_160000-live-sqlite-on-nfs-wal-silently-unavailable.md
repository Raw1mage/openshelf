# BR-20260820_160000 — 線上 SQLite 位於 NFS 掛載，WAL 在網路檔案系統上不可靠且靜默降級

Status: OPEN
Owner: ses_fe7b5cbadffeSlxj0dv1Z740O4（值星官）
Family: db-storage-substrate
Filed: 2026-08-20 by ses_fe7b5cbadffeSlxj0dv1Z740O4
Found-during: 驗收 `73de0aa`（引導快取修復）時的獨立複核，非 handler 回報

**Related**:
- `BR-20260820_124500-quick-collections-modal-blocking`（PARTIAL）— **同一條執行路徑**。
  該案的後端 20-27s 根因之一已由 `73de0aa` 修掉（每請求重跑 schema 引導），
  但**修復生效後我仍量到 30.70s / 23.51s 兩次尖峰**，成因指向本案。
  兩案是同一個症狀的兩層：一層是應用層冗餘寫入，一層是儲存層基質。
- `BR-20260820_111523-mirror-resolver-dead-mirrors`（closed，`16890d7`）—
  **同一失效類別**：「設定成功」與「實際能用」共用同一個輸出。
  該案在鏡像健康層（查封頁回 200），本案在 `PRAGMA journal_mode` 層（回 `wal` 但 WAL 不可用）。

## 一句話

線上 DB 位於 **NFS4 掛載**，而 **SQLite 的 WAL 模式在網路檔案系統上不能運作**
（`-shm` 需要 mmap 共享記憶體）。但 `PRAGMA journal_mode` 查詢**仍然回傳 `wal`**，
於是「WAL 已啟用」與「WAL 實際不可用」共用同一個輸出，
整個系統在一個自以為是 WAL、實際上不是的基質上運作。

## 證據

### 掛載事實

```
容器內 /proc/mounts：
  192.168.100.40:/volume1/docker/hyerasuno/openshelf/db  /data/db  nfs4
  rw,relatime,vers=4.1,rsize=131072,wsize=131072,hard,proto=tcp,timeo=600,retrans=2

stat -f -c "%T" /data/db   →  nfs
CONTROL stat -f -c "%T" /app/app  →  ext2/ext3     ← 證明 stat 有鑑別力，不是恆回 nfs
```

`docker-compose.yml:19`：`${OPENSHELF_NAS_DIR:-/nas/openshelf}/db:/data/db`

### 靜默降級的直接證據

```
PRAGMA journal_mode 查詢回傳    wal
CONTROL PRAGMA page_count      19715          ← 證明查詢管道是通的
ls -la /data/db/openshelf.sqlite*
  -rwxrwxrwx 1 1024 users 80752640 openshelf.sqlite
  （無 -wal，無 -shm）
```

**DB 有活躍連線的情況下，真正運作於 WAL 模式時 `-wal` 與 `-shm` 兩個檔必然存在。**
它們不在，而 pragma 仍回 `wal`。

### 修復生效後仍存在的尖峰（本案的實際危害）

在 `73de0aa` 已生效（bind-mount + `--reload`，存檔即上線）之後，我獨立量到：

```
第一輪（我尚未下任何鎖）
  L2_collections  total=30.704  ttfb=30.704  http=200    ← 逼近 timeout=30.0 上限
  L3_search       total=30.845  ttfb=30.845  http=200
  L0_404          total=0.002                            ← 同刻正常
  L1_jobs         total=0.005                            ← 同刻正常

我持鎖 22s 期間
  L2_collections  total=0.029                            ← 反而正常（修復確實生效）

鎖釋放後
  L2_after        total=23.507  ttfb=23.507
  L3_after        total=23.621
```

**兩件事同時成立**：修復真的生效了（持鎖期間 0.029s，修前同條件 20.9s），
但**沒有明顯寫入者時反而卡到 30 秒天花板**。`L0_404` 同刻恆為 0.002s
⇒ 不是 threadpool、不是事件迴圈。慢的是 DB 存取本身。

`timeo=600` 是 NFS 的 60 秒 RPC 逾時，`hard` 表示無限重試——
與觀察到的 20-30 秒尖峰在同一個數量級。

### 但這格是 UNDECIDABLE，不是已證實

後續持續 25 次量測**零尖峰**（0.040-0.089s，`slow_over_1s = 0/25`）。
我**無法按需重現**那兩次尖峰。所以：

- **已證實**：DB 在 NFS 上；WAL 檔案不存在；pragma 仍回 wal。
- **已證實**：`73de0aa` 的修復有效（持鎖對照 20.9s → 0.029s）。
- **UNDECIDABLE**：那兩次 30.70s / 23.51s 的確切成因。NFS 是最強嫌疑，但未坐實。

**「我沒量到」與「它不存在」共用同一個輸出**——這正是本 BR 家族的核心失效類別，
所以我寫 UNDECIDABLE 而非「已排除」。

## 為什麼這格重要

1. **WAL 的核心保證是「讀不阻塞寫、寫不阻塞讀」。** 若實際跑在 rollback journal 上，
   任何寫入都會**排他鎖住整個 DB**，讀取請求只能等 `timeout=30.0`。
   前一顆 handler 量到「純 SELECT 持鎖下 3.2ms 不受阻」——那是在鎖**未實際持有寫入**的情況下，
   不足以證明 WAL 真的在工作。
2. **NFS 上的 SQLite 鎖依賴 POSIX advisory lock**，在 NFS 上實作不完整且已知有故障模式，
   SQLite 官方文件明確警告資料庫**可能損毀**（不只是慢）。
3. **這是資料完整性風險，不只是效能問題。** 80MB 的書庫是使用者的資產。

## 選項（需使用者拍板，AI 不得自行決定）

| 選項 | 代價 | 得到什麼 |
|---|---|---|
| **A. DB 移到容器本地 volume，NAS 只放書檔** | 需搬移 + 改 compose；DB 不再隨 NAS 備份 | WAL 真的可用，鎖語義正確，資料完整性有保障 |
| **B. 維持 NFS，明確改用 `journal_mode=DELETE`** | 寫入期間完全阻塞讀取（效能更差但誠實） | 消除「以為是 WAL」的錯覺，行為可預期 |
| **C. 維持現狀，只加監測** | 風險留著 | 至少知道它何時發作 |

**我的建議是 A**，但這動到部署拓撲與備份策略，**是產品決策不是實作細節**，不由 AI 決定。

## 驗收判準（無論選哪個方案）

1. **必須有一個能區分「WAL 真的在用」與「pragma 說它在用」的檢查**，
   並在啟動時執行、不一致時 `log.warning`。
   最低限度：查 `journal_mode` **並且**檢查 `-wal` 檔是否存在，兩者不一致就出聲。
   **只查 pragma 不算**——那正是本 BR 的病。
2. 該檢查需兩個方向的測試：本地 ext4 上 → 一致，無 warning；模擬不一致 → 有 warning。
   只測一邊的話，恆真或恆假的實作都會通過。
3. 若選 A，需證明搬移後 `-wal` 與 `-shm` 檔真的出現，且既有 37 筆 work 資料完整。
4. `pytest` 不得下降（當前基線 **150 passed**，`.venv/bin/python -m pytest`）。

## 沒驗證的

- **未實測 NFS RPC 層的實際延遲**（未量 `/data/db` 的 I/O latency）。
- **未證明那兩次尖峰確實由 NFS 造成**。時序吻合、數量級吻合、L0/L1 同刻正常，
  但未取得直接證據（需在尖峰當下 dump NFS 統計或 SQLite 鎖狀態）。
- **未查證 Synology NAS 端的 NFS 設定**（`nolock` / `local_lock` 選項會改變鎖行為）。
  目前掛載選項是 `local_lock=none`，意即鎖走 NLM/NFSv4 伺服器端——這格值得查。
- **未評估搬移 DB 對現有備份策略的影響**。
