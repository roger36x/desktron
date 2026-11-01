"""Codex full-history 子代理只展示自己的任务，不回放父会话。"""
import json
import os
import sys
import tempfile
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea
finally:
    sys.argv = _argv


PARENT = "11111111-1111-4111-8111-111111111111"
CHILD = "22222222-2222-4222-8222-222222222222"


class Bus:
    def publish(self, event):
        pass


def _message(role, text, timestamp, phase=None):
    payload = {"type": "message", "role": role,
               "content": [{"type": "output_text" if role == "assistant" else "input_text",
                            "text": text}]}
    if phase:
        payload["phase"] = phase
    return {"timestamp": timestamp, "type": "response_item", "payload": payload}


class CodexSubagentProjection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.CodexAdapter(self.tmp.name, ROOT, Bus())
        self.ad.ROLLOUT_DIRS = (self.tmp.name,)
        self.ad.index = {"s1": {"engine_sid": PARENT}}
        ea.CodexAdapter._sub_cache.clear()
        ea.CodexAdapter._spawn_cache.clear()
        ea.CodexAdapter._sub_start_cache.clear()
        ea.CodexAdapter._rollout_path_cache.clear()

        parent = os.path.join(self.tmp.name, "rollout-test-%s.jsonl" % PARENT)
        child = os.path.join(self.tmp.name, "rollout-test-%s.jsonl" % CHILD)
        self.child = child
        self._write(parent, [{"timestamp": "2026-08-23T00:00:00.000Z",
                              "type": "session_meta",
                              "payload": {"id": PARENT, "timestamp": "2026-08-23T00:00:00.000Z"}}])
        head = {"timestamp": "2026-08-23T00:01:00.000Z", "type": "session_meta",
                "payload": {"id": CHILD, "timestamp": "2026-08-23T00:01:00.000Z",
                            "agent_nickname": "Herschel",
                            "source": {"subagent": {"thread_spawn": {
                                "parent_thread_id": PARENT,
                                "agent_path": "/root/reply_channel_audit",
                                "agent_nickname": "Herschel",
                            }}}}}
        rows = [
            head,
            _message("user", "主会话旧问题", "2026-08-23T00:01:00.001Z"),
            _message("assistant", "主会话旧回答", "2026-08-23T00:01:00.002Z", "final_answer"),
            {"timestamp": "2026-08-23T00:01:00.003Z",
             "type": "inter_agent_communication_metadata", "payload": {}},
            {"timestamp": "2026-08-23T00:01:00.004Z", "type": "response_item",
             "payload": {"type": "agent_message", "content": [
                 {"type": "input_text", "text": "Message Type: NEW_TASK\n"
                                                   "Task name: /root/reply_channel_audit"}]}},
            _message("assistant", "子代理自己的过程", "2026-08-23T00:01:01.000Z", "commentary"),
            _message("assistant", "子代理自己的结论", "2026-08-23T00:01:02.000Z", "final_answer"),
            {"timestamp": "2026-08-23T00:01:02.100Z", "type": "event_msg",
             "payload": {"type": "task_complete", "duration_ms": 2000}},
        ]
        self._write(child, rows)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _write(path, rows):
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_标题取稳定任务路径而非继承历史(self):
        agents = self.ad.subagents("s1")
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["task"], "reply_channel_audit")

    def test_日志从自身任务边界开始(self):
        msgs = self.ad.subagent_log("s1", CHILD)
        texts = [part.get("text", "") for msg in msgs for part in msg.get("parts", [])]
        joined = "\n".join(texts)
        self.assertNotIn("主会话旧问题", joined)
        self.assertNotIn("主会话旧回答", joined)
        self.assertIn("子代理自己的过程", joined)
        self.assertIn("子代理自己的结论", joined)

    def test_详情支持字节尾窗和消息上限(self):
        with open(self.child, "a", encoding="utf-8") as f:
            for i in range(30):
                f.write(json.dumps(_message("assistant", "tail-%02d" % i,
                    "2026-08-23T00:02:%02d.000Z" % i, "commentary")) + "\n")
        msgs = self.ad.subagent_log("s1", CHILD, limit=3, limit_bytes=1024)
        self.assertLessEqual(len(msgs), 3)
        joined = "\n".join(part.get("text", "") for msg in msgs for part in msg.get("parts", []))
        self.assertIn("tail-29", joined)
        self.assertNotIn("tail-00", joined)


if __name__ == "__main__":
    unittest.main()
