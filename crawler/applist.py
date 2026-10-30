"""抓取 Steam 全量游戏 appid 列表。

两条路，优先走快的：
  1. GetAppList v2 —— 一次请求返回全量（约 25 万条，含 DLC/软件），耗时几秒
  2. 商店搜索分页  —— category1=998 只含游戏（约 18.7 万），但要 1870 次请求，慢

实测 GetAppList 在海外节点稳定，所以在 GitHub Actions 上正常几秒就能完成；
本机直连常被阻断，会自动回退到分页方式。

用法:
    python applist.py --out ../data/appids.jsonl --concurrency 8
    python applist.py --no-v2        # 强制走分页
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

V2 = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
SEARCH = (
    "https://store.steampowered.com/search/results/"
    "?start={start}&count={count}&infinite=1&json=1&l=schinese&category1=998"
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_APPID = re.compile(r'data-ds-appid="(\d+)"')

# 名字里明确是附属内容/工具的，直接不要，省下几十万次详情请求
_NOISE = re.compile(
    r"(soundtrack|original soundtrack|ost\b|dedicated server|sdk\b|"
    r"demo$|playtest|beta\b|dlc\b|season pass|artbook|art book|"
    r"wallpaper|companion|benchmark)",
    re.I,
)

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


def try_v2() -> list[int]:
    """一次拿全量。失败返回空列表。"""
    print("[1/2] 尝试 GetAppList v2（一次请求拿全量）...")
    for attempt in range(3):
        t0 = time.time()
        try:
            data = json.loads(_get(V2, timeout=120))
            apps = (data.get("applist") or {}).get("apps") or []
            keep = []
            noise = 0
            for a in apps:
                aid = a.get("appid")
                name = (a.get("name") or "").strip()
                if not aid or not name:
                    continue
                if _NOISE.search(name):
                    noise += 1
                    continue
                keep.append(int(aid))
            print(
                "      返回 %d 条，过滤掉附属内容 %d 条，保留 %d 个（%.1fs）"
                % (len(apps), noise, len(keep), time.time() - t0)
            )
            if len(keep) >= 50000:
                return sorted(set(keep))
            print("      条数异常偏少，回退到分页方式", file=sys.stderr)
            return []
        except Exception as exc:  # noqa: BLE001
            print("      第 %d 次失败：%s" % (attempt + 1, str(exc)[:80]), file=sys.stderr)
            time.sleep(3 * (attempt + 1))
    return []


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


def by_search(concurrency: int, count: int) -> list[int]:
    print("[1/2] 探测游戏总数 ...")
    _, _, total = fetch_page(0, count)
    if not total:
        print("错误：探测总数失败，网络可能不通", file=sys.stderr)
        return []
    pages = math.ceil(total / count)
    print("      总数 %d，共 %d 页" % (total, pages))

    print("[2/2] 并发 %d 抓取分页 ..." % concurrency)
    seen: dict[int, None] = {}
    empty_streak = 0
    t0 = time.time()
    with ThreadPoolExecutor(concurrency) as ex:
        futs = [ex.submit(fetch_page, s * count, count) for s in range(pages)]
        for i, fut in enumerate(as_completed(futs), 1):
            _, ids, _ = fut.result()
            for x in ids:
                seen[x] = None
            empty_streak = empty_streak + 1 if not ids else 0
            if i % 100 == 0:
                print(
                    "      %d/%d 页  已收 %d 个  用时 %.0fs"
                    % (i, pages, len(seen), time.time() - t0),
                    flush=True,
                )
            if empty_streak >= 25:
                print("      连续 25 页为空，提前停止（可能被限流）", file=sys.stderr)
                break
    return sorted(seen)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="../data/appids.jsonl")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--no-v2", action="store_true", help="强制走搜索分页")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    ids = [] if args.no_v2 else try_v2()
    if not ids:
        ids = by_search(args.concurrency, args.count)
    if not ids:
        print("错误：拿不到任何 appid", file=sys.stderr)
        return 1

    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        for x in ids:
            f.write("%d\n" % x)
    print("完成：%d 个 appid -> %s" % (len(ids), args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
