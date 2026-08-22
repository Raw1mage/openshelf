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
- [x] Task 3.2: 現役資料完成備份與回填；42 個 pending 全部持久化，連同新增作品共 43 本皆為 classified。

### Phase 4: 驗證與文件
- [x] Task 4.1: 加入規則、LLM response、失敗態、回填與 mutation 測試。
- [x] Task 4.2: 全套件 422 passed / 27 skipped；現役回填後第二次 dry-run 為 0 candidates、DB byte-identical，OS／電腦架構書在 cat_850 命中 0。
- [x] Task 4.3: 同步 `docs/ARCHITECTURE.md` 與 event log。
