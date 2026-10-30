"""Steam pc_requirements 解析器。

Steam 返回的 pc_requirements.minimum / .recommended 是一段 HTML，格式不统一：
    <p>最低配置:</p><p>操作系统: Windows 10</p>
    <ul><li><strong>操作系统 *:</strong>Windows 10</li></ul>
    <strong>OS:</strong> Windows 10<br><strong>Processor:</strong> i5-750<br>

而且语言取决于游戏是否有中文本地化，同一份库里中英文混杂。
本模块把两种排版、两种语言统一解析成结构化字段。

设计原则（来自 GameChecker/ConfigTextParser 踩过的坑）：
1. 先把 </p> </li> </tr> <br> 全部换成换行，再剥标签 —— 这样 <p> 和 <li> 两种排版通吃
2. 数值只从明确带单位的串里取，绝不从型号数字里猜
   （GTX 1060 的 1060 不是显存）
3. 显存只接受 1~32 GB，超出必然是型号数字
4. 解析不出来就返回 None，让上层跳过该项，而不是填 0 造成误判
"""

from __future__ import annotations

import html
import re
from typing import Any

GB = 1024.0 ** 3
MB = 1024.0 ** 2

# 标签 -> 标准字段名。顺序有意义：先匹配到的优先。
_LABEL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("dx", re.compile(r"^(directx\s*(版本|version)?)\s*[:：]", re.I)),
    ("os", re.compile(r"^(操作系统|os)\s*\*?\s*[:：]", re.I)),
    ("cpu", re.compile(r"^(处理器|processor|cpu)\s*[:：]", re.I)),
    ("ram", re.compile(r"^(内存|memory|ram)\s*[:：]", re.I)),
    ("gpu", re.compile(r"^(显卡|图形|graphics|video\s*card|gpu)\s*[:：]", re.I)),
    ("disk", re.compile(r"^(存储空间|存储|硬盘|storage|hard\s*(disk|drive)\s*space)\s*[:：]", re.I)),
    ("sound", re.compile(r"^(声卡|sound\s*card)\s*[:：]", re.I)),
    ("network", re.compile(r"^(网络|network)\s*[:：]", re.I)),
    ("notes", re.compile(r"^(附注|additional\s*notes|备注)\s*[:：]", re.I)),
]

# 单纯的分节标题，丢弃
_SECTION = re.compile(r"^(最低配置|推荐配置|minimum|recommended)\s*[:：]?$", re.I)


def html_to_lines(raw: str) -> list[str]:
    """把一段 HTML 配置转成按顺序的纯文本行。

    关键：</p> </li> </h2> <br> 一律换成换行。只做 <p> 会漏掉
    <ul><li> 排版的游戏（实测漏掉 23.8%）。
    """
    if not raw:
        return []
    s = re.sub(r"</(p|li|div|tr|td|h[1-6]|ul|ol|dl|dt|dd)>", "\n", raw, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s).replace("\xa0", " ").replace("®", "").replace("™", "")
    lines = []
    for piece in s.split("\n"):
        piece = re.sub(r"[ \t]+", " ", piece).strip(" \t:：")
        if piece:
            lines.append(piece)
    return lines


def _to_gb(num: float, unit: str) -> float | None:
    u = unit.lower()
    if u.startswith("t"):
        return round(num * 1024, 1)
    if u.startswith("g"):
        return round(num, 1)
    if u.startswith("m"):
        return round(num / 1024, 2)
    return None


_CAP = re.compile(r"(\d+(?:[.,]\d+)?)\s*(TB|GB|MB|G|M)\b", re.I)


def extract_capacity(text: str, prefer: re.Pattern[str] | None = None) -> float | None:
    """从一段文字里抽容量，返回 GB。

    prefer 用来指定优先区域：内存这一项里常出现「可用空间 50 GB」之类，
    需要优先取靠近关键词的那个数。
    """
    if not text:
        return None
    if prefer is not None:
        m = prefer.search(text)
        if m:
            window = text[m.start(): m.start() + 40]
            cap = _CAP.search(window)
            if cap:
                got = _to_gb(float(cap.group(1).replace(",", ".")), cap.group(2))
                if got is not None:
                    return got
    cap = _CAP.search(text)
    if cap:
        return _to_gb(float(cap.group(1).replace(",", ".")), cap.group(2))
    return None


def parse_ram(text: str) -> float | None:
    return extract_capacity(text, re.compile(r"内存|memory|ram", re.I))


