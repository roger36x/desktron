"""文件应用的顶栏是项目目录的标签条,像浏览器那样。

原来一次只看一个目录:顶栏是一枚文件夹图标加名字,从别处点另一个目录就地换过去,上一个
没有留下痕迹。要在两个仓之间来回看就得每次回工作流面板重点一遍。

形制不另画:浏览器应用与终端应用的内部标签条早就共用 .ps-item 那一套,这里并进同一批
选择器(.br-tabs / .fa-tabs …),三处看起来是同一个东西。

只留一份 Monaco:切目录是换 iframe 的地址,不为每个目录各留一个实例。那一页启动时会按
cd_files_app_<root> 把上次的文件标签与展开态恢复回来,所以切回去看到的还是原样;代价是
切换要重走一次启动。聊天页那次卡死正是几十份重活页面攒出来的,这里不重蹈。
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FILES = open(os.path.join(ROOT, "frontend", "app-files.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


def fn(name):
    m = re.search(r"\nfunction %s\(.*?\n}" % re.escape(name), FILES, re.S)
    assert m, "%s 不见了" % name
    return m.group(0)


class RootTabs(unittest.TestCase):
    def test_顶栏装的是标签条(self):
        seg = fn("_filesBuild")
        self.assertIn('<span class="fa-tabs"></span>', seg)
        self.assertNotIn("fa-name", seg)   # 单个名字那版已经撤了

    def test_同一个目录不重复开(self):
        seg = fn("openFilesPage")
        self.assertIn("filesRoots.findIndex((x) => x.id === id)", seg)
        self.assertIn("if (at < 0) filesRoots.push(row)", seg)

    def test_切换只换地址不多开实例(self):
        seg = fn("filesActivateRoot")
        self.assertIn("filesShown()", seg)
        # 一份 iframe:files 应用那一路只有 filesShown 在写 src。
        # 原来数的是整个文件,而 2026-09-02 起同一文件里还住着文件浏览器应用
        # (openFileBrowser,自己的 .fbr-frame),它一进来这条就必然超——数错了范围,
        # 不是真多开了实例。改为只数 files 应用自己的那几个函数。
        own = "".join(fn(n) for n in ("filesShown", "filesActivateRoot", "filesCloseRoot"))
        self.assertEqual(own.count('setAttribute("src"'), 1)
        # 两者各用各的 iframe,别再混进同一笔账
        self.assertIn(".fa-frame", FILES)
        self.assertIn(".fbr-frame", FILES)

    def test_关掉当前那个落到邻座(self):
        seg = fn("filesCloseRoot")
        self.assertIn("filesRoots.splice(at, 1)", seg)
        self.assertIn("filesRoots[Math.min(at, filesRoots.length - 1)]", seg)

    def test_关光了整页收掉(self):
        seg = fn("filesCloseRoot")
        i = seg.index("if (!filesRoots.length)")
        self.assertIn("closePage(p)", seg[i:i + 260])

    def test_加号列的是还没开的目录(self):
        seg = fn("filesRootCandidates")
        self.assertIn("filesRoots.map((x) => x.id)", seg)   # 已开的排掉
        self.assertIn("d.repos", seg)                        # 工作流下登记的仓各算一个


class Persistence(unittest.TestCase):
    def test_存整条标签条与当前那个(self):
        seg = fn("persistFilesPage")
        self.assertIn("roots: filesRoots.map(", seg)
        self.assertIn("cur: filesCurrent ? filesCurrent.id", seg)

    def test_三种旧存法都认(self):
        """{roots,cur} 是新的;{id,name} 是上一版;再往前是 cd_pages 里每个工作流一条。"""
        seg = fn("restoreFilesPage")
        self.assertIn("Array.isArray(d.roots)", seg)
        self.assertIn("} else if (d && d.id) {", seg)
        self.assertIn("_filesLegacy", seg)


class SharedStripLook(unittest.TestCase):
    def test_与浏览器终端共用同一批选择器(self):
        for sel in (".br-tabs, .fa-tabs {", ".br-tab, .fa-tab { max-width: 220px; }",
                    ".br-new, .fa-new {"):
            self.assertIn(sel, CSS)

    def test_标签用共用的_ps_item(self):
        seg = fn("filesRenderHead")
        self.assertIn('"ps-item fa-tab"', seg)
        self.assertIn("ps-x", seg)


if __name__ == "__main__":
    unittest.main()
