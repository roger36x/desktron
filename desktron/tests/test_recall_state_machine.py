"""只操作临时索引和模拟协议；不调用模型，不修改用户会话。"""
import tempfile
import threading
import unittest
from unittest import mock

from test_codex_recall import ea, Bus, app
from test_codex_appserver import FakeConnection
from recall_protocol import boundary, verified_revert


class History:
    def __init__(self, ids=None):
        self.ids = list(ids if ids is not None else ["keep", "recall"])
        self.writes = []
        self.fail_after_write = False
        self.ignore_write = False

    def request(self, method, params):
        if method == "thread/read":
            return {"thread": {"id": "thread", "turns": [{"id": i} for i in self.ids]}}
        self.writes.append(method)
        if not self.ignore_write:
            self.ids.pop()
        if self.fail_after_write:
            raise TimeoutError("response lost")
        return {}


class Protocol(unittest.TestCase):
    def test_paginated_history_uses_all_pages_before_and_after_delete(self):
        history = History(["keep", "recall"])
        def request(method, params):
            if method == "thread/read":
                return {"thread": {"id": "thread", "historyMode": "paginated", "turns": []}}
            if method == "thread/turns/list":
                self.assertEqual(params["sortDirection"], "asc")
                offset = int(params.get("cursor", "0"))
                return {"data": [{"id": history.ids[offset]}],
                        "nextCursor": str(offset + 1) if offset + 1 < len(history.ids) else None}
            return history.request(method, params)
        result = verified_revert(request, "thread", "recall", "revert")
        self.assertTrue(result["verified"])
        self.assertEqual(history.ids, ["keep"])

    def test_broken_pagination_never_writes(self):
        for page in ({}, {"data": [], "nextCursor": "loop"},
                     {"data": [{"id": "recall"}], "nextCursor": "loop"}):
            with self.subTest(page=page):
                writes = []
                def request(method, params):
                    if method == "thread/read":
                        return {"thread": {"id": "thread", "historyMode": "paginated", "turns": []}}
                    if method == "thread/turns/list":
                        return page
                    writes.append(method)
                with self.assertRaises(RuntimeError):
                    verified_revert(request, "thread", "recall", "revert")
                self.assertEqual(writes, [])

    def test_receipt_loss_retry_observes_without_second_delete(self):
        for method in ("rollback", "revert"):
            with self.subTest(method=method):
                history, persisted = History(), {}
                history.fail_after_write = True
                with self.assertRaises(TimeoutError):
                    verified_revert(history.request, "thread", "recall", method,
                                    checkpoint=lambda **p: persisted.update(p))
                self.assertEqual(persisted["phase"], "applying")
                out = verified_revert(history.request, "thread", "recall", method,
                                      expected=persisted["expected"])
                self.assertTrue(out["ok"])
                self.assertEqual(history.ids, ["keep"])
                self.assertEqual(len(history.writes), 1)

    def test_unknown_target_or_later_turn_never_deletes(self):
        for ids in (["keep"], ["keep", "recall", "new"], []):
            with self.subTest(ids=ids):
                history = History(ids)
                out = verified_revert(history.request, "thread", "recall", "rollback")
                self.assertTrue(out["conflict"])
                self.assertFalse(history.writes)

    def test_prefix_mismatch_never_deletes(self):
        history = History(["changed", "recall"])
        out = verified_revert(history.request, "thread", "recall", "rollback",
                              expected=boundary(["keep"]))
        self.assertTrue(out["conflict"])
        self.assertFalse(history.writes)

    def test_success_response_without_history_change_is_not_success(self):
        history = History()
        history.ignore_write = True
        out = verified_revert(history.request, "thread", "recall", "revert")
        self.assertFalse(out["ok"])
        self.assertTrue(out["conflict"])

    def test_incomplete_or_running_history_never_deletes(self):
        cases = [{}, {"thread": {"id": "wrong", "turns": []}},
                 {"thread": {"id": "thread", "turns": [{"id": "recall", "status": "inProgress"}]}},
                 {"thread": {"id": "thread", "turns": [{"id": "recall"}], "hasMore": True}},
                 {"thread": {"id": "thread", "turns": [{"id": "recall"}, {"id": "recall"}]}}]
        for case in cases:
            with self.subTest(case=case):
                request = mock.Mock(return_value=case)
                with self.assertRaises(RuntimeError):
                    verified_revert(request, "thread", "recall", "rollback")
                self.assertEqual(request.call_count, 1)

    def test_superseded_operation_cannot_write(self):
        history = History()
        with self.assertRaises(RuntimeError):
            verified_revert(history.request, "thread", "recall", "rollback",
                            checkpoint=lambda **p: False)
        self.assertFalse(history.writes)


