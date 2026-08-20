#!/usr/bin/env python3
"""OpenShelf 延遲尖峰探針 — BR-20260820_160000 觀察期量測工具。

設計意圖：把「偶發尖峰」變成「可歸因的連續指標」。

三支探針各量一個**不同**的東西（這是鑑別力的來源）：

  health       GET /api/health         sync def，無 DB、無檔案 IO
                                       -> 量 anyio threadpool 可用性
  jobs         GET /api/crawler/jobs   async def，純記憶體 dict 讀
                                       -> 量 **event loop 排隊延遲**（最關鍵的一支）
  collections  GET /api/collections    sync def，走 DB 讀
                                       -> 量 DB 路徑（含 SQLite 鎖爭用）

歸因表（三支同時量，尖峰形狀直接指認成因）：

  三支同時尖峰            -> event loop 被同步呼叫阻塞
                             （BR-20260820_210000 E 節 download_worker f.write
                              寫 NFS 是目前唯一殘留候選）
  只有 collections 尖峰   -> SQLite 鎖爭用（BR-20260820_160000 候選 1）
  只有 health 尖峰        -> threadpool 飽和（sync def 端點佔滿 40 個 worker）
  jobs 尖峰但 health 不   -> event loop 阻塞而 threadpool 仍有餘裕

為何 jobs 是關鍵：它是 async def 且 body 只讀記憶體，不做任何 IO。
它的 wall time 扣掉網路往返之後，**就是 event loop 的排隊延遲本身**。
沒有這一支，「DB 慢」與「整個 process 卡住」共用同一個輸出。
"""
import argparse
import json
import os
import statistics
import threading
import time
import urllib.error
import urllib.request

BASE = os.environ.get("PROBE_BASE", "http://127.0.0.1:8088")

PROBES = {
    "health": "/api/health",
    "jobs": "/api/crawler/jobs",
    "collections": "/api/collections",
}

_stop = threading.Event()
_lock = threading.Lock()


def _hit(url, timeout=120.0):
    """單次請求，回傳 (毫秒, http_code, err)。

    code = -1 代表連線層失敗（逾時、拒絕），與 HTTP 錯誤碼刻意分開，
    否則「服務掛了」與「回了 500」會共用同一個輸出。
    """
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            r.read()
            return (time.perf_counter() - t0) * 1000.0, r.status, ""
    except urllib.error.HTTPError as e:
        return (time.perf_counter() - t0) * 1000.0, e.code, type(e).__name__
    except Exception as e:
        return (time.perf_counter() - t0) * 1000.0, -1, "%s:%s" % (type(e).__name__, e)


def _record(out, name, ms, code, err):
    with _lock:
        out.append({"t": time.time(), "probe": name, "ms": round(ms, 2),
                    "code": code, "err": err})


def probe_loop(name, path, interval, out):
    url = BASE + path
    while not _stop.is_set():
        ms, code, err = _hit(url)
        _record(out, name, ms, code, err)
        _stop.wait(interval)


def load_loop(path, tag, out):
    """負載產生器：無間隔連發，製造併發連線與 threadpool 壓力。"""
    url = BASE + path
    while not _stop.is_set():
        ms, code, err = _hit(url)
        _record(out, tag, ms, code, err)


def summarize(out):
    by = {}
    for r in out:
        by.setdefault(r["probe"], []).append(r)
    res = {}
    for name, rows in sorted(by.items()):
        ms = sorted(x["ms"] for x in rows)
        n = len(ms)
        ok = sum(1 for x in rows if x["code"] == 200)
        conn_fail = sum(1 for x in rows if x["code"] == -1)
        http_err = sum(1 for x in rows if x["code"] not in (200, -1))

        def pct(p):
            if not ms:
                return None
            k = min(n - 1, int(round((p / 100.0) * (n - 1))))
            return round(ms[k], 2)

        res[name] = {
            "n": n, "ok_200": ok, "conn_fail": conn_fail, "http_err": http_err,
            "min": round(ms[0], 2) if ms else None,
            "p50": pct(50), "p95": pct(95), "p99": pct(99),
            "max": round(ms[-1], 2) if ms else None,
            "mean": round(statistics.fmean(ms), 2) if ms else None,
            "over_1s": sum(1 for x in ms if x >= 1000),
            "over_5s": sum(1 for x in ms if x >= 5000),
            "over_20s": sum(1 for x in ms if x >= 20000),
        }
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--interval", type=float, default=0.25,
                    help="每支探針的取樣間隔（秒）")
    ap.add_argument("--load-threads", type=int, default=0,
                    help="負載執行緒數；0 = idle 量測")
    ap.add_argument("--load-path", default="/api/search?q=the&page_size=100")
    ap.add_argument("--out", required=True, help="raw jsonl 輸出路徑")
    ap.add_argument("--label", default="run")
    args = ap.parse_args()

    out = []
    threads = []
    for name, path in PROBES.items():
        t = threading.Thread(target=probe_loop, args=(name, path, args.interval, out),
                             daemon=True)
        t.start()
        threads.append(t)
    for i in range(args.load_threads):
        t = threading.Thread(target=load_loop, args=(args.load_path, "LOAD", out),
                             daemon=True)
        t.start()
        threads.append(t)

    t_start = time.time()
    try:
        time.sleep(args.duration)
    finally:
        _stop.set()
    for t in threads:
        t.join(timeout=130.0)
    t_end = time.time()

    with open(args.out, "w", encoding="utf-8") as f:
        for r in sorted(out, key=lambda x: x["t"]):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    summary = {
        "label": args.label, "base": BASE,
        "started": t_start, "ended": t_end,
        "wall_sec": round(t_end - t_start, 2),
        "load_threads": args.load_threads,
        "load_path": args.load_path if args.load_threads else None,
        "interval": args.interval,
        "raw": args.out,
        "probes": summarize(out),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
