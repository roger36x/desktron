"""Esc targets the ephemeral native turn without poisoning the logical session."""
from pathlib import Path
import json
import signal
import tempfile
import threading
import unittest
from unittest import mock

from shepherd.codex_connector import CodexFamilyConnector
from shepherd.harness import ExecutionHarness
from shepherd.runners import CodexFamilyRunner
from shepherd.service import Service
from shepherd.store import Conflict, PromptNotStarted
from shepherd.unified_runners import UnifiedRunner


class UnifiedCancel(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.service = Service(self.root / 'state', storage='unified')
        for target, kwargs in (
            ('shepherd.codex_connector.CodexFamilyConnector.availability',
             {'return_value': {'available': True}}),
            ('shepherd.codex_connector.CodexFamilyConnector._seed_private_config', {}),
            ('shepherd.runners.threading.Timer', {}),
        ):
            patcher = mock.patch(target, **kwargs)
            value = patcher.start()
            self.addCleanup(patcher.stop)
            if target.endswith('.Timer'):
                self.timer = value

    def runner(self, engine):
        if not (self.root / engine).exists():
            (self.root / engine).mkdir()
            self.service.create(engine, engine, self.root / engine)
        active = self.service.active(engine)
        connector = self.service.connector(active['binding'])
        return UnifiedRunner(connector,options={'model':'fixture','_context_window':400000}), active['lease']

    def run_turn(self, runner, lease):
        return ExecutionHarness(self.service.coordinator, runner.connector, runner).run(
            runner.connector.engine, lease_id=lease['lease_id'], generation=lease['generation'],
            prompt='test question', input_public=[{'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': 'test question'}]}])

    def native(self, runner, *, cancel_at='', terminal='interrupted', unrelated=False,
               fail_after_terminal=False, raw_output=True, raw_items=None):
        conn = mock.Mock(proc=None)
        notified = threading.Event()
        conn.request_nowait.side_effect = lambda *args: notified.set()

        def turn(*args, **kwargs):
            self.assertIsNone(args[2])  # Unified execution creates a new native thread.
            kwargs['on_connection'](conn)
            kwargs['on_turn_submitting']('ephemeral-thread')
            if cancel_at == 'waiting_id':
                runner.cancel()
                conn.request_nowait.assert_not_called()
            kwargs['on_turn_started']('ephemeral-thread', 'native-turn')
            if cancel_at == 'running':
                runner.cancel()
            if cancel_at:
                self.assertTrue(notified.wait(2), 'Esc did not reach the native turn')
                conn.request_nowait.assert_called_with('turn/interrupt', {
                    'threadId': 'ephemeral-thread', 'turnId': 'native-turn'})
                self.assertNotEqual(runner.connector.native_session_id, 'ephemeral-thread')
            if terminal == 'completed' and raw_output:
                for item in raw_items or [{'type':'message','role':'assistant',
                        'content':[{'type':'output_text','text':'fixture answer'}]}]:
                    kwargs['on_notification']('rawResponseItem/completed', {'turnId':'native-turn','item':item})
            if terminal:
                kwargs['on_notification']('turn/completed', {'turn': {
                    'id': 'unrelated-turn' if unrelated else 'native-turn', 'status': terminal}})
                if not unrelated:
                    # A later Esc cannot arm a kill timer after an authoritative terminal.
                    self.assertIsNone(runner.controller.connection)
                    self.assertIsNone(runner.controller._cancel_timer)
            if not terminal or fail_after_terminal:
                raise RuntimeError('app-server 已退出')
            return 'ephemeral-thread'
        return turn, conn

    def test_missing_raw_events_cannot_commit_completed_empty_history(self):
        for engine in ('codex',):
            runner, lease = self.runner(engine)
            fake, _ = self.native(runner, terminal='completed', raw_output=False)
            with mock.patch('codex_appserver.run_turn', side_effect=fake):
                with self.assertRaisesRegex(Conflict, '缺少输出归档事件'):
                    self.run_turn(runner, lease)
            self.assertFalse(self.service.coordinator.state(engine)['turns'])

    def test_interrupt_commits_and_next_turn_can_send_for_both_engines(self):
        for engine in ('codex',):
            for cancel_at in ('running', 'waiting_id'):
                with self.subTest(engine=engine, cancel_at=cancel_at):
                    runner, lease = self.runner(engine)
                    fake, _ = self.native(runner, cancel_at=cancel_at)
                    with mock.patch('codex_appserver.run_turn', side_effect=fake):
                        result = self.run_turn(runner, lease)
                    self.assertEqual(result['terminal_state'], 'interrupted_confirmed')
                    self.timer.return_value.cancel.assert_called()
                    self.assertIsNone(runner.controller.connection)
                    state = self.service.coordinator.state(engine)
                    self.assertIsNone(state.get('active_turn'))
                    next_runner, next_lease = self.runner(engine)
                    fake, conn = self.native(next_runner, terminal='completed')
                    with mock.patch('codex_appserver.run_turn', side_effect=fake):
                        result = self.run_turn(next_runner, next_lease)
                    self.assertEqual(result['terminal_state'], 'completed')
                    conn.request_nowait.assert_not_called()

    def test_portable_checkpoint_is_history_not_a_second_model_turn(self):
        runner, lease = self.runner('codex')
        history=[{'type':'message','id':'portable-summary','role':'user',
                  'content':[{'type':'input_text','text':'historical checkpoint'}],
                  'internal_chat_message_metadata_passthrough':{
                      'turn_id':'shepherd-checkpoint:v2:portable_test:0:'+'a'*64}}]
        checkpoint={'turn_id':'codex:0','through_seq':0,
                    'payload':{'source_hash':'','native':{'model':'portable','items':history,
                    'portable_checkpoint_id':'portable_test','estimated_tokens':20}}}
        fake, _ = self.native(runner, terminal='completed')
        calls=[]
        def inspect(*args,**kwargs):
            calls.append(kwargs)
            self.assertEqual(kwargs['history_items'],history)
            self.assertEqual(kwargs['resident_through_seq'],0)
            self.assertEqual(args[3],'test question')
            return fake(*args,**kwargs)
        with mock.patch.object(runner.connector,'context',return_value=('target-key',checkpoint,[])), \
             mock.patch('codex_appserver.run_turn',side_effect=inspect):
            result=self.run_turn(runner,lease)
        self.assertEqual(result['terminal_state'],'completed')
        self.assertEqual(len(calls),1)

    def test_portable_checkpoint_with_current_image_reaches_verified_native_turn(self):
        runner, lease = self.runner('codex')
        runner.image_paths=['/tmp/image.png']
        history=[{'type':'message','id':'portable-summary','role':'user',
                  'content':[{'type':'input_text','text':'historical checkpoint'}],
                  'internal_chat_message_metadata_passthrough':{
                      'turn_id':'shepherd-checkpoint:v2:portable_test:0:'+'a'*64}}]
        checkpoint={'turn_id':'codex:0','through_seq':0,
                    'payload':{'source_hash':'','native':{'model':'portable','items':history,
                    'portable_checkpoint_id':'portable_test','estimated_tokens':20}}}
        fake, _ = self.native(runner, terminal='completed')
        def inspect(*args, **kwargs):
            self.assertEqual(kwargs['history_items'],history)
            self.assertEqual(kwargs['image_paths'],['/tmp/image.png'])
            self.assertTrue(callable(kwargs['on_history_injected']))
            return fake(*args, **kwargs)
        with mock.patch.object(runner.connector,'context',return_value=('target-key',checkpoint,[])), \
             mock.patch('codex_appserver.run_turn',side_effect=inspect):
            result=self.run_turn(runner,lease)
        self.assertEqual(result['terminal_state'],'completed')

    def test_exit_after_interrupted_terminal_still_commits(self):
        runner, lease = self.runner('codex')
        fake, _ = self.native(runner, cancel_at='running', fail_after_terminal=True)
        with mock.patch('codex_appserver.run_turn', side_effect=fake):
            result = self.run_turn(runner, lease)
        self.assertEqual(result['terminal_state'], 'interrupted_confirmed')

    def test_completion_racing_with_esc_preserves_native_outcome(self):
        runner, lease = self.runner('codex')
        fake, _ = self.native(runner, cancel_at='running', terminal='completed')
        with mock.patch('codex_appserver.run_turn', side_effect=fake):
            result = self.run_turn(runner, lease)
        self.assertEqual(result['terminal_state'], 'completed')

    def test_interrupted_status_does_not_hide_wrong_returned_thread(self):
        runner, lease = self.runner('codex')
        fake, _ = self.native(runner, cancel_at='running')
        def wrong_thread(*args, **kwargs):
            fake(*args, **kwargs)
            return 'foreign-thread'
        with mock.patch('codex_appserver.run_turn', side_effect=wrong_thread):
            with self.assertRaisesRegex(Conflict, 'thread identity mismatch'):
                self.run_turn(runner, lease)
        self.assertIsNone(runner.receipts.read(runner.binding, runner.round_id, required=False))

    def test_missing_or_foreign_terminal_stays_blocked_and_cleans_timer(self):
        for engine, terminal in (('codex', ''),):
            with self.subTest(engine=engine):
                runner, lease = self.runner(engine)
                fake, _ = self.native(runner, cancel_at='running', terminal=terminal, unrelated=True)
                with mock.patch('codex_appserver.run_turn', side_effect=fake):
                    with self.assertRaises((RuntimeError, Conflict)):
                        self.run_turn(runner, lease)
                self.assertIsNone(runner.controller.connection)
                self.assertIsNone(runner.controller._cancel_timer)
                self.timer.return_value.cancel.assert_called()
                self.assertIsNone(runner.receipts.read(runner.binding, runner.round_id, required=False))
                state = self.service.coordinator.state(engine)
                self.assertEqual(state['active_turn']['recovery_state'], 'blocked')

    def snapshot_native(self, runner, records, *, items=None, terminal='completed'):
        fake, conn = self.native(runner, terminal=terminal, raw_items=items)
        def turn(*args, **kwargs):
            result = fake(*args, **kwargs)
            home = Path(args[1]['CODEX_HOME'])
            home.mkdir(parents=True, exist_ok=True)
            path = home / 'rollout.jsonl'
            raw = ''.join(json.dumps(row) + '\n' for row in records).encode()
            path.write_bytes(raw)
            kwargs['on_working_context']('ephemeral-thread', str(path))
            return result
        return turn, conn

    def test_cache_incompatibility_auto_recovers_and_next_turn_uses_preserved_history(self):
        records = [{'type': 'session_meta', 'payload': {'id': 'ephemeral-thread'}},
                   {'type': 'future_metadata', 'payload': {'opaque': 'retained'}}]
        items = [{'type': 'function_call', 'call_id': 'once', 'name': 'send', 'arguments': '{}'},
                 {'type': 'function_call_output', 'call_id': 'once', 'output': 'already sent'},
                 {'type': 'message', 'role': 'assistant', 'content': [
                     {'type': 'output_text', 'text': 'completed result'}]}]
        for engine in ('codex',):
            runner, lease = self.runner(engine)
            fake, conn = self.snapshot_native(runner, records, items=items)
            with mock.patch('codex_appserver.run_turn', side_effect=fake) as run, \
                 mock.patch('codex_appserver.drop_session_connection') as drop:
                row = self.run_turn(runner, lease)
            self.assertEqual(run.call_count, 1)
            drop.assert_called_with('shepherd:' + runner.binding['binding_id'], conn)
            self.assertEqual(row['terminal_state'], 'completed')
            self.assertIsNone(self.service.coordinator.state(engine)['inflight'])
            c = runner.connector
            evidence = runner.history_recovery['evidence']
            path = c.archive.directory / evidence['file']
            raw = path.read_bytes()
            self.assertEqual(raw, ''.join(json.dumps(r) + '\n' for r in records).encode())
            self.assertEqual(c.resolve_source(row['source'])['public'], items)
            self.assertIsNone(c.archive.checkpoint(c.context({'model': 'fixture'})[0]))
            # A fresh execution reads evidence as history; it never resends the
            # previous prompt and never calls the recorded tool again.
            next_runner, next_lease = self.runner(engine)
            fake_next, _ = self.native(next_runner, terminal='completed')
            def next_turn(*args, **kwargs):
                self.assertIn('completed result', json.dumps(kwargs['history_items']))
                return fake_next(*args, **kwargs)
            with mock.patch('codex_appserver.run_turn', side_effect=next_turn) as run:
                self.assertEqual(self.run_turn(next_runner, next_lease)['terminal_state'], 'completed')
            self.assertEqual(run.call_count, 1)
            self.assertEqual(path.read_bytes(), raw)

    def test_cache_recovery_never_hides_identity_tools_or_persistence_failure(self):
        from shepherd.store import InvalidState
        cases = ('identity', 'tools', 'unknown_source', 'storage', 'rollback', 'no_terminal')
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                # Each case needs an independent conversation because failures
                # intentionally leave its execution fence in place.
                self.root = Path(directory)
                self.service = Service(self.root / 'state', storage='unified')
                runner, lease = self.runner('codex')
                records = [{'type': 'session_meta', 'payload': {
                    'id': 'foreign' if case == 'identity' else 'ephemeral-thread'}},
                    {'type': 'future_metadata', 'payload': {}}]
                if case == 'rollback':
                    records.append({'type': 'event_msg', 'payload': {'type': 'thread_rolled_back'}})
                items = ([{'type': 'function_call', 'call_id': 'c', 'name': 'send', 'arguments': '{}'}]
                         if case == 'tools' else [{'type': 'future_tool'}] if case == 'unknown_source' else [])
                fake, _ = self.snapshot_native(runner, records, items=items,
                                              terminal='failed' if case == 'no_terminal' else 'completed')
                with mock.patch('codex_appserver.run_turn', side_effect=fake), \
                     mock.patch.object(runner.connector.archive, 'retain_context_evidence',
                         side_effect=OSError('disk full')) if case == 'storage' else \
                     mock.patch('shepherd.unified_runners.sweep'):
                    if case == 'no_terminal':
                        # Failed native status is kept as failed, never upgraded.
                        self.assertEqual(self.run_turn(runner, lease)['terminal_state'], 'failed_recoverable')
                    else:
                        with self.assertRaises((Conflict, InvalidState, OSError)):
                            self.run_turn(runner, lease)
                        self.assertIsNone(runner.receipts.read(runner.binding, runner.round_id, required=False))

    def test_cancel_during_setup_is_carried_to_controller_and_does_not_dispatch(self):
        runner, lease = self.runner('codex')
        def seed(*args):
            runner.cancel()  # Controller does not exist yet.
        conn = mock.Mock(proc=None)
        def turn(*args, **kwargs):
            kwargs['on_connection'](conn)
            self.assertTrue(runner.controller.cancelled.is_set())
            self.assertTrue(kwargs['cancelled']())
            raise RuntimeError('cancelled before dispatch')
        with mock.patch.object(CodexFamilyConnector, '_seed_private_config', side_effect=seed), \
             mock.patch('codex_appserver.run_turn', side_effect=turn):
            with self.assertRaises(PromptNotStarted):
                self.run_turn(runner, lease)
        conn.request_nowait.assert_not_called()
        self.assertIsNone(runner.controller.connection)
        self.assertIsNone(self.service.coordinator.state('codex').get('active_turn'))


