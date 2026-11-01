"""Monaco IDE 在手机端采用文件树抽屉，不把目录与编辑器并排挤窄。"""
import os
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
HTML = open(os.path.join(ROOT, "frontend", "pages", "files.html"), encoding="utf-8").read()
ENTRY = open(os.path.join(ROOT, "frontend", "app-assets", "files", "src", "entry.js"),
             encoding="utf-8").read()


class MobileFilesApp(unittest.TestCase):
    def test_移动形制只在窄屏触控设备启用(self):
        query = ('(pointer: coarse) and (max-width: 820px), '
                 '(pointer: coarse) and (max-height: 520px)')
        self.assertIn("@media (pointer: coarse) and (max-width: 820px),", HTML)
        self.assertIn("(pointer: coarse) and (max-height: 520px)", HTML)
        self.assertIn(f'matchMedia("{query}")', ENTRY)
        self.assertNotIn('matchMedia("(max-width: 820px),', ENTRY)

    def test_文件树改成抽屉且有遮罩(self):
        self.assertIn("body.side-open #side { transform: none; }", HTML)
        self.assertIn('id="side-mask"', HTML)
        self.assertIn('id="m-side"', HTML)
        self.assertIn("width: min(84vw, 320px) !important", HTML)

    def test_宽屏目录也可收起并让正文扩展(self):
        self.assertIn("body.side-collapsed #side, body.side-collapsed #divider { display: none; }", HTML)
        self.assertIn("st.sideCollapsed = !on", ENTRY)

    def test_手机目录树与搜索框紧凑而按钮保留触控高度(self):
        self.assertIn("gap: 5px; min-height: 27px; padding: 0 7px", HTML)
        self.assertNotIn("\n    .row { min-height: 44px", HTML)
        self.assertIn("#search-field { height: 36px; }", HTML)
        self.assertIn("height: calc(100% * 16 / 13); font-size: 16px", HTML)
        self.assertIn("transform: scale(.8125); transform-origin: left top", HTML)
        self.assertIn('role="searchbox" aria-label="搜索文件与内容"', HTML)
        self.assertIn('id="search-icon"', HTML)
        self.assertIn("#results .row { min-height: 44px", HTML)
        self.assertIn(".tab .tb-x { visibility: visible; width: 28px", HTML)

    def test_手机编辑字号阻止系统自动放大(self):
        self.assertIn(".monaco-editor textarea.inputarea { font-size: 16px !important; }", HTML)
        self.assertIn("fontSize: 14.5, lineHeight: 23", ENTRY)
        self.assertIn("...ideCodeMetrics()", ENTRY)

    def test_手机使用原生输入层以保留选择粘贴菜单(self):
        self.assertIn('id="mobile-input"', HTML)
        self.assertIn("-webkit-touch-callout: default", HTML)
        self.assertIn("font: 16px/25.38px", HTML)
        self.assertIn("transform: scale(.90625)", HTML)
        self.assertIn('input.addEventListener("input"', ENTRY)
        self.assertIn("syncMobileModel", ENTRY)

    def test_手机行号栏收窄(self):
        self.assertIn("lineNumbersMinChars: 2", ENTRY)
        self.assertIn("glyphMargin: false", ENTRY)
        self.assertIn("lineDecorationsWidth: 6", ENTRY)
        self.assertIn("grid-template-columns: 34px", HTML)
        self.assertIn('id="mobile-line-track"', HTML)

    def test_编辑动作沿用差异按钮样式(self):
        self.assertIn("#bar #btn-diff, #bar #btn-save, #bar #btn-cancel", HTML)
        self.assertNotIn("#btn-save { background: var(--gh-success-emphasis)", HTML)

    def test_打开文件自动收目录(self):
        self.assertIn("if (mobileIDE()) setSideVisible(false);", ENTRY)
        self.assertIn('$("#m-side").onclick', ENTRY)
        self.assertIn('$("#side-mask").onclick', ENTRY)

    def test_手机关闭minimap并使用单列差异(self):
        self.assertIn("enabled: !mobileIDE()", ENTRY)
        self.assertIn('wordWrap: mobileIDE() ? "on" : "off"', ENTRY)
        self.assertIn("renderSideBySide: !mobileIDE()", ENTRY)


if __name__ == "__main__":
    unittest.main()
