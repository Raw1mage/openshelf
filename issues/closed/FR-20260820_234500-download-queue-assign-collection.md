# FR-20260820_234500 — 下載佇列中直接指定書籍歸屬的自訂書單

Status: **CLOSED** — 驗收主體 R1~R4 + AC1~AC6 全數實作並經 dispatcher 獨立驗收（`921ac8e`）。
  兩格原殘留已於 2026-08-21 全數銷帳（見文末「殘留銷帳」節）：
  Playwright 18 條已收進版控（`0a7b1ed`，`tests/e2e/` 4 檔 tracked）；
  R5 為原文自標的 nice-to-have，其參數鏈後端環已備妥，僅前端不送——**明示不做，非漏做**。
Closed: 2026-08-21 by ses_fe7b5cbadffeSlxj0dv1Z740O4
Verified: 2026-08-21 by ses_fe7b5cbadffeSlxj0dv1Z740O4
Type: Feature Request
Owner: ses_fe7b5cbadffeSlxj0dv1Z740O4
Family: collections-binding
Filed: 2026-08-20 by ses_fe05f3458ffeOGWEl7RZU15Vx1（值星官）
Requested-by: 使用者

**Related**:
- `BR-20260820_124500-quick-collections-modal-blocking`（top-level, PARTIAL）— **同一條執行路徑**：
  本 FR 要在下載佇列新增的「選書單」UI，會呼叫與該 BR 相同的 `GET /api/collections` +
  `callExtension()` 前端路徑。該 BR 的後端延遲殘留未結，**本 FR 實作時若沿用同一個
  collections 載入函式，會直接繼承該延遲**。實作者必須先讀該 BR 的「勘誤與殘留」一節。
- `BR-20260820_131500-download-path-cannot-carry-publication-year`（closed）— **同一種缺陷類別**：
  下載請求的欄位無法把上游已知的中繼資料一路帶到落地端。該案是 `publication_year`，
  本 FR 是 `collection_id`；`DownloadRequestItem` → `DownloadWorker.enqueue` → `DownloadJob`
  這條參數鏈要再穿一次，**該 BR 的修法就是本 FR 的施工圖**。

---

## 一句話

使用者希望在**下載佇列**（而非等下載完成後回書櫃再操作）當下就決定這本書要進哪一個
自訂書單，包含 ★ 我的最愛。需要在佇列項目上多一個「指定書單」按鈕。

## 現況（實測自 source，非推論）

**書單機制已完整存在**，缺的只是「與下載流程的接線」：

| 能力 | 位置 | 狀態 |
|---|---|---|
| 列出書單 | `app/api/collection_routes.py:17` `GET /api/collections` | ✅ 已有 |
| 加書進書單 | `app/api/collection_routes.py:59` `POST /api/collections/{cid}/items` | ✅ 已有，吃 `work_id` |
| 查某書屬於哪些書單 | `app/api/collection_routes.py:80` `GET /api/collections/work/{wid}/status` | ✅ 已有 |
| 預設「我的最愛」書單 | `col_favorites`（見 BR-20260820_124500 實測輸出） | ✅ 已有 |
| **下載請求攜帶 collection** | `app/api/crawler_routes.py:33` `DownloadRequestItem` | ❌ **無此欄位** |
| **佇列任務記住 collection** | `app/crawler/download_worker.py:19` `DownloadJob.__init__` | ❌ **無此屬性** |
| **落地後自動歸戶** | `DownloadWorker` 完成流程（`job.work_id` 產生處） | ❌ **無此步驟** |

關鍵時序事實：`DownloadJob.work_id` 在 **enqueue 當下是 `None`**，只有下載完成、
`IngestionPipeline` 入庫後才有值（`download_worker.py:45` `self.work_id: Optional[str] = None`）。
而 `POST /api/collections/{cid}/items` 吃的是 `work_id`。
**故「使用者在佇列指定」與「實際能寫進書單」之間隔著一段時間差**，這是本 FR 的核心設計約束，
不是可以忽略的細節。

## 需求

### R1 — 佇列項目可指定書單（**本 FR 的存在理由，不可降級**）
下載佇列 modal（`app/static/js/app.js:1208` 起的 `queue-item` 區塊）每一列新增一個
按鈕，點擊後可選擇一個或多個既有書單。UI 上「★ 我的最愛」（`col_favorites`）
應為一鍵可及，不要求使用者展開選單再找。

