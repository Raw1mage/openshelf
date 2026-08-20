"""BR-20260820_223000 程式層迴歸鎖：BR 落點失效不得靜默帶過。

失效形狀（實測，非假想）：容器內 `/app/issues` 沒有掛載，而
`MirrorValidator.__init__` 的 `mkdir(parents=True, exist_ok=True)` 每次都幫它
造出來，於是自動產生的診斷 BR 全寫進 rebuild 即蒸發的 ephemeral 目錄——
host 端 `issues/` 永遠看不到，前端清單恆為 `total=0`，**零錯誤、零 log**。
「真的沒有 BR」與「BR 寫到別的地方去了」共用同一個輸出。

部署層（compose 掛載）與測試層（test_container_mount_contract.py）已補。
本檔鎖的是**程式層**：掛載哪天被移除，程式自己必須出聲。
"""

import logging
from pathlib import Path

import pytest

from app.crawler.validator import BRDispatchTargetMissing, MirrorValidator


# ---------------------------------------------------------------- 控制組


def test_explicit_issues_dir_is_still_created_silently(tmp_path):
    """控制組（也是相容性鎖）：顯式指定落點時，照建、不出聲。

    這一條的存在是為了證明本次修復**沒有**退化成「一律不建目錄」。
    tests/ 內既有 fixture（test_settings_and_validator.py、
    test_event_loop_blocking_cdf.py）全部依賴
    `MirrorValidator(issues_dir=tmp_path / "issues")` 能直接建構並寫入。
    若本條掛掉，代表修復把測試環境一起鎖死了。
    """
    target = tmp_path / "nested" / "issues"
    assert not target.exists(), "前提失效：測試起點目錄不該已存在"

    validator = MirrorValidator(issues_dir=target)

    assert target.is_dir(), "顯式指定的落點必須照建"
    assert validator.issues_dir_is_explicit is True
    assert validator.issues_dir_missing is False


def test_dispatch_br_writes_when_dir_exists(tmp_path):
    """控制組：目錄存在時 dispatch_br 必須正常落檔。

    沒有這一條，下面「缺目錄會 raise」的測試在「dispatch_br 永遠 raise」時
    也會通過——那是一個壞掉的實作偽裝成修好的實作。
    """
    issues_dir = tmp_path / "issues"
    validator = MirrorValidator(issues_dir=issues_dir)

    br_id, br_path = validator.dispatch_br(
        "https://probe.example.org", 200, "<html>snippet</html>", "測試用失敗原因")

    assert br_path.exists(), "目錄存在時必須真的寫出檔案"
    assert br_id.startswith("BR-")
    assert br_path.parent == issues_dir
    content = br_path.read_text(encoding="utf-8")
    assert "https://probe.example.org" in content


# ------------------------------------------------- 主鎖 1：預設落點不得自動建立


def test_default_issues_dir_is_not_silently_created(monkeypatch, tmp_path, caplog):
    """主鎖：走預設落點且目錄不存在時，**不得**自動建出來，且必須 log.error。

    這是 BR-223000 那一格。把 `mkdir(parents=True, exist_ok=True)` 還原，
    這條會死在「目錄竟然被建出來了」。

    做法：把 validator 模組的 __file__ 錨點搬到 tmp_path 下的假 package 結構，
    讓 `Path(__file__).parent.parent.parent / "issues"` 解析到一個確定不存在的
    位置——不動真實 repo 的 issues/。
    """
    import app.crawler.validator as vmod

    fake_pkg = tmp_path / "fakeroot" / "app" / "crawler"
    fake_pkg.mkdir(parents=True)
    monkeypatch.setattr(vmod, "__file__", str(fake_pkg / "validator.py"))

    expected_dir = tmp_path / "fakeroot" / "issues"
    assert not expected_dir.exists(), "前提失效：預期落點不該已存在"

    with caplog.at_level(logging.ERROR, logger=vmod.__name__):
        validator = MirrorValidator()

    # 1. 沒有被靜默建出來——這是 BR 的核心
    assert not expected_dir.exists(), (
        f"落點 {expected_dir} 被自動建立了。這正是 BR-223000 的失效形狀："
        "掛載失效被 mkdir 抹平，診斷 BR 寫進 rebuild 即消失的目錄。"
    )

    # 2. 解析結果仍是那個位置（證明上面那條不是因為路徑算錯才「沒被建」）
    assert validator.issues_dir == expected_dir, (
        "落點解析結果與預期不符——上面的『沒被建立』可能只是因為它建到別處去了，"
        "而不是因為修復生效。"
    )

    # 3. 出聲了，而且是 ERROR 等級
    assert validator.issues_dir_missing is True
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "落點缺失必須 log.error 出聲，不得靜默"
    assert any(str(expected_dir) in r.getMessage() for r in error_records), (
        "log 必須指名是哪一個路徑失效，否則使用者無法據此排查掛載"
    )


def test_default_issues_dir_present_is_quiet(monkeypatch, tmp_path, caplog):
    """控制組：預設落點存在時不得誤報。

    沒有這一條，上面的斷言在「無條件 log.error」的實作下也會通過——
    一個對所有情況都尖叫的偵測器沒有鑑別力。
    """
    import app.crawler.validator as vmod

    fake_pkg = tmp_path / "fakeroot" / "app" / "crawler"
    fake_pkg.mkdir(parents=True)
    (tmp_path / "fakeroot" / "issues").mkdir()
    monkeypatch.setattr(vmod, "__file__", str(fake_pkg / "validator.py"))

    with caplog.at_level(logging.ERROR, logger=vmod.__name__):
        validator = MirrorValidator()

    assert validator.issues_dir_missing is False
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "落點正常時不得發出 ERROR——否則訊號會被雜訊淹沒而失去意義"
    )


# ------------------------------------------------- 主鎖 2：寫入當下不得靜默建立


def test_dispatch_br_raises_when_dir_missing(tmp_path, caplog):
    """主鎖：寫入當下落點不存在時 raise 具名例外，且不留下任何檔案。

    `__init__` 的檢查只看得到建構那一刻。掛載可能在建構後、寫入前消失；
    更關鍵的是——就算 __init__ 已經 log 過，若寫入本身照樣進行，
    寫入行為仍然是靜默的。

    用具名例外而非裸 FileNotFoundError：後者與「某個無關檔案不見了」
    共用同一個型別，呼叫端無法只針對本情況處置。
    """
    import app.crawler.validator as vmod

    issues_dir = tmp_path / "issues"
    validator = MirrorValidator(issues_dir=issues_dir)
    assert issues_dir.is_dir(), "前提：建構時目錄應存在"

    # 模擬掛載在建構後消失
    issues_dir.rmdir()
    assert not issues_dir.exists()

    with caplog.at_level(logging.ERROR, logger=vmod.__name__):
        with pytest.raises(BRDispatchTargetMissing):
            validator.dispatch_br(
                "https://probe.example.org", 200, "<html>x</html>", "測試用失敗原因")

    assert not issues_dir.exists(), (
        "dispatch_br 竟然把落點目錄重建了——那正是要修掉的靜默行為"
    )
    assert [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "放棄寫入必須 log.error 出聲"
    )
