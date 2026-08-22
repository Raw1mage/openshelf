"""BR-20260821_040000 機制②：下載暫存必須寫在本地檔案系統，完成後才搬上 NAS。

這個檔鎖住三件事，缺一都會讓修復靜默退化：

  1. `.part` 落在 `staging_dir`（本地 ext4）而**不是** `raw_dir`（NAS）
  2. 跨檔案系統搬移真的能成功——`os.replace` 跨裝置是 **EXDEV 直接失敗**，
     不是「退化成整檔複製」（BR 原文的說法已被實測推翻，見 T1/T2）
  3. 孤兒 `.part` 檔有回收路徑——本地暫存區只有 530G，不是 42T 的 NAS

每一組斷言都配一個控制組：一個「該通過」的正向樣本證明測法有鑑別力，
一個「該失敗」的負向樣本證明斷言不是恆真的裝飾品。
"""

import errno
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.crawler.download_worker import DownloadJob, DownloadWorker
from app.storage.manager import StorageManager


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def _make_worker(tmp_path: Path) -> DownloadWorker:
    """建一個只有 storage 是真的、其餘都被替身掉的 worker。

    刻意不用真的 `IngestionPipeline`（它會開 DB、載 PDF extractor）——本檔
    測的是檔案落點與搬移，那些相依項只會讓失敗原因變得不可讀。
    """
    storage = StorageManager(base_dir=tmp_path)
    pipeline = MagicMock()
    pipeline.storage = storage
    pipeline.dao = MagicMock()
    worker = DownloadWorker(pipeline=pipeline, resolver=MagicMock())
    return worker


def _job(job_id: str = "job_abc", md5: str = "d41d8cd9") -> DownloadJob:
    return DownloadJob(job_id=job_id, md5=md5, title="T", extension="pdf")


# --------------------------------------------------------------------------
# 1. 落點：.part 在 staging_dir 不在 raw_dir
# --------------------------------------------------------------------------

def test_staging_dir_is_under_db_dir_not_base_dir(tmp_path):
    """暫存區必須在 `db_dir` 底下。

    這不是風格選擇：實測容器內只有 `/data/raw`(nfs4)、`/data/parsed`(nfs4)、
    `/data/db`(ext4) 三條 bind-mount，`/data` **本身是 overlay**。落在
    `base_dir/staging` 會在容器 rebuild 時整個消失，讓跨重啟續傳靜默退化成
    整檔重下——功能還在、行為變了、沒有任何錯誤訊號。
    """
    storage = StorageManager(base_dir=tmp_path)
    assert storage.staging_dir == storage.db_dir / "staging", (
        f"暫存區落點不在 db_dir 底下：{storage.staging_dir}。"
        "只有 db_dir 同時是本地 ext4 且被 bind-mount（跨 rebuild 持久）。"
    )
    # 控制組：證明上面的相等判斷有鑑別力（不是兩個都是 None 之類的恆真）。
    assert storage.staging_dir != storage.base_dir / "staging"
    assert storage.staging_dir != storage.raw_dir


def test_staging_dir_created_on_bootstrap(tmp_path):
    storage = StorageManager(base_dir=tmp_path)
    assert storage.staging_dir.is_dir(), "暫存目錄未被引導建立"
    # 控制組：一個確定沒被建立的兄弟目錄必須不存在，證明 is_dir() 分得出來。
    assert not (storage.db_dir / "zzz_never_created").is_dir()


def test_staging_dir_honours_env_override(tmp_path, monkeypatch):
    """`OPENSHELF_STAGING_DIR` 是逃生口：compose 補上專用掛載後可搬離 db/。"""
    override = tmp_path / "elsewhere"
    monkeypatch.setenv("OPENSHELF_STAGING_DIR", str(override))
    storage = StorageManager(base_dir=tmp_path / "data")
    assert storage.staging_dir == override.resolve()
    assert storage.staging_dir.is_dir()


