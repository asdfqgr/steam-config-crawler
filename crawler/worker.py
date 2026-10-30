"""按分片采集 Steam 游戏配置。

针对实测到的配额限制专门设计（GitHub Actions 上实测结论）：
  单 IP 突发配额约 200-240 个请求，之后返回 429；约 5 分钟完全恢复。
  → 可持续约 2400 请求/小时/IP。猛跑只会让之后几分钟全是 429，反而更慢。

因此这里用「令牌桶 + 长退避」：
  令牌桶    平稳消耗配额，不触发 429（并发只是让请求排满，不改变总速率）
  长退避    真被 429 就打住整个进程 45s/90s/180s/300s，而不是硬撞
  403/429 一律当作可重试，绝不写进跳过表，避免永久丢数据

其它特性：
  断点续传  已采过的 appid 直接跳过，中断重跑只补剩下的
  边写边落盘 每完成一条立刻 flush，进程被杀也不丢已采数据

用法:
    python worker.py --ids ../data/appids.jsonl --out ../data/part-00.jsonl \
                     --shard 0 --shards 20 --concurrency 4
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from parser import build_record

DETAIL = "https://store.steampowered.com/api/appdetails?appids={aid}&l={lang}"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 令牌桶参数：突发放 110 个（实测 200 才触发 429，留足余量），
# 之后每秒补 0.5 个 = 1800 请求/小时，比实测可持续水位 2400 保守 25%。
BURST = float(os.environ.get("STEAM_BURST", 110))
RATE = float(os.environ.get("STEAM_RATE", 0.5))
BACKOFF = [45, 90, 180, 300, 300, 300]

_lock = threading.Lock()
_pause_until = 0.0
_stats = {"ok": 0, "skip": 0, "fail": 0, "blocked": 0}


class Bucket:
    def __init__(self, cap: float, rate: float):
        self.cap = cap
        self.rate = rate
        self.tokens = cap
        self.ts = time.time()
        self.lock = threading.Lock()

    def take(self) -> None:
        while True:
            with self.lock:
                now = time.time()
                self.tokens = min(self.cap, self.tokens + (now - self.ts) * self.rate)
                self.ts = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                need = (1 - self.tokens) / self.rate
            time.sleep(min(need, 1.0))

    def drain(self) -> None:
        with self.lock:
            self.tokens = 0.0
            self.ts = time.time()


_bucket = Bucket(BURST, RATE)


def _bump(key: str) -> None:
    with _lock:
        _stats[key] += 1


def _block(seconds: float) -> None:
    """整个进程暂停：被限流时大家都停，别继续硬撞。"""
    global _pause_until
    with _lock:
        _pause_until = max(_pause_until, time.time() + seconds)
    _bucket.drain()


def _wait_if_paused() -> None:
    while True:
        with _lock:
            remain = _pause_until - time.time()
        if remain <= 0:
            return
        time.sleep(min(remain, 2.0))


OK, SKIP, RETRY = "ok", "skip", "retry"


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in s or "")


def _get_json(appid: int, lang: str, timeout: int) -> dict | None:
    req = urllib.request.Request(
        DETAIL.format(aid=appid, lang=lang), headers={"User-Agent": UA}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        node = json.loads(r.read()).get(str(appid)) or {}
    return node.get("data") if node.get("success") else None


def fetch_detail(appid: int, retries: int = 8, timeout: int = 25) -> tuple[str, dict | None]:
    """取单个游戏详情并解析。

    返回 (状态, 记录)：
      ("ok",    rec)  采到配置
      ("skip",  None) 确认无数据（DLC/已下架/无配置）——以后不用再请求
      ("retry", None) 网络失败 / 一直限流——不写跳过表，下次必须重来

    名字处理：`l=schinese` 对有官方中文名的游戏只返回中文名，
    玩家搜 "cyberpunk" 会查不到 "赛博朋克 2077"。所以中文名含汉字时，
    额外用 `l=english` 再取一次原名；纯英文名的游戏跳过这步。
    """
    block_hits = 0
    for attempt in range(retries):
        _wait_if_paused()
        _bucket.take()
        try:
            data = _get_json(appid, "schinese", timeout)
            if data is None:
                return SKIP, None
            rec = build_record(appid, data)
            if rec is None:
                return SKIP, None
            if _has_cjk(rec["name"]):
                _wait_if_paused()
                _bucket.take()
                try:
                    en = _get_json(appid, "english", timeout)
                    if en and en.get("name") and not _has_cjk(en["name"]):
                        rec["name_en"] = en["name"]
                except urllib.error.HTTPError as exc:
                    if exc.code in (403, 429):
                        _block(BACKOFF[min(block_hits, len(BACKOFF) - 1)])
                        block_hits += 1
                        _bump("blocked")
                except Exception:  # noqa: BLE001
                    pass  # 英文名拿不到不影响主数据
            return OK, rec
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                # 限流：退避后重试同一个 id，不算失败
                _block(BACKOFF[min(block_hits, len(BACKOFF) - 1)])
                block_hits += 1
                _bump("blocked")
                continue
            if attempt == retries - 1:
                _bump("fail")
                return RETRY, None
            time.sleep(1.5 * (attempt + 1))
        except Exception:  # noqa: BLE001
            if attempt == retries - 1:
                _bump("fail")
                return RETRY, None
            time.sleep(1.5 * (attempt + 1))
    _bump("fail")
    return RETRY, None


def load_done(paths: list[str]) -> set[int]:
    """读已有输出 + 跳过表，得到「已处理过」的 appid 集合。"""
    done: set[int] = set()
    for p in paths:
        if not os.path.exists(p):
            continue
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                aid = obj.get("appid") if isinstance(obj, dict) else None
                if aid is None and line.isdigit():
                    aid = int(line)
                if aid:
                    done.add(int(aid))
    return done


def _progress(done: int, total: int, t0: float) -> None:
    el = max(time.time() - t0, 0.001)
    rate = done / el
    remain = (total - done) / rate if rate > 0 else 0
    print(
        "      %d/%d  已写 %d  速率 %.2f/s  成功 %d 跳过 %d 失败 %d 限流 %d  预计剩余 %.0f 分钟"
        % (done, total, _stats["ok"], rate, _stats["ok"], _stats["skip"],
           _stats["fail"], _stats["blocked"], remain / 60),
        flush=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", required=True, help="appids.jsonl")
    ap.add_argument("--out", required=True, help="输出 jsonl")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="只跑 N 个（调试用，等距抽样）")
    args = ap.parse_args()

    with open(args.ids, "r", encoding="utf-8") as f:
        all_ids = [int(x) for x in f.read().split() if x.strip()]
    mine = [a for i, a in enumerate(all_ids) if i % args.shards == args.shard]
    if args.limit and len(mine) > args.limit:
        # 等距抽样而不是取前 N 个：appid 是升序的，取前 N 会全是上古小 ID。
        step = len(mine) / float(args.limit)
        mine = [mine[int(i * step)] for i in range(args.limit)]

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    outdir = os.path.dirname(os.path.abspath(args.out))
    skip_path = os.path.join(outdir, "skip-%02d.jsonl" % args.shard)
    parts = sorted(
        os.path.join(outdir, n)
        for n in os.listdir(outdir or ".")
        if n.endswith(".jsonl") and (n.startswith("part-") or n.startswith("skip-"))
    )
    done = load_done(parts)
    todo = [a for a in mine if a not in done]

    print(
        "分片 %d/%d：本片 %d 个，已处理 %d，待采 %d，并发 %d，速率上限 %.0f/小时"
        % (args.shard, args.shards, len(mine), len(mine) - len(todo), len(todo),
           args.concurrency, RATE * 3600)
    )
    if not todo:
        print("本片已全部完成")
        return 0

    t0 = time.time()
    written = 0
    with open(args.out, "a", encoding="utf-8", newline="\n") as fout, open(
        skip_path, "a", encoding="utf-8", newline="\n"
    ) as fskip:
        with ThreadPoolExecutor(args.concurrency) as ex:
            futs = {ex.submit(fetch_detail, a): a for a in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                status, rec = fut.result()
                if status == OK and rec:
                    fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fout.flush()
                    written += 1
                    _bump("ok")
                elif status == SKIP:
                    fskip.write("%d\n" % futs[fut])
                    fskip.flush()
                    _bump("skip")
                else:
                    _bump("fail")
                if i % 50 == 0:
                    _progress(i, len(todo), t0)

    el = time.time() - t0
    print(
        "分片 %d 结束：采集 %d 条，成功 %d / 跳过 %d / 失败 %d / 限流 %d，用时 %.0f 分钟"
        % (args.shard, written, _stats["ok"], _stats["skip"], _stats["fail"],
           _stats["blocked"], el / 60)
    )
    return 0 if written or _stats["skip"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
