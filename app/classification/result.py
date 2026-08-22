"""分類結果的值物件與狀態列舉。

刻意不用 `Optional[List[str]]` 之類的裸型別表達結果：
「未執行」「執行了但判不出」「執行失敗」三者必須互斥可辨——把它們壓成
「category_ids 是空的」會讓三種完全不同的處置（等待排程 / 接受未分類 / 重試）
共用同一個輸出。
"""

from dataclasses import dataclass, field
from typing import List, Optional


class ClassificationState:
    """Work 的分類可判定狀態（寫入 `work.classification_state`）。"""

    PENDING = "pending"            # 尚未跑過模型層（規則零命中，等待排程）
    CLASSIFIED = "classified"      # 已有可信分類
    UNCLASSIFIED = "unclassified"  # 已判定，但確實判不出（模型明確表示無法歸類）
    ERROR = "error"                # 執行失敗（網路/逾時/非法輸出），可重試
    DISABLED = "disabled"          # 模型分類未設定，無法進一步判定

    ALL = (PENDING, CLASSIFIED, UNCLASSIFIED, ERROR, DISABLED)


class ClassificationSource:
    """單一 work_category 關聯的來源（寫入 `work_category.source`）。

    `AUTOMATIC` 與 `MANUAL` 的分界是一條**寫入權限**界線，不只是標籤：
    自動分類器只能刪改自己產生的列（AUTOMATIC），使用者手動指定的列
    （MANUAL）任何自動路徑都不得覆寫。把兩者混在同一個集合裡，回填一次
    就會靜默吃掉使用者的人工修正，而且沒有任何輸出會顯示這件事發生過。
    """

    RULE = "rule"
    LLM = "llm"
    LEGACY = "legacy"
    MANUAL = "manual"

    # 自動路徑可刪改的來源。回填/重跑只動這一組。
    AUTOMATIC = (RULE, LLM, LEGACY)

    ALL = (RULE, LLM, LEGACY, MANUAL)


@dataclass(frozen=True)
class ClassificationOutcome:
    """一次分類嘗試的完整結果。

    `category_ids` 僅在 `state == CLASSIFIED` 時才有意義且必非空；其餘狀態下
    一律為空 list——這是不變量，由 `__post_init__` 強制，避免呼叫端在 error
    狀態下讀到半套結果就寫進 DB。
    """

    state: str
    category_ids: List[str] = field(default_factory=list)
    source: Optional[str] = None
    confidence: Optional[float] = None
    model: Optional[str] = None
    prompt_version: Optional[str] = None
    reason: Optional[str] = None
    error: Optional[str] = None

    def __post_init__(self) -> None:
        if self.state not in ClassificationState.ALL:
            raise ValueError(f"未知的 classification state: {self.state}")
        if self.state == ClassificationState.CLASSIFIED:
            if not self.category_ids:
                raise ValueError("state=classified 必須帶至少一個 category_id")
            if self.source not in ClassificationSource.ALL:
                raise ValueError(f"state=classified 必須帶合法 source，得到 {self.source!r}")
        else:
            if self.category_ids:
                raise ValueError(
                    f"state={self.state} 不得帶 category_ids（得到 {self.category_ids!r}）；"
                    "非 classified 狀態一律維持未分類"
                )

    @property
    def is_classified(self) -> bool:
        return self.state == ClassificationState.CLASSIFIED
