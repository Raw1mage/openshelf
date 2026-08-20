"""BR-20260821_020000 迴歸鎖：讀取端不得讓「來源目錄不可用」與「真的沒有 BR」共用同一個輸出。

本檔鎖住的**不是** total 的值——兩種狀態的 total 都是 0，那正是本 BR 的病灶。
鎖的是「兩者可被區分」這件事本身：必須存在一個獨立欄位，在兩種狀態下取不同值。

失效形狀（修復前）：
    issues/ 不存在  → {"total": 0, "issues": []}  HTTP 200，零 log
    issues/ 存在但空 → {"total": 0, "issues": []}  HTTP 200，零 log
    ↑ 位元組完全相同，使用者與程式都無從分辨。

所以下面每一條斷言都刻意「成對」出現：只斷言其中一態會留下同一個漏洞——
若某天有人讓兩態都回 source_available=False，單邊斷言仍會全綠。
"""

import importlib
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.api.settings_routes as settings_routes
from app.main import app


ENDPOINT = "/api/settings/libgen-mirrors/issues"


@pytest.fixture
def client():
    return TestClient(app)


def _point_issues_dir_at(monkeypatch, target: Path):
    """讓 list_dispatched_issues 的路徑解析指向 target。

    該函式用 `Path(__file__).parent.parent.parent / "issues"` 就地計算，沒有可注入
    的參數，所以這裡改寫模組全域的 Path，只在被測模組命名空間內生效。
    """

    # 被測函式算的是 Path(__file__).parent.parent.parent / "issues"，
    # 也就是往上三層再進 issues。所以假的模組位置必須是 target/app/api/x.py，
    # 三層 parent 才會回到 target 本身。
    fake_module_file = target / "app" / "api" / "settings_routes.py"
    monkeypatch.setattr(settings_routes, "__file__", str(fake_module_file))

    # 自證：確認這個 helper 真的把解析結果導到我們期待的位置。
    # 少了這行，helper 算錯一層時所有測試會以「實作壞了」的形式失敗，
    # 而真正壞的是測試自己——那正是缺席態與失敗態共用輸出的另一種形式。
    resolved = Path(fake_module_file).parent.parent.parent / "issues"
    assert resolved == target / "issues", (
        f"helper 自身算錯：解析到 {resolved}，期待 {target / 'issues'}"
    )


# --------------------------------------------------------------------------
# 控制組：先證明這些測試的探針有鑑別力
# --------------------------------------------------------------------------

def test_control_endpoint_is_reachable_at_all(client):
    """控制組：端點真的在，且真的回 JSON。

    沒有這條，下面的測試在「路由根本沒註冊 → 404 → .json() 取到錯誤 body」時，
    可能以令人安心的方式失敗（或更糟，若斷言寫成 .get(...) 形式而靜默通過）。
    """
    res = client.get(ENDPOINT)
    assert res.status_code == 200, f"端點不可達，後續斷言全部無鑑別力：{res.status_code}"
    body = res.json()
    assert "total" in body and "issues" in body, f"回應形狀不是預期的清單結構：{body}"


def test_control_negative_route_returns_404(client):
    """負控制組：證明 TestClient 對不存在的路由真的會回 404。

    若這條也回 200，代表有 catch-all 路由，上面那條的 200 就不構成證據。
    """
    res = client.get("/api/settings/zzz_not_a_route")
    assert res.status_code == 404, (
        f"不存在的路由沒回 404（得到 {res.status_code}）——"
        "代表本檔所有以 status_code 為據的斷言都失去鑑別力"
    )


# --------------------------------------------------------------------------
# 主鎖：兩態必須可區分
# --------------------------------------------------------------------------

def test_dir_exists_but_empty_reports_source_available_true(client, monkeypatch, tmp_path):
    """狀態一：目錄在、但一份 BR 都沒有 → total=0 且 source_available=True。"""
    (tmp_path / "issues").mkdir()
    _point_issues_dir_at(monkeypatch, tmp_path)

    res = client.get(ENDPOINT)
    assert res.status_code == 200
    body = res.json()

    assert body["total"] == 0, f"空目錄應回 total=0：{body}"
    assert body["source_available"] is True, (
        f"目錄存在且可讀，source_available 必須為 True，實得 {body.get('source_available')!r}"
    )


def test_dir_missing_reports_source_available_false(client, monkeypatch, tmp_path):
    """狀態二：目錄根本不存在 → total 同樣是 0，但 source_available=False。"""
    # 刻意不建立 tmp_path / "issues"
    _point_issues_dir_at(monkeypatch, tmp_path)

    res = client.get(ENDPOINT)
    assert res.status_code == 200
    body = res.json()

    assert body["total"] == 0, f"目錄不存在時仍回 total=0（本 BR 不改這點）：{body}"
    assert body["source_available"] is False, (
        f"目錄不存在，source_available 必須為 False，實得 {body.get('source_available')!r}"
    )


