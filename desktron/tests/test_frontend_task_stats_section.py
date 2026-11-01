"""任务统计挪进设置页的最后一节。

任务页要回答的是「现在有什么待办」;四张数字卡与十四天柱状图回答的是「最近干得怎么样」。
后者偶尔回看一次,压在待办前面每次都要先翻过去。2026-08-16 挪到设置页末尾。

两处显示的数必须同源:取数(tpTaskRows)与出 HTML(tpStatsHtml)都只有一份,设置页那一节
直接调它们。各算各的话,对不上时没人说得清哪个对。
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


class MovedOut(unittest.TestCase):
    def test_任务页不再画统计块(self):
        seg = fn(PANEL, "openTasksPage")
        for gone in ("wf-stats", "wf-trend", "今日任务", "最近 14 天完成情况", "_tpWireTrend"):
            self.assertNotIn(gone, seg, "任务页里还留着 " + gone)

    def test_任务页仍有待办那两块(self):
        seg = fn(PANEL, "openTasksPage")
        self.assertIn("分工作流", seg)
        self.assertIn("所有待办", seg)


class SharedSource(unittest.TestCase):
    def test_取数与出图各只有一份(self):
        self.assertIn("async function tpTaskRows()", PANEL)
        self.assertIn("function tpStatsHtml(", PANEL)
        # 任务页与设置页都调它们,不各写一份
        self.assertIn("await tpTaskRows()", fn(PANEL, "openTasksPage"))
        seg = fn(SETTINGS, "renderTaskStatsSection")
        self.assertIn("tpTaskRows()", seg)
        self.assertIn("tpStatsHtml(byOpen, _tpAgg(rows))", seg)

    def test_柱状图的悬停也接上(self):
        self.assertIn("_tpWireTrend(box, byOpen)", fn(SETTINGS, "renderTaskStatsSection"))


class InSettings(unittest.TestCase):
    def test_是最后一节(self):
        seg = fn(SETTINGS, "renderSettings")
        i = seg.index('smSection("任务统计"')
        for other in ('smSection("外观"', 'smSection("调试"'):
            self.assertLess(seg.index(other), i, "任务统计要排在最后")

    def test_异步填不拖住整页(self):
        seg = fn(SETTINGS, "renderSettings")
        self.assertIn("renderTaskStatsSection(tsBox);", seg)
        self.assertNotIn("await renderTaskStatsSection", seg)

    def test_取不到时说得出话(self):
        seg = fn(SETTINGS, "renderTaskStatsSection")
        self.assertIn("读取失败", seg)
        self.assertIn("还没有工作流", seg)

    def test_卡里不再套一层卡(self):
        # 设置的 .sm-card 已有底色与圆角,里面的统计块要收一档,否则是双层框
        self.assertIn(".sm-tasks .wf-stat, .sm-tasks .wf-trend", CSS)
        self.assertIn(".sm-tasks .wf-stats { margin-top: 0; }", CSS)

    def test_窄屏日期横轴斜排(self):
        # 手机宽度容不下 14 个水平日期；只在窄屏斜排，并预留高度避免压住图例。
        media = CSS.index("@media (max-width: 600px)", CSS.index(".tp-xaxis"))
        seg = CSS[media:media + 240]
        self.assertIn(".tp-xaxis { height: 28px;", seg)
        self.assertIn("transform: rotate(-45deg)", seg)
        self.assertIn("transform-origin: 50% 0", seg)


if __name__ == "__main__":
    unittest.main()