**使用者原話（2026-08-20，決定性理由）**：
> 「在下載當下還沒辦法建立，但在下載界面如果不能提供當場加入書單的功能，之後要加會很麻煩，
> 我得重新再下一次搜尋要求，再從搜尋結果中重新找到該項目然後加入。
> 所以佇列選單中必須同時給我加入書單的機會，才是合理的做法。」

**這句話定義了本 FR 的驗收下限**：`work_id` 尚不存在 ≠ 使用者的意圖不能被接受。
把「等下載完再去書櫃加」當成替代方案是**不可接受的**——那條路徑要求使用者重跑一次搜尋、
在結果中重新定位同一本書。**「技術上晚點才寫得進去」不得被翻譯成「UI 上晚點才問使用者」**，
兩者是不同的層。實作必須在**意圖被表達的當下**（佇列）承接它，時間差由系統吸收，不由使用者吸收。

### R2 — 指定意圖必須被持久化
指定動作發生在 `work_id` 尚不存在時，故意圖必須存在 job 上並隨
`download_jobs.json` 一起落地（`download_worker.py:_save_jobs_to_disk`），
**程序重啟後不得遺失**。

### R3 — 落地後自動歸戶
job 轉為 `completed` 且 `work_id` 產生後，worker 自動把該 work 加入被指定的書單。

### R4 — 已完成的 job 仍可指定
佇列中已 `completed` 的項目（`work_id` 已存在）點同一個按鈕時，應直接寫入書單並即時生效。
**同一個按鈕在兩種時機下行為不同，但對使用者必須看起來是同一件事。**

### R5 — 下載階段就可指定（可選，次要）
搜尋結果頁按「下載」時即可預先指定書單，經
`DownloadRequestItem` / `BatchDownloadRequest` 帶進佇列。
**此項為 nice-to-have，R1–R4 才是本 FR 的驗收主體。**

## 驗收條件（Acceptance Criteria）

1. 下載佇列中任一 `queued` / `downloading` 狀態的項目，可指定書單並在 UI 上看得出已指定哪些。
2. 指定後 `data/db/download_jobs.json` 內該 job 的紀錄含該 collection 資訊；**重啟服務後指定仍在**。
3. 該 job 下載完成後，`GET /api/collections/{cid}` 的 items 含該 work；
   `GET /api/collections/work/{work_id}/status` 回傳含該 cid。
4. 對已 `completed` 的項目做同一操作，效果即時（不需重跑下載）。
5. 指定一個不存在的 collection_id 時 **fail loud**（明確錯誤），不得靜默忽略。
6. 下載失敗的 job 不得產生任何書單寫入。

## 實作提示（給接手者，非強制路線）

- **參數鏈照抄 `publication_year` 那條**（`BR-20260820_131500`，closed）：
  `DownloadRequestItem` → `enqueue(...)` → `DownloadJob.__init__` → `to_dict()` →
  `_load_jobs_from_disk()` 五處都要動，漏掉 `to_dict`/`_load` 任一處就是「存了但重啟後消失」。
- **建議欄位形狀 `collection_ids: List[str]`**，不是單一 str——R1 明說可多選，
  且單值之後要擴成多值會是一次 schema break。
- **新增一個端點讓已入列的 job 事後被指定**（例如
  `POST /api/crawler/jobs/{job_id}/collections`），因為 R1 的指定動作發生在 enqueue 之後。
- **⚠ 缺席態不得與失敗態共用輸出**：歸戶失敗（書單被刪、DB 寫入失敗）必須留下明確訊號，
  不可讓「沒指定書單」與「指定了但寫入失敗」在 log 與 UI 上長得一樣。
  這是本 repo 已重複踩過三次的失效類別（見 Related 兩案 + `BR-20260820_111523`）。
- **前端不要沿用會付 3 秒 extension timeout 的 collections 載入路徑**，見
  `BR-20260820_124500` 的殘留節。

## 範圍外（Out of scope）

- 書單本身的 CRUD（已存在）。
- 書單排序、巢狀書單、書單分享。
- 依規則自動歸戶（例如按作者自動分類）。

---

## 觀察中（未重現）— jobs.json 讀到 0 筆與 API 回 1 筆的瞬時不一致

**狀態：UNDECIDABLE。未重現，不主張是缺陷，也不開 BR。**
記在這裡是因為**如果它成立，就是本 repo 第四次同類失效**（缺席態與失敗態共用輸出），
而下一個碰到的人不該從零重推。

### 我看到什麼

實作 R2 的落盤驗證時，同一分鐘內對同一份佇列做兩次觀測，結果相反：

