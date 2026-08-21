# BR-20260821_060000 — `save_raw_bytes` / `save_parsed_markdown` 在 NAS 上留下的 `.tmp_<pid>` 孤兒沒有任何回收路徑

- **Status**: **OPEN** —— 已坐實機制，當下孤兒數為 0，未修。
- **Owner**: ses_fe7b5cbadffeSlxj0dv1Z740O4（值星官）
- **Family**: `db-storage-substrate`
- **Severity**: **低**（見「為何不是高」節）—— 機制坐實但當下無實例，且爆炸半徑已被
  BR-20260820_210000 E 節的 `_FILE_IO_LIMITER(4)` 侷限在下載/上傳功能內。
- **Filed**: 2026-08-21 by ses_fe7b5cbadffeSlxj0dv1Z740O4
- **Found-by**: handler `ses_fde28053affeLHPxbJPFA4czfn` 在 BR-20260821_040000 選項 C 的
  「沒量什麼」第 4 格主動標出「`save_raw_bytes` / `save_parsed_markdown` 是同一個病」，
  並明講「值得一張新 BR，但沒有替你開（`issues/` 是你的禁區）」。
  **dispatcher 獨立查證後，證實它說對一半、說錯一半，且另有一格它沒提到的。**

**Related**（每條都帶可引用的依據，非「感覺相關」）：

- `BR-20260821_040000-raw-and-parsed-dirs-still-on-nfs-after-db-migration` —
  **同一個 family，同一份 `raw_dir` / `parsed_dir`**。該 BR 機制② 處理的是
  「下載路徑的 `.part` 落在 NFS」，已由選項 C 搬到本地 `staging_dir`。
  本張處理的是**同一組目錄上的另一種暫存檔**（`.tmp_<pid>`），它走的是
  `IngestionPipeline` 路徑而非下載路徑，**選項 C 完全沒有碰到它**。
- `BR-20260820_210000-async-routes-sync-io-on-event-loop-family` —
  **同一條執行緒契約**。該 BR C 節（upload）已把 `ingest_bytes` 包進
  `run_in_threadpool`（`app/api/routes.py:168`）、E 節把 `process_file` 包進
  `_run_file_io`（`app/crawler/download_worker.py:1064`）。**正是這兩格讓本張
  的 Severity 停在「低」而不是「高」**——寫入雖仍在 NFS，但不在 event loop 上。
- `BR-20260820_223000`（validator.py 靜默 mkdir，已 closed）—
  **同一種失效類別：靜默的檔案系統副作用**。該案是「寫進未掛載路徑靜默成功」，
  本案是「暫存檔寫失敗後靜默殘留」。兩者共通點是**沒有任何輸出把異常態與正常態分開**。

---

## 一、機制（已坐實）

`app/storage/manager.py` 兩個函式都用「寫 `.tmp_<pid>` → `replace()` 成正式檔」的原子落檔法：

```
src:133  def save_raw_bytes(self, data, extension)
src:141      tmp_path = target_path.with_suffix(f".tmp_{os.getpid()}")
src:142      with open(tmp_path, "wb") as f:
src:143          f.write(data)
src:144      tmp_path.replace(target_path)

src:148  def save_parsed_markdown(self, work_id, markdown_content)
src:152      tmp_path = target_path.with_suffix(f".tmp_{os.getpid()}")
src:154      with open(tmp_path, "w", encoding="utf-8") as f:
src:155          f.write(markdown_content)
src:156      tmp_path.replace(target_path)
```

`target_path` 分別落在 `raw_dir` / `parsed_dir`，兩者**都在 NAS 上**（`docker inspect` Mounts：
`/nas/openshelf/raw -> /data/raw`、`/nas/openshelf/parsed -> /data/parsed`，`df -T` 皆 `nfs4`）。

**缺陷**：`open` 成功、`f.write` 失敗（NFS stall / 磁碟滿 / 程序被 SIGKILL）時，
`.tmp_<pid>` 留在 NAS 上，而**沒有任何路徑會回收它**：

```
sweep_orphan_parts()   只掃 staging_dir 的 "*.part"    ← 目錄不對、pattern 也不對
delete_job / adelete_job   只刪 .part                   ← 同上
clear_completed()      只 sweep .part                   ← 同上
```

控制組（證明 grep 讀得到）：`grep -rn "tmp_" app/ --include='*.py'` 命中 12 行 rc=0；
bogus pattern `zzz_no_such_tmp_marker` rc=1。

## 二、當下實測：孤兒數為 0

```
ls -1 /data/raw/    | grep -c "tmp_"   →  0
ls -1 /data/parsed/ | grep -c "tmp_"   →  0
ls -1 /data/parsed/ | wc -l            →  47    ★控制組：列舉有鑑別力
```

⇒ **這是一個尚未發生的缺陷**，不是正在發生的。與 BR-20260821_030000 的空 md5 同一形狀：
機制成立、觸發機率目前為 0，**而正因為現在是 0，任何一次非 0 都是強訊號不是雜訊**。

## 三、為何不是高 —— handler 說「同一個病」只對一半

