# Tasks: feature_smart-book-classification

## Tasks

### Phase 1: 分類契約與資料來源
- [x] Task 1.1: 重構規則分類器，移除 `cat_800 + cat_850` fallback 並修正短字串誤命中。
- [x] Task 1.2: 擴充 schema/DAO，保存分類來源、模型、prompt 版本與可判定狀態。

### Phase 2: OpenAI-compatible 智慧分類
- [x] Task 2.1: 實作可注入的 `LLMClassifier` 與嚴格 JSON/taxonomy 驗證。
- [x] Task 2.2: 入庫只執行零網路規則層；低信心 Work 標為可重試狀態，由批次命令呼叫模型。

### Phase 3: 既有資料回填
- [x] Task 3.1: 建立可重跑、可 dry-run 的 42 本 legacy 分類回填流程。
- [~] Task 3.2: 程式與隔離資料庫已驗證錯誤 legacy 分類不可見且可回填；現役 42 本尚未 dry-run／apply。

### Phase 4: 驗證與文件
- [x] Task 4.1: 加入規則、LLM response、失敗態、回填與 mutation 測試。
- [~] Task 4.2: 全套件 422 passed / 27 skipped 與 CLI 控制組已通過；現役 API／資料庫回填控制待部署階段執行。
- [x] Task 4.3: 同步 `docs/ARCHITECTURE.md` 與 event log。
