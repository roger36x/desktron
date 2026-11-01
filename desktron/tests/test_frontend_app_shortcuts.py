"""⌘ 系快捷键归当前应用,Ctrl 系一律透传给 shell。

2026-09-03 用户报了两件事:终端在侧栏时 ⌘T 开的是浏览器新标签(终端自己也支持 ⌘T),
以及终端「很多快捷键不支持」。跟用户确认的口径是对齐 macOS 原生终端:

    ⌘ 系归应用——⌘T 新建标签、⌘W 关标签、⌘1..9 跳标签、⌘⇧[ ] 循环、⌘K 清屏、
    ⌘± 字号、⌘C 有选区才复制、⌘A 全选;
    Ctrl 系一律不拦,原样透传给 shell。

分派的两个要点:

其一,「当前应用」不等于 pActive。侧栏与主区可以同时开着两页(会话在主区、终端在侧栏),
所以要有 focusedAppPage():先按 document.activeElement 落在哪一页算,焦点不在任何应用页
里再回主区/侧栏。旧的 ⌘W 只认 pActive,终端搬进侧栏后那一下整个落空。

其二,focusedAppPage 分宽严两档。⌘T/⌘W/⌘1..9 是窗口层面的动作,页面在前台就算数;
⌘K/⌘±/⌘C/⌘A 作用于内容,必须要求键盘焦点确实在这一页里。不分这两档的话,终端摆在侧栏
时它一直可见,焦点在设置搜索框里按 ⌘K 也会被终端的清屏抢走——app-settings-core.js 那个
⌘K 是 document 冒泡档的监听,先被这里的捕获监听 stopPropagation 吞掉,根本轮不到它。

⌘C 没选区时不接管:让这一下什么也不发生,与原生终端一致。中断进程是 ⌃C 的活,
不在这里代劳——那会让「Ctrl 系永远属于 shell」这条口径出现一个例外。
"""
import os
import re
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PAGES = open(os.path.join(ROOT, "frontend", "app-pages.js"), encoding="utf-8").read()
TERMINAL = open(os.path.join(ROOT, "frontend", "app-terminal.js"), encoding="utf-8").read()
BROWSER = open(os.path.join(ROOT, "frontend", "app-browser.js"), encoding="utf-8").read()
ASSETS_TERM = open(os.path.join(ROOT, "frontend", "app-assets-term.js"), encoding="utf-8").read()
EMBED = open(os.path.join(ROOT, "frontend", "term-embed.js"), encoding="utf-8").read()

FOCUSED = re.search(r"function focusedAppPage\(strict\)[\s\S]+?\n}", PAGES).group(0)
DISPATCH = PAGES[PAGES.index('window.addEventListener("keydown", (ev) => {\n  if (!ev.metaKey'):]
DISPATCH = DISPATCH[:DISPATCH.index("}, true);") + len("}, true);")]


class 当前应用按焦点判定(unittest.TestCase):
    def test_侧栏那一页也在候选里(self):
        """旧实现只看 pActive,终端搬进侧栏后 ⌘W 落空——这是这次修复的直接起因。

        断言只认「侧栏那几页与主区那页都过一遍 appPageVisible」这件事,不钉具体写法:
        侧栏宿主后来从一个扩到两个(appSidePages),表达式跟着变,而这条约束没有变。"""
        self.assertTrue(re.search(r"appSidePages\(\)|sideAppPage", FOCUSED))
        self.assertIn("appPageVisible(pActive)", FOCUSED)
        self.assertGreaterEqual(FOCUSED.count("appPageVisible"), 2)

    def test_焦点所在的那一页优先(self):
        """两页同时开着时,先看 document.activeElement 真的落在谁里面。"""
        self.assertIn("document.activeElement", FOCUSED)
        self.assertIn("side.el.contains(ae)", FOCUSED)
        self.assertIn("main.el.contains(ae)", FOCUSED)

    def test_严格档在焦点不在页内时返回null(self):
        """同样不钉具体的兜底表达式,只认 strict 为真时必须返回 null。"""
        self.assertTrue(re.search(r"return strict \? null :", FOCUSED))

    def test_分派同时取了宽严两档(self):
        self.assertIn("const p = focusedAppPage();", DISPATCH)
        self.assertIn("const fp = focusedAppPage(true);", DISPATCH)


