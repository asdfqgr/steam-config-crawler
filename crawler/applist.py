"""抓取 Steam 游戏 appid 列表。

三条路，按实测可用性排序（GitHub Actions 环境）：
  1. SteamSpy 分页        实测 HTTP 200，1000 条/页，约 60-90 页就够，最快最稳
  2. 商店搜索分页         搜索接口对 GitHub IP 大量 403，几乎不可用，仅作兜底
  3. GetAppList v2        已废弃，返回 404

SteamSpy 只收录「有人玩过」的游戏（约 6-9 万款），正好过滤掉大量空壳/DLC，
对本工具是优点不是缺点——用户要查的本来就是有人在玩的游戏。

用法:
    python applist.py --out ../data/appids.jsonl
    python applist.py --source search     # 强制走搜索分页
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

STEAMSPY = "https://steamspy.com/api.php?request=all&page={p}"
SEARCH = (
    "https://store.steampowered.com/search/results/"
    "?start={start}&count={count}&infinite=1&json=1&l=schinese&category1=998"
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_APPID = re.compile(r'data-ds-appid="(\d+)"')

# 名字里明确是附属内容/工具的，直接不要，省下大量详情请求
_NOISE = re.compile(
    r"(soundtrack|original soundtrack|\bost\b|dedicated server|\bsdk\b|"
    r"demo$|playtest|\bbeta\b|\bdlc\b|season pass|artbook|art book|"
    r"wallpaper|companion|benchmark|test server)",
    re.I,
)


def _get(url: str, timeout: int = 40) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def by_steamspy() -> list[int]:
    print("[1/2] SteamSpy 分页抓取 ...")
    seen: dict[int, None] = {}
    noise = 0
    page = 0
    empty = 0
    t0 = time.time()
    while page < 300:
        try:
            d = json.loads(_get(STEAMSPY.format(p=page)))
        except urllib.error.HTTPError as exc:
            print("  page %d HTTP %d，作为结尾处理" % (page, exc.code))
            empty += 1
            if empty >= 2:
                break
            page += 1
            time.sleep(2)
            continue
        except Exception as exc:  # noqa: BLE001
            print("  page %d 失败 %s" % (page, str(exc)[:50]))
            empty += 1
            if empty >= 3:
                break
            page += 1
            time.sleep(3)
            continue
        if not d:
            print("  page %d 空页，结束" % page)
            break
        empty = 0
        for k, v in d.items():
            try:
                aid = int(k)
            except Exception:  # noqa: BLE001
                continue
            if _NOISE.search((v or {}).get("name") or ""):
                noise += 1
                continue
            seen[aid] = None
        if page % 10 == 0:
            print("  page %3d  累计 %6d 个  %.0fs" % (page, len(seen), time.time() - t0), flush=True)
        page += 1
        time.sleep(0.4)
    print("      共 %d 页，收 %d 个，过滤附属内容 %d 个（%.0fs）"
          % (page, len(seen), noise, time.time() - t0))
    return sorted(seen)


def by_search(concurrency: int, count: int) -> list[int]:
    print("[1/2] 探测游戏总数 ...")
    total = 0
    for attempt in range(4):
        try:
            d = json.loads(_get(SEARCH.format(start=0, count=count), timeout=25))
            total = int(d.get("total_count") or 0)
            break
        except Exception as exc:  # noqa: BLE001
            print("  探测失败 %s，重试" % str(exc)[:50])
            time.sleep(5 * (attempt + 1))
    if not total:
        return []
    pages = math.ceil(total / count)
    print("      总数 %d，共 %d 页（搜索接口易 403，会比较慢）" % (total, pages))

    seen: dict[int, None] = {}
    t0 = time.time()

    def page_at(s: int) -> list[int]:
        for attempt in range(6):
            try:
                d = json.loads(_get(SEARCH.format(start=s, count=count), timeout=25))
                return [int(x) for x in _APPID.findall(d.get("results_html", ""))]
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 429):
                    time.sleep(20 * (attempt + 1))
                else:
                    return []
            except Exception:  # noqa: BLE001
                time.sleep(5 * (attempt + 1))
        return []

    print("[2/2] 抓取分页 ...")
    with ThreadPoolExecutor(concurrency) as ex:
        futs = [ex.submit(page_at, s * count) for s in range(pages)]
        for i, fut in enumerate(as_completed(futs), 1):
            for x in fut.result():
                seen[x] = None
            if i % 100 == 0:
                print("      %d/%d 页  已收 %d 个  %.0fs"
                      % (i, pages, len(seen), time.time() - t0), flush=True)
    return sorted(seen)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="../data/appids.jsonl")
    ap.add_argument("--source", default="steamspy", choices=["steamspy", "search"])
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--count", type=int, default=100)
    args = ap.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    ids = by_steamspy() if args.source == "steamspy" else by_search(args.concurrency, args.count)
    if not ids:
        print("SteamSpy 失败，回退到搜索分页", file=sys.stderr)
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
