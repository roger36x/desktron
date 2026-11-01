"""会话进侧栏这件事,只有一条路:term_meta_register。

2026-09-08 用户实见:codex 下把一段会话 resume 回来,再怎么发消息,聊天区也不出现它。
根因是登记只挂在「引擎新开线程」那个事件上——resume 与「找回聊天」都不发那个事件,
另有三处拿到引擎 id 之后直接写索引、绕过登记。这里钉住的是登记的入口口径:
凡是拿到引擎 id 的路,都要过 _term_register;分叉那一处例外,它拿的是源会话的 id。
"""
import os
import sys
import tempfile
import unittest

import _srcmap
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv

LIB = os.path.join(ROOT, "desktron", "lib")
CODEX = open(os.path.join(LIB, "engine_adapters", "codex.py"), encoding="utf-8").read()
CLAUDE_TR = open(os.path.join(LIB, "engine_adapters", "claude_transcript.py"), encoding="utf-8").read()
STORE = open(os.path.join(LIB, "engine_adapters", "cli_store.py"), encoding="utf-8").read()
TERM = open(os.path.join(LIB, "term.py"), encoding="utf-8").read()


class Recorder:
    def __init__(self):
        self.events = []

    def publish(self, ev):
        self.events.append(ev)


class 登记入口(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ad = ea.CodexAdapter(self.tmp.name, self.tmp.name, Recorder())

    def _patched(self):
        m = mock.patch("term._term_migrate_identity")
        r = mock.patch("term.term_meta_register")
        self.addCleanup(m.stop)
        self.addCleanup(r.stop)
        return m.start(), r.start()

    def test_同一个引擎id只登记一次(self):
        row = self.ad.create_session("desk-iter")
        _, reg = self._patched()
        self.ad._term_register(row["id"], "tid-1")
        self.ad._term_register(row["id"], "tid-1")
        self.ad._term_register(row["id"], "tid-1")
        reg.assert_called_once_with("tid-1", "codex", "desk-iter", task=False)

    def test_登记失败下一轮还会再试(self):
        row = self.ad.create_session()
        _, reg = self._patched()
        reg.side_effect = [RuntimeError("term 还没起来"), None]
        self.ad._term_register(row["id"], "tid-2")
        self.ad._term_register(row["id"], "tid-2")
        self.assertEqual(reg.call_count, 2)

    def test_空的引擎id不登记(self):
        row = self.ad.create_session()
        _, reg = self._patched()
        self.ad._term_register(row["id"], "")
        reg.assert_not_called()

    def test_找回聊天后这段会话进侧栏(self):
        """import_session 是「找回聊天」与切 GUI 走的那条路。它此前直写 engine_sid,
        于是搬进来的会话在聊天区一直不出现。"""
        row = self.ad.create_session("desk-iter")
        msgs = [{"info": {"role": "user", "at": 1, "sessionID": row["id"]},
                 "parts": [{"type": "text", "text": "你好"}]},
                {"info": {"role": "assistant", "at": 2, "sessionID": row["id"]},
                 "parts": [{"type": "text", "text": "在"}]}]
        _, reg = self._patched()
        with mock.patch.object(self.ad, "_rollout_path", return_value="/tmp/x.jsonl"), \
             mock.patch.object(self.ad, "_rollout_tail", return_value=msgs):
            self.ad.import_session("tid-9", sid=row["id"])
        self.assertEqual((self.ad.index[row["id"]] or {}).get("engine_sid"), "tid-9")
        reg.assert_called_once_with("tid-9", "codex", "desk-iter", task=False)

    def test_resume回合按当前线程补登记(self):
        """thread/started 只在新开线程时发。每回合起头再补一次,才能覆盖 resume。"""
        seg = CODEX[CODEX.index("def on_turn_started"):][:600]
        self.assertIn("self._term_register(ctx[\"sid\"], thread_id)", seg)

    def test_claude搬迁也走同一个登记入口(self):
        seg = CLAUDE_TR[CLAUDE_TR.index("def import_session"):]
        seg = seg[:seg.index("\n    @classmethod")]
        self.assertIn("self._term_register(sid, esid)", seg)

    def test_分叉不拿源会话的引擎id去登记(self):
        """分叉时手上只有「从哪一支分出来」的锚点,新会话自己的引擎 id 还没有。
        拿锚点登记,两段会话会指向同一个 id。"""
        seg = STORE[STORE.index("def fork_session"):]
        seg = seg[:seg.index("\n    def ", 10)]
        self.assertIn('e["fork_pending"] = True', seg)
        self.assertNotIn("_term_register", seg)

    def test_侧栏丢弃会话时留一行(self):
        seg = TERM[TERM.index("if not s:"):][:200]
        self.assertIn("_term_note_dropped(sid, eng)", seg)
        self.assertIn("deskcfg.log(\"term\"", TERM)


if __name__ == "__main__":
    unittest.main()
