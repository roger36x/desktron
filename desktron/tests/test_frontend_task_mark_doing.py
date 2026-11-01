"""任务勾选框「进行中」态:边框跟「未开始」同色,内部只留一枚缩小的方块。

原来 .st-doing 把边框和填充一起涂成蓝色,整块实心——「进行中」的框跟「未开始」的框
长得完全不一样,读起来像两种控件,而它们本该是同一个框,只是里面有没有东西的区别
(2026-08-25 用户实见)。改成边框统一取 --text-weaker(未开始的默认色),内部用 ::after
摆一枚比外框内径小一圈的方块。
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


def rule(sel):
    hits = re.findall(r"\n%s \{[^}]*\}" % re.escape(sel), CSS)
    assert hits, "找不到 %s" % sel
    return hits[-1]


class 进行中的边框(unittest.TestCase):
    def test_边框跟未开始同色(self):
        base = rule(".td-mark")
        doing = rule(".td-row.st-doing .td-mark")
        self.assertIn("border: 1.5px solid var(--text-weaker)", base)
        self.assertIn("border-color: var(--text-weaker)", doing)
        self.assertNotIn("#8fb8e6", doing, "边框不该再单独涂蓝")

    def test_没有整块背景色(self):
        doing = rule(".td-row.st-doing .td-mark")
        self.assertNotIn("background:", doing)


class 内部的小方块(unittest.TestCase):
    def test_方块比外框内径小(self):
        """外框边宽 1.5px,内部可用区域是 11-1.5*2=8px;方块用 inset 缩进 2.5px 落在
        中间,比这个内径还要小一圈,不是填满。"""
        after = rule(".td-row.st-doing .td-mark::after")
        self.assertIn("inset: 2.5px", after)
        self.assertIn("background: #8fb8e6", after)

    def test_挂在伪元素不改DOM(self):
        """.td-mark 本身是个空 span(见 app-tasks.js),多加一层视觉不该要求改 JS。"""
        self.assertIn("position: relative", rule(".td-mark"))


if __name__ == "__main__":
    unittest.main()
