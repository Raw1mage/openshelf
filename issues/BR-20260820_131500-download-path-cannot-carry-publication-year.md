# BR-20260820_131500 — 下載路徑無法把出版年份從搜尋帶到入庫

Status: OPEN
Owner: ses_fe7b5cbadffeSlxj0dv1Z740O4（值星官）
Family: metadata-field-loss
Filed: 2026-08-20 by ses_fe7b5cbadffeSlxj0dv1Z740O4
Reported-by: handler ses_fe27556c4ffeWZLm2DnDItEhNf（交件時主動標為範圍問題，未自行越界修）

**Related**:
- `BR-20260820_130500-publication-year-silently-dropped`（已 closed，commit `823905b`）—
  **同族同一條資料流的下游未閉合段**。該案修好了「解析端能取出年份」與「ingest 端能寫入年份」
  兩頭，本案是中間那條把值從搜尋結果送到 ingest 的管道，**五層都沒有這個欄位**。
  兩案合起來才等於「使用者按下載，年份會跟著進書庫」。
- `BR-20260820_111523-mirror-resolver-dead-mirrors`（已 closed，commit `16890d7`）—
  同族失效類別「欄位不存在與值為空共用同一個輸出」，但層級不同（該案在鏡像健康，本案在資料傳遞契約）。

## 一句話

`publication_year` 在下載鏈的**五個環節全部缺席**。解析端已能正確取出年份、ingest 端已能正確寫入，
但中間沒有任何一層帶得動這個值——所以使用者按下下載後，入庫的書仍然沒有年份。

## 實測的缺口鏈（2026-08-20，逐層 grep 確認）

```
app/static/js/app.js:1009-1014     body: { md5, title, authors, extension, mirror_links }   ← 無 year
app/api/crawler_routes.py:31       class DownloadRequestItem(BaseModel)                     ← 無 year
app/api/crawler_routes.py:90/109   worker.enqueue(md5, title, authors, extension, links)    ← 無 year
app/crawler/download_worker.py:18  class DownloadJob.__init__                               ← 無 year
app/crawler/download_worker.py:403 metadata_override = { title, authors_display }           ← 無 year
                                        ↓
app/pipeline/ingest.py             publication_year=  ← 已就緒（commit 823905b），但永遠收到 None
```

控制組：同樣的 grep 對 `title` 在 `DownloadJob` 定義內命中 **2** 處，證明 grep 讀得到那個區塊，
`publication_year` 的 rc=1 是真的沒有。

## 為何現況看起來「好像修好了」

搜尋畫面現在**會顯示年份**（`/api/crawler/search` 實測 21/21），因為那是解析端直接吐給前端的。
但那個值只活在畫面上——按下下載時前端不送它，於是它在 enqueue 的那一刻就消失了。

**症狀因此比修復前更隱蔽**：使用者在搜尋結果看到「1972年」，下載完打開自己的書庫卻沒有年份。
修復前是「到處都沒有」，現在是「線上有、我的書庫沒有」——後者更難聯想到是同一件事。

## 已知的實作順序約束

`app/static/js/app.js` 是本案第一層，但該檔此刻由另一顆 handler 持有（BR-20260820_124500 書單選單）。
**不要在該 handler 交件前動這個檔**——bind-mount + `--reload`，兩顆同時寫等於互相覆蓋。

四個後端層（`crawler_routes.py` / `download_worker.py`）與該 handler 無交集，但**不建議先做**：
只補後端而前端不送，等於讓一個永遠收不到值的欄位進版控，下一個讀者會以為它是活的。

## 驗收判準

1. 五層逐層帶上 `publication_year`（`Optional[int] = None`，缺值不得炸）。
2. **端到端實打**：搜尋一本有年份的書 → 按下載 → 落庫後查 `/api/search`，該筆 `publication_year` 非 None。
   給出下載前後兩個數字。
3. **負向**：搜尋一本上游本來就沒年份的書 → 下載 → 落庫後該欄位為 `None` 而非 0 或空字串。
   缺這格就無法證明它不是把某個預設值灌進去。
4. 既有的下載流程不得退化——不帶 `publication_year` 的舊 payload（例如磁碟上已存在的
   `jobs.json` 反序列化，`download_worker.py:99`）必須仍能載入。

## 已知的驗證陷阱

`data/db/openshelf.sqlite`（repo 內，root:root，`work` 表 0 rows）**不是線上那顆**。
線上是 NAS 掛載 `/nas/openshelf/db`。端到端一律打容器 API，不要直接開 repo 內的 sqlite。

## 存量資料不在本案範圍

既有 35 筆是修復前入庫的，DB 裡就是 NULL。本案只保證**新入庫**帶年份。
存量 backfill 需要動 `app/db/`，是另一個工作包，尚未開。
