"""智慧圖書分類：規則層、模型層、持久化、回填的完整測試。

不做任何真實網路呼叫：所有模型互動走 `httpx.MockTransport`，契約由注入的
handler 決定。API key 只出現在測試自建的 env dict 中，不進 DB、不進 fixture 檔。
"""

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from app.classification.llm import (
    PROMPT_VERSION,
    ClassificationRejected,
    ClassifierConfig,
    LLMClassifier,
    build_user_prompt,
    parse_and_validate,
)
from app.classification.result import (
    ClassificationOutcome,
    ClassificationSource,
    ClassificationState,
)
from app.classification.rules import CATEGORY_KEYWORDS, RuleClassifier, match_rule_categories
from app.classification.service import ClassificationService
from app.classification.taxonomy import all_category_ids, leaf_category_ids, parent_of
from app.db.dao import CatalogDAO
from app.db.engine import DatabaseEngine
from app.models.catalog import WorkCreate


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def dao(tmp_path):
    engine = DatabaseEngine(db_path=tmp_path / "cls.db")
    engine.init_database()
    return CatalogDAO(engine=engine)


ENABLED_ENV = {
    "OPENSHELF_CLASSIFIER_BASE_URL": "https://example.invalid/v1",
    "OPENSHELF_CLASSIFIER_API_KEY": "sk-test-not-a-real-key",
    "OPENSHELF_CLASSIFIER_MODEL": "test-model",
}


def make_llm(handler, env=None, timeout=None):
    cfg_env = dict(env if env is not None else ENABLED_ENV)
    if timeout is not None:
        cfg_env["OPENSHELF_CLASSIFIER_TIMEOUT_SECONDS"] = str(timeout)
    config = ClassifierConfig.from_env(cfg_env)
    return LLMClassifier(config=config, transport=httpx.MockTransport(handler))


def chat_response(content, status=200):
    def handler(request):
        return httpx.Response(
            status, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
        )
    return handler


# --------------------------------------------------------------------------
# Task 1.1 — 規則層：移除 fallback、詞元邊界
# --------------------------------------------------------------------------

def test_zero_hit_never_falls_back_to_any_category():
    """零命中就是零命中。舊版在此塞 cat_800+cat_850。"""
    outcome = RuleClassifier().classify("Zzyzx Quorbleflux Handbook", "Nobody")
    assert outcome.state == ClassificationState.PENDING
    assert outcome.category_ids == []


@pytest.mark.parametrize("title", [
    "Operating System Concepts",
    "Modern Operating Systems",
    "Computer Architecture: A Quantitative Approach",
    "Structured Computer Organization",
])
def test_os_and_architecture_never_land_in_cat_850(title):
    """線上 cat_850 的 13 筆全是這類書；它們必須離開華文經典小說。"""
    matched = match_rule_categories(title, None)
    assert "cat_850" not in matched
    assert "cat_800" not in matched
    outcome = RuleClassifier().classify(title, None)
    assert "cat_850" not in outcome.category_ids


def test_real_chinese_classic_fiction_is_positively_matched():
    """正向對照：真正的華文經典小說仍要進 cat_850。

    這是 test_os_and_architecture_never_land_in_cat_850 的控制組——沒有它，
    「把 cat_850 整個拿掉」也會讓那條測試通過。
    """
    outcome = RuleClassifier().classify("紅樓夢", "曹雪芹")
    assert outcome.state == ClassificationState.CLASSIFIED
    assert outcome.category_ids == ["cat_850"]


def test_short_ascii_keyword_respects_token_boundary():
    """'ai' 不得命中 Gaines/Maine/said；但獨立詞 'AI' 仍要命中。"""
    for negative in ["The Life of Ernest Gaines", "A History of Maine", "He said so"]:
        assert "cat_472" not in match_rule_categories(negative, None), negative
    assert "cat_472" in match_rule_categories("AI for Everyone", None)


@pytest.mark.parametrize("title", ["Handcuffs and Chains", "The Grandchild", "Windcheater Design"])
def test_short_ascii_keyword_dc_does_not_match_inside_words(title):
    """'dc' 裸 substring 時會在 Han-dc-uffs / Gran-dc-hild / Win-dc-heater 裡命中。

    這三個字是實測過確實含 'dc' 子字串的（不是 'Introduction'——那裡是
    'duc' 不是 'dc'，拿它當負向案例會是一條空洞斷言）。
    """
    assert "dc" in title.lower()          # 控制組：確認這個案例真的能觸發誤命中
    assert "cat_092" not in match_rule_categories(title, None)


def test_dc_as_standalone_token_still_matches():
    """控制組：把 'dc' 從關鍵字表刪掉也能讓上面那條通過，這條不行。"""
    assert "cat_092" in match_rule_categories("DC Comics Encyclopedia", None)


def test_boundary_pattern_handles_non_word_trailing_chars():
    """c++ 的結尾是非 word 字元，\\b 在那裡不成立；lookaround 才行。"""
    assert "cat_471" in match_rule_categories("Effective C++", "Meyers")
    assert "cat_473" in match_rule_categories("k8s in Action", None)


def test_cjk_keyword_still_uses_substring():
    """中文無空白分詞，必須維持 substring 比對。"""
    assert "cat_310" in match_rule_categories("寫給工程師的微積分入門", None)


def test_single_hit_classifies_multi_hit_defers_to_model():
    single = RuleClassifier().classify("Deep Learning with PyTorch", None)
    assert single.state == ClassificationState.CLASSIFIED
    assert single.category_ids == ["cat_472"]

    multi = RuleClassifier().classify("量子物理與微積分導論", None)
    assert multi.state == ClassificationState.PENDING
    assert multi.category_ids == []
    assert "conflicting" in (multi.reason or "")


def test_rule_conflict_and_zero_hit_have_distinct_reasons():
    """兩者都交給模型，但原因必須可分辨。"""
    zero = RuleClassifier().classify("Zzyzx Quorbleflux", None)
    conflict = RuleClassifier().classify("量子物理與微積分導論", None)
    assert zero.reason != conflict.reason
    assert "no keyword hit" in zero.reason


def test_keyword_table_points_only_at_leaf_nodes():
    leaves = leaf_category_ids()
    assert set(CATEGORY_KEYWORDS) <= leaves


def test_legacy_fallback_classifier_is_gone():
    """舊 infer_categories_for_work 必須完全不可 import。

    保留一份 deprecated 副本等於保留一條讓 fallback 復活的路徑。
    """
    import app.db.categories as legacy
    assert not hasattr(legacy, "infer_categories_for_work")
    assert not hasattr(legacy, "CATEGORY_KEYWORDS")


# --------------------------------------------------------------------------
# Task 2.1 — 模型輸出驗證
# --------------------------------------------------------------------------