| 觀測 | 對象 | 結果 |
|---|---|---|
| 第一次 | 直接讀 `data/db/download_jobs.json` | **0 筆** |
| 同時 | `GET /api/crawler/jobs` | **1 筆**（且 `collection_ids` 正確） |
| 第二次 | 再讀同一個 `data/db/download_jobs.json` | **1 筆**，`collection_ids: ["col_favorites"]` |

### 我排除了什麼

- **不是路徑看錯**：容器掛載點是 `/data/db` 而非 `/app/data/db`，
  但 `./data/db/download_jobs.json` 與容器內是同一個檔（已驗證 inode 級一致）。
  第二次讀到 1 筆用的是**同一個路徑**——所以「讀錯檔」解釋不了兩次結果不同。
- **不是欄位沒接通**：`collection_ids` 在第二次觀測中完整存在，
  參數鏈五處（`DownloadRequestItem` → `enqueue` → `__init__` → `to_dict` →
  `_load_jobs_from_disk`）已有往返測試鎖住
  （`tests/test_download_queue_collection_binding.py::test_collection_ids_survive_save_and_reload`）。
- **不是我的新端點造成**：現象出現在 `POST /jobs/{id}/collections` 之前的入列階段。

### 我沒能排除什麼（也就是它為何仍是 UNDECIDABLE）

沒有重現。單次觀測分不出下列三種因，而三者**在檔案內容上長得一樣**：

1. **良性競態**：讀檔的瞬間 `_save_jobs_to_disk()` 正在做
   `tmp_file.replace(self._jobs_file)`（`download_worker.py:118`）。
   `replace` 是原子的，但「舊內容」與「新內容」之間我可能取到前者。
   → 若是這一種，**不是缺陷**，只是我觀測時機不巧。
2. **靜默 early-return 後被覆寫**：`_load_jobs_from_disk()` 在檔案不存在時
   直接 `return`（`download_worker.py:129-130`），**不留任何訊號**。
   若某次啟動時該檔尚未建立（或路徑解析到別處），worker 會帶著空的
   `self.jobs` 起來；接著任何一次 `_save_jobs_to_disk()` 都會把磁碟上
   **既有的**佇列覆寫成 `[]`——因為它寫的是 `self.jobs.values()` 的全量快照。
   → 若是這一種，**這就是第四次同類失效**：
   「檔案本來就沒有任務」與「檔案有任務但這次沒載進來」共用同一個輸出。
3. **載入失敗被吞**：`_load_jobs_from_disk()` 的 `except` 有 `log.warning`
   （`:171-174`），比 (2) 好——但那行 log 我當時沒有去撈，所以無法據以排除。

### 重現條件的猜測（給下一個人）

若要證實或推翻 (2)，成本最低的路徑：

```
1. 讓 worker 啟動時 _jobs_file 指向一個不存在的路徑（或啟動前把該檔改名）
2. 觀察啟動後是否**完全無聲**（現況預期：無聲，這正是可疑之處）
3. 觸發任一次 _save_jobs_to_disk()（例如入列一筆）
4. 檢查原檔是否被覆寫成只剩新那一筆
```

**若第 2 步確實無聲**，則不論這次的不一致是否由它造成，
`_load_jobs_from_disk()` 的 early-return 本身就值得補一個 `log.info`／`log.warning`：
它是目前這條鏈上**唯一**一個「什麼都沒做」與「什麼都沒發生」無法區分的點
（存檔失敗有 `:122` 的 warning，載入失敗有 `:171` 的 warning，
只有「檔案不存在」這一格是靜默的）。

### 為什麼不現在就修

- 沒有重現 ⇒ 沒有證據說它導致了我看到的現象。
- 加 log 不在本 FR 的授權範圍（本 FR 動 `download_worker.py` 的授權是為了
  `collection_ids` 參數鏈，不是為了改啟動期的可觀測性）。
- 硬要現在修，就變成「憑一次無法重現的觀測去動一條與本 FR 無關的路徑」——
  那是本 repo 明令要避免的「基於局部症狀的修補」。

**記錄者**：本 FR 的實作 handler，2026-08-21。

### dispatcher 補：上面第 (2) 點已實測定案，**不必再重做那個實驗**

dispatcher 在補 R4 證據時**意外執行了幾乎相同的實驗**，結果如下（`ses_fe7b5cbadffeSlxj0dv1Z740O4`，2026-08-21）：

