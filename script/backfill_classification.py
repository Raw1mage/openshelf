#!/usr/bin/env python3
"""可重跑的分類回填命令。

用途：消化 `work.classification_state` 不是 `classified` 的 Work——那個欄位就是
待辦佇列本身，不是另建的機制（與 download_job 用 DB status 當佇列同一個形狀）。

設計要點：
- **dry-run 是預設，而且是真的唯讀**。dry-run 走 `readonly=True` 的連線，任何
  寫入在 SQLite 層直接拋錯；不 bootstrap、不建表、不 ALTER、不 seed。要真的寫
  DB 必須顯式 `--apply`。
- **開工前驗 schema**。缺表/缺欄位就 rc=2 退出，而不是靜默把它引導成一個空 DB
  然後回報「0 本待回填」——那是最令人安心也最錯的答案。
- **逐本隔離**。任何一本拋例外都只記在該本身上，不中斷整批。
- **分類 outcome 與 persisted 分開計**。「分類器說 classified」與「真的寫進去了」
  是兩件事；共用一個計數會讓一次寫入失敗看起來像成功。
- **失敗即非零 rc**。任何一本 classification exception 或 persist 失敗，整批
  最終 rc 非零；否則自動化排程會把半套結果當成功。
- **`error` / `disabled` outcome 也算本輪未成功**（VANS R2）。它們代表「因為
  環境或遠端故障而沒判成」——HTTP 5xx、timeout、非法回應、API 未設定。這些
  outcome 能被成功寫進 DB（供日後重試），但**寫得進去不等於這輪成功**：把它們
  當成功會讓排程器在整批書都因為 API 掛掉而失敗時安靜回報 rc=0，錯書繼續隱藏
  且沒有任何告警。`unclassified` 不同——那是模型有效判定「確實歸不了類」，
  是正常結果，rc=0。
- **冪等**。重跑只會處理仍非 classified 的；已 classified 的不再動。

用法（DB 路徑：開發機 data/db/openshelf.sqlite，容器部署 /data/db/openshelf.sqlite）：
    python script/backfill_classification.py --db data/db/openshelf.sqlite
    python script/backfill_classification.py --db data/db/openshelf.sqlite --apply
    python script/backfill_classification.py --db data/db/openshelf.sqlite \
        --states pending error unclassified disabled
"""

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.classification.result import ClassificationState  # noqa: E402
from app.classification.service import ClassificationService  # noqa: E402
from app.db.dao import CatalogDAO  # noqa: E402
from app.db.engine import DatabaseEngine  # noqa: E402

log = logging.getLogger("backfill_classification")

# 預設候選：**所有非 classified 的狀態**。
#
# 為何 unclassified / disabled 也要納入（VANS R1 P1-6，產品裁決）：它們代表
# 「用當時的 API 設定與當時的模型判不出」，而那兩者都會變——補上 API key、
# 換更強的模型之後，這些書必須能被重試。把它們排除在預設之外，等於讓一次
# 暫時性的環境缺陷變成永久判決，而且使用者從介面上看不出還有這批書可救。
DEFAULT_STATES: Tuple[str, ...] = tuple(
    s for s in ClassificationState.ALL if s != ClassificationState.CLASSIFIED
)

# 本輪視為「未成功」的 outcome 狀態（VANS R2 契約）。
#
# 三者的分界是**這一輪有沒有得到有效判定**，不是「有沒有寫進 DB」：
#   classified   有效判定，成功           -> rc 0
#   unclassified 有效判定（判不出）       -> rc 0
#   pending      規則層未命中、尚未跑模型 -> rc 0（不是故障，等下一輪）
#   error        遠端/逾時/非法輸出       -> rc 1（環境故障，要告警）
#   disabled     API 未設定，無法判定     -> rc 1（設定缺陷，要告警）
UNSUCCESSFUL_STATES: Tuple[str, ...] = (
    ClassificationState.ERROR,
    ClassificationState.DISABLED,
)

# 開工前必須存在的 schema。缺任何一項即 rc=2。
REQUIRED_SCHEMA: Dict[str, Tuple[str, ...]] = {
    "work": ("work_id", "title", "authors_display", "language", "work_type",
             "classification_state"),
    "work_category": ("work_id", "category_id", "source"),
    "category": ("category_id", "parent_id"),
}


class SchemaIncomplete(Exception):
    """目標 DB 缺少必要 schema。訊息即為缺了什麼。"""


