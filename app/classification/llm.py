"""OpenAI-compatible 模型分類器。

設計約束（全部來自 design.md，且每一條都有對應測試）：
- 只在規則層零命中或衝突時被呼叫。
- 送出的內容只有 taxonomy + title + authors + language + work_type，不送全文。
- 回應必須是單一 JSON object，欄位經嚴格驗證；任何一項不合即**拒絕整份結果**，
  不做部分採用——部分採用會讓「模型答對一半」與「模型亂答」共用同一個輸出。
- 任何失敗（未設定、逾時、429、5xx、非 JSON、驗證不過）都回 ERROR/DISABLED，
  絕不回任何預設分類。
- API key 只從環境變數讀，不進 DB、不進 log、不進回傳值。
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx

from app.classification.result import (
    ClassificationOutcome,
    ClassificationSource,
    ClassificationState,
)
from app.classification.taxonomy import all_category_ids, leaf_category_ids, taxonomy_catalog

log = logging.getLogger(__name__)

# prompt 內容一改就要動這個版本號：持久化它才能回答「這批分類是用哪版 prompt 產的」。
PROMPT_VERSION = "v1"

MAX_CATEGORIES = 2

ENV_BASE_URL = "OPENSHELF_CLASSIFIER_BASE_URL"
ENV_API_KEY = "OPENSHELF_CLASSIFIER_API_KEY"
ENV_MODEL = "OPENSHELF_CLASSIFIER_MODEL"
ENV_TIMEOUT = "OPENSHELF_CLASSIFIER_TIMEOUT_SECONDS"

DEFAULT_TIMEOUT_SECONDS = 20.0

_SYSTEM_PROMPT = (
    "You are a library cataloguing assistant. Classify the given book into the "
    "provided taxonomy. You MUST only choose category ids that appear in the "
    "taxonomy list. Reply with a single JSON object and nothing else, in the form "
    '{"category_ids": ["cat_xxx"], "confidence": 0.0-1.0, "reason": "short reason"}. '
    f"Use at most {MAX_CATEGORIES} category ids. "
    'If you genuinely cannot determine the category, reply {"category_ids": [], '
    '"confidence": 0.0, "reason": "undetermined"} rather than guessing.'
)


class ClassifierConfig:
    """從環境變數讀取的模型設定。

    `is_enabled` 為 False 代表「未設定」，是一個明確的狀態，不是失敗——兩者
    的處置不同（未設定＝不必重試；失敗＝可重試），故不得共用同一個輸出。
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ):
        self.base_url = (base_url or "").strip().rstrip("/")
        self._api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.timeout_seconds = timeout_seconds or DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "ClassifierConfig":
        env = env if env is not None else os.environ
        raw_timeout = (env.get(ENV_TIMEOUT) or "").strip()
        timeout: Optional[float]
        try:
            timeout = float(raw_timeout) if raw_timeout else None
        except ValueError:
            # 設定值壞掉與未設定不同：前者是使用者輸入錯誤，要出聲。
            log.warning("%s 不是合法數字（%r），改用預設 %.1fs",
                        ENV_TIMEOUT, raw_timeout, DEFAULT_TIMEOUT_SECONDS)
            timeout = None
        return cls(
            base_url=env.get(ENV_BASE_URL),
            api_key=env.get(ENV_API_KEY),
            model=env.get(ENV_MODEL),
            timeout_seconds=timeout,
        )

    @property
    def is_enabled(self) -> bool:
        return bool(self.base_url and self._api_key and self.model)

    @property
    def missing_fields(self) -> List[str]:
        missing = []
        if not self.base_url:
            missing.append(ENV_BASE_URL)
        if not self._api_key:
            missing.append(ENV_API_KEY)
        if not self.model:
            missing.append(ENV_MODEL)
        return missing

    def auth_header(self) -> Dict[str, str]:
        """只在真正發請求的當下取出 key；不提供任何 repr/序列化路徑。"""
        return {"Authorization": f"Bearer {self._api_key}"}

    def __repr__(self) -> str:  # pragma: no cover - 只為避免意外印出 key
        return (
            f"ClassifierConfig(base_url={self.base_url!r}, model={self.model!r}, "
            f"api_key=<redacted len={len(self._api_key)}>)"
        )


class ClassificationRejected(Exception):
    """模型輸出未通過驗證。訊息即為拒絕原因，會寫進 outcome.error。"""


def build_user_prompt(
    title: str,
    authors: Optional[str],
    language: Optional[str],
    work_type: Optional[str],
) -> str:
    lines = ["Taxonomy (choose ONLY from these ids):"]
    for entry in taxonomy_catalog():
        lines.append(f"- {entry['id']}: {entry['name']} (under {entry['parent_name']})")
    lines.append("")
    lines.append("Book:")
    lines.append(f"- title: {title}")
    lines.append(f"- authors: {authors or 'unknown'}")
    lines.append(f"- language: {language or 'unknown'}")
    lines.append(f"- work_type: {work_type or 'book'}")
    return "\n".join(lines)


_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """容忍 ```json 包裹，但不容忍任何其他偏離。

    這是**格式**容忍不是**語義**容忍：去掉圍欄後仍必須是完整合法 JSON object，
    不做「從一段散文中撈出第一個 {...}」那種寬鬆解析——寬鬆解析會把模型的
    閒聊當成答案。
    """
    stripped = text.strip()
    m = _FENCE_RE.match(stripped)
    return m.group(1).strip() if m else stripped


