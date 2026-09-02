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

- [x] 3.1 寫入時一次性呼叫 `search.json?fields=key,title,ia,ebook_access,isbn,oclc,lccn,id_project_gutenberg`，失敗不阻斷主流程 — artifact: app/crawler/openlibrary_bridge.py（`OL_SEARCH_URL` + `OL_FIELDS` + `build_query()` + `OpenLibraryBridge.enrich_item()`；**2026-09-02 實測驗證非憑記憶**，見下方「OL API 實測存證」。`enrich_item()` 刻意**不拋例外**，全數錯誤轉譯成 `outcome="failed"`，把「吞掉不阻斷」做成模組保證而非呼叫端責任；`test_ol_failure_does_not_block_or_rollback_the_main_write` 鎖定）
- [x] 3.2 回填 ISBN/OCLC/LCCN/Gutenberg-ID 為可空欄位；不當查詢時即時依賴（no synchronous call on request path） — artifact: app/db/remote_catalog.py（`mark_ol_enriched()` / `list_items_needing_ol_enrichment()`，白名單 `_OL_BRIDGE_COLUMNS` 限定可寫欄位，**絕不觸碰 Phase 1 的 source / source_native_id / md5**）；schema 側走 additive-only：`app/db/schema.sql` 內聯 6 個可空欄位供新 DB bootstrap、`app/db/dao.py` 的 `_COLUMN_MIGRATIONS["remote_catalog_item"]` 追加同 6 欄供舊 DB ALTER，**未動 Phase 1 的 `idx_remote_catalog_item_identity` 複合唯一索引也未改任何既有欄位語意**（`test_ol_migration_is_additive_and_keeps_phase1_composite_index` 逐項驗證）；請求路徑雔離由 `test_refresh_runs_ol_enrich_after_write_not_on_request_path` 以原碼掃描斷言 `category_routes.py` 完全不引用本模組
- [x] 3.3 節流測試：非 bulk、無 hundreds-of-requests 模式（單元測試以 mock transport 驗證呼叫頻率上界） — artifact: tests/test_remote_catalog.py（`test_ol_bridge_concurrency_is_bounded_to_one` 量測併發峰值：5 個並行請求觀察到 peak ≤ 1 且 > 0（正向控制）；`test_ol_bridge_enforces_minimum_interval_between_requests` 以假時鐘斷言 sleep 序列恰為 `[0.75]`（已過 0.25s 需補滿 1s）；`enrich_category(max_items=25)` 硬上限防 hundreds-of-requests）

### Phase 3 OL API 實測存證（2026-09-02，不得憑記憶）

| 探針 | 結果 |
| --- | --- |
| `q=isbn:9780553213119` + 完整 fields | HTTP 200、`numFound:1`、docs[0] 帶 ia/ebook_access/isbn/oclc/lccn |
| `q=title:moby dick` | HTTP 200、`numFound:1283`（**控制組**：不同查詢真的回不同結果，非固定回應） |
| `q=title:zzqqxxjjvvwwkk9987654` | HTTP 200、`numFound:0`、`docs:[]`（**真正的 empty 長這樣**） |
| `q=isbn:0000000000000` | HTTP 200、`numFound:1`，且該 doc 的 isbn 陣列**真的含這串**（`/works/OL45733017W`） |

最後一列是實測推翻直覺的地方：「明顯造假的 ISBN」**不是**可靠的 empty 判準，OL 真的收錄了帶該 ISBN 的資料。若拿它當空查詢的測試依據，會得到一個永遠紅不了的「empty」測試。已記入 `openlibrary_bridge.py` 模組 docstring。

### Phase 3 判別力證據（判準①）

`OLEnrichResult` 不用 Phase 2 的「例外 vs 回傳值」型別分離，而是**同型別但欄位互斥**。這是刻意的取捨：Gutenberg 的失敗**應該**中止該次刷新，OL 的失敗**不應該**（技術要求 5）；若用例外，就得在每個呼叫點包 try，反而把「吞掉」變成呼叫端的責任。