def verify_schema(db_path: Path) -> List[str]:
    """唯讀驗證必要 schema，回傳實際存在的表名清單（供控制組斷言用）。

    刻意用**獨立的唯讀連線**而非 DAO：DAO 的建構子會引導 schema，用它來
    「檢查 schema 在不在」等於先把答案寫成 yes 再去問。
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        present = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing: List[str] = []
        for table, columns in REQUIRED_SCHEMA.items():
            if table not in present:
                missing.append(f"table {table}")
                continue
            cols = {
                r["name"]
                for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for col in columns:
                if col not in cols:
                    missing.append(f"{table}.{col}")
        if missing:
            raise SchemaIncomplete(
                f"目標 DB 缺少必要 schema：{', '.join(missing)}。"
                "這通常代表 --db 指到了錯誤的檔案（例如 0-byte 佔位檔或舊路徑）。"
            )
        return sorted(present)
    finally:
        conn.close()


def open_dao(db_path: Path, apply: bool) -> CatalogDAO:
    """依模式開啟 DAO：dry-run 走唯讀且不引導，apply 才是可寫。"""
    if apply:
        return CatalogDAO(engine=DatabaseEngine(db_path=db_path))
    engine = DatabaseEngine(db_path=db_path, bootstrap=False, readonly=True)
    return CatalogDAO(engine=engine, bootstrap=False)


def run_backfill(
    dao: CatalogDAO,
    service: Optional[ClassificationService] = None,
    states: Optional[List[str]] = None,
    limit: Optional[int] = None,
    apply: bool = False,
) -> Dict[str, Any]:
    """回填主體。`apply=False`（預設）時**不寫任何 DB**。

    回傳的 report 一定含 `applied` 旗標與 `per_work` 明細，讓呼叫端能證明
    dry-run 真的沒寫——只回一組計數的話，「沒寫」與「寫了但結果一樣」看起來相同。

    四組計數彼此獨立，任兩者不得互相推導：
      - `outcomes`      分類器對每本書的判定（與有沒有寫進去無關）
      - `persisted`     真的落到 DB 的筆數（只有 apply 模式才可能非零）
      - `failures`      分類例外 + 寫入失敗
      - `unsuccessful`  outcome 落在 `UNSUCCESSFUL_STATES` 的本數

    `failures` 與 `unsuccessful` 刻意分開：前者是「這支程式或 DB 出事」，
    後者是「遠端/設定出事」。兩者都讓 rc 非零，但混成一個計數會讓報告分不出
    「模型 API 掛了」與「磁碟寫不進去」，而這兩件事的處置完全不同。
    """
    service = service or ClassificationService(dao=dao)
    states = list(states) if states else list(DEFAULT_STATES)

    candidates = dao.list_works_for_classification(states=states, limit=limit)

    report: Dict[str, Any] = {
        "applied": apply,
        "candidates": len(candidates),
        "outcomes": {s: 0 for s in ClassificationState.ALL},
        "persisted": 0,
        "persist_failures": 0,
        "classification_failures": 0,
        "failures": 0,
        "unsuccessful": 0,
        "per_work": [],
    }

    for row in candidates:
        wid = row["work_id"]
        try:
            outcome = service.classify(
                title=row["title"],
                authors=row["authors_display"],
                language=row["language"],
                work_type=row["work_type"],
            )
        except Exception as exc:
            # 逐本隔離：這一本炸掉不影響其餘。記成 failure 而非 error 狀態——
            # 「分類器回報 error」與「分類器整個炸了」是不同的事。
            log.warning("work %s 分類過程拋出例外：%s: %s", wid, type(exc).__name__, exc)
            report["classification_failures"] += 1
            report["failures"] += 1
            report["per_work"].append({
                "work_id": wid,
                "title": row["title"],
                "state": "exception",
                "persisted": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue

        report["outcomes"][outcome.state] += 1
        if outcome.state in UNSUCCESSFUL_STATES:
            # 這一本這輪沒判成。outcome 仍會照常寫入（保留狀態供日後重試），
            # 但整批不得因此回報成功。
            report["unsuccessful"] += 1
        entry = {
            "work_id": wid,
            "title": row["title"],
            "state": outcome.state,
            "category_ids": list(outcome.category_ids),
            "source": outcome.source,
            "confidence": outcome.confidence,
            "model": outcome.model,
            "prompt_version": outcome.prompt_version,
            "persisted": False,
            "error": outcome.error,
        }
        report["per_work"].append(entry)

        if apply:
            try:
                dao.apply_classification(wid, outcome)
            except Exception as exc:
                log.warning("work %s 寫入失敗：%s: %s", wid, type(exc).__name__, exc)
                report["persist_failures"] += 1
                report["failures"] += 1
                entry["error"] = f"persist failed: {type(exc).__name__}: {exc}"
            else:
                report["persisted"] += 1
                entry["persisted"] = True

    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="回填既有 Work 的智慧分類")
    parser.add_argument(
        "--db", required=True,
        help="SQLite 資料庫路徑（開發機 data/db/openshelf.sqlite；容器 /data/db/openshelf.sqlite）",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="真的寫入 DB。未指定時為 dry-run（預設），以唯讀連線執行，不寫入任何位元組。",
    )
    parser.add_argument(
        "--states", nargs="+", default=None,
        help=f"要處理的 classification_state（預設 {' '.join(DEFAULT_STATES)}）。"
             f"可選：{' '.join(ClassificationState.ALL)}",
    )
    parser.add_argument("--limit", type=int, default=None, help="最多處理幾本")
    parser.add_argument("--json", action="store_true", help="以 JSON 輸出完整 report")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.states:
        unknown = [s for s in args.states if s not in ClassificationState.ALL]
        if unknown:
            parser.error(f"未知的 state: {unknown}；可選 {list(ClassificationState.ALL)}")

    db_path = Path(args.db)
    if not db_path.exists():
        # 缺席態要出聲：靜默建一個空 DB 然後回報「0 本待回填」會是最令人安心
        # 也最錯的答案。
        parser.error(f"資料庫不存在：{db_path}")

    try:
        verify_schema(db_path)
    except (SchemaIncomplete, sqlite3.DatabaseError) as exc:
        # 0-byte 檔會在此以 DatabaseError（"file is not a database"）或缺表被擋下。
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2

    dao = open_dao(db_path, apply=args.apply)
    before = dao.get_classification_stats()
    report = run_backfill(dao, states=args.states, limit=args.limit, apply=args.apply)
    after = dao.get_classification_stats()

    report["stats_before"] = before
    report["stats_after"] = after

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        mode = "APPLY（已寫入）" if args.apply else "DRY-RUN（唯讀連線，未寫入任何資料）"
        used_states = args.states or list(DEFAULT_STATES)
        print(f"模式：{mode}")
        print(f"候選：{report['candidates']} 本（states={used_states}）")
        print("分類判定分佈（與是否寫入無關）：")
        for state, count in report["outcomes"].items():
            print(f"  {state:<14} {count}")
        print(f"實際寫入：{report['persisted']} 本")
        print(f"分類例外：{report['classification_failures']}　寫入失敗：{report['persist_failures']}")
        print(
            f"本輪未得到有效判定（{'/'.join(UNSUCCESSFUL_STATES)}）："
            f"{report['unsuccessful']} 本"
        )
        print("狀態統計 before -> after：")
        for state in ClassificationState.ALL:
            print(f"  {state:<14} {before.get(state, 0)} -> {after.get(state, 0)}")

    # 逐本失敗已隔離且後續仍處理完，但整批不得回報成功——否則排程器會把
    # 半套結果當完成，而失敗的那幾本再也不會被看見。
    #
    # 兩類都要讓 rc 非零，但訊息分開列：exception/persist 是「這支程式或 DB 出事」，
    # error/disabled outcome 是「遠端或設定出事」——後者寫得進 DB，所以它不會被
    # failures 捕到，而這正是一整批書都因 API 掛掉却回 rc=0 的漏洞（VANS R2）。
    if report["failures"] or report["unsuccessful"]:
        if report["failures"]:
            print(
                f"警告：{report['failures']} 本執行失敗（分類例外 "
                f"{report['classification_failures']}、寫入失敗 {report['persist_failures']}）",
                file=sys.stderr,
            )
        if report["unsuccessful"]:
            print(
                f"警告：{report['unsuccessful']} 本未得到有效判定（"
                + "、".join(
                    f"{s} {report['outcomes'][s]}" for s in UNSUCCESSFUL_STATES
                )
                + "）；狀態已保留，修好設定/遠端後重跑本命令可重試。",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
