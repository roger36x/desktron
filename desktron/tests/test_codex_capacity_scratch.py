"""Codex capacity retry must not stop a Shepherd turn by re-reporting its process.

A capacity retry can continue the same turn on the same resident app-server,
reporting that connection again. Shepherd must not stop the
turn with "Native process already registered for this turn".
"""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.dirname(__file__))
import codex_appserver as app
from shepherd.execution_scratch import ExecutionScratch
import test_codex_appserver as base  # module import: do not re-collect its test classes


class CapacityRetryScratch(unittest.TestCase):
    def test_capacity_retry_reports_same_process_to_shepherd_scratch(self):
        capacity = base.CapacityRetry._capacity

        class Engine(base.FakeConnection):
            def __init__(self, cwd=None, env=None, binary="codex", opts=None):
                super().__init__(cwd, env, binary, opts)
                self.turn_no = 0
                self.user_echo = False

            def request(self, method, params=None, on_notification=None, timeout=30, **kw):
                if method == "turn/start":
                    self.calls.append((method, params or {}))
                    self.turn_no += 1
                    self.user_echo = True
                    return {"turn": {"id": f"turn-{self.turn_no}"}}
                return super().request(method, params, on_notification, timeout, **kw)

            def read(self, timeout=30):
                if self.user_echo:
                    self.user_echo = False
                    return {"method": "item/completed", "params": {"threadId": "thread-1",
                        "turnId": f"turn-{self.turn_no}", "item": {"id": f"user-{self.turn_no}",
                        "type": "userMessage", "content": []}}}
                if self.turn_no < 2:
                    return capacity(f"turn-{self.turn_no}")
                return {"method": "turn/completed", "params": {"threadId": "thread-1",
                    "turn": {"id": "turn-2", "status": "completed"}}}

        identity = {"pid": 4242, "pgid": 4242, "start_token": "fixture"}
        reported = []
        with tempfile.TemporaryDirectory() as root:
            scratch = ExecutionScratch(Path(root), {"binding_id": "codex-fixture"}, "conversation:1")

            def on_process(proc):
                reported.append(proc)
                scratch.register(proc)

            try:
                with mock.patch.object(app, "_Connection", Engine), \
                     mock.patch.object(app, "catalog", return_value=base.CATALOG), \
                     mock.patch.object(app, "CAPACITY_RETRY_DELAYS", (0,)), \
                     mock.patch("shepherd.execution_scratch.process_identity", return_value=identity):
                    got = app.run_turn("/work", {}, "", "原问题", {"model": "gpt-5.6-sol"},
                                       session_id="desk-capacity-scratch", on_process=on_process)
            finally:
                app.drop_session("desk-capacity-scratch")
            self.assertEqual(got, "thread-1")
            self.assertEqual(len(reported), 2)
            self.assertIs(reported[0], reported[1])
            self.assertEqual(len(scratch.data["processes"]), 1)
            scratch.data["processes"][0]["released"] = True
            scratch._save()
            scratch.__exit__(None, None, None)