```
1. 容器內手改 /data/db/download_jobs.json 塞入一筆 completed job   → 確認寫入成功（1 筆）
2. docker compose restart openshelf                               → 起來後 API 回 0 筆
3. 讀回 jobs.json                                                  → []  ← 檔案真的被覆寫成空
```

**看起來完全坐實了 (2)。但它不是 (2)，是第四種因——原記錄漏列的那個。**

真因是 `app/main.py:33` 的 `finally: await worker.stop()`
→ `download_worker.py src:623 _save_jobs_to_disk()`：
**優雅關閉時 worker 會用「記憶體裡的 self.jobs」無條件覆寫磁碟全量快照。**
容器活著的時候對 `jobs.json` 做的任何手改，都必然在關閉那一刻被記憶體狀態蓋掉——
與 `_load_jobs_from_disk` 的 early-return **完全無關**。

改走 `docker compose kill`（SIGKILL，跳過 lifespan 的 `finally`）後，同一筆 job 正常載入，
R4 的 HTTP 實測才跑得起來。

**同時重驗了原記錄第 (3) 點所說「那行 log 我當時沒有去撈」**：

```
grep -c "下載任務佇列載入失敗" <容器 log>  = 0   rc=1
grep -c "下載任務狀態存檔失敗" <容器 log>  = 0   rc=1
CONTROL "Application startup complete"     = 4   rc=0   ← 儀器有鑑別力
CONTROL 不存在的 pattern                   = 0   rc=1   ← 負控制組
```

**兩個 warning 都沒發出，而控制組證明 grep 讀得到 log。** 所以載入失敗那條路徑當時沒被走到。

**結論**：那次瞬時不一致最可能是第 (1) 種（良性競態，`tmp_file.replace` 的前後內容）
或本節新增的第 (4) 種（觀測方式本身觸發了記憶體覆寫）。**(2) 沒有被本次實驗坐實。**

**但 handler 原記錄的核心主張仍然成立且未被推翻**：
`_load_jobs_from_disk()` 的「檔案不存在 → 靜默 return」，
確實是這條鏈上**唯一**一個沒有訊號的分支（存檔失敗有 `:122`、載入失敗有 `:171`）。
它是否曾經造成資料遺失仍是 UNDECIDABLE，
但「這一格缺一個 log」本身**不需要重現就成立**——那是可觀測性缺口，不是缺陷指控。
是否補這行 log 由使用者裁決，本 FR 不動。

**方法論教訓（值得比這個 bug 本身更被記住）**：
我一度把自己的量測副作用誤讀成系統缺陷，而它看起來**完全像**是 handler 描述的形狀。
若 handler 當初把那格寫成 BR 而非 UNDECIDABLE，我會照著它去修一個不存在的東西。
**把無法定案的觀測標成無法定案，而不是打扮成缺陷或打扮成安全，在這裡直接避免了一次錯誤的修復。**


---

## 驗收紀錄（dispatcher ses_fe7b5cbadffeSlxj0dv1Z740O4，2026-08-21）

**commit `921ac8e`（已 push）。R1~R4 全數實作並獨立驗證。**

### 已驗證（我自己重跑，非採信 handler 自報）

| 項 | 證據 |
|---|---|
| 全套件 | 237 passed rc=0（229 baseline + handler 補測 8）；三檔 sha 皆對回原值 |
| mutation A | 拆掉 AC5 fail-loud（`raise ValueError` 2→2 刻意不變，只讓它到不了）→ 死 `test_assign_unknown_collection_raises` + `test_assign_partially_unknown_is_all_or_nothing`，227 passed |
| mutation B | 拆掉 `to_dict` 的 `collection_ids`（24→22，`collection_sync_error` 12→12 刻意不變）→ 死 4 條含 `test_collection_ids_survive_save_and_reload`，225 passed |
| R4 `applied=true` | **HTTP 全鏈實測**：200 + `applied=true`，DB `collection_item` 該成員 0→1、總數 5→6，控制組 bogus work_id=0。探針已清（DB 回 baseline 5、jobs=0） |
| AC5 / 404 | 不存在 cid → 422 指名；不存在 job → 404。兩種失敗不共用輸出 |
| 前端指紋 | 新四函式 `ext_hits` 全 0；控制組 `openQuickCollection ext_hits=2` 證明偵測有鑑別力；`node --check` rc=0 |
| AC6 控制流 | `_apply_collections` 在 `src:750`，`work_id` 指派在 `src:740`；上游 `src:722/725` 任一失敗即 raise |

