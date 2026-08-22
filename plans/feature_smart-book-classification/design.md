# Design: 智慧圖書分類

## Architecture

採兩階段 classifier：

1. `RuleClassifier`：以邊界安全的關鍵字／詞元規則產生候選；只有單一高信心葉節點時直接採用。
2. `LLMClassifier`：規則零命中或衝突時，以 OpenAI-compatible chat/completions endpoint 判斷。

`ClassificationService` 將本地規則與遠端模型分成兩個執行階段。入庫路徑只跑零網路規則層：單一高信心葉節點直接寫入，零命中或衝突則標記為 `pending`。可重跑的回填命令才呼叫遠端模型，避免佔住共用 threadpool 或下載 I/O limiter；結果經 taxonomy allowlist 驗證後，由 DAO 以短 transaction 原子替換該 Work 的自動分類。

## Configuration

- `OPENSHELF_CLASSIFIER_BASE_URL`
- `OPENSHELF_CLASSIFIER_API_KEY`
- `OPENSHELF_CLASSIFIER_MODEL`
- `OPENSHELF_CLASSIFIER_TIMEOUT_SECONDS`

缺少前三項代表模型分類未啟用；此時低信心 Work 保持未分類並記錄明確狀態，不使用 fallback。

## Model Contract

輸入只含 taxonomy、title、authors、language、work_type；不傳全文。輸出為單一 JSON object：

```json
{"category_ids":["cat_471"],"confidence":0.94,"reason":"Operating systems textbook"}
```

約束：ID 必須存在、只允許葉節點、最多兩個葉節點、confidence 介於 0–1。解析／驗證失敗即拒絕整份結果。

## Persistence

擴充 `work_category`：`source`（`rule|llm|legacy|manual`）、`model`、`prompt_version`。Work 以 `pending|classified|unclassified|error|disabled` 區分未執行、有效判定、遠端失敗與未設定；不可把這些狀態合併。人工來源不會被自動回填覆寫。

現有資料全部由 `app/db/dao.py` 的 bootstrap/create_work 自動產生，可先標為 `legacy`。回填逐本成功後才替換該 Work 的 legacy rows；模型失敗則保留原資料供重試，但書攤查詢不得把已知錯誤的 legacy fallback 當成可信分類。

## Execution

- 新書：入庫完成後只跑本地規則；低信心項目標為 `pending`，不阻斷檔案保存與索引。
- 回填：可重跑的批次命令逐本呼叫模型並持久化；支援唯讀 dry-run、schema 驗證與統計輸出。
- 觀測：`classified|unclassified` 視為有效判定；`error|disabled` 保留狀態供重試並讓 CLI 回非零。記錄來源、模型與 prompt 版本，不記 API key。

## Failure Policy

- 網路、429、5xx、逾時、非法 JSON：Work 保持未分類／待重試，發 warning。
- 不新增跨 provider 或預設分類 fallback。
- API key 僅由環境變數讀取，不入 DB、log 或 event。
