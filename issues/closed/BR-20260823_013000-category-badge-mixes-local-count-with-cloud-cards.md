# BR-20260823_013000 — 線上書攤分類數量必須涵蓋所有可逛書目

- **Status**: CLOSED
- **Reopened**: 2026-08-23 — 前次修復反向移除線上可逛書目，違反產品語意
- **Closed**: 2026-08-23 by `ses_fe7b5cbadffeSlxj0dv1Z740O4`
- **Severity**: Medium
- **Scope**: `classification-display-consistency`

## 使用者可見症狀

「奇幻與魔法」分類徽章顯示 2，本頁卻顯示超過 2 張書卡，使用者無法判斷數量與分類內容何者可信。

## RCA

分類樹的 `works_count` 只統計資料庫內具有可信分類的本地藏書；書架前端卻固定以 `include_cloud=true` 呼叫分類 API。API 除本地分類藏書外，再混入最多 15 筆即時 Libgen 雲端推薦，最後以混合後的 `len(items)` 回傳 `total`。

現役控制組：

- `cat_880` 分類樹徽章：2
- `include_cloud=true`：17 張卡（本地 2、雲端 15）
- `include_cloud=false`：`category.works_count=2`、`total=2`、`items=2`
- 不存在分類：HTTP 404

前次 RCA 正確找出兩個集合不一致，卻錯誤裁決成「砍掉線上集合」。線上書攤的產品語意本來就是瀏覽本地藏書與線上可收書目；真正缺陷是 sidebar 徽章仍顯示本地數量，沒有在雲端書目載入後同步成實際可逛總數。

## 最終修復契約

1. 線上書攤先從 SQLite 讀取本地藏書與已累積遠端 catalog 的去重聯集，不等待外部網路。
2. 過期、失敗或從未刷新時，僅排入單分類背景刷新；刷新只 upsert，某次缺席不得刪除舊書目。
3. API `total` 是分類子樹內本地＋遠端的穩定 ID 去重聯集；書卡按 `page/page_size` 分頁，單頁長度不得冒充總數。
4. `catalog_status.accumulated_total` 專指持久化遠端 catalog distinct 全集；`never_refreshed|failed|fresh` 均須回真實持久化數量。
5. 全鏡像網路／解析失敗必須記 `failed` 並保留舊 rows；合法空結果與失敗不得共用輸出。
6. 來源游標依原始 provider rows 判斷，不能因無 MD5 row 被過濾後提前停止後續頁。
7. 保留 AbortController + generation 競態修補；舊分類或舊分頁回應不得覆蓋目前書卡、徽章或 tooltip。

## 最終驗證

- VANS Round 2：CLEARED；三項 finding（失敗誤報 fresh、過濾後短頁提前終止、狀態總數非持久化全集）均關閉。
- 聚焦測試：15 passed；完整測試：435 passed、27 skipped；缺席測試控制組 rc=4；`git diff --check` rc=0。
- 線上 `cat_880` 首次請求：`total=2`、`status=never_refreshed`、`refresh_scheduled=true`，證明不等待外網。
- 12 秒後：本地＋遠端聯集 `total=27`、本頁 `items=20`、遠端 `accumulated_total=25`、`status=fresh`。
- SQLite：`remote_catalog_item=25`、`cat_880` 遠端 distinct=25、`PRAGMA integrity_check=ok`；不存在分類控制組 HTTP 404。
- 真瀏覽器 pixels 與跨 daemon restart：UNVERIFIED；未冒充已驗。持久性由現役 SQLite rows 與 reopen 測試覆蓋。
- Architecture Sync: `docs/ARCHITECTURE.md` 3.10 已更新為 `PersistentRemoteCatalog`。
