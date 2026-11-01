"""卡片表面按角色统一，浅深主题都保持可辨识的层级。"""
import os
import re
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


def rule(sel):
    m = re.search(r"\n" + re.escape(sel) + r"\s*\{([^}]*)\}", CSS, re.S)
    assert m, sel
    return m.group(1)


def theme_block(sel):
    m = re.search(re.escape(sel) + r"\s*\{(.*?)\n\}", CSS, re.S)
    assert m, sel
    return m.group(1)


def value(block, token):
    m = re.search(re.escape(token) + r":\s*([^;]+);", block)
    assert m, token
    return m.group(1).strip()


class SurfaceRoles(unittest.TestCase):
    def test_深色卡片是中性灰且层级有序(self):
        root = theme_block(":root")
        vals = [value(root, x) for x in
                ("--surface-inset", "--surface-card-subtle", "--surface-card")]
        nums = []
        for raw in vals:
            self.assertRegex(raw, r"^#[0-9a-fA-F]{6}$")
            rgb = tuple(int(raw[i:i + 2], 16) for i in (1, 3, 5))
            self.assertLessEqual(max(rgb) - min(rgb), 1, raw)
            nums.append(sum(rgb) / 3)
        self.assertEqual(nums, sorted(nums))

    def test_浅色卡片不靠重边框区分(self):
        light = theme_block('html[data-theme="light"]')
        self.assertEqual(value(light, "--surface-float"), "#ffffff")
        self.assertEqual(value(light, "--surface-card"), "#f7f7f8")
        self.assertEqual(value(light, "--surface-inset"), "#f3f3f4")

    def test_常驻卡片不再借文字灰阶做背景(self):
        for sel in (".sm-card", ".wf-stat", ".wf-trend",
                    ".hs-page .hs-card", ".ss-card"):
            self.assertIn("var(--surface-card", rule(sel), sel)


class FloatingCards(unittest.TestCase):
    def test_刻度预览不再透出后方正文(self):
        body = rule(".hs-page .hs-peek")
        self.assertIn("background: var(--surface-float)", body)
        self.assertNotIn("raise-rgb", body)
        self.assertNotIn("backdrop-filter", body)

    def test_任务浮层只保留一圈主边界(self):
        sheet = rule(".wf-sheet")
        self.assertIn("background: var(--surface-float)", sheet)
        self.assertIn("border: 1px solid var(--border-weak-base)", sheet)
        doc = rule(".wf-sheet .doc-in")
        self.assertIn("background: transparent", doc)
        self.assertIn("border: none", doc)
        tree = rule(".wf-sheet .wf-note-tree")
        self.assertIn("background: transparent", tree)
        self.assertIn("border: none", tree)


if __name__ == "__main__":
    unittest.main()
