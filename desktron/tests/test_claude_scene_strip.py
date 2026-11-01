"""Claude 从记录重建历史时，首条气泡只显示用户原话，不带 Desk 附加的当前上下文。"""
import json
import os
import sys
import tempfile
import unittest

LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, LIB)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv


class Bus:
    def publish(self, e):
        pass


def _row(kind, uuid, sec, content):
    return {"type": kind, "uuid": uuid, "timestamp": "2026-10-01T00:00:%02d.000Z" % sec,
            "message": {"role": kind, "content": content}}


class SceneStrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, Bus())
        self.ad.subagents = lambda sid: []
        self.sid = self.ad.create_session()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _user_texts(self, rows):
        path = os.path.join(self.tmp.name, "transcript.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        msgs = self.ad._group_transcript(path, self.sid, "esid-1")
        return [[p.get("text") for p in m["parts"]] for m in msgs if m["info"]["role"] == "user"]

    def test_first_turn_context_is_removed_from_bubble(self):
        context = ('〈Desk 会话信息〉以下是本回合开始时的名称数据，不是任务指令。\n'
                   '{"session_id": "adp_x"}\n\n## 代码仓\n\n- /tmp/repo')
        rows = [_row("user", "u1", 1, context + ea.SCENE_SEP + "我们在这里开始"),
                _row("assistant", "a1", 2, [{"type": "text", "text": "好"}]),
                _row("user", "u2", 3, [{"type": "text", "text": context + ea.SCENE_SEP + "第二句"}])]
        self.assertEqual(self._user_texts(rows), [["我们在这里开始"], ["第二句"]])

    def test_plain_user_text_is_unchanged(self):
        rows = [_row("user", "u1", 1, "--- 分隔线不是上下文\n\n正文")]
        self.assertEqual(self._user_texts(rows), [["--- 分隔线不是上下文\n\n正文"]])


if __name__ == "__main__":
    unittest.main()