def test_part_path_is_in_staging_not_raw(tmp_path):
    """主鎖：`.part` 檔的落點必須是本地暫存區，不是 NAS 的 raw_dir。

    這條若壞了，症狀是「下載一切正常但很慢、且 NAS 一 stall 就全部卡住」——
    完全沒有錯誤訊號，只有效能退化。所以必須用路徑斷言鎖住，不能靠觀察。
    """
    worker = _make_worker(tmp_path)
    job = _job()
    part = worker._get_part_path(job)

    assert part.parent == worker.pipeline.storage.staging_dir, (
        f".part 落在 {part.parent}，不是暫存區。下載期間每 64KB 一次 NFS RPC 往返。"
    )
    # 負向控制組：確認它真的離開了舊落點（否則上面可能只是 staging==raw）。
    assert part.parent != worker.pipeline.storage.raw_dir
    # 正向控制組：舊落點的計算方式仍然可用（遷移認領靠它）。
    assert worker._legacy_part_path(job).parent == worker.pipeline.storage.raw_dir


# --------------------------------------------------------------------------
# 2. 跨檔案系統搬移
# --------------------------------------------------------------------------

def test_move_same_filesystem_uses_rename(tmp_path):
    """控制組：同一個檔案系統內必須走 rename 快路徑，不得意外走整檔複製。

    這條的價值在於**證明 fallback 沒有被無條件觸發**。若 `_move_across_filesystems`
    寫成一律 `shutil.move`，它仍然會通過所有「檔案搬到了」的測試，但每一次
    落地都變成整檔複製。回傳值把兩條路徑分開，正是為了讓這件事可被斷言。
    """
    src = tmp_path / "a.bin"
    src.write_bytes(b"X" * 2048)
    dst = tmp_path / "b.bin"

    mode = DownloadWorker._move_across_filesystems(src, dst)

    assert mode == "rename", f"同檔案系統竟然走了 {mode!r} 路徑"
    assert not src.exists()
    assert dst.read_bytes() == b"X" * 2048


def test_move_falls_back_to_copy_on_exdev(tmp_path, monkeypatch):
    """跨檔案系統時必須 fallback 成 copy+unlink，而不是把 EXDEV 拋出去。

    ⚠ 這條測的是 BR 原文說錯的那一格。實測（容器內，本地 ext4 -> NAS nfs4）：

        Path.replace 跨裝置 -> OSError errno=18 (EXDEV)，src 仍在、dst 未建立
        shutil.move  跨裝置 -> 成功

    `os.replace` 跨裝置是**直接失敗**，不是「退化成整檔複製」。照 BR 原文
    實作的話，每一個下載都會在最後一步拋 EXDEV 而整個 job 失敗。
    """
    src = tmp_path / "a.bin"
    src.write_bytes(b"Y" * 4096)
    dst = tmp_path / "sub" / "b.bin"
    dst.parent.mkdir()

    real_replace = os.replace
    calls = {"n": 0}

    def fake_replace(a, b):
        # 只讓「src -> dst」那一次假裝跨裝置；tmp -> dst 的收尾必須真的執行。
        if Path(a) == src:
            calls["n"] += 1
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(a, b)

    monkeypatch.setattr(os, "replace", fake_replace)
    mode = DownloadWorker._move_across_filesystems(src, dst)

    assert calls["n"] == 1, "EXDEV 注入沒有真的被觸發，這條測試沒有測到東西"
    assert mode == "copy", f"跨裝置竟然回報 {mode!r}"
    assert not src.exists(), "來源未被清除，本地暫存會累積孤兒檔"
    assert dst.read_bytes() == b"Y" * 4096


def test_move_does_not_swallow_non_exdev_errors(tmp_path, monkeypatch):
    """負向控制組：EXDEV 以外的 OSError 必須往上拋，不得被 fallback 吃掉。

    沒有這條，一個無條件的 `except OSError: shutil.move(...)` 也會通過上面
    兩條測試——而它會把「權限不足」「磁碟滿」「目標目錄不存在」全部偽裝成
    「跨裝置」，然後在 fallback 裡以另一個面目失敗。
    """
    src = tmp_path / "a.bin"
    src.write_bytes(b"Z" * 16)
    dst = tmp_path / "b.bin"

    def fake_replace(a, b):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(os, "replace", fake_replace)
    with pytest.raises(OSError) as exc:
        DownloadWorker._move_across_filesystems(src, dst)
    assert exc.value.errno == errno.EACCES
    assert src.exists(), "非 EXDEV 失敗後來源不該被動過"


