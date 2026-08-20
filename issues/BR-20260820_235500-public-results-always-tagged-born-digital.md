# BR-20260820_235500 — 公網書一律標「原生 PDF」，與本地的真實判定共用同一個標籤系統

Status: OPEN
Owner: ses_fe7b5cbadffeSlxj0dv1Z740O4
Family: openshelf/signal-fidelity
Severity: 使用者可感知（看到的格式標籤不可信，且無法分辨哪一個可信）
Decision: 使用者裁示「建檔不修，列待辦」（2026-08-20）

## **Related**

- `BR-20260820_223000-dispatch-br-writes-to-ephemeral-container-dir.md` — **同一種失效類別**：
  兩個不同狀態共用同一個輸出，且無錯誤訊號。223000 是「真的沒有 BR」與「BR 寫到別處」
  共用 `total=0`；本案是「真的是原生 PDF」與「沒判定過」共用同一個紅標。
  **類別名：缺席態與失敗態共用同一個輸出。**
- `BR-20260820_210000-async-routes-sync-io-on-event-loop-family.md` — **同一條執行路徑**：
  兩者都在 `app/crawler/libgen_live.py` 的公網搜尋結果解析路徑上。210000 的 A 節修的是
  該路徑的**阻塞**（`_parse_libgen_*_html` 移進 `to_thread`），本案是該路徑的**產出內容**。
  修 210000 不會修好本案，但**動同一個解析函式時會看到彼此**。

## 症狀

搜尋結果卡片上的格式標籤（紅/橙/綠小方塊）對公網書籍**恆為紅色「原生 PDF」**，
除非副檔名剛好是 epub。實際是掃描版的書也顯示紅標。

使用者無法從標籤分辨：這本是真的 born-digital（可搜尋、可選取文字），
還是只是「系統沒判定過」。

## 證據（dispatcher 實測）

```
app/crawler/libgen_live.py src:364
    format_type = "epub" if extension == "epub" else "pdf_born_digital"
                                                     ^^^^^^^^^^^^^^^^^^
    ← 非 epub 一律 born_digital，無任何判定

grep -n "pdf_scanned" app/crawler/libgen_live.py     → 無命中，rc=1
    ⇒ 公網解析路徑從不產生 pdf_scanned

CONTROL  grep -rn "pdf_scanned" app/ --include=*.py  → 5 處命中，rc=0
    app/pipeline/pdf_extractor.py:43   "pdf_scanned" if is_scanned else "pdf_born_digital"
    app/pipeline/ingest.py:71          format_type = "pdf_scanned"
    app/pipeline/ingest.py:161         format_type = "pdf_scanned"
    app/api/routes.py:39               格式篩選參數含 pdf_scanned
    app/models/catalog.py:59           型別註解含 pdf_scanned
    ⇒ 證明 grep 讀得到、且 pdf_scanned 這個值在系統中真實存在並被使用
```

**關鍵不是「公網沒判定」，是「兩條路徑共用同一個標籤系統而語意不同」**：

| 路徑 | 紅標 `pdf_born_digital` 的意思 |
|---|---|
| 本地入庫（`pdf_extractor.py:43`） | **已判定**：`is_scanned` 為假，真的是原生 PDF |
| 公網搜尋（`libgen_live.py:364`） | **未判定**：只知道副檔名不是 epub |

同一個紅色方塊，在兩條路徑上意義完全不同，而**使用者看不出自己在看哪一種**。

前端渲染處（不區分來源）：
```
app/static/js/app.js src:1260-1265   getFormatTag(format)
app/static/css/style.css src:411-413 tag-pdf-born 紅 / tag-pdf-scan 橙 / tag-epub 綠
```

## 為什麼一直沒被發現

- **沒有錯誤、沒有 log、沒有例外**。標籤永遠渲染得出來，只是可能是錯的。
- 本地書的標籤是對的，所以「標籤系統看起來能用」。
- 要發現它必須**下載一本被標成紅色的公網書、打開來看是不是掃描版**——
  沒有人會為了驗證標籤而做這件事。

## 影響

- 使用者依標籤挑版本時被誤導（想要可搜尋的 PDF，挑到掃描版）。
- **任何「建議最優版本」的功能都不能用這個訊號**——它在公網結果上恆為同一值。
  （2026-08-20 評估搜尋結果聚合功能時，這是判定「最優」無法實作的原因之一；
  該功能已由使用者決定放棄，但本缺陷獨立存在。）

## 修復方向（未定，需決策）

**未拍板前不動手。** 三條路，取捨不同：

- **A. 拿掉公網書的格式標籤** —— 只在 `format` 來自真實判定時才渲染。
  最誠實：不知道就不說。代價是公網卡片少一個視覺元素，且 epub 那格其實**是**可信的
  （副檔名為 epub 是事實不是推論），一起拿掉會損失真訊息。
- **B. 加第三種視覺狀態「未判定」** —— 例如灰色方塊或 `?`。
  保留資訊量且不說謊。代價是要動 `getFormatTag` 與 CSS，且要決定 epub 是否也算未判定
  （它應該不算）。
- **C. 真的去判定** —— 下載後才知道，或從 libgen 回傳欄位推測。
  **可行性未知**：需先確認 libgen 是否回傳任何可區分掃描/原生的訊號
  （探勘報告指出公網結果無 ISBN/DOI/edition，可能也無此訊號）。
  若沒有，C 不可行，退回 A 或 B。

**判準（無論走哪條）**：修復後必須有一個測試能區分「這本真的是原生 PDF」
與「這本沒被判定過」。目前紅標同時是兩者的答案。

## 沒驗證的（filer 誠實標示）

- **沒有實際下載一本紅標公網書驗證它是不是掃描版**。本 BR 的證據是**程式碼層**的
  （公網路徑不產生 `pdf_scanned`），不是**產出層**的。理論上若 libgen 上非 epub 的書
  碰巧全是原生 PDF，使用者不會受害——但那是運氣不是設計。
- **沒有確認 libgen 回傳欄位中是否存在可用的掃描/原生訊號**。這格決定修復方向 C
  是否可行，需要實際看一次 libgen 的原始 HTML/回應。
- **沒有量化影響範圍**：不知道公網結果中實際掃描版的比例。若極低，本 BR 的
  Severity 應下調。
- **沒有檢查 `extension` 欄位本身的可信度**。src:364 依賴它，若 libgen 的
  extension 欄位也不可信，問題比本 BR 描述的更深。

---

*Filed by openshelf 值星官。使用者裁示建檔不修，列待辦。*
