# Errors: aggregator_multi-source-provider

<!-- plan-builder:scaffold — replace every <placeholder>, then delete this line -->

## Outcome Codomain

Every functional module declares the COMPLETE set of states it can leave behind.
Mandatory classes: `ok` / `failed` / `not-run` — these are the three a bare
status collapses. `empty` (ran, produced nothing) and `indeterminate` (ran,
cannot decide) must be ADDRESSED: give a value, or a row with
`n/a (<reason>)` stating the module cannot produce it.

Every outcome names the site that emits it. An outcome nobody emits is a comment.

| Module | Value | Class | Means | Emitted at |
| ------ | ----- | ----- | ----- | ---------- |
| gutenberg_provider.refresh | ok | ok | 本次抓取成功，所有頁均取得 | app/crawler/gutenberg_provider.py |
| gutenberg_provider.refresh | failed | failed | 全鏡像/catalog CSV 下載失敗、解析失敗，舊 rows 保留不刪 | app/crawler/remote_catalog_refresh.py |
| gutenberg_provider.refresh | empty | empty | catalog 回傳 0 筆但操作成功（異常但不是錯誤） | app/crawler/gutenberg_provider.py |
| gutenberg_provider.refresh | not-run | not-run | 未設定 provider，本輪跳過（不開 refresh row） | app/crawler/remote_catalog_refresh.py |
| gutenberg_provider.refresh | n/a (網路途中中斷未回覆) | indeterminate | timeout 尚未觸發但連線已建立，尚未收到完整回應 | n/a (依 httpx timeout 設定，最終會轉 failed 或 ok) |
| openstax_provider.refresh | ok | ok | 全量分頁拉完，產出 >= 1 本 | app/crawler/openstax_provider.py |
| openstax_provider.refresh | failed | failed | API 下載/解析失敗、HTTP 非 2xx（含 `fields=` 帶未知欄位名的 400），舊 rows 保留不刪 | app/crawler/remote_catalog_refresh.py |
| openstax_provider.refresh | empty | empty | API 回 200 但 `items` 為 0 本（異常但不是錯誤） | app/crawler/openstax_provider.py |
| openstax_provider.refresh | not-run | not-run | 未設定 provider，本輪跳過（不開 refresh row） | app/crawler/remote_catalog_refresh.py |
| openstax_provider.refresh | n/a (逐本授權未宣告不是不確定) | indeterminate | 129 本中有 11 本 `license_name` 為 null；那是來源未宣告，寫 NULL 即正確，不得套用預設授權 | n/a (設計上允許，見 Error Catalogue OPENSTAX_LICENSE_UNDECLARED) |
| identity.upsert | ok | ok | (source, source_native_id) 寫入成功，去重正確 | app/db/remote_catalog.py |
| identity.upsert | failed | failed | UNIQUE 衝突以外的 DB 錯誤（磁碟滿、鎖定等） | app/db/remote_catalog.py |
| identity.upsert | n/a (md5=NULL 不是錯誤) | indeterminate | 非 libgen 來源本來就沒有 md5，不該被視為異常 | n/a (設計上允許) |
| identity.upsert | not-run | not-run | 呼叫端未帶 source_native_id（上游 provider 契約缺失），提前拒絕不寫入 | app/db/remote_catalog.py |
| identity.upsert | n/a (empty 不適用此模組) | empty | upsert 是單筆寫入操作，沒有「跑了但沒東西」的狀態 | n/a (單筆操作性質) |
| openlibrary_bridge.enrich | ok | ok | 回填至少一項橋接欄位 | app/crawler/openlibrary_bridge.py |
| openlibrary_bridge.enrich | failed | failed | HTTP 錯誤、非法 JSON，不阻斷主寫入流程 | app/crawler/openlibrary_bridge.py |
| openlibrary_bridge.enrich | empty | empty | 查詢成功但 OL 無對應記錄 | app/crawler/openlibrary_bridge.py |
| openlibrary_bridge.enrich | not-run | not-run | 節流命中（同分類短時間內已 enrich 過），本次跳過 | app/crawler/openlibrary_bridge.py |
| openlibrary_bridge.enrich | n/a (逾時前中斷未回覆) | indeterminate | 連線已建立但尚未收到完整回應，最終會轉 failed 或 ok | n/a (依 httpx timeout 設定) |

## Error Catalogue

The `failed` region of the codomain above, in detail.

| Code | Condition | Surface | Recovery |
| ---- | --------- | ------- | -------- |
| GUTENBERG_FETCH_FAILED | catalog CSV/robot-harvest 下載失敗 | log + refresh row 標 failed，舊 rows 保留 | 下輪自動重試，不需人工介入 |
| OPENSTAX_FETCH_FAILED | OpenStax CMS API 下載/解析失敗，或 HTTP 非 2xx | log + refresh row 標 failed，舊 rows 保留 | 下輪自動重試 |
| OPENSTAX_UNKNOWN_FIELD | `fields=` 帶了 API 不認得的欄位名 → 400 `{"message":"unknown fields: <name>"}` | 同上，歸 OPENSTAX_FETCH_FAILED | 修正 `OPENSTAX_FIELDS` 常數；這是編程錯誤不是暫時性故障 |
| OPENSTAX_LICENSE_UNDECLARED | 某本書的 `license_name` 為 null（2026-09-02 實測 129 本中有 11 本） | 該 row 的 license 寫 NULL，API 回應留空白 | **不是錯誤也不需恢復**。來源未宣告授權與已確認授權是不同的事，不得套用任何預設值頑上 |
| IDENTITY_DUP_KEY | 同一 (source, source_native_id) 重複寫入 | upsert 接受並更新，不報錯 | 無需巡回，upsert 本身就是強幂等 |
| OL_BRIDGE_TIMEOUT | Open Library 查詢逾時 | log warning，橋接欄位保留空白 | 不阻斷寫入，下次寫入同一書目時可重試 |