class StateMachine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.CodexAdapter(self.tmp.name, self.tmp.name, Bus())
        self.sid = self.ad.create_session()["id"]
        self.ctx = {"sid": self.sid, "seq": 1, "esid": "thread", "engine_turn_id": "recall"}

    def tearDown(self):
        app._drop_all_sessions()
        self.tmp.cleanup()

    def test_restart_after_native_write_before_receipt_is_idempotent(self):
        history = History()
        history.fail_after_write = True
        operation_id = self.ad._recall_begin(self.ctx)
        def native(sid, op, env, checkpoint):
            return verified_revert(history.request, "thread", "recall", "rollback",
                                   op.get("expected"), checkpoint)
        with mock.patch.object(self.ad, "_recall_native", side_effect=native):
            self.assertFalse(self.ad._apply_recall_revert(self.sid, {})["ok"])
        restored = ea.CodexAdapter(self.tmp.name, self.tmp.name, Bus())
        self.assertEqual(restored._recall_load(self.sid)["id"], operation_id)
        with mock.patch.object(restored, "_recall_native", side_effect=native):
            self.assertTrue(restored._apply_recall_revert(self.sid, {})["ok"])
            self.assertTrue(restored._apply_recall_revert(self.sid, {})["ok"])
        self.assertEqual(len(history.writes), 1)
        self.assertEqual(restored.turn_state(self.sid)["recall"]["phase"], "done")

    def test_new_recall_cannot_replace_unresolved_operation(self):
        first = self.ad._recall_begin(self.ctx)
        self.assertIsNone(self.ad._recall_begin(dict(sid=self.sid, seq=2)))
        self.assertEqual(self.ad._recall_load(self.sid)["id"], first)
        self.assertFalse(self.ad._recall_update(self.sid, "late-wrong-id", phase="done"))

    def test_transition_and_native_target_are_guarded(self):
        operation_id = self.ad._recall_begin(self.ctx)
        with self.assertRaises(ValueError):
            self.ad._recall_update(self.sid, operation_id, phase="verifying")
        with self.assertRaises(ValueError):
            self.ad._recall_update(self.sid, operation_id, before_turn_id="different")

    def test_missing_turn_id_after_dispatch_stays_blocked(self):
        ctx = {"sid": self.sid, "esid": "thread", "recall_dispatched": True}
        self.ad._recall_begin(ctx)
        with mock.patch.object(app, "_Connection") as conn:
            self.assertFalse(self.ad._apply_recall_revert(self.sid, {})["ok"])
            conn.assert_not_called()
        self.assertEqual(self.ad._recall_load(self.sid)["phase"], "blocked")

    def test_not_sent_needs_no_engine_mutation(self):
        self.ad._recall_begin({"sid": self.sid})
        with mock.patch.object(self.ad, "_recall_native") as native:
            self.assertTrue(self.ad._apply_recall_revert(self.sid, {})["ok"])
            native.assert_not_called()
        self.assertEqual(self.ad._recall_load(self.sid)["receipt"]["method"], "not-dispatched")

    def test_restart_repairs_projection_without_removing_new_message(self):
        def message(mid, at):
            return {"info": {"id": mid, "at": at, "role": "user", "source": self.ad.SRC_USER},
                    "parts": [{"type": "text", "text": "same text"}]}
        self.ad._append_msg(self.sid, message("keep", 10))
        self.ad._append_msg(self.sid, message("recalled", 20))
        self.ad._recall_begin({"sid": self.sid, "t0": 20})
        end = self.ad._recall_load(self.sid)["projection"]["until"]
        self.ad._append_msg(self.sid, message("new", end + 1))
        restored = ea.CodexAdapter(self.tmp.name, self.tmp.name, Bus())
        self.assertTrue(restored._apply_recall_revert(self.sid, {})["ok"])
        self.assertEqual([m["info"]["id"] for m in restored.messages(self.sid)], ["keep", "new"])
        self.assertEqual(restored.index[self.sid]["recall_cuts"], [[20, end]])

    def test_late_callback_cannot_reopen_completed_transaction(self):
        operation_id = self.ad._recall_begin(self.ctx)
        self.ad._recall_update(self.sid, operation_id, phase="done")
        self.ad._remember_recall_revert(self.ctx)
        self.assertEqual(self.ad._recall_load(self.sid)["phase"], "done")

    def test_recall_capability_is_not_enabled_for_claude_or_pi(self):
        from engine_adapters.recall import RecallMixin
        self.assertFalse(issubclass(ea.ClaudeAdapter, RecallMixin))
        self.assertFalse(issubclass(ea.PiAdapter, RecallMixin))

    def test_two_esc_requests_only_pop_once(self):
        self.ad._live[self.sid] = dict(self.ctx, parts={}, order={})
        with mock.patch.object(self.ad, "_soft_interrupt", return_value=True), \
                mock.patch.object(threading, "Timer"), \
                mock.patch.object(self.ad, "_pop_last_user_record", return_value={"at": 100, "text": "draft"}) as pop:
            first = self.ad.abort(self.sid, recall=True)
            again = self.ad.abort(self.sid, recall=True)
        self.assertEqual(first["text"], again["text"])
        pop.assert_called_once()

    def test_concurrent_apply_is_rejected_without_entering_native(self):
        self.ad._recall_begin(self.ctx)
        def native(*args):
            nested = self.ad._apply_recall_revert(self.sid, {})
            self.assertFalse(nested["ok"])
            return {"ok": True, "verified": True}
        with mock.patch.object(self.ad, "_recall_native", side_effect=native) as call:
            self.assertTrue(self.ad._apply_recall_revert(self.sid, {})["ok"])
            call.assert_called_once()

    def test_uncertain_recall_blocks_next_dispatch(self):
        for cls in (ea.CodexAdapter,):
            with self.subTest(adapter=cls.__name__):
                ad = cls(self.tmp.name, self.tmp.name, Bus())
                sid = ad.create_session()["id"]
                ad._recall_begin({"sid": sid, "recall_dispatched": True})
                with mock.patch.object(app, "run_turn") as run:
                    with mock.patch.object(ad, "_emit_text") as emit:
                        error = ad._turn_dispatch({"sid": sid, "saved": [], "opts": {}}, [], {}, None)
                        emit.assert_called_once()
                        self.assertEqual(emit.call_args.args[1], error)
                self.assertTrue(error)
                run.assert_not_called()

    def recall_with_message(self):
        self.ad._append_msg(self.sid, {"info": {"id": "user-recall", "at": 20,
            "role": "user", "source": self.ad.SRC_USER},
            "parts": [{"type": "text", "text": "original request"}]})
        oid = self.ad._recall_begin(self.ctx)
        self.ad._pop_last_user_record(self.sid)
        self.ad.index[self.sid]["recall_cuts"] = [[10, 11], [20, 30]]
        return oid

    def test_keep_restores_message_once_and_only_removes_its_cut(self):
        oid = self.recall_with_message()
        for _ in range(2):
            result = self.ad.resolve_recall(self.sid, oid, "keep")
            self.assertTrue(result["ok"], result)
        self.assertEqual(self.ad.index[self.sid]["recall_cuts"], [[10, 11]])
        self.assertEqual([m["info"]["id"] for m in self.ad.messages(self.sid)], ["user-recall"])
        self.assertEqual(self.ad._recall_load(self.sid)["receipt"]["method"], "kept-interrupted")
        self.assertIsNone(self.ad.recall_block(self.sid))

    def test_keep_cannot_override_possible_native_write(self):
        oid = self.recall_with_message()
        self.ad._recall_update(self.sid, oid, phase="checking", expected=boundary(["keep"]))
        self.assertNotIn("keep", [a["id"] for a in self.ad.recall_actions(self.sid)])
        self.assertFalse(self.ad.resolve_recall(self.sid, oid, "keep")["ok"])

    def test_recovery_actions_share_turn_lock_and_reject_stale_operation(self):
        oid = self.recall_with_message()
        self.assertFalse(self.ad.resolve_recall(self.sid, "stale", "keep")["ok"])
        guard = self.ad._turn_locks.setdefault(self.sid, threading.Lock())
        with guard:
            self.assertFalse(self.ad.resolve_recall(self.sid, oid, "keep")["ok"])
        self.assertIsNotNone(self.ad.recall_block(self.sid))

    def test_pending_recall_rejects_prompt_before_work_starts(self):
        self.recall_with_message()
        with mock.patch.object(threading.Thread, "start") as start:
            with self.assertRaises(RuntimeError):
                self.ad.prompt_async(self.sid, "same request")
            start.assert_not_called()

    def test_retry_can_complete_without_submitting_a_model_turn(self):
        oid = self.recall_with_message()
        with mock.patch("engine_adapters.recap._fork_engine_env", return_value={}), \
                mock.patch.object(self.ad, "_recall_native", return_value={"ok": True}), \
                mock.patch.object(app, "run_turn") as run:
            self.assertTrue(self.ad.resolve_recall(self.sid, oid, "retry")["ok"])
            run.assert_not_called()

    def test_auto_retries_transient_failure_then_completes(self):
        oid = self.recall_with_message()
        with mock.patch("engine_adapters.recap._fork_engine_env", return_value={}), \
                mock.patch.object(self.ad, "_recall_native", side_effect=[
                    {"ok": False, "error": "temporary disconnect"},
                    {"ok": True, "verified": True}]), \
                mock.patch("engine_adapters.recall.time.sleep") as sleep:
            result = self.ad.resolve_recall(self.sid, oid, "auto")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.ad._recall_load(self.sid)["phase"], "done")
        sleep.assert_called_once()

    def test_auto_falls_back_to_verified_copy_after_retries(self):
        oid = self.recall_with_message()
        recovered = {"ok": True, "session_id": "copy-session"}
        with mock.patch("engine_adapters.recap._fork_engine_env", return_value={}), \
                mock.patch.object(self.ad, "_recall_native",
                                  return_value={"ok": False, "error": "offline"}), \
                mock.patch.object(self.ad, "_recall_recover_locked",
                                  return_value=recovered) as recover, \
                mock.patch("engine_adapters.recall.time.sleep"):
            result = self.ad.resolve_recall(self.sid, oid, "auto")
        self.assertEqual(result["session_id"], "copy-session")
        self.assertEqual(self.ad._recall_load(self.sid)["attempts"],
                         self.ad.AUTO_RECALL_ATTEMPTS)
        recover.assert_called_once()

    def test_auto_conflict_skips_more_writes_and_uses_copy(self):
        oid = self.recall_with_message()
        with mock.patch("engine_adapters.recap._fork_engine_env", return_value={}), \
                mock.patch.object(self.ad, "_recall_native",
                                  return_value={"ok": False, "conflict": True,
                                                "error": "history changed"}) as native, \
                mock.patch.object(self.ad, "_recall_recover_locked",
                                  return_value={"ok": True, "session_id": "copy"}) as recover:
            result = self.ad.resolve_recall(self.sid, oid, "auto")
        self.assertEqual(result["session_id"], "copy")
        native.assert_called_once()
        recover.assert_called_once()

    def test_adapter_start_resumes_pending_recall_without_page(self):
        oid = self.recall_with_message()
        started = []
        class ImmediateThread:
            def __init__(self, target, args=(), **_):
                self.target, self.args = target, args
            def start(self):
                started.append((self.target, self.args))
        with mock.patch("engine_adapters.recall.threading.Thread", ImmediateThread):
            self.assertEqual(self.ad.resume_pending_recalls(), 1)
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0][1], ())
        self.assertIn((self.sid, oid), self.ad._recall_workers)

    def test_adapter_start_does_not_loop_after_auto_failure(self):
        oid = self.recall_with_message()
        self.ad._recall_update(self.sid, oid, auto_failed=True)
        with mock.patch("engine_adapters.recall.threading.Thread") as thread:
            self.assertEqual(self.ad.resume_pending_recalls(), 0)
        thread.assert_not_called()

    def test_turn_completion_schedules_recall_after_turn_lock_release(self):
        seen = []
        self.ad._recall_begin(self.ctx)
        def run(*_):
            return None
        def resume(sid):
            guard = self.ad._turn_locks[sid]
            seen.append((sid, guard.acquire(blocking=False)))
            if seen[-1][1]:
                guard.release()
        with mock.patch.object(self.ad, "_run", side_effect=run), \
                mock.patch.object(self.ad, "resume_pending_recall", side_effect=resume):
            self.ad._run_serial(self.sid, "message", {}, [])
        self.assertEqual(seen, [(self.sid, True)])

    def test_recovery_reuses_session_after_restart_and_preserves_domain_options(self):
        oid = self.recall_with_message()
        self.ad.index[self.sid].update(domain="test-domain", opts={"model": "test-model"})
        native = {"ok": True, "thread_id": "recovered-thread", "verified": True}
        with mock.patch("engine_adapters.recap._fork_engine_env", return_value={}), \
                mock.patch("codex_recovery.recover_recalled_thread", return_value=native) as recover, \
                mock.patch.object(self.ad, "import_session", return_value={}):
            self.assertFalse(self.ad.resolve_recall(self.sid, oid, "recover")["ok"])
        nsid = self.ad._recall_load(self.sid)["recovery"]["session_id"]
        self.assertTrue(self.ad.index[nsid]["recovery_pending"])
        restored = ea.CodexAdapter(self.tmp.name, self.tmp.name, Bus())
        with mock.patch("engine_adapters.recap._fork_engine_env", return_value={}), \
                mock.patch("codex_recovery.recover_recalled_thread", return_value=native), \
                mock.patch.object(restored, "import_session", return_value={"id": nsid}):
            result = restored.resolve_recall(self.sid, oid, "recover")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["session_id"], nsid)
        self.assertEqual(restored.index[nsid]["domain"], "test-domain")
        self.assertEqual(restored.index[nsid]["opts"], {"model": "test-model"})
        self.assertNotIn("recovery_pending", restored.index[nsid])
        self.assertIsNotNone(restored.recall_block(self.sid))
        with mock.patch("engine_adapters.recap._fork_engine_env", return_value={}), \
                mock.patch("codex_recovery.recover_recalled_thread") as recover:
            self.assertEqual(restored.resolve_recall(self.sid, oid, "recover")["session_id"], nsid)
            recover.assert_not_called()


