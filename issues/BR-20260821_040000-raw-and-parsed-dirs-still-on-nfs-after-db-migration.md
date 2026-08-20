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

### ⬆ 2026-08-21 追加：上面這五個引用點已逐點判定（原「沒驗證的 #1」已結案）

由 subagent `ses_fdf669310` 唯讀調查，dispatcher 覆核控制組後採納。
**結論推翻了本 BR 原本的部分寫法**（見下方「本節推翻了什麼」）：

| 引用點 | 所在函式 | 在 event loop 上？ | 真 IO 還是路徑拼接 |
|---|---|---|---|
| `routes.py:122` | `get_raw_file` (`routes.py:84`，**sync def**) | **否 — threadpool** | **純 `Path.__truediv__` 拼接，零 syscall** |
| `manager.py:15` | `StorageManager.__init__` | — | 純拼接 |
| **`manager.py:22`** | `ensure_directories`（`__init__:17` 無條件呼叫） | 否（threadpool） | **真 IO — 見下節，每請求一次** |
| `ingest.py:84` | `ingest_bytes` (`ingest.py:41`) | **否** — 唯一呼叫者 `routes.py:168-169` 已包 `run_in_threadpool` | 該行拼接；`ingest.py:85` `convert_to_pdf` 才是真寫入 |
| **`ingest.py:174`** | `process_file` (`ingest.py:128`) | **是 ⚠** | `ingest.py:175` `convert_to_pdf` + `ingest.py:198` `save_parsed_markdown` **都在 event loop 上寫 NFS** |

**唯一在 event loop 上的 `parsed` 寫入是 `ingest.py:174` 那條**，呼叫鏈：

```
ingest.py:174  process_file (sync，重量級)
  ← download_worker.py:739   直接呼叫，未包 run_in_threadpool / to_thread   ★漏網點
  ← download_worker.py:626   async def _execute_download_with_resume
  ← download_worker.py:614   async def _process_queue
  ← download_worker.py:199   loop.create_task(...)
  ← main.py:26-27            lifespan → worker.start()
  最上層 = uvicorn event loop 上的 background asyncio task
```

`process_file` 一次呼叫在 event loop 上做的同步工作：`compute_file_hashes` 全檔讀（raw/NFS）、
`PDFExtractor.extract` / OCR（CPU-bound 數十秒）、`convert_to_pdf` 寫 NFS、
`save_parsed_markdown` 寫 NFS、9 次 SQLite 寫。

**控制組（證明 `739 未包` 不是 grep 壞掉）**：同次 grep `run_in_threadpool|to_thread` 對全 `app/`
命中 **17 行**（`crawler_routes:69,257`、`category_routes:55,59,101`、`settings_routes:65,105`、
`routes:168`、`libgen_live`、`validator`）⇒ 專案**會**用它，`download_worker.py:739` 是漏網的例外。
負控制組 `zzz_*_zzz` 每批 rc=1 / 0 行。

### ⚠ 新發現：每一個 API 請求都對 NFS 下一次 `os.mkdir`

**這格不在原本任何一張 BR 裡，且影響範圍遠大於下載路徑。**

`StorageManager` **不是** module-level singleton，每次 `Depends` 都新建，而 `__init__:17`
無條件呼叫 `ensure_directories()`：

```
routes.py:17-18   get_storage()  → StorageManager()            每次 Depends 新建
db/engine.py:24   DatabaseEngine.__init__ 在 db_path is None 時 也新建 StorageManager()
  ⇒ Depends(get_dao) / Depends(get_search) → CatalogDAO()/SearchEngine()
                                            → DatabaseEngine() → StorageManager()
                                            → ensure_directories() → mkdir on NFS
```

`DatabaseEngine._bootstrapped_paths`（`engine.py:19`）與 `CatalogDAO._bootstrapped_paths`
（`dao.py:164`）**只擋 schema/migration，完全不擋 `ensure_directories`**。

**`exist_ok=True` 不會省掉 syscall** —— `/usr/lib/python3.12/pathlib.py:1313` 是
先無條件 `os.mkdir(2)` 再 `except OSError` 吞 `EEXIST`。

實測（`DATA_DIR` 指到 scratch，monkeypatch `Path.mkdir` 計數）：

```
DEP=get_storage    mkdir_total=5  mkdir_on_parsed=1
DEP=get_dao        mkdir_total=3  mkdir_on_parsed=1
DEP=get_search     mkdir_total=3  mkdir_on_parsed=1
第二輪（測有無快取）
DEP=get_storage#2  mkdir_total=3  mkdir_on_parsed=1   ← 沒有快取，照做
DEP=get_dao#2      mkdir_total=3  mkdir_on_parsed=1
DEP=get_search#2   mkdir_total=3  mkdir_on_parsed=1
NEGCTL 純 exists() 後 mkdir_total=0   ← 有鑑別力，不是恆回 1
```

推論到線上：

- `GET /api/search` → `Depends(get_search)` → **每請求 1 次 `os.mkdir("/data/parsed")` NFS syscall**
- `GET /api/works/{id}/content` → `get_storage` + `get_dao` → **每請求 2 次**
- 執行位置：sync 相依項由 `fastapi/dependencies/utils.py:676` 一律 `await run_in_threadpool(call, ...)`
  送進 threadpool，**與端點是否 async 無關** ⇒ 不在 event loop 上，**但吃 anyio threadpool 名額**
  （實測 `total_tokens=40`）。
