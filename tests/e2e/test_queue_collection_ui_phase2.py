"""FR-20260820_234500 R1 前端實測 Phase 2 —— 書單 modal 資料來源與三態 badge。

原始碼來自 `$XDG_RUNTIME_DIR/openshelf-fe/fe_test_phase2.py`。兩格標的：

  A. 點 📚 之後 modal 列出的是**書單**，不是 Chrome 書籤資料夾（Q2 那格的實地驗證）
  B. 三態 badge 在畫面上真的可區分（沒指定 / 已指定待歸戶 / 歸戶失敗）

## B 為什麼用 `page.route` 注入假 job，而不是造真資料

第三態（`collection_sync_error` 非空）在線上**造不出來**：它需要 job 已
completed、work_id 存在、且 DB 寫入失敗（例如 FK 違反）。要在真實環境湊齊
這三格，得先讓一個下載真的成功、再想辦法讓歸戶寫入炸掉。

而 B 的標的其實不是「後端會不會產生第三態」，是「**渲染邏輯拿到三種輸入
時會不會產生三種可辨的輸出**」。攔截 `/api/crawler/jobs` 把三種輸入直接餵給
前端，驗的正是這件事，且完全不污染線上資料。

閘門與跳過語意見 `tests/e2e/conftest.py`。
"""
import json

import pytest

from tests.e2e.conftest import BASE_URL, JOB_TITLE, api_get

# 三筆假 job，各自代表一種書單歸屬狀態。
FAKE_JOBS = [
    {
        "job_id": "job_state_none", "md5": "1" * 32, "title": "A沒指定書單",
        "authors": None, "extension": "pdf", "publication_year": None,
        "collection_ids": [], "collection_sync_error": None,
        "status": "queued", "progress_percent": 0, "downloaded_bytes": 0,
        "total_bytes": 0, "retry_count": 0, "error_message": None, "work_id": None,
        "created_at": "2026-08-21T00:00:00+00:00",
        "updated_at": "2026-08-21T00:00:00+00:00",
    },
    {
        "job_id": "job_state_pending", "md5": "2" * 32, "title": "B已指定待歸戶",
        "authors": None, "extension": "pdf", "publication_year": None,
        "collection_ids": ["col_favorites", "col_x"], "collection_sync_error": None,
        "status": "queued", "progress_percent": 0, "downloaded_bytes": 0,
        "total_bytes": 0, "retry_count": 0, "error_message": None, "work_id": None,
        "created_at": "2026-08-21T00:00:00+00:00",
        "updated_at": "2026-08-21T00:00:00+00:00",
    },
    {
        "job_id": "job_state_failed", "md5": "3" * 32, "title": "C歸戶失敗",
        "authors": None, "extension": "pdf", "publication_year": None,
        "collection_ids": ["col_gone"],
        "collection_sync_error": (
            "1/1 個書單寫入失敗：col_gone"
            "(IntegrityError: FOREIGN KEY constraint failed)"
        ),
        "status": "completed", "progress_percent": 100, "downloaded_bytes": 10,
        "total_bytes": 10, "retry_count": 0, "error_message": None,
        "work_id": "wrk_fake",
        "created_at": "2026-08-21T00:00:00+00:00",
        "updated_at": "2026-08-21T00:00:00+00:00",
    },
]


# ---------------------------------------------------------------------------
# A. 📚 modal 的資料來源是書單
# ---------------------------------------------------------------------------

@pytest.fixture
def live_collections():
    return api_get("/api/collections")


@pytest.fixture
def collection_modal(page, live_job):
    """點開某筆 job 的 📚 書單指定 modal。"""
    page.goto(BASE_URL + "/", wait_until="networkidle", timeout=30000)
    page.evaluate("openQueueModal()")
    page.wait_for_timeout(1500)
    row = page.locator(f".queue-item:has-text('{JOB_TITLE}')")
    row.locator("button[title='指定歸屬書單']").click()
    page.wait_for_timeout(2000)
    return page


