"""Native retry projection does not own dispatch, compaction or terminal state."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from test_engine_adapters_gui import ea, Recorder, ROOT
from engine_adapters.lifecycle import retry_clear, retry_update, commit_request_failure


class NativeRetryProjection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make(self, cls):
        bus = Recorder()
        ad = cls(self.tmp.name, self.tmp.name, bus)
        ctx = {"sid": "adp_projection", "msg_id": "msg_projection", "seq": 42,
               "parts": {}, "order": [], "t0": int(time.time() * 1000)}
        ad._live[ctx["sid"]] = ctx
        return ad, ctx, bus

    def start(self, ad, ctx):
        if ad.name == "codex":
            ad._app_event("error", {
                "willRetry": True, "error": {"message": "opaque diagnostic"}}, ctx)
        elif ad.name == "claude":
            ad.handle_event({"type": "system", "subtype": "api_retry",
                "attempt": 2, "max_retries": 4, "retry_delay_ms": 5000,
                "error": "opaque diagnostic", "error_status": 503}, ctx)
        else:
            ad.handle_event({"type": "auto_retry_start", "attempt": 2,
                "maxAttempts": 4, "delayMs": 5000,
                "errorMessage": "opaque diagnostic"}, ctx)

    def test_four_engines_share_event_state_and_checkpoint(self):
        for cls in (ea.PiAdapter, ea.ClaudeAdapter, ea.CodexAdapter):
            with self.subTest(engine=cls.name):
                ad, ctx, bus = self.make(cls)
                self.start(ad, ctx)
                event = [e for e in bus.events if e["type"] == "session.retry"][-1]["properties"]
                state = ad.turn_state(ctx["sid"])
                live = ad.live_message(ctx["sid"])
                disk = ad._read_live_checkpoint(ctx["sid"])
                self.assertTrue(event["active"])
                self.assertEqual(event["generation"], 42)
                self.assertEqual(event["messageID"], ctx["msg_id"])
                for snap in (state, live["info"], disk["info"]):
                    self.assertEqual(snap["retry"]["source"], "native")
                    self.assertEqual(snap["retry"]["signal"], event["signal"])
                self.assertEqual(state["state"], "running")
                self.assertNotIn("engine_error", ctx)
                self.assertFalse({"session.error", "session.idle", "session.state"}
                                 & {e["type"] for e in bus.events})

    def test_clear_updates_disk_without_finishing_or_erasing_failure(self):
        ad, ctx, bus = self.make(ea.PiAdapter)
        self.start(ad, ctx)
        ctx["engine_failure"] = {"code": "PERMISSION_EXTENSION_ERROR"}
        ad.handle_event({"type": "auto_retry_end", "success": False,
                         "finalError": "completed 成功 取消"}, ctx)
        self.assertNotIn("retry", ad.turn_state(ctx["sid"]))
        self.assertNotIn("retry", ad._read_live_checkpoint(ctx["sid"])["info"])
        self.assertEqual(ctx["engine_failure"]["code"], "PERMISSION_EXTENSION_ERROR")
        self.assertNotIn("_close_stdin", ctx)
        self.assertIs(bus.events[-1]["properties"]["active"], False)

    def test_codex_recovery_clears_retry_checkpoint(self):
        for cls in (ea.CodexAdapter,):
            ad, ctx, bus = self.make(cls)
            self.start(ad, ctx)
            ad._stream_recovered(ctx)
            self.assertNotIn("retry", ad._read_live_checkpoint(ctx["sid"])["info"])
            self.assertFalse([e for e in bus.events if e["type"] == "session.retry"][-1]
                             ["properties"]["active"])

    def test_foreign_or_closed_context_cannot_publish_or_overwrite_checkpoint(self):
        for closed in (False, True):
            ad, ctx, bus = self.make(ea.PiAdapter)
            old = dict(ctx, msg_id="old", seq=41, _lifecycle_closed=closed)
            if closed:
                ad._live.clear()
            self.assertFalse(retry_update(ad, old, {"attempt": 8}))
            self.assertFalse(retry_clear(ad, old))
            self.assertFalse(bus.events)
            self.assertIsNone(ad._read_live_checkpoint(ctx["sid"]))

    def test_retry_payload_is_copied_and_text_never_decides_lifecycle(self):
        ad, ctx, bus = self.make(ea.PiAdapter)
        payload = {"attempt": 1, "error": "failed completed aborted 已完成 已失败"}
        retry_update(ad, ctx, payload)
        payload["attempt"] = 99
        self.assertEqual(ctx["retry"]["attempt"], 1)
        self.assertNotIn("engine_error", ctx)
        self.assertNotIn("_close_stdin", ctx)

    def test_frontend_clear_is_not_a_terminal_event(self):
        source = (Path(ROOT) / "frontend/app-chat-model.js").read_text(encoding='utf-8')
        core = (Path(ROOT) / "frontend/app-chat-core.js").read_text(encoding='utf-8')
        fence = "function hsStateGeneration" + core.split("function hsStateGeneration", 1)[1].split(
            "\nfunction hsTurnSettled", 1)[0]
        fn = source.split("function hsRetrySync(v, p) {", 1)[1].split(
            "\nfunction hsStatusPaint", 1)[0]
        script = "const assert=require('node:assert/strict'); let painted=0;" + fence + \
            "function hsStatusPaint(){painted++}; function hsRetrySync(v,p){" + fn + \
            "const v={retry:{n:2},busy:true,turnGeneration:42};" + \
            "hsRetrySync(v,{active:false,generation:41});assert.equal(v.retry.n,2);" + \
            "hsRetrySync(v,{active:false,generation:42});" + \
            "assert.equal(v.retry,null);assert.equal(v.busy,true);assert.equal(painted,1);"
        subprocess.run(["node", "-e", script], check=True, capture_output=True, timeout=10)


class NativeRequestBoundary(unittest.TestCase):
    setUp = NativeRetryProjection.setUp
    make = NativeRetryProjection.make
    start = NativeRetryProjection.start
    def pi_error(self, ad, ctx, code="NETWORK"):
        ad.handle_event({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "error", "errorMessage": "opaque failure"}}, ctx)

    def test_pi_complete_retry_sequence_never_publishes_premature_failure(self):
        ad, ctx, bus = self.make(ea.PiAdapter)
        self.pi_error(ad, ctx)
        self.start(ad, ctx)
        ad.handle_event({"type": "agent_end", "willRetry": True}, ctx)
        self.assertNotIn("engine_error", ctx)
        self.assertNotIn("_close_stdin", ctx)
        ad.handle_event({"type": "message_start", "message": {"role": "assistant"}}, ctx)
        ad.handle_event({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "stop", "content": []}}, ctx)
        ad.handle_event({"type": "auto_retry_end", "success": True}, ctx)
        ad.handle_event({"type": "agent_settled"}, ctx)
        self.assertTrue(ctx["_close_stdin"])
        self.assertNotIn("engine_error", ctx)
        self.assertNotIn("_request_failure", ctx)
        self.assertFalse([e for e in bus.events if e["type"] == "session.error"
            or (e["type"] == "session.environment" and e["properties"].get("state") == "error")])

    def test_pi_retry_exhaustion_commits_only_at_settled(self):
        ad, ctx, bus = self.make(ea.PiAdapter)
        self.pi_error(ad, ctx)
        self.start(ad, ctx)
        ad.handle_event({"type": "auto_retry_end", "success": False,
                         "finalError": "request failed"}, ctx)
        self.assertNotIn("engine_error", ctx)
        ad.handle_event({"type": "agent_settled"}, ctx)
        self.assertEqual(ctx["engine_failure"]["code"], "UNKNOWN")
        self.assertTrue(ctx["_close_stdin"])

    def test_pi_later_error_replaces_candidate_without_inheriting_provider_code(self):
        ad, ctx, bus = self.make(ea.PiAdapter)
        self.pi_error(ad, ctx, "AUTH")
        ad.handle_event({"type": "message_start", "message": {"role": "assistant"}}, ctx)
        ad.handle_event({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "error", "errorMessage": "different"}}, ctx)
        ad.handle_event({"type": "agent_settled"}, ctx)
        self.assertEqual(ctx["engine_failure"]["code"], "UNKNOWN")
        self.assertEqual(ctx["engine_error"], "different")

    def test_abort_discards_candidate_but_safety_failure_survives_recovery(self):
        ad, ctx, bus = self.make(ea.PiAdapter)
        self.pi_error(ad, ctx)
        ctx["aborted"] = "interrupt"
        ad.handle_event({"type": "agent_settled"}, ctx)
        self.assertNotIn("engine_error", ctx)
        for failure in ({"source": "permission", "code": "APPROVAL_DENIED"},
                        {"source": "engine", "code": "PERMISSION_EXTENSION_ERROR"},
                        {"source": "provider", "code": "PROTOCOL"}):
            ad, ctx, bus = self.make(ea.PiAdapter)
            self.pi_error(ad, ctx)
            ctx.update(engine_failure=dict(failure, message="protected"), engine_error="protected")
            ad._pi_recovered(ctx)
            ad.handle_event({"type": "agent_settled"}, ctx)
            self.assertEqual(ctx["engine_error"], "protected")
            self.assertEqual(ctx["engine_failure"]["code"], failure["code"])

    def test_rejected_prompt_remains_immediate_failure(self):
        ad, ctx, bus = self.make(ea.PiAdapter)
        ad.handle_event({"type": "response", "command": "prompt", "success": False,
                         "error": "invalid request"}, ctx)
        self.assertEqual(ctx["engine_error"], "invalid request")
        self.assertTrue(ctx["_close_stdin"])

    def test_claude_synthetic_failure_retry_and_main_result(self):
        for failed in (False, True):
            ad, ctx, bus = self.make(ea.ClaudeAdapter)
            ad.handle_event({"type": "assistant", "isApiErrorMessage": True,
                "apiErrorStatus": 503, "message": {"content": [
                    {"type": "text", "text": "temporary"}]}}, ctx)
            self.start(ad, ctx)
            self.assertNotIn("engine_error", ctx)
            self.assertFalse([e for e in bus.events if e["type"] == "session.error"])
            if not failed:
                ad.handle_event({"type": "assistant", "message": {"content": []}}, ctx)
            ad.handle_event({"type": "result", "is_error": failed, "result": "result"}, ctx)
            self.assertEqual(bool(ctx.get("engine_error")), failed)
            self.assertNotIn("_request_failure", ctx)
            self.assertTrue(ctx["_close_stdin"])

    def test_late_failure_events_do_not_poison_new_or_closed_turn(self):
        for cls in (ea.PiAdapter, ea.ClaudeAdapter, ea.CodexAdapter):
            ad, ctx, bus = self.make(cls)
            for closed in (False, True):
                old = dict(ctx, seq=41, _lifecycle_closed=closed)
                if closed:
                    ad._live.clear()
                if cls.name == "codex":
                    ad._app_event("error", {"error": {"message": "late"}}, old)
                else:
                    ad.handle_event({"type": "message_end" if cls.name == "pi" else "assistant",
                        "isApiErrorMessage": True, "message": {
                            "role": "assistant", "stopReason": "error", "errorMessage": "late"}}, old)
                self.assertNotIn("_request_failure", old)
                self.assertNotIn("engine_error", old)
            self.assertFalse(bus.events)

    def test_desk_closure_keeps_process_error_and_commits_only_unresolved_candidate(self):
        ad, ctx, bus = self.make(ea.PiAdapter)
        self.pi_error(ad, ctx)
        commit_request_failure(ctx)
        self.assertEqual(ctx["engine_failure"]["code"], "UNKNOWN")
        ad, ctx, bus = self.make(ea.PiAdapter)
        self.pi_error(ad, ctx)
        ctx.update(engine_error="pipe closed", engine_failure={"source": "process", "code": "EXIT"})
        self.assertIsNone(commit_request_failure(ctx))
        self.assertEqual(ctx["engine_error"], "pipe closed")

    def test_shepherd_and_trial_workers_share_all_three_retry_snapshots(self):
        from engine_adapters.shepherd import ShepherdAdapter, ShepherdTrialAdapter
        for cls in (ShepherdAdapter, ShepherdTrialAdapter):
            ad, ctx, bus = self.make(cls)
            for engine in ("pi", "claude", "codex"):
                with self.subTest(mode=cls.__name__, engine=engine):
                    worker = ad._worker(engine)
                    self.start(worker, ctx)
                    self.assertIn("retry", ad.turn_state(ctx["sid"]))
                    self.assertIn("retry", ad.live_message(ctx["sid"])["info"])
                    self.assertIn("retry", ad._read_live_checkpoint(ctx["sid"])["info"])
                    retry_clear(worker, ctx)
                    self.assertNotIn("retry", ad.live_message(ctx["sid"])["info"])

    def test_pi_settled_failure_persists_structured_error_once(self):
        ad, ctx, bus = self.make(ea.PiAdapter)
        sid = ad.create_session()["id"]
        ad._live.clear()
        ctx["sid"] = sid
        ad._live[sid] = ctx
        self.pi_error(ad, ctx, "AUTH")
        self.assertFalse(ad.messages(sid))
        ad.handle_event({"type": "agent_settled"}, ctx)
        ad._finish_turn(sid, ctx, None)
        saved = ad.messages(sid)[-1]["info"]
        self.assertTrue(saved["failed"])
        self.assertEqual(saved["error"]["code"], "UNKNOWN")
        self.assertEqual(len([e for e in bus.events if e["type"] == "session.error"]), 1)
        self.assertEqual(ad.turn_state(sid)["state"], "ready")

    def test_child_api_failure_does_not_become_parent_candidate(self):
        ad, ctx, bus = self.make(ea.ClaudeAdapter)
        ad.handle_event({"type": "assistant", "parent_tool_use_id": "child",
            "isApiErrorMessage": True, "message": {"content": [
                {"type": "text", "text": "child failure"}]}}, ctx)
        self.assertNotIn("_request_failure", ctx)
        self.assertNotIn("engine_error", ctx)
