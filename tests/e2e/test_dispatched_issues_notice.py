"""BR-20260821_020000 殘留② —— 「已派發 BR」提示列在**真實瀏覽器**裡的渲染。

## 這批補的是哪一格

020000 的主修復（`7c0ff48`）在前端加了三分支：`source_available === false` 顯示警示、
`total > 0` 顯示計數、其餘隱藏。當時的驗證是把函式本體抽出來用 `new Function` 執行，
證明的是**分支邏輯**——它跑在沒有 DOM 的環境裡，所以以下三件事一件都沒被證明：

  1. `loadDispatchedIssuesNotice` 在真實頁面上**真的會被呼叫**
     （`app.js:1667`，藏在 `loadLibgenMirrorsSettings` 裡，而後者又掛在
     設定 modal 的開啟流程 `app.js:1652` 上）
  2. `innerHTML` 塞進去的那段 HTML **真的會被瀏覽器渲染成可見文字**
  3. `style.display` 的三個值**真的會讓元素在視覺上出現/消失**
     （`display:none` 的元素 `innerText` 為空，這一格在 jsdom 或字串比對上驗不出來）

`route_hits` 斷言存在正是為了第 1 格：沒有它，「函式被呼叫但渲染錯了」與
「函式根本沒被呼叫」共用同一個輸出（提示列都不顯示）。

## 為什麼用 `page.route` 注入而不是打真的無掛載服務

`source_available: false` 需要一個**沒有掛載 `./issues`** 的後端。線上 8088 有掛載，
而測試不可以去改線上容器的掛載。所以本批攔截該端點注入回應。

這讓本批的範圍必須誠實界定：**本批測的是「前端拿到某個 payload 時渲染什麼」，
不是「後端真的讀不到目錄時前端渲染什麼」。** 後半段由 dispatcher 的容器實測負責
（獨立臨時容器移除掛載，實測 HTTP 200 + `source_available:false` + 容器 log 有
`log.error`）。兩段之間的接縫是 **payload 的形狀**，所以：

  * `UNAVAILABLE_PAYLOAD` 是從那次容器實測的 response body **逐欄位照抄**的，
    不是我手寫的假設（見常數上方註解記載的實測值）。
  * `test_live_endpoint_shape_matches_injected_payload` 會去打**真的線上端點**，
    斷言它回的 key 集合與注入的 payload 一致。若後端哪天改了欄位名，
    這條會紅——不然注入式測試會在後端已經改壞之後繼續綠燈。

## 控制組

`locator.count()` / `is_visible()` 的失敗模式是「選擇器寫錯 ⇒ 恆回 0/False」，
而 0/False 同時也是「元素真的不存在/真的隱藏」的正確答案——兩態共用一個輸出。
`test_control_*` 兩條分別反證：不存在的 id 真的回 0，而提示列在**該顯示的分支**
真的回 True，所以上面的 `is_visible() is False` 有鑑別力。

閘門與跳過語意見 `tests/e2e/conftest.py`。
"""
import json

import pytest

from tests.e2e.conftest import BASE_URL, api_get

ENDPOINT_GLOB = "**/api/settings/libgen-mirrors/issues*"
NOTICE_SEL = "#dispatchedIssuesNotice"

# 以下三份 payload 的 key 形狀來自 dispatcher 的容器實測（2026-08-21）：
#
#   無掛載容器 (docker run 同 image、無 -v issues、port 18088)
#     HTTP 200
#     {"total":0,"issues":[],"source_available":false,"source_path":"/app/issues"}
#
#   空目錄掛載容器 (port 18089)
#     {"total":0,"issues":[],"source_available":true,"source_path":"/app/issues"}
#
# 第三份 `LEGACY_PAYLOAD` 沒有實測來源，它刻意模擬**修復前的舊後端**（不回
# `source_available` 欄位）。它存在的理由是前端那句 `=== false` 的必要性：
# 若寫成 `!== true`，舊後端的 `undefined` 會被誤判成「來源不可用」而誤報。
UNAVAILABLE_PAYLOAD = {
    "total": 0,
    "issues": [],
    "source_available": False,
    "source_path": "/app/issues",
}
EMPTY_OK_PAYLOAD = {
    "total": 0,
    "issues": [],
    "source_available": True,
    "source_path": "/app/issues",
}
LEGACY_PAYLOAD = {"total": 0, "issues": []}
POPULATED_PAYLOAD = {
    "total": 3,
    "issues": [
        {"br_id": f"BR-TEST-{i}", "title": f"測試 BR {i}",
         "file_name": f"BR-TEST-{i}.md", "updated_at": 0, "path": "/app/issues"}
        for i in range(3)
    ],
    "source_available": True,
    "source_path": "/app/issues",
}


