"""菜单里那枚选中圆点与标签的中线对齐。

行本身是 flex 居中的,几何上勾位与文字盒的中线重合——这一处不是几何问题:文字行盒的
上半留给上伸部,小写为主的标签墨迹落在偏下的一段,几何居中的圆点读起来就偏高。
2026-08-17 用户在 pi 的模型菜单上看到的正是这个(标签全是小写的
deepseek/deepseek-v4-pro,还带 p 的下伸)。

修法是给圆点一个与字号成比例的下压量。用比例而不是固定像素:同一枚勾在 rp-wide
(14.5px)与普通菜单(13.5px)两处都用。
"""
import os
import re
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
PANEL = _srcmap.panel_src()


class CheckDotAlign(unittest.TestCase):
    def test_圆点按字号比例下压(self):
        m = re.search(r"\.rp-check svg \{([^}]*)\}", CSS)
        self.assertTrue(m, "圆点的下压规则不见了,菜单里它会重新偏高")
        self.assertRegex(m.group(1), r"translateY\(0?\.0\d+em\)")

    def test_行仍是flex居中_下压只是补光学差(self):
        # 几何居中那一层不能撤:撤了之后下压量补的就不是光学差,而是两个错位叠加
        m = re.search(r"\n\.rp-row \{([^}]*)\}", CSS).group(1)
        self.assertIn("display: flex", m)
        self.assertIn("align-items: center", m)

    def test_下压的是图形不是整格(self):
        """勾位那一格在 chev 行里装的是文字记号「›」,它按文字自己对齐。
        压整格会把记号一起带下去。"""
        self.assertIn(".rp-check svg {", CSS)
        self.assertNotIn(".rp-ctx .rp-row .rp-check { transform", CSS)

    def test_圆点是唯一装在勾位里的图形(self):
        # 这条规则按「.rp-check 里的 svg 只有这一枚」写;真多出一枚图标时要重看
        self.assertEqual(PANEL.count('class="rp-check"'), 1)
        m = re.search(r"const RP_CHECK = '(<svg[^']+)'", PANEL)
        self.assertTrue(m)
        self.assertIn("<circle", m.group(1))


if __name__ == "__main__":
    unittest.main()