def test_valid_single_leaf_response_accepted():
    outcome = parse_and_validate('{"category_ids":["cat_471"],"confidence":0.94,"reason":"prog"}')
    assert outcome.state == ClassificationState.CLASSIFIED
    assert outcome.category_ids == ["cat_471"]
    assert outcome.confidence == pytest.approx(0.94)
    assert outcome.source == ClassificationSource.LLM
    assert outcome.prompt_version == PROMPT_VERSION


def test_code_fence_is_tolerated_but_prose_is_not():
    ok = parse_and_validate('```json\n{"category_ids":["cat_471"],"confidence":0.9}\n```')
    assert ok.category_ids == ["cat_471"]
    with pytest.raises(ClassificationRejected):
        parse_and_validate('Sure! Here you go: {"category_ids":["cat_471"],"confidence":0.9}')


@pytest.mark.parametrize("payload,needle", [
    ('{"category_ids":["cat_999"],"confidence":0.9}', "unknown category id"),
    ('{"category_ids":["cat_400"],"confidence":0.9}', "not a leaf node"),
    ('{"category_ids":["cat_471","cat_472","cat_473"],"confidence":0.9}', "too many"),
    ('{"category_ids":["cat_471","cat_471"],"confidence":0.9}', "duplicate"),
    ('{"category_ids":["cat_471"],"confidence":1.5}', "out of range"),
    ('{"category_ids":["cat_471"],"confidence":-0.1}', "out of range"),
    ('{"category_ids":["cat_471"]}', "missing field: confidence"),
    ('{"confidence":0.9}', "missing field: category_ids"),
    ('{"category_ids":"cat_471","confidence":0.9}', "must be a list"),
    ('{"category_ids":[471],"confidence":0.9}', "only strings"),
    ('{"category_ids":["cat_471"],"confidence":"high"}', "must be a number"),
    ('{"category_ids":["cat_471"],"confidence":true}', "must be a number"),
    ('["cat_471"]', "expected JSON object"),
    ('not json at all', "not valid JSON"),
    ('', "empty model response"),
])
def test_invalid_model_output_is_rejected(payload, needle):
    with pytest.raises(ClassificationRejected) as exc:
        parse_and_validate(payload)
    assert needle in str(exc.value)


def test_unknown_id_and_non_leaf_id_give_distinct_reasons():
    """『不存在』與『存在但非葉節點』是不同的失敗，訊息必須可分辨。"""
    with pytest.raises(ClassificationRejected) as e1:
        parse_and_validate('{"category_ids":["cat_999"],"confidence":0.9}')
    with pytest.raises(ClassificationRejected) as e2:
        parse_and_validate('{"category_ids":["cat_400"],"confidence":0.9}')
    assert str(e1.value) != str(e2.value)
    assert "cat_400" in all_category_ids()          # 確實存在
    assert "cat_400" not in leaf_category_ids()     # 但不是葉節點


def test_empty_category_ids_means_unclassified_not_error():
    """模型明確說判不出：這是有效回應，不該當成錯誤重試。"""
    outcome = parse_and_validate('{"category_ids":[],"confidence":0.0,"reason":"undetermined"}')
    assert outcome.state == ClassificationState.UNCLASSIFIED
    assert outcome.category_ids == []


def test_prompt_never_contains_full_text_and_only_lists_leaves():
    prompt = build_user_prompt("Some Book", "Some Author", "en", "book")
    assert "Some Book" in prompt and "Some Author" in prompt
    leaves = leaf_category_ids()
    for cid in all_category_ids():
        if cid in leaves:
            assert cid in prompt, cid
        else:
            assert cid not in prompt, f"父節點 {cid} 不該出現在 prompt"


# --------------------------------------------------------------------------
# Task 2.1 — 傳輸層失敗態
# --------------------------------------------------------------------------

def test_api_not_configured_yields_disabled_not_a_default_category():
    llm = LLMClassifier(config=ClassifierConfig.from_env({}), transport=httpx.MockTransport(
        lambda r: pytest.fail("未設定時不得發出任何請求")
    ))
    outcome = llm.classify("Anything", None)
    assert outcome.state == ClassificationState.DISABLED
    assert outcome.category_ids == []
    assert "OPENSHELF_CLASSIFIER_BASE_URL" in outcome.error


@pytest.mark.parametrize("missing", list(ENABLED_ENV))
def test_partial_configuration_is_disabled(missing):
    env = {k: v for k, v in ENABLED_ENV.items() if k != missing}
    llm = LLMClassifier(config=ClassifierConfig.from_env(env))
    assert llm.classify("X", None).state == ClassificationState.DISABLED


@pytest.mark.parametrize("status", [429, 500, 502, 503, 400, 401])
def test_http_error_status_yields_error_never_a_category(status):
    def handler(request):
        return httpx.Response(status, json={"error": "boom"})
    outcome = make_llm(handler).classify("Some Book", None)
    assert outcome.state == ClassificationState.ERROR
    assert outcome.category_ids == []
    assert str(status) in outcome.error


def test_timeout_yields_error_never_a_category():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)
    outcome = make_llm(handler, timeout=0.5).classify("Some Book", None)
    assert outcome.state == ClassificationState.ERROR
    assert outcome.category_ids == []
    assert "timeout" in outcome.error


def test_connect_error_yields_error():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)
    outcome = make_llm(handler).classify("Some Book", None)
    assert outcome.state == ClassificationState.ERROR
    assert outcome.category_ids == []


def test_non_json_body_yields_error():
    def handler(request):
        return httpx.Response(200, text="<html>gateway</html>")
    assert make_llm(handler).classify("X", None).state == ClassificationState.ERROR


def test_malformed_envelope_yields_error():
    def handler(request):
        return httpx.Response(200, json={"unexpected": "shape"})
    outcome = make_llm(handler).classify("X", None)
    assert outcome.state == ClassificationState.ERROR
    assert "choices" in outcome.error


def test_api_key_is_sent_but_never_leaks_into_outcome_or_repr():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(500, json={"error": "x"})

    llm = make_llm(handler)
    outcome = llm.classify("X", None)
    assert seen["auth"] == "Bearer sk-test-not-a-real-key"
    blob = json.dumps({
        "repr": repr(llm.config),
        "error": outcome.error,
        "model": outcome.model,
        "reason": outcome.reason,
    }, ensure_ascii=False)
    assert "sk-test-not-a-real-key" not in blob


def test_successful_call_records_model_and_prompt_version():
    outcome = make_llm(chat_response('{"category_ids":["cat_610"],"confidence":0.8}')).classify(
        "A History of Rome", None
    )
    assert outcome.state == ClassificationState.CLASSIFIED
    assert outcome.model == "test-model"
    assert outcome.prompt_version == PROMPT_VERSION


# --------------------------------------------------------------------------
# Task 2.2 — service 分層：規則命中不呼叫模型
# --------------------------------------------------------------------------

