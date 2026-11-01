"""TUI 退场之后的不变量(2026-08-20)。

引擎不再以嵌入式终端的形态跑:所有会话都是 GUI 会话(POST /session,进程由适配器按回合起)。
这份用例锁的是「没有第二条路」——分流点恒落在 GUI 那一侧、创建入口不再起 PTY、
执行记录与工作流聊天框都走同一条 newHtmlSessionWithPrompt。

替代的是原来的 test_frontend_view_pref_role(视图偏好按后端身份分流),那条前提已经不存在。
"""
import os
import re
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FE = os.path.join(ROOT, "frontend")


def _read(name):
    return _srcmap.frontend_src(name)


class NoTuiPath(unittest.TestCase):
    def test_视图偏好恒为_gui(self):
        """hsViewPref/termUsable 是二十来处分流点共用的判据,恒定之后那些分支才不会再走回终端。"""
        src = _read("app-chat.js")
        self.assertIn('function hsViewPref() { return "html"; }', src)
        self.assertIn("function termUsable() { return true; }", src)

    def test_不再有终端引擎这一档(self):
        """TERMINAL_ENGINES 恒空:按它分流的几处(执行记录删除、执行入口)一律落在 GUI。"""
        src = _read("app-assets.js")
        self.assertRegex(src, r"const TERMINAL_ENGINES = \[\];")

    def test_创建会话不再起_pty(self):
        """newTerminal 与工作流聊天框都不再 POST /workbench/terminals 建引擎会话。

        /workbench/terminals 这个路径仍在(shell 终端、执行记录的删除历史路径会用到),
        所以判据是「不带 engine 字段地建会话」这件事没有了,而不是路径整个消失。"""
        for name in ("app-assets.js", "app-panel.js"):
            src = _read(name)
            self.assertNotIn('body: JSON.stringify({ engine, domain: dom', src, name)
            self.assertNotIn("engine: curEngine, domain: domainId, prompt: text", src, name)

    def test_执行记录与工作流聊天框共用一个入口(self):
        src = _read("app-panel.js")
        self.assertGreaterEqual(src.count("newHtmlSessionWithPrompt"), 2, "两处入口应各调一次")
        self.assertIn("task: true", src)

    def test_切回终端的入口已撤(self):
        """「终端视图」那枚钮与 /login /logout 的「切到终端办」都没有去处了,
        登录改为开一个内嵌 shell 跑引擎自己的命令。"""
        src = _read("app-chat.js")
        self.assertNotIn("hsToTerminal", src)
        self.assertNotIn("hsSetViewPref", src)
        self.assertIn("function hsLoginShell(", src)
        self.assertNotIn("opencode auth login", src)


if __name__ == "__main__":
    unittest.main()