class DispatchBoundary(unittest.TestCase):
    def tearDown(self):
        app._drop_all_sessions()

    def test_cancelled_before_dispatch_never_sends_prompt(self):
        with mock.patch.object(app, "_Connection", FakeConnection):
            app.run_turn("/work", {}, "", "must not send", {},
                         on_turn_submitting=lambda tid: False)
        self.assertNotIn("turn/start", [name for name, _ in FakeConnection.last.calls])

    def test_cancelled_during_initialize_does_not_load_mcp_or_start_thread(self):
        cancelled = False
        class SlowInitialize(FakeConnection):
            def initialize(self, **kwargs):
                nonlocal cancelled
                super().initialize(**kwargs)
                cancelled = True
        with mock.patch.object(app, "_Connection", SlowInitialize):
            app.run_turn("/work", {}, "", "must not send", {}, cancelled=lambda: cancelled)
        self.assertEqual([name for name, _ in FakeConnection.last.calls], ["initialize"])
        self.assertEqual(FakeConnection.last.closed, 1)

    def test_interrupt_before_turn_start_response_is_delivered_after_id_arrives(self):
        class CancelDuringStart(FakeConnection):
            def request(self, method, *args, **kwargs):
                if method == "turn/start":
                    self.interrupted = app.interrupt("pending-session")
                return super().request(method, *args, **kwargs)
            def request_nowait(self, method, params):
                self.calls.append((method, params))
        with mock.patch.object(app, "_Connection", CancelDuringStart):
            app.run_turn("/work", {}, "", "cancel", {}, session_id="pending-session")
        self.assertTrue(FakeConnection.last.interrupted)
        calls = [params for method, params in FakeConnection.last.calls if method == "turn/interrupt"]
        self.assertEqual(calls, [{"threadId": "thread-1", "turnId": "turn-1"}])
        self.assertNotIn("pending-session", app._PENDING_INTERRUPT)

    def test_interrupt_is_resent_when_native_turn_becomes_active(self):
        class StartsAfterReply(FakeConnection):
            active = False
            def request_nowait(self, method, params):
                self.calls.append((method, params))
                # 首次中断发生在 start 回执之后、实际激活之前；只有第二次能成功。
                self.stopped = self.active
            def read(self, timeout=30):
                if not self.active:
                    self.active = True
                    return {"method": "turn/started", "params": {"threadId": "thread-1",
                            "turn": {"id": "turn-1", "status": "inProgress"}}}
                if not self.stopped:
                    raise AssertionError("启动激活后没有补发中断")
                return super().read(timeout)
        with mock.patch.object(app, "_Connection", StartsAfterReply):
            app.run_turn("/work", {}, "", "cancel", {}, session_id="start-race",
                         on_turn_started=lambda *_: app.interrupt("start-race"))
        calls = [params for method, params in FakeConnection.last.calls if method == "turn/interrupt"]
        self.assertEqual(calls, [{"threadId": "thread-1", "turnId": "turn-1"}] * 2)
        self.assertFalse(app._SESSIONS["start-race"].interrupt_requested)

    def test_recall_lock_wait_is_bounded(self):
        guard = app._session_lock("locked")
        guard.acquire()
        try:
            with mock.patch.object(app, "_Connection") as conn:
                result = app.revert_thread("locked", "thread", "turn", timeout=0.01)
                self.assertFalse(result["ok"])
                conn.assert_not_called()
        finally:
            guard.release()
