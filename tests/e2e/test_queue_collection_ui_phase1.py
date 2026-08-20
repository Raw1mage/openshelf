"""FR-20260820_234500 R1 前端實測 Phase 1 —— 下載佇列的書單指定按鈕（真瀏覽器）。

原始碼來自 `$XDG_RUNTIME_DIR/openshelf-fe/fe_test_phase1.py`（一支自帶 runner 的
獨立腳本）。收進 `tests/` 的理由：那支腳本落在 scratch 目錄，重開機就蒸發，
等於驗過即失。7 條斷言的價值在於它們證明的是「點了有反應」，而不是
`node --check` 那種「語法沒壞」。

閘門與跳過語意見 `tests/e2e/conftest.py`。

## 每一格都配控制組

`locator.count() == 1` 這種斷言的失敗模式是「選擇器寫錯 ⇒ 永遠 count()==0」，
而 0 同時也是「元素真的不存在」的正確答案——兩態共用一個輸出。所以
`test_control_nonexistent_button_is_absent` 存在：它用一個保證不存在的
selector 驗證「找不到」這件事真的會回 0，反證上面的 count()==1 有鑑別力。
"""
import pytest

from tests.e2e.conftest import BASE_URL, JOB_TITLE, api_call


@pytest.fixture
def queue_page(page, live_job):
    """開好下載佇列 modal、且我造的那筆 job 已在列表中的頁面。"""
    page.goto(BASE_URL + "/", wait_until="networkidle", timeout=30000)
    page.evaluate("openQueueModal()")
    page.wait_for_timeout(1500)
    return page


@pytest.fixture
def job_row(queue_page):
    return queue_page.locator(f".queue-item:has-text('{JOB_TITLE}')")


def test_new_job_starts_with_no_collections(live_job):
    """前置：剛建立的 job，collection_ids 必須是空的（缺席態）。

    這條同時是後面「點 ★ 之後變非空」的對照——沒有它，
    「本來就有 col_favorites」與「點擊真的寫進去了」無法區分。
    """
    assert live_job["collection_ids"] == [], (
        f"新 job 不該預先帶書單，實得 {live_job['collection_ids']}"
    )


def test_queue_modal_opens(queue_page):
    active = queue_page.evaluate(
        "document.getElementById('queueModal').classList.contains('active')"
    )
    assert active is True, f"queueModal 沒有進入 active 狀態，active={active}"


def test_created_job_visible_in_queue(job_row):
    assert job_row.count() == 1, (
        f"佇列裡應該剛好看到一筆「{JOB_TITLE}」，實得 count={job_row.count()}"
    )


def test_each_row_has_favorite_button(job_row):
    """AC1：每列有 ★ 一鍵最愛按鈕。"""
    star = job_row.locator("button[title*='最愛']")
    assert star.count() == 1, (
        f"每列應有 1 個 ★ 最愛按鈕，實得 count={star.count()} "
        f"texts={star.all_inner_texts()}"
    )


def test_each_row_has_multiselect_button(job_row):
    """AC1：每列有 📚 多選書單按鈕。"""
    books = job_row.locator("button[title='指定歸屬書單']")
    assert books.count() == 1, (
        f"每列應有 1 個 📚 多選按鈕，實得 count={books.count()} "
        f"texts={books.all_inner_texts()}"
    )


def test_control_nonexistent_button_is_absent(job_row):
    """控制組：一個保證不存在的 selector 必須回 0。

    這條若失敗（回非 0），代表 locator 比對根本沒在作用，
    上面兩條的 count()==1 也就不可信。
    """
    ctrl = job_row.locator("button[title='ZZZ_NOT_A_REAL_BUTTON']")
    assert ctrl.count() == 0, (
        f"不存在的按鈕竟然找得到 count={ctrl.count()}，locator 比對失去鑑別力"
    )


def test_clicking_favorite_persists_via_api(queue_page, job_row, live_job):
    """點 ★ 之後打 API 覆核，不看 UI。

    只看 UI 有勾起來，證明的是「前端自己畫了個勾」；打 API 才知道
    後端真的收到了。這兩件事在畫面上長得一模一樣。
    """
    job_id = live_job["job_id"]
    job_row.locator("button[title*='最愛']").click()
    queue_page.wait_for_timeout(2000)

    st, after = api_call(f"/api/crawler/jobs/{job_id}")
    cids = after.get("collection_ids") or []
    assert "col_favorites" in cids, (
        f"點 ★ 後 col_favorites 應寫進 job，API 實得 collection_ids={cids}"
    )