| outcome | error | fields_written | queried | 鎖定測試 |
| --- | --- | --- | --- | --- |
| ok | None | ≥ 1 | True | `test_ol_enrich_ok_writes_bridge_fields_without_touching_identity` |
| empty | None | 0 | True | `test_ol_enrich_empty_is_not_failed_and_still_marks_enriched` |
| failed | 非 None | 0 | True | `test_ol_enrich_http_error_is_failed_with_non_none_error`、`test_ol_enrich_invalid_json_is_failed_not_empty`、`test_ol_enrich_timeout_is_failed_with_ol_bridge_timeout_code` |
| not-run | None | 0 | **False** | `test_ol_enrich_within_ttl_is_not_run_and_makes_no_request`、`test_ol_enrich_without_any_clue_is_not_run_and_makes_no_request` |

`queried` 分開 not-run 與 empty（兩者 fields_written 都是 0）；`error` 分開 failed 與 empty。三態沒有任何一組欄位值重疊。

另外兩組控制：`ol_enriched_at` 在 **empty 時仍蓋**（那是一次有效查詢）、**failed 時不蓋**（否則一次失敗會被當成一次有效查詢而永不重試）；`test_ol_enrich_expired_ttl_requeries` 是節流的反向控制，排除「一旦 enrich 過就永遠不再查」。

**mutation 指紋（3.2 節流鍵）**：把 `list_items_needing_ol_enrichment` 的 `WHERE rci.ol_enriched_at IS NULL` 改成以橋接欄位是否為空判斷（`WHERE rci.ol_key IS NULL`）→ `test_ol_enrich_empty_is_not_failed_and_still_marks_enriched` 失敗（`1 failed, 48 passed`，AssertionError 落在該測試末段的 pending 斷言）；還原後 `49 passed`，還原檔 sha256 `732d8c61ef31531cb61a97155aefd5be2cf684b3381ad291d4e7f3818448a83b`（mutation 前後一致）。

> **這條 mutation 第一次跑是「未殺死」（`49 passed`），紀錄於此不刪。** 當時沒有任何測試檢查「empty 之後該 item 不再被列為 pending」——`empty` 的 item 橋接欄位全空，用哪個鍵判定 pending 在測試上完全不可觀察，於是節流鍵可以被換掉而無人叫。**那個綠燈正是它要防的病灶在測試層的翻版**：缺席態（沒查過）與空結果態（查過但 OL 沒有）共用同一個輸出。補上 `assert remote.list_items_needing_ol_enrichment("cat_471") == []` 後 mutation 才轉紅。先寫指紋、後驗指紋會得到一份看起來完備但沒有判別力的文件——保留兩次結果作為證據。

**errors.md `openlibrary_bridge.enrich` 五態對應**：`ok` / `empty` / `failed` / `not-run` 見上表；`indeterminate` 依 errors.md 已宣告為 `n/a`（連線已建立但未收到完整回應，最終依 httpx timeout 轉 failed 或 ok），無專屬測試，此為文件既定值非本階段遺漏。

## 4. OpenStax（DD-3 延伸，待 Gutenberg 驗證通過才開工）

- [x] 4.1 新增 OpenStax provider，逐本讀 `license_name`（不得套用全域授權假設） — artifact: app/crawler/openstax_provider.py（`OpenStaxProvider.fetch_books()` 分頁拉完，`parse_books_payload()` 逐本帶出 `license_name`；`source_native_id` 取 CMS 數字 `id` 而非 slug（實測兩者各自唯一 129/129，但 slug 含非 ASCII 且可重命名）；`work_id` 重用 `make_work_id()`。調度側 `remote_catalog_refresh.py:refresh_openstax()` 以 `openstax=` 具名參數並存，逐字不改 libgen / Gutenberg 兩條路徑）
- [x] 4.2 全欄位取得（`&fields=` 會 400，需整份 payload 解析） — artifact: app/crawler/openstax_provider.py（**該前提已被 2026-09-02 實測推翻，見下方專節**：`fields=` 不會 400，而且不帶它根本拿不到 `license_name`，故本任務**必須**用 `fields=`。欄位清單收新在 `OPENSTAX_FIELDS` 單一常數，寫錯會在第一次請求 400 fail fast）；逐本授權落地於 `app/db/schema.sql` + `app/db/dao.py` 的 `license_name` 可空欄（additive-only），讀路徑 `app/db/remote_catalog.py:query_browseable()` 做兩層解析

