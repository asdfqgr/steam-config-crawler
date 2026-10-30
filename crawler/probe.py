"""探测 Steam 各接口在 GitHub Actions 环境下的可用性。

只在 workflow 里跑，用来回答两个问题：
  1. appdetails 会不会像 search 一样被 429？
  2. 全量 appid 列表还有哪些可用来源？
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DETAIL = "https://store.steampowered.com/api/appdetails?appids={aid}&l=schinese"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 一批真实、长期在架的游戏
REAL = [
    730, 570, 578080, 1172470, 230410, 381210, 271590, 292030, 1091500,
    2358720, 1245620, 1174180, 400, 620, 440, 550, 105600, 413150,
]


def one(aid, timeout=20):
    t = time.time()
    try:
        req = urllib.request.Request(DETAIL.format(aid=aid), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return "ok", time.time() - t
    except urllib.error.HTTPError as exc:
        return str(exc.code), time.time() - t
    except Exception:  # noqa: BLE001
        return "neterr", time.time() - t


def tally(results):
    from collections import Counter
    c = Counter(r[0] for r in results)
    lat = sorted(r[1] for r in results)
    return dict(c), (lat[len(lat) // 2] if lat else 0)


def phase(label, ids, concurrency, delay_between_batches=0.0):
    t0 = time.time()
    with ThreadPoolExecutor(concurrency) as ex:
        res = list(ex.map(lambda a: one(a), ids))
    c, med = tally(res)
    print("  %-28s 并发%d  %d 个请求  %.1fs  结果=%s  中位延迟=%.2fs"
          % (label, concurrency, len(ids), time.time() - t0, c, med))
    return c


def probe_urls():
    urls = [
        ("GetAppList v2", "https://api.steampowered.com/ISteamApps/GetAppList/v2/"),
        ("GetAppList v0002", "https://api.steampowered.com/ISteamApps/GetAppList/v0002/?format=json"),
        ("steamcmd.net apps", "https://api.steamcmd.net/v1/apps"),
        ("steamcmd.net info", "https://api.steamcmd.net/v1/info/730"),
        ("steamspy all", "https://steamspy.com/api.php?request=all&page=0"),
        ("steam search cat=998", "https://store.steampowered.com/search/results/?start=0&count=100&infinite=1&json=1&l=schinese&category1=998"),
        ("steam search 无过滤", "https://store.steampowered.com/search/results/?start=0&count=100&infinite=1&json=1&l=schinese"),
    ]
    for name, u in urls:
        t = time.time()
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                body = r.read()
            print("  %-24s HTTP 200  %8.1f KB  %.1fs" % (name, len(body) / 1024.0, time.time() - t))
        except urllib.error.HTTPError as exc:
            print("  %-24s HTTP %d  %.1fs" % (name, exc.code, time.time() - t))
        except Exception as exc:  # noqa: BLE001
            print("  %-24s 失败 %s  %.1fs" % (name, str(exc)[:40], time.time() - t))


def main():
    ids = [a for a in REAL for _ in range(12)][:220]
    print("[A] appdetails 压力测试（重复真实 appid，模拟真实负载）")
    phase("并发4 无间隔", ids, 4)
    time.sleep(5)
    phase("并发2 无间隔", ids[:120], 2)
    time.sleep(5)
    phase("并发1 无间隔", ids[:60], 1)

    print("\n[B] 全量列表候选源")
    probe_urls()

    print("\n[C] 搜索接口连续 40 页（并发1，间隔1秒）")
    got = 0
    for i in range(40):
        u = ("https://store.steampowered.com/search/results/?start=%d&count=100"
             "&infinite=1&json=1&l=schinese&category1=998" % (i * 100))
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.loads(r.read())
            ids_page = re.findall(r'data-ds-appid="(\d+)"', d.get("results_html", ""))
            got += len(ids_page)
            print("  page %2d  ok  %4d 个  累计 %5d  total=%s"
                  % (i, len(ids_page), got, d.get("total_count")))
        except urllib.error.HTTPError as exc:
            print("  page %2d  HTTP %d" % (i, exc.code))
        except Exception as exc:  # noqa: BLE001
            print("  page %2d  失败 %s" % (i, str(exc)[:40]))
        time.sleep(1.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
