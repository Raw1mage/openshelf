# Design: aggregator_multi-source-provider

<!-- plan-builder:scaffold — replace every <placeholder>, then delete this line -->

## Context

現有 `remote_catalog_item` 只服務 libgen 單一來源：`md5 TEXT UNIQUE`（`schema.sql:183`）當主鍵，`work_id = f"libgen_{md5}"` 硬編於 `libgen_live.py:470/:552`。使用者要求擴充合法來源；調研（handler `ses_fd564662dffeNAwbCf6gqQ5N79` 派出 subagent `ses_f9fca06c2ffea7itSoYG65wtG2`，2026-09-02 實查，24,452 bytes 結論）證實：沒有任何免費識別符能單獨當跨來源主鍵（ISBN/OCLC/LCCN 回傳陣列、非一對一；同書在不同來源有不同 ID）。若不先重構 identity，接第二個來源時 SQLite 對多筆 `md5=NULL` 不觸發 UNIQUE 衝突 → 靜默失去去重（缺席態與失敗態共用輸出）。

## Goals / Non-Goals

**Goals**

- 重構 identity 為 `(source, source_native_id)` 複合鍵，`md5` 降級為可空橋接欄位，不破壞既有 libgen 資料。
- 接入 Project Gutenberg 作第一個非 libgen provider，驗證抽象是否成立。
- 只有 Gutenberg 驗證通過才接 OpenStax（129 本，per-item 授權明確）。
- Open Library 當寫入時 enrich 橋接層，不當即時查詢依賴（rate limit 1-3 req/s，明文禁 bulk）。
- 改寫 `SOURCE-SURVEY.md` 反映 2026-09-02 實查數字，標註已推翻的 8 項舊結論。

**Non-Goals**

- 不做通用 provider plugin 架構（不為未驗證的未來來源預先抽象）。
- 不追求跨來源即時查詢效能（OL enrich 只在寫入時做）。
- 不接 Standard Ebooks（OPDS feed 401，免費替代路徑未驗證本數）、DOAB（全文可得率未量測）、NAP（法務明文禁 TDM，直接剔除不進候選）。

## Decisions

- DD-1: identity 主鍵改為 `UNIQUE(source, source_native_id)`。理由：沒有免費跨來源識別符能單獨當主鍵（ISBN/OCLC/LCCN 回傳陣列非一對一，Gutenberg 同書有 3 個 ID，OL 同書有 2 個 work key）。拒絕方案：維持 `md5` 當主鍵並讓非 libgen 來源填 md5=NULL——已被實測證明 SQLite 對多筆 NULL 不觸發 UNIQUE，靜默去重失效。
- DD-2: `md5` 欄位保留但降級為可空、非唯一。理由：既有 libgen 資料與下載流程（`MirrorResolver` 的 `ads.php`/`get.php`）仍依賴 md5，不可斷；新來源（Gutenberg）本身不提供 md5。
- DD-3: Gutenberg 是第一個新 provider，而非 OpenStax 或 DOAB。理由：唯一無需 API key、metadata+全文皆可直接批次取得的來源（catalog CSV 79,288 筆、robot/harvest 官方三例外之一），最適合驗證「多來源抽象是否真的成立」這個核心風險，且失敗代價最低（不涉及授權判斷複雜度）。
- DD-4: Open Library 只當寫入時橋接，不當查詢時依賴。理由：官方明文禁止「hundreds of single-book requests」與 bulk harvest，未識別 1 req/s；`search.json` 一次查詢可回 6 方對映（ISBN/OCLC/LCCN/IA/Gutenberg-ID），故只需在新書寫入當下呼叫一次並快取結果。
- DD-5: 不採用 Wikidata 當主橋接。理由：實測已驗證案例（Q174596 Moby Dick）P2034（Gutenberg ID）不存在，覆蓋率不可信。
- DD-6: 不信任 IA `licenseurl` 欄位。理由：實測公版書被錯填 GPL；改讀 `rights` 欄位。
- DD-7: NAP 直接剔除不進候選清單。理由：`terms-of-use` 明文禁止 automated scraping/TDM/AI training，援引 EU DSM Directive Art. 4(3)；連 metadata 索引都不行，非技術可繞過的限制。

## Risks / Trade-offs

- **Schema migration 影響已驗收的 `remote_catalog` 契約**（`feature_persistent-remote-catalog` 剛於 `6b3c77a` 經 VANS R2 放行）—— mitigation: additive-only migration（新增複合唯一索引、md5 唯一約束降級為一般索引），不刪除既有欄位；migration 前後跑該 plan 既有的 mutation 與控制組回歸，並另開 VANS 稽核。
- **`work_id` 生成邏輯變更影響下載/收藏等下游呼叫點** —— mitigation: 全 repo grep 所有 `work_id` 消費點，逐一驗證新舊格式相容或提供遷移路徑；既有 libgen work_id 格式不變（向下相容）。
- **Gutenberg 授權非全球公版**（`dcterms:rights = "Public domain in the USA."`）——mitigation: per-item 授權欄位必須顯示於 UI/API，不得暗示為通用公版。
- **Gutenberg rate limit 無官方數字**（僅定性「封 IP」）——mitigation: 保守節流 + 指數退避，批次抓取安排在離峰時段。
- **DOAB/library.oapen.org 主機層 timeout**（非網路層，來源盤點已用 `directory.doabooks.org` 佐證）——不在本包範圍，留待 DOAB 若未來重啟時處理。

## Critical Files

- `app/db/schema.sql:183` — `remote_catalog_item` 的 identity 定義，DD-1/DD-2 的直接落點。
- `app/crawler/libgen_live.py:470,552` — `work_id` 生成邏輯，需抽象為跨 provider 共用函式。
- `app/db/remote_catalog.py` — upsert/query DAO，需改為以 `(source, source_native_id)` 為鍵。
- `app/crawler/remote_catalog_refresh.py` — 背景刷新排程器，需新增 provider 調度（不再假設只有 libgen）。
- `app/crawler/mirror_resolver.py`（下載路徑）— 硬綁 `ads.php`/`get.php`，需確認 Gutenberg 直鏈下載不誤觸此路徑。
- `SOURCE-SURVEY.md` — 8 項推翻結論待改寫。
