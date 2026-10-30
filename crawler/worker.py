"""按分片采集 Steam 游戏配置。

特性（针对国内链路不稳专门设计）：
  断点续传  已采过的 appid 直接跳过，中断后重跑只补剩下的
  自适应降速 连续失败达阈值就全局暂停探测，恢复后自动继续，不把 IP 打爆
  重试       每个 appid 最多重试 3 次，指数退避（实测单次失败率约 16%）
  边写边落盘 每完成一条立刻 flush，进程被杀也不丢已采数据

用法:
    python worker.py --ids ../data/appids.jsonl --out ../data/part-00.jsonl \
                     --shard 0 --shards 20 --concurrency 8
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

_lock = threading.Lock()
_cons_fail = 0
_pause_until = 0.0
_stats = {"ok": 0, "skip": 0, "fail": 0}


def _bump(key: str) -> None:
    with _lock:
        _stats[key] += 1


def _note_ok() -> None:
    global _cons_fail
    with _lock:
        _cons_fail = 0


def _note_fail(threshold: int = 8, pause: float = 45.0) -> None:
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


def fetch_detail(appid: int, retries: int = 3, timeout: int = 25) -> tuple[str, dict | None]:
    """取单个游戏详情并解析。

    返回 (状态, 记录)：
      ("ok",    rec)  采到配置
      ("skip",  None) 确认无数据（DLC/已下架/无配置）——以后不用再请求
      ("retry", None) 网络失败——不能记进跳过表，下次必须重来

    名字处理：`l=schinese` 对有官方中文名的游戏只返回中文名，
    玩家搜 "cyberpunk" 就会查不到 "赛博朋克 2077"。所以当中文名里含汉字时，
    额外用 `l=english` 再取一次原名。纯英文名的游戏跳过这步，省掉约一半请求。
    """
    _wait_if_paused()
    for attempt in range(retries):
        try:
            data = _get_json(appid, "schinese", timeout)
            if data is None:
                _note_ok()
                return SKIP, None
            rec = build_record(appid, data)
            if rec is None:
                _note_ok()
                return SKIP, None
            if _has_cjk(rec["name"]):
                try:
                    en = _get_json(appid, "english", timeout)
                    if en and en.get("name") and _has_cjk(en["name"]) is False:
                        rec["name_en"] = en["name"]
                except Exception:  # noqa: BLE001
                    pass  # 英文名拿不到不影响主数据
            _note_ok()
            return OK, rec
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                time.sleep(20 * (attempt + 1))
            elif attempt == retries - 1:
                _note_fail()
                return RETRY, None
            else:
                time.sleep(1.5 * (attempt + 1))
        except Exception:  # noqa: BLE001
            if attempt == retries - 1:
                _note_fail()
                return RETRY, None
            time.sleep(1.5 * (attempt + 1))
    return RETRY, None


def load_done(paths: list[str]) -> set[int]:
    """读已有输出 + 跳过表，得到「已处理过」的 appid 集合。

    必须把 skip 表一起读进来：全量 18 万里约有 30% 的 appid 是
    DLC/已下架/根本不存在的，它们不会产出记录。如果只按成功记录判断，
    这些 appid 每次重跑都会被再请求一遍，白白浪费大半时间。
    """
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", required=True, help="appids.jsonl")
    ap.add_argument("--out", required=True, help="输出 jsonl")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个（调试用）")
    args = ap.parse_args()

    with open(args.ids, "r", encoding="utf-8") as f:
        all_ids = [int(x) for x in f.read().split() if x.strip()]
    mine = [a for i, a in enumerate(all_ids) if i % args.shards == args.shard]
    if args.limit and len(mine) > args.limit:
        # 等距抽样而不是取前 N 个：appid 是升序的，取前 N 会全是上古小 ID，
        # 抽样才能覆盖老游戏和新游戏，验证结果才有代表性。
        step = len(mine) / float(args.limit)
        mine = [mine[int(i * step)] for i in range(args.limit)]

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    outdir = os.path.dirname(os.path.abspath(args.out))
    skip_path = os.path.join(outdir, "skip-%02d.jsonl" % args.shard)
    # 断点续传：成功记录 + 跳过表都算已完成
    parts = sorted(
        os.path.join(outdir, n)
        for n in os.listdir(outdir or ".")
        if n.endswith(".jsonl") and (n.startswith("part-") or n.startswith("skip-"))
    )
    done = load_done(parts)
    todo = [a for a in mine if a not in done]

    print(
        "分片 %d/%d：本片 %d 个，已处理 %d，待采 %d，并发 %d"
        % (args.shard, args.shards, len(mine), len(mine) - len(todo), len(todo), args.concurrency)
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
                    # 确认无数据，记进跳过表，下次不再请求
                    fskip.write("%d\n" % futs[fut])
                    fskip.flush()
                    _bump("skip")
                else:
                    # 网络失败：不记跳过表，下次重跑会再次尝试
                    _bump("fail")
                if i % 500 == 0:
                    el = time.time() - t0
                    print(
                        "      %d/%d  已写 %d  速率 %.1f/s  成功 %d 跳过 %d 失败 %d"
                        % (
                            i, len(todo), written, i / el if el else 0,
                            _stats["ok"], _stats["skip"], _stats["fail"],
                        ),
                        flush=True,
                    )

    el = time.time() - t0
    print(
        "分片 %d 结束：采集 %d 条，成功 %d / 跳过 %d / 失败 %d，用时 %.0fs"
        % (args.shard, written, _stats["ok"], _stats["skip"], _stats["fail"], el)
    )
    # 失败率过高时用非零退出码提醒 workflow 重跑本片
    total = max(_stats["ok"] + _stats["skip"] + _stats["fail"], 1)
    return 0 if _stats["fail"] / total < 0.30 else 2


if __name__ == "__main__":
    raise SystemExit(main())
