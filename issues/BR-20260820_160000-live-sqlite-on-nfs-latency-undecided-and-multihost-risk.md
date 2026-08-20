# BR-20260820_160000 — 線上 SQLite 位於 NFS 掛載：偶發 20-30s 尖峰成因 UNDECIDABLE，且多主機擴充有資料損毀風險

Status: **PARTIAL** —— 使用者已拍板選 B（DB 搬離 NFS），搬移已執行並 commit `dec5b44`。
  判準 6（資料完整）、7（還原演練）已達成。**判準 8（尖峰是否消失）未達成**：
  搬移後只量到 idle（3.0-3.8ms），而尖峰本來就是偶發的——
  **「我沒量到」與「它不存在」共用同一個輸出**，需觀察期才能結案。詳見「處置」節。
Owner: ses_fe7b5cbadffeSlxj0dv1Z740O4（值星官）
Family: db-storage-substrate
Filed: 2026-08-20 by ses_fe7b5cbadffeSlxj0dv1Z740O4
Revised: 2026-08-20 by ses_fe7b5cbadffeSlxj0dv1Z740O4
  ← **核心假說已被實測推翻並改寫**，見下方「勘誤」節。原標題為
    `live-sqlite-on-nfs-wal-silently-unavailable`，該標題主張的事實不成立。
Found-during: 驗收 `73de0aa`（引導快取修復）時的獨立複核，非 handler 回報

**Related**:
- `BR-20260820_124500-quick-collections-modal-blocking`（PARTIAL）— **同一條執行路徑**。
  該案的後端 20-27s 根因之一已由 `73de0aa` 修掉（每請求重跑 schema 引導），
  但**修復生效後我仍量到 30.70s / 23.51s 兩次尖峰**，成因指向本案。
  兩案是同一個症狀的兩層：一層是應用層冗餘寫入，一層是儲存層基質。
- `BR-20260820_111523-mirror-resolver-dead-mirrors`（closed，`16890d7`）—
  **同一失效類別**：「設定成功」與「實際能用」共用同一個輸出。
  該案在鏡像健康層（查封頁回 200）。**本案原本主張的同類實例已被推翻**——
  真正踩到這個失效類別的是**我自己的量測工具**（`ls` 看不到 `-wal` ⇒ 誤判 WAL 不可用），
  見「勘誤」節。這使本案成為該失效類別的實例，只是主體從系統換成了觀察者。

---

## 勘誤（2026-08-20，本 BR 的核心假說已被推翻）

**原主張**：SQLite 的 WAL 在 NFS 上不能運作，`PRAGMA journal_mode` 回 `wal` 但實際不是。

**實測結果：該主張錯誤。WAL 在 `/data/db` 這個 NFS4 卷上真的在工作。**

由 handler `ses_fe1e42061ffelY9nWWmlTw3GdZ` 於 `[BRNS-ASK round=1]` 提出更正，
值星官獨立重做全部實驗後採納（容器內獨立探針檔，未觸碰線上 DB，跑完即刪）：

```
FSTYPE_datadb                = nfs            ← 確認仍是 NFS，掛載事實不變
CONTROL_FSTYPE_appapp        = ext2/ext3      ← 證明 stat 有鑑別力

A1_SET_MODE_RETURNED         = wal
A2_AFTER_COMMIT  wal_bytes   = 12392
                 shm_bytes   = 32768          ← ★ shm 共享記憶體真的分配了
A3_SECOND_CONN_READS         = [('hello',)]   ← 跨連線可見性成立

C1_DEFAULT_MODE_SAME_DIR     = delete         ← ★ 控制組：同目錄新建 DB 預設是
                                                 delete，證明 A1 的 wal 不是預設值
CLEANED_removed = 4  LEFTOVER = []  CONTROL_live_db_untouched = True
```

### 為什麼原本會判錯：踩到的正是本 BR 家族的核心失效類別

原證據是「`ls /data/db/openshelf.sqlite*` 只有主檔，無 `-wal` 無 `-shm`」。
**但 SQLite 在最後一個連線關閉時會自動刪除 `-wal` 與 `-shm`。** 實測生命週期：