def test_move_leaves_no_half_written_final_file(tmp_path, monkeypatch):
    """跨裝置複製中途失敗時，不得留下一個「檔名正確但長度不足」的半成品。

    留下半成品是靜默資料損毀：`process_file` 會照樣對它算雜湊、抽文字、入庫，
    而「完整的書」與「截斷的書」共用同一個輸出（都是一筆 work 記錄）。
    """
    import shutil as _shutil

    src = tmp_path / "a.bin"
    src.write_bytes(b"W" * 8192)
    dst = tmp_path / "b.bin"

    def fake_replace(a, b):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    def boom(a, b):
        Path(b).write_bytes(b"W" * 100)  # 寫了一半
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "replace", fake_replace)
    monkeypatch.setattr(_shutil, "copyfile", boom)

    with pytest.raises(OSError):
        DownloadWorker._move_across_filesystems(src, dst)

    assert not dst.exists(), "失敗後留下了以正式檔名命名的半成品"
    leftovers = list(tmp_path.glob("b.bin.tmp_*"))
    assert leftovers == [], f"暫存半成品未清理：{leftovers}"
    # 控制組：來源還在，代表資料沒有憑空消失（可以重試）。
    assert src.stat().st_size == 8192


# --------------------------------------------------------------------------
# 3. 遷移認領
# --------------------------------------------------------------------------

def test_adopt_legacy_part_moves_nas_leftover_into_staging(tmp_path):
    """遷移期：舊落點（NAS）的 `.part` 必須被認領，續傳進度不得靜默丟棄。"""
    worker = _make_worker(tmp_path)
    job = _job()
    legacy = worker._legacy_part_path(job)
    legacy.write_bytes(b"P" * 512)
    part = worker._get_part_path(job)

    assert worker._adopt_legacy_part(job, part) == "adopted"
    assert part.read_bytes() == b"P" * 512
    assert not legacy.exists(), "舊檔未清除，NAS 上留下永遠沒人引用的孤兒"


def test_adopt_legacy_part_reports_absence_distinctly(tmp_path):
    """控制組：沒有舊檔時回 `"none"`，與 `"failed"` 不共用輸出。

    兩者若都回同一個值，「本來就沒有舊檔」與「有舊檔但搬不動」就不可區分，
    而後者代表 NAS 上有一個沒人再引用的檔。
    """
    worker = _make_worker(tmp_path)
    job = _job()
    assert worker._adopt_legacy_part(job, worker._get_part_path(job)) == "none"


def test_adopt_legacy_part_prefers_new_and_drops_redundant(tmp_path):
    """新舊落點都有檔時，新的優先、舊的必須刪掉（否則它永遠是孤兒）。"""
    worker = _make_worker(tmp_path)
    job = _job()
    legacy = worker._legacy_part_path(job)
    legacy.write_bytes(b"OLD")
    part = worker._get_part_path(job)
    part.write_bytes(b"NEWER-DATA")

    assert worker._adopt_legacy_part(job, part) == "redundant"
    assert part.read_bytes() == b"NEWER-DATA", "新落點的資料被舊檔覆蓋了"
    assert not legacy.exists()


# --------------------------------------------------------------------------
# 4. 孤兒清理
# --------------------------------------------------------------------------

def test_sweep_removes_only_unreferenced_parts(tmp_path):
    """主鎖：掃除只刪沒有 job 引用的 `.part`，活著的續傳檔一個都不准動。

    這是本次變更最危險的一格：掃錯方向會刪掉正在續傳的檔，而症狀是
    「下載莫名其妙從 0 開始」——沒有錯誤、沒有 log，只有進度條倒退。
    """
    worker = _make_worker(tmp_path)
    live_job = _job("job_live", "aaaa")
    worker.jobs[live_job.job_id] = live_job
    live_part = worker._get_part_path(live_job)
    live_part.write_bytes(b"LIVE")

    orphan = worker.pipeline.storage.staging_dir / "job_gone_bbbb.part"
    orphan.write_bytes(b"ORPHAN")

    stats = worker.sweep_orphan_parts()

    assert stats == {"scanned": 2, "removed": 1, "kept": 1}, stats
    assert live_part.exists(), "正在續傳的斷點檔被當成孤兒刪掉了"
    assert not orphan.exists(), "孤兒檔未被清理"


