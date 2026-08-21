"""BR-20260821_060000：`.tmp_*` 孤兒檔的 piggyback lazy sweeper。

這批測試的設計重點在於：**證明掃除真的在工作**，而不只是「沒有拋例外」。

線上當下孤兒數為 0，所以這支掃除上線後很可能永遠 `removed=0`。
一支永遠不工作的掃除可以永遠通過一個只斷言 `removed == 0` 的測試——
因此每個測試都必須把 `scanned`（控制組）一起斷言，或直接製造真的孤兒來驗證刪除。
"""

import logging
import os
import time
from pathlib import Path

import pytest

from app.storage.manager import (
    ORPHAN_TMP_GLOB,
    ORPHAN_TMP_THRESHOLD_SECONDS,
    SWEEP_INTERVAL_SECONDS,
    StorageManager,
)


@pytest.fixture(autouse=True)
def _reset_sweep_state():
    """`_last_sweep` / `_ensured_dirs` 是 class 層級狀態，會跨測試殘留。

    不清會讓「這個測試自己被節流擋掉」與「掃除壞了」共用同一個輸出。
    """
    StorageManager._last_sweep.clear()
    StorageManager._ensured_dirs.clear()
    yield
    StorageManager._last_sweep.clear()
    StorageManager._ensured_dirs.clear()


def _make_tmp(directory: Path, name: str, age_seconds: float) -> Path:
    """在 directory 下造一個 age_seconds 秒前寫的 `.tmp_*` 檔。"""
    p = directory / name
    p.write_bytes(b"orphan payload")
    past = time.time() - age_seconds
    os.utime(p, (past, past))
    return p


# --------------------------------------------------------------------------
# 判準本體：mtime 閾值
# --------------------------------------------------------------------------


def test_sweep_removes_orphan_older_than_threshold(tmp_path):
    storage = StorageManager(base_dir=tmp_path)
    old = _make_tmp(storage.raw_dir, "deadbeef.tmp_999", ORPHAN_TMP_THRESHOLD_SECONDS + 60)

    stats = storage.sweep_orphan_tmp(storage.raw_dir)

    assert stats == {"scanned": 1, "removed": 1, "kept": 0}
    assert not old.exists()


def test_sweep_keeps_orphan_younger_than_threshold(tmp_path):
    """剛寫的 `.tmp_*` 可能是**另一台主機正在寫**的檔，絕不能刪。"""
    storage = StorageManager(base_dir=tmp_path)
    fresh = _make_tmp(storage.raw_dir, "cafebabe.tmp_1", 5)

    stats = storage.sweep_orphan_tmp(storage.raw_dir)

    # scanned=1 是控制組：證明「沒刪」是因為它還新，不是因為根本沒掃到。
    assert stats == {"scanned": 1, "removed": 0, "kept": 1}
    assert fresh.exists()


def test_sweep_boundary_exactly_at_threshold_is_kept(tmp_path):
    """邊界取 `>= cutoff` 保留：兩個方向的代價不對稱，往「留」的一邊偏。"""
    storage = StorageManager(base_dir=tmp_path)
    now = 1_000_000.0
    p = storage.raw_dir / "edge.tmp_7"
    p.write_bytes(b"x")
    exact = now - ORPHAN_TMP_THRESHOLD_SECONDS
    os.utime(p, (exact, exact))

    stats = storage.sweep_orphan_tmp(storage.raw_dir, now=now)

    assert stats == {"scanned": 1, "removed": 0, "kept": 1}
    assert p.exists()


def test_sweep_mixed_population_separates_old_from_new(tmp_path):
    storage = StorageManager(base_dir=tmp_path)
    old_a = _make_tmp(storage.parsed_dir, "a.tmp_11", ORPHAN_TMP_THRESHOLD_SECONDS * 2)
    old_b = _make_tmp(storage.parsed_dir, "b.tmp_12", ORPHAN_TMP_THRESHOLD_SECONDS + 1)
    fresh = _make_tmp(storage.parsed_dir, "c.tmp_13", 1)

    stats = storage.sweep_orphan_tmp(storage.parsed_dir)

    assert stats == {"scanned": 3, "removed": 2, "kept": 1}
    assert not old_a.exists()
    assert not old_b.exists()
    assert fresh.exists()


def test_sweep_never_touches_non_tmp_files(tmp_path):
    """真正的成品檔即使很舊也絕不能碰——這是資料遺失的方向。"""
    storage = StorageManager(base_dir=tmp_path)
    real = storage.raw_dir / "abcdef.pdf"
    real.write_bytes(b"a real book")
    ancient = time.time() - ORPHAN_TMP_THRESHOLD_SECONDS * 100
    os.utime(real, (ancient, ancient))

    stats = storage.sweep_orphan_tmp(storage.raw_dir)

    # scanned=0 在這裡是**預期**：pattern 刻意不該命中成品檔。
    assert stats == {"scanned": 0, "removed": 0, "kept": 0}
    assert real.exists()
    assert real.read_bytes() == b"a real book"


