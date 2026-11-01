"""Codex 子代理索引不能反复全量解析正在增长的父 rollout。"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FRONTEND = os.path.join(ROOT, "frontend")
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
_argv = sys.argv
sys.argv = ["ui_server.py", FRONTEND, "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv


ESID = "019ee670-1111-7222-8333-444444444444"


class Bus:
    def publish(self, _event):
        pass


def _call(cid, task):
    return {"payload": {"type": "function_call", "name": "spawn_agent", "call_id": cid,
                        "arguments": json.dumps({"message": task}, ensure_ascii=False)}}


def _result(cid, aid):
    return {"payload": {"type": "function_call_output", "call_id": cid,
                        "output": json.dumps({"agent_id": aid})}}


class CodexSpawnIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rollouts = os.path.join(self.tmp.name, "sessions", "2026", "08", "23")
        os.makedirs(self.rollouts)
        self.path = os.path.join(self.rollouts, "rollout-test-%s.jsonl" % ESID)
        self.ad = ea.CodexAdapter(self.tmp.name, self.tmp.name, Bus())
        self.ad.ROLLOUT_DIRS = (os.path.join(self.tmp.name, "sessions"),)
        self.ad._SPAWN_WINDOW_BYTES = 512
        with self.ad._sub_lock:
            self.ad._spawn_cache.clear()
            self.ad._sub_cache.clear()
        with self.ad._rollout_path_lock:
            self.ad._rollout_path_cache.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def _append(self, *rows):
        with open(self.path, "a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_冷读只保留尾部窗口内的派发表(self):
        self._append(_call("old", "旧任务"), _result("old", "agent-old"))
        self._append(*({"payload": {"type": "event_msg", "message": "x" * 80}}
                       for _ in range(12)))
        self._append(_call("new", "最近任务"), _result("new", "agent-new"))

        got = self.ad._spawn_index(ESID)

        self.assertEqual(got.get("agent-new"), "最近任务")
        self.assertNotIn("agent-old", got)

    def test_后续调用只续读新增行并保留旧表(self):
        self._append(_call("one", "任务一"), _result("one", "agent-one"))
        self.assertEqual(self.ad._spawn_index(ESID), {"agent-one": "任务一"})

        self._append(_call("two", "任务二"), _result("two", "agent-two"))

        self.assertEqual(self.ad._spawn_index(ESID), {
            "agent-one": "任务一", "agent-two": "任务二",
        })

    def test_rollout路径命中后不再遍历目录(self):
        self._append({"payload": {"type": "session_meta"}})
        self.assertEqual(self.ad._rollout_path(ESID), self.path)
        with mock.patch.object(ea.os, "walk", side_effect=AssertionError("不应再次遍历")):
            self.assertEqual(self.ad._rollout_path(ESID), self.path)


if __name__ == "__main__":
    unittest.main()
