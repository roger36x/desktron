"""仅操作生成的临时历史；原生测试不运行模型，也不读取用户会话。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

from test_codex_recall import app
from codex_recovery import _snapshot, recover_recalled_thread


def fixture(home, mode="paginated", duplicate=False):
    tid = str(uuid.uuid4())
    turns = [str(uuid.uuid4()) for _ in range(3)]
    stamp = "2026-09-08T00:00:00.000Z"
    rows = [{"type": "session_meta", "payload": {"id": tid, "session_id": tid,
             "timestamp": stamp, "cwd": str(home), "originator": "desktron",
             "cli_version": "0.153.0", "source": "cli", "model_provider": "openai",
             "history_mode": mode, "base_instructions": {"text": "Isolated recovery test."}}}]
    for i, turn in enumerate(turns):
        rows.extend([
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
            {"type": "turn_context", "payload": {"turn_id": turn, "cwd": str(home),
             "approval_policy": "never", "sandbox_policy": {"type": "read-only"},
             "model": "gpt-5.5", "summary": "auto"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": f"request-{i}",
                                                 "images": [], "local_images": []}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": f"request-{i}"}]}},
            {"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": turn}},
        ])
    folder = home/"sessions/2026/09/08"
    folder.mkdir(parents=True)
    path = folder/f"rollout-2026-09-08T00-00-00-{tid}.jsonl"
    with open(path, "w") as f:
        for i, row in enumerate(rows):
            row.update(timestamp=stamp, ordinal=i - (1 if duplicate and i >= 6 else 0))
            f.write(json.dumps(row) + "\n")
    return tid, turns, path


class Snapshot(unittest.TestCase):
    def test_snapshot_repairs_ordinals_without_changing_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            tid, _, path = fixture(Path(tmp), duplicate=True)
            original = path.read_bytes()
            dest = Path(tmp)/"copy.jsonl"
            _snapshot(path, dest, tid, "copy")
            rows = [json.loads(line) for line in dest.read_text().splitlines()]
            self.assertEqual([r["ordinal"] for r in rows], list(range(len(rows))))
            self.assertEqual(rows[0]["payload"]["id"], "copy")
            self.assertEqual(path.read_bytes(), original)

    def test_snapshot_rejects_unresolved_history_base_and_wrong_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            tid, _, path = fixture(Path(tmp))
            dest = Path(tmp)/"copy.jsonl"
            with self.assertRaises(RuntimeError):
                _snapshot(path, dest, "wrong-id", "copy")
            rows = path.read_text().splitlines()
            head = json.loads(rows[0]); head["payload"]["history_base"] = {"thread_id": "parent"}
            rows[0] = json.dumps(head); path.write_text("\n".join(rows) + "\n")
            with self.assertRaises(RuntimeError):
                _snapshot(path, dest, tid, "copy")
            self.assertFalse(dest.exists())


@unittest.skipUnless(os.environ.get("DESK_TEST_NATIVE_RECALL") == "1", "显式启用本机历史协议测试")
class NativeRecovery(unittest.TestCase):
    def test_repeated_native_start_interrupt_resume_keeps_ordinals_contiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            # 模型端点固定到本机关闭端口；测试只验证取消协议，绝不调用真实模型。
            (home/"config.toml").write_text('''model = "local-test"
model_provider = "loopback"
[model_providers.loopback]
name = "Loopback test"
base_url = "http://127.0.0.1:9/v1"
wire_api = "responses"
''')
            env = dict(os.environ, CODEX_HOME=tmp)
            tid = ""
            catalog = {"models": [{"id": "local-test", "model": "local-test"}], "modes": []}
            for _ in range(3):
                notifications = []
                with mock.patch.object(app, "catalog", return_value=catalog):
                    tid = app.run_turn(tmp, env, tid, "cancel immediately", {"model": "local-test"},
                        session_id="native-cancel-test", timeout=15,
                        on_turn_started=lambda *_: app.interrupt("native-cancel-test"),
                        on_notification=lambda method, params: notifications.append((method, params)))
                completed = [p for m, p in notifications if m == "turn/completed"]
                self.assertTrue(completed)
                self.assertEqual(completed[-1]["turn"]["status"], "interrupted")
                app.drop_session("native-cancel-test")
            for path in home.rglob("rollout-*.jsonl"):
                app._validate_recall_history({"id": tid, "path": str(path), "historyMode": "paginated"})

    def test_native_recovery_and_receipt_loss_retry_preserve_previous_turns(self):
        for mode, fail_once in (("legacy", False), ("paginated", True)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)/"source"
                tid, turns, path = fixture(home, mode, duplicate=True)
                original = path.read_bytes()
                state, failed = {}, False
                def checkpoint(**patch):
                    nonlocal failed
                    state.update(patch)
                    if fail_once and patch.get("phase") == "verifying" and not failed:
                        failed = True
                        raise TimeoutError("simulated crash after native write")
                env = dict(os.environ, CODEX_HOME=str(home))
                args = (tid, turns[-1], tmp, env, str(Path(tmp)/"recovery"), state, checkpoint)
                if fail_once:
                    with self.assertRaises(RuntimeError):
                        recover_recalled_thread(*args)
                    self.assertIn("expected", state)
                result = recover_recalled_thread(*args)
                self.assertTrue(result["verified"])
                self.assertEqual(result["boundary"]["count"], 2)
                self.assertEqual(result["boundary"]["tail"], turns[-2])
                self.assertEqual(path.read_bytes(), original)
                again = recover_recalled_thread(*args)
                self.assertEqual(again["thread_id"], result["thread_id"])
                self.assertEqual(again["boundary"], result["boundary"])


if __name__ == "__main__":
    unittest.main()
