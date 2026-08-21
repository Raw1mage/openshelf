"""應用層 logging 配置。

**為什麼是獨立模組而不是塞進 `main.py`**：

1. `main.py` 是 ASGI app 的定義檔，logging 是**部署面**關注點。混在一起會讓
   「改路由」與「改可觀測性」共用同一個檔案的修改史。
2. `main.py` 不是唯一入口。`script/` 下的一次性工具、以及任何直接
   `from app.crawler.libgen_live import LibgenCrawler` 的呼叫端都不會匯入
   `main.py`；獨立模組讓那些入口能自己決定要不要配置，而不是被迫拖進整個
   FastAPI app。
3. 反面同樣重要：**pytest 不會匯入本模組的配置**（除非測試自己叫），所以
   `caplog` 的行為不受生產配置影響。

**為什麼只配置 `app` 這個 namespace，不碰 root**：

root 是所有第三方套件的共同祖先。把 root 調到 INFO 會讓 `httpx` 對每一次
鏡像請求印一行 `HTTP Request: GET ... 200 OK`、讓 `watchfiles` 印每次
reload 掃描——那正是「訊號被無關訊息淹沒」的形狀。本專案自己的 6 個 logger
全部以 `app.` 開頭（`app.api.settings_routes` / `app.crawler.libgen_live` /
`app.crawler.mirror_resolver` / `app.crawler.validator` /
`app.crawler.download_worker` / `app.db.dao`），所以 `app` 是恰好涵蓋
「我們自己的程式」且不多一分的邊界。

**為什麼 `propagate` 維持 True**：

改成 False 會讓 `app.*` 的 record 在 `app` 這一層停止上溯，於是 pytest 的
`caplog`（它的 handler 掛在 root）再也收不到——`tests/test_publication_year.py`、
`tests/test_libgen_parser_md5_gate.py`、`tests/test_download_worker_enqueue_autostart.py`、
`tests/test_dispatched_issues_read_side_signal.py` 四個檔的斷言會全部變成
「看不到 log」而失敗。維持 True 不會造成重複輸出：root 沒有掛任何 handler，
而 `logging.lastResort` 只在整條鏈上**一個 handler 都沒找到**時才啟用——
`app` 已經掛了一個，所以 lastResort 不再介入。

**這個配置修正了什麼（實測，2026-08-21）**：

配置前 root 停在預設 WARNING 且 handlers 為空，唯一的輸出管道是
`logging.lastResort`（一個 level=WARNING 的裸 `_StderrHandler`）。後果有二：

  - `log.debug` / `log.info` **全數被丟棄**，永遠不會出現在 `docker logs`。
  - `log.warning` 以上雖然發得出來，但 lastResort 沒有 formatter，印出來是
    光禿禿一行訊息，**沒有時間、沒有等級、沒有 logger 名**，無法與 uvicorn
    的輸出對齊，也看不出是哪個模組發的。

`uvicorn` 自己的 logger 由 uvicorn 在啟動時配置（`propagate=False` + 自帶
handler），不經過 root 也不經過 `app`，故本配置**不改變 uvicorn 的 access log
與啟動訊息**的格式與數量。
"""

import logging
import os
import sys

APP_LOGGER_NAME = "app"

DEFAULT_LEVEL = "INFO"

# 刻意與 uvicorn 的 `INFO:     <msg>` 明顯不同，避免兩種來源的行看起來像同一個
# 系統發的。帶 logger 名是關鍵：6 個模組共用這個 handler，沒有名字就分不出來源。
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 用來辨識「本模組掛上去的那個 handler」。沒有這個標記，重複呼叫
# configure_logging() 會疊上第二個 handler，於是每行 log 印兩次——而那個症狀
# 與「log 被某處重複發送」看起來一模一樣，很難回溯。
_HANDLER_MARKER = "_openshelf_app_handler"


def resolve_level(raw: str | None = None) -> int:
    """把環境變數字串解析成 logging 等級。

    無法辨識的字串**不靜默退回預設**：那會讓「打錯字」與「刻意設成 INFO」
    共用同一個輸出。改為退回預設並在該 handler 建立後記一行 warning
    （見 configure_logging），使打錯字這件事本身可觀測。
    """
    if raw is None:
        raw = os.environ.get("OPENSHELF_LOG_LEVEL", DEFAULT_LEVEL)
    candidate = str(raw).strip().upper()
    resolved = logging.getLevelName(candidate)
    if isinstance(resolved, int):
        return resolved
    return logging.getLevelName(DEFAULT_LEVEL)


def configure_logging(level: str | None = None) -> logging.Logger:
    """為 `app` namespace 掛上 stderr handler 並設定等級。

    冪等：重複呼叫只會更新等級，不會疊加 handler。
    回傳被配置的 logger，方便呼叫端斷言。
    """
    raw = level if level is not None else os.environ.get("OPENSHELF_LOG_LEVEL", DEFAULT_LEVEL)
    resolved = resolve_level(raw)
    unrecognized = resolve_level(raw) == logging.getLevelName(DEFAULT_LEVEL) and str(
        raw
    ).strip().upper() != DEFAULT_LEVEL

    app_logger = logging.getLogger(APP_LOGGER_NAME)
    app_logger.setLevel(resolved)

    existing = [h for h in app_logger.handlers if getattr(h, _HANDLER_MARKER, False)]
    if existing:
        handler = existing[0]
    else:
        # stderr 而非 stdout：lastResort 先前就是寫 stderr，維持同一串流可以讓
        # 既有的 warning 行不會突然換邊，只有格式變好。
        handler = logging.StreamHandler(sys.stderr)
        setattr(handler, _HANDLER_MARKER, True)
        app_logger.addHandler(handler)

    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    handler.setLevel(logging.NOTSET)  # 等級由 logger 決定，handler 不再二次過濾

    if unrecognized:
        app_logger.warning(
            "無法辨識的 OPENSHELF_LOG_LEVEL=%r，已退回預設 %s。"
            " 這行存在的理由是：不出聲的話，打錯的等級名與刻意設成預設值"
            " 會共用同一個輸出。",
            raw,
            DEFAULT_LEVEL,
        )

    return app_logger
