"""智慧圖書分類（規則層 + 模型層）。

分層原則：
- `rules`：零成本、高精確度、可離線；只在**單一葉節點**命中時直接採用。
- `llm`：規則零命中或多類衝突時才呼叫；輸出經 taxonomy 驗證，失敗即拒絕整份結果。
- `service`：協調兩層並把結果交給 DAO 原子寫入；任何失敗都維持「未分類」，
  絕不退回任何預設分類。
"""

from app.classification.result import (
    ClassificationOutcome,
    ClassificationState,
    ClassificationSource,
)

__all__ = [
    "ClassificationOutcome",
    "ClassificationState",
    "ClassificationSource",
]