def test_collection_modal_opens(collection_modal):
    active = collection_modal.evaluate(
        "document.getElementById('queueCollectionModal').classList.contains('active')"
    )
    assert active is True, f"queueCollectionModal 沒有進入 active，active={active}"


def test_modal_item_count_matches_live_collections(collection_modal, live_collections):
    boxes = collection_modal.locator(".queue-col-checkbox")
    assert boxes.count() == len(live_collections), (
        f"modal 列出的項目數應等於線上書單數，"
        f"dom={boxes.count()} api={len(live_collections)}"
    )


def test_modal_ids_are_real_collection_ids(collection_modal, live_collections):
    """Q2 的核心：checkbox 帶的必須是 collection_id，不是書籤資料夾 id。"""
    boxes = collection_modal.locator(".queue-col-checkbox")
    dom_ids = [
        boxes.nth(i).get_attribute("data-col-id") for i in range(boxes.count())
    ]
    live_ids = {c["collection_id"] for c in live_collections}

    assert set(dom_ids) == live_ids, (
        f"modal 的 id 集合與線上書單不符\n  dom={sorted(dom_ids)}\n  api={sorted(live_ids)}"
    )
    assert all(i.startswith("col_") for i in dom_ids), (
        f"有 id 不是 col_ 開頭（疑似書籤資料夾 id）：{dom_ids}"
    )


def test_modal_shows_every_collection_name(collection_modal, live_collections):
    dom_text = collection_modal.locator("#queueCollectionList").inner_text()
    missing = [c["name"] for c in live_collections if c["name"] not in dom_text]
    assert not missing, f"這些書單名沒出現在 modal 裡：{missing}"


def test_control_nonexistent_collection_name_absent(collection_modal):
    """控制組：一個保證不存在的書單名絕不能出現。

    上一條是「每個名字都在」，其失敗模式是 `dom_text` 抓成了整頁文字，
    於是什麼都「在」。本條反證 dom_text 的比對真的有鑑別力。
    """
    dom_text = collection_modal.locator("#queueCollectionList").inner_text()
    assert "ZZZ_NOT_A_COLLECTION" not in dom_text, (
        "不存在的書單名竟然出現在 modal 裡，名稱比對失去鑑別力"
    )


def test_previously_assigned_collection_is_checked(collection_modal, live_job):
    """phase1 點過 ★ 的那筆，在 modal 裡應呈現為已勾選。

    依賴同一個 session 級的 live_job，且 phase1 已對它點過 ★。
    """
    fav = collection_modal.locator(".queue-col-checkbox[data-col-id='col_favorites']")
    assert fav.is_checked() is True, (
        f"col_favorites 應為已勾選，實得 checked={fav.is_checked()}"
    )


# ---------------------------------------------------------------------------
# B. 三態 badge
# ---------------------------------------------------------------------------

@pytest.fixture
def three_state_page(page):
    """攔截 /api/crawler/jobs 注入三筆假 job，回傳已渲染的頁面。"""
    page.goto(BASE_URL + "/", wait_until="networkidle", timeout=30000)
    page.route(
        "**/api/crawler/jobs",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(FAKE_JOBS),
        ),
    )
    page.evaluate("openQueueModal()")
    page.wait_for_timeout(2000)
    return page


def _badge_of(page, title):
    """取某列裡與書單有關的 badge 文字（📚 或 ⚠️ 開頭的 span）。"""
    row = page.locator(f".queue-item:has-text('{title}')")
    if row.count() != 1:
        return f"<row count={row.count()}>"
    spans = row.locator("span[title]")
    out = []
    for i in range(spans.count()):
        t = spans.nth(i).inner_text().strip()
        if "📚" in t or "⚠️" in t:
            out.append(t)
    return "|".join(out)


