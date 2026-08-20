# BR-20260821_040000 — `dec5b44` 只搬了 DB：`raw_dir` 與 `parsed_dir` 仍在 NFS，且寫入跑在 event loop 執行緒上

- **Status**: OPEN
- **Owner**: ses_fe7b5cbadffeSlxj0dv1Z740O4（值星官）
- **Family**: `db-storage-substrate`
- **Severity**: 待定 —— **理論風險已坐實，實際危害尚未觀察到**（見「為何不是高」節）
- **Filed**: 2026-08-21 by ses_fe7b5cbadffeSlxj0dv1Z740O4
- **Found-by**: handler `ses_fdf8fc2c4ffeHyJI2iGo3sB5b8`（`[BRNS-ASK] round=1` 第三格）；
  dispatcher 獨立驗證掛載事實後建檔。**這格三張既有 BR 都沒有把它連起來。**

**Related**（每條都帶可引用的依據，非「感覺相關」）：

- `BR-20260820_160000-live-sqlite-on-nfs-latency-undecided-and-multihost-risk` —
  **同一個 commit 的另一半**。`dec5b44` 是該 BR 的處置，它把 `/data/db` 從 NFS 搬到
  ext4，**但同一份 `docker-compose.yml` 裡的 `/data/raw` 與 `/data/parsed` 兩行未動**。
  兩者是同一次遷移決策下的同一個檔案的相鄰行。
- `BR-20260820_210000-async-routes-sync-io-on-event-loop-family` —
  **本案是該 BR E 節的基質層事實**。E 節標的是 `download_worker.py:707` 的
  `f.write(chunk)` 跑在 event loop 執行緒上；本案指出那個 `f` 的落點是 NFS
  （`hard,timeo=600`）。E 節原本被歸類為「效能問題」，加上本案的事實後，
  它是目前唯一還能產生 20s 級全域停頓的機制。
- `BR-20260820_124500-quick-collections-modal-blocking` — **同一個觀察對象**
  （後端偶發長延遲），已與 BR-160000 合併判定。

## 一句話

`dec5b44` 把 SQLite 從 NFS 搬到本地 ext4，解決了 DB 路徑的 NFS 阻塞；
**但同一台服務的下載寫入（`raw_dir`）與轉檔寫入（`parsed_dir`）仍然落在
`hard,timeo=600` 的 NFS 上，而 `raw_dir` 的寫入是在 event loop 執行緒上做的同步 `write()`。**

## 證據（dispatcher 獨立實測，非採信 handler 自報）

### 掛載事實（權威來源：`docker inspect`，非讀 compose 推論）

```
$ docker inspect openshelf-app --format '{{range .Mounts}}{{.Type}} {{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
bind /home/pkcs12/projects/openshelf/app       -> /app/app
bind /nas/openshelf/raw                        -> /data/raw        ← NFS
bind /nas/openshelf/parsed                     -> /data/parsed     ← NFS
bind /home/pkcs12/projects/openshelf/data/db   -> /data/db         ← ext4（dec5b44 搬過）
bind /home/pkcs12/projects/openshelf/issues    -> /app/issues
```

### fstype 與掛載選項（附控制組）

```
$ df -T /nas
192.168.100.40:/volume1/docker/hyerasuno  nfs4  ...  /nas

CONTROL $ df -T data/db
/dev/sde  ext4  ...  /              ← 兩者確實不同，證明 df -T 有鑑別力

$ mount | grep -i nas
192.168.100.40:/volume1/docker/hyerasuno on /nas type nfs4
  (rw,vers=4.1,rsize=131072,wsize=131072,hard,proto=tcp,timeo=600,retrans=2,...)
```

**`hard` + `timeo=600`**：RPC 逾時 60 秒、無限重試。與歷史觀察到的 20-30 秒尖峰同一個數量級
（BR-160000 原文已記載此推論，但當時只把它套在 DB 上）。

### `dec5b44` 只動了一行（附控制組）

```
$ git show 73de0aa:docker-compose.yml | grep -n db
19:      - ${OPENSHELF_NAS_DIR:-/nas/openshelf}/db:/data/db      ← 搬移前

$ git show dec5b44:docker-compose.yml | grep -n db
22:      - ./data/db:/data/db                                     ← 搬移後

CONTROL $ git show zzzz_no_such_ref:docker-compose.yml
rc=1                                                              ← 證明 git show 對壞 ref 會失敗
```

`raw` / `parsed` 兩行在兩個版本間**逐字不變**。

### 落點鏈（程式碼層，逐跳可驗）

```
download_worker.py:108   _get_part_path() -> self.pipeline.storage.raw_dir / f"{job_id}_{md5}.part"
download_worker.py:703   with open(part_file, mode) as f:
download_worker.py:707       f.write(chunk)          ← BR-210000 E 節標的，同步呼叫
download_worker.py:732   part_file.replace(final_dest)

storage/manager.py:12    base_dir = os.getenv("DATA_DIR", "./data")
storage/manager.py:14    self.raw_dir    = self.base_dir / "raw"
storage/manager.py:15    self.parsed_dir = self.base_dir / "parsed"
```

`parsed_dir` 的寫入者（同樣落在 NFS）：