- 唯一在 event loop 上跑的 `ensure_directories` 是 `main.py:22-23`（lifespan，啟動時一次）。

**⇒ 風險形狀是「threadpool 飽和」而非「event loop 卡死」。** NFS 一次 stall，40 個並發請求的
相依項解析全部堵在 `os.mkdir` 上，名額耗盡後所有 sync 路由（含 `/api/search`）一起排隊。
**表徵與 event-loop 卡死幾乎不可區分，但根因不同、修法也不同。**

### 本節推翻了什麼（dispatcher 自己記，不埋在腳註）

1. **dispatcher 把 `routes.py:122` 當成 IO —— 錯。** 那行只是字串拼接。同區塊的
   `routes.py:123 .exists()` / `routes.py:125 convert_to_pdf` 才是真 IO，且**只在 MOBI/AZW
   預覽模式**才走到，整個路由又是 sync def ⇒ threadpool。**這格對搜尋延遲沒有解釋力。**
2. **dispatcher 猜 `manager.py:22` 可能每請求跑 —— 對，而且比猜的更糟**（連完全不碰檔案的
   `/api/search` 都中招）。
3. **「parsed 寫入量遠小於 raw，風險等級不同」—— 部分推翻。** 位元組量小，但**次數不小**：
   `save_parsed_markdown` 在 `ingest.py:106` 與 `ingest.py:198` 對**每一本**入庫書都寫 `.md`
   （不只 PDF 轉檔）；讀側 `routes.py:79 get_parsed_content` 每次 `/works/{id}/content` 都對 NFS
   做 `resolve_path` + `exists` + 全檔讀。加上每請求 mkdir ——
   **`parsed` 的 NFS syscall 頻率其實高於 `raw`，並列不算誤導，反而低估了 parsed。**

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

### ⬆ 2026-08-21 追加：`dec5b44` 順帶搬走的第三樣，與它留下的孤兒檔

**本節推翻了本 BR（與 BR-210000 E 節）的一個共同前提。** 來源：handler
`ses_fdf38a329ffehvLOfKjb3cCBwm` 的 `[BRNS-ASK] round=1` 第①格；dispatcher 獨立重驗後採納。

#### 前提錯在哪

BR-210000 E 節把 `_save_jobs_to_disk()` 的 9 個呼叫點列進「同步阻塞 IO **落在 NFS**」這一族，
dispatcher 的派工單也照抄了這個分類。**兩者都錯了，而且不是「列多了」，是列錯了類。**

```
download_worker.py:104   self._jobs_file = self.pipeline.storage.db_dir / "download_jobs.json"
                                                                ^^^^^^
manager.py:16            self.db_dir = self.base_dir / "db"
CONTROL  grep -n '_zzz_jobs_file_zzz'  rc=1          ← grep 有鑑別力

df -T data/db      →  /dev/sde  ext4
CONTROL df -T /nas →  nfs4                            ← 兩者確實不同
```

**`download_jobs.json` 走的是 `db_dir`，而 `dec5b44` 搬的正是 `db_dir`。**
所以那次遷移**順帶把它一起搬離 NFS 了** —— 兩張 BR 都只追 `raw`/`parsed`，
沒人注意到第三樣東西也掛在同一個目錄底下。

⇒ 那 9 個呼叫點寫的是**本地 ext4 的小 JSON**（現役檔 2 bytes），與本族其餘各項
**不是同一個機制**，不該被同一個處方治。

#### 連帶：這 9 個呼叫點刻意不改（不是漏掉）

handler 進一步指出，其中 `src:407-409`（`_run_single_job` finally）與
`src:621-624`（`_process_queue` finally）位在 **CancelledError 傳播路徑上的 `finally`**。
換成 `await` 會讓該 await 在取消情境下自己被取消 ⇒ **落盤被跳過**。

而那個落盤保護的正是 `BR-20260820_230000` 修好的東西 —— `main.py:33` 的
`finally: await worker.stop()`，其註解自陳「下載中的 job 沒機會落盤標記狀態（證據 ③）」。

**拿 1-2ms 的本地 ext4 小寫去換「關機時任務狀態可能不落盤」是負收益。**
9 個呼叫點一律不動，這是判斷過的決定，不是遺漏。

#### 孤兒檔（本 BR 新增的殘留項）

```
現役  data/db/download_jobs.json            2 bytes   Aug 21 03:54  ← "[]"，活的
殘留  /nas/openshelf/db/download_jobs.json  1118 bytes Aug 20 14:21  ← dec5b44 前的孤兒

$ grep -rn 'openshelf/db\|/data/db/download_jobs\|nas.*download_jobs' app/ --include=*.py
GREP_RC=1                                    ← 零命中：無任何程式碼會再讀它
CONTROL $ grep -rn 'download_jobs.json' app/ --include=*.py
crawler_routes.py:148 / download_worker.py:104 / :254   rc=0   ← 3 行，證明 grep 讀得到
```

**這份 1118 bytes 的舊檔停留在 `dec5b44` 之前的狀態，且已無讀者。**
它本身無害（沒有人讀），但它是一個**看起來像現況的過期快照** —— 下一個 debug
`download_jobs` 的人若先找到 NFS 那份，會對著一份 8/20 14:21 的狀態推論。

處置：**未決**。刪除是安全的（無讀者已坐實），但那是 `dec5b44` 遷移的收尾債，
不屬本 BR 的修法選項 A-D 任何一項。列在此處以免失傳。

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
