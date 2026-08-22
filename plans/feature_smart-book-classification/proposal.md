# Proposal: feature_smart-book-classification

## Why

現行分類器只比對少量標題／作者關鍵字；零命中時硬塞入 `cat_800` 與 `cat_850`，造成所有未知書籍被冒充為「華文經典小說」。線上 `cat_850` 的 13 筆目前全是作業系統或電腦架構書。

## What Changes

1. 保留高精確度規則作為零成本第一層，但移除錯誤 fallback。
2. 規則無命中或多類衝突時，呼叫 OpenAI-compatible API 做結構化分類。
3. 模型只可從現有 taxonomy 選擇分類，回傳 category IDs、confidence 與短理由；非法輸出 fail fast。
4. API 未設定、逾時或回應無效時維持「未分類」，不得改用預設分類。
5. 持久化分類來源、模型與 prompt 版本，支援稽核與重跑。
6. 對現有 42 本書做一次性回填；現有 `work_category` 只有兩個自動寫入點，無手動寫入路徑。

## Boundaries

- IN: `app/db/categories.py`、分類服務、DAO/schema migration、匯入後分類、回填命令、測試、架構文件。
- OUT: 前端版面重設、下載器、搜尋來源、任何 silent fallback、新套件（除非另行批准）。

## Success Criteria

- Operating System 與 Computer Architecture 不再進入 `cat_850`。
- 真正無法判定的書籍保持未分類並有可觀測狀態。
- 模型失效不阻斷書籍入庫。
- 新書分類與一次性回填皆有測試及可重跑性。