handler 在交件時把這兩個函式標成「與 BR-20260821_040000 機制② 同一個病」。dispatcher
獨立查證後的結論是：**共病的是「寫在 NFS 上」，不共病的是「跑在 event loop 上」。**

兩個入口都**已經**被包進執行緒，這是 BR-20260820_210000 兩節處置的既有成果：

```
上傳路徑  app/api/routes.py:168        await run_in_threadpool(pipeline.ingest_bytes, ...)
                                        （C 節處置，docstring 明寫 BR-20260820_210000）
下載路徑  app/crawler/download_worker.py:1064
                                        await _run_file_io(self.pipeline.process_file, ...)
                                        （E 節處置，_FILE_IO_LIMITER = CapacityLimiter(4)，src:38）
```

控制組：`grep -rn "save_raw_bytes\|save_parsed_markdown" app/ --include='*.py'` 命中 7 行
rc=0；bogus pattern `save_zzz_no_such_fn` rc=1 ⇒ grep 有鑑別力，上面「只有這兩條路徑」
不是漏掉。

`ingest.py` 內部三個呼叫點（`src:54` / `src:106` / `src:198`）全部位於
`ingest_bytes` 與 `process_file` 之下，**沒有第三條繞過執行緒的入口**。

⇒ **NFS 抖動時不會全站停頓**，只會讓下載/上傳功能排隊（`_FILE_IO_LIMITER` 4 個 token）。
這正是 BR-20260820_210000 E 節處置買到的東西：**故障被侷限在功能內**。

**所以本張的殘餘危害是「NAS 上靜默累積孤兒檔」，不是「服務停擺」** —— 這是低嚴重度而非高。

### ⚠ 但有一格 handler 沒提到，而它才是本張真正的內容

BR-20260821_040000 選項 C 新增的 `sweep_orphan_parts()` **不會回收這些檔**，兩個理由都成立：

```
掃的目錄   staging_dir（本地 ext4）      而 .tmp_<pid> 在 raw_dir / parsed_dir（NAS）
掃的 pattern  "*.part"                   而這些檔叫 "<sha256>.tmp_<pid>"
```

`sweep_orphan_parts` 原始碼（容器內 `inspect.getsource` 取得）：
`entries = list(staging.glob("*.part"))`，`live = {self._get_part_path(j).name for j in ...}`。
兩個維度都對不上。

**即使把 sweep 指向 NAS 也不能直接套用**，因為 `.part` 的「活著」判準是「有 job 引用它」，
而 `.tmp_<pid>` 沒有等價的引用來源 —— 它的生命週期完全落在單一函式內，
唯一可靠的判準是**寫它的那個 pid 是否還活著**，而容器重建後 pid 會重用。

⇒ 修法不是「擴大 sweep 範圍」，這格需要獨立設計。

## 四、修法選項（未裁決）

| | 做什麼 | 代價 |
|---|---|---|
| **A** | `try/except` 包住寫入，失敗時 `unlink` 暫存檔 | 只涵蓋「例外被攔到」的情形；SIGKILL / 容器被殺仍留孤兒 |
| **B** | 兩個函式也改寫本地 `staging_dir` 再 `_move_across_filesystems` 搬 NAS | 與選項 C 一致的架構，但 `save_parsed_markdown` 的內容通常只有數十 KB，多一次搬移不划算 |
| **C** | `start()` 增加一次 NAS `.tmp_*` 掃除 | 每次啟動一次 NFS glob（成本可接受）；但**判準難定**——見上方「pid 會重用」 |
| **D** | 不修，維持已知風險 | 當下孤兒數為 0，且爆炸半徑已被侷限；靠 BR 留痕讓下一個人知道它存在 |

**dispatcher 傾向 D**，理由與 BR-20260821_030000 的空 md5 殘留同型：機制坐實但無實例、
無使用者可感知傷害、且修法的判準本身不乾淨（A 涵蓋不全、C 的 pid 判準會誤刪）。
**但這格的裁決權在使用者，不在 dispatcher。**

## 五、沒驗證什麼（範圍邊界）

1. **沒有製造真實的寫入失敗**來觀察孤兒產生。要製造得讓 NFS stall 或塞滿 NAS，
   兩者都會影響線上。所以「孤兒會留下」是**機制推論**（`open` 成功後 `f.write`
   失敗則 `replace` 不會執行），不是觀察到的。
2. **沒有查歷史上是否曾經產生過孤兒**。當下為 0，但無法排除「曾經有、被人手動清掉」。
3. **`.tmp_{pid}` 的 pid 重用風險未量化**。理論上兩個不同世代的容器可能撞同一個 pid
   而覆寫彼此的暫存檔，但那需要同時寫同一個 `target_path`（同一個 sha256），
   而那種情形下 `save_raw_bytes` 的 `if not target_path.exists()` 守衛已經跳過寫入。
   **未實測。**
4. **未檢查 `_save_jobs_to_disk` 的 `.tmp`**（`download_worker.py:342`）。它落在
   `db_dir`（本地 ext4）且無 pid 後綴，與本張不同機制 —— 但它同樣沒有回收路徑。
   **這格記在這裡，不另開張。**
