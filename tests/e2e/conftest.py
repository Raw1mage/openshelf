"""End-to-end（真瀏覽器 + 線上服務）測試的目錄級閘門。

## 為什麼這個檔存在，以及它刻意**不**做什麼

本目錄底下的測試會啟動真的 chromium 並打 port 8088 上的線上服務。
它們與 `tests/` 底下其餘 246 條純單元測試的環境需求完全不同，
但**必須仍被 `pytest tests/` 預設收集到**——否則「測試被收進 repo 但沒跑」
與「我根本沒把測試收進來」在預設輸出上共用同一個結果，那正是本專案
一再要消滅的「缺席態與失敗態共用一個輸出」。所以預設模式下它們顯示為
`skipped` 而不是被 `testpaths` 排除：一行 `18 skipped` 是存在證明，
排除則什麼痕跡都不留。

刻意不做的事：
  * 不新增 `pytest.ini` / `pyproject.toml` / 根目錄 `conftest.py`。
    `conftest.py` **不是 rootdir 錨點**（只有 ini 類檔案是），所以本檔
    不會位移 rootdir，既有測試的收集路徑零影響。
  * 不註冊 marker、不使用 marker。目錄級 conftest 的 autouse fixture
    天然只作用於本目錄底下，不需要靠 marker 或路徑過濾。
  * **module 層不 import playwright。** 若在 module 層 import，缺
    playwright 的環境會得到 collection **error**（rc≠0），而不是 skip，
    CI 就會因為一個可選依賴而紅。所有 playwright import 一律延遲到
    fixture 內部。

## 三態，不是兩態

| 狀態 | 條件 | 結果 |
|---|---|---|
| 沒被要求 | 未設 `OPENSHELF_E2E=1` | **skip**（誠實：不主張任何關於系統的事實） |
| 被要求但環境壞 | 設了，但 playwright / chromium / 服務缺一 | **loud 失敗**，訊息指名壞在哪一格 |
| 被要求且環境好 | 三者齊全 | 真的跑 |

第二列刻意不是 skip。「我試了但連不上」與「我驗過了沒問題」若共用
`skipped` 這個看起來安全的輸出，等於讓服務掛掉偽裝成測試通過。
「你沒要求我跑」則不同——它不主張任何事實，所以 skip 是誠實的。

## 怎麼跑

    OPENSHELF_E2E=1 .venv/bin/python -m pytest tests/e2e/ -v

可選環境變數：
  OPENSHELF_E2E_BASE_URL   預設 http://127.0.0.1:8088
  OPENSHELF_E2E_CHROME     chromium 執行檔路徑；預設見 DEFAULT_CHROME
"""
import json
import os
import urllib.error
import urllib.request

import pytest

E2E_FLAG = "OPENSHELF_E2E"

BASE_URL = os.environ.get("OPENSHELF_E2E_BASE_URL", "http://127.0.0.1:8088").rstrip("/")

# playwright 1.62 名義上要 chromium-1234，但本機只有 1200 是完整的 chrome
# （1228 只有 headless_shell，跑不了需要完整瀏覽器的情境）。用 executable_path
# 指既有的 1200，不下載新版。
DEFAULT_CHROME = "/home/pkcs12/.cache/ms-playwright/chromium-1200/chrome-linux64/chrome"
CHROME_PATH = os.environ.get("OPENSHELF_E2E_CHROME", DEFAULT_CHROME)


