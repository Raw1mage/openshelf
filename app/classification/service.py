"""ClassificationService：協調規則層與模型層，並把結果交給 DAO 原子寫入。

呼叫時機的關鍵決策（推翻了 design.md 的一個假設，理由見 module 尾註）：
`classify_and_persist` 是**同步且會發網路請求**的，故它不在任何請求路徑上被
呼叫。ingest 只跑 `classify_rules_only`（純本地、零網路），模型層由可重跑的
backfill/drain 命令執行。
"""

import logging
from typing import Optional

from app.classification.llm import LLMClassifier
from app.classification.result import ClassificationOutcome, ClassificationState
from app.classification.rules import RuleClassifier

log = logging.getLogger(__name__)


class ClassificationService:
    def __init__(
        self,
        dao=None,
        rule_classifier: Optional[RuleClassifier] = None,
        llm_classifier: Optional[LLMClassifier] = None,
    ):
        self.dao = dao
        self.rules = rule_classifier or RuleClassifier()
        self._llm = llm_classifier
        self._llm_explicit = llm_classifier is not None

    @property
    def llm(self) -> LLMClassifier:
        """延後建立：未設定環境變數時也不該在 import/建構期就炸。"""
        if self._llm is None:
            self._llm = LLMClassifier()
        return self._llm

    def classify_rules_only(
        self, title: str, authors: Optional[str] = None
    ) -> ClassificationOutcome:
        """零網路的規則層。ingest 路徑只走這條。"""
        return self.rules.classify(title, authors)

    def classify(
        self,
        title: str,
        authors: Optional[str] = None,
        language: Optional[str] = None,
        work_type: Optional[str] = None,
    ) -> ClassificationOutcome:
        """完整兩階段分類（會發網路請求）。"""
        rule_outcome = self.rules.classify(title, authors)
        if rule_outcome.is_classified:
            return rule_outcome
        return self.llm.classify(
            title=title, authors=authors, language=language, work_type=work_type
        )

    def classify_and_persist(self, work_id: str, dao=None) -> ClassificationOutcome:
        """對單一 Work 跑完整分類並寫入。回傳實際採用的 outcome。

        逐本獨立：任何一本拋出的例外由呼叫端（backfill）攔截並記錄，不會讓
        整批中斷——但這個方法本身不吞例外，因為「這本失敗了」與「這本被判為
        未分類」必須可分辨。
        """
        dao = dao or self.dao
        if dao is None:
            raise ValueError("classify_and_persist 需要 dao")

        work = dao.get_work_classification_input(work_id)
        if work is None:
            raise KeyError(f"work not found: {work_id}")

        outcome = self.classify(
            title=work["title"],
            authors=work["authors_display"],
            language=work["language"],
            work_type=work["work_type"],
        )
        dao.apply_classification(work_id, outcome)
        return outcome

    def classify_new_work(self, work_id: str, title: str, authors: Optional[str], dao=None) -> ClassificationOutcome:
        """入庫路徑專用：只跑規則層並寫入，永不發網路請求。

        規則零命中時寫入 `pending` 狀態——那是一個**可被查詢的待辦**，而不是
        「安靜地沒有分類」。backfill/drain 命令即以此狀態為工作佇列。
        """
        dao = dao or self.dao
        outcome = self.classify_rules_only(title, authors)
        if dao is not None:
            dao.apply_classification(work_id, outcome)
        return outcome


# --- 為何模型呼叫不放在 ingest 路徑上（推翻 design.md「入庫後同步呼叫也不阻斷」）---
#
# design.md §Execution 寫「新書：入庫完成後排入分類工作；不阻斷檔案保存與索引」，
# 派工單則問「同步呼叫模型是否真的不阻斷」。實測 repo 的兩條 ingest 路徑：
#
#   1. `app/api/routes.py:169` — `run_in_threadpool(pipeline.ingest_bytes, ...)`
#      走 **FastAPI/anyio 預設 threadpool**，實測上限 40 token，且那 40 個同時是
#      **每一個 sync 相依項與 sync 路由**在用的（見 download_worker.py:25-38 已記錄
#      的同一件事）。在此加一個 timeout 20s 的遠端呼叫，等於每本上傳佔住一格
#      最多 20 秒。
#
#   2. `app/crawler/download_worker.py:1086` — `_run_file_io(pipeline.process_file, ...)`
#      走專用 `anyio.CapacityLimiter(4)`。塞進遠端呼叫後，**4 本併發下載即可
#      耗盡整個下載子系統的 I/O 頃額**，症狀是下載全部停住。
#
# 「不阻斷」在這裡不是「不阻斷該書自己入庫」，而是「不阻斷別人」——而兩條路徑
# 都是共用池。所以不論放在 ingest 之後的哪一行，遠端 latency 都會外溢。
#
# 不自造 queue / 不 setTimeout / 不 polling（AGENTS.md 天條）：改用 repo 已有的
# **持久化狀態當佇列**。`work.classification_state = 'pending'` 就是待辦清單，
# 由 `script/backfill_classification.py` 這個可重跑、可 dry-run 的命令消化。
# 這與 download_job 用 DB status 當佇列是同一個既有形狀，沒有新機制。
