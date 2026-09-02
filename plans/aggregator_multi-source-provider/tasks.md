# Tasks: aggregator_multi-source-provider

<!-- plan-builder:scaffold — replace every <placeholder>, then delete this line -->

> **Every `- [x]` must name a machine-verifiable artifact.** Ticking a box is a
> claim made by whoever ticked it; it carries no evidence on its own. Close each
> task with an `artifact:` field so the gate can check the claim:
>
> | form | example |
> |---|---|
> | file | `artifact: packages/lib/src/tools.ts` |
> | code anchor | `artifact: packages/lib/src/tools.ts:158` |
> | registered verb | `artifact: verb:plan_closeout` |
> | several | `artifact: a/b.ts, verb:spec_tick_task` |
> | genuinely none | `artifact: none (decision only — see DD-3)` |
>
> `plan-validate` warns on an unnamed or non-existent artifact; `plan_closeout`
> blocks. Use `none` honestly rather than naming a file that does not exist —
> a broken claim blocks on EVERY package, an honest `none` never does.

## 1. Identity 重構（DD-1/DD-2，不得跳過，接第二個來源的前置條件）

- [x] 1.1 `remote_catalog_item` schema 新增 `UNIQUE(source, source_native_id)` 複合鍵；`md5` 降級為可空、非唯一 — artifact: app/db/schema.sql（新 DB 由 schema.sql 內聯欄位帶出；複合唯一索引實際落在 app/db/dao.py 的 `_POST_MIGRATION_INDEXES`，理由見該處註解——需等回填跑完才能建索引）
- [x] 1.2 additive-only migration：新舊 DB 皆可 bootstrap，既有 libgen rows 的 `source_native_id` 由既有 md5 回填，不得遺失資料 — artifact: app/db/dao.py（`_COLUMN_MIGRATIONS["remote_catalog_item"]` ALTER 補欄位 + `apply_column_migrations()` 內顯式 `UPDATE ... SET source_native_id = md5 WHERE source_native_id IS NULL`，回填後才建複合唯一索引）
- [x] 1.3 反向控制組測試：插入 2 筆 `md5=NULL` 不同來源 item 總數須為 2；插入同一 `(source, source_native_id)` 兩次總數須為 1 — artifact: tests/test_remote_catalog.py（`test_null_md5_different_sources_are_not_collapsed`、`test_same_composite_key_upsert_twice_keeps_one`，另加 `test_missing_source_native_id_and_md5_is_rejected_not_run` 驗證 errors.md 的 not-run 語意）
- [x] 1.4 `work_id` 生成邏輯抽為跨 provider 共用函式，libgen 既有格式不變（向下相容） — artifact: app/crawler/libgen_live.py:12（`make_work_id(source, source_native_id)`，libgen 呼叫端已改用 `make_work_id("libgen", md5_val)`，輸出格式與舊版逐字相同，見下方 1.5 相容性盤點）
- [x] 1.5 全 repo grep 所有 `work_id` 消費點，逐一驗證新舊格式相容或提供遷移路徑 — artifact: none (verification only — see task 1.2 migration)。盤點結果：`app/api/category_routes.py:92`（`f"libgen_{md5}"` 手刻同格式，未接 make_work_id，屬 non-`remote_catalog_item` 讀路徑，格式不變不受影響）；`app/db/remote_catalog.py:254`（`query_browseable` 讀路徑，`COALESCE('libgen_' || lower(rci.md5), rci.catalog_id)`，libgen 分支不變、非 libgen 分支新增 catalog_id 回退）；`tests/test_bookstall_race.py:89`、`tests/test_categories.py:136`、`tests/test_libgen_parser_md5_gate.py`（多處）皆斷言字面值 `libgen_{md5}`，全部通過（見 §驗證 full-suite 438 passed）。**未發現新舊格式不相容的消費點**——本次改動未修改 libgen work_id 的輸出字串，只新增非 libgen 分支。
- [x] 1.6 mutation 指紋：複合鍵約束被移除時上述控制組須失敗，並附還原 sha256 — artifact: tests/test_remote_catalog.py（見 §mutation 指紋表；commit 65e945d8f1c04d9739ef24085db5770e128f8747，dispatcher 獨立重跑 mutation 判別力成立：移除索引 → 12/13 fail/error，還原後 13 passed）