```
D1_WHILE_OPEN    wal=True   shm=True
D2_AFTER_CLOSE   wal=False  shm=False        ← 正常清理，不是壞掉
D3_MAIN_DB_STILL_EXISTS = True   CONTROL_bogus = False
```

於是 **「WAL 壞掉了」與「此刻剛好沒有連線開著」共用同一個輸出**，
而那個 `ls` 恰好跑在沒有活躍連線的瞬間。
這是本包第三個同類實例，**踩到的是觀察者（我），不是系統**。

### 但 handler 用來證明 WAL 的那格也無鑑別力（雙向勘誤）

handler 主張「writer 持鎖時 reader 在 0.1ms 讀到資料 ⇒ WAL 核心保證成立」。
值星官加上 rollback journal 控制組後，該推論不成立：

```
B1_WAL_READ_DURING_WRITER     mode=wal     rows=1  0.0ms
B2_DELETE_READ_DURING_WRITER  mode=delete  rows=1  2.3ms   ← 也沒被阻塞
```

`BEGIN IMMEDIATE` 只取得 RESERVED 鎖、永不升級 EXCLUSIVE，
**兩種 journal 模式下讀取都不會被阻塞**，故該測試無法區分 WAL 與非 WAL。
**WAL 成立的依據是 A2 的 `shm_bytes=32768` 實際分配 + C1 控制組，不是 B1。**

### 仍然成立的部分：多主機並發風險

WAL 的 `-shm` 靠 mmap 共享記憶體協調多個連線。
- **單主機存取**（當前形態：一個容器）→ 正常，上方已證。
- **多主機並發**（未來若加第二台主機掛同一個 NFS 匯出）→ 跨主機 mmap 不一致，
  SQLite 官方文件明確警告可能**靜默資料損毀**。

**⇒ 現狀安全，但這個部署形態禁止再掛第二台主機存取同一個 DB 檔。**
這格是本 BR 保留下來的唯一原始主張，理由已從「WAL 不可用」換成「WAL 的跨主機前提不成立」。

## 一句話

線上 DB 位於 **NFS4 掛載**（`timeo=600,hard`）。`73de0aa` 修掉應用層冗餘寫入後，
`/api/collections` 與 `/api/search` **仍偶發 20-30 秒尖峰且回 HTTP 200**，
同刻不碰 DB 的端點正常。**成因未坐實（UNDECIDABLE）**，兩個候選機制都未被排除：
SQLite 鎖爭用的累計等待、或 NFS RPC 層 I/O 阻塞。
此外，當前 NFS 部署形態使 WAL 的跨主機前提不成立，**禁止再掛第二台主機**。

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

### WAL 狀態（已勘誤，見上方「勘誤」節）

```
PRAGMA journal_mode 查詢回傳    wal
CONTROL PRAGMA page_count      19715          ← 證明查詢管道是通的
A2 探針 shm_bytes              32768          ← WAL 真的在這個 NFS 卷上工作
C1 控制組 同目錄新建 DB 預設    delete         ← 證明上一行不是預設值
```

`ls` 看不到 `-wal`/`-shm` 是**連線關閉後的正常清理**，不是降級證據。

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

### 兩個候選機制，都未被排除（這格 UNDECIDABLE）

後續持續 25 次量測**零尖峰**（0.040-0.089s，`slow_over_1s = 0/25`）。
**無法按需重現**那兩次尖峰。

#### 候選 1：SQLite 鎖爭用的**累計**等待

handler 主張「`total=30.845 > timeout=30.0` 且 HTTP 200 ⇒ 排除 SQLite busy timeout」，
因為若是鎖等待就該在 30.0s 拋 `OperationalError` 並回 500。

**值星官實測推翻此推論：`timeout` 是 per-lock-attempt，不是 per-request 預算。**

```
per-stmt timeout = 1000ms，holder 每次持鎖 800ms，連續三次：
  E_stmt1 waited_ms=892
  E_stmt2 waited_ms=739
  E_stmt3 waited_ms=748
  E1_TOTAL_ms=2832   EXCEPTION=None        ← ★ 累計 2.8x 單次 timeout，未拋錯

CONTROL E4  無鎖連續 3 次 INSERT = 24.6ms  ← 證明上面的等待真的來自鎖
CONTROL E5  持鎖 2.5s vs timeout 1.0s
            → elapsed 1003ms, OperationalError: database is locked
CONTROL E6  該拋錯時真的拋錯 = True         ← 證明這個判準有鑑別力
```