def api_get(path, timeout=15):
    """GET 一個 JSON API。錯誤一律往上拋，不吞。"""
    with urllib.request.urlopen(BASE_URL + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def api_call(path, method="GET", body=None, timeout=20):
    """打一個 JSON API，回 (status, payload)。"""
    req = urllib.request.Request(
        BASE_URL + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw else None)


def _probe_environment():
    """回傳一份 problems 清單。空清單代表三格都齊。

    每一格都獨立回報，因為「playwright 沒裝」「chromium 不在」「服務沒起來」
    是三種不同的成因，共用一個 "environment not ready" 訊息會讓使用者
    無從下手。
    """
    problems = []

    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception as e:  # ImportError 或 playwright 內部 import 失敗
        problems.append(
            f"playwright 不可用：{type(e).__name__}: {e}\n"
            f"    安裝：.venv/bin/pip install playwright"
        )

    if not os.path.exists(CHROME_PATH):
        problems.append(
            f"chromium 執行檔不存在：{CHROME_PATH}\n"
            f"    用 OPENSHELF_E2E_CHROME=<path> 指定，或安裝 playwright chromium"
        )

    try:
        cols = api_get("/api/collections", timeout=8)
        if not isinstance(cols, list):
            problems.append(
                f"線上服務 {BASE_URL} 有回應但 /api/collections 不是 list："
                f"{type(cols).__name__}"
            )
    except Exception as e:
        problems.append(
            f"線上服務不可達：{BASE_URL} —— {type(e).__name__}: {e}\n"
            f"    起服務：docker compose up -d openshelf   （port 8088）"
        )

    return problems


def pytest_collection_modifyitems(config, items):
    """opt-in 閘：**在收集階段**就把本目錄的 item 標成 skip。

    這一層存在的理由是一個實測到的陷阱，不是潔癖：

    **session 級 fixture 的 setup 排在 function 級 autouse fixture 之前。**
    原本這裡只有一個 function 級 autouse 的閘，於是：

        browser_ctx (session) → 真的發動了 chromium
            ↓
        _e2e_gate (function, autouse) → 這才 skip

    測試報告寫著 `skipped`，但 playwright 已經啟動、sync driver 已經在
    主執行緒裝上它自己的 event loop，後續所有 pytest-asyncio 測試全部死於
    `RuntimeError: Runner.run() cannot be called from a running event loop`
    （實測：37 failed）。同樣地，`live_job` 也會真的在線上服務造出一筆 job。

    也就是說，`skipped` 這個輸出同時代表了「沒跑」與「跑了並且造了副作用」
    兩種狀態——正是本包要消滅的那種兩態共用一個輸出。

    收集階段標記則沒有這個問題：被標上 skip 的 item **一個 fixture 都不會 setup**。

    這個 hook 寫在目錄級 conftest，但 pytest 會把**全部** item 傳進來，
    所以還是得自己濾路徑——不濾的話會把 `tests/` 底下 246 條一起 skip 掉。
    """
    if os.environ.get(E2E_FLAG) == "1":
        return

    here = os.path.dirname(os.path.abspath(__file__))
    marker = pytest.mark.skip(
        reason=(
            f"e2e opt-in：未設 {E2E_FLAG}=1。"
            f"本批需要真 chromium + 線上服務（{BASE_URL}），"
            f"故預設不執行；跑法見 tests/e2e/conftest.py"
        )
    )
    for item in items:
        path = os.path.abspath(str(getattr(item, "fspath", "")))
        if path == here or path.startswith(here + os.sep):
            item.add_marker(marker)


@pytest.fixture(scope="session")
def e2e_env():
    """環境探測，壞了就 **loud 失敗**。

    能走到這裡代表 `OPENSHELF_E2E=1` 已經設了（否則收集階段就 skip 掉了），
    也就是使用者**明確要求**跑 e2e。這時環境壞掉不可以 skip：
    skip 會讓「服務掛了」與「測試通過了」共用一個看起來安全的輸出。

    它是 session 級，且被 `browser_ctx` / `residue_baseline` 依賴，
    所以依賴圖保證它在任何 chromium 發動、任何線上寫入之前就先跑。
    """
    problems = _probe_environment()
    if problems:
        pytest.fail(
            f"{E2E_FLAG}=1 已啟用 e2e，但環境不完整（{len(problems)} 項）：\n"
            + "\n".join(f"  [{i + 1}] {p}" for i, p in enumerate(problems)),
            pytrace=False,
        )
    return True


@pytest.fixture(autouse=True)
def _e2e_gate(e2e_env):
    """確保每一條測試都過過環境閘（即使它沒有要 browser）。"""
    return e2e_env


@pytest.fixture(scope="session")
def browser_ctx(e2e_env):
    """啟動一個 session 級的 chromium。

    import 延遲到這裡，module 層看不到 playwright。
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            executable_path=CHROME_PATH,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def page(browser_ctx):
    """每條測試一個乾淨的 page（獨立 context，不共用 cookie/route）。"""
    ctx = browser_ctx.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    try:
        yield pg
    finally:
        ctx.close()


# --------------------------------------------------------------------------
# 殘留稽核：本批會在線上服務造資料，跑完必須清乾淨並**證明**清乾淨。
# --------------------------------------------------------------------------

JOB_TITLE = "PLAYWRIGHT前端實測書"


def count_residue():
    """回傳 (job 數, collection_item 總列數)。

    `collection_item` 沒有直接的計數 API，但 `/api/collections` 每筆帶
    `items_count`，其總和即為該表的列數。
    """
    jobs = api_get("/api/crawler/jobs")
    cols = api_get("/api/collections")
    return len(jobs), sum(int(c.get("items_count") or 0) for c in cols)


@pytest.fixture(scope="session")
def residue_baseline(e2e_env):
    """在造任何資料**之前**量一次基線。"""
    return count_residue()


@pytest.fixture(scope="session")
def live_job(residue_baseline):
    """造一筆註定下載失敗的 job（md5 是假的，鏡像解析必失敗）。

    finalizer 一律刪除，即使測試中途炸掉。已被別的測試刪掉時容忍 404——
    「已經不在了」與「刪除成功」在這裡是同一個目標狀態。
    """
    st, job = api_call(
        "/api/crawler/download",
        "POST",
        {
            "md5": "fe" + "0" * 30,
            "title": JOB_TITLE,
            "authors": "測試",
            "extension": "pdf",
        },
    )
    assert st == 200, f"造 job 失敗 status={st}"
    job_id = job["job_id"]
    try:
        yield job
    finally:
        try:
            api_call(f"/api/crawler/jobs/{job_id}", "DELETE")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
