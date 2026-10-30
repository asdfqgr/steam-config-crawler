"""第二轮探测：测 appdetails 的可持续速率，以及 SteamSpy 分页是否可用。

要回答：
  1. 被打成 403 之后，等多久能恢复？
  2. 按「一批 + 间隔」的节奏，能稳定跑多少请求/分钟？
  3. SteamSpy request=all 分页能不能替代已废弃的 GetAppList？
"""
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DETAIL = "https://store.steampowered.com/api/appdetails?appids={aid}&l=schinese"
STEAMSPY = "https://steamspy.com/api.php?request=all&page={p}"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REAL = [730, 570, 578080, 1172470, 230410, 381210, 271590, 292030, 1091500,
        2358720, 1245620, 1174180, 400, 620, 440, 550, 105600, 413150]


def one(aid, timeout=20):
    try:
        req = urllib.request.Request(DETAIL.format(aid=aid), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return "ok"
    except urllib.error.HTTPError as exc:
        return str(exc.code)
    except Exception:  # noqa: BLE001
        return "neterr"


def get_json(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def main():
    ids = [a for a in REAL for _ in range(30)]
    print("[1] 先打到被限制，确认触发点")
    burned = 0
    with ThreadPoolExecutor(5) as ex:
        for round_no in range(50):
            batch = ids[burned % 300: burned % 300 + 20]
            if not batch:
                batch = ids[:20]
            res = list(ex.map(one, batch))
            burned += len(batch)
            n_ok = res.count("ok")
            from collections import Counter
            print("   第%2d批 累计%4d  %s" % (round_no + 1, burned, dict(Counter(res))))
            if n_ok == 0:
                break
            time.sleep(1.0)

    print("\n[2] 测恢复：每 20 秒探 3 个，看多久解封")
    t0 = time.time()
    for i in range(15):
        res = [one(a) for a in REAL[:3]]
        print("   +%3.0fs  %s" % (time.time() - t0, res))
        if res.count("ok") == 3:
            print("   已恢复")
            break
        time.sleep(20)

    print("\n[3] 按节奏跑：每批 40 个、间隔 6 秒，共 10 批")
    t0 = time.time()
    total_ok = 0
    with ThreadPoolExecutor(5) as ex:
        for r in range(10):
            res = list(ex.map(one, ids[:40]))
            n = res.count("ok")
            total_ok += n
            from collections import Counter
            print("   批%d  ok=%2d  %s" % (r + 1, n, dict(Counter(res))))
            time.sleep(6)
    el = time.time() - t0
    print("   合计 ok=%d / 400，用时 %.0fs，约 %.1f 请求/分钟" % (total_ok, el, total_ok / el * 60))

    print("\n[4] SteamSpy 分页")
    for p in (0, 1, 2, 60, 100):
        try:
            d = get_json(STEAMSPY.format(p=p))
            keys = list(d.keys())[:2]
            print("   page %3d  %d 条  样例 appid=%s name=%s"
                  % (p, len(d), keys, (d.get(keys[0]) or {}).get("name", "")[:30] if keys else ""))
        except urllib.error.HTTPError as exc:
            print("   page %3d  HTTP %d" % (p, exc.code))
        except Exception as exc:  # noqa: BLE001
            print("   page %3d  失败 %s" % (p, str(exc)[:40]))
        time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