def test_rule_hit_does_not_call_model():
    def handler(request):
        pytest.fail("規則已高信心命中，不該呼叫模型")
    service = ClassificationService(llm_classifier=make_llm(handler))
    outcome = service.classify("Deep Learning with PyTorch", None)
    assert outcome.source == ClassificationSource.RULE
    assert outcome.category_ids == ["cat_472"]


def test_zero_hit_calls_model():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"category_ids":["cat_610"],"confidence":0.9}'}}]
        })

    service = ClassificationService(llm_classifier=make_llm(handler))
    outcome = service.classify("Zzyzx Quorbleflux", None)
    assert len(calls) == 1
    assert outcome.source == ClassificationSource.LLM


def test_rule_conflict_calls_model():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"category_ids":["cat_320"],"confidence":0.9}'}}]
        })

    service = ClassificationService(llm_classifier=make_llm(handler))
    outcome = service.classify("量子物理與微積分導論", None)
    assert len(calls) == 1
    assert outcome.category_ids == ["cat_320"]


# --------------------------------------------------------------------------
# Task 1.2 — 持久化：provenance / state / 原子性
# --------------------------------------------------------------------------

def test_new_work_with_rule_hit_is_classified_with_rule_provenance(dao):
    wid = dao.create_work(WorkCreate(title="Effective C++", authors_display="Meyers"))
    rows = dao.get_work_categories_detail(wid)
    ids = {r["category_id"] for r in rows}
    assert "cat_471" in ids
    assert all(r["source"] == ClassificationSource.RULE for r in rows)
    assert dao.get_work_classification_input(wid)["classification_state"] == \
        ClassificationState.CLASSIFIED


def test_new_work_without_rule_hit_is_pending_and_uncategorised(dao):
    wid = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    assert dao.get_work_categories_detail(wid) == []
    assert dao.get_work_classification_input(wid)["classification_state"] == \
        ClassificationState.PENDING


def test_ingest_path_never_calls_the_model(dao, monkeypatch):
    """入庫路徑必須零網路：模型 latency 會外溢到共用 threadpool。"""
    import app.classification.llm as llm_mod

    def explode(*a, **k):
        raise AssertionError("入庫路徑不得建立 HTTP client")

    monkeypatch.setattr(llm_mod.httpx, "Client", explode)
    wid = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    assert dao.get_work_classification_input(wid)["classification_state"] == \
        ClassificationState.PENDING


def test_model_failure_does_not_block_work_creation(dao):
    """模型故障時 Work 仍完整入庫，只是維持未分類。"""
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    wid = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    service = ClassificationService(dao=dao, llm_classifier=make_llm(handler))
    outcome = service.classify_and_persist(wid)

    assert outcome.state == ClassificationState.ERROR
    assert dao.get_work_detail(wid) is not None          # 書還在
    assert dao.get_work_categories_detail(wid) == []     # 沒有被塞任何預設分類


def test_model_failure_preserves_existing_categories_for_retry(dao):
    """失敗態不得清掉既有分類：那是用『判不出』覆蓋『判得出』。"""
    wid = dao.create_work(WorkCreate(title="Effective C++", authors_display="Meyers"))
    before = dao.get_work_categories_detail(wid)
    assert before

    dao.apply_classification(wid, ClassificationOutcome(
        state=ClassificationState.ERROR, error="simulated 503"
    ))
    after = dao.get_work_categories_detail(wid)
    assert [r["category_id"] for r in after] == [r["category_id"] for r in before]
    row = dao.get_work_classification_input(wid)
    assert row["classification_state"] == ClassificationState.ERROR


def test_llm_result_replaces_legacy_rows_atomically(dao):
    wid = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    with dao.engine.session() as conn:
        conn.execute(
            "INSERT INTO work_category (work_id, category_id, source) VALUES (?, 'cat_850', 'legacy')",
            (wid,),
        )
        conn.execute(
            "INSERT INTO work_category (work_id, category_id, source) VALUES (?, 'cat_800', 'legacy')",
            (wid,),
        )
    assert len(dao.get_work_categories_detail(wid)) == 2

    dao.apply_classification(wid, ClassificationOutcome(
        state=ClassificationState.CLASSIFIED,
        category_ids=["cat_473"],
        source=ClassificationSource.LLM,
        confidence=0.91,
        model="test-model",
        prompt_version=PROMPT_VERSION,
    ))
    rows = dao.get_work_categories_detail(wid)
    ids = {r["category_id"] for r in rows}
    assert "cat_850" not in ids and "cat_800" not in ids
    assert ids == {"cat_473", "cat_400"}   # 葉 + 父
    assert all(r["source"] == ClassificationSource.LLM for r in rows)
    assert all(r["model"] == "test-model" for r in rows)


def test_parent_row_is_written_so_parent_counts_survive(dao):
    """推翻 design.md 的葉節點-only 契約：get_category() 只 COUNT 自身。

    沒有父 row，所有父分類的 works_count 會變 0，而該值經
    CategoryWorksResponse.category 回給前端的分類樹徽章。
    """
    wid = dao.create_work(WorkCreate(title="Effective C++", authors_display="Meyers"))
    ids = {r["category_id"] for r in dao.get_work_categories_detail(wid)}
    assert ids == {"cat_471", parent_of("cat_471")}
    assert dao.get_category("cat_400").works_count >= 1
    assert dao.get_category_works("cat_400")[0] >= 1


def test_outcome_invariant_forbids_categories_on_failure_states():
    for state in (ClassificationState.ERROR, ClassificationState.DISABLED,
                  ClassificationState.PENDING, ClassificationState.UNCLASSIFIED):
        with pytest.raises(ValueError):
            ClassificationOutcome(state=state, category_ids=["cat_471"])


def test_outcome_invariant_requires_categories_when_classified():
    with pytest.raises(ValueError):
        ClassificationOutcome(state=ClassificationState.CLASSIFIED, category_ids=[])


def test_stats_report_zero_states_explicitly(dao):
    stats = dao.get_classification_stats()
    assert set(stats) == set(ClassificationState.ALL)
    assert stats[ClassificationState.ERROR] == 0


def test_bootstrap_does_not_reclassify_existing_works(dao):
    """舊 seed 路徑每次啟動都重跑 fallback，會覆蓋回填結果。"""
    wid = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    dao.apply_classification(wid, ClassificationOutcome(
        state=ClassificationState.CLASSIFIED, category_ids=["cat_473"],
        source=ClassificationSource.LLM, confidence=0.9, model="m",
    ))
    before = dao.get_work_categories_detail(wid)

    dao.seed_categories_if_needed()   # 模擬重新啟動
    assert dao.get_work_categories_detail(wid) == before


# --------------------------------------------------------------------------
# Task 3.1 — 回填
# --------------------------------------------------------------------------

