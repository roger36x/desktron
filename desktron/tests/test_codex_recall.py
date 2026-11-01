"""Codex 快速 Esc 必须同时撤回界面投影与原生 thread 上下文。"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import _srcmap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import codex_appserver as app
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea
    from session_search import codex_documents
finally:
    sys.argv = _argv


class Bus(ea.EventBus):
    def publish(self, event):
        pass


class CodexRecall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.CodexAdapter(self.tmp.name, self.tmp.name, Bus())
        self.sid = self.ad.create_session()["id"]

    def tearDown(self):
        app._drop_all_sessions()
        self.tmp.cleanup()

    @staticmethod
    def row(at, role, text, phase=""):
        payload = {"type": "message", "role": role, "content": [{"text": text}]}
        if phase:
            payload["phase"] = phase
        return {"timestamp": at, "type": "response_item", "payload": payload}

    def rollout(self):
        rows = [
            self.row("2026-09-03T01:00:00.000Z", "user", "保留一"),
            self.row("2026-09-03T01:00:01.000Z", "assistant", "回答一", "final_answer"),
            self.row("2026-09-03T01:00:02.000Z", "user", "应撤回"),
            self.row("2026-09-03T01:00:03.000Z", "assistant", "迟到内容", "final_answer"),
            {"timestamp": "2026-09-03T01:00:04.000Z", "type": "event_msg",
             "payload": {"type": "turn_aborted", "duration_ms": 2100}},
            self.row("2026-09-03T01:00:05.000Z", "user", "保留二"),
            self.row("2026-09-03T01:00:06.000Z", "assistant", "回答二", "final_answer"),
        ]
        path = os.path.join(self.tmp.name, "rollout.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        at = int(self.ad._iso_ms("2026-09-03T01:00:02.000Z"))
        self.ad.index[self.sid]["recall_cuts"] = [[at - 1, at + 1]]
        return path

    def test_刷新重建不会复活撤回回合及迟到中断内容(self):
        messages = self.ad._group_rollout(self.rollout(), self.sid)
        text = " ".join(part.get("text", "") for message in messages
                        for part in message.get("parts") or [])
        self.assertIn("保留一", text)
        self.assertIn("保留二", text)
        self.assertNotIn("应撤回", text)
        self.assertNotIn("迟到内容", text)
        self.assertEqual(sum(self.ad._turn_start(m) for m in messages), 2)

    def test_全局搜索也不会索引已撤回消息(self):
        path = self.rollout()
        docs, turns = codex_documents(self.ad, path, self.sid, self.ad.index[self.sid])
        text = " ".join(doc["text"] for doc in docs)
        self.assertNotIn("应撤回", text)
        self.assertNotIn("迟到内容", text)
        self.assertEqual(turns, 2)

    def test_回合完成后调用原生revert并清除持久凭据(self):
        ctx = {"sid": self.sid, "text": "应撤回", "saved": [], "opts": {},
               "aborted": "recall"}

        def run_turn(*args, **kwargs):
            kwargs["on_turn_started"]("thread-1", "turn-2")
            return "thread-1"

        with mock.patch.object(app, "run_turn", side_effect=run_turn), \
                mock.patch.object(app, "revert_thread", return_value={"ok": True}) as revert:
            self.assertIsNone(self.ad._turn_dispatch(ctx, [], {}, None))

        revert.assert_called_once()
        self.assertEqual(revert.call_args.args[:3], (self.sid, "thread-1", "turn-2"))
        self.assertNotIn("recall_revert", self.ad.index[self.sid])

    def test_撤回失败时凭据保留供下一轮发送前重试(self):
        self.ad.index[self.sid]["recall_revert"] = {
            "thread_id": "thread-1", "before_turn_id": "turn-2"}
        ctx = {"sid": self.sid, "text": "新消息", "saved": [], "opts": {}}
        with mock.patch.object(app, "revert_thread", return_value={
                    "ok": False, "error": "active writer"}), \
                mock.patch.object(app, "run_turn") as run, \
                mock.patch.object(self.ad, "_emit_text") as emit:
            error = self.ad._turn_dispatch(ctx, [], {}, None)
        emit.assert_called_once_with(ctx, error)
        self.assertIn("尚未从 Codex 上下文撤回", error)
        run.assert_not_called()
        self.assertNotIn("recall_revert", self.ad.index[self.sid])
        self.assertEqual(self.ad.index[self.sid]["recall"]["phase"], "retryable")

    def test_回合已结束时中断兜底不误杀撤回连接(self):
        ctx = {"sid": self.sid, "engine_turn_done": True}
        self.ad._live[self.sid] = ctx
        with mock.patch.object(app, "drop_session") as drop:
            self.ad._interrupt_fallback(self.sid, ctx, mock.Mock())
        drop.assert_not_called()

    def test_重试失败更新旧警告且成功后清除(self):
        pending = {"thread_id": "thread-1", "before_turn_id": "turn-2"}
        self.ad.index[self.sid].update(recall_revert=pending, environment={
            "state": "warning", "stage": "recall", "details": ["thread not found"]})
        with mock.patch.object(app, "revert_thread", side_effect=[
                {"ok": False, "error": "thread/revert only supports paginated threads"},
                {"ok": True, "method": "thread/rollback"}]):
            self.assertFalse(self.ad._apply_recall_revert(self.sid, {})["ok"])
            row = self.ad.index[self.sid]
            self.assertEqual(row["environment"]["details"],
                             ["thread/revert only supports paginated threads"])
            self.assertEqual(row["recall"]["before_turn_id"], pending["before_turn_id"])
            self.assertTrue(self.ad._apply_recall_revert(self.sid, {})["ok"])
        self.assertNotIn("recall_revert", row)
        self.assertNotIn("environment", row)

    def test_撤回成功不清除其他引擎警告(self):
        self.ad.index[self.sid].update(recall_revert={
            "thread_id": "thread-1", "before_turn_id": "turn-2"},
            environment={"state": "warning", "stage": "mcp", "details": ["MCP failed"]})
        with mock.patch.object(app, "revert_thread", return_value={"ok": True}):
            self.ad._apply_recall_revert(self.sid, {})
        self.assertEqual(self.ad.index[self.sid]["environment"]["stage"], "mcp")

    def test_撤回区间从实际用户消息时间开始而非可能串轮的live时间(self):
        self.ad._append_msg(self.sid, {
            "info": {"id": "u1", "role": "user", "sessionID": self.sid,
                     "source": self.ad.SRC_USER, "at": 1000},
            "parts": [{"type": "text", "text": "应撤回"}],
        })
        self.ad._live[self.sid] = {"sid": self.sid, "t0": 2000, "proc": None,
                                   "parts": {}, "order": []}

        with mock.patch.object(self.ad, "_soft_interrupt", return_value=False), \
                mock.patch.object(self.ad, "_remember_recall_revert"):
            out = self.ad.abort(self.sid, recall=True)

        self.assertEqual(out["text"], "应撤回")
        self.assertEqual(self.ad.index[self.sid]["recall_cuts"][-1][0], 1000)
        self.assertEqual(self.ad.messages(self.sid), [])


class NativeRevert(unittest.TestCase):
    def test_native_revert_segment_continues_at_history_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "segment.jsonl")
            rows = [{"type": "session_meta", "ordinal": 23, "payload": {
                "id": "thread-1", "history_base": {"end_ordinal_exclusive": 23}}},
                {"type": "event_msg", "ordinal": 24}]
            with open(path, "w") as f:
                for row in rows:
                    f.write(json.dumps(row) + "\n")
            app._validate_recall_history({"id": "thread-1", "historyMode": "paginated", "path": path})

    def test_corrupt_paginated_log_cannot_validate_a_stale_receipt(self):
        from recall_protocol import boundary
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rollout.jsonl")
            rows = [{"type": "session_meta", "ordinal": 0, "payload": {"id": "thread-1"}},
                    {"type": "event_msg", "ordinal": 1},
                    {"type": "event_msg", "ordinal": 1}]
            with open(path, "w") as f:
                for row in rows:
                    f.write(json.dumps(row) + "\n")
            conn = self.connection()
            conn.request.side_effect = lambda method, params, **kw: {"thread": {
                "id": "thread-1", "historyMode": "paginated", "path": path,
                "turns": [{"id": "turn-1"}]}}
            with mock.patch.object(app, "_Connection", return_value=conn):
                out = app.revert_thread("desk-corrupt", "thread-1", "turn-2",
                                        expected=boundary(["turn-1"]))
            self.assertTrue(out["history_corrupt"])
            self.assertTrue(out["conflict"])
            self.assertIn("第 3 行", out["error"])
            self.assertEqual([c.args[0] for c in conn.request.call_args_list],
                             ["thread/resume", "thread/read"])

    def tearDown(self):
        app._drop_all_sessions()

    @staticmethod
    def connection(error=None):
        conn = mock.Mock()
        conn.proc.poll.return_value = None
        ids = ["turn-1", "turn-2"]
        def request(method, params, **kwargs):
            if method == "thread/read":
                return {"thread": {"id": "thread-1", "turns": [{"id": i} for i in ids]}}
            if method in ("thread/revert", "thread/rollback"):
                if method == "thread/revert" and error:
                    raise app.AppServerError(error)
                ids.pop()
            return {"thread": {"id": "thread-1"}}
        conn.request.side_effect = request
        return conn

    def test_优先复用持有thread的常驻连接(self):
        conn = self.connection()
        entry = app._SessionConnection(conn, ("key",))
        entry.thread_id = "thread-1"
        app._SESSIONS["desk-1"] = entry

        out = app.revert_thread("desk-1", "thread-1", "turn-2")

        self.assertTrue(out["ok"])
        self.assertEqual([c.args[0] for c in conn.request.call_args_list],
                         ["thread/read", "thread/revert", "thread/read"])
        self.assertEqual(conn.request.call_args_list[1].args[1], {
            "threadId": "thread-1", "beforeTurnId": "turn-2"})
        self.assertFalse(entry.busy)

    def test_常驻连接已释放时短连接先恢复thread再撤回(self):
        conn = self.connection()
        with mock.patch.object(app, "_Connection", return_value=conn):
            out = app.revert_thread(
                "desk-new", "thread-1", "turn-2", cwd="/tmp/project")

        self.assertTrue(out["ok"])
        conn.initialize.assert_called_once()
        self.assertLessEqual(conn.initialize.call_args.kwargs["overall"], 30)
        self.assertEqual([c.args[0] for c in conn.request.call_args_list],
                         ["thread/resume", "thread/read", "thread/revert", "thread/read"])
        conn.close.assert_called_once_with()

    def test_旧非分页会话恢复后使用rollback(self):
        conn = self.connection("thread/revert only supports paginated threads")
        with mock.patch.object(app, "_Connection", return_value=conn):
            out = app.revert_thread("desk-old", "thread-1", "turn-2", cwd="/tmp/project")
        self.assertTrue(out["ok"])
        self.assertTrue(out["verified"])
        self.assertEqual(out["method"], "thread/rollback")
        self.assertEqual([call.args[0] for call in conn.request.call_args_list],
                         ["thread/resume", "thread/read", "thread/revert", "thread/read",
                          "thread/rollback", "thread/read"])
        self.assertEqual(conn.request.call_args_list[-2].args[1], {"threadId": "thread-1", "numTurns": 1})
        conn.close.assert_called_once_with()

    def test_真实错误不能作为旧协议兼容而删除上一轮(self):
        for error in ["thread not found: thread-1", "active writer", "permission denied"]:
            with self.subTest(error=error):
                conn = self.connection(error)
                with mock.patch.object(app, "_Connection", return_value=conn):
                    out = app.revert_thread("desk-error", "thread-1", "turn-2")
                self.assertFalse(out["ok"])
                self.assertEqual(out["error"], error)
                self.assertEqual(conn.request.call_count, 3)
                conn.close.assert_called_once_with()


@unittest.skipUnless(os.environ.get("DESK_TEST_NATIVE_RECALL") == "1",
                     "显式开启时使用本机 Codex，仅操作临时会话，不请求模型")
class NativeLegacyRecall(unittest.TestCase):
    def test_真实旧会话只撤掉最后一轮(self):
        import uuid
        from pathlib import Path
        with tempfile.TemporaryDirectory(prefix="desk-recall-probe-") as tmp:
            tid, kept, recalled = [str(uuid.uuid4()) for _ in range(3)]
            stamp = "2026-09-05T00:00:00.000Z"
            rows = [{"timestamp": stamp, "type": "session_meta", "payload": {
                "id": tid, "session_id": tid, "timestamp": stamp, "cwd": tmp,
                "originator": "desktron", "cli_version": "0.150.1",
                "source": "cli", "model_provider": "openai", "history_mode": "legacy",
                "base_instructions": {"text": "Isolated recall test."}}}]
            for turn_id, text in [(kept, "keep this turn"), (recalled, "recall this turn")]:
                rows.extend([
                    {"timestamp": stamp, "type": "event_msg", "payload": {
                        "type": "task_started", "turn_id": turn_id}},
                    {"timestamp": stamp, "type": "turn_context", "payload": {
                        "turn_id": turn_id, "cwd": tmp, "approval_policy": "never",
                        "sandbox_policy": {"type": "read-only"}, "model": "gpt-5.5",
                        "summary": "auto"}},
                    {"timestamp": stamp, "type": "event_msg", "payload": {
                        "type": "user_message", "message": text, "images": [],
                        "local_images": []}},
                    {"timestamp": stamp, "type": "response_item", "payload": {
                        "type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": text}]}},
                    {"timestamp": stamp, "type": "event_msg", "payload": {
                        "type": "turn_aborted", "turn_id": turn_id}},
                ])
            folder = Path(tmp) / "sessions" / "2026" / "09" / "05"
            folder.mkdir(parents=True)
            path = folder / f"rollout-2026-09-05T00-00-00-{tid}.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            env = dict(os.environ, CODEX_HOME=tmp)
            result = app.revert_thread("isolated-legacy", tid, recalled, cwd=tmp, env=env)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["method"], "thread/rollback")
            self.assertTrue(result["verified"])
            again = app.revert_thread("isolated-legacy", tid, recalled, cwd=tmp, env=env,
                                      expected=result["boundary"])
            self.assertTrue(again["ok"], again)
            self.assertEqual(again["method"], "already-reverted")
            conn = app._Connection(cwd=tmp, env=env)
            try:
                conn.initialize(overall=10)
                loaded = conn.request("thread/read", {"threadId": tid, "includeTurns": True}, overall=15)
                turns = loaded["thread"]["turns"]
                self.assertEqual(len(turns), 1)
                self.assertIn("keep this turn", json.dumps(turns))
                self.assertNotIn("recall this turn", json.dumps(turns))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
