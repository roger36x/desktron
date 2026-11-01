"""发行版内嵌页的手机布局与宿主主题契约。"""
import os
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PAGES = os.path.join(ROOT, "frontend", "pages")


def read(name):
    return open(os.path.join(PAGES, name), encoding="utf-8").read()


class MobilePageContract(unittest.TestCase):
    def test_所有内嵌页声明真实手机视口(self):
        for name in os.listdir(PAGES):
            if name.endswith(".html"):
                self.assertIn('name="viewport"', read(name), name)

    def test_轻量观测页跟随宿主主题(self):
        for name in ("_desktop.html",):
            h = read(name)
            self.assertIn('src="/page-theme.js"', h, name)
            self.assertIn('href="/page-theme.css"', h, name)

    def test_备忘录手机一次只显示一层(self):
        h = read("notepad.html")
        self.assertIn('.app.stack-layout[data-m="list"] .editor-pane', h)
        self.assertIn('.app.stack-layout[data-m="note"] .list-pane', h)
        self.assertIn("mGo('note')", h)
        self.assertIn(".app.stack-layout .search input, .app.stack-layout .editor { font-size:16px; }", h)
        self.assertIn("notepadNarrow.addEventListener('change', syncNotepadLayout)", h)
        self.assertIn("|| notepadNarrow.matches", h)

    def test_可编辑内嵌应用不触发手机自动缩放(self):
        self.assertIn(".edit, .palette input { font-size: 16px !important; }", read("taskpaper.html"))
        self.assertIn("textarea:not(.excalidraw-wysiwyg), select", read("excalidraw.html"))
        theme = open(os.path.join(ROOT, "frontend", "page-theme.css"), encoding="utf-8").read()
        self.assertIn('[contenteditable]:not([contenteditable="false"])', theme)
        self.assertIn("font-size: 16px !important", theme)

    def test_宿主输入控件统一避免手机聚焦缩放(self):
        style = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
        self.assertIn('(pointer: coarse) and (max-width: 820px)', style)
        self.assertIn('input:not([type="checkbox"]):not([type="radio"]):not([type="range"])', style)
        self.assertIn('[contenteditable]:not([contenteditable="false"])', style)
        self.assertNotIn("maximum-scale=1", open(os.path.join(ROOT, "frontend", "index.html"), encoding="utf-8").read())

    def test_画板使用原生可收起侧栏(self):
        src = open(os.path.join(ROOT, "frontend", "app-assets", "excalidraw", "src", "entry.jsx"),
                   encoding="utf-8").read()
        self.assertIn("<Sidebar.Trigger", src)
        self.assertIn("<Sidebar name={SIDEBAR}", src)

    def test_画板手机工具栏可横滑且列表操作不靠hover(self):
        h = read("excalidraw.html")
        self.assertIn(".excalidraw--mobile .App-top-bar > section", h)
        self.assertIn("overflow-x: auto !important", h)
        self.assertIn(".cv-ops { display: flex; }", h)
        self.assertIn(".cv-ops button { width: 32px; height: 32px; }", h)


if __name__ == "__main__":
    unittest.main()