def test_scanned_distinguishes_no_orphans_from_unreadable_dir(tmp_path):
    """`removed=0` 的兩種意思必須可區分（設計要求④）。"""
    storage = StorageManager(base_dir=tmp_path)

    # 情況 A：目錄好好的，只是沒有孤兒。
    empty = storage.sweep_orphan_tmp(storage.raw_dir)

    # 情況 B：目錄根本不存在 ⇒ 掃不到任何東西。
    missing = storage.sweep_orphan_tmp(tmp_path / "no_such_dir")

    assert empty == {"scanned": 0, "removed": 0, "kept": 0}
    assert missing == {"scanned": 0, "removed": 0, "kept": 0}

    # 兩者 scanned 都是 0，所以**光看回傳值分不出來**——這正是為何目錄不可讀時
    # 必須記 warning log（見 test_unlistable_directory_logs_warning）。
    # 控制組：有孤兒時 scanned 必須非 0，證明計數器有鑑別力。
    _make_tmp(storage.raw_dir, "x.tmp_5", 1)
    assert storage.sweep_orphan_tmp(storage.raw_dir)["scanned"] == 1


# --------------------------------------------------------------------------
# 設計要求⑤：失敗不得阻斷主流程
# --------------------------------------------------------------------------


def test_unlistable_directory_does_not_raise_and_logs(tmp_path, caplog):
    storage = StorageManager(base_dir=tmp_path)
    ghost = tmp_path / "vanished"

    with caplog.at_level(logging.WARNING, logger="app.storage.manager"):
        stats = storage.sweep_orphan_tmp(ghost)

    assert stats == {"scanned": 0, "removed": 0, "kept": 0}
    assert any("無法列舉目錄" in r.message for r in caplog.records)


def test_unlink_failure_is_counted_as_kept_not_crash(tmp_path, monkeypatch, caplog):
    """檔案在 glob 與 unlink 之間消失是正常競態，不得讓入庫失敗。"""
    storage = StorageManager(base_dir=tmp_path)
    _make_tmp(storage.raw_dir, "racy.tmp_3", ORPHAN_TMP_THRESHOLD_SECONDS + 60)

    def boom(self):
        raise OSError("Stale file handle")

    monkeypatch.setattr(Path, "unlink", boom)

    with caplog.at_level(logging.WARNING, logger="app.storage.manager"):
        stats = storage.sweep_orphan_tmp(storage.raw_dir)

    assert stats == {"scanned": 1, "removed": 0, "kept": 1}
    assert any("回收失敗" in r.message for r in caplog.records)


def test_save_still_succeeds_when_sweep_explodes(tmp_path, monkeypatch):
    """整支掃除炸掉也不能害使用者的入庫失敗——這是最重要的一條。"""
    storage = StorageManager(base_dir=tmp_path)

    def boom(self, pattern):
        raise OSError("NFS server not responding")

    monkeypatch.setattr(Path, "glob", boom)

    rel, sha256, md5, size = storage.save_raw_bytes(b"payload", "pdf")
    assert storage.resolve_path(rel).read_bytes() == b"payload"

    rel_md = storage.save_parsed_markdown("wk_ok", "# still works")
    assert storage.get_parsed_content("wk_ok") == "# still works"


# --------------------------------------------------------------------------
# 設計要求③：節流
# --------------------------------------------------------------------------


def test_throttle_skips_second_call_within_interval(tmp_path):
    storage = StorageManager(base_dir=tmp_path)

    first = storage._maybe_sweep(storage.raw_dir)
    second = storage._maybe_sweep(storage.raw_dir)

    # None 與 dict 是不同的東西：「被節流跳過」不得與「掃了但沒看到檔」共用輸出。
    assert first == {"scanned": 0, "removed": 0, "kept": 0}
    assert second is None


def test_throttle_expires_after_interval(tmp_path, monkeypatch):
    storage = StorageManager(base_dir=tmp_path)
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

    assert storage._maybe_sweep(storage.raw_dir) is not None
    clock["t"] += SWEEP_INTERVAL_SECONDS - 1
    assert storage._maybe_sweep(storage.raw_dir) is None
    clock["t"] += 2
    assert storage._maybe_sweep(storage.raw_dir) is not None


def test_throttle_is_per_directory(tmp_path):
    """raw 掃過不應該讓 parsed 也被跳過——兩個目錄各自計時。"""
    storage = StorageManager(base_dir=tmp_path)

    assert storage._maybe_sweep(storage.raw_dir) is not None
    assert storage._maybe_sweep(storage.parsed_dir) is not None
    assert storage._maybe_sweep(storage.raw_dir) is None