### ⚠ Phase 4 實測推翻了任務前提：`&fields=` 不會 400（2026-09-02）

派工單與本文件原記載「加 `&fields=` 會 400，需整份 payload 解析」。實測四組探針：

| 探針 | 結果 |
| --- | --- |
| `?type=books.Book&limit=100`（無 fields） | HTTP 200、`meta.total_count:129`，但 items 只有 `id` / `meta` / `title` 三個 key——**沒有 `license_name`** |
| `&fields=title,license_name` | **HTTP 200**（非 400），且真的帶回 `license_name` |
| `&fields=zzz_not_a_field` | **HTTP 400** `{"message": "unknown fields: zzz_not_a_field"}` |
| `&fields=*` | HTTP 200、77 個欄位、3.6 MB |
| `&limit=100&offset=100` | HTTP 200、回 29 筆（129 − 100），分頁成立 |

**真相**：400 只發生在 `fields=` 帶了 API 不認得的**欄位名**，不是「帶 fields 就 400」。而且方向是反的——不帶 `fields=` 根本拿不到 `license_name`，本 phase 的核心要求（逐本授權）在不帶 `fields=` 的情況下**做不到**。

`fields=*` 是另一條能拿到 license_name 的路，但要付 3.6 MB / 77 欄去換 5 個欄位，且未來新增欄位會靜默改變 payload 大小，故不採用。已將此結論寫入 `openstax_provider.py` 模組 docstring 與 errors.md 的 `OPENSTAX_UNKNOWN_FIELD`。

### Phase 4 逐本授權的實測分佈（這是本 phase 存在的理由）

129 本 → **3 種**值：

| 本數 | `license_name` |
| --- | --- |
| 72 | `Creative Commons Attribution-NonCommercial-ShareAlike License` |
| 46 | `Creative Commons Attribution License` |
| **11** | **`null`（來源未宣告）** |

那 11 本（`college-physics-courseware`、`cálculo-volumen-1/2/3`、`física-universitaria-volumen-2` 等）是**來源未宣告**，不是抓取缺陷。寫 NULL 才正確——套用任何預設值等於替出版方做了它沒做的聲明。已登記為 errors.md 的 `OPENSTAX_LICENSE_UNDECLARED`（明註「不是錯誤也不需恢復」）。

### Phase 4 授權模型選型（派工單要求明講，不留給人猜）

**選：兩層並存，逐本優先、來源層回退。不擴充 `SOURCE_LICENSE_LABEL` 支援逐本覆寫，也不廢掉它。**

```
query_browseable() 讀路徑：
    item["license"] = rci.license_name  or  license_for_source(rci.source)
                      ↑逐本（Phase 4）      ↑來源層（Phase 2）
    兩者都無 ⇒ None（空白）
```

理由：兩種授權在**資料性質上不同**，不該塵縮成同一個機制。Gutenberg 的 `"Public domain in the USA."` 是**來源的性質**（全庫同一句，寫進 DB 反而讓舊 rows 停留在舊字串，Phase 2 已記載此理由）；OpenStax 的是**逐筆資料**（同一來源 3 種值，必須隨 row 存）。把後者塞進前者的 dict 需要一個 `(source, native_id) -> license` 的全域表，那就是把 DB 搬進常數。

### Phase 4 判別力證據（判準①）

**選型：沿用 Gutenberg 的「例外 vs 回傳值」型別分離，不用 OL 的欄位互斥。** 判準不是「哪個寫法好看」而是「失敗要不要阻斷主流程」：OpenStax 是主資料來源，抓取失敗**應該**中止該次刷新（同 Gutenberg）；OL 只是補充欄位，失敗**不應該**中止（故那邊用欄位互斥）。

