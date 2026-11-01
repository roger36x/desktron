"""Cross-session ask uses a disposable peer sidecar, never a public user turn."""
import os
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
sys.path.insert(0, LIB)

import peer_mailbox
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    from engine_adapters import CliAdapter, EventBus
    from engine_adapters.claude import ClaudeAdapter
    from engine_adapters.codex import CodexAdapter
    from engine_adapters.pi import PiAdapter
    from engine_adapters.shepherd import ShepherdAdapter
finally:
    sys.argv = _argv


class PeerProbe(CliAdapter):
    name = "peer-probe"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.snapshots = []
        self.answer = "共享树目前只读。"

    def recap_generate(self, sid, run, snap):
        self.snapshots.append((sid, dict(snap)))
        return self.answer


class BlockingPeerProbe(PeerProbe):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.peer_started = threading.Event()
        self.user_finished = threading.Event()
        self.user_prompts = []

    def recap_generate(self, sid, run, snap):
        self.snapshots.append((sid, dict(snap)))
        run.mark_prepared()
        self.peer_started.set()
        while not run.cancelled.wait(.01):
            pass
        return ""

    def _run(self, sid, text, opts=None, atts=None):
        self.user_prompts.append(text)
        self.user_finished.set()


class StuckPeerProbe(BlockingPeerProbe):
    """Projection stand-in that cannot observe cancellation until I/O returns."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.release_peer = threading.Event()

    def recap_generate(self, sid, run, snap):
        self.snapshots.append((sid, dict(snap)))
        run.mark_prepared()
        self.peer_started.set()
        self.release_peer.wait(2)
        return "late answer"


class PeerAskIsolation(unittest.TestCase):
    def setUp(self):
        # 用户回合在后台线程里跑；替身 _run 返回后 _run_serial 仍会写回执。记下已有线程，
        # 清理临时目录前等本用例启动的线程收尾，避免与 TemporaryDirectory 清理竞态。
        self._threads_before = set(threading.enumerate())
        self.tmp = tempfile.TemporaryDirectory()
        self.mail = tempfile.TemporaryDirectory()
        self.old_data = os.environ.get("DESKTRON_DATA_HOME")
        os.environ["DESKTRON_DATA_HOME"] = self.mail.name
        self.bus = EventBus()
        self.ad = PeerProbe(self.tmp.name, self.tmp.name, self.bus)
        self.sid = self.ad.create_session(domain="desk-iter")["id"]
        with self.ad._lock:
            self.ad.index[self.sid]["engine_sid"] = "native-existing"
            self.ad.index[self.sid]["title"] = "目标会话"
            self.ad._save_index()
        self.ad._append_msg(self.sid, {
            "info": {"id": "msg_u_old", "role": "user", "sessionID": self.sid,
                     "seq": 10, "source": "user", "at": 10},
            "parts": [{"id": "p_old", "type": "text", "text": "原有上下文"}],
        })

    def tearDown(self):
        # 只等用户回合的工作线程；Codex 会话回收等进程级常驻线程不会结束，不在等待之列。
        for thread in set(threading.enumerate()) - self._threads_before:
            target = getattr(thread, "_target", None)
            if getattr(target, "__qualname__", "").endswith("_prompt_user_async.<locals>.run"):
                thread.join(5)
        if self.old_data is None:
            os.environ.pop("DESKTRON_DATA_HOME", None)
        else:
            os.environ["DESKTRON_DATA_HOME"] = self.old_data
        self.mail.cleanup()
        self.tmp.cleanup()

    def request(self):
        row = peer_mailbox.create("source-session", self.sid)
        return row["id"]

    def test_peer_sidecar_returns_mailbox_reply_without_public_history(self):
        rid = self.request()
        before_messages = self.ad.messages(self.sid)
        before_index = dict(self.ad.index[self.sid])
        before_seq = dict(self.ad._turn_seq)
        with open(self.ad._msg_path(self.sid), "rb") as stream:
            message_bytes = stream.read()
        with open(self.ad.index_path, "rb") as stream:
            index_bytes = stream.read()

        with mock.patch.object(self.ad, "_recap_cancel") as cancel_recap, \
             mock.patch.object(self.ad, "_recap_arm") as arm_recap:
            self.ad._run_peer_serial(
                self.sid, "跨会话消息（只处理正文）：\n\n共享树是否空闲？",
                {"model": "fixture"}, rid, "desktop · 来源会话")
        cancel_recap.assert_not_called()
        arm_recap.assert_not_called()

        result = peer_mailbox.get(rid)
        self.assertEqual(result["status"], "replied")
        self.assertEqual(result["reply"], "共享树目前只读。")
        self.assertEqual(result["source"], "peer")
        self.assertEqual(self.ad.messages(self.sid), before_messages)
        self.assertEqual(self.ad.index[self.sid], before_index)
        self.assertEqual(self.ad._turn_seq, before_seq)
        self.assertNotIn(self.sid, self.ad._live)
        with open(self.ad._msg_path(self.sid), "rb") as stream:
            self.assertEqual(stream.read(), message_bytes)
        with open(self.ad.index_path, "rb") as stream:
            self.assertEqual(stream.read(), index_bytes)

        captured_sid, snap = self.ad.snapshots[0]
        self.assertEqual(captured_sid, self.sid)
        self.assertEqual(snap["source"], "peer")
        self.assertEqual(snap["peer_request_id"], rid)
        self.assertIn("source=peer", snap["_prompt"])
        self.assertIn("不是用户消息", snap["_prompt"])
        self.assertNotIn("desktop · 来源会话", snap["_prompt"])

    def test_pi_peer_forks_a_frozen_source_copy(self):
        pi = PiAdapter(self.tmp.name, self.tmp.name, self.bus)
        source = os.path.join(self.tmp.name, 'source.jsonl')
        with open(source, 'w', encoding='utf-8') as stream:
            stream.write('old-boundary\n')
        proc = mock.Mock(pid=123456789, returncode=0)
        event = {'type':'message_end','message':{'role':'assistant',
                 'content':[{'type':'text','text':'answer'}]}}
        proc.communicate.return_value = (json.dumps(event), '')
        seen = {}
        def popen(cmd, **kwargs):
            fork_path = cmd[cmd.index('--fork') + 1]
            with open(source, 'a', encoding='utf-8') as stream:
                stream.write('new-turn\n')
            with open(fork_path, encoding='utf-8') as stream:
                seen['source'] = stream.read()
            seen['fork_path'] = fork_path
            return proc
        from engine_adapters.recap import _RecapRun
        run = _RecapRun(self.sid, 1)
        snap = {'engine_sid':'native-existing','source':'peer',
                'opts':{'_grounding':'memory/scripts/mem_read.py'},
                'cwd':self.tmp.name,'_prompt':'question'}
        with mock.patch.object(pi, '_pi_path', return_value=source), \
             mock.patch('engine_adapters.pi.subprocess.Popen', side_effect=popen), \
             mock.patch('engine_adapters.recap._engine_env', return_value={}):
            self.assertEqual(pi.recap_generate(self.sid, run, snap), 'answer')
        self.assertEqual(seen['source'], 'old-boundary\n')
        self.assertNotEqual(seen['fork_path'], source)
        self.assertTrue(run.prepared.is_set())

    def test_pi_generation_timeout_is_not_reported_as_user_preemption(self):
        from engine_adapters.recap import _RecapRun
        pi = PiAdapter(self.tmp.name, self.tmp.name, self.bus)
        proc = mock.Mock(pid=123456789, returncode=-15)
        proc.communicate.side_effect = [subprocess.TimeoutExpired('fixture', 120), ('', '')]
        captured = []
        def generate(sid, run, snap):
            opts = {**(snap.get('opts') or {}), '_grounding':'memory/scripts/mem_read.py'}
            answer = pi.recap_generate(sid, run, {**snap, 'cwd': self.tmp.name, 'opts':opts})
            captured.append(run.reason)
            return answer
        rid = self.request()
        before = self.ad.messages(self.sid)
        with mock.patch.object(self.ad, 'recap_generate', side_effect=generate), \
             mock.patch.object(pi, '_pi_path', return_value='/unused/source.jsonl'), \
             mock.patch('engine_adapters.pi.shutil.copyfile'), \
             mock.patch('engine_adapters.pi.os.chmod'), \
             mock.patch('engine_adapters.pi.subprocess.Popen', return_value=proc), \
             mock.patch('engine_adapters.recap._engine_env', return_value={}), \
             mock.patch.object(_RecapRun, '_stop'):
            self.assertFalse(self.ad._run_peer_serial(self.sid, 'question', {}, rid))
        row = peer_mailbox.get(rid)
        self.assertEqual(captured, ['Pi 支线超时 120s'])
        self.assertEqual(row['error_code'], 'generation_timeout')
        self.assertIn('Pi 支线超时 120s', row['error'])
        self.assertNotIn('新回合', row['error'])
        self.assertEqual(before, self.ad.messages(self.sid))

    def test_generic_expiry_and_user_cancel_have_distinct_first_causes(self):
        for preempt in (False, True):
            with self.subTest(preempt=preempt):
                rid = self.request()
                def generate(sid, run, snap):
                    if preempt:
                        self.ad._peer_cancel(sid, '目标会话发送了新消息，隔离问询已取消')
                    run.expire('超时 120s')
                    return ''
                with mock.patch.object(self.ad, 'recap_generate', side_effect=generate):
                    self.assertFalse(self.ad._run_peer_serial(self.sid, 'question', {}, rid))
                row = peer_mailbox.get(rid)
                self.assertEqual(row['error_code'], 'cancelled' if preempt else 'generation_timeout')
                self.assertIn('发送了新消息' if preempt else '超时 120s', row['error'])

    def test_timeout_first_is_not_relabelled_by_later_shutdown(self):
        rid = self.request()
        def generate(sid, run, snap):
            run.expire('Pi 支线超时 120s')
            self.ad._peer_cancel(sid, 'Desk 正在退出，隔离问询已取消')
            return ''
        with mock.patch.object(self.ad, 'recap_generate', side_effect=generate):
            self.assertFalse(self.ad._run_peer_serial(self.sid, 'question', {}, rid))
        row = peer_mailbox.get(rid)
        self.assertEqual(row['error_code'], 'generation_timeout')
        self.assertIn('Pi 支线超时 120s', row['error'])
        self.assertNotIn('正在退出', row['error'])

    def test_timeout_first_survives_a_later_source_boundary_change(self):
        rid = self.request()
        def generate(sid, run, snap):
            run.expire('Pi 支线超时 120s')
            return ''
        with mock.patch.object(self.ad, 'recap_generate', side_effect=generate), \
             mock.patch.object(self.ad, '_recap_context_ready', side_effect=[True, False]):
            self.assertFalse(self.ad._run_peer_serial(self.sid, 'question', {}, rid))
        row = peer_mailbox.get(rid)
        self.assertEqual(row['error_code'], 'generation_timeout')
        self.assertIn('Pi 支线超时 120s', row['error'])
        self.assertNotIn('上下文已变化', row['error'])

    def test_ownership_loss_is_not_attributed_to_a_user_message(self):
        rid = self.request()
        def generate(sid, run, snap):
            self.ad._peer_runs.pop(sid)
            return 'answer'
        with mock.patch.object(self.ad, 'recap_generate', side_effect=generate):
            self.assertFalse(self.ad._run_peer_serial(self.sid, 'question', {}, rid))
        row = peer_mailbox.get(rid)
        self.assertEqual(row['error_code'], 'ownership_lost')
        self.assertNotIn('新回合', row['error'])

    def test_peer_failure_reason_does_not_cross_session_with_raw_provider_text(self):
        from engine_adapters.recap import _peer_public_error, _peer_public_reason
        self.assertEqual(_peer_public_reason("Pi 服务端错误: token=secret"),
                         "Pi 服务端返回错误")
        self.assertEqual(_peer_public_reason("rc=7 stderr:/private/user/path"),
                         "模型支线退出码 7")
        self.assertEqual(_peer_public_reason("result error api=secret terminal=private"),
                         "模型支线返回失败结果")
        self.assertEqual(_peer_public_error(RuntimeError("token=secret")),
                         "隔离问询执行失败（RuntimeError）")

    def test_completed_reply_detaches_before_mailbox_io_and_cannot_be_cancelled(self):
        rid = self.request()
        peer_mailbox.accept(rid, self.sid)
        original = peer_mailbox.update
        observed = []

        def update(request_id, status, **kwargs):
            if status == "replied":
                observed.append(self.ad.peer_state(self.sid))
                self.ad._peer_user_admit(self.sid, "")
                self.ad._peer_user_release(self.sid)
            return original(request_id, status, **kwargs)

        with mock.patch("peer_mailbox.update", side_effect=update):
            self.assertTrue(self.ad._run_peer_serial(
                self.sid, "question", {}, rid, "desktop · 来源"))
        self.assertFalse(observed[0]["active"])
        self.assertEqual(peer_mailbox.get(rid)["status"], "replied")
        self.assertEqual(peer_mailbox.get(rid)["reply"], "共享树目前只读。")

    def test_peer_work_admission_failure_releases_snapshot_waiters(self):
        rid = self.request()
        peer_mailbox.accept(rid, self.sid)
        seen = []
        original = self.ad._peer_finish
        def finish(sid, run, phase):
            seen.append(run)
            return original(sid, run, phase)
        with mock.patch("self_update.begin_work", return_value=False), \
             mock.patch.object(self.ad, '_peer_finish', side_effect=finish):
            with self.assertRaisesRegex(RuntimeError, "强制更新"):
                self.ad.peer_prompt_async(self.sid, "question", {}, peer_request_id=rid)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].finished.is_set())
        self.assertTrue(seen[0].prepared.is_set())
        self.assertNotIn(self.sid, self.ad._peer_runs)

    def test_peer_thread_start_failure_releases_waiters_even_if_finish_work_fails(self):
        rid = self.request()
        peer_mailbox.accept(rid, self.sid)
        seen = []
        original = self.ad._peer_finish
        def finish(sid, run, phase):
            seen.append(run)
            return original(sid, run, phase)
        with mock.patch("self_update.begin_work", return_value=True), \
             mock.patch("self_update.finish_work", side_effect=RuntimeError('finish failed')), \
             mock.patch("threading.Thread.start", side_effect=RuntimeError('start failed')), \
             mock.patch.object(self.ad, '_peer_finish', side_effect=finish):
            with self.assertRaisesRegex(RuntimeError, "finish failed"):
                self.ad.peer_prompt_async(self.sid, "question", {}, peer_request_id=rid)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].finished.is_set())
        self.assertTrue(seen[0].prepared.is_set())
        self.assertNotIn(self.sid, self.ad._peer_runs)

    def test_peer_sidecar_rejects_unaccepted_mailbox_request(self):
        rid = peer_mailbox.create("source-session", self.sid)["id"]
        with self.assertRaisesRegex(ValueError, "未被目标会话接收"):
            self.ad.peer_prompt_async(self.sid, "question", {}, peer_request_id=rid)
        self.assertNotIn(self.sid, self.ad._peer_runs)

    def test_cancellation_bit_is_set_before_native_stop(self):
        from engine_adapters.recap import _RecapRun
        class Run(_RecapRun):
            def cancel(self):
                self.saw_mark = self.cancelled.is_set()
                super().cancel()

        run = Run(self.sid, 0)
        with self.ad._lock:
            self.ad._peer_runs[self.sid] = run
        self.assertTrue(self.ad._peer_cancel(self.sid))
        self.assertTrue(run.saw_mark)

    def test_user_prompt_runs_after_snapshot_while_peer_finishes_in_background(self):
        ad = StuckPeerProbe(self.tmp.name, self.tmp.name, self.bus)
        sid = ad.create_session()["id"]
        with ad._lock:
            ad.index[sid]["engine_sid"] = "native-existing"
            ad._save_index()
        rid = peer_mailbox.create("source-session", sid)["id"]
        peer_mailbox.accept(rid, sid)
        with mock.patch("self_update.begin_work", return_value=True), \
             mock.patch("self_update.finish_work"):
            ad.peer_prompt_async(sid, "协调问题", {}, peer_request_id=rid,
                                 peer_head="desktop · 来源会话:")
            self.assertTrue(ad.peer_started.wait(5))
            ad.prompt_async(sid, "真实用户继续", {})
            self.assertTrue(ad.user_finished.wait(5))
            self.assertTrue(ad.turn_state(sid)["peer"]["active"])
            self.assertEqual(ad.user_prompts, ["真实用户继续"])
            ad.release_peer.set()
            deadline = time.time() + 2
            while peer_mailbox.get(rid)["status"] != "replied" and time.time() < deadline:
                time.sleep(.01)
        state = peer_mailbox.get(rid)
        self.assertEqual(state["status"], "replied")
        self.assertEqual(state["reply"], "late answer")
        self.assertFalse(ad.turn_state(sid)["peer"]["active"])

    def test_same_target_rejects_second_peer_while_first_runs(self):
        ad = StuckPeerProbe(self.tmp.name, self.tmp.name, self.bus)
        sid = ad.create_session()["id"]
        with ad._lock:
            ad.index[sid]["engine_sid"] = "native-existing"
            ad._save_index()
        first = peer_mailbox.create("source-session", sid)["id"]
        peer_mailbox.accept(first, sid)
        second = peer_mailbox.create("another-source", sid)["id"]
        peer_mailbox.accept(second, sid)
        with mock.patch("self_update.begin_work", return_value=True), \
             mock.patch("self_update.finish_work"):
            ad.peer_prompt_async(sid, "first", {}, peer_request_id=first)
            self.assertTrue(ad.peer_started.wait(5))
            with self.assertRaisesRegex(RuntimeError, "已有协作问询"):
                ad.peer_prompt_async(sid, "second", {}, peer_request_id=second)
            ad.release_peer.set()

    def test_duplicate_user_delivery_returns_receipt_while_first_waits_for_guard(self):
        ad = BlockingPeerProbe(self.tmp.name, self.tmp.name, self.bus)
        sid = ad.create_session()["id"]
        with ad._lock:
            guard = ad._turn_locks.setdefault(sid, threading.Lock())
        with mock.patch("self_update.begin_work", return_value=True), \
             mock.patch("self_update.finish_work"):
            guard.acquire()
            try:
                first = ad.prompt_async(sid, "same", {}, client_request_id="duplicate")
                second = ad.prompt_async(sid, "same", {}, client_request_id="duplicate")
                self.assertEqual(first['request_id'], second['request_id'])
                self.assertEqual(second['status'], 'accepted')
            finally:
                guard.release()
            self.assertTrue(ad.user_finished.wait(5))
        self.assertEqual(ad.user_prompts, ["same"])

    def test_user_fence_blocks_later_ask_until_turn_guard_is_owned(self):
        from engine_adapters.recap import _RecapRun
        ad = BlockingPeerProbe(self.tmp.name, self.tmp.name, self.bus)
        sid = ad.create_session()["id"]
        with ad._lock:
            ad.index[sid]["engine_sid"] = "native-existing"
            guard = ad._turn_locks.setdefault(sid, threading.Lock())
            run = _RecapRun(sid, 1, request_id='pm_' + 'a' * 32)
            run.mark_prepared()
            ad._peer_runs[sid] = run
        second = peer_mailbox.create("another-source", sid)["id"]
        peer_mailbox.accept(second, sid)
        with mock.patch("self_update.begin_work", return_value=True), \
             mock.patch("self_update.finish_work"):
            guard.acquire()
            try:
                ad.prompt_async(sid, "已接收用户回合", {})
                deadline = time.time() + 5
                while not ad._peer_blocks.get(sid) and time.time() < deadline:
                    time.sleep(.01)
                with ad._lock:
                    ad._peer_runs.pop(sid, None)
                run.finished.set()
                with self.assertRaisesRegex(RuntimeError, "正在删除"):
                    ad.peer_prompt_async(sid, "later ask", {}, peer_request_id=second)
            finally:
                guard.release()
            self.assertTrue(ad.user_finished.wait(5))
        self.assertEqual(ad.user_prompts, ["已接收用户回合"])

    def test_delete_fence_rejects_new_user_admission(self):
        ad = PeerProbe(self.tmp.name, self.tmp.name, self.bus)
        sid = ad.create_session()["id"]
        ad._peer_fence_enter(sid)
        try:
            with self.assertRaisesRegex(RuntimeError, "控制操作"):
                ad.prompt_async(sid, "删除期间的新消息", {})
        finally:
            ad._peer_fence_exit(sid)

    def test_generic_delete_fences_late_peer_and_waits_for_real_worker(self):
        ad = StuckPeerProbe(self.tmp.name, self.tmp.name, self.bus)
        sid = ad.create_session()["id"]
        with ad._lock:
            ad.index[sid]["engine_sid"] = "native-existing"
            ad._save_index()
        rid = peer_mailbox.create("source-session", sid)["id"]
        peer_mailbox.accept(rid, sid)
        removed, errors = threading.Event(), []
        with mock.patch("self_update.begin_work", return_value=True), \
             mock.patch("self_update.finish_work"):
            ad.peer_prompt_async(sid, "阻塞投影", {}, peer_request_id=rid)
            self.assertTrue(ad.peer_started.wait(5))
            def remove():
                try:
                    ad.delete_session(sid)
                except Exception as exc:
                    errors.append(exc)
                finally:
                    removed.set()
            threading.Thread(target=remove, daemon=True).start()
            self.assertFalse(removed.wait(.1))
            with ad._lock:
                self.assertGreater(ad._peer_blocks.get(sid, 0), 0)
                self.assertIn(sid, ad.index)
            late = peer_mailbox.create("source-session", sid)["id"]
            peer_mailbox.accept(late, sid)
            with self.assertRaisesRegex(RuntimeError, "正在删除"):
                ad.peer_prompt_async(sid, "late", {}, peer_request_id=late)
            ad.release_peer.set()
            self.assertTrue(removed.wait(1))
        self.assertEqual(errors, [])
        self.assertNotIn(sid, ad.index)

    def test_peer_fence_is_reference_counted_for_concurrent_deletes(self):
        self.ad._peer_fence_enter(self.sid)
        self.ad._peer_fence_enter(self.sid)
        self.ad._peer_fence_exit(self.sid)
        with self.ad._lock:
            self.assertEqual(self.ad._peer_blocks.get(self.sid), 1)
        self.ad._peer_fence_exit(self.sid)
        with self.ad._lock:
            self.assertNotIn(self.sid, self.ad._peer_blocks)

    def test_user_prompt_waits_for_queued_ask_snapshot_not_generation(self):
        ad = StuckPeerProbe(self.tmp.name, self.tmp.name, self.bus)
        sid = ad.create_session()["id"]
        with ad._lock:
            ad.index[sid]["engine_sid"] = "native-existing"
            guard = ad._turn_locks.setdefault(sid, threading.Lock())
            ad._save_index()
        rid = peer_mailbox.create("source-session", sid)["id"]
        peer_mailbox.accept(rid, sid)
        admitted = threading.Event()
        with mock.patch("self_update.begin_work", return_value=True), \
             mock.patch("self_update.finish_work"):
            guard.acquire()
            try:
                ad.peer_prompt_async(sid, "排队协调问题", {}, peer_request_id=rid)
                deadline = time.time() + 5
                while sid not in ad._peer_runs and time.time() < deadline:
                    time.sleep(.01)
                threading.Thread(target=lambda: (
                    ad.prompt_async(sid, "真实用户继续", {}), admitted.set()),
                    daemon=True).start()
                self.assertTrue(admitted.wait(.2))
                self.assertFalse(ad.user_finished.is_set())
            finally:
                guard.release()
            self.assertTrue(ad.peer_started.wait(5))
            self.assertTrue(admitted.wait(1))
            self.assertTrue(ad.user_finished.wait(5))
            self.assertTrue(ad.turn_state(sid)["peer"]["active"])
            ad.release_peer.set()
        self.assertEqual(ad.user_prompts, ["真实用户继续"])

    def test_codex_family_peer_sidecar_requires_readonly_ephemeral_fork(self):
        import codex_appserver
        for cls in (CodexAdapter,):
            with self.subTest(engine=cls.name):
                ad = cls(self.tmp.name, self.tmp.name, self.bus)
                sid = ad.create_session()["id"]
                with ad._lock:
                    ad.index[sid]["engine_sid"] = "native-existing"
                    ad._save_index()
                snap = ad._recap_snapshot(sid, {"seq": 1, "opts": {}, "source": "peer"})
                snap.update(source="peer", _prompt="peer question")
                seen = {}
                with mock.patch.object(codex_appserver, "recap_fork",
                                       side_effect=lambda *a, **kw: seen.update(kw) or "answer"):
                    self.assertEqual(ad.recap_generate(sid, mock.Mock(), snap), "answer")
                self.assertTrue(seen["read_only"])
                self.assertTrue(seen["require_ephemeral"])
                self.assertTrue(seen["no_tools"])

    def test_peer_waits_for_idle_continuation_then_uses_settled_boundary(self):
        rid = self.request()
        with self.ad._lock:
            self.ad._live[self.sid] = {"kind": "idle-continuation"}
        finished = threading.Event()

        def run():
            self.ad._run_peer_serial(self.sid, "question", {}, rid)
            finished.set()

        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(.05)
        self.assertFalse(finished.is_set())
        with self.ad._lock:
            self.ad._live.pop(self.sid, None)
        self.assertTrue(finished.wait(2))
        self.assertEqual(peer_mailbox.get(rid)["status"], "replied")

    def test_all_public_engines_share_peer_sidecar_contract(self):
        for cls in (ClaudeAdapter, CodexAdapter, PiAdapter, ShepherdAdapter):
            with self.subTest(engine=cls.name):
                self.assertTrue(callable(getattr(cls, "peer_prompt_async", None)))
                self.assertIsNotNone(getattr(cls, "recap_generate", None))


if __name__ == "__main__":
    unittest.main()
