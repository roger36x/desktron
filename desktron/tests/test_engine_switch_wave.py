"""切引擎 = PUT 偏好 + 整页重载。重载后的那一波请求决定人等多久(2026-09-04 盘点)。

Playwright 对着活 Desk 抓调用栈,打开一段会话后 ~600ms 又来了一轮 /session → /session/adopt →
整份 /message(经隧道 243KB、1.4s)加 /config、/state:是事件流第一次连上时走了「断线恢复」。
首次连上没有断线窗口可补。另有 pageshow 在首次导航也触发 hsResume,把随首页内联的任务清单
清掉再拉一遍。这里把几条守卫钉住,并把列表里思考正文的截断与对账口径一起固定。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea
finally:
    sys.argv = _argv

CHAT = _srcmap.chat_src()


def _read(name):
    return open(os.path.join(ROOT, "frontend", name), encoding="utf-8").read()


def fn(src, name):
    m = re.search(r"\n(?:async )?function %s\(.*?\n}" % re.escape(name), src, re.S)
    assert m, "%s 不见了" % name
    return m.group(0)


class FirstConnectIsNotReconnect(unittest.TestCase):
    CORE = _read("app-chat-core.js")

    def test_首次连上只核对在跑的会话(self):
        seg = fn(self.CORE, "hsEnsureSSE")
        i = seg.index("_hsSSE.onopen")
        self.assertIn("const first = !_hsSSEOpened", seg[i:])
        self.assertIn("hsRecoverLiveViews({ busyOnly: true }); return;", seg[i:])
        self.assertIn("hsRecoverLiveViews();", seg[i:], "重连那一支照旧全量核对")

    def test_只跑在跑的那几段_且不清任务缓存(self):
        seg = fn(self.CORE, "hsRecoverLiveViews")
        self.assertIn("if (busyOnly && !v.busy) continue;", seg)
        self.assertIn("if (!busyOnly) try {", seg, "首次连上不清任务缓存:那份是随首页内联来的")
        self.assertIn("!v.historyReady || hsHidden(v)", seg)

    def test_首次导航的_pageshow_不算回来(self):
        seg = fn(self.CORE, "hsResume")
        self.assertIn('e.type === "pageshow" && !e.persisted) return;', seg)
        # 守卫在重连判断之前,不动「重连之后照样核对」那条既有约束
        self.assertLess(seg.index("!e.persisted"), seg.index("away > 5000"))


class CachedLookups(unittest.TestCase):
    def test_归属按_session_id_匹配侧栏清单(self):
        src = _read("app-chat-tasksbar.js")
        seg = fn(src, "hsSessionDomain")
        self.assertIn("x.session_id === v.sid || x.id === v.sid", seg,
                      "清单行的 id 是引擎侧 uuid,只比 id 对 GUI 会话恒不中")

    def test_引擎侧id先查侧栏清单(self):
        seg = fn(CHAT, "hsEngineSid")
        self.assertIn("x.session_id === v.sid", seg)
        self.assertLess(seg.index("_termsList"), seg.index('api("/session")'))


class ShapeUsesFullLength(unittest.TestCase):
    def _shape(self, parts):
        script = (fn(CHAT, "hsProcessShape")
                  + "\nconsole.log(JSON.stringify(hsProcessShape("
                  + json.dumps(parts, ensure_ascii=False) + ")));")
        got = subprocess.run(["node", "-e", script], check=True, text=True,
                             capture_output=True).stdout
        return json.loads(got)

    def test_正文思考与工具输出按全长标记比较(self):
        got = self._shape([
            {"type": "text", "text": "正文"},
            {"type": "reasoning", "text": "截", "textMore": 20},
            {"type": "tool", "state": {"output": "截", "outputMore": 30}},
        ])
        self.assertEqual(got, {"text": 2, "reasoning": 20, "tools": 1, "output": 30,
                               "returns": 0, "returnText": 0, "returnMeta": "", "approvals": "", "compacts": ""})

    def test_同一工具调用的实时外壳与历史归一化形状相同(self):
        live = self._shape([{"type": "tool", "state": {
            "input": {"command": '{"input":"const r = await tools.exec_command(...)"}'},
            "inputMore": 9000,
            "output": "done",
        }}])
        replay = self._shape([{"type": "tool", "state": {
            "input": {"command": "const r = await tools.exec_command(...)"},
            "output": "done",
        }}])
        self.assertEqual(live, replay)

    def test_真实缺工具或缺输出仍能检出(self):
        full = self._shape([{"type": "tool", "state": {"output": "done"}}])
        missing_tool = self._shape([])
        missing_output = self._shape([{"type": "tool", "state": {"output": ""}}])
        self.assertNotEqual(full, missing_tool)
        self.assertNotEqual(full, missing_output)


class ReasoningTrim(unittest.TestCase):
    class Bus:
        def publish(self, e):
            pass

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, self.Bus())
        self.sid = self.ad.create_session()["id"]
        self.cap = ea.CliAdapter.LIST_REASON_CAP

    def tearDown(self):
        self.tmp.cleanup()

    def _msg(self, parts):
        return [{"info": {"id": "m1", "role": "assistant"}, "parts": parts}]

    def think(self, pid, text):
        return {"id": pid, "type": "reasoning", "text": text}

    def test_长思考截断并标全长(self):
        got = self.ad._trim_outputs(self._msg([self.think("r1", "思" * 5000)]))
        p = got[0]["parts"][0]
        self.assertEqual(len(p["text"]), self.cap)
        self.assertEqual(p["textMore"], 5000)

    def test_短思考原样(self):
        rows = self._msg([self.think("r1", "短")])
        got = self.ad._trim_outputs(rows)
        self.assertIs(got[0], rows[0])
        self.assertNotIn("textMore", got[0]["parts"][0])

    def test_不改原对象(self):
        rows = self._msg([self.think("r1", "思" * 5000)])
        self.ad._trim_outputs(rows)
        self.assertEqual(len(rows[0]["parts"][0]["text"]), 5000)

    def test_列表那条路真的截了_按块取回全文(self):
        for p in self._msg([self.think("r1", "思" * 5000)]):
            self.ad._append_msg(self.sid, p)
        body = json.loads(b"[" + self.ad.messages_window_json(self.sid, 25) + b"]")
        self.assertEqual(body[0]["parts"][0]["textMore"], 5000)
        self.assertEqual(len(self.ad.part(self.sid, "r1")["text"]), 5000)

    def test_阈值有据(self):
        """p50 727 字的样本里截到 600 剩四成;思考默认折叠,展开才多一趟请求。"""
        self.assertGreaterEqual(self.cap, 500)
        self.assertLess(self.cap, ea.CliAdapter.LIST_OUTPUT_CAP)


class ReasoningLazyFrontend(unittest.TestCase):
    def test_展开时取回全文(self):
        seg = fn(CHAT, "hsThinkFull")
        self.assertIn("part.textMore", seg)
        self.assertIn("/part/", seg)
        self.assertIn('hsViewportChange(v, () => {', seg)
        self.assertIn('}, false, "reasoning-full")', seg)
        tx = seg[seg.index('hsViewportChange(v, () => {'):seg.index('}, false, "reasoning-full")')]
        self.assertIn("part.text = got.text", tx)
        self.assertIn("delete part.textMore", tx)
        self.assertIn("more.hidden = true", tx)
        self.assertIn("hsPaint(v, part)", tx)

    def test_外层展开接到全文取回而非隐藏的思考按钮(self):
        seg = fn(CHAT, "hsPaint")
        gate = seg.index('if (!part.turn.procEl?.classList.contains("open"))')
        self.assertIn("hsThinkFull(v, part)", seg[gate:])
        toggle = fn(CHAT, "hsProcBox")
        self.assertIn('if (q.type !== "reasoning") continue', toggle)
        self.assertIn("hsPaint(t.view, q)", toggle)


if __name__ == "__main__":
    unittest.main()