## 2. Gutenberg Provider（DD-3，驗證多來源抽象是否成立）

- [x] 2.1 新增 `app/crawler/gutenberg_provider.py`：拉取 `cache/epub/feeds/pg_catalog.csv`，解析為 `RemoteCatalogItem`，`source="gutenberg"`、`source_native_id=<Gutenberg ID>` — artifact: app/crawler/gutenberg_provider.py（`GUTENBERG_CATALOG_URL` = `https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv`，**2026-09-02 實證非憑記憶**：`HTTP/2 200`、`content-type: text/csv`、`content-length: 21196613`、`last-modified: Sun, 30 Aug 2026 21:32:16 GMT`；負向控制：同 host 的 `pg_catalog_DOES_NOT_EXIST.csv` 回 404，故 200 不是「什麼都回 200」。`parse_catalog_csv()` 逐字對齊實查表頭 `Text#,Type,Issued,Title,Language,Authors,Subjects,LoCC,Bookshelves`，只收 `Type == "Text"`，`work_id` 重用 `libgen_live.make_work_id()` 未重複實作）
- [x] 2.2 授權標記：`license="Public domain in the USA."`，UI/API 需可見且不得與全球公版混同 — artifact: app/models/catalog.py（`SOURCE_LICENSE_LABEL` 字面值 SSOT + `license_for_source()`；`SearchResultItem` 新增 `source` / `license` 兩欄，`remote_catalog.py:query_browseable()` 在讀路徑推導。libgen 之 license 為 `None`（來源未宣告 ≠ 已確認公版），測試 `test_api_model_exposes_gutenberg_license_and_leaves_libgen_blank` 同時鎖定兩側）
- [x] 2.3 全鏡像/連線失敗記為 `failed`，不得與合法空頁共用輸出（沿用既有 libgen 契約，`remote_catalog_refresh.py` 已有此不變量） — artifact: app/crawler/remote_catalog_refresh.py（新增 `refresh_gutenberg()` 與 libgen `refresh()` **並存**，逐字未改既有路徑，`test_libgen_refresh_path_is_unchanged_by_provider_dispatch` 鎖定；`failed` 走 `GutenbergFetchError` 例外分支 → `status='failed'` + error 含 `GUTENBERG_FETCH_FAILED`，`empty` 走正常回傳 → `status='fresh'` + `error IS NULL`，**型別與 refresh row 皆不共用**）
- [x] 2.4 端到端測試：Gutenberg provider 產出的 item 與既有 libgen item 可同時出現在同一分類的去重總數中，不重複計數同一 FRBR work（若可判定） — artifact: tests/test_remote_catalog.py（`test_gutenberg_and_libgen_items_coexist_in_one_category` libgen 1 + gutenberg 2 = 3 且無一筆因 md5=NULL 被吃掉；`test_gutenberg_refresh_is_idempotent_on_repeat` 重刷仍為 2。FRBR work 層去重是 design.md 明列 Non-Goal，未做）
- [x] 2.5 保守節流 + 指數退避（無官方 req/s 數字，依既有 CapacityLimiter 形狀） — artifact: app/crawler/gutenberg_provider.py（`_HTTP_LIMITER = anyio.CapacityLimiter(2)`，形狀比照 download_worker.py:39；`test_gutenberg_concurrency_is_bounded_by_capacity_limiter` 以 mock transport 量測併發峰值：6 個並行請求觀察到 peak ≤ 2 且 > 0（正向控制，證明請求真的進到 transport）。退避由 `test_gutenberg_retries_with_exponential_backoff_before_failing` 斷言 sleep 序列 `[0.5, 1.0]`，正向控制 `test_gutenberg_recovers_on_second_attempt` 排除「provider 永遠失敗」）

### Phase 2 判別力證據（判準①）