def test_state_none_shows_no_badge(three_state_page):
    """缺席態：沒指定書單時不顯示 badge。"""
    b = _badge_of(three_state_page, "A沒指定書單")
    assert b == "", f"沒指定書單時不該有 badge，實得 {b!r}"


def test_state_pending_shows_count_and_label(three_state_page):
    """待歸戶態：顯示數量且標明待歸戶。"""
    b = _badge_of(three_state_page, "B已指定待歸戶")
    assert "📚" in b, f"待歸戶 badge 應含 📚，實得 {b!r}"
    assert "2" in b, f"待歸戶 badge 應顯示書單數 2，實得 {b!r}"
    assert "待歸戶" in b, f"待歸戶 badge 應標明「待歸戶」，實得 {b!r}"


def test_state_failed_shows_failure(three_state_page):
    """失敗態：顯示歸戶失敗。"""
    b = _badge_of(three_state_page, "C歸戶失敗")
    assert "⚠️" in b, f"失敗 badge 應含 ⚠️，實得 {b!r}"
    assert "失敗" in b, f"失敗 badge 應標明「失敗」，實得 {b!r}"


def test_three_states_are_mutually_distinguishable(three_state_page):
    """三種輸入必須產生三種**兩兩不同**的輸出。

    個別斷言各自通過，仍可能三態長得一樣（例如全部都顯示
    「📚 2 待歸戶」）。可辨性要獨立驗。
    """
    outs = {
        _badge_of(three_state_page, "A沒指定書單"),
        _badge_of(three_state_page, "B已指定待歸戶"),
        _badge_of(three_state_page, "C歸戶失敗"),
    }
    assert len(outs) == 3, f"三態應兩兩可辨，實得 {len(outs)} 種相異輸出：{outs}"


def test_failure_badge_tooltip_names_the_failed_collection(three_state_page):
    """失敗原因必須帶得出是哪個 cid 失敗，不能只寫「有錯」。"""
    row = three_state_page.locator(".queue-item:has-text('C歸戶失敗')")
    tip = row.locator("span[title*='寫入失敗']")
    assert tip.count() == 1, f"應有 1 個帶失敗說明的 tooltip，實得 count={tip.count()}"
    title = tip.get_attribute("title") or ""
    assert "col_gone" in title, f"tooltip 應指名失敗的 cid，實得 {title[:120]!r}"


# ---------------------------------------------------------------------------
# 殘留稽核：本批在線上造的資料，跑完必須清乾淨
# ---------------------------------------------------------------------------

def test_zzz_residue_is_clean(residue_baseline, live_job):
    """收尾稽核：job 數與 collection_item 總列數必須回到基線。

    命名以 `zzz_` 開頭讓它在檔內排最後（pytest 預設按檔內定義順序，
    但這條刻意也在字母序末尾，避免有人加 `-p randomly` 後亂序）。

    `live_job` 是 session 級 fixture，其 finalizer 在整個 session 結束時
    才刪除 job，所以本條執行時那筆 job **還在**——因此預期差值是 +1 job，
    而不是 0。這不是放寬，是把「fixture 尚未收尾」這件事寫進斷言，
    否則本條會恆綠（永遠差 1，永遠對不上，於是有人把它改成不比對）。

    真正的「回到基線」由 dispatcher 在整個 pytest 程序結束後獨立覆核。
    """
    base_jobs, base_items = residue_baseline
    now_jobs, now_items = __import__(
        "tests.e2e.conftest", fromlist=["count_residue"]
    ).count_residue()

    assert now_jobs == base_jobs + 1, (
        f"session 內應只多出 live_job 這 1 筆，"
        f"基線 {base_jobs} → 現在 {now_jobs}（差 {now_jobs - base_jobs}）"
    )
    assert now_items == base_items, (
        f"本批不應改變 collection_item 列數（job 尚未完成，歸戶只記意圖不寫 DB），"
        f"基線 {base_items} → 現在 {now_items}"
    )
