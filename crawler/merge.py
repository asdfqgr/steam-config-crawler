"""把所有分片合并成一个 SQLite 库，供 GameChecker 内置使用。

用法:
    python merge.py --parts ../data --out ../data/steam_games.db

可选：--alias-from <jsonl> 把 lzlgo.com 抓来的中文别名一并写进 alias 表，
让「萨姆森」这类站内译名也能搜到 Steam 上的原名。

产物:
    steam_games.db
      games   主表，一行一个游戏
      alias   别名表，中文译名/站内标题 -> appid
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
import unicodedata

SCHEMA = """
CREATE TABLE games (
    appid       INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    name_norm   TEXT NOT NULL,
    release     TEXT,
    url         TEXT,
    min_json    TEXT,
    rec_json    TEXT,
    min_os TEXT, min_cpu TEXT, min_ram_gb REAL, min_gpu TEXT, min_vram_gb REAL, min_dx TEXT, min_disk_gb REAL,
    rec_os TEXT, rec_cpu TEXT, rec_ram_gb REAL, rec_gpu TEXT, rec_vram_gb REAL, rec_dx TEXT, rec_disk_gb REAL
);
CREATE TABLE alias (
    appid     INTEGER NOT NULL,
    name      TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    source    TEXT
);
CREATE INDEX idx_games_norm ON games(name_norm);
CREATE INDEX idx_games_release ON games(release);
CREATE INDEX idx_alias_norm ON alias(name_norm);
"""

_NOISE = re.compile(
    r"(免安装|中文版|英文版|绿色版|解压即撸|单机|联机|送修改器|虚拟机版|全dlc|豪华版|标准版|"
    r"build\.?\d+|v\d+(?:\.\d+)*|update|repack)",
    re.I,
)


def normalize_name(s: str) -> str:
    """名称归一化。必须和客户端 C# 侧保持一致，否则匹配不上。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.lower()
    s = _NOISE.sub(" ", s)
    s = re.sub(r"[\s\-_—–·・:：,，.。!！?？'\"‘’“”()（）\[\]【】/\\|]+", "", s)
    return s.strip()


def iter_parts(folder: str):
    for path in sorted(glob.glob(os.path.join(folder, "part-*.jsonl"))):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:  # noqa: BLE001
                    continue


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", default="../data")
    ap.add_argument("--out", default="../data/steam_games.db")
    ap.add_argument("--alias-from", default="", help="lzlgo 别名 jsonl")
    args = ap.parse_args()

    if os.path.exists(args.out):
        os.remove(args.out)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    db = sqlite3.connect(args.out)
    db.executescript(SCHEMA)

    seen: set[int] = set()
    rows = []
    records: list[dict] = []
    has_min = has_rec = has_release = 0
    for rec in iter_parts(args.parts):
        aid = rec.get("appid")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        records.append(rec)
        mn = rec.get("min") or {}
        rc = rec.get("rec") or {}
        if mn:
            has_min += 1
        if rc:
            has_rec += 1
        if rec.get("release"):
            has_release += 1
        name = (rec.get("name") or "").strip()
        rows.append(
            (
                aid, name, normalize_name(name), rec.get("release"), rec.get("url"),
                json.dumps(mn, ensure_ascii=False) if mn else None,
                json.dumps(rc, ensure_ascii=False) if rc else None,
                mn.get("os"), mn.get("cpu"), mn.get("ram_gb"), mn.get("gpu"),
                mn.get("vram_gb"), mn.get("dx"), mn.get("disk_gb"),
                rc.get("os"), rc.get("cpu"), rc.get("ram_gb"), rc.get("gpu"),
                rc.get("vram_gb"), rc.get("dx"), rc.get("disk_gb"),
            )
        )

    db.executemany("INSERT OR REPLACE INTO games VALUES (%s)" % ",".join("?" * 21), rows)

    alias_rows = []
    # Steam 自己的名字也进别名表，方便统一搜索
    n_en = 0
    for rec, r in zip(records, rows):
        if r[1]:
            alias_rows.append((r[0], r[1], r[2], "steam"))
        en = (rec.get("name_en") or "").strip()
        if en and en != r[1]:
            alias_rows.append((r[0], en, normalize_name(en), "steam-en"))
            n_en += 1
    if args.alias_from and os.path.exists(args.alias_from):
        n_extra = 0
        with open(args.alias_from, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    a = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                aid, nm = a.get("appid"), (a.get("name") or "").strip()
                if aid and nm:
                    alias_rows.append((aid, nm, normalize_name(nm), a.get("source") or "lzlgo"))
                    n_extra += 1
        print("额外别名: %d 条" % n_extra)

    db.executemany("INSERT INTO alias VALUES (?,?,?,?)", alias_rows)
    db.commit()

    size_mb = os.path.getsize(args.out) / 1048576
    print("=" * 52)
    print("合并完成 -> %s" % args.out)
    print("  游戏总数    %6d" % len(rows))
    print("  有最低配置  %6d  (%.1f%%)" % (has_min, 100 * has_min / max(len(rows), 1)))
    print("  有推荐配置  %6d  (%.1f%%)" % (has_rec, 100 * has_rec / max(len(rows), 1)))
    print("  有发售日期  %6d  (%.1f%%)" % (has_release, 100 * has_release / max(len(rows), 1)))
    print("  别名条目    %6d  (其中英文原名 %d)" % (len(alias_rows), n_en))
    print("  库文件大小  %.1f MB" % size_mb)

    # 抽查
    cur = db.execute(
        "SELECT name, release, min_ram_gb, min_gpu FROM games "
        "WHERE min_ram_gb IS NOT NULL ORDER BY appid LIMIT 5"
    )
    print("-" * 52)
    print("抽查（有内存字段的前 5 条）:")
    for name, rel, ram, gpu in cur:
        print("  %-34s %-8s %4sGB  %s" % ((name or "")[:32], rel or "-", ram or "-", (gpu or "")[:28]))
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