| 判別對 | 正向控制 | 反向控制 |
| --- | --- | --- |
| 解析器有沒有壞掉 vs CSV 真的 0 筆 | `test_parse_catalog_csv_produces_source_scoped_identity_without_md5`（2 筆） | `test_parse_catalog_csv_on_header_only_is_empty_not_an_error`（0 筆不丟例外） |
| `failed` vs `empty` | `test_gutenberg_zero_rows_is_empty_outcome_not_failure` | `test_gutenberg_fetch_failure_raises_and_is_not_an_empty_result`、`test_gutenberg_http_404_is_failed_not_silently_parsed_as_empty` |
| refresh row 層可分辨 | `test_refresher_records_failed_and_empty_with_distinguishable_rows`（同一測試內同時建立兩情境，斷言 status/error 皆不同） | 同左 |
| `not-run` vs `empty` | `test_refresher_without_gutenberg_provider_is_not_run`（not-run 連 refresh row 都沒有，status 仍 `never_refreshed`） | 同左 |
| 節流真的存在 | peak > 0（請求有進 transport） | peak ≤ 2（拿掉 limiter 會衝到 6） |

**mutation 指紋（2.3 HTTP status gate）**：移除 `gutenberg_provider.py` 的 `response.raise_for_status()` 一行 → `test_gutenberg_http_404_is_failed_not_silently_parsed_as_empty` 失敗（`1 failed, 30 passed`）；還原後 `31 passed`。還原檔 sha256 `7c9540a794ade0fa6e2d6bc4ed0cd698c6d074c9dc56de1d644e2a14fd342f4a`（mutation 前後一致，已確認完整還原）。

**errors.md `gutenberg_provider.refresh` 五態對應**：`ok` → `test_gutenberg_non_empty_catalog_is_ok_outcome` / `test_gutenberg_and_libgen_items_coexist_in_one_category`；`failed` → `test_gutenberg_fetch_failure_raises_and_is_not_an_empty_result` + `test_gutenberg_http_404_...` + refresh row 側 `test_refresher_records_failed_and_empty_...`；`empty` → `test_gutenberg_zero_rows_is_empty_outcome_not_failure` + 同一條 refresh row 測試；`not-run` → `test_refresher_without_gutenberg_provider_is_not_run`；`indeterminate` → errors.md 已宣告為 `n/a`（依 httpx timeout 最終轉 failed 或 ok），無專屬測試，此為文件既定值非本階段遺漏。

**mirror_resolver 邊界**：未修改 `mirror_resolver.py`。`test_gutenberg_mirror_links_do_not_match_libgen_download_markers` 斷言 PG 直鏈（`https://www.gutenberg.org/ebooks/<id>.epub.images`）不含 `ads.php` / `get.php` / `md5=`，故不會被 `mirror_resolver.py:101` 的 libgen 判斷式誤傷或誤放行。

## 3. Open Library 橋接層（DD-4/DD-5）

- [ ] 3.1 寫入時一次性呼叫 `search.json?fields=key,title,ia,ebook_access,isbn,oclc,lccn,id_project_gutenberg`，失敗不阻斷主流程 — artifact: app/crawler/openlibrary_bridge.py
- [ ] 3.2 回填 ISBN/OCLC/LCCN/Gutenberg-ID 為可空欄位；不當查詢時即時依賴（no synchronous call on request path） — artifact: app/db/remote_catalog.py
- [ ] 3.3 節流測試：非 bulk、無 hundreds-of-requests 模式（單元測試以 mock transport 驗證呼叫頻率上界） — artifact: tests/test_remote_catalog.py

## 4. OpenStax（DD-3 延伸，待 Gutenberg 驗證通過才開工）

- [ ] 4.1 新增 OpenStax provider，逐本讀 `license_name`（不得套用全域授權假設） — artifact: app/crawler/openstax_provider.py
- [ ] 4.2 全欄位取得（`&fields=` 會 400，需整份 payload 解析） — artifact: app/crawler/openstax_provider.py

## 5. 文件債清理

- [ ] 5.1 改寫 `SOURCE-SURVEY.md` 書籍側，反映 2026-09-02 實查數字並標註 8 項已推翻結論 — artifact: SOURCE-SURVEY.md
- [ ] 5.2 `docs/ARCHITECTURE.md` 3.10 新增多來源 identity 段落 — artifact: docs/ARCHITECTURE.md

## 6. 收尾

- [ ] 6.1 完整測試套件通過、VANS 獨立稽核 identity 重構與 mutation 控制組 — artifact: verb:plan_closeout
- [ ] 6.2 event log + architecture sync 判定 — artifact: verb:event_record
