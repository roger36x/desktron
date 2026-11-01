"""浮层菜单那一圈描边收到 0.8px,并统一到一个 token。

菜单靠底色与投影立起来,描边只是收个口;1px 在这些小面上偏重(2026-08-27 用户实见
「再细一点」)。

2 倍屏实测(dsf=2、截图再放大 2 倍,故 1 CSS px = 4 图像 px):
  1px   → 边框占 4 图像 px,峰值灰度 64
  0.8px → 边框占 2 图像 px,峰值灰度 64
  0.5px → 边框占 2 图像 px,峰值灰度 64
即 0.8px 不是被抗锯齿摊淡,而是硬落成 1 个物理像素——颜色不变、厚度减半。
0.5px 在这一档与 0.8px 渲染结果完全一样,但 1 倍屏上它可能被抹掉,而 0.8px 会退回
整数 1px,所以取 0.8。

只给菜单与同类浮层用。对话框那几张大面(.sm-panel / .auth-tui-panel)仍是 1px:面越大,
描边越该撑得住边界。

刻度列的悬停预览卡 .hs-peek 起初没跟着改(它 340px 宽,介于菜单与对话框之间),2026-08-27
用户问起后一并收细。2026-08-31 卡片表面统一后，它的边框也归到弱边界：浮层已经靠不透明
中性底与投影立起，不需要再单独维护一档颜色。
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()

# 该用细描边的浮层面。前五个是弹出菜单;.hs-peek 是刻度列的悬停预览卡——它和菜单是
# 同一类东西(浮出来的临时层,靠毛玻璃底与投影立起来),2026-08-27 用户问起后一并收细。
MENUS = [".rp-ctx", ".resume-pop", ".add-box", ".float-menu", ".hs-page .hs-peek"]
# 不该跟着变细的几张大面(对话框/面板)
PANELS = [".sm-panel", ".auth-tui-panel"]


def rule_of(sel):
    """取该选择器的那一条规则体(只认顶层、以该选择器起头的那条)。"""
    m = re.search(r"\n" + re.escape(sel) + r"\s*\{([^}]*)\}", CSS)
    return m.group(1) if m else None


class 细描边收在一个token里(unittest.TestCase):
    def test_token有定义(self):
        m = re.search(r"--menu-hairline:\s*([^;]+);", CSS)
        self.assertTrue(m, "没找到 --menu-hairline")
        self.assertEqual(m.group(1).strip(), "0.8px")

    def test_只定义一次(self):
        """宽度与主题无关,浅色那套不必再给一份——给了就是两处要一起改。"""
        self.assertEqual(len(re.findall(r"--menu-hairline:", CSS)), 1)

    def test_五处菜单都走token(self):
        for sel in MENUS:
            body = rule_of(sel)
            self.assertIsNotNone(body, f"{sel} 的规则不见了")
            self.assertIn("var(--menu-hairline) solid", body, f"{sel} 没用细描边")

    def test_没有漏网的写死值(self):
        """回归防护:再有人在菜单面上写回 1px,这里会红。"""
        for sel in MENUS:
            self.assertNotIn("border: 1px solid", rule_of(sel), f"{sel} 又写回 1px 了")

    def test_权限卡使用单物理像素描边(self):
        body = rule_of(".hs-page .hs-card")
        self.assertIsNotNone(body)
        self.assertIn("border: var(--menu-hairline) solid var(--border-weak-base)", body)


class 预览卡使用统一弱边界(unittest.TestCase):
    def test_hs_peek使用弱边界(self):
        body = rule_of(".hs-page .hs-peek")
        self.assertIsNotNone(body)
        self.assertIn("var(--menu-hairline) solid var(--border-weak-base)", body)


class 大面不跟着变细(unittest.TestCase):
    def test_对话框仍是1px(self):
        """面越大,描边越该撑得住边界;跟着变细会让整张面浮不住。"""
        for sel in PANELS:
            body = rule_of(sel)
            self.assertIsNotNone(body, f"{sel} 的规则不见了")
            self.assertIn("border: 1px solid", body, f"{sel} 不该用菜单那档细描边")
            self.assertNotIn("--menu-hairline", body)


if __name__ == "__main__":
    unittest.main()
