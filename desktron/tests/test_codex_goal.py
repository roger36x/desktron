"""Codex 目标从 rollout 的成功工具回执恢复,并与 Claude 暴露同一界面契约。"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv


class CodexGoal(unittest.TestCase):
    ESID = "019ee670-40e5-7972-abaf-b945ef812bb9"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "rollout.jsonl")
        self.ad = ea.CodexAdapter(self.tmp.name, self.tmp.name, ea.EventBus())
        self.ad.index = {"desk": {"engine_sid": self.ESID}}
        self.ad._rollout_path = lambda _esid: self.path
        self.ad._goal_rollout_cache.pop(self.path, None)

    def tearDown(self):
        self.ad._goal_rollout_cache.pop(self.path, None)
        self.tmp.cleanup()

    def append(self, *rows):
        with open(self.path, "a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def call(self, cid, method="create_goal", direct=False):
        payload = {"type": "function_call" if direct else "custom_tool_call",
                   "name": method if direct else "exec", "call_id": cid}
        if direct:
            payload["arguments"] = json.dumps({"objective": "完整交付"})
        else:
            payload["input"] = f"const r = await tools.{method}({{ status: \"complete\" }});"
        return {"type": "response_item", "payload": payload}

    def output(self, cid, objective, status):
        body = {"goal": {"threadId": self.ESID, "objective": objective,
                         "status": status, "updatedAt": 1787458877}}
        return {"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "call_id": cid,
            "output": [{"type": "input_text", "text": "Script completed\nOutput:\n"},
                       {"type": "input_text", "text": json.dumps(body, ensure_ascii=False)}]}}

    def test_读取嵌套_create_goal_成功回执(self):
        self.append(self.call("c1"), self.output("c1", "优化，直到完整交付", "active"))
        self.assertEqual(self.ad.goal("desk"), {
            "condition": "优化，直到完整交付", "met": False,
            "status": "active", "at": 1787458877000})

    def test_增量读取_update_goal_完成状态(self):
        self.append(self.call("c1"), self.output("c1", "完整交付", "active"))
        self.assertFalse(self.ad.goal("desk")["met"])
        self.append(self.call("c2", "update_goal"), self.output("c2", "完整交付", "complete"))
        self.assertTrue(self.ad.goal("desk")["met"])

    def test_普通命令回显_goal_json_不能篡改状态(self):
        self.append(self.call("c1"), self.output("c1", "真实目标", "complete"))
        self.append({"type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "echo",
            "input": "tools.exec_command({cmd: 'cat log'})"}},
            self.output("echo", "过期目标", "active"))
        got = self.ad.goal("desk")
        self.assertEqual(got["condition"], "真实目标")
        self.assertTrue(got["met"])

    def test_没有成功回执不算设置目标(self):
        self.append(self.call("c1", direct=True))
        self.assertEqual(self.ad.goal("desk"), {})


if __name__ == "__main__":
    unittest.main()