### 殘留（**本節已全數銷帳，見文末「殘留銷帳」；保留原文為紀錄**）

1. **~~前端零瀏覽器實測~~ → 已完成；真正的殘留是「證據不在版控」**

   ⚠ **上一版本本項寫「前端零瀏覽器實測」，那是 stale 的敘述，已更正。**
   保留刪節線是為了讓讀過舊版的人知道發生什麼事——照舊文字做會把已完成的 18 條重做一次。
   時序：handler 在 dismiss 抵達前已跑完，dispatcher 驗收當下樹上看不到，
   因為腳本落在 `$XDG_RUNTIME_DIR/openshelf-fe/`（scratch，0700，依規矩不進 repo）。
   由 handler 與驅動者各自獨立指出本項過期。

   **dispatcher 獨立重跑結果（我亲自執行那兩支腳本，非採信自報）**：

   ```
   phase1  total=7   passed=7   failed=0   PHASE1_RC=0
     [PASS] 每列有 ★ 一鍵最愛按鈕 :: count=1 text=['☆']
     [PASS] 每列有 📚 多選按鈕     :: count=1 text=['📚']
     [PASS] 控制組：不存在的按鈕確實找不到 :: count=0
     [PASS] 點 ★ 後 col_favorites 真的寫進 job（打 API 覆核，不看 UI）
            :: collection_ids=['col_favorites']

   phase2  total=11  passed=11  failed=0   PHASE2_RC=0
     [PASS] modal 列出項目數 == 線上書單數 :: dom=4 api=4
     [PASS] 每個 checkbox 的 id 都是真實 collection_id（不是書籤資料夾 id）
            :: ['col_favorites','col_9222f3d4d8024d99','col_a9cfca019b8c4c24','col_1e44822709e04f13']
     [PASS] 控制組：不存在的書單名確實不在 modal 裡
     [PASS] 三態互斥可辨 :: distinct=3
            badge(none)='' / badge(pending)='📚 2 待歸戶' / badge(failed)='⚠️ 歸戶失敗'
     [PASS] 失敗 badge tooltip 帶出是哪個 cid
            :: 1/1 個書單寫入失敗：col_gone(IntegrityError: FOREIGN KEY constraint failed)
   ```

   所以「點了有反應」已證，且 **Q2（`openQuickCollection` 會吃 Chrome 書籤樹）實地坐實**：
   DOM 四個 id 全部 `col_` 開頭且與 `/api/collections` 完全一致。
   我重跑造的探針已清（`jobs=0`、`collection_item` 回 baseline 5）。

   **殘留的是版控，不是驗證**：`grep -rl playwright tests/` rc=1 命中 0
   （控制組：`tests/` 內 25 檔含 `import pytest`，證明 grep 讀得到目錄）。
   證據隨 scratch 蒸發，未來無法回歸。收不收進 `tests/` 是產品決策
   （會給 CI 加上 chromium 依賴與線上服務前置條件），已發 QA 待使用者裁決。

   ⚠ **另記一格（差一點變成假綠燈）**：`$XDG_RUNTIME_DIR/openshelf-fe/full.log` 的內容是
   `237 passed`（pytest 輸出），**不是 Playwright 的輸出**。若只讀那個檔就採信
   「18 條全綠」，就是拿一個不相干的綠燈當證據。腳本本身確實含 playwright
   （phase1 命中 5、phase2 命中 4，負控制組 0 有鑑別力），但結論靠的是我親自重跑。
2. **R5 未做** —— 原文已標「nice-to-have，R1–R4 才是驗收主體」，
   `DownloadRequestItem.collection_ids` 參數鏈已備妥但前端不送。
   （**2026-08-21 銷帳：明示不做，非漏做。** 見文末。）

### dispatcher 自己踩到的一格（記錄，非缺陷）

補 R4 證據時我手改容器內 `jobs.json` 再 `docker compose restart`，job 消失。
一度以為坐實了 handler 標為 UNDECIDABLE 的「靜默清空」形狀。**是我的量測方法錯誤**：
`main.py:33` 的 `finally: await worker.stop()` → `src:623 _save_jobs_to_disk()`
用**記憶體狀態**無條件覆寫檔案，容器活著時的手改必然被蓋掉。
改用 `docker compose kill`（SIGKILL 跳過 lifespan finally）後 job 正常載入。
容器 log 的載入/存檔失敗訊號皆 0，而控制組 `Application startup complete`=4 rc=0
證明儀器有鑑別力 —— **系統沒有靜默失效，是我的探針走錯路徑。**
handler 把這格標成 UNDECIDABLE 而不打扮成缺陷，是對的。

