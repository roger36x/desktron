"""终端不回答颜色问句:回话会落到提示符上,被回显成字面、还被当成命令执行。

2026-09-03 用户实见,提示符上冒出 `11;rgb:1919/1919/1919`。CDP 实测复现并定位:

    发问   printf '\\033]11;?\\007'        (程序问「你的背景色是什么」)
    回话   \\033]11;rgb:1919/1919/1919\\033\\\\   (xterm 1.3ms 后答,写回 PTY 当输入)
    结果   提示符上出现 11;rgb:1919/1919/1919,随后 bash: 11: command not found

回话本身是对的,真实终端也答。差别在于谁来得及读:发问的程序多半问完就走(探一下深浅
底色再决定配色),它一走,答复就落在 shell 的提示符上。真实终端是本机管道、亚毫秒;
我们这一路要绕一圈浏览器(PTY→后端→WebSocket→xterm 生成→再原路回去),窗口更宽。

不答的代价近乎为零:终端底色写死 #191919(见 termOpts),永远是深色,问与不问得到的
是同一个结论;拿不到答复的程序退回自己的深色默认,与实情一致。若哪天底色跟着主题变,
这个取舍要重算——那时答案才真的带信息。

只吞问句,不碰设置。OSC 4 的 `4;1;rgb:ff00/0000/0000` 是设调色板,吞了就改不了颜色;
判据取「载荷以 ? 结尾」。

光标位置查询(CSI 6n)不在此列:答案只有终端知道,TUI 拿它量位置,不答会排版错乱,
比偶尔漏一段字面严重得多。改完之后 CPR 仍然照答(实测回 \\033[9;1R)。

改完实测:
    OSC 11 问句  → 无回话,屏幕干净
    OSC 4 设置色 → 未被吞,RED 照常显示
    CSI 6n       → 仍回 \\033[9;1R
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TERM = open(os.path.join(ROOT, "frontend", "app-assets-term.js"), encoding="utf-8").read()
EMBED = open(os.path.join(ROOT, "frontend", "term-embed.js"), encoding="utf-8").read()


class 颜色问句被吞掉(unittest.TestCase):
    def test_四个颜色相关的OSC都登记了(self):
        """10/11/12 是前景、背景、光标色,4 是调色板。少登记一个就漏一路。"""
        for src in (TERM, EMBED):
            self.assertIn("[4, 10, 11, 12]", src)
            self.assertIn("registerOscHandler(n, oscQuery)", src)

    def test_判据是载荷以问号结尾(self):
        """返回 true 表示这一条已处理、不再传给 xterm 内建的应答;
        设置形态带具体色值,返回 false 交回去照常生效。"""
        for src in (TERM, EMBED):
            self.assertIn('String(d || "").trim().endsWith("?")', src)

    def test_不去动光标位置查询(self):
        """CSI 6n 归 xterm 自己答:TUI 拿它量位置,吞掉会排版错乱。"""
        for src in (TERM, EMBED):
            self.assertNotIn("registerCsiHandler", src)

    def test_两份终端初始化都改了(self):
        """app-assets-term.js 与 term-embed.js 是各自独立的副本,改一处必须同时改另一处
        ——回滚行数当年就这样分叉过。"""
        self.assertIn("oscQuery", TERM)
        self.assertIn("oscQuery", EMBED)

    def test_登记有异常保护(self):
        """老 build 没有 parser.registerOscHandler,不能因此炸掉整个挂载。"""
        for src in (TERM, EMBED):
            seg = src[src.index("oscQuery") - 200:src.index("registerOscHandler(n, oscQuery)") + 60]
            self.assertIn("try {", seg)
            self.assertIn("catch {}", seg)