class Ctrl系不被拦截(unittest.TestCase):
    def test_入口就把带ctrl的挡在门外(self):
        """⌃C / ⌃D / ⌃Z / ⌃R 必须原样进 PTY。这一句是那条口径的唯一实现点,
        动它之前先想清楚 shell 那边会丢什么。"""
        self.assertIn("if (!ev.metaKey || ev.ctrlKey || ev.altKey) return;", DISPATCH)


class 窗口层面的键按应用分派(unittest.TestCase):
    def test_cmd_t终端在前台时开终端标签(self):
        """从前无条件 browserNewTab,终端在前台时也一样(用户实见)。"""
        self.assertIn('if (p && p.terminal && typeof openShellTerminal === "function") { openShellTerminal(); return; }', DISPATCH)

    def test_cmd_t没有应用在前台时仍退回浏览器(self):
        """保留原有落点:不聚焦任何应用时 ⌘T 开浏览器新标签,与桌面习惯一致。"""
        seg = DISPATCH[DISPATCH.index('if (!ev.shiftKey && k === "t")'):DISPATCH.index('if (ev.shiftKey && (k === "{"')]
        self.assertIn('if (typeof browserNewTab === "function") browserNewTab(true);', seg)

    def test_cmd_w用focusedAppPage而不是pActive(self):
        seg = DISPATCH[DISPATCH.index('if (k === "w")'):DISPATCH.index('if (ev.shiftKey && k === "t")')]
        self.assertNotIn("pActive", seg)
        self.assertIn("p.browser", seg)
        self.assertIn("p.terminal", seg)
        self.assertIn("closePage(p);", seg)

    def test_cmd_w没有应用在前台时交回宿主(self):
        """浮层没开时不能吞掉 ⌘W,那是关窗口。"""
        seg = DISPATCH[DISPATCH.index('if (k === "w")'):DISPATCH.index('if (ev.shiftKey && k === "t")')]
        self.assertIn("if (!p) return;", seg)

    def test_cmd数字先给应用内标签_再落到程序坞(self):
        """⌘1..9 从前写死给程序坞。现在应用有标签就先归它(macOS 终端/浏览器都是这样),
        没有标签可跳才落回程序坞——两个语义共用一组键,顺序不能反。"""
        seg = DISPATCH[DISPATCH.index('if (!ev.shiftKey && k >= "1"'):]
        i_term = seg.index("termAppActivateNth(n)")
        i_br = seg.index("browserActivateNth(n)")
        i_dock = seg.index("dockActivateNth(n)")
        self.assertLess(i_term, i_dock)
        self.assertLess(i_br, i_dock)

    def test_数字9是末张不是第九张(self):
        """与 macOS 一致:⌘9 跳最后一张。"""
        self.assertIn('const n = k === "9" ? -1 : Number(k) - 1;', DISPATCH)


class 内容类的键要求焦点在页内(unittest.TestCase):
    def test_清屏字号复制全选都走严格档(self):
        """见文件头「其二」。用 p 而不是 fp 会把设置面板的 ⌘K 抢走。"""
        for call in ("termAppClear()", "termAppFontSize", "termAppCopySelection()", "termAppSelectAll()"):
            seg = DISPATCH[:DISPATCH.index(call)]
            tail = seg[seg.rindex("if ("):]
            self.assertIn("fp", tail, "%s 这一支应当用严格档 fp" % call)