| 判別對 | 正向控制 | 反向控制 |
| --- | --- | --- |
| 解析器壞掉 vs 真的 0 本 | `test_parse_books_payload_keeps_per_book_license_including_null`（3 筆） | `test_parse_books_payload_on_empty_items_is_empty_not_an_error`（0 筆不丟例外） |
| `failed` vs `empty` | `test_openstax_zero_books_is_empty_outcome_not_failure` | `test_openstax_connection_failure_raises_not_empty`、`test_openstax_unknown_field_400_is_failed_not_parsed_as_empty` |
| refresh row 層可分辨 | `test_refresher_openstax_failed_and_empty_are_distinguishable`（同一測試內建兩情境，status/error 皆不同） | 同左 |
| `not-run` vs `empty` | `test_refresher_without_openstax_provider_is_not_run`（not-run 連 refresh row 都沒有） | 同左 |
| **逐本授權 vs 全域假設** | 兩本有宣告的拿到**不同**字串 | 未宣告那本必須是 `None`，不得被預設值頂上 |
| 並存不回歸 | Gutenberg 來源層字面值在同一分類內仍正確 | `test_openstax_per_book_license_is_visible_in_api_model` 四種情境同時斷言 |
| 節流真的存在 | peak > 0（請求有進 transport） | peak ≤ 2（拿掉 limiter 會衝到 5） |

**mutation 指紋（4.2 逐本授權）**：把 `parse_books_payload()` 的 `"license_name": license_name` 改成寫死單一全域值（`"Creative Commons Attribution License"`，即 design.md 明禁的全域授權假設）→ **`2 failed, 64 passed`**，失敗的是 `test_parse_books_payload_keeps_per_book_license_including_null` 與 `test_openstax_per_book_license_is_visible_in_api_model`；還原後 `66 passed`，還原檔 sha256 `534956cfbf16b4d9b17186b510429860d5492ef01baad25579dc84829ed60c2b`（mutation 前後一致）。

**errors.md `openstax_provider.refresh` 五態對應**：`ok` → `test_openstax_fetch_ok_and_request_shape_uses_fields_param` / `test_three_sources_coexist_in_one_category`；`failed` → `test_openstax_connection_failure_raises_not_empty` + `test_openstax_unknown_field_400_...` + refresh row 側 `test_refresher_openstax_failed_and_empty_are_distinguishable`；`empty` → `test_openstax_zero_books_is_empty_outcome_not_failure` + 同一條 refresh row 測試；`not-run` → `test_refresher_without_openstax_provider_is_not_run`；`indeterminate` → errors.md 宣告為 `n/a`（逐本授權未宣告不是不確定，寫 NULL 即正確），由 `test_parse_books_payload_keeps_per_book_license_including_null` 的 `licenses["311"] is None` 鎖定。

## 5. 文件債清理

- [x] 5.1 改寫 `SOURCE-SURVEY.md` 書籍側，反映 2026-09-02 實查數字並標註 8 項已推翻結論 — artifact: SOURCE-SURVEY.md（檔首新增修訂標記並**明寫論文側未重驗不背書**；§書籍側整節重寫為 7 列含採用狀態旗標（✅/—/❌）；新增〈已被 Phase 1-4 實測推翻的舊結論〉**8 列對照表**，每列帶依據欄；§Q-C 填補方向改為四列現況表；§未查到清單第 3/8 項划掉並標實查數字。**推翻清單是重建的不是拄來的**，見下方 Phase 5 摩擦欄）
- [x] 5.2 `docs/ARCHITECTURE.md` 3.10 新增多來源 identity 段落 — artifact: docs/ARCHITECTURE.md:116（新增 §3.10.1 `多來源 Identity 與授權模型`，插在 3.10 末尾與 3.11 之間，不動原 3.10 任何字；內容含複合鍵 `(source, source_native_id)` 與 md5 降級理由、additive-only migration 約束、三 provider 並存對照表、OL 橋接層請求路徑雔離與「欄位互斥 vs 例外分離」取捨理由、兩層授權模型及未宣告必須寫 NULL）

### ⚠ Phase 5 摩擦（判準②）：「8 項推翻結論」從來沒有明細

`design.md:17`、`design.md:50`、`proposal.md:7`、`proposal.md:80` 四處都寫「8 項已推翻結論」，
**但 plan 包內任何一份文件都沒有列出那 8 項是哪 8 項**。這不是我搜尋不到，是資訊從一開始
就不在系統裡（原始調研結論 24,452 bytes 在 subagent `ses_f9fca06c2ffea7itSoYG65wtG2` 的 session 內，
未落成檔案）。

