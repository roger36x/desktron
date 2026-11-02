"""切引擎重载后，欢迎页不应被会话列表刷新改回会话布局(2026-11-02 用户实见)。

从 Shepherd 会话切到 Pi：重载后恢复流程先把上一段会话页建出来，activeTermId 指向它；
随后按 Pi 的快照落回欢迎页。会话列表刷新时见「有活动会话、头部未建」便调 setTermHead，
把那段会话的标题挂到欢迎页上，并摘掉 #terminal 的 welcome-on。欢迎页随即退回 1040px
栏宽，两侧露出 #terminal 深一档的底色，即「两边灰边」。
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TERM_UI = open(os.path.join(ROOT, "frontend", "app-assets-term-ui.js"), encoding="utf-8").read()
TERMS = open(os.path.join(ROOT, "frontend", "app-panel-terms.js"), encoding="utf-8").read()


def welcome_fn():
    m = re.search(r"\nfunction _isWelcomeVisible\(\) \{.*?\n\}\n", TERM_UI, re.S)
    assert m, "_isWelcomeVisible 不见了"
    return m.group(0)


def head_block():
    """取会话列表刷新里补建主区头部的那一段。"""
    m = re.search(r"\n    const welcomeShown = .*?\n    \}\n", TERMS, re.S)
    assert m, "会话列表刷新里的头部补建段不见了"
    return m.group(0)


HARNESS = r"""
let _termWatermark = WM;
let activeTermId = "adp_prev";
let _termHeadSid = null;
const calls = [];
function termById(id) { return { id, title: "previous" }; }
function setTermHead(t) { calls.push(t.id); }
function stripTitleGlyph(s) { return s; }
function renderTermToc() {}
const $ = () => null;
__FN__
(function () {
__BLOCK__
})();
console.log(JSON.stringify(calls));
"""


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class WelcomeHeadAfterEngineSwitch(unittest.TestCase):
    def _run(self, watermark):
        wm = "null" if watermark is None else (
            "{ style: { display: %s }, classList: { contains: (c) => %s.includes(c) } }"
            % (json.dumps(watermark["display"]), json.dumps(watermark.get("classes", []))))
        js = HARNESS.replace("WM", wm).replace("__FN__", welcome_fn()).replace("__BLOCK__", head_block())
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_welcome_visible_skips_head(self):
        self.assertEqual(self._run({"display": "block"}), [])

    def test_no_welcome_still_builds_head(self):
        self.assertEqual(self._run(None), ["adp_prev"])
        self.assertEqual(self._run({"display": "none"}), ["adp_prev"])

    def test_handoff_progress_keeps_old_behavior(self):
        self.assertEqual(self._run({"display": "block", "classes": ["handoff-progress"]}), ["adp_prev"])


if __name__ == "__main__":
    unittest.main()
