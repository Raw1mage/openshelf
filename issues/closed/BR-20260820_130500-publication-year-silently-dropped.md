# BR-20260820_130500 — 出版年份在兩層各自被靜默丟棄

Status: CLOSED — fixed and committed in `823905b`
Closed: 2026-08-20 by ses_fe7b5cbadffeSlxj0dv1Z740O4
Owner: ses_fe7b5cbadffeSlxj0dv1Z740O4（值星官）
Family: metadata-field-loss
Filed: 2026-08-20 by ses_fe7b5cbadffeSlxj0dv1Z740O4
Reported-by: 使用者（「很多搜尋結果都不顯示書的年份。是資訊不足嗎？還是 bug」）

**Related**:
- `BR-20260820_111523-mirror-resolver-dead-mirrors`（已 closed，commit `16890d7`）—
  **同一種失效類別「缺席態與失敗態共用同一個輸出」**。該案是「鏡像已死」偽裝成「查無此書」；
  本案是「解析失敗 / 欄位沒傳」偽裝成「來源沒有這個資訊」。兩案都以 `None` / `[]` 作為
  唯一輸出，呼叫端無法區分。
- `BR-20260820_130000-search-query-punctuation-and-term-semantics` —
  同屬本輪使用者回報、同一批量測產出，**同族失效類別但不同層**（該案在查詢層，本案在解析與寫入層）。

## 一句話

`publication_year` 在**兩個獨立的層**各被丟棄一次：公網 parser 用 `str.isdigit()` 判斷年份，
遇到 libgen 現行的完整日期格式（`1972 June 01`）直接吞成 `None`；本地 ingest 建立 `WorkCreate`
時**根本沒傳這個欄位**，於是一律寫 NULL。兩者都不報錯。

## 實測（2026-08-20，容器內）

| 來源 | 總筆數 | 有年份 | 空 |
|---|---|---|---|
| libgen HTML 源頭 | 21 | 21 | 0 |
| `/api/crawler/search`（公網） | 21 | **2** | 19 |
| `/api/search`（本地） | 35 | **0** | 35 |

源頭 21/21 全部有年份 → **不是資訊不足**。
本地 35/35 全空（控制組：同一批 `title` 35 筆全非空，證明查詢方式有效）。

## 缺陷 A — 公網 parser：`isdigit()` 對完整日期回 False

`app/crawler/libgen_live.py:286` 與 `:363`（兩個適配器各一處）：

```python
year = int(year_str) if year_str.isdigit() else None
```

libgen 現在吐的是 `1972 June 01` / `1989 March 04` 這類完整日期。
`"1972 June 01".isdigit()` → `False` → 靜默成 `None`。
只有 `1987`、`2015` 這種光禿禿四位數活得下來——這解釋了 21 筆為何恰好只有 2 筆有值。

**這一格 `else None` 同時代表三件事**：來源真的沒年份 / 來源給了但格式不合 / 解析器沒跟上版型變更。
三態共用一個輸出，所以版型變更了沒有人會知道。

## 缺陷 B — 本地 ingest：欄位根本沒傳

`app/pipeline/ingest.py:72` 與 `:159`：

```python
work_create = WorkCreate(
    title=..., authors_display=..., availability_tier=0
)   # 沒有 publication_year
```

`WorkCreate.publication_year` 預設 `None`，**不傳不報錯**，直接寫 NULL。
驗證：`grep -n publication_year app/pipeline/ingest.py` → rc=1 無命中；
控制組同檔 `authors_display` 命中 3 處，證明 grep 讀得到該檔。

## 為何兩處必須一起修

只修 A：公網搜尋看得到年份，但**使用者一按下載入庫，年份又歸零**——
症狀從「都沒有年份」變成「線上有、我的書庫沒有」，更難查。
只修 B：ingest 忠實傳遞了一個上游已經吞掉的 `None`，畫面毫無變化。

## API 與前端清白

欄位名 `publication_year` 從 model 到 API 到前端一致，前端只是忠實反映上游的 `None`
（空值時整段 DOM 不渲染，所以看起來是「沒有年份」而非「年份欄位空白」）。
**不要往那兩層找。**

## 驗收判準

1. `_parse_libgen_is_html` / `_parse_libgen_li_html` 對 `1972 June 01`、`1989 March 04`、
   `1987`、`""`、`n/a` 五種輸入**逐一斷言**——含負向：真正無年份時仍須是 `None`，
   不得為了讓測試變綠而預設塞值。
2. 解析失敗（有字串但取不出年份）必須與「來源真的沒有」**在 log 層可區分**，
   至少一句 `log.debug` 帶原始字串。缺這格就是用同一個病去修這個病。
3. ingest 兩處 `WorkCreate` 傳入 `publication_year`；**測試需含控制組**——
   上游給值時寫得進去、上游是 `None` 時仍寫 `None` 不炸。
4. 端到端：對 `Operating System Concept` 實打 `/api/crawler/search`，
   `has_year` 需顯著高於現況的 2/21。給出修前修後兩個數字。

## 已知的驗證陷阱（會咬人）

`data/db/openshelf.sqlite`（repo 內，root:root，114 KB，`work` 表 **0 rows**）
**不是線上那顆**。線上讀的是 NAS 掛載 `/nas/openshelf/db`（容器內 80 MB、35 筆）。
拿 repo 內那顆做驗證會得到「查無資料」而誤判為修好了或修壞了。