**處置**：依 Phase 1-4 實際留下的可驗證依據（commit sha / 實測表 / DD 條目）**逐項重建**出
8 項，寫進 SOURCE-SURVEY.md，並在表前**明寫「它是否與原作者心中那 8 項逐字相同，無法從
現存文件判定——此為待確認項」**。沒有用推測填空，也沒有靠沉默把它讀成「已完成」。

另一個相關摩擦：`design.md` DD-3 寫「catalog CSV 79,288 筆」，那是調研 subagent 的二手數字，
而我 Phase 2 只實測過 URL / 表頭 / `content-length`，**從未數過行**。本次實下並以 `csv.DictReader`
逐列計數：**總列數 79,288 確實吻合，但 `Type=="Text"` 只有 78,037 本**（餘 1,251 列是 Sound 1,114 /
Dataset 89 / Image 33 / MovingImage 8 / Collection 4 / StillImage 3）。`gutenberg_provider.py` 只收
`Type == "Text"`，所以「79,288」拿來當書籍本數會**高估 1,251 本**。SOURCE-SURVEY.md 已兩個數字並列。

### Phase 5 改動對應依據（每項都對得回一個實測或 sha）

| 改動 | 依據 |
| --- | --- |
| Gutenberg 79,288 列 / 78,037 本 Text | 2026-09-02 實下 CSV + `csv.DictReader` 逐列計數；`http_code=200`、`size=21196613`；負向控制：同 host `pg_catalog_DOES_NOT_EXIST.csv` 回 **404**（故 200 不是「什麼都回 200」） |
| Gutenberg catalog CSV 是官方免申請入口 | 同上；另見 tasks.md 2.1 的 Phase 2 實證 |
| OpenStax 129 本 | 2026-09-02 重驗 `?type=books.Book&limit=1` → HTTP 200、`meta.total_count=129`；commit `4d471c8b` |
| OpenStax 逐本授權 72/46/11 | tasks.md §Phase 4 逐本授權實測分佈；commit `4d471c8b` |
| `&fields=` 不會 400（400 只在未知欄位名） | tasks.md §⚠ Phase 4 四組探針表 |
| Standard Ebooks OPDS 401 | 2026-09-02 curl `/feeds/opds/all` → **401**；**正向控制組**：同站首頁 → **200**（故 401 是授權牆，不是站台不通或我網路壞了） |
| Standard Ebooks 本數**仍未查到** | 實話實說，未以推測填空 |
| OL `search.json` 6 方對映可用 | 2026-09-02 重驗完整 fields 查詢 → HTTP 200；tasks.md §Phase 3 OL API 實測存證；commit `049c32cf` |
| 跨來源主鍵不存在 → 複合鍵 | `design.md` DD-1；commit `65e945d8` |
| IA `licenseurl` 不可信 | `design.md` DD-6（承襲調研實測，本次未重驗，文件中未改其語氣） |
| NAP 剔除 | `design.md` DD-7 |
| Wikidata P2034 不可信 | `design.md` DD-5 |
| HathiTrust / DOAB / OL dump 大小 | **未重驗**，文中已逐列標明「此為 2026-08-19 原數字，本次未重驗」 |
| 論文側全節 | **未動**，檔首修訂標記已明寫不背書 |

## 6. 收尾

- [x] 6.1 完整測試套件通過、VANS 獨立稽核 identity 重構與 mutation 控制組 — artifact: verb:plan_closeout（VANS session `ses_f9ed1018effeRrVImqGd38dYpU`（claude-cli/claude-opus-5）首輪稽核 e5c9c8f..c69ff3e7：7/8 cleared，1 項 request-changes（`upsert_batch` not-run/ok 共用輸出，判準①違反）；handler 修正後複驗 **cleared**。最終 commit `b971521f`。完整測試套件：**492 passed, 27 skipped, rc=0**）
- [x] 6.2 event log + architecture sync 判定 — artifact: verb:event_record（Phase 1-6 逐階段皆已記錄，Phase 6 收尾兩筆：`event_2026-09-02_aggregator-multi-source-provider-phase-6-vans_jufpqc`、`event_2026-09-02_aggregator-multi-source-provider-phase-6-architect_jufpvs`）