def open_settings_with_payload(page, payload):
    """開設定頁，把 issues 端點換成指定 payload，回傳觀察到的渲染狀態。

    回傳 dict 一定含 `route_hits`：它是「函式真的被呼叫了」的證據。
    """
    hits = {"n": 0}

    def handler(route):
        hits["n"] += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route(ENDPOINT_GLOB, handler)
    page.goto(BASE_URL + "/", wait_until="networkidle", timeout=30000)
    page.click("#openSettingsBtn")
    page.wait_for_timeout(2500)

    return {
        "route_hits": hits["n"],
        "display": page.evaluate(
            f"getComputedStyle(document.querySelector('{NOTICE_SEL}')).display"
        ),
        "visible": page.locator(NOTICE_SEL).is_visible(),
        "text": page.locator(NOTICE_SEL).inner_text().strip(),
        "html_len": page.evaluate(
            f"document.querySelector('{NOTICE_SEL}').innerHTML.length"
        ),
    }


# ---------------------------------------------------------------------------
# 前置：那個 DOM 節點在真實頁面上存在（不然底下每一條都在驗一個不存在的東西）
# ---------------------------------------------------------------------------

def test_notice_element_exists_in_real_dom(page):
    page.goto(BASE_URL + "/", wait_until="networkidle", timeout=30000)
    assert page.locator(NOTICE_SEL).count() == 1, (
        f"真實頁面應該剛好有一個 {NOTICE_SEL}，"
        f"實得 count={page.locator(NOTICE_SEL).count()}"
    )


def test_control_nonexistent_element_is_absent(page):
    """控制組：反證上一條的 count()==1 有鑑別力。"""
    page.goto(BASE_URL + "/", wait_until="networkidle", timeout=30000)
    assert page.locator("#zzzNoSuchNoticeElement").count() == 0


# ---------------------------------------------------------------------------
# 三分支 × 真實渲染
# ---------------------------------------------------------------------------

def test_source_unavailable_renders_visible_warning(page):
    """`source_available:false` ⇒ 提示列**可見**且文字指名是「未知」而非「無報告」。"""
    r = open_settings_with_payload(page, UNAVAILABLE_PAYLOAD)

    assert r["route_hits"] == 1, (
        "loadDispatchedIssuesNotice 沒有真的發出那個 fetch —— "
        f"route 攔截次數={r['route_hits']}。這代表 app.js:1667 的呼叫點沒被觸發，"
        "而不是渲染錯誤（兩者在畫面上都表現為『提示列沒出現』）。"
    )
    assert r["display"] == "block", f"display 應為 block，實得 {r['display']!r}"
    assert r["visible"] is True, "提示列在 source_available=false 時必須可見"
    assert "未知" in r["text"], (
        f"提示文字必須讓使用者看出這是「不知道」而非「沒有」，實得：{r['text']!r}"
    )
    assert "無報告" in r["text"], (
        f"提示文字必須明確對比掉「無報告」這個誤讀，實得：{r['text']!r}"
    )


def test_source_available_but_empty_renders_nothing(page):
    """正常空清單 ⇒ **不顯示**任何東西。

    這條與上一條互為對照：沒有它，「提示列永遠顯示」也會讓上一條通過，
    而那正是 BR 裡點名的鏡像病（把錯誤訊號變成常態雜訊 ⇒ 使用者學會忽略）。
    """
    r = open_settings_with_payload(page, EMPTY_OK_PAYLOAD)

    assert r["route_hits"] == 1, f"fetch 未發出，route_hits={r['route_hits']}"
    assert r["display"] == "none", f"display 應為 none，實得 {r['display']!r}"
    assert r["visible"] is False, "正常空清單不得顯示提示列"
    assert r["text"] == "", f"隱藏時不應有可見文字，實得 {r['text']!r}"


