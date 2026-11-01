"""终端应用的手机端。

原来这一路在手机上整个下线,点了只弹一句「终端在手机上用不了」。那道闸的理由是
「TUI 按 80 列排版,竖屏只剩四十列出头」——对 claude/codex 的 TUI 成立,对这个应用不成立:
它跑的是普通 shell,ls / git status / tail -f 四十列足够。闸拦错了对象(2026-08-17 用户实见)。

放行之后手机上真正缺的是软键盘没有的那几个键:Esc、Tab 补全、Ctrl+C、上下翻历史。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class ShellOnPhone(unittest.TestCase):
    def setUp(self):
        self.js = _read("frontend/app-terminal.js")
        self.css = _read("frontend/style.css")

    def test_不再拦住_shell(self):
        fn = re.search(r"function openTerminalApp\(opts = \{\}\)[\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(fn, "openTerminalApp 不见了")
        # 注释里仍会提到它(说明这道闸为什么挪走),断言的是没有实际调用
        code = "\n".join(ln for ln in fn.group(0).split("\n")
                          if not ln.strip().startswith("//"))
        self.assertNotIn("termUsable", code)
        self.assertNotIn("终端在手机上用不了", self.js)

    def test_引擎_tui_那一路已经没有了(self):
        """原来这条锁的是「窄屏挡住引擎 TUI」。2026-08-20 TUI 整体退场,不存在要挡的那一路了,
        judgement 因此反过来:termUsable 恒真,手机与电脑走同一条 GUI 路。
        本文件其余用例锁的是 shell 终端应用——它留着,与引擎无关。"""
        chat = _srcmap.chat_src()
        self.assertIn("function termUsable() { return true; }", chat)
        self.assertNotIn('if (!termUsable()) return "html"', chat)

    def test_补齐软键盘没有的键(self):
        for k in ("Esc", "Tab", "Ctrl", "^C", "↑", "↓"):
            self.assertIn(f'k: "{k}"', self.js, k)

    def test_按键走_ws_不走本地回显(self):
        """term.input 只是本地回显,进不了 PTY。"""
        fn = re.search(r"function termAppSend\(seq\)[\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(fn)
        self.assertIn("ws.send", fn.group(0))
        self.assertNotIn("term.input", fn.group(0))

    def test_按键不抢焦点(self):
        """click 会让 xterm 先失焦、软键盘跟着收起,每按一次特殊键就要重新点一下终端。"""
        self.assertIn('addEventListener("pointerdown"', self.js)
        self.assertIn("ev.preventDefault()", self.js)

    def test_ctrl_是粘滞的(self):
        """手机上没法同时按住两个键:Ctrl 按下后举着,吃掉下一个字母再落下。"""
        self.assertIn("_tmCtrlArmed", self.js)
        fn = re.search(r"function _termAppWireCtrl\(h\)[\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(fn, "粘滞 Ctrl 没接进按键流")
        body = fn.group(0)
        self.assertIn("attachCustomKeyEventHandler", body)
        self.assertIn("charCodeAt(0) - 96", body)   # a→\x01 … z→\x1a
        self.assertIn("return false", body)         # 接管这一下,xterm 不再自己处理
        # 定义了还要接上:挂载每条终端时都得注册一次,否则 Ctrl 按下去没有落点
        mount = re.search(r"function _termAppMount\(tab\)[\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(mount)
        self.assertIn("_termAppWireCtrl(h)", mount.group(0))

    def test_有粘贴键(self):
        """手机上没有 Cmd+V,长按也唤不出系统菜单——xterm 真正接输入的是隐藏 textarea,
        手指长按落在画布上(2026-08-17 用户实见)。只能由一枚键去读剪贴板。"""
        self.assertIn('k: "Paste"', self.js)
        fn = re.search(r"async function termAppPaste\(\)[\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(fn, "termAppPaste 不见了")
        body = fn.group(0)
        self.assertIn("clipboard.readText", body)
        self.assertIn("isSecureContext", body)      # 安全上下文才读得了
        self.assertIn("term.paste", body)           # 多行走 bracketed paste,不逐条执行
        # 定义了还要接上:那枚键按下去得真的走到这里
        keys = re.search(r"function _termAppKeys\(root\)[\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(keys)
        self.assertIn("termAppPaste()", keys.group(0))

    def test_http_下有退路(self):
        """http 直连(隧道、局域网 IP)不是安全上下文,clipboard.readText 整个不通;
        系统又只对真实的可编辑区域给「粘贴」菜单,而 xterm 那个 textarea 是隐藏的。
        所以退回一个自己的输入框,让系统的长按粘贴落进来(2026-08-17 用户实见报错)。"""
        fn = re.search(r"async function termAppPaste\(\)[\s\S]*?\n}\n", self.js)
        self.assertIn("_termAppPasteBox()", fn.group(0), "读不到剪贴板时没有退路")
        box = re.search(r"function _termAppPasteBox\(\)[\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(box, "_termAppPasteBox 不见了")
        body = box.group(0)
        self.assertIn("textarea", body)
        self.assertIn('addEventListener("paste"', body)   # 贴进来即发,不必再点一次
        self.assertIn("ta.focus()", body)
        self.assertIn(".tm-paste", self.css)

    def test_长按复制也接上了(self):
        """这套手势原来只绑在主区终端的 pane 上,终端应用一直没接。"""
        mount = re.search(r"function _termAppMount\(tab\)[\s\S]*?\n}\n", self.js)
        self.assertIn("wireTermTouchSelect", mount.group(0))

    def test_键条只在窄屏出现(self):
        """按视口判,不按 UA。桌面上有物理键盘,这条只会占地方。"""
        self.assertIn(".tm-keys { display: none; }", self.css)
        i = self.css.index(".tm-keys { display: none; }")
        seg = self.css[i:]
        self.assertIn("@media (max-width: 820px), (max-height: 520px) and (pointer: coarse)", seg)
        self.assertIn(".tm-key {", seg)

    def test_键条让开_home_指示条(self):
        i = self.css.index(".tm-keys {\n    display: flex")
        self.assertIn("env(safe-area-inset-bottom)", self.css[i:i + 400])


if __name__ == "__main__":
    unittest.main()