class CancelFallback(unittest.TestCase):
    def test_blocked_interrupt_write_does_not_hold_lifecycle_lock(self):
        connector = mock.Mock(store_root='/tmp', binding={'binding_id': 'test'})
        runner = CodexFamilyRunner(connector)
        conn = mock.Mock()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def blocked_write(*args):
            entered.set()
            release.wait(3)
            finished.set()
        conn.request_nowait.side_effect = blocked_write
        with mock.patch('shepherd.runners.threading.Timer'):
            runner._connected(conn)
            runner._turn_started('thread', 'turn')
            runner.cancel()
            try:
                self.assertTrue(entered.wait(2))
                acquired = runner._connection_lock.acquire(timeout=.5)
                if acquired:
                    runner._connection_lock.release()
                self.assertTrue(acquired, 'interrupt write blocks timeout and terminal cleanup')
            finally:
                release.set()
                finished.wait(2)
                runner._disconnected()

    def test_timeout_starts_only_after_native_turn_id_and_cleanup_disarms_it(self):
        connector = mock.Mock(store_root='/tmp', binding={'binding_id': 'test'})
        runner = CodexFamilyRunner(connector)
        conn = mock.Mock()
        conn.proc.pid = 123456
        foreign = mock.Mock()
        with mock.patch('shepherd.runners.threading.Timer') as timer, \
             mock.patch('shepherd.runners.os.killpg') as kill:
            runner._connected(conn)
            runner.cancel()
            runner.cancel()
            # initialize, history injection and turn/start may all still be in
            # progress. Esc is remembered, but cannot kill a turn not yet known.
            timer.assert_not_called()
            runner._turn_started('thread', 'turn')
            timer.assert_called_once_with(
                3, runner._cancel_expired, args=(conn, 'thread', 'turn'))
            runner._cancel_expired(foreign, 'thread', 'turn')
            runner._cancel_expired(conn, 'other-thread', 'turn')
            runner._cancel_expired(conn, 'thread', 'other-turn')
            kill.assert_not_called()
            runner._cancel_expired(conn, 'thread', 'turn')
            kill.assert_called_once_with(123456, signal.SIGKILL)
            runner._disconnected()
            timer.return_value.cancel.assert_called_once()
            kill.reset_mock()
            runner._cancel_expired(conn, 'thread', 'turn')
            kill.assert_not_called()

    def test_old_turn_timeout_cannot_kill_successor_on_reused_controller(self):
        connector = mock.Mock(store_root='/tmp', binding={'binding_id': 'test'})
        runner = CodexFamilyRunner(connector)
        conn = mock.Mock()
        conn.proc.pid = 123456
        with mock.patch('shepherd.runners.threading.Timer'), \
             mock.patch('shepherd.runners.os.killpg') as kill:
            runner._connected(conn)
            runner._turn_started('thread', 'old-turn')
            runner.cancel()
            runner._turn_started('thread', 'new-turn')
            runner._cancel_expired(conn, 'thread', 'old-turn')
            kill.assert_not_called()
            runner._disconnected()