def _backfill(dao, handler, **kw):
    from script.backfill_classification import run_backfill
    service = ClassificationService(dao=dao, llm_classifier=make_llm(handler))
    return run_backfill(dao, service=service, **kw)


def test_backfill_dry_run_writes_nothing(dao):
    wid = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    handler = chat_response('{"category_ids":["cat_473"],"confidence":0.9}')

    report = _backfill(dao, handler, apply=False)

    assert report["applied"] is False
    assert report["candidates"] == 1
    assert report["outcomes"][ClassificationState.CLASSIFIED] == 1
    # 關鍵斷言：DB 完全沒動
    assert dao.get_work_categories_detail(wid) == []
    assert dao.get_work_classification_input(wid)["classification_state"] == \
        ClassificationState.PENDING


def test_backfill_apply_writes(dao):
    wid = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    handler = chat_response('{"category_ids":["cat_473"],"confidence":0.9}')

    report = _backfill(dao, handler, apply=True)

    assert report["applied"] is True
    ids = {r["category_id"] for r in dao.get_work_categories_detail(wid)}
    assert ids == {"cat_473", "cat_400"}
    assert dao.get_work_classification_input(wid)["classification_state"] == \
        ClassificationState.CLASSIFIED


def test_backfill_is_idempotent(dao):
    dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    handler = chat_response('{"category_ids":["cat_473"],"confidence":0.9}')

    first = _backfill(dao, handler, apply=True)
    second = _backfill(dao, handler, apply=True)

    assert first["candidates"] == 1
    assert second["candidates"] == 0   # 已 classified 不再入選


def test_backfill_isolates_per_work_failures(dao):
    """一本失敗不得中斷整批。"""
    good = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    bad = dao.create_work(WorkCreate(title="Wibblefrotz Grimlock Compendium"))

    def handler(request):
        body = json.loads(request.content)
        prompt = body["messages"][1]["content"]
        if "Wibblefrotz" in prompt:
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"category_ids":["cat_473"],"confidence":0.9}'}}]
        })

    report = _backfill(dao, handler, apply=True)

    assert report["candidates"] == 2
    assert report["outcomes"][ClassificationState.CLASSIFIED] == 1
    assert report["outcomes"][ClassificationState.ERROR] == 1
    assert dao.get_work_categories_detail(good)
    assert dao.get_work_categories_detail(bad) == []
    assert dao.get_work_classification_input(bad)["classification_state"] == \
        ClassificationState.ERROR


def test_backfill_retries_error_state_works(dao):
    wid = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    _backfill(dao, lambda r: httpx.Response(503, json={"e": 1}), apply=True)
    assert dao.get_work_classification_input(wid)["classification_state"] == \
        ClassificationState.ERROR

    report = _backfill(dao, chat_response('{"category_ids":["cat_473"],"confidence":0.9}'),
                       apply=True)
    assert report["candidates"] == 1
    assert dao.get_work_classification_input(wid)["classification_state"] == \
        ClassificationState.CLASSIFIED


def test_backfill_retries_unclassified_by_default(dao):
    """P1-6：unclassified 必須預設可重試。

    它代表「用當時的模型判不出」，不是「這本書永遠無法分類」——補上 API key
    或換更強的模型之後，這批書必須能被撿回來。若被永久跳過，使用者從介面上
    完全看不出還有這批書可救。
    """
    wid = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    _backfill(dao, chat_response('{"category_ids":[],"confidence":0.0}'), apply=True)
    assert dao.get_work_classification_input(wid)["classification_state"] == \
        ClassificationState.UNCLASSIFIED

    default_run = _backfill(dao, chat_response('{"category_ids":["cat_473"],"confidence":0.9}'),
                            apply=True)
    assert default_run["candidates"] == 1
    assert dao.get_work_classification_input(wid)["classification_state"] == \
        ClassificationState.CLASSIFIED


def test_backfill_retries_disabled_state_by_default(dao):
    """P1-6：API 未設定造成的 disabled 也必須預設可重試。"""
    from script.backfill_classification import run_backfill

    wid = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    unconfigured = ClassificationService(
        dao=dao, llm_classifier=LLMClassifier(config=ClassifierConfig.from_env({})))
    run_backfill(dao, service=unconfigured, apply=True)
    assert dao.get_work_classification_input(wid)["classification_state"] == \
        ClassificationState.DISABLED

    later = _backfill(dao, chat_response('{"category_ids":["cat_473"],"confidence":0.9}'),
                      apply=True)
    assert later["candidates"] == 1
    assert dao.get_work_classification_input(wid)["classification_state"] == \
        ClassificationState.CLASSIFIED


def test_backfill_default_states_cover_every_non_classified_state(dao):
    """控制組：預設候選集合必須恰好是「所有非 classified」，不多不少。

    直接斷言集合本身，而不是只測某幾個狀態——後者在有人新增第六個狀態時
    會靜默漏掉它。
    """
    from script.backfill_classification import DEFAULT_STATES

    assert set(DEFAULT_STATES) == set(ClassificationState.ALL) - {ClassificationState.CLASSIFIED}
    assert set(dao.DEFAULT_BACKFILL_STATES) == set(DEFAULT_STATES)


def test_backfill_is_idempotent_across_all_default_states(dao):
    """已 classified 的不再入選——冪等性不因擴大候選而破壞。"""
    dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    handler = chat_response('{"category_ids":["cat_473"],"confidence":0.9}')
    first = _backfill(dao, handler, apply=True)
    second = _backfill(dao, handler, apply=True)
    assert first["candidates"] == 1
    assert second["candidates"] == 0


def test_backfill_removes_wrong_cat_850_rows(dao):
    """Task 3.2：回填後錯誤的 cat_850 關聯確實消失。"""
    wid = dao.create_work(WorkCreate(title="Operating System Concepts", authors_display="Silberschatz"))
    with dao.engine.session() as conn:   # 模擬 legacy 遺留
        conn.execute(
            "INSERT OR REPLACE INTO work_category (work_id, category_id, source) "
            "VALUES (?, 'cat_850', 'legacy')", (wid,))
        conn.execute(
            "INSERT OR REPLACE INTO work_category (work_id, category_id, source) "
            "VALUES (?, 'cat_800', 'legacy')", (wid,))
    assert dao.get_category_works("cat_850")[0] == 1

    dao.apply_classification(wid, ClassificationOutcome(
        state=ClassificationState.CLASSIFIED, category_ids=["cat_473"],
        source=ClassificationSource.RULE, confidence=1.0))

    assert dao.get_category_works("cat_850")[0] == 0
    assert dao.get_category_works("cat_473")[0] == 1


def test_backfill_with_api_unconfigured_marks_disabled_not_a_category(dao):
    from script.backfill_classification import run_backfill
    wid = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    service = ClassificationService(
        dao=dao,
        llm_classifier=LLMClassifier(config=ClassifierConfig.from_env({})),
    )
    report = run_backfill(dao, service=service, apply=True)
    assert report["outcomes"][ClassificationState.DISABLED] == 1
    assert dao.get_work_categories_detail(wid) == []


