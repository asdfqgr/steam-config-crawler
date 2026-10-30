"""抓取 Steam 全量游戏 appid 列表。

Steam 的 GetAppList v2 接口已废弃(404)，改走商店搜索接口：
    /search/results/?start=N&count=100&infinite=1&json=1&l=schinese&category1=998
category1=998 表示只取「游戏」，可排除 DLC / 软件 / 原声带。
实测总数约 18.7 万，深层分页(start=180000)也正常返回。

用法:
    python applist.py --out ../data/appids.jsonl --concurrency 8
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

SEARCH = (
    "https://store.steampowered.com/search/results/"
    "?start={start}&count={count}&infinite=1&json=1&l=schinese&category1=998"
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_APPID = re.compile(r'data-ds-appid="(\d+)"')

_lock = threading.Lock()
_cons_fail = 0
_pause_until = 0.0


def _note_ok() -> None:
    global _cons_fail
    with _lock:
        _cons_fail = 0


def _note_fail(threshold: int = 6, pause: float = 30.0) -> None:
    """连续失败达到阈值就全局暂停，避免把 IP 打爆。"""
    global _cons_fail, _pause_until
    with _lock:
        _cons_fail += 1
        if _cons_fail >= threshold:
            _pause_until = time.time() + pause
            _cons_fail = 0


def _wait_if_paused() -> None:
    while True:
        with _lock:
            remain = _pause_until - time.time()
        if remain <= 0:
            return
        time.sleep(min(remain, 2.0))


def _get(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_page(start: int, count: int, retries: int = 3) -> tuple[int, list[int], int]:
    """返回 (start, appid 列表, 总游戏数)。失败返回空列表。"""
    _wait_if_paused()
    for attempt in range(retries):
        try:
            raw = _get(SEARCH.format(start=start, count=count))
            data = json.loads(raw)
            ids = [int(x) for x in _APPID.findall(data.get("results_html", ""))]
            _note_ok()
            return start, ids, int(data.get("total_count") or 0)
        except Exception as exc:  # noqa: BLE001
            if attempt == retries - 1:
                _note_fail()
                print("  page start=%d 失败: %s" % (start, str(exc)[:60]), file=sys.stderr)
            else:
                time.sleep(1.5 * (attempt + 1))
    return start, [], 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="../data/appids.jsonl")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--count", type=int, default=100)
    args = ap.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    print("[1/2] 探测游戏总数 ...")
    _, _, total = fetch_page(0, args.count)
    if not total:
        print("错误：探测总数失败，网络可能不通", file=sys.stderr)
        return 1
    pages = math.ceil(total / args.count)
    print("      总数 %d，共 %d 页" % (total, pages))

    print("[2/2] 并发 %d 抓取分页 ..." % args.concurrency)
    seen: dict[int, None] = {}
    empty_streak = 0
    t0 = time.time()
    with ThreadPoolExecutor(args.concurrency) as ex:
        futs = [ex.submit(fetch_page, s * args.count, args.count) for s in range(pages)]
        for i, fut in enumerate(as_completed(futs), 1):
            start, ids, _ = fut.result()
            for x in ids:
                seen[x] = None
            empty_streak = empty_streak + 1 if not ids else 0
            if i % 200 == 0:
                print(
                    "      %d/%d 页  已收 %d 个  用时 %.0fs"
                    % (i, pages, len(seen), time.time() - t0),
                    flush=True,
                )
            if empty_streak >= 25:
                print("      连续 25 页为空，提前停止（可能被限流）", file=sys.stderr)
                break

    ids = sorted(seen)
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        for x in ids:
            f.write("%d\n" % x)
    print("完成：%d 个 appid -> %s（%.0fs）" % (len(ids), args.out, time.time() - t0))
    return 0 if ids else 1


if __name__ == "__main__":
    raise SystemExit(main())
