"""应用内部的标签条可以拖动改位。

浏览器 / 终端 / 文件三个应用的标签条共用 .ps-item 那一套形制,拖动这件事因此只写一份
(app-pages.js 的 psTabsBindDrag)。不用 HTML5 的 draggable:它在 WKWebView 里给不出稳定的
落点反馈,而这里要的只是「按住往边上挪」这一种手势。

三处细节各有缘由:
  · 挪够 4px 才算拖,否则一次普通点击会被判成零距离的拖,标签切不过去。
  · 松手之后还会来一发 click,那一发要吃掉——指针停在哪一枚上是拖的结果,不是人点的。
    标志位不能用 moved:它在 pointerup 里已经清了,click 时读到的是假。
  · 落点只在 .ps-item 之间找,末尾那枚「+」不参与。

模型归各应用自己:这里只把 DOM 的次序当作 id 序交回去,由它们改自己的数组、重画、落盘。
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
F = lambda n: open(os.path.join(ROOT, "frontend", n), encoding="utf-8").read()
PAGES, BROWSER, TERM, FILES = F("app-pages.js"), F("app-browser.js"), F("app-terminal.js"), F("app-files.js")
CSS = F("style.css")
DRAG = re.search(r"function psTabsBindDrag\(strip, onOrder\)[\s\S]+?\n}", PAGES).group(0)


class SharedDragHelper(unittest.TestCase):
    def test_只绑一次(self):
        self.assertIn("if (!strip || strip._psDrag) return;", DRAG)

    def test_挪够四像素才算拖(self):
        self.assertIn("if (Math.abs(ev.clientX - x0) < 4) return;", DRAG)

    def test_关闭钮不参与拖动(self):
        self.assertIn('ev.target.closest(".ps-x")', DRAG)

    def test_越过邻座中线才换位(self):
        """按重叠面积算会在两枚之间来回抖。"""
        self.assertIn("r.left + r.width / 2", DRAG)

    def test_拖完那一下的点击要吃掉_且不能用moved判(self):
        self.assertIn("justDragged = true;", DRAG)
        seg = DRAG[DRAG.index('addEventListener("click"'):]
        self.assertIn("if (!justDragged) return;", seg)
        self.assertNotIn("if (moved)", seg)

    def test_次序按DOM交回_不碰模型(self):
        self.assertIn('onOrder(items().map((el) => el.dataset.id).filter(Boolean))', DRAG)
        self.assertIn('strip.querySelectorAll(".ps-item")', DRAG)   # 「+」不在其中


class AllThreeStripsWired(unittest.TestCase):
    def test_浏览器(self):
        self.assertIn("psTabsBindDrag(strip, (ids) => {", BROWSER)
        self.assertIn("browserTabs.sort((a, b) => ids.indexOf(a.id) - ids.indexOf(b.id));", BROWSER)
        self.assertIn("browserRenderTabs(); persistBrowserTabs();", BROWSER)

    def test_终端(self):
        self.assertIn("termAppTabs.sort((a, b) => ids.indexOf(a.id) - ids.indexOf(b.id));", TERM)
        self.assertIn("termAppRenderTabs(); persistTerminalTabs();", TERM)
        self.assertIn("d.dataset.id = t.id;", TERM)   # 原来没有,拖动按它认标签

    def test_文件(self):
        self.assertIn("filesRoots.sort((a, b) => ids.indexOf(a.id) - ids.indexOf(b.id));", FILES)
        self.assertIn("filesRenderHead(); persistFilesPage();", FILES)

    def test_拖动中的样子(self):
        self.assertIn(".ps-item.ps-drag {", CSS)


if __name__ == "__main__":
    unittest.main()