def test_schema_has_provenance_columns(dao):
    with dao.engine.session() as conn:
        wc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(work_category)")}
        w_cols = {r["name"] for r in conn.execute("PRAGMA table_info(work)")}
    assert {"source", "model", "prompt_version", "assigned_at"} <= wc_cols
    assert {"classification_state", "classified_at", "classification_error"} <= w_cols


def test_migration_adds_columns_to_legacy_db(tmp_path):
    """舊 DB（無新欄位）必須能被 ALTER 補齊，且 legacy row 標為 legacy。"""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE work (work_id TEXT PRIMARY KEY, title TEXT NOT NULL,
            title_provenance TEXT NOT NULL DEFAULT 'filename_parsed',
            work_type TEXT NOT NULL DEFAULT 'unknown', language TEXT,
            publication_year INTEGER, authors_display TEXT,
            availability_tier INTEGER NOT NULL DEFAULT 0, relevance_authority REAL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, merged_into TEXT);
        CREATE TABLE category (category_id TEXT PRIMARY KEY, parent_id TEXT,
            name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE, icon TEXT,
            level INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE work_category (work_id TEXT NOT NULL, category_id TEXT NOT NULL,
            confidence REAL DEFAULT 1.0, PRIMARY KEY (work_id, category_id));
        INSERT INTO work VALUES ('wk_old','Operating System Concepts','filename_parsed',
            'book','en',NULL,'Silberschatz',0,1.0,'2026-01-01','2026-01-01',NULL);
        INSERT INTO category VALUES ('cat_850',NULL,'華文經典小說','chinese-fiction','🏮',2,4);
        INSERT INTO work_category (work_id, category_id) VALUES ('wk_old','cat_850');
    """)
    conn.commit()
    conn.close()

    dao = CatalogDAO(engine=DatabaseEngine(db_path=db))
    applied = dao.apply_column_migrations()

    # 舊 DB 缺這三個欄位，故第一次遷移必須真的執行它們。
    # 原本寫成 `... in applied or True`，那條斷言恆真——遷移完全沒跑也會綠。
    with dao.engine.session() as conn:
        work_cols = {r["name"] for r in conn.execute("PRAGMA table_info(work)")}
        wc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(work_category)")}
    assert {"classification_state", "classified_at", "classification_error"} <= work_cols
    assert {"source", "model", "prompt_version", "assigned_at"} <= wc_cols
    assert applied == [] or "work.classification_state" in applied

    rows = dao.get_work_categories_detail("wk_old")
    assert rows[0]["source"] == ClassificationSource.LEGACY
    assert dao.get_work_classification_input("wk_old")["classification_state"] == \
        ClassificationState.PENDING


# ==========================================================================
# VANS R1 P1 回歸測試
# ==========================================================================

def _insert_legacy_row(dao, work_id, category_id, source=ClassificationSource.LEGACY):
    with dao.engine.session() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO work_category (work_id, category_id, source) "
            "VALUES (?, ?, ?)", (work_id, category_id, source))


def _tree_count(dao, category_id):
    """從樹狀結果取某節點的 works_count（找不到即 KeyError，不回 0）。"""
    def walk(nodes):
        for n in nodes:
            if n.category_id == category_id:
                return n.works_count
            got = walk(n.children)
            if got is not None:
                return got
        return None
    got = walk(dao.get_category_tree())
    if got is None:
        raise KeyError(f"category not in tree: {category_id}")
    return got


# --- P1-1：回填前的錯誤 legacy 分類不得出現在使用者書攤 ---------------------

def test_p1_1_legacy_pending_rows_hidden_from_all_three_read_paths(dao):
    """《Operating System Concepts》被舊 fallback 塞進 cat_850，回填前就不該看得到。

    三條讀路徑（架位查詢 / 分類詳情徽章 / 分類樹）必須一致，否則書攤上沒有這本、
    徽章卻寫著 1。
    """
    wid = dao.create_work(WorkCreate(title="Operating System Concepts",
                                     authors_display="Silberschatz"))
    # 規則層會把它判為 cat_473；先清掉自動列，模擬純 legacy 遺留。
    with dao.engine.session() as conn:
        conn.execute("DELETE FROM work_category WHERE work_id = ?", (wid,))
        conn.execute("UPDATE work SET classification_state = ? WHERE work_id = ?",
                     (ClassificationState.PENDING, wid))
    _insert_legacy_row(dao, wid, "cat_850")
    _insert_legacy_row(dao, wid, "cat_800")

    assert dao.get_category_works("cat_850")[0] == 0
    assert dao.get_category(  "cat_850").works_count == 0
    assert _tree_count(dao, "cat_850") == 0
    # 控制組：同一個 legacy 列若 Work 已 classified 就必須看得見（證明查詢本身沒壞）
    with dao.engine.session() as conn:
        conn.execute("UPDATE work SET classification_state = ? WHERE work_id = ?",
                     (ClassificationState.CLASSIFIED, wid))
    assert dao.get_category_works("cat_850")[0] == 1
    assert dao.get_category(  "cat_850").works_count == 1
    assert _tree_count(dao, "cat_850") == 1


@pytest.mark.parametrize("state", [
    ClassificationState.PENDING,
    ClassificationState.ERROR,
    ClassificationState.DISABLED,
    ClassificationState.UNCLASSIFIED,
])
def test_p1_1_every_non_classified_state_is_invisible(dao, state):
    wid = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    with dao.engine.session() as conn:
        conn.execute("DELETE FROM work_category WHERE work_id = ?", (wid,))
    _insert_legacy_row(dao, wid, "cat_850")
    with dao.engine.session() as conn:
        conn.execute("UPDATE work SET classification_state = ? WHERE work_id = ?",
                     (state, wid))
    assert dao.get_category_works("cat_850")[0] == 0
    assert dao.get_category("cat_850").works_count == 0


def test_p1_1_after_backfill_correct_category_becomes_visible(dao):
    """回填後：錯的 cat_850 消失、對的 cat_473 出現。"""
    wid = dao.create_work(WorkCreate(title="Operating System Concepts",
                                     authors_display="Silberschatz"))
    with dao.engine.session() as conn:
        conn.execute("DELETE FROM work_category WHERE work_id = ?", (wid,))
        conn.execute("UPDATE work SET classification_state = ? WHERE work_id = ?",
                     (ClassificationState.PENDING, wid))
    _insert_legacy_row(dao, wid, "cat_850")

    report = _backfill(dao, chat_response('{"category_ids":["cat_473"],"confidence":0.9}'),
                       apply=True)
    assert report["candidates"] == 1

    assert dao.get_category_works("cat_850")[0] == 0
    assert dao.get_category("cat_850").works_count == 0
    assert dao.get_category_works("cat_473")[0] == 1
    assert dao.get_category("cat_473").works_count == 1
    assert _tree_count(dao, "cat_473") == 1


def test_p1_1_manual_rows_visible_regardless_of_state(dao):
    """手動分類不受自動狀態影響：pending 的書若有 manual 列仍必須看得見。"""
    wid = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    with dao.engine.session() as conn:
        conn.execute("DELETE FROM work_category WHERE work_id = ?", (wid,))
        conn.execute("UPDATE work SET classification_state = ? WHERE work_id = ?",
                     (ClassificationState.PENDING, wid))
    _insert_legacy_row(dao, wid, "cat_890", source=ClassificationSource.MANUAL)

    assert dao.get_category_works("cat_890")[0] == 1
    assert dao.get_category("cat_890").works_count == 1
    assert _tree_count(dao, "cat_890") == 1


# --- P1-2：manual provenance 不得被自動分類覆寫 -----------------------------

def _classified(cat_ids, source=ClassificationSource.LLM):
    return ClassificationOutcome(
        state=ClassificationState.CLASSIFIED, category_ids=list(cat_ids),
        source=source, confidence=0.9)


def test_p1_2_manual_row_on_same_category_is_preserved(dao):
    """自動分類命中同一個 category 時，manual 列的 provenance 不得被改寫。"""
    wid = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    _insert_legacy_row(dao, wid, "cat_473", source=ClassificationSource.MANUAL)

    dao.apply_classification(wid, _classified(["cat_473"]))

    rows = {r["category_id"]: r for r in dao.get_work_categories_detail(wid)}
    assert rows["cat_473"]["source"] == ClassificationSource.MANUAL
    assert rows["cat_473"]["model"] is None          # 未被 LLM 欄位汙染
    # 控制組：父節點是自動寫入的，證明這次 apply 真的執行了
    assert rows["cat_400"]["source"] == ClassificationSource.LLM


def test_p1_2_manual_row_on_different_category_is_preserved(dao):
    """自動分類判到別的 category 時，manual 列仍必須留著。"""
    wid = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    _insert_legacy_row(dao, wid, "cat_890", source=ClassificationSource.MANUAL)

    dao.apply_classification(wid, _classified(["cat_473"]))

    rows = {r["category_id"]: r for r in dao.get_work_categories_detail(wid)}
    assert rows["cat_890"]["source"] == ClassificationSource.MANUAL
    assert "cat_473" in rows


@pytest.mark.parametrize("source", [
    ClassificationSource.RULE,
    ClassificationSource.LLM,
    ClassificationSource.LEGACY,
])
def test_p1_2_automatic_rows_are_still_replaced(dao, source):
    """控制組：rule/llm/legacy 三種自動來源必須照常被替換掉。

    沒有這一組，上面兩條 manual 測試就是空洞的——把 DELETE 整段刪掉也會綠。
    """
    wid = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    _insert_legacy_row(dao, wid, "cat_890", source=source)
    assert "cat_890" in {r["category_id"] for r in dao.get_work_categories_detail(wid)}

    dao.apply_classification(wid, _classified(["cat_473"]))

    assert "cat_890" not in {r["category_id"] for r in dao.get_work_categories_detail(wid)}


def test_p1_2_manual_row_survives_unclassified_outcome(dao):
    """模型判不出時清掉自動列，但 manual 列仍在。"""
    wid = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    _insert_legacy_row(dao, wid, "cat_890", source=ClassificationSource.MANUAL)
    _insert_legacy_row(dao, wid, "cat_850", source=ClassificationSource.LEGACY)

    dao.apply_classification(wid, ClassificationOutcome(
        state=ClassificationState.UNCLASSIFIED, confidence=0.0))

    cats = {r["category_id"]: r["source"] for r in dao.get_work_categories_detail(wid)}
    assert cats == {"cat_890": ClassificationSource.MANUAL}


# --- P1-3：父分類 works_count 必須 COUNT(DISTINCT work_id) ------------------

def test_p1_3_parent_badge_counts_one_work_once_across_sibling_leaves(dao):
    """同一本書命中兩個 sibling 葉節點時，父分類徽章必須是 1 不是 2。"""
    wid = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    dao.apply_classification(wid, _classified(["cat_471", "cat_472"]))

    detail = {r["category_id"] for r in dao.get_work_categories_detail(wid)}
    assert {"cat_471", "cat_472", "cat_400"} <= detail   # 前提：真的雙葉

    assert dao.get_category("cat_400").works_count == 1
    assert _tree_count(dao, "cat_400") == 1
    # 與實際列表一致：點進去只有一本
    assert dao.get_category_works("cat_400")[0] == 1


def test_p1_3_single_leaf_control(dao):
    """單葉控制組：確保上面那條不是因為計數恆為 1 而通過。"""
    a = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    dao.apply_classification(a, _classified(["cat_471"]))
    assert dao.get_category("cat_400").works_count == 1

    b = dao.create_work(WorkCreate(title="Wibblefrotz Grimlock Compendium"))
    dao.apply_classification(b, _classified(["cat_472"]))
    assert dao.get_category("cat_400").works_count == 2
    assert _tree_count(dao, "cat_400") == 2
    assert dao.get_category_works("cat_400")[0] == 2


# --- P1-4：dry-run 絕不建庫 / 不 migrate / 不碰錯 DB ------------------------

def _run_cli(args):
    """跑 CLI 並回傳 (rc, stdout+stderr)。rc 直接取自 main()，不經管線。"""
    import io
    import contextlib
    from script.backfill_classification import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = main(args)
        except SystemExit as exc:          # argparse 的 parser.error()
            rc = exc.code if isinstance(exc.code, int) else 2
    return rc, out.getvalue() + err.getvalue()


def test_p1_4_zero_byte_db_is_rejected_and_left_untouched(tmp_path):
    z = tmp_path / "openshelf.sqlite"
    z.write_bytes(b"")
    rc, out = _run_cli(["--db", str(z)])
    assert rc != 0
    assert z.stat().st_size == 0          # 沒有被 bootstrap 成一個真 DB
    assert "schema" in out


def test_p1_4_db_missing_required_schema_is_rejected_byte_identical(tmp_path):
    import hashlib
    p = tmp_path / "wrong.sqlite"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE unrelated (a TEXT)")
    conn.commit()
    conn.close()
    before = hashlib.sha256(p.read_bytes()).hexdigest()

    rc, out = _run_cli(["--db", str(p)])

    assert rc != 0
    assert hashlib.sha256(p.read_bytes()).hexdigest() == before
    assert "work" in out                   # 訊息指出缺了哪張表


def test_p1_4_missing_file_is_rejected_and_not_created(tmp_path):
    p = tmp_path / "nope.sqlite"
    rc, _ = _run_cli(["--db", str(p)])
    assert rc != 0
    assert not p.exists()


def test_p1_4_dry_run_on_real_db_leaves_bytes_identical(dao, tmp_path, monkeypatch):
    """控制組同時證明兩件事：正常 DB 會被接受（不是每個 DB 都被擋），
    且 dry-run 走完整條路徑後位元組不變。

    必須注入一個能得出有效判定的 service：本檔的 dao fixture 沒有 API 設定，
    若用 CLI 預設的 service，outcome 會是 disabled，依 VANS R2 契約 rc=1——那時
    `rc == 0` 就不再是「schema 閘接受了這個 DB」的代理，這條斷言會量到別的東西。
    """
    import hashlib

    dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    db = Path(dao.engine.db_path)
    _install_service(monkeypatch, ClassificationService(
        dao=dao,
        llm_classifier=make_llm(chat_response('{"category_ids":["cat_473"],"confidence":0.9}'))))
    # WAL 內容也算資料：先 checkpoint 併回主檔，才能用單檔 sha 當判準
    with dao.engine.session() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    rc, out = _run_cli(["--db", str(db)])

    assert rc == 0                                   # 正常 DB 未被誤擋
    assert "schema" not in out                       # 且不是被 schema 閘檔下
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert "DRY-RUN" in out


def test_p1_4_dry_run_connection_actually_rejects_writes(dao):
    """dry-run 用的 DAO 必須連寫都寫不進去，而不是靠「我沒呼叫寫入」的自律。"""
    from script.backfill_classification import open_dao

    db = Path(dao.engine.db_path)
    ro = open_dao(db, apply=False)
    with pytest.raises(sqlite3.OperationalError):
        with ro.engine.session() as conn:
            conn.execute("UPDATE work SET title = 'tampered'")
    # 控制組：唯讀連線讀得到東西，證明上面不是因為連線壞掉才失敗
    assert isinstance(ro.get_classification_stats(), dict)


# --- P1-5：部分失敗必須非零 rc，且成功數不得失真 ----------------------------

class _ExplodingPersistDAO:
    """代理 DAO，但 apply_classification 一律拋例外。"""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def apply_classification(self, work_id, outcome):
        raise RuntimeError("disk on fire")


def test_p1_5_persist_failure_is_counted_and_not_reported_as_success(dao):
    from script.backfill_classification import run_backfill

    dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    proxy = _ExplodingPersistDAO(dao)
    service = ClassificationService(
        dao=proxy,
        llm_classifier=make_llm(chat_response('{"category_ids":["cat_473"],"confidence":0.9}')))

    report = run_backfill(proxy, service=service, apply=True)

    assert report["outcomes"][ClassificationState.CLASSIFIED] == 1   # 分類確實成功
    assert report["persisted"] == 0                                  # 但沒寫進去
    assert report["persist_failures"] == 1
    assert report["failures"] == 1
    assert report["per_work"][0]["persisted"] is False
    assert "persist failed" in report["per_work"][0]["error"]


def test_p1_5_persist_failure_does_not_stop_later_works(dao):
    """逐本隔離：第一本寫入炸掉，後面的仍要被處理完。"""
    from script.backfill_classification import run_backfill

    dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    dao.create_work(WorkCreate(title="Wibblefrotz Grimlock Compendium"))
    proxy = _ExplodingPersistDAO(dao)
    service = ClassificationService(
        dao=proxy,
        llm_classifier=make_llm(chat_response('{"category_ids":["cat_473"],"confidence":0.9}')))

    report = run_backfill(proxy, service=service, apply=True)

    assert report["candidates"] == 2
    assert report["persist_failures"] == 2          # 兩本都跑到了，不是第一本就中斷
    assert len(report["per_work"]) == 2


def test_p1_5_classification_exception_is_isolated_and_counted(dao):
    from script.backfill_classification import run_backfill

    dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    dao.create_work(WorkCreate(title="Wibblefrotz Grimlock Compendium"))

    class _BoomService(ClassificationService):
        calls = 0

        def classify(self, title, authors=None, language=None, work_type=None):
            _BoomService.calls += 1
            if _BoomService.calls == 1:
                raise RuntimeError("classifier exploded")
            return ClassificationOutcome(state=ClassificationState.UNCLASSIFIED,
                                         confidence=0.0)

    report = run_backfill(dao, service=_BoomService(dao=dao), apply=True)

    assert report["candidates"] == 2
    assert report["classification_failures"] == 1
    assert report["failures"] == 1
    assert report["outcomes"][ClassificationState.UNCLASSIFIED] == 1   # 第二本仍被處理
    assert report["per_work"][0]["state"] == "exception"


def test_p1_5_cli_returns_nonzero_on_partial_failure(dao, monkeypatch):
    """CLI 層：有失敗就 rc != 0；全成功才 rc == 0。"""
    import script.backfill_classification as bf

    dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    db = Path(dao.engine.db_path)

    real_open = bf.open_dao
    monkeypatch.setattr(bf, "open_dao", lambda p, apply: _ExplodingPersistDAO(real_open(p, apply)))
    monkeypatch.setattr(bf, "ClassificationService", lambda dao=None, **kw: ClassificationService(
        dao=dao, llm_classifier=make_llm(chat_response('{"category_ids":["cat_473"],"confidence":0.9}'))))

    rc_fail, out_fail = _run_cli(["--db", str(db), "--apply"])
    assert rc_fail == 1
    # 寫入失敗走的是「執行失敗」那條警告（與 error/disabled 的「未得到有效判定」
    # 分開列）；斷言完整前綴才能確定 rc=1 是來自 persist 失敗而非別的原因。
    assert "警告：1 本執行失敗（分類例外 0、寫入失敗 1）" in out_fail

    # 控制組：拿掉爆炸代理後同一條路徑必須 rc == 0，否則上面那個 1 可能來自別的原因
    monkeypatch.setattr(bf, "open_dao", real_open)
    rc_ok, _ = _run_cli(["--db", str(db), "--apply"])
    assert rc_ok == 0


# --- VANS R2：error / disabled outcome 必須算本輪未成功且 rc 非零 -------------
#
# 漏洞形狀：這兩種 outcome 會被「成功持久化」（狀態寫進 DB 供日後重試），於是
# persist 沒失敗、分類器也沒拋例外 —— failures 停在 0，CLI 回 rc=0。排程器因此
# 把「模型 API 整批掛掉」讀成「這批書都處理好了」：錯書仍隱藏、沒有任何告警。
#
# 契約：error / disabled -> rc 1；classified / unclassified -> rc 0。
# unclassified 是模型的有效判定（確實歸不了類），不是故障，所以不算未成功。


def _install_service(monkeypatch, service):
    """把 CLI 內部建的 ClassificationService 換成指定實例（不動 open_dao）。"""
    import script.backfill_classification as bf
    monkeypatch.setattr(bf, "ClassificationService", lambda dao=None, **kw: service)


class _FixedOutcomeService(ClassificationService):
    """對每一本都回傳同一個 outcome，並記錄實際被呼叫幾次。"""

    def __init__(self, outcome, **kw):
        super().__init__(**kw)
        self._outcome = outcome
        self.calls = 0

    def classify(self, title, authors=None, language=None, work_type=None):
        self.calls += 1
        return self._outcome


def _outcome(state):
    if state == ClassificationState.CLASSIFIED:
        return ClassificationOutcome(
            state=state, category_ids=["cat_473"],
            source=ClassificationSource.LLM, confidence=0.9)
    return ClassificationOutcome(state=state, confidence=0.0)


@pytest.mark.parametrize("state", [ClassificationState.ERROR, ClassificationState.DISABLED])
def test_vans_r2_error_and_disabled_outcomes_count_as_unsuccessful(dao, state):
    """runner 層：outcome 落在 error/disabled 時 unsuccessful 必須計數。

    同時斷言 persisted == 1 —— 證明這一本**確實被成功寫入**，所以舊的
    `failures` 計數抓不到它；unsuccessful 是獨立的第二格，不是 failures 的別名。
    """
    from script.backfill_classification import run_backfill

    wid = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    service = _FixedOutcomeService(_outcome(state), dao=dao)

    report = run_backfill(dao, service=service, apply=True)

    assert report["outcomes"][state] == 1
    assert report["unsuccessful"] == 1
    assert report["failures"] == 0            # 沒有例外、沒有寫入失敗
    assert report["persisted"] == 1           # 狀態確實寫進去了（供日後重試）
    assert dao.get_work_classification_input(wid)["classification_state"] == state


@pytest.mark.parametrize("state", [ClassificationState.CLASSIFIED,
                                   ClassificationState.UNCLASSIFIED])
def test_vans_r2_classified_and_unclassified_are_not_unsuccessful(dao, state):
    """控制組：有效判定不得被算成未成功，否則 rc 會永遠是 1。"""
    from script.backfill_classification import run_backfill

    dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    service = _FixedOutcomeService(_outcome(state), dao=dao)

    report = run_backfill(dao, service=service, apply=True)

    assert report["outcomes"][state] == 1
    assert report["unsuccessful"] == 0
    assert report["failures"] == 0


@pytest.mark.parametrize("state", [ClassificationState.ERROR, ClassificationState.DISABLED])
def test_vans_r2_cli_returns_nonzero_for_error_and_disabled(dao, monkeypatch, state):
    """CLI 層真實 rc：走完整 main()，不是只看 report dict。"""
    dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    db = Path(dao.engine.db_path)
    _install_service(monkeypatch, _FixedOutcomeService(_outcome(state), dao=dao))

    rc, out = _run_cli(["--db", str(db), "--apply"])

    assert rc == 1
    # 斷言「警告：」前綴而非某個詞：報告裡有一行常態統計也含同樣的字（即使為 0），
    # 拿那種字串當判準會讓這條斷言在 0 本未成功時也綠——空洞通過。
    assert "警告：1 本未得到有效判定" in out
    assert state in out


@pytest.mark.parametrize("state", [ClassificationState.CLASSIFIED,
                                   ClassificationState.UNCLASSIFIED])
def test_vans_r2_cli_returns_zero_for_valid_verdicts(dao, monkeypatch, state):
    """控制組：同一條 CLI 路徑在有效判定下必須 rc == 0。

    沒有這一組，上面那個 rc == 1 可能來自任何原因（DB 開不了、參數錯），
    而不是來自 error/disabled 的判定。
    """
    dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    db = Path(dao.engine.db_path)
    _install_service(monkeypatch, _FixedOutcomeService(_outcome(state), dao=dao))

    rc, out = _run_cli(["--db", str(db), "--apply"])

    assert rc == 0
    assert "警告：" not in out


def test_vans_r2_error_outcome_does_not_stop_later_works(dao, monkeypatch):
    """逐本隔離不因 rc 契約而破壞：第一本 error，後面的仍要跑完並寫入。"""
    import script.backfill_classification as bf

    a = dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    b = dao.create_work(WorkCreate(title="Wibblefrotz Grimlock Compendium"))
    db = Path(dao.engine.db_path)

    class _FirstErrorsService(ClassificationService):
        calls = 0

        def classify(self, title, authors=None, language=None, work_type=None):
            _FirstErrorsService.calls += 1
            if _FirstErrorsService.calls == 1:
                return _outcome(ClassificationState.ERROR)
            return _outcome(ClassificationState.CLASSIFIED)

    _install_service(monkeypatch, _FirstErrorsService(dao=dao))
    rc, out = _run_cli(["--db", str(db), "--apply"])

    assert rc == 1                          # 有一本 error，整批不得回報成功
    assert "警告：1 本未得到有效判定" in out
    assert _FirstErrorsService.calls == 2    # 第二本確實被處理，沒有提早中斷
    states = {
        dao.get_work_classification_input(a)["classification_state"],
        dao.get_work_classification_input(b)["classification_state"],
    }
    assert states == {ClassificationState.ERROR, ClassificationState.CLASSIFIED}


def test_vans_r2_real_http_failure_path_yields_nonzero_rc(dao, monkeypatch):
    """端到端：不是用假 outcome，而是讓真的 LLMClassifier 撞 HTTP 503。

    上面的測試都注入 outcome，若 service 那層把 503 對映成別的狀態，那些測試
    仍會綠。這一組把 transport 也接上，證明「遠端掛掉」真的走到 rc=1。
    """
    dao.create_work(WorkCreate(title="Zzyzx Quorbleflux Handbook"))
    db = Path(dao.engine.db_path)
    service = ClassificationService(
        dao=dao, llm_classifier=make_llm(lambda r: httpx.Response(503, json={"e": 1})))
    _install_service(monkeypatch, service)

    rc, out = _run_cli(["--db", str(db), "--apply"])

    assert rc == 1
    assert "警告：1 本未得到有效判定" in out
    assert ClassificationState.ERROR in out


def test_vans_r2_unsuccessful_states_set_is_exactly_error_and_disabled():
    """控制組：直接斷言集合本身。

    只測「error 會非零」的話，有人日後把 unclassified 也塞進去（讓每一批書都
    告警）不會有任何測試變紅。
    """
    from script.backfill_classification import UNSUCCESSFUL_STATES

    assert set(UNSUCCESSFUL_STATES) == {ClassificationState.ERROR,
                                        ClassificationState.DISABLED}
    assert ClassificationState.UNCLASSIFIED not in UNSUCCESSFUL_STATES
    assert ClassificationState.CLASSIFIED not in UNSUCCESSFUL_STATES