def parse_and_validate(raw_text: str) -> ClassificationOutcome:
    """把模型的原始文字轉成 outcome，或以 ClassificationRejected 拒絕。"""
    body = _strip_code_fence(raw_text or "")
    if not body:
        raise ClassificationRejected("empty model response")

    try:
        data = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise ClassificationRejected(f"not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ClassificationRejected(f"expected JSON object, got {type(data).__name__}")

    if "category_ids" not in data:
        raise ClassificationRejected("missing field: category_ids")
    ids = data["category_ids"]
    if not isinstance(ids, list):
        raise ClassificationRejected(f"category_ids must be a list, got {type(ids).__name__}")
    if any(not isinstance(i, str) for i in ids):
        raise ClassificationRejected("category_ids must contain only strings")

    confidence = data.get("confidence")
    if confidence is None:
        raise ClassificationRejected("missing field: confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ClassificationRejected(
            f"confidence must be a number, got {type(confidence).__name__}"
        )
    confidence = float(confidence)
    if not (0.0 <= confidence <= 1.0):
        raise ClassificationRejected(f"confidence out of range [0,1]: {confidence}")

    reason = data.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ClassificationRejected(f"reason must be a string, got {type(reason).__name__}")

    # 模型明確表示判不出：這是一個**有效**回應，狀態為 unclassified 而非 error。
    # 兩者可分辨才知道要不要重試。
    if not ids:
        return ClassificationOutcome(
            state=ClassificationState.UNCLASSIFIED,
            confidence=confidence,
            prompt_version=PROMPT_VERSION,
            reason=reason or "model reported undetermined",
        )

    if len(ids) > MAX_CATEGORIES:
        raise ClassificationRejected(
            f"too many category_ids: {len(ids)} > {MAX_CATEGORIES}"
        )

    deduped = list(dict.fromkeys(ids))
    if len(deduped) != len(ids):
        raise ClassificationRejected(f"duplicate category_ids: {ids}")

    known = all_category_ids()
    leaves = leaf_category_ids()
    for cid in deduped:
        if cid not in known:
            raise ClassificationRejected(f"unknown category id: {cid}")
        if cid not in leaves:
            # 存在但非葉節點：與「不存在」是不同的錯誤，訊息要能分辨。
            raise ClassificationRejected(f"category id is not a leaf node: {cid}")

    return ClassificationOutcome(
        state=ClassificationState.CLASSIFIED,
        category_ids=deduped,
        source=ClassificationSource.LLM,
        confidence=confidence,
        prompt_version=PROMPT_VERSION,
        reason=reason,
    )


def _extract_message_content(payload: Any) -> str:
    """從 OpenAI-compatible chat/completions 回應中取出訊息內容。

    形狀不符即拒絕；不做任何「猜猜看哪個欄位像內容」的搜尋。
    """
    if not isinstance(payload, dict):
        raise ClassificationRejected(
            f"response body is not a JSON object: {type(payload).__name__}"
        )
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ClassificationRejected("response has no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise ClassificationRejected("choices[0].message missing or malformed")
    content = message.get("content")
    if not isinstance(content, str):
        raise ClassificationRejected(
            f"choices[0].message.content is not a string: {type(content).__name__}"
        )
    return content


class LLMClassifier:
    """透過 OpenAI-compatible endpoint 分類。transport 可注入以供測試。"""

    def __init__(
        self,
        config: Optional[ClassifierConfig] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.config = config or ClassifierConfig.from_env()
        self._transport = transport

    def classify(
        self,
        title: str,
        authors: Optional[str] = None,
        language: Optional[str] = None,
        work_type: Optional[str] = None,
    ) -> ClassificationOutcome:
        if not self.config.is_enabled:
            return ClassificationOutcome(
                state=ClassificationState.DISABLED,
                error="classifier not configured: missing "
                      + ", ".join(self.config.missing_fields),
            )

        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_user_prompt(title, authors, language, work_type),
                },
            ],
            "temperature": 0,
        }
        headers = {"Content-Type": "application/json"}
        headers.update(self.config.auth_header())
        url = f"{self.config.base_url}/chat/completions"

        try:
            with httpx.Client(
                transport=self._transport, timeout=self.config.timeout_seconds
            ) as client:
                resp = client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            return self._error(f"timeout after {self.config.timeout_seconds}s: {type(exc).__name__}")
        except httpx.HTTPError as exc:
            return self._error(f"transport error: {type(exc).__name__}: {exc}")

        if resp.status_code != 200:
            # 狀態碼進 error 訊息，body 不進——body 可能回顯我們送出的 header。
            return self._error(f"http {resp.status_code}")

        try:
            body = resp.json()
        except ValueError as exc:
            return self._error(f"response is not JSON: {exc}")

        try:
            content = _extract_message_content(body)
            outcome = parse_and_validate(content)
        except ClassificationRejected as exc:
            return self._error(f"rejected: {exc}")

        if outcome.is_classified:
            # model 名稱在此補上（parse 層不知道是哪個 model 產的）。
            return ClassificationOutcome(
                state=outcome.state,
                category_ids=outcome.category_ids,
                source=outcome.source,
                confidence=outcome.confidence,
                model=self.config.model,
                prompt_version=outcome.prompt_version,
                reason=outcome.reason,
            )
        return ClassificationOutcome(
            state=outcome.state,
            confidence=outcome.confidence,
            model=self.config.model,
            prompt_version=outcome.prompt_version,
            reason=outcome.reason,
        )

    def _error(self, message: str) -> ClassificationOutcome:
        log.warning("分類模型呼叫失敗（維持未分類）：%s", message)
        return ClassificationOutcome(
            state=ClassificationState.ERROR,
            model=self.config.model,
            prompt_version=PROMPT_VERSION,
            error=message,
        )