**⇒ 一個請求只要做多次寫入交易，每次都在 timeout 內拿到鎖，
總耗時就能任意超過 30s 而永遠回 200。** 而本 repo 的請求路徑正是如此：
`init_database()` 的 `executescript(schema.sql)`、`apply_column_migrations()`、
`seed_categories_if_needed()` 是**三個獨立的寫入交易**。

`73de0aa` 已把這三者移出每請求路徑，**但下載入庫等真實寫入者仍會產生鎖爭用**，
且其他路徑（如 `crawler_routes.py:60` 迴圈內建 `CatalogDAO()`）仍可能多次取鎖。

#### 候選 2：NFS RPC 層 I/O 阻塞

`timeo=600` = 60 秒 RPC 逾時，`hard` = 無限重試。網路抖動時單次 `read()`
可 block 到 60 秒，對 SQLite 與 threadpool 都不可見，且 L0/L1 不碰 `/data/db` 故正常。
形狀吻合（無固定上限、HTTP 200、只有碰 DB 的層慢、無法按需重現）。

**handler 提出的可證偽預測：尖峰時 NFS retrans 計數應增加。目前該訊號不在：**

```
HOST /proc/net/rpc/nfs   rpc_calls=620581   retrans=0
CONTROL awk 讀不存在的 proc 檔 → fatal: cannot open   ← 證明 awk 真的讀得到東西
```

但這是**開機以來的累計值且涵蓋全主機**，不能排除「尖峰當下有、但被錯過」。
要坐實需在尖峰**當下**取樣。

#### 結論

- **已證實**：DB 在 NFS4 上（`timeo=600,hard`）。
- **已證實**：WAL 在此卷上真的可用（`shm_bytes=32768` + `delete` 控制組）。
- **已證實**：`73de0aa` 的修復有效（持鎖對照 20.9s → 0.029s）。
- **已證實**：`total > timeout` + HTTP 200 **不能**排除鎖爭用（E1/E4/E5/E6）。
- **UNDECIDABLE**：那兩次 30.70s / 23.51s 的確切成因。候選 1 與候選 2 都未排除。

**「我沒量到」與「它不存在」共用同一個輸出**——這正是本 BR 家族的核心失效類別，
所以寫 UNDECIDABLE 而非「已排除」。

## 為什麼這格重要

1. **使用者可感知**：`/api/collections` 與 `/api/search` 偶發 20-30 秒無回應，
   而前端沒有任何超時提示（回 200，只是很慢）。
2. **多主機擴充禁令**：當前形態下 WAL 正常，但 `-shm` 靠 mmap 協調，
   **跨主機並發存取同一 NFS 上的 DB 會靜默資料損毀**（SQLite 官方警告）。
   這不是「現在壞了」，是「這個部署形態鎖死了未來的擴充路徑」，需明文記錄。
3. **診斷成本**：兩個候選機制需要不同的觀測手段，而目前**兩者都沒有常設觀測**。
   下一次尖峰發生時，仍然只能事後猜。

## 選項（需使用者拍板，AI 不得自行決定）

| 選項 | 代價 | 得到什麼 |
|---|---|---|
| **A. 先加常設觀測，尖峰當下自動取證** | 一包實作工；不直接解決問題 | 把 UNDECIDABLE 變成可判定。慢請求（>2s）時自動 dump NFS retrans delta + SQLite 鎖狀態 + 該請求的寫入交易次數，**一刀切開候選 1 與候選 2** |
| **B. DB 移到容器本地 volume，NAS 只放書檔** | 需搬移 + 改 compose；DB 不再隨 NAS 備份；不可逆 | 直接消除候選 2 與多主機風險。若尖峰仍在 ⇒ 反證候選 1 |
| **C. 維持現狀** | 風險與不可診斷性都留著 | — |

~~**建議 A，理由已改變**：原本建議 B（當時以為 WAL 不可用，B 是修 bug）。
現在 WAL 沒壞、根因未定，在未定位前搬移部署拓撲是在沒有證據的地方動刀。~~

