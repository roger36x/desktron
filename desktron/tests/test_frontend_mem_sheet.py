"""记忆卡也改成弹浮层,与任务详情同一套壳。

就地展开会把下面的卡片顶下去半屏,而一条记忆常常是整篇文档(2026-08-16 按用户要求改)。

做法是借节点,不重画:`.doc` 上挂着编辑(铅笔)、溯源跳转、保存后重载几套行为,重画一遍
必然漏其中之一。浮层把这个节点借走,关掉时还回卡片——所以那几套行为一行都不用动。
"""
import os
import re
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PANEL = _srcmap.panel_src()
SETTINGS = _srcmap.settings_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


def fn(src, name):
    m = re.search(r"\n(?:async )?function %s\(.*?\n}" % re.escape(name), src, re.S)
    assert m, "%s 不见了" % name
    return m.group(0)


class SharedShell(unittest.TestCase):
    def test_浮层壳是共用的(self):
        # 两处各写一个壳,改一处必漏另一处(定位、关闭、层级都要一样)
        self.assertIn("function openFloatSheet(opts)", PANEL)
        self.assertIn("openFloatSheet({", fn(PANEL, "wfOpenTaskSheet"))
        self.assertIn("openFloatSheet({", fn(SETTINGS, "_toggleMemCard"))

    def test_标题旁操作与靠右操作分槽(self):
        seg = fn(PANEL, "openFloatSheet")
        self.assertIn("opts.titleAct", seg)
        self.assertIn('class="wf-sheet-title-group"', seg)
        self.assertLess(seg.index("opts.titleAct"), seg.index("opts.act"))
        self.assertIn(".wf-sheet-title-group { flex: 1 1 auto", CSS)

    def test_壳负责定位与两条关闭路径(self):
        seg = fn(PANEL, "openFloatSheet")
        self.assertIn("_wfSheetFit(back)", seg)
        self.assertIn("!sheet.contains(ev.target)", seg)
        self.assertIn('ev.key === "Escape"', seg)

    def test_长内容与任务详情共用同一高度上限(self):
        self.assertIn("--wf-sheet-max-h: min(66vh, 980px)", CSS)
        self.assertEqual(CSS.count("--wf-sheet-max-h:"), 1)


class BorrowsTheNode(unittest.TestCase):
    def test_工作流项目记忆使用单卡而非旧散条列表(self):
        self.assertIn('flat === "card"', SETTINGS)
        self.assertIn('host.className = "pv-mem wf-memcard"', SETTINGS)
        self.assertIn(".wf-memcard .pvmem-card {", CSS)
        self.assertIn("-webkit-line-clamp: 2", CSS)

    def test_借节点而不是重画(self):
        seg = fn(SETTINGS, "_toggleMemCard")
        self.assertIn('card.querySelector(".doc")', seg)
        self.assertIn("body.appendChild(doc)", seg)
        self.assertNotIn("md.render", seg)      # 正文渲染仍在加载那一步,不在这里

    def test_关掉要还回去(self):
        seg = fn(SETTINGS, "_toggleMemCard")
        self.assertIn("onClose", seg)
        self.assertIn("home.appendChild(doc)", seg)
        self.assertIn('home.classList.remove("open")', seg)   # 箭头复位
        # 壳在关闭时回调
        self.assertIn("if (el._onClose)", fn(PANEL, "wfCloseTaskSheet"))

    def test_加载与展开分开(self):
        # 编辑保存后要重取正文,那一步只需要「重新加载」,不该顺带再开一张浮层
        self.assertIn("async function _memCardLoad(card)", SETTINGS)
        self.assertIn("await _memCardLoad(card)", fn(SETTINGS, "_toggleMemCard"))

    def test_浮层里撤掉折叠高度并使用独立正文容器(self):
        # 卡片里那套是给「藏在行下面」用的:浮层不继承 640px 折叠高度与实线分隔；
        # 浮层壳已经提供完整边界，正文直接落在壳内，不再套第二圈虚线卡。
        self.assertIn(".wf-sheet .doc { max-height: none; overflow: visible; }", CSS)
        start = CSS.index(".wf-sheet .doc-in {")
        block = CSS[start:CSS.index("}", start)]
        self.assertIn("padding: 12px 0", block)
        self.assertIn("background: transparent", block)
        self.assertIn("border: none", block)
        self.assertNotIn("padding: 6px 14px 14px", block)
        self.assertNotIn("border-top: 1px solid", block)


if __name__ == "__main__":
    unittest.main()
