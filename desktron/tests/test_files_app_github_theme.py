"""Monaco 文件应用使用 GitHub Default，并跟随 Desk 明暗模式。

vendor/ 是由 src/entry.js 打出的本机产物，不进 git；契约钉在可重建的源码与 files.html。
"""
import os
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENTRY = open(os.path.join(ROOT, "frontend", "app-assets", "files", "src", "entry.js"),
             encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "frontend", "pages", "files.html"), encoding="utf-8").read()


class GithubDefaultTheme(unittest.TestCase):
    def test_明暗两套主题都注册(self):
        self.assertIn('dark: "github-dark-default"', ENTRY)
        self.assertIn('light: "github-light-default"', ENTRY)
        self.assertIn("monaco.editor.defineTheme(GITHUB_THEME_NAME[mode]", ENTRY)
        self.assertIn('base: "vs-dark"', ENTRY)
        self.assertIn('base: "vs"', ENTRY)

    def test_关键色与过程代码展开共用中性画布和_GitHub_语义(self):
        # 代码正文对齐过程展开区；编辑器 chrome 与交互焦点仍保持 Desk 自身层级。
        for value in ("#232323", "#d1d1d1", "8F8F8F", "FF7B72", "#97a3f7",
                      "#f9f9f9", "#393939", "777777", "CF222E", "#4f46b8",
                      "#23863626", "#ffcecb4d"):
            self.assertIn(value, ENTRY)

    def test_普通与差异视图共用过程代码排版(self):
        self.assertIn('fontFamily: \'"Desk Mono", "Desk Mono CJK", Menlo, monospace\'', ENTRY)
        self.assertIn("fontSize: 11", ENTRY)
        self.assertIn("lineHeight: 18", ENTRY)
        self.assertIn("const ideCodeMetrics = () => mobileIDE()", ENTRY)
        self.assertGreaterEqual(ENTRY.count("...ideCodeMetrics()"), 4)
        self.assertIn('font-family: "Desk Mono"', HTML)
        self.assertIn('font-family: "Desk Mono CJK"', HTML)

    def test_普通与差异编辑器不再写死_vs_dark(self):
        self.assertEqual(ENTRY.count("theme: ideThemeName()"), 2)
        self.assertNotIn('theme: "vs-dark"', ENTRY)

    def test_输入法合成层使用不透底的当前行颜色(self):
        # Monaco 会把当前行底色直接给可见 textarea；透明色会与下层文本叠成双影。
        self.assertIn('"editor.lineHighlightBackground": "#2b2b2b"', ENTRY)
        self.assertIn('"editor.lineHighlightBackground": "#f1f1f1"', ENTRY)
        self.assertNotIn('"editor.lineHighlightBackground": "#ffffff0a"', ENTRY)
        self.assertNotIn('"editor.lineHighlightBackground": "#00000008"', ENTRY)


class FollowsDeskAppearance(unittest.TestCase):
    def test_切换只换主题不重载模型(self):
        self.assertIn("monaco.editor.setTheme(ideThemeName())", ENTRY)
        self.assertIn("new MutationObserver", ENTRY)
        self.assertIn('attributeFilter: ["data-theme"]', ENTRY)
        self.assertIn("parent.document.documentElement", ENTRY)

    def test_外壳也有同源的明暗变量(self):
        self.assertIn(":root {", HTML)
        self.assertIn(':root[data-theme="light"]', HTML)
        self.assertIn("--gh-canvas: #191919", HTML)
        self.assertIn("--gh-canvas: #ffffff", HTML)
        self.assertIn("background: var(--gh-canvas)", HTML)

    def test_目录树文字在深浅主题均比次级文字更清晰(self):
        self.assertIn("--gh-tree-fg: #c3c3c3", HTML)
        self.assertIn("--gh-tree-fg: #464646", HTML)
        self.assertIn("color: var(--gh-tree-fg)", HTML)

    def test_外壳灰阶不再带暖红色偏(self):
        for stale in ("#1b1a19", "#201f1e", "#171614", "#e7e3df", "#aaa49e",
                      "#7d7873", "#363330", "#2a2826", "#fcfcfb", "#f6f5f3",
                      "#f1f0ed", "#2f2d2b", "#706c68", "#97918c", "#d9d6d2",
                      "#e8e5e1", "#f4f3f1", "#ebe9e6"):
            self.assertNotIn(stale, HTML)

    def test_交互色统一为品牌紫且标签不再使用蓝色顶边(self):
        for value in ("--gh-accent: #97a3f7", "--gh-accent: #4f46b8"):
            self.assertIn(value, HTML)
        self.assertNotIn("inset 0 2px 0 var(--gh-accent)", HTML)

    def test_搜索焦点与拖拽线使用中性一像素描边(self):
        # 搜索图标与输入共用一层边框，焦点落在输入时仅改变外层描边。
        self.assertIn("#search-field:focus-within { border-color: var(--gh-fg-muted); }", HTML)
        field = HTML.split("#search-field {", 1)[1].split("}", 1)[0]
        self.assertIn("border: 1px solid var(--gh-border-muted)", field)
        input_style = HTML.split("#q {", 1)[1].split("}", 1)[0]
        self.assertIn("border: 0", input_style)
        self.assertIn("box-shadow: none", input_style)
        self.assertIn("left: -1px; width: 1px", HTML)
        self.assertNotIn("left: 50%; width: 1px", HTML)
        self.assertIn("#divider:hover::after, #divider.dragging::after", HTML)
        self.assertIn('div.classList.add("dragging")', ENTRY)
        self.assertIn('div.classList.remove("dragging")', ENTRY)

    def test_差异入口是弱化工具操作而非强调按钮(self):
        self.assertIn("#bar #btn-diff, #bar #btn-save, #bar #btn-cancel {", HTML)
        self.assertIn("height: 24px; padding: 0 8px; border: 0", HTML)
        self.assertIn("color: var(--gh-fg-muted); font-size: 11.5px", HTML)
        self.assertIn("#bar #btn-diff:hover, #bar #btn-save:hover, #bar #btn-cancel:hover", HTML)
        self.assertIn("background: var(--gh-button-hover)", HTML)
        self.assertIn("#bar #btn-diff:focus-visible, #bar #btn-save:focus-visible", HTML)

    def test_首帧先读取宿主避免闪色(self):
        self.assertIn("parent.document.documentElement.dataset.theme", HTML)
        self.assertLess(HTML.index("parent.document.documentElement.dataset.theme"),
                        HTML.index("app.bundle.css"))


class SublimeLikeMinimap(unittest.TestCase):
    def test_绘制微缩字符而不是粗色块(self):
        self.assertIn('renderCharacters: true', ENTRY)
        self.assertNotIn('renderCharacters: false', ENTRY)
        self.assertIn('size: "proportional"', ENTRY)
        self.assertIn('scale: 1', ENTRY)
        self.assertIn('maxColumn: 100', ENTRY)


if __name__ == "__main__":
    unittest.main()