def test_legacy_backend_without_field_does_not_false_alarm(page):
    """舊後端不回 `source_available`（undefined）⇒ 走正常分支，不得誤報。

    這條鎖的是 `=== false` 而不是 `!== true`：後者會把 undefined 誤判成不可用。
    """
    r = open_settings_with_payload(page, LEGACY_PAYLOAD)

    assert r["route_hits"] == 1, f"fetch 未發出，route_hits={r['route_hits']}"
    assert r["visible"] is False, (
        "舊後端的 undefined 被誤判成「來源不可用」——"
        "這代表前端條件寫成了 `!== true` 之類的寬鬆比較"
    )


def test_populated_list_renders_count(page):
    """`total>0` ⇒ 顯示計數。

    這條同時是「`visible is False` 有鑑別力」的控制組：證明在該顯示的分支上
    `is_visible()` 真的會回 True，所以上面兩條的 False 不是選擇器壞掉。
    """
    r = open_settings_with_payload(page, POPULATED_PAYLOAD)

    assert r["route_hits"] == 1, f"fetch 未發出，route_hits={r['route_hits']}"
    assert r["visible"] is True, "total>0 時提示列必須可見"
    assert "3" in r["text"], f"提示文字應帶出數量 3，實得 {r['text']!r}"


def test_three_branches_are_mutually_distinguishable(page):
    """三個分支在**使用者看得到的層面**上必須是三個不同結果。

    分開斷言每條分支還不夠：若三者剛好都渲染成同一個樣子，上面的個別斷言
    仍可能各自通過（例如文字都含「未知」）。這條直接比對三元組。

    ⚠ 相異性比對不讓空字串以外的欄位獨自承擔：簽章取 (visible, display, text)，
    而 text 在隱藏分支必為空——所以真正提供鑑別力的是 visible/display，
    text 只是附加資訊。若只用 text 比對，「沒有訊息」與「有不同訊息」會湊出假的相異數。
    """
    sigs = []
    for name, payload in [
        ("unavailable", UNAVAILABLE_PAYLOAD),
        ("empty_ok", EMPTY_OK_PAYLOAD),
        ("populated", POPULATED_PAYLOAD),
    ]:
        r = open_settings_with_payload(page, payload)
        assert r["route_hits"] == 1, f"[{name}] fetch 未發出"
        sigs.append((name, (r["visible"], r["display"], r["text"])))
        page.unroute(ENDPOINT_GLOB)

    distinct = {s for _, s in sigs}
    assert len(distinct) == 3, (
        "三個分支必須在畫面上互相可分，實得 "
        f"{len(distinct)} 種：{[(n, s) for n, s in sigs]}"
    )


# ---------------------------------------------------------------------------
# 接縫：注入的 payload 形狀必須與真後端一致
# ---------------------------------------------------------------------------

def test_live_endpoint_shape_matches_injected_payload():
    """真後端回的 key 集合 == 我注入的 key 集合。

    上面每一條都是「餵前端一份我自己寫的 JSON」。若後端哪天改了欄位名
    （例如 `source_available` → `mount_ok`），那些測試會**繼續全綠**，
    因為它們根本沒碰後端——注入式測試的固有盲點。這條把接縫鎖住。

    線上服務有掛載 `./issues`，所以只驗得到 `source_available: true` 這一側；
    `false` 那一側的實測由容器實測負責，本條無法涵蓋（誠實標出）。
    """
    live = api_get("/api/settings/libgen-mirrors/issues")

    assert isinstance(live, dict), f"端點應回 dict，實得 {type(live).__name__}"
    assert set(live.keys()) == set(EMPTY_OK_PAYLOAD.keys()), (
        "真後端的欄位集合與本檔注入的 payload 不一致——"
        "注入式測試已經在驗一個不存在的契約。\n"
        f"  真後端: {sorted(live.keys())}\n"
        f"  本檔假設: {sorted(EMPTY_OK_PAYLOAD.keys())}"
    )
    assert live["source_available"] is True, (
        "線上服務應該有掛載 issues/。若這裡是 False，"
        "代表線上掛載真的掉了——那是要處理的事實，不是測試問題。"
    )
