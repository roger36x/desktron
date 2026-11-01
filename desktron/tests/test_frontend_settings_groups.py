"""设置页 10 个小节按性质分成四组,不再是写入顺序的平铺列表。

原来偏好、凭证、基础设施、统计四类混在一起,新功能一律接在末尾(2026-08-24 审阅发现)。
分组只改容器:smGroup() 建一个带标签的包装 div,各小节原有的渲染逻辑一行没动,只是把
appendChild 的目标从 body 换成对应的组。四个组容器提前建好、立刻按目标顺序挂上 body,
下面各小节该往哪个组填,只是各自 appendChild 到那个变量上——组此刻是空的不影响它的位置。
"""
import os
import re
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
JS = _srcmap.settings_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()

RENDER = re.search(r"async function renderSettings\(\)[\s\S]+?\n}\n", JS).group(0)


class 四组的构成(unittest.TestCase):
    def test_四个组按目标顺序挂上body(self):
        self.assertIn('body.append(gPref, gEngine, gTools, gStats);', RENDER)

    def test_偏好组收外观与调试(self):
        self.assertIn("gPref.appendChild(ap);", RENDER)
        self.assertIn("gPref.appendChild(dbg);", RENDER)

    def test_引擎组收三家(self):
        for var in ("cl", "cx", "pi"):
            self.assertIn("gEngine.appendChild(%s);" % var, RENDER)

    def test_工具组收浏览器与工作工具(self):
        for var in ("brw", "it"):
            self.assertIn("gTools.appendChild(%s);" % var, RENDER)
        self.assertNotIn("gTools.appendChild(dbx);", RENDER)

    def test_统计组收任务统计(self):
        self.assertIn("gStats.appendChild(ts);", RENDER)

    def test_不再有直接挂body的小节(self):
        """漏一个换回 body.appendChild 的话,那一节就会飘到组外面,不跟着任何标签走。"""
        names = ("ap", "cl", "cx", "tr", "brw", "it", "rc", "dbg", "ts")
        for n in names:
            self.assertNotIn("body.appendChild(%s);" % n, RENDER)


class smGroup助手(unittest.TestCase):
    def test_存在且不重载appendChild(self):
        """smSection 的 appendChild 要转给卡片内部,smGroup 不需要——组里装的是整段
        smSection 节点,调用方直接 append 到组变量本身就够。"""
        m = re.search(r"function smGroup\(title\)[\s\S]+?\n}", JS)
        self.assertTrue(m, "smGroup 不见了")
        self.assertNotIn("appendChild = ", m.group(0))

    def test_标题落进group_title(self):
        m = re.search(r"function smGroup\(title\)[\s\S]+?\n}", JS).group(0)
        self.assertIn('h.className = "sm-group-title"', m)


class 侧栏导航按分组走(unittest.TestCase):
    def test_目录也分组_不只是正文(self):
        """目录使用与设置视图一致的任务分组模型，不从正文节点反推结构。"""
        nav = re.search(r"function _renderSettingsNav\(\)[\s\S]+?\n}", JS).group(0)
        self.assertIn("for (const group of _SM_NAV_MODEL)", nav)
        self.assertIn("for (const item of group.items)", nav)
        self.assertNotIn('querySelectorAll("#sm-host .sm-group")', nav)

    def test_分组只留在模型里_不渲染成目录标签(self):
        """分组用于组织和搜索；目录只显示可点击的设置项，不再插入不可操作标题。"""
        nav = re.search(r"function _renderSettingsNav\(\)[\s\S]+?\n}", JS).group(0)
        self.assertIn("for (const group of _SM_NAV_MODEL)", nav)
        self.assertNotIn('className = "sm-nav-group"', nav)
        self.assertNotIn("gRow", nav)


class 分组样式(unittest.TestCase):
    def test_组间距比小节间距宽(self):
        sec = re.search(r"\n\.sm-section \{ margin-bottom: (\d+)px", CSS)
        grp = re.search(r"\n\.sm-group \{ margin-top: (\d+)px", CSS)
        self.assertTrue(sec and grp)
        self.assertGreater(int(grp.group(1)), int(sec.group(1)))

    def test_首组不领上边距(self):
        self.assertIn(".sm-group:first-child { margin-top: 0; }", CSS)


if __name__ == "__main__":
    unittest.main()