def test_the_two_states_are_actually_distinguishable(client, monkeypatch, tmp_path):
    """本 BR 的核心斷言：兩態的回應不得位元組等價。

    上面兩條各自斷言一邊；這條直接把兩個回應擺在一起比對。
    缺這條的話，若有人讓**兩態都**回 source_available=False，
    上面兩條會一過一敗，但「可區分性」這個真正的保證沒有被單獨鎖住。
    """
    missing_dir_root = tmp_path / "case_missing"
    missing_dir_root.mkdir()
    _point_issues_dir_at(monkeypatch, missing_dir_root)
    body_missing = client.get(ENDPOINT).json()

    empty_dir_root = tmp_path / "case_empty"
    (empty_dir_root / "issues").mkdir(parents=True)
    _point_issues_dir_at(monkeypatch, empty_dir_root)
    body_empty = client.get(ENDPOINT).json()

    # 前提：兩者的 total 確實相同——證明 total 本身無鑑別力，
    # 從而證明下面的不等式是靠新欄位撐起來的，不是靠別的差異。
    assert body_missing["total"] == body_empty["total"] == 0, (
        f"前提不成立：兩態 total 不同（{body_missing['total']} vs {body_empty['total']}），"
        "本測試失去它要證明的東西"
    )
    assert body_missing["issues"] == body_empty["issues"] == [], "前提不成立：issues 不同"

    assert body_missing["source_available"] != body_empty["source_available"], (
        "兩態回應無法區分——這正是 BR-20260821_020000 的失效形狀本身：\n"
        f"  目錄不存在: {body_missing}\n"
        f"  目錄存在但空: {body_empty}"
    )


def test_missing_dir_emits_log_error_naming_the_path(client, monkeypatch, tmp_path, caplog):
    """目錄不可用時必須 log.error，且訊息要指名是哪一個路徑。

    只斷言「有 ERROR」不夠：一則不含路徑的錯誤訊息無法據以排查，
    等於把「哪裡失效」這格留白。
    """
    _point_issues_dir_at(monkeypatch, tmp_path)

    with caplog.at_level(logging.ERROR, logger="app.api.settings_routes"):
        res = client.get(ENDPOINT)

    assert res.status_code == 200
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "目錄不可用卻沒有任何 ERROR log——訊號完全不存在"

    message = errors[0].getMessage()
    expected_path = str(tmp_path / "issues")
    assert expected_path in message, (
        f"log 沒指名失效路徑，無法據以排查。\n  期待含: {expected_path}\n  實得: {message}"
    )


def test_healthy_dir_does_not_emit_log_error(client, monkeypatch, tmp_path, caplog):
    """反向鎖：正常情況不得吐 ERROR。

    缺這條的話，一個「無論如何都 log.error」的實作會讓上一條全綠，
    而那種實作等於把錯誤訊號變成常態雜訊——使用者學會忽略它之後，
    真的失效時同樣看不見（換一種形式的同一個病）。
    """
    (tmp_path / "issues").mkdir()
    _point_issues_dir_at(monkeypatch, tmp_path)

    with caplog.at_level(logging.ERROR, logger="app.api.settings_routes"):
        res = client.get(ENDPOINT)

    assert res.status_code == 200
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, f"目錄正常卻吐了 ERROR，訊號被稀釋成雜訊：{[r.getMessage() for r in errors]}"


def test_path_exists_but_is_a_file_is_also_unavailable(client, monkeypatch, tmp_path):
    """邊界：路徑存在但不是目錄，同樣算不可用。

    `.exists()` 對一個同名檔案會回 True，讓後續 scandir 直接炸成 500。
    改用 `.is_dir()` 才涵蓋這格——這也是欄位不叫 mount_ok 的理由之一：
    這種情況跟「掛載」毫無關係。
    """
    (tmp_path / "issues").write_text("我是檔案不是目錄", encoding="utf-8")
    _point_issues_dir_at(monkeypatch, tmp_path)

    res = client.get(ENDPOINT)
    assert res.status_code == 200, (
        f"路徑是檔案時端點爆掉（{res.status_code}），而不是回報不可用"
    )
    body = res.json()
    assert body["source_available"] is False, (
        f"路徑存在但不是目錄，必須視為不可用，實得 {body.get('source_available')!r}"
    )


def test_source_path_is_reported_in_both_states(client, monkeypatch, tmp_path):
    """source_path 兩態都要有——它是排查的落腳點，不能只在失敗時出現。

    只在失敗時回傳的話，使用者無從確認「正常時它到底指到哪」，
    也就無法比對出路徑何時被改掉。
    """
    (tmp_path / "issues").mkdir()
    _point_issues_dir_at(monkeypatch, tmp_path)
    ok_body = client.get(ENDPOINT).json()

    other_root = tmp_path / "gone"
    other_root.mkdir()
    _point_issues_dir_at(monkeypatch, other_root)
    bad_body = client.get(ENDPOINT).json()

    assert ok_body["source_path"] == str(tmp_path / "issues")
    assert bad_body["source_path"] == str(other_root / "issues")
