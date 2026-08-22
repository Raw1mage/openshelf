# BR-20260823_013000 — 分類徽章只計本地藏書，書架卻混入雲端推薦

- **Status**: CLOSED
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

因此不是 DAO 計數、父子 scope、分類回填或前端快取錯誤，而是本地分類集合與雲端推薦集合被預設混成同一個書架。

## 修復契約

1. 預設分類書架只顯示本地分類藏書，使徽章、總數與書卡一致。
2. 雲端探索改為使用者明確觸發，且與本地藏書分區呈現。
3. API 的 `include_cloud` 預設為 `false`；顯式 `true` 仍保留既有雲端探索能力。
4. 補測試證明預設路徑不呼叫 crawler；顯式雲端路徑才加入 remote items。
5. 防止快速切換分類時舊請求覆蓋新分類畫面。

## 驗證

- 聚焦測試：`tests/test_categories.py` + `tests/test_bookstall_race.py`，4 passed。
- 缺席控制組：不存在測試路徑 rc=4，證明 pytest 指令具鑑別力。
- 完整測試：424 passed、27 skipped。
- 線上 `cat_880`：`category.works_count=2`、`total=2`、`items=2`；不存在分類控制組 HTTP 404。
- 快速切換競態：分類 A→B 與同分類本地→雲端兩組延遲回應測試均進入斷言。
- JavaScript 語法與 diff：`node --check`、`git diff --check` 均 rc=0。
- 真瀏覽器互動：UNVERIFIED；Chromium 可啟動，但本機缺少可用 CDP WebSocket client。未將此缺口包裝成通過。
- Architecture Sync: 已更新 `docs/ARCHITECTURE.md` 3.10，明定本地分類藏書與按需雲端推薦為獨立檢視。