def test_sweep_scanned_count_distinguishes_empty_from_broken(tmp_path):
    """控制組：`removed=0` 有兩種意思，靠 `scanned` 分開。

    沒有 `scanned`，一支「glob 寫錯 pattern 因而永遠掃不到東西」的掃除
    會回報 `removed=0`，與「掃過了、確實沒有孤兒」共用同一個輸出——
    也就是一支永遠不工作的掃除可以永遠通過測試。
    """
    worker = _make_worker(tmp_path)
    empty = worker.sweep_orphan_parts()
    assert empty == {"scanned": 0, "removed": 0, "kept": 0}, empty

    # 正向：放一個檔進去，scanned 必須跟著動（證明 glob 真的看得到）。
    (worker.pipeline.storage.staging_dir / "job_x_y.part").write_bytes(b"o")
    after = worker.sweep_orphan_parts()
    assert after["scanned"] == 1, f"glob 掃不到剛剛建立的檔：{after}"
    assert after["removed"] == 1

    # 負向：非 .part 的檔不得被掃（那是別人的資料）。
    other = worker.pipeline.storage.staging_dir / "not_mine.txt"
    other.write_bytes(b"keep me")
    third = worker.sweep_orphan_parts()
    assert third["scanned"] == 0, f".part 以外的檔被納入掃除範圍：{third}"
    assert other.exists()


def test_clear_completed_sweeps_orphans(tmp_path):
    """`clear_completed()` 刪掉 job 記錄後，其 `.part` 必須跟著回收。

    這是原本就存在但被 42T NAS 掩蓋的缺陷：`clear_completed` 只 `del` 字典
    項目、不碰檔案。改成 530G 的本地暫存後同一個缺陷後果變重。
    """
    worker = _make_worker(tmp_path)
    job = _job("job_done", "cccc")
    job.status = "completed"
    worker.jobs[job.job_id] = job
    part = worker._get_part_path(job)
    part.write_bytes(b"LEFTOVER")

    assert worker.clear_completed() == 1
    assert not part.exists(), "clear_completed 後 .part 殘留在本地暫存區"


def test_clear_completed_keeps_active_job_parts(tmp_path):
    """負向控制組：清完成任務時，未完成任務的斷點檔不得被波及。"""
    worker = _make_worker(tmp_path)
    done = _job("job_done", "cccc")
    done.status = "completed"
    running = _job("job_run", "dddd")
    running.status = "downloading"
    worker.jobs[done.job_id] = done
    worker.jobs[running.job_id] = running
    done_part = worker._get_part_path(done)
    done_part.write_bytes(b"X")
    run_part = worker._get_part_path(running)
    run_part.write_bytes(b"Y")

    assert worker.clear_completed() == 1
    assert not done_part.exists()
    assert run_part.exists(), "進行中任務的斷點檔被誤刪"


def test_delete_job_removes_staging_part(tmp_path):
    """`delete_job` 的既有清理路徑必須跟著落點一起搬過來。

    若 `_remove_part_file` 還指著舊的 NAS 路徑，刪 job 會「成功」但檔案
    留在本地暫存區——回傳 True 與真的刪到檔共用同一個輸出。
    """
    worker = _make_worker(tmp_path)
    job = _job("job_del", "eeee")
    worker.jobs[job.job_id] = job
    part = worker._get_part_path(job)
    part.write_bytes(b"D")

    assert worker.delete_job(job.job_id) is True
    assert not part.exists()