def test_throttle_state_is_class_level_not_instance_level(tmp_path):
    """`StorageManager` 不是 singleton，每個 `Depends` 都新建一個。

    節流狀態若掛在 instance 上，每次寫入都會拿到全新的空 dict ⇒ 節流形同虛設。
    """
    a = StorageManager(base_dir=tmp_path)
    b = StorageManager(base_dir=tmp_path)

    assert a._maybe_sweep(a.raw_dir) is not None
    assert b._maybe_sweep(b.raw_dir) is None


def test_throttled_call_does_not_even_enumerate(tmp_path, monkeypatch):
    """節流命中時連目錄列舉都不做——這是節流唯一的成本理由。

    這裡插樁的是 `os.scandir`（實作真正用的列舉器），不是 `Path.glob`。
    插錯函式會讓計數器永遠是 0，於是「真的沒列舉」與「我量錯地方」共用同一個輸出——
    所以底下的控制組是必要的：它證明這個計數器在該非空時真的會非空。
    """
    storage = StorageManager(base_dir=tmp_path)
    storage._maybe_sweep(storage.raw_dir)

    calls = []
    real_scandir = os.scandir

    def counting_scandir(path):
        calls.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", counting_scandir)

    assert storage._maybe_sweep(storage.raw_dir) is None
    assert calls == []

    # 控制組：解除節流後列舉必須真的發生，證明計數器有鑑別力。
    StorageManager._last_sweep.clear()
    assert storage._maybe_sweep(storage.raw_dir) is not None
    assert calls == [str(storage.raw_dir)]


# --------------------------------------------------------------------------
# 設計要求①：掛載點在寫入路徑上
# --------------------------------------------------------------------------


def test_save_raw_bytes_triggers_sweep(tmp_path):
    storage = StorageManager(base_dir=tmp_path)
    orphan = _make_tmp(storage.raw_dir, "old.tmp_404", ORPHAN_TMP_THRESHOLD_SECONDS + 60)

    storage.save_raw_bytes(b"new book bytes", "pdf")

    assert not orphan.exists()


def test_save_parsed_markdown_triggers_sweep(tmp_path):
    storage = StorageManager(base_dir=tmp_path)
    orphan = _make_tmp(storage.parsed_dir, "old.tmp_405", ORPHAN_TMP_THRESHOLD_SECONDS + 60)

    storage.save_parsed_markdown("wk_abc", "# content")

    assert not orphan.exists()


def test_constructor_does_not_sweep(tmp_path):
    """建構子每個 API 請求都會走（BR-20260821_040000 機制①），絕不能掛掃除。"""
    storage = StorageManager(base_dir=tmp_path)
    orphan = _make_tmp(storage.raw_dir, "old.tmp_406", ORPHAN_TMP_THRESHOLD_SECONDS + 60)
    StorageManager._last_sweep.clear()

    StorageManager(base_dir=tmp_path)
    StorageManager(base_dir=tmp_path).ensure_directories()

    # 控制組：這個檔夠舊，一旦真的掃就會被刪——所以它還在，證明沒掃。
    assert orphan.exists()
    assert storage.sweep_orphan_tmp(storage.raw_dir)["removed"] == 1


# --------------------------------------------------------------------------
# glob pattern 涵蓋性：`with_suffix` 對含 `.` 的 work_id 行為
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "work_id",
    [
        "wk_0123456789abcdef",   # 正常 uuid 形狀
        "wk.with.dots",          # 含點：with_suffix 只換最後一段
        "trailing.",             # 尾點
        "a",                     # 極短
    ],
)
def test_tmp_name_from_any_work_id_is_matched_by_glob(tmp_path, work_id):
    """實際用 `save_parsed_markdown` 走的那條命名路徑造檔，證明 glob 抓得到。

    若 `with_suffix` 對某種 work_id 造出不符 `*.tmp_*` 的名字，
    那個形狀的孤兒就會永遠回收不到——而測試會靜默通過。
    """
    storage = StorageManager(base_dir=tmp_path)
    target = storage.base_dir / f"parsed/{work_id}.md"
    tmp_name = target.with_suffix(f".tmp_{os.getpid()}").name

    orphan = _make_tmp(storage.parsed_dir, tmp_name, ORPHAN_TMP_THRESHOLD_SECONDS + 60)
    stats = storage.sweep_orphan_tmp(storage.parsed_dir)

    assert stats["scanned"] == 1, f"glob 漏掉了 {tmp_name}"
    assert stats["removed"] == 1
    assert not orphan.exists()
