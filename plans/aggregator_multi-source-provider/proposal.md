# Proposal: aggregator_multi-source-provider

## Why

- 目前唯一的線上書源是 libgen 系鏡像；使用者詢問「ebook 來源還有擴充的可能性嗎」，並在得知需評估合法來源後裁示「合法來源也值得納入」。
- `remote_catalog` 現有 identity 設計（`md5 TEXT UNIQUE` + `work_id=f"libgen_{md5}"` 硬編）只對單一來源成立，是接第二個來源前的硬阻擋，不是可延後的技術債。
- `SOURCE-SURVEY.md`（2026-08-19 實查）與本 plan 的實查（2026-09-02）之間，8 項結論已被推翻，該檔現況會誤導下一個讀它的 agent。

## Original Requirement Wording (Baseline)

- 「目前的 ebook 來源還有擴充的可能性嗎」
- 「我覺得合法來源也值得納入」
- 「轉交值星官開 plan」

## Requirement Revision History

- 2026-09-02: initial draft created via plan-init.ts
- 2026-09-02: 依 handler `ses_fd564662dffeNAwbCf6gqQ5N79` 唯讀調研（subagent `ses_f9fca06c2ffea7itSoYG65wtG2`，24,452 bytes 結論，全程帶負控制）收斂為本規格

## Effective Requirement Description

1. 在不破壞既有 libgen 資料的前提下，讓 `remote_catalog` 能容納多個合法來源（優先 Project Gutenberg，其後視驗證結果擴及 OpenStax／DOAB）。
2. 每個來源的書目必須能與其他來源的同一本書（FRBR Work 層級）判定為同一作品，避免重複書卡；但沒有任何免費識別符可單獨當跨來源主鍵，去重層級必須是 Work 而非來源記錄。
3. 新來源必須各自標示 per-item 授權（如 OpenStax 的 `license_name`），不得套用單一全域授權假設。
4. 明確排除法務層面禁止 TDM／爬取的來源（如 NAP）。

## Scope

### IN
- `remote_catalog_item` identity 重構：新增 `(source, source_native_id)` 複合唯一鍵，`md5` 降級為可空橋接欄位（非主鍵）。
- Project Gutenberg 作為第一個非 libgen provider，驗證多來源抽象是否成立。
- Open Library `search.json` 當橋接層（實作時一次性 enrich，不當查詢時依賴）。
- 只有在 Gutenberg 驗證通過後才納入 OpenStax（129 本，per-item 授權明確）。
- 去重控制組：插入兩筆 `md5=NULL` 不同來源 item 必須總數=2；插入同一 `(source, source_native_id)` 兩次必須總數=1。
- `SOURCE-SURVEY.md` 改寫，反映 2026-09-02 實查數字並標註推翻項。

### OUT
- Standard Ebooks（OPDS feed 401，免費替代途徑未驗證）。
- DOAB（全文可得率未量測，昊缰未決）。
- NAP（法務明確禁止 TDM，直接剔除，不進入候選）。
- 論文側來源（OpenAlex/arXiv/PMC 等，`SOURCE-SURVEY.md` 論文部分不在本 plan 範圍）。
- 下載ー直鏈解析層（`MirrorResolver`）不在本包重寫，維持現有 libgen 行為。

## Non-Goals

- 不追求跨來源即時查詢性能（OL rate limit 1-3 req/s，只能寫入時 enrich）。
- 不建立通用 provider plugin 架構（不提前抽象未驗證的未來來源）。

## Constraints

- Gutenberg 授權為 `dcterms:rights = "Public domain in the USA."`（非全球公版），必須在 UI 或元資料標示。
- Gutenberg 官方三個例外入口（private mirror / robot harvest / catalog data）才可批次取得，不得爬 `/ebooks/search`（robots.txt 明文禁止）。
- OL `search.json` 未識別 1 req/s，帶 UA+email 3 req/s，明文禁 bulk harvest。
- IA `licenseurl` 欄位存在已知錯誤（實測公版書被填成 GPL），不得信任，需改讀 `rights` 欄位。
- Wikidata `P2034`（Gutenberg ID）實測不存在於已驗證案例，不可當主橋接。

## What Changes

- `remote_catalog_item` schema：`md5 TEXT UNIQUE` 改為 `UNIQUE(source, source_native_id)`，`md5` 改為可空非唯一欄位。
- `work_id` 生成邏輯不再硬編 `f"libgen_{md5}"`，改為依 `(source, source_native_id)` 推導，舊 資料 migration 保證向下相容。
- 新增 `app/crawler/gutenberg_provider.py`（或同等模組）實作 Gutenberg 全量目錄拉取（catalog CSV 或 robot/harvest）。
- 新增 Open Library enrich 服務（寫入時呼叫，失敗不阻斷主流程）。

## Capabilities

### New Capabilities
- `MultiSourceIdentity`：以 `(source, source_native_id)` 當來源層主鍵，並保留 ISBN/OCLC/Gutenberg-ID 當可空橋接欄位。
- `GutenbergProvider`：免費公版書目拉取，不需 API key。
- `OpenLibraryBridge`：寫入時一次性查詢，回填 ISBN/OCLC/LCCN/Gutenberg-ID 多方對映。

### Modified Capabilities
- `Bookstalls & PersistentRemoteCatalog`：去重層級從單來源 md5 改為跨來源 identity，sidebar 總數仍為 distinct work 總數，不因新來源而重複計數。

## Impact

- `app/db/schema.sql`：`remote_catalog_item` 表結構。
- `app/crawler/libgen_live.py`：`work_id` 生成點（`:470`, `:552`）需抽象成共用函式。
- `app/db/remote_catalog.py`： upsert/query 需改對 `(source, source_native_id)`。
- `app/crawler/remote_catalog_refresh.py`：新增 provider 調度（不只限 libgen）。
- `SOURCE-SURVEY.md`：改寫 8 項推翻結論。
- `docs/ARCHITECTURE.md` 3.10：新增多來源 identity 說明。
