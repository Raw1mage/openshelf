# FR-20260820_234500 — 下載佇列中直接指定書籍歸屬的自訂書單

Status: PARTIAL — R1~R4 已實作並驗證（commit 921ac8e）；前端零瀏覽器實測，R5 未做
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

### 殘留（本 FR 標 PARTIAL 的理由）

1. **前端零瀏覽器實測** —— 只有靜態指紋 + `node --check`。「按鈕會渲染」已證，
   「點了有反應」未證。R1 是本 FR 的存在理由，而這格正是 R1 本身。
   （BR-20260820_124500 正是栽在同一格：靜態閱讀的推論被寫成證據。）
2. **R5 未做** —— 原文已標「nice-to-have，R1–R4 才是驗收主體」，
   `DownloadRequestItem.collection_ids` 參數鏈已備妥但前端不送。

### dispatcher 自己踩到的一格（記錄，非缺陷）

補 R4 證據時我手改容器內 `jobs.json` 再 `docker compose restart`，job 消失。
一度以為坐實了 handler 標為 UNDECIDABLE 的「靜默清空」形狀。**是我的量測方法錯誤**：
`main.py:33` 的 `finally: await worker.stop()` → `src:623 _save_jobs_to_disk()`
用**記憶體狀態**無條件覆寫檔案，容器活著時的手改必然被蓋掉。
改用 `docker compose kill`（SIGKILL 跳過 lifespan finally）後 job 正常載入。
容器 log 的載入/存檔失敗訊號皆 0，而控制組 `Application startup complete`=4 rc=0
證明儀器有鑑別力 —— **系統沒有靜默失效，是我的探針走錯路徑。**
handler 把這格標成 UNDECIDABLE 而不打扮成缺陷，是對的。
