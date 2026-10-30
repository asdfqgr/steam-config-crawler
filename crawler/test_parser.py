"""parser 单元测试。用真实 Steam 返回格式做断言。

运行: python -m unittest discover -s crawler -p "test_*.py" -v
"""

import unittest

from parser import (
    build_record,
    parse_block,
    parse_disk,
    parse_dx,
    parse_ram,
    parse_release,
    parse_requirements,
    parse_vram,
)

# 真实样本：CS2（<p> 排版，中文）
CS2_MIN = """最低配置:<br><strong>操作系统:</strong> Windows&reg; 10<br>
<strong>处理器:</strong> 4 硬件线程 CPU——英特尔&reg; 酷睿&trade; i5 750 或以上<br>
<strong>内存:</strong> 8 GB RAM<br>
<strong>显卡:</strong> 显卡须为 1 GB 或以上，且应兼容 DirectX 11 并支持 Shader Model 5.0<br>
<strong>DirectX 版本:</strong> 11<br><strong>存储空间:</strong> 需要 85 GB 可用空间<br>"""

# 真实样本：星露谷物语（<ul><li> 排版，中文）—— 曾因只解析 <p> 被误判为空
STARDEW_MIN = """<p class="wp-block-paragraph"><strong>最低配置:</strong></p>
<ul class="wp-block-list"><li><ul class="wp-block-list">
<li><strong>操作系统 *:</strong>Windows Vista 或更高版本</li>
<li><strong>处理器:</strong>2 赫兹</li>
<li><strong>内存:</strong>2 GB 内存</li>
<li><strong>显卡:</strong>256 MB 显存，着色器模型 3.0+</li>
<li><strong>DirectX 版本:</strong>10</li>
<li><strong>存储空间:</strong> 需要 500 MB 可用空间</li>
</ul></li></ul>"""

# 英文排版
EN_MIN = """<strong>Minimum:</strong><br><ul class="bb_ul">
<li><strong>OS:</strong> Windows 7/8.1/10 (64-bit versions)<br></li>
<li><strong>Processor:</strong> Intel Core i5-750<br></li>
<li><strong>Memory:</strong> 8 GB RAM<br></li>
<li><strong>Graphics:</strong> NVIDIA GTX 780 3GB / AMD R9 280X 3GB<br></li>
<li><strong>Storage:</strong> 60 GB available space<br></li></ul>"""


class TestLines(unittest.TestCase):
    def test_p_tag_format(self):
        got = parse_block(CS2_MIN)
        self.assertEqual(got["os"], "Windows 10")
        self.assertEqual(got["ram_gb"], 8)
        self.assertEqual(got["dx"], "11")
        self.assertEqual(got["disk_gb"], 85)
        self.assertIn("i5 750", got["cpu"])

    def test_ul_li_format(self):
        got = parse_block(STARDEW_MIN)
        self.assertIn("Windows Vista", got["os"])
        self.assertEqual(got["ram_gb"], 2)
        self.assertEqual(got["dx"], "10")
        self.assertAlmostEqual(got["disk_gb"], 0.49, places=1)

    def test_english_format(self):
        got = parse_block(EN_MIN)
        self.assertIn("Windows 7", got["os"])
        self.assertEqual(got["ram_gb"], 8)
        self.assertEqual(got["disk_gb"], 60)
        self.assertIn("GTX 780", got["gpu"])

    def test_empty(self):
        self.assertEqual(parse_block(""), {})
        self.assertEqual(parse_block("<p>最低配置:</p>"), {})


class TestNumbers(unittest.TestCase):
    def test_ram_units(self):
        self.assertEqual(parse_ram("1024 MB RAM"), 1.0)
        self.assertEqual(parse_ram("16 GB RAM"), 16.0)
        self.assertEqual(parse_ram("2 GB 内存"), 2.0)
        self.assertIsNone(parse_ram("无"))

    def test_disk_prefers_storage_keyword(self):
        # 内存 6 GB 和 存储 50 GB 混在一行时不能取错
        self.assertEqual(parse_disk("内存 6 GB 存储 50 GB 可用空间"), 50.0)

    def test_vram_never_from_model_number(self):
        # GTX 1060 的 1060 绝不能被当成 1060 GB 显存
        self.assertIsNone(parse_vram("NVIDIA GeForce GTX 1060"))
        self.assertEqual(parse_vram("NVIDIA GTX 780 3GB"), 3.0)
        self.assertEqual(parse_vram("显卡须为 1 GB 或以上"), 1.0)

    def test_vram_range_guard(self):
        self.assertIsNone(parse_vram("需要 128 GB 空间"))  # 超出 32 上限

    def test_dx(self):
        self.assertEqual(parse_dx("DirectX 版本: 12"), "12")
        self.assertEqual(parse_dx("DirectX 9.0c"), "9.0c")


class TestRelease(unittest.TestCase):
    def test_chinese_date(self):
        self.assertEqual(parse_release("2020 年 12 月 9 日"), "2020-12")
        self.assertEqual(parse_release("2024 年 8 月 19 日"), "2024-08")

    def test_english_date(self):
        self.assertEqual(parse_release("9 Dec, 2020"), "2020-12")
        self.assertEqual(parse_release("Dec 9, 2020"), "2020-12")

    def test_year_only(self):
        self.assertEqual(parse_release("2015"), "2015-00")

    def test_garbage(self):
        self.assertIsNone(parse_release("即将推出"))
        self.assertIsNone(parse_release(""))


class TestBuildRecord(unittest.TestCase):
    def test_skips_non_game(self):
        self.assertIsNone(build_record(1, {"type": "dlc", "name": "x"}))
        self.assertIsNone(build_record(1, {"type": "game", "name": "x", "pc_requirements": {}}))

    def test_full_record(self):
        data = {
            "type": "game",
            "name": "Counter-Strike 2",
            "release_date": {"date": "2017 年 9 月 15 日"},
            "pc_requirements": {"minimum": CS2_MIN},
        }
        rec = build_record(730, data)
        self.assertEqual(rec["appid"], 730)
        self.assertEqual(rec["release"], "2017-09")
        self.assertEqual(rec["min"]["ram_gb"], 8)
        self.assertEqual(rec["rec"], {})
        self.assertTrue(rec["url"].endswith("/730"))

    def test_both_blocks(self):
        mn, rc = parse_requirements({"minimum": CS2_MIN, "recommended": EN_MIN})
        self.assertEqual(mn["ram_gb"], 8)
        self.assertEqual(rc["disk_gb"], 60)


if __name__ == "__main__":
    unittest.main(verbosity=2)