# --------------------------------------------------------------------------
# 5. 掃除的失敗態必須發得出聲（控制組自己的控制組）
#
# 上面 `test_sweep_scanned_count_distinguishes_empty_from_broken` 用 `scanned`
# 當控制組去區分 `removed=0` 的兩種意思。但那個控制組只在「列舉失敗真的會發出
# 聲音」時才成立——若列舉器對不可讀的目錄靜默回空，`scanned=0` 自己又有了兩種
# 意思（真的沒檔 / 目錄不見了），控制組就失去鑑別力。
#
# 這一節鎖的就是那一層：**證明 `except OSError` 分支可達**。
# --------------------------------------------------------------------------

def test_sweep_missing_staging_dir_is_loud_not_silent(tmp_path, caplog):
    """staging 目錄不存在時必須走 `except OSError` 並記 warning，不得靜默回空。

    `Path.glob()` 對不存在的目錄回 `[]` 且不拋例外，會讓那個 except 分支
    **永遠不可達**；`os.scandir` 拋 `FileNotFoundError(errno=2)`。

    這條路徑不是理論情境：staging 落在 `/data/db/staging`，而 `/data/db` 是
    bind-mount。掛載掉、remount 唯讀、或 `OPENSHELF_STAGING_DIR` 指向不存在的
    路徑都會踩到，而斷點續傳檔正在無人回收地累積。
    """
    worker = _make_worker(tmp_path)
    staging = worker.pipeline.storage.staging_dir

    # 先證明目錄本來是好的、掃得到東西——否則下面的 0 分不出是哪一種 0。
    (staging / "job_ctl_aaaa.part").write_bytes(b"x")
    baseline = worker.sweep_orphan_parts()
    assert baseline["scanned"] == 1, f"控制組失效，掃除本來就看不到檔：{baseline}"

    # 現在把目錄整個移除，模擬掛載掉 / 被外部刪除。
    for child in staging.iterdir():
        child.unlink()
    staging.rmdir()
    assert not staging.exists()

    with caplog.at_level(logging.WARNING, logger="app.crawler.download_worker"):
        stats = worker.sweep_orphan_parts()

    assert stats == {"scanned": 0, "removed": 0, "kept": 0}, stats
    assert any("暫存區掃描失敗" in r.message for r in caplog.records), (
        "staging 目錄不存在卻沒有任何 warning——失敗態與「沒有孤兒」共用了同一個輸出"
    )


def test_sweep_unreadable_staging_dir_is_loud(tmp_path, caplog):
    """權限不足時同樣必須出聲。

    與上一條是**不同的 errno**（EACCES vs ENOENT），走的是同一個分支但
    來源不同——只測 ENOENT 會讓「只對不存在的目錄出聲」這種半套修法通過。
    """
    worker = _make_worker(tmp_path)
    staging = worker.pipeline.storage.staging_dir
    (staging / "job_perm_bbbb.part").write_bytes(b"x")

    original_mode = staging.stat().st_mode
    os.chmod(staging, 0o000)
    try:
        if os.access(staging, os.R_OK):
            pytest.skip("以 root 執行時權限位元不生效，本條無法建立失敗態")

        with caplog.at_level(logging.WARNING, logger="app.crawler.download_worker"):
            stats = worker.sweep_orphan_parts()

        assert stats == {"scanned": 0, "removed": 0, "kept": 0}, stats
        assert any("暫存區掃描失敗" in r.message for r in caplog.records), (
            "staging 不可讀卻沒有任何 warning"
        )
    finally:
        os.chmod(staging, original_mode)


def test_sweep_healthy_dir_logs_no_warning(tmp_path, caplog):
    """負向控制組：目錄正常時**不得**出現掃描失敗的 warning。

    沒有這條，一個「無論如何都記 warning」的實作也會讓上面兩條通過。
    """
    worker = _make_worker(tmp_path)
    (worker.pipeline.storage.staging_dir / "job_ok_cccc.part").write_bytes(b"x")

    with caplog.at_level(logging.WARNING, logger="app.crawler.download_worker"):
        stats = worker.sweep_orphan_parts()

    assert stats["scanned"] == 1
    assert not any("暫存區掃描失敗" in r.message for r in caplog.records), (
        "目錄正常卻報了掃描失敗——warning 是恆真的，不具鑑別力"
    )
