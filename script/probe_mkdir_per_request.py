#!/usr/bin/env python3
"""量測「一個 API 請求打下去，究竟對 raw/ 與 parsed/ 下了幾次 mkdir syscall」。

BR-20260821_040000 的核心指控是：`StorageManager.__init__` 無條件呼叫
`ensure_directories()`，而每個 `Depends()` 都新建一個 StorageManager
=> 每個請求都對 NFS 下 mkdir。

**為什麼攔 `os.mkdir` 而不是 `Path.mkdir`**：`pathlib.Path.mkdir` 是
`os.mkdir` 的包裝（`/usr/lib/python3.12/pathlib.py` 先無條件呼叫
`os.mkdir(2)` 再 `except OSError` 吞 EEXIST）。攔最底層那個才涵蓋
「不經 pathlib 的呼叫者」，也才貼近「syscall 次數」這個提問。

**兩態必須可分**：本探針同時輸出
  - SUBJECT：lifespan 啟動**之後**、單一請求期間的 mkdir 次數
  - CONTROL_STARTUP：lifespan 期間的 mkdir 次數（**必須非零**，否則
    代表攔截根本沒生效，而「攔截失效」與「真的零次」會共用同一個輸出）

用法：
    .venv/bin/python script/probe_mkdir_per_request.py [路由]
預設路由 `/api/search?q=zzzznomatch`。
"""
import os
import sys
import shutil
import tempfile
from pathlib import Path

ROUTE = sys.argv[1] if len(sys.argv) > 1 else "/api/search?q=zzzznomatch"

scratch = Path(tempfile.mkdtemp(prefix="openshelf-mkdir-probe-", dir=os.environ.get("XDG_RUNTIME_DIR") or None))
os.environ["DATA_DIR"] = str(scratch / "data")

_real_mkdir = os.mkdir
_records: list[str] = []
_armed = False


def _spy_mkdir(path, *a, **kw):
    if _armed:
        _records.append(str(path))
    return _real_mkdir(path, *a, **kw)


os.mkdir = _spy_mkdir

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
import app.main as main_module  # noqa: E402


def _bucket(records):
    raw = sum(1 for p in records if "/raw" in p)
    parsed = sum(1 for p in records if "/parsed" in p)
    db = sum(1 for p in records if "/db" in p)
    return raw, parsed, db, len(records)


def main():
    global _armed

    # ---- CONTROL：啟動期。必須非零，證明攔截真的有效 ----
    _armed = True
    with TestClient(main_module.app) as client:
        startup = list(_records)
        _records.clear()

        # ---- SUBJECT：單一請求 ----
        resp = client.get(ROUTE)
        during = list(_records)
        _records.clear()

        # ---- SUBJECT2：第二次同樣請求（測有無「只有第一次才做」的快取） ----
        resp2 = client.get(ROUTE)
        during2 = list(_records)
        _records.clear()

    _armed = False

    s_raw, s_parsed, s_db, s_all = _bucket(startup)
    d_raw, d_parsed, d_db, d_all = _bucket(during)
    e_raw, e_parsed, e_db, e_all = _bucket(during2)

    print(f"ROUTE                {ROUTE}")
    print(f"HTTP_STATUS          {resp.status_code} / {resp2.status_code}")
    print("")
    print(f"CONTROL_STARTUP      total={s_all:3d}  raw={s_raw}  parsed={s_parsed}  db={s_db}"
          f"   <-- 必須非零，否則攔截失效")
    print(f"SUBJECT_REQUEST_1    total={d_all:3d}  raw={d_raw}  parsed={d_parsed}  db={d_db}")
    print(f"SUBJECT_REQUEST_2    total={e_all:3d}  raw={e_raw}  parsed={e_parsed}  db={e_db}")
    print("")
    if during:
        print("REQUEST_1 mkdir paths:")
        for p in during:
            print(f"    {p}")
    print("")
    print(f"CONTROL_HAS_DISCRIMINATION  {'YES' if s_all > 0 else 'NO — 這格無鑑別力，重做'}")
    print(f"VERDICT_PER_REQUEST_MKDIR   {d_all + e_all}")

    shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