**⚠ 上述建議已被使用者裁示覆蓋（2026-08-20）。使用者選 B，見下方「處置」節。**
保留原建議文字是為了讓「AI 建議 A、使用者選 B」這個分歧可被後人看見——
**刪掉它會讓這份 BR 看起來像 AI 一開始就建議 B**，那是事後合理化。

## 處置（2026-08-20，使用者拍板選 B）

使用者選 **B 的具體化**：DB 放 **WSL 本地 repo 內 `data/db/`**（ext4），
而非原選項描述的「容器本地 volume」。差別在於前者是 host 端可直接存取的
bind mount，備份與檢查都不需進容器。

### 實際執行（commit `dec5b44`）

```
1. 備份           NAS 的 openshelf.sqlite + download_jobs.json → ~/openshelf-db-backup-20260820/（0700）
2. 還原演練       integrity=ok / work 37 rows / 18 tables / FTS5 'operating'=9
                  CONTROL 亂字串=0、不存在的 DB rc=1   ← 證明這組查詢有鑑別力
3. 舊空殼處置     repo 內原有的 114KB 空殼（work 0 rows / 12 tables）
                  改名 openshelf.sqlite.stale-empty-20260820 保留，未刪
                  CONTROL 空殼 0 rows vs 線上 37 rows  ← 證明我搬的不是空殼
4. 搬移           NAS → repo data/db/，權限 666/777 供容器 uid 寫入
5. compose        ${OPENSHELF_NAS_DIR}/db:/data/db  →  ./data/db:/data/db
                  raw/parsed 仍留 NAS（書檔本體，不受 SQLite 鎖語意影響）
6. 起容器         health 200 / collections 200
```

**程式碼零改動**——`app/storage/manager.py:96` 由 `DATA_DIR=/data` 推導路徑，
是容器內視角，掛載換了它不知道也不需要知道。

### 量測（搬移後）

```
/api/collections   40ms  →  3.0-3.8ms      約 11 倍
/api/search        0.392-0.394s（穩定，非尖峰）
CONTROL 404        1.8ms

fstype  容器內 /data/db = ext2/ext3
        容器內 /data/raw = nfs             ← 控制組，證明 stat -f 有鑑別力

WAL     連線期間 wal=16512 shm=32768
        關閉後兩者消失                      ← SQLite 正常清理
        CONTROL bogus path exists = False
```

**`/api/search` 的 0.39s 與掛載無關**（穩定值非尖峰，且 collections 已降到 3ms）。
那格指向 FTS5 查詢本身或 search 路徑的其他成本，是本 BR 之外的獨立問題。

### 備份機制（配套，同一 commit）

DB 搬離 NAS 後就不在 NAS 備份範圍內。新增 `script/backup-db.sh`，
cron 每日 04:30 備份回 `/nas/openshelf/db-backup/`，保留 14 天。

**用 `sqlite3 .backup` 而非 `cp`**——`cp` 會抓到寫入中的不一致快照
（WAL 模式下 `-wal` 尚未 checkpoint）。`.backup` 走 Online Backup API，
產出交易一致的快照且不需停服務。

script 的每一步都讓失敗態與缺席態產生不同輸出，且**備份後驗
`integrity_check` + 資料筆數**——「備份檔存在」不等於「備份可還原」。
已實測：cron 產出的 NAS 備份還原演練通過。

### Rollback 路徑

NAS 上原本的 `/nas/openshelf/db/` **未刪**。要回退只需把
`docker-compose.yml:22` 改回 `${OPENSHELF_NAS_DIR:-/nas/openshelf}/db:/data/db`
並重啟容器。**但回退會遺失搬移後寫入的資料**——NAS 那份停在 2026-08-20 14:21。

### 這個處置改變了什麼、沒改變什麼

| | |
|---|---|
| ✅ 消除候選 2（NFS I/O 阻塞） | DB 不再走 NFS，`timeo=600,hard` 對它不再適用 |
| ✅ 消除多主機資料損毀風險 | `-shm` 不再跨主機協調 |
| ✅ idle 效能 11 倍 | 這是副產物不是目標 |
| ❌ **未證明尖峰已消失** | 見驗收判準第 8 條——需觀察期 |
| ❌ 未排除候選 1（鎖爭用累計） | 若搬移後仍有尖峰，反而**坐實**候選 1 |