```
app/api/routes.py:122        converted_pdf = storage.parsed_dir / f"{work_id}.pdf"
app/pipeline/ingest.py:84    converted_pdf = self.storage.parsed_dir / f"{sha256}.pdf"
app/pipeline/ingest.py:174   （同上）
CONTROL grep 'zzz_not_a_dir' app/  →  0 命中（rc=1），證明上面的命中不是 pattern 太寬
```

## 為何 Severity 不是「高」——這格是本 BR 最重要的判斷

**理論機制成立，但歷史觀察到的尖峰形狀不符合它的預測。**

BR-160000 原文那組尖峰量測是四支探針同時打的，形狀是：

```
L2_collections  total=30.704   ← 尖峰（DB 路徑）
L3_search       total=30.845   ← 尖峰（DB 路徑）
L1_jobs         total=0.005    ← 同刻正常   ★ 關鍵
L0_404          total=0.002    ← 同刻正常
```

`L1_jobs` 打的是 `crawler_routes.py:179 async def list_download_jobs`，
**body 只讀記憶體 dict，不做任何 IO**——它與 `L2` 排在同一條 event loop 上。

**若 event loop 真的被 `f.write` 阻塞住 30 秒，`L1_jobs` 不可能是 5ms。**

所以：

| | |
|---|---|
| 本案假說預測的形狀 | 三支（含 `jobs` / `health`）**同時**尖峰 |
| 歷史實際觀察到的形狀 | **只有 DB 路徑**尖峰，event loop 探針同刻正常 |
| 結論 | **兩者不符** |

而那組量測發生時 DB 還在 NFS（`git show 73de0aa:docker-compose.yml:19`）。
**所以歷史 30.7s 的成因很可能就是「DB 在 NFS 上」，而那個已被 `dec5b44` 消除。**

⇒ **本案找到的是一個「尚未被觀察到的第二機制」，不是歷史尖峰的解釋。**
把它寫成後者會是誤歸因——而誤歸因會讓下一個人去修一個不是成因的東西。

**這也正是它需要被記錄的理由**：它是一條真實存在、但目前沒有觀測證據支持其已被觸發的路徑。
判它「高」會製造假急迫；判它「不存在」則是把一個坐實的機制當成沒有。

## 觸發條件（若要坐實，這是實驗設計）

本案要從「理論」升級為「實際危害」，需要同時滿足：

1. 有一個真實下載 job 正在寫 `raw_dir`（或 `ingest` 正在寫 `parsed_dir`）
2. 該時刻 NFS 發生 stall（`timeo=600` 級）
3. **同刻三支探針（`health` / `jobs` / `collections`）同時尖峰**

第 3 點是判準：**只有 `collections` 尖峰不算**，那是 DB 路徑（且 DB 已不在 NFS）。

已交給 handler `ses_fdf8fc2c4ffeHyJI2iGo3sB5b8` 的三支探針設計正好能分辨這兩者。
dispatcher 已裁示執行順序：先量真實負載下的尖峰形狀 → 量不到才人為製造 NFS 壓力 →
兩步都指向需要才授權建真實下載 job。

## 修法選項（未裁決）

| | 動什麼 | 代價 | 風險 |
|---|---|---|---|
| **A** | `raw_dir` 也搬到本地 ext4 | 書檔本體體積大，本地磁碟 45% 已用 | 儲存容量；且 NAS 是刻意選的永久儲存層（compose:16 註解明寫「體積大、以順序讀寫為主」） |
| **B** | 把 `f.write` 包 `run_in_threadpool`（＝BR-210000 E 節） | 一處改動 | **使用者已裁示暫緩**：修它會抹掉 BR-160000 觀察期的診斷訊號 |
| **C** | 先寫本地暫存、完成後搬到 NAS | 需要暫存空間 = 單檔大小 | 多一次複製；`part_file.replace()` 跨檔案系統會退化成 copy |
| **D** | 不修，記錄為已知理論風險 | 0 | 若哪天 NAS 抖動，全站停頓且無人知道原因 |

**選項 B 與 BR-210000 E 節是同一件事**，不要重複做。若使用者解除 E 節的暫緩，
本案的 `raw_dir` 那一半即隨之解決；`parsed_dir` 那一半不受影響（它走 `ingest`，
是否在 event loop 執行緒上未查——見下）。

## 沒驗證的

1. **`parsed_dir` 的寫入是否也在 event loop 執行緒上** —— 未查。`routes.py:122`
   在 `upload_book` 內，而該路由已於 BR-210000 C 節包了 `run_in_threadpool`；
   但 `ingest.py:84/174` 的呼叫路徑未逐條追。**不要假設它安全。**
2. **NAS 實際的抖動頻率** —— 完全沒有資料。沒有歷史 NFS stall 紀錄，
   所以「這條路徑多久會被觸發一次」是未知數，不是低。
3. **`part_file.replace()` 跨檔案系統的行為** —— 若採選項 C，`Path.replace` 在
   跨 mount 時會丟 `OSError: Invalid cross-device link`，需改用 `shutil.move`。未實測。
4. **本地磁碟能否容納 raw_dir** —— `df` 顯示 45% 已用（約 550GB 可用），
   但 NAS 上 raw 目前多大未量。選項 A 的可行性未評估。
5. **歷史尖峰的四支探針資料只有一組** —— BR-160000 原文那組是單次觀察。
   「event loop 探針同刻正常」這個關鍵反證只有一個樣本，
   **若日後量到相反形狀，本 BR 的 Severity 判定要重來。**