def parse_disk(text: str) -> float | None:
    return extract_capacity(text, re.compile(r"存储|硬盘|空间|storage|space|disk", re.I))


def parse_vram(gpu_text: str, own_text: str = "") -> float | None:
    """抽显存。只接受 1~32 GB 的数值。

    `GTX 1060` / `RTX 3060` 里的数字绝不能被当成显存，
    因此这里只认带 GB/MB 单位的数字，且落在 0.5~32 区间。
    """
    for src in (own_text, gpu_text):
        if not src:
            continue
        for m in _CAP.finditer(src):
            got = _to_gb(float(m.group(1).replace(",", ".")), m.group(2))
            if got is not None and 0.5 <= got <= 32:
                return got
    return None


_DX = re.compile(r"(1[0-2]|9(?:\.0[a-c]?)?)\b", re.I)


def parse_dx(text: str) -> str | None:
    m = _DX.search(text or "")
    return m.group(1).lower() if m else None


def parse_block(raw: str) -> dict[str, Any]:
    """解析一段（最低或推荐）配置 HTML，返回字段字典。"""
    out: dict[str, Any] = {}
    for line in html_to_lines(raw):
        if _SECTION.match(line):
            continue
        matched = False
        for field, pat in _LABEL_PATTERNS:
            m = pat.match(line)
            if not m:
                continue
            value = line[m.end():].strip(" :：")
            if not value:
                matched = True  # 标签在但值空，跳过这条
                break
            if field == "ram":
                gb = parse_ram(value)
                if gb:
                    out["ram_gb"] = gb
            elif field == "disk":
                gb = parse_disk(value)
                if gb:
                    out["disk_gb"] = gb
            elif field == "gpu":
                out["gpu"] = value
                vram = parse_vram(value)
                if vram:
                    out["vram_gb"] = vram
            elif field == "dx":
                dx = parse_dx(value)
                if dx:
                    out["dx"] = dx
            else:
                out[field] = value
            matched = True
            break
        if matched:
            continue
        # 没匹配到标签的行，可能是「需要 64 位处理器和操作系统」这类说明，忽略

    # 有些游戏把显存写在内存行后面，或单独一行 "显存: 4 GB"
    if "vram_gb" not in out:
        m = re.search(r"显存[^\d]{0,6}([\d.]+)\s*(GB|MB)", raw, re.I)
        if m:
            got = _to_gb(float(m.group(1)), m.group(2))
            if got and 0.5 <= got <= 32:
                out["vram_gb"] = got
    return out


def parse_requirements(pc_req: dict[str, str] | None) -> tuple[dict, dict]:
    """解析 pc_requirements，返回 (minimum, recommended)。"""
    if not pc_req:
        return {}, {}
    return parse_block(pc_req.get("minimum") or ""), parse_block(pc_req.get("recommended") or "")


_EN_MONTH = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b", re.I
)
_EN_MONTH_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_release(date_text: str) -> str | None:
    """把发售日期归一成 YYYY-MM。

    中文: 2020 年 12 月 9 日 / 2024 年 8 月 19 日
    英文: 9 Dec, 2020 / Dec 9, 2020 / 2020
    """
    if not date_text:
        return None
    t = date_text.strip()
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", t)
    if m:
        return "%04d-%02d" % (int(m.group(1)), int(m.group(2)))
    m = re.search(r"(\d{4})", t)
    if not m:
        return None
    year = int(m.group(1))
    if not (1970 <= year <= 2100):
        return None
    # 只能靠英文月份名判断。不能用裸数字当月份——"9 Dec, 2020" 里的 9 是日期，
    # 会被误判成 9 月（已踩过这个坑）。
    mm = _EN_MONTH.search(t)
    if mm:
        return "%04d-%02d" % (year, _EN_MONTH_NUM[mm.group(1).lower()])
    return "%04d-00" % year


def build_record(appid: int, data: dict) -> dict | None:
    """把 appdetails 的 data 转成一条可入库的记录。"""
    if not data or data.get("type") != "game":
        return None
    pc = data.get("pc_requirements") or {}
    minimum, recommended = parse_requirements(pc)
    if not minimum and not recommended:
        return None
    return {
        "appid": int(appid),
        "name": (data.get("name") or "").strip(),
        "release": parse_release((data.get("release_date") or {}).get("date", "")),
        "min": minimum,
        "rec": recommended,
        "url": "https://store.steampowered.com/app/%d" % int(appid),
    }