**候選 1 仍然活著。** 搬移消除的是候選 2 的**機制**，不是候選 1。
若觀察期內尖峰仍發生，那就是候選 1 的直接證據，且屆時選項 A
（常設觀測）會變成必要而非可選。

## 驗收判準

### 共通（無論選哪個方案）

1. `pytest` 不得下降（**基線 163 passed**，`.venv/bin/python -m pytest`）。
   ⚠ 本行原寫 150，是舊值——handler `ses_fe18eab55ffeEQU8vBGV8DrmVd` 在 BR-143000
   推翻過同一個錯誤。BR 內文的基線數字會隨其他包推進而過期，**引用前先重量**。
2. **任何宣稱「已排除某候選」的證據，必須附能證明該檢查有鑑別力的控制組。**
   本 BR 已有兩次「看起來成立、實則無鑑別力」的推論（`ls` 無 `-wal`、
   `total > timeout` + 200），兩次都是缺控制組。

### 若選 A（常設觀測）

3. 觀測必須**一刀切開兩個候選**，各自需要不同訊號：
   - **候選 1（鎖爭用累計）**：記錄該請求內**寫入交易次數**與**每次取鎖等待時間**。
     只記總耗時不算——那正是 `total > timeout` 那個誤判的來源。
   - **候選 2（NFS I/O）**：尖峰**當下**取 `/proc/net/rpc/nfs` 的 retrans **delta**
     （不是累計值），以及該請求期間的 `read()` 阻塞時間。
4. 觸發門檻需可設定，且**觸發與未觸發必須產生不同輸出**——
   若慢請求與正常請求都不寫任何東西，這個觀測無法證明自己在工作。
   需一個「人為製造慢請求 ⇒ 確實產出取證記錄」的正向測試。
5. 不得在 `async def` 內做同步 I/O 取證（會複製 `crawler_routes.py:47,60` 的既有缺陷）。

### 若選 B（搬移 DB）

6. ✅ **已達成** — 搬移後 API `total=37`、直查 `work` 37 rows、`integrity_check=ok`、
   18 tables，FTS5 `MATCH 'operating'` 回 9（控制組：亂字串回 0，證明是真比對）。
7. ✅ **已達成** — 兩次還原演練皆通過（搬移前的家目錄備份、以及 cron 產出的 NAS 備份），
   每次都驗 `integrity_check` + 資料筆數 + FTS5 可查 + 控制組。
8. ❌ **未達成，這是本 BR 仍為 PARTIAL 的唯一原因。**
   搬移後只量到 idle 值，**未經過足以觀察到偶發尖峰的時間窗**。
   原始尖峰在數小時的使用中只出現數次，而我的量測窗口是分鐘級。
   **不得以「搬移後沒量到尖峰」宣稱候選 2 已排除**——那正是本 BR 已犯過兩次的錯。
   結案條件：累積足夠使用時數後，`/api/collections` 與 `/api/search` 未再出現 >2s 請求。

## 沒驗證的

- **未在尖峰當下取樣。** retrans=0 是開機以來的全主機累計值，
  不能排除「尖峰當下有 retrans、但被錯過」。這是候選 2 未被排除的唯一原因。
- **未實測 `/data/db` 的 I/O latency 分布**（只有 API 層的端到端時間）。
- **未量真實下載入庫路徑持鎖的時長與頻率。** 所有鎖實驗都是 `BEGIN IMMEDIATE` 合成，
  形狀一致（RESERVED 鎖）但不是同一條 code path。
- **未查證 Synology NAS 端的 NFS 設定**（`nolock` / `local_lock` 會改變鎖行為）。
  目前掛載選項是 `local_lock=none`，意即鎖走 NLM/NFSv4 伺服器端——這格值得查。
- **未評估搬移 DB 對現有備份策略的影響。**
- **未量 `crawler_routes.py:60` 迴圈內建 `CatalogDAO()` 在 `73de0aa` 之後
  是否仍構成多次取鎖**（`73de0aa` 讓建構子變便宜，但未消除連線建立本身）。