class 终端提供对应的能力(unittest.TestCase):
    def test_跳第n张与循环(self):
        self.assertIn("function termAppActivateNth(n)", TERMINAL)
        self.assertIn("function termAppCycleTab(dir)", TERMINAL)

    def test_清屏用term_clear而不是给shell发命令(self):
        """发 clear 会在提示符上留一行回显,TUI 占屏时还会被当成输入。"""
        fn = re.search(r"function termAppClear\(\)[\s\S]+?\n}", TERMINAL).group(0)
        self.assertIn("term.clear()", fn)
        self.assertNotIn("termAppSend", fn)

    def test_字号有上下界且改完refit(self):
        """列数变了必须把新几何告诉 PTY,否则 shell 仍按旧宽度折行。"""
        fn = re.search(r"function termAppFontSize\(delta\)[\s\S]+?\n}", TERMINAL).group(0)
        self.assertIn("TM_FONT_MIN", fn)
        self.assertIn("TM_FONT_MAX", fn)
        self.assertIn("refit()", fn)

    def test_字号键收下带shift的写法(self):
        """美式布局上放大那一颗要按 shift 才出 "+"(不按出 "=");只认不带 shift 的话
        ⌘⇧+ 会漏掉,而多数人正是这么按的。"""
        seg = re.search(r'if \(k === "=" \|\| [\s\S]+?\n  \}', DISPATCH).group(0)
        for ch in ('"="', '"+"', '"-"', '"_"'):
            self.assertIn(ch, seg)
        self.assertIn('!ev.shiftKey && k === "0"', seg)

    def test_字号顶到边仍算接住(self):
        """返回 false 的话这一下会漏给宿主浏览器去缩放整页。"""
        fn = re.search(r"function termAppFontSize\(delta\)[\s\S]+?\n}", TERMINAL).group(0)
        self.assertIn("if (next === cur) return true;", fn)

    def test_复制没选区时不接管(self):
        """没选区返回 false → 分派那边不 preventDefault,这一下什么也不发生。
        不改成发 SIGINT:那是 ⌃C 的活,代劳会给「Ctrl 系永远属于 shell」开一个例外。"""
        fn = re.search(r"function termAppCopySelection\(\)[\s\S]+?\n}", TERMINAL).group(0)
        self.assertIn("if (!sel) return false;", fn)
        self.assertNotIn("\\x03", fn)


class 浏览器提供对应的能力(unittest.TestCase):
    def test_跳第n张与循环(self):
        self.assertIn("function browserActivateNth(n)", BROWSER)
        self.assertIn("function browserCycleTab(dir)", BROWSER)


class 两份termOpts保持同步(unittest.TestCase):
    """app-assets-term.js 与 term-embed.js 各有一份 termOpts,是彼此独立的副本。
    改一处必须同时改另一处,否则应用终端与内嵌终端的行为会悄悄分叉——回滚行数就是
    这样分叉过:内嵌一直是 5000,应用终端漏了,停在 xterm 默认的 1000。"""

    def test_回滚都是5000(self):
        self.assertIn("scrollback: 5000", ASSETS_TERM)
        self.assertIn("scrollback: 5000", EMBED)

    def test_选区色两边一致(self):
        """xterm 不给 selectionBackground 时用一个偏蓝的默认值,压在深色底上几乎看不见。
        用半透明白,盖住任何前景色都还能读出下面的字;因此不设 selectionForeground
        (设了会把彩色输出统一刷成一种颜色)。失焦档再淡一半,好看出键盘在哪个终端上。"""
        for src in (ASSETS_TERM, EMBED):
            self.assertIn('selectionBackground: "rgba(255,255,255,0.26)"', src)
            self.assertIn('selectionInactiveBackground: "rgba(255,255,255,0.13)"', src)
            # 只查它有没有作为配置项写进去;注释里提到这个名字是允许的。
            self.assertIsNone(re.search(r"selectionForeground\s*:", src))

    def test_终端有浅色主题并跟随外观切换(self):
        for src in (ASSETS_TERM, EMBED):
            self.assertIn('background: "#191919"', src)
            self.assertIn('background: "#fbfbfb"', src)
            self.assertIn('foreground: "#343434"', src)
            self.assertRegex(src, r'term\.options\.theme = termTheme\((?:hostEl)?\)')
            self.assertIn('attributeFilter: ["data-theme"]', src)
            self.assertIn("themeObserver.disconnect()", src)
        css = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
        self.assertIn("--terminal-background: #191919", css)
        self.assertIn("--terminal-background: #fbfbfb", css)
        self.assertIn("background: var(--terminal-background)", css)

    def test_终端字面两边一致且不再压缩字距(self):
        for src in (ASSETS_TERM, EMBED):
            self.assertIn("fontSize: 13.5", src)
            self.assertIn("lineHeight: 1.0", src)
            self.assertIn("letterSpacing: 0", src)

    def test_右键选词两边都开(self):
        self.assertIn("rightClickSelectsWord: true", ASSETS_TERM)
        self.assertIn("rightClickSelectsWord: true", EMBED)


if __name__ == "__main__":
    unittest.main()
