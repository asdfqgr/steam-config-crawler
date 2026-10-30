# SteamCrawler — Steam 游戏配置采集器

把 Steam 全量游戏的**系统配置要求**离线采集下来，生成一个可以内置进 `GameChecker.exe` 的 SQLite 数据库。软件运行时**不联网**，一个请求都不发。

## 为什么要在 GitHub Actions 上跑

国内直连 `store.steampowered.com` 是**间歇性可通**的，实测同一天内会出现：

| 时段 | 状态 |
|---|---|
| 02:00 | HTTP 200，单请求 1.18 秒，20 并发零失败 |
| 08:20 | 全部 TCP 层超时，DNS 解析到不同 IP 全部连不上 |
| 08:24 | 3 次里成功 1 次，但耗时 7.86 秒 |

在这种链路上跑 18 万条，有效速率可能只有 1–2 请求/秒，还要跑 40 多个小时。GitHub Actions 的服务器在海外，链路稳定得多，全量约 3–6 小时跑完，而且免费。

## 部署步骤

1. 新建一个 GitHub 仓库（**公开仓库的 Actions 完全免费**；私有仓库每月 2000 分钟额度，一次全量约消耗 400–500 分钟）
2. 把本目录所有文件推上去，注意 `.github/workflows/crawl.yml` 必须在仓库根目录下的 `.github/workflows/`
3. 进仓库的 **Actions** 标签页 → 左侧选「Steam 配置采集」→ 右侧 **Run workflow**
4. 首次建议先填 `limit = 500` 跑一次，确认流程正常（约 2 分钟）
5. 确认没问题后，`limit` 填 `0` 跑全量
6. 跑完在 Actions 页面最下方下载 `steam-db` 产物，得到 `steam_games.db.gz`

## 三个 job 各自干什么

```
applist   抓全量 appid 列表（约 18.7 万个，8 分钟）
   ↓
crawl     20 个分片并行，每片约 9300 个，各自断点续传
   ↓
merge     合并去重 → steam_games.db → gzip
```

单片跑失败不影响其他片（`fail-fast: false`）。任何一片失败了，**直接重跑整个 workflow 即可**——`worker.py` 会读取已有的 `part-*.jsonl` 和 `skip-*.jsonl`，把已处理的跳过，只补没采到的。

## 本地运行

```bash
cd crawler

# 1. 拿 appid 列表
python applist.py --out ../data/appids.jsonl --concurrency 8

# 2. 采集（分片可选，单机跑就不分片）
python worker.py --ids ../data/appids.jsonl --out ../data/part-00.jsonl \
                 --shard 0 --shards 1 --concurrency 8

# 3. 合并
python merge.py --parts ../data --out ../data/steam_games.db
```

调试时用 `--limit 100` 只跑前 100 个。

## 断点续传怎么工作

目录下有两个文件：

- `part-XX.jsonl` —— 采到的游戏，一行一条 JSON
- `skip-XX.jsonl` —— 确认无数据的 appid（DLC、已下架、不存在）

重跑时会读这两个文件，**两者都算已处理**。这点很关键：全量 18.7 万里约有 **三成** 是 DLC 或无效 appid，它们不产出记录；如果只按成功记录判断，每次重跑都会把这 5 万多个再请求一遍，白白浪费大半时间。

而网络失败**不会**写进跳过表，所以下次一定会重试。

## 对不稳定的三重防护

| 机制 | 行为 |
|---|---|
| 重试 | 每个 appid 最多 3 次，指数退避（1.5s / 3s / 4.5s） |
| 遇 403/429 | 等 20s / 40s / 60s 再试 |
| 全局暂停 | 连续 8 个失败就整体停 45 秒，避免把 IP 打爆；恢复后自动继续 |

## 数据库结构

```sql
games (
  appid, name, name_norm, release, url,
  min_json, rec_json,                    -- 完整原始字段
  min_os, min_cpu, min_ram_gb, min_gpu, min_vram_gb, min_dx, min_disk_gb,
  rec_os, rec_cpu, rec_ram_gb, rec_gpu, rec_vram_gb, rec_dx, rec_disk_gb
)
alias (appid, name, name_norm, source)   -- source: steam / steam-en / lzlgo
```

`name_norm` 是归一化后的搜索键（小写、去空格标点、去版本号），**必须和客户端 C# 侧的归一化逻辑完全一致**，否则匹配不上。

`alias` 表同时存中文名、英文原名（`steam-en`）和站内译名（`lzlgo`），
所以搜「赛博朋克」「cyberpunk」「Cyberpunk 2077」都能命中同一条记录。

## 解析规则里的几个坑

这些都是实测踩出来、已写进 `test_parser.py` 断言的：

1. **`<p>` 和 `<ul><li>` 两种排版都要解析**。只解析 `<p>` 会漏掉约 24% 的游戏（《星露谷物语》就是列表排版）。
2. **`GTX 1060` 的 1060 不是显存**。显存只从带 GB/MB 单位的数字里取，且限制在 0.5–32 之间。
3. **`1024 MB RAM` 要换算成 1 GB**。
4. **内存和存储混在一行时不能取错**，取容量要优先看「存储/硬盘/空间」附近的数字。
5. **`9 Dec, 2020` 里的 9 是日期不是月份**，月份只能靠英文月份名判断。
6. 解析不出来就返回空，让上层跳过该项——绝不能填 0，否则会把「未知」判成「不达标」。

## 已知限制

- **请求不能批量**：`appids=730,570` 会返回 400，只能一个 appid 一次请求
- **英文名要额外一次请求**：`l=schinese` 对有官方中文名的游戏只返回中文名，所以这类游戏会再请求一次 `l=english`。纯英文名的游戏会跳过这步，省掉约一半请求
- **不是所有游戏都有推荐配置**：约 60% 有最低+推荐双配置，20% 只有单侧
- **无中文配置的游戏**返回英文配置（如《战争雷霆》），解析器同样能处理