## 殘留銷帳（dispatcher ses_fe7b5cbadffeSlxj0dv1Z740O4，2026-08-21）

上方「殘留」節列的兩格**全部 stale**，已逐格實測銷帳。保留原文不刪，是為了讓讀過舊版的人
知道發生什麼事——照舊文字做會把已完成的工作重做一次（本 FR 已因同一形狀被更正過一次，
見殘留第 1 項自身的刪節線）。

### 銷帳 1 — 「證據不在版控」→ 已進版控

原文寫：「證據隨 scratch 蒸發，未來無法回歸」。**該敘述在 `0a7b1ed` 之後不再成立。**

```
git ls-files tests/e2e/                    →  4 檔（E2E_TRACKED_FILES=4）
  tests/e2e/conftest.py
  tests/e2e/test_dispatched_issues_notice.py
  tests/e2e/test_queue_collection_ui_phase1.py
  tests/e2e/test_queue_collection_ui_phase2.py
CONTROL  git ls-files tests/zzz_not_exist/ →  0     ← 證明該指令對不存在的路徑真的回 0
```

收錄形態（handler `ses_fdff1782c`，dispatcher 驗收 `0a7b1ed`）：**零全域配置**——
無 `pytest.ini` / 無根 `conftest.py` / 無 marker 註冊，靠目錄內 `tests/e2e/conftest.py`
的 `pytest_collection_modifyitems` 在**收集階段**標 skip。預設 `pytest tests/` 顯示
`19 skipped`（存在證明），`OPENSHELF_E2E=1` 才真的跑。

**三態設計（handler 推翻 dispatcher 原驗收條件的產物，已採納）**：

| 狀態 | 結果 | 理由 |
|---|---|---|
| 未設 `OPENSHELF_E2E=1` | **skip** | 「你沒要求」是誠實的 skip，不主張任何關於系統的事實 |
| 設了但環境壞（缺 playwright / chromium / 8088 不可達） | **FAIL loud** | 「我試了但連不上」主張了卻沒證據，不能給綠燈 |
| 設了且環境齊全 | 真的跑 | — |

dispatcher 原本要求「服務不可達就 skip」，那會讓**「服務掛了」與「測試通過了」共用一個綠燈**
——判準①長在驗收條件本身。已改。

### 銷帳 2 — 「R5 未做」→ 明示不做，非漏做

R5 在需求原文即自標 **「（可選，次要）… nice-to-have，R1–R4 才是本 FR 的驗收主體」**。
它不在 AC1~AC6 任何一條裡。而其後端環**已經備妥**：

```
app/api/crawler_routes.py
  class DownloadRequestItem:
      collection_ids: Optional[List[str]] = None      ← 已存在
      # 原始註解：「目前前端不送這個欄位（使用者的指定動作發生在佇列，
      #   走 /jobs/{id}/collections），但參數鏈上不能只有這一環缺它
      #   ——否則將來補要再動一次同樣的檔。」
  class BatchDownloadRequest:
      items: List[DownloadRequestItem]                ← 批次層未帶（單筆層已帶）
CONTROL  該檔 collection_ids 總命中 = 9；bogus pattern = 0   ← 證明計數有鑑別力
```

**所以缺的只有前端「搜尋結果頁下載時預先指定」的 UI。** 使用者原話定義的驗收下限是
「佇列中必須給我加入書單的機會」——那條**已經滿足**（R1 已實作並經真瀏覽器實測）。
R5 是在**更早的時機**多給一次機會，屬體驗優化，不是本 FR 的存在理由。

**判定：不做。** 若日後要做，範圍是 `app/static/js/app.js` 的下載按鈕 +
`BatchDownloadRequest` 加一個 `collection_ids`，另開 FR，不要重開本張。

### 為何本 FR 現在是 CLOSED 而非 PARTIAL

PARTIAL 的定義是「主修復已 landed 但有**明確的、被承認的**殘留」。本 FR 兩格殘留：
一格已實際完成（版控），一格是原文即明示的範圍外（R5）。**兩者都不是欠債。**

留在頂層只會讓下一個接手者以為還有工作要做——而那正是本張 FR 已經發生過一次的傷害
（dispatcher 依 stale 資訊向使用者提了一個「派回原 handler 補前端實測」的選項，
而那件事早已完成）。

