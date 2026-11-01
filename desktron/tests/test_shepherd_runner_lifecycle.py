"""Real stdio subprocesses and permission routing without external model calls."""
import importlib.util
import json
import os
from _platform import assert_private_file, version_cli
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'desktron/lib'))
from shepherd.execution_budget import (
    ExecutionDeadline, ExecutionReviewClock, execution_review_prompt,
    hard_timeout_seconds, model_context_window, runner_timeouts,
)
from shepherd.runner_process import NativeProcess
from shepherd.store import Conflict, InvalidState
from shepherd.claude_codec import CODEC_VERSION as CLAUDE_CODEC_VERSION
from shepherd.runners import ClaudeRunner, CodexFamilyRunner, PiRunner, clean_claude_parent_env
from shepherd.source_codec import pi_public, codex_public

_argv = sys.argv
sys.argv = ['ui_server.py', str(Path(__file__).resolve().parents[2] / 'frontend'),
            '7999', '7998', str(Path(__file__).resolve().parents[2])]
try:
    from engine_adapters.bus import EventBus
    from engine_adapters.shepherd import ShepherdAdapter
    from app_control import capability as app_control_capability
finally:
    sys.argv = _argv


class PiWebExtensionCommand(unittest.TestCase):
    def command(self, env):
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
        connector = mock.Mock(store_root=root.name, native_cwd=root.name,
                              path=os.path.join(root.name, 'pi.jsonl'), native_session_id='native')
        connector.binding = {'binding_id':'binding'}
        connector.ownership_nonce = 'nonce'
        connector.ownership.verify.return_value = None
        connector.verify.side_effect = [
            {'entries':0, 'native_revision':'before'},
            {'entries':1, 'native_revision':'after'},
        ]
        connector.revision.return_value = 'after'
        process = mock.Mock(proc=mock.Mock())
        process.events.return_value = iter([{'type':'agent_settled'}])
        runner = PiRunner(connector, env=env)
        runner.receipts = mock.Mock()
        with mock.patch('shepherd.runners.NativeProcess', return_value=process) as native:
            result = runner.run(prompt='q', round_id='round', on_dispatched=lambda **_kwargs: None)
        self.assertEqual(result['terminal_state'], 'completed')
        return native.call_args.args[0]

    def test_pi_runner_web_extension_is_default_on_and_explicitly_disabled(self):
        self.assertIn('pi_web.ts', ' '.join(self.command({})))
        self.assertNotIn('pi_web.ts', ' '.join(self.command({'DESKTRON_WEB_TOOLS':'0'})))


class ExecutionReviewBudget(unittest.TestCase):
    def test_hourly_review_requires_native_ack_and_completed_continuation(self):
        now = [0.]
        wall = [1000.]
        ids = iter(('review-1', 'review-2'))
        review = ExecutionReviewClock(clock=lambda:now[0], wall_clock=lambda:wall[0],
            interval=3600, absolute_seconds=12*3600, id_factory=lambda:next(ids))
        self.assertIsNone(review.mark_due())
        now[0] = 3600
        due = review.mark_due()
        self.assertEqual((due['status'], due['review_sequence']), ('review_due', 1))
        prompt = review.prompt()
        self.assertIn('review_id=review-1', prompt)
        accepted = review.accepted()
        self.assertEqual(accepted['status'], 'injected')
        now[0] = 3660
        completed = review.completed()
        self.assertEqual(completed['status'], 'running')
        self.assertEqual(review.next_deadline(), (7200, 'review'))
        now[0] = 7200
        self.assertEqual(review.mark_due()['review_sequence'], 2)

    def test_review_ack_timeout_and_absolute_limit_are_hard_bounds(self):
        now = [0.]
        review = ExecutionReviewClock(clock=lambda:now[0], interval=10,
            absolute_seconds=25, ack_seconds=2, id_factory=lambda:'review')
        now[0] = 10
        review.mark_due(); review.prompt()
        self.assertEqual(review.next_deadline(), (12, 'review_ack'))
        review.reject('ack timed out')
        self.assertEqual(review.next_deadline(), (10, 'review_rejected'))

        review = ExecutionReviewClock(clock=lambda:now[0], interval=10,
            absolute_seconds=25, id_factory=lambda:'review')
        now[0] = 35
        review.mark_due()
        self.assertEqual(review.status, 'absolute_limit')
        self.assertEqual(review.next_deadline(), (35, 'absolute_review_limit'))

    def test_prompt_requests_bounded_judgement_without_new_scope(self):
        prompt = execution_review_prompt('review-1', 1)
        self.assertIn('预计耗时', prompt)
        self.assertIn('不扩展到新发现的相邻问题', prompt)
        self.assertIn('不是新的用户请求', prompt)


class PiExecutionReviewProtocol(unittest.TestCase):
    def runner(self, engine='pi'):
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
        connector = mock.Mock(store_root=root.name, engine=engine,
                              native_session_id='native', native_cwd=root.name)
        connector.binding = {'binding_id':'binding', 'conversation_id':'conversation'}
        runner = importlib.import_module('shepherd.unified_runners').UnifiedRunner(
            connector, options={'_execution_review':True})
        runner.round_id = 'round'; runner._context_dispatched = False
        runner.process = mock.Mock()
        return runner

    def test_only_pi_enables_review_and_matching_steer_ack_is_required(self):
        runner = self.runner('pi')
        self.assertTrue(runner._review_enabled())
        runner.process.review_prompt.return_value = 'internal review'
        runner.process.review_snapshot.return_value = {'status':'dispatching'}
        runner._review_due({'status':'review_due'})
        frame = runner.process.send.call_args.args[0]
        self.assertEqual((frame['type'], frame['message']), ('steer', 'internal review'))
        runner.process.accept_review.return_value = {'status':'injected'}
        self.assertTrue(runner._review_response({'type':'response','id':frame['id'],
                                                'command':'steer','success':True}))
        runner.process.accept_review.assert_called_once_with()
        self.assertFalse(self.runner('claude')._review_enabled())
        self.assertFalse(self.runner('codex')._review_enabled())

    def test_rejected_or_mismatched_steer_never_renews(self):
        runner = self.runner('pi')
        runner.process.review_prompt.return_value = 'internal review'
        runner.process.review_snapshot.return_value = {'status':'dispatching'}
        runner._review_due({'status':'review_due'})
        frame = runner.process.send.call_args.args[0]
        self.assertFalse(runner._review_response({'type':'response','id':'other',
                                                  'command':'get_state','success':True}))
        runner.process.reject_review.return_value = {'status':'rejected'}
        with self.assertRaisesRegex(Conflict, 'steering'):
            runner._review_response({'type':'response','id':frame['id'],
                                     'command':'steer','success':False,'error':'unsupported'})
        runner.process.accept_review.assert_not_called()


class ExecutionBudget(unittest.TestCase):

    def test_tool_wait_and_metadata_do_not_forge_progress_or_terminal(self):
        now = [0.]
        budget = ExecutionDeadline(900, 7200, clock=lambda:now[0])
        budget.touch({'type':'tool_execution_start', 'toolCallId':'one'})
        budget.touch({'type':'tool_execution_start', 'toolCallId':'two'})
        now[0] = 100
        budget.touch({'type':'tool_execution_end', 'toolCallId':'one'})
        now[0] = 500
        budget.touch({'type':'extension_ui_request', 'method':'notify', 'message':'heartbeat'})
        self.assertEqual(budget.next_deadline(), (1000, 'idle'))
        now[0] = 1000
        stalled = budget.mark_soft_stall()
        self.assertEqual(stalled['activity']['active_tools'], 1)
        self.assertEqual(budget.next_deadline(), (7200, 'hard'))
        now[0] = 1100
        self.assertTrue(budget.touch({'type':'tool_execution_end', 'toolCallId':'two'}))
        self.assertEqual(budget.next_deadline(), (2000, 'idle'))
        # A fresh turn never carries old tool/transport observations across recovery.
        fresh = ExecutionDeadline(900, 7200, clock=lambda:now[0])
        self.assertEqual(fresh.observation()['active_tools'], 0)
        self.assertIsNone(fresh.observation()['transport_age_seconds'])
        self.assertIsNone(fresh.observation()['provider_transport_age_seconds'])

    def test_compaction_has_an_absolute_budget_without_a_streaming_requirement(self):
        now = [0.]
        budget = ExecutionDeadline(900, 7200, operation='compact', clock=lambda:now[0])
        now[0] = 1800
        self.assertEqual(budget.next_deadline(), (7200, 'hard'))
        budget.touch({'type':'compaction_start'})
        self.assertEqual(budget.next_deadline(), (7200, 'hard'))


    def test_transport_metadata_and_empty_tool_updates_do_not_delay_soft_stall(self):
        now = [0.]
        budget = ExecutionDeadline(900, 7200, clock=lambda:now[0])
        budget.touch({'type':'tool_execution_start', 'toolCallId':'one'})
        for tick, event in ((100, {'type':'turn_end', 'usage':{'totalTokens':1}}),
                            (200, {'type':'system', 'subtype':'status', 'status':'ready'}),
                            (300, {'type':'unknown_control', 'value':'heartbeat'}),
                            (400, {'type':'tool_execution_update', 'toolCallId':'one',
                                   'partialResult':{'content':[]}}),
                            (500, {'type':'tool_execution_update', 'toolCallId':'one',
                                   'partialResult':{'content':[{'type':'tool_result'}]}}),
                            (600, {'type':'tool_execution_update', 'toolCallId':'one',
                                   'partialResult':{'content':[{'type':'toolCall'}]}})):
            now[0] = tick
            self.assertFalse(budget.touch(event))
            self.assertEqual(budget.last_activity, 0)
        self.assertEqual(budget.observation()['transport_age_seconds'], 0)
        now[0] = 900
        self.assertIsNotNone(budget.mark_soft_stall())
        self.assertEqual(budget.observation()['transport_age_seconds'], 300)
        now[0] = 1000
        output = {'type':'tool_execution_update', 'toolCallId':'one',
                  'partialResult':{'content':[{'type':'text','text':'output'}]}}
        self.assertTrue(budget.touch(output))
        self.assertEqual(budget.last_activity, 1000)
        self.assertEqual(budget.last_progress, 1000)
        self.assertFalse(budget.soft_stalled)

    def test_repeated_cumulative_tool_snapshot_is_transport_until_content_changes(self):
        now = [0.]
        budget = ExecutionDeadline(100, 7200, clock=lambda:now[0])
        budget.touch({'type':'tool_execution_start', 'toolCallId':'one'})
        first = {'type':'tool_execution_update', 'toolCallId':'one',
                 'partialResult':{'content':[{'type':'text','text':'same output'}]}}
        now[0] = 10
        self.assertFalse(budget.touch(first))
        self.assertEqual(budget.last_progress, 10)
        now[0] = 110
        self.assertIsNotNone(budget.mark_soft_stall())
        now[0] = 120
        self.assertFalse(budget.touch(first))
        self.assertTrue(budget.soft_stalled)
        self.assertEqual(budget.last_progress, 10)
        self.assertEqual(budget.observation()['transport_age_seconds'], 0)
        changed = {'type':'tool_execution_update', 'toolCallId':'one',
                   'partialResult':{'content':[{'type':'text','text':'same output\nmore'}]}}
        now[0] = 130
        self.assertTrue(budget.touch(changed))
        self.assertFalse(budget.soft_stalled)
        self.assertEqual(budget.last_progress, 130)
        self.assertEqual(len(budget.tool_output_fingerprints), 2)
        now[0] = 140
        self.assertFalse(budget.touch(first))
        self.assertEqual(budget.last_progress, 130)

    def test_tool_snapshot_digest_is_cleared_on_end_and_reused_start(self):
        now = [0.]
        budget = ExecutionDeadline(900, 7200, clock=lambda:now[0])
        update = {'type':'tool_execution_update', 'toolCallId':'one',
                  'partialResult':{'content':[{'type':'text','text':'output'}]}}
        budget.touch({'type':'tool_execution_start', 'toolCallId':'one'})
        now[0] = 10; budget.touch(update)
        tool_id, digest = next(iter(budget.tool_output_fingerprints))
        self.assertEqual(tool_id, 'one')
        self.assertEqual(len(digest), 64)
        self.assertNotIn('output', repr(budget.tool_output_fingerprints))
        now[0] = 15; budget.touch({'type':'tool_execution_start', 'toolCallId':'one'})
        now[0] = 16; budget.touch(update)
        self.assertEqual(budget.last_progress, 10)
        now[0] = 20; budget.touch({'type':'tool_execution_end', 'toolCallId':'one'})
        self.assertFalse(budget.tool_output_fingerprints)
        now[0] = 25; budget.touch({'type':'tool_execution_end', 'toolCallId':'one'})
        self.assertEqual(budget.last_progress, 20)
        self.assertEqual(budget.last_tool_result, 20)
        now[0] = 30; budget.touch({'type':'tool_execution_start', 'toolCallId':'one'})
        self.assertFalse(budget.tool_output_fingerprints)
        now[0] = 40; budget.touch(update)
        self.assertEqual(budget.last_progress, 40)

    def test_late_tool_snapshot_after_end_is_transport_only(self):
        now = [0.]
        budget = ExecutionDeadline(100, 7200, clock=lambda:now[0])
        update = {'type':'tool_execution_update', 'toolCallId':'one',
                  'partialResult':{'content':[{'type':'text','text':'output'}]}}
        budget.touch({'type':'tool_execution_start', 'toolCallId':'one'})
        now[0] = 10; budget.touch(update)
        now[0] = 20; budget.touch({'type':'tool_execution_end', 'toolCallId':'one'})
        now[0] = 120
        self.assertIsNotNone(budget.mark_soft_stall())
        now[0] = 130
        self.assertFalse(budget.touch(update))
        self.assertTrue(budget.soft_stalled)
        self.assertEqual(budget.last_progress, 20)
        self.assertFalse(budget.tool_output_fingerprints)

    def test_unidentified_or_unserializable_tool_snapshot_is_transport_only(self):
        now = [0.]
        budget = ExecutionDeadline(900, 7200, clock=lambda:now[0])
        now[0] = 10
        budget.touch({'type':'tool_execution_update',
                      'partialResult':{'content':[{'type':'text','text':'output'}]}})
        self.assertIsNone(budget.last_progress)
        now[0] = 20
        budget.touch({'type':'tool_execution_update', 'toolCallId':'one',
                      'partialResult':{'content':[{'type':'text','text':'output'}, object()]}})
        self.assertIsNone(budget.last_progress)
        self.assertEqual(budget.observation()['transport_age_seconds'], 0)
        self.assertFalse(budget.tool_output_fingerprints)

    def test_tool_snapshot_digest_cache_is_bounded_without_suppressing_new_output(self):
        now = [0.]
        budget = ExecutionDeadline(900, 7200, clock=lambda:now[0])
        for index in range(140):
            now[0] = index + 1
            tool_id = f'tool-{index}'
            budget.touch({'type':'tool_execution_start', 'toolCallId':tool_id})
            budget.touch({'type':'tool_execution_update', 'toolCallId':tool_id,
                          'partialResult':{'content':[{'type':'text',
                                                      'text':f'sensitive-{index}'}]}})
        self.assertEqual(budget.last_progress, 140)
        self.assertEqual(len(budget.tool_output_fingerprints), 128)
        self.assertTrue(all(len(key[1]) == 64 for key in budget.tool_output_fingerprints))
        self.assertNotIn('sensitive-', repr(budget.tool_output_fingerprints))
        now[0] = 200
        budget.touch({'type':'tool_execution_update', 'toolCallId':'tool-139',
                      'partialResult':{'content':[{'type':'text','text':'new output'}]}})
        self.assertEqual(budget.last_progress, 200)

    def test_claude_and_pi_semantic_frames_share_the_progress_clock(self):
        now = [0.]
        budget = ExecutionDeadline(900, 7200, clock=lambda:now[0])
        cases = (
            {'type':'event', 'event':{'type':'assistant', 'message':{
                'content':[{'type':'text','text':'answer'}]}}},
            {'type':'event', 'event':{'type':'stream_event', 'event':{
                'type':'content_block_start', 'content_block':{
                    'type':'tool_use', 'name':'Bash'}}}},
            {'type':'event', 'event':{'type':'user', 'message':{'content':[
                {'type':'tool_result', 'tool_use_id':'one', 'content':'result'}]}}},
            {'type':'message_end', 'message':{'role':'assistant',
                'content':[{'type':'thinking','thinking':'plan'}]}},
            {'type':'tool_execution_start', 'toolCallId':'one'},
            {'type':'tool_execution_end', 'toolCallId':'one'},
        )
        for tick, event in enumerate(cases, 1):
            now[0] = tick * 100
            budget.touch(event)
            self.assertEqual(budget.last_activity, now[0])
            self.assertEqual(budget.last_progress, now[0])
        now[0] = 700
        budget.touch({'type':'event', 'event':{'type':'assistant',
            'isApiErrorMessage':True, 'message':{'content':[{'type':'text','text':'retry'}]}}})
        self.assertEqual(budget.last_activity, 600)
        for tick, event in (
                (700, {'type':'message_update', 'assistantMessageEvent':{'type':'text_start'}}),
                (800, {'type':'stream_event', 'event':{
                    'type':'content_block_delta', 'delta':{'text':''}}}),
                (900, {'type':'stream_event', 'event':{
                    'type':'content_block_delta', 'delta':{'signature':'opaque'}}}),
                (1000, {'type':'tool_execution_update', 'partialResult':{
                    'content':[{'type':'heartbeat'}]}})):
            now[0] = tick
            budget.touch(event)
            self.assertEqual(budget.last_activity, 600)

    def test_automatic_compaction_returns_to_normal_silence_budget(self):
        now = [0.]
        budget = ExecutionDeadline(900, 7200, clock=lambda:now[0])
        budget.touch({'type':'compaction_start'})
        now[0] = 1800
        self.assertEqual(budget.next_deadline(), (7200, 'hard'))
        budget.touch({'type':'compaction_end'})
        self.assertEqual(budget.next_deadline(), (2700, 'idle'))
        self.assertEqual(budget.hard_deadline, 7200)

    def test_late_automatic_compaction_gets_a_separate_bounded_pool(self):
        now = [7199.]
        budget = ExecutionDeadline(900, 7200, clock=lambda:now[0])
        # Rebase the fixture's start without spending real time in the constructor.
        budget.started = budget.last_activity = 0
        budget.hard_deadline = 7200
        budget.touch({'type':'compaction_start'})
        self.assertEqual(budget.next_deadline(), (14399, 'hard'))
        now[0] = 7299
        budget.touch({'type':'compaction_end'})
        # The one-shot reserve remains available for native settlement or resumed
        # work, but the whole turn is still bounded by twice its original hard limit.
        self.assertEqual(budget.hard_deadline, 14399)
        self.assertLessEqual(budget.hard_deadline, 14400)

    def test_automatic_compaction_reserve_is_one_shot(self):
        now = [75.]
        budget = ExecutionDeadline(10, 100, clock=lambda:now[0])
        budget.started = budget.last_activity = 0
        budget.hard_deadline = 100
        budget.maximum_hard_deadline = 200
        budget.touch({'type':'compaction_start'})
        self.assertEqual(budget.next_deadline(), (175, 'hard'))
        now[0] = 90
        budget.touch({'type':'compaction_end'})
        now[0] = 150
        budget.touch({'type':'compaction_start'})
        self.assertEqual(budget.next_deadline(), (175, 'hard'))
        # Repeated start notifications cannot renew the current phase either.
        now[0] = 160
        budget.touch({'type':'compaction_start'})
        self.assertEqual(budget.next_deadline(), (175, 'hard'))

    def test_absolute_limit_can_be_shorter_than_idle_limit(self):
        budget = ExecutionDeadline(30, 5, clock=lambda:0)
        self.assertEqual(budget.next_deadline(), (5, 'hard'))

    def test_hard_timeout_scales_with_thinking_and_context(self):
        base = hard_timeout_seconds(900, effort='medium', context_window=200_000)
        deep = hard_timeout_seconds(900, effort='max', context_window=200_000)
        large = hard_timeout_seconds(900, effort='max', context_window=800_000)
        self.assertEqual(base, 7200)
        self.assertGreater(deep, base)
        self.assertGreater(large, deep)
        self.assertLessEqual(large, 12 * 60 * 60)

    def test_unknown_model_uses_baseline_without_guessed_window(self):
        catalog = {'models':[{'id':'known','contextWindow':936_000}]}
        self.assertEqual(model_context_window(catalog, 'missing'), 0)
        budget = runner_timeouts({'model':'missing','effort':'medium'}, catalog)
        self.assertEqual(budget['hard_timeout'], 7200)

    def test_explicit_compaction_window_is_used_when_catalog_is_unknown(self):
        budget = runner_timeouts({'effort':'high','autocompact':'600000'}, {})
        self.assertEqual(budget['context_window'], 600000)
        self.assertGreater(budget['hard_timeout'], 7200)

    def test_provider_qualifies_short_model_without_borrowing_another_window(self):
        catalog = {'models': [
            {'id': 'office-qwen/Qwen3.8-27B-FP8', 'contextWindow': 32768},
            {'id': 'Qwen3.8-27B-FP8', 'contextWindow': 200000},
            {'id': 'other/Qwen3.8-27B-FP8', 'contextWindow': 100000},
        ]}
        for model, provider, expected in (
            ('Qwen3.8-27B-FP8', 'office-qwen', 32768),
            ('office-qwen/Qwen3.8-27B-FP8', '', 32768),
            ('Qwen3.8-27B-FP8', 'missing', 0),
            ('Qwen3.8-27B-FP8', '', 200000),
        ):
            with self.subTest(model=model, provider=provider):
                budget = runner_timeouts({'model': model, 'provider': provider}, catalog)
                self.assertEqual(budget['context_window'], expected)

    def test_catalog_window_is_used_for_selected_variant(self):
        catalog = {'models':[{'id':'family','variants':[
            {'id':'provider/model__max','contextWindow':936_000}]}]}
        self.assertEqual(model_context_window(catalog, 'provider/model__max'), 936_000)
        budget = runner_timeouts({'model':'provider/model__max','effort':'max'}, catalog)
        self.assertGreater(budget['hard_timeout'], 7200)


class ProcessLifecycle(unittest.TestCase):
    def test_native_constructor_delegates_target_cwd_to_owned_supervisor(self):
        proc = mock.Mock()
        with mock.patch('shepherd.runner_process.subprocess.Popen', return_value=proc) as spawn, \
             mock.patch('owned_process.command', return_value=['owned','native']) as command, \
             mock.patch.object(NativeProcess, '_initialize'):
            got = NativeProcess(['native'], cwd='/protected/task', env={}, timeout=1,
                                cancel_frame={'type':'cancel'})
        command.assert_called_once_with(['native'], cwd='/protected/task')
        self.assertNotIn('cwd', spawn.call_args.kwargs)
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])
        self.assertIs(got.proc, proc)

    def test_unknown_source_blocks_fail_closed_instead_of_losing_content(self):
        with self.assertRaises(InvalidState):
            pi_public([{'type':'message','message':{'role':'user','content':[{'type':'image','data':'x'}]}}])
        with self.assertRaises(InvalidState):
            codex_public([{'type':'message','role':'user','content':[{'type':'input_image','image_url':'x'}]}])

    def process(self, code, timeout=5):
        process = NativeProcess([sys.executable, '-u', '-c', code], cwd=tempfile.gettempdir(),
            env=dict(os.environ), timeout=timeout, cancel_frame={'type':'cancel'})
        self.addCleanup(process.close)
        return process

    def test_stderr_is_drained_and_permission_uses_bidirectional_pipe(self):
        process = self.process("import sys,json\nsys.stderr.write('x'*200000)\n"
            "print(json.dumps({'type':'permission','id':'ask'}),flush=True)\n"
            "reply=json.loads(input())\nprint(json.dumps(reply),flush=True)")
        events = process.events()
        self.assertEqual(next(events)['type'], 'permission')
        process.send({'type':'permission_result','allow':False,'updatedInput':{'x':1}})
        self.assertEqual(list(events), [{'type':'permission_result','allow':False,'updatedInput':{'x':1}}])
        self.assertLessEqual(sum(map(len, process.errors)), 32768)

    def test_idle_window_reports_soft_stall_and_does_not_kill_process(self):
        observed = []
        process = NativeProcess(
            [sys.executable, '-S', '-u', '-c',
             "import json,time\ntime.sleep(.35)\nprint(json.dumps({'type':'progress'}),flush=True)"],
            cwd=tempfile.gettempdir(), env=dict(os.environ), timeout=.1, hard_timeout=30,
            cancel_frame={'type':'cancel'}, on_stall=observed.append)
        self.addCleanup(process.close)
        self.assertEqual(list(process.events()), [{'type':'progress'}])
        self.assertFalse(process.timed_out)
        deadline=time.monotonic()+2
        while len(observed)<2 and time.monotonic()<deadline:time.sleep(.01)
        states = [row.get('active') for row in observed]
        self.assertGreaterEqual(len(states), 2)
        self.assertEqual(states[:2], [True, False])
        self.assertEqual([row.get('sequence') for row in observed[:2]], [1,2])

    def test_blocked_stall_observer_cannot_delay_hard_timeout(self):
        entered=threading.Event();release=threading.Event();killed=threading.Event()
        process=object.__new__(NativeProcess)
        process.proc=mock.Mock();process.proc.poll.return_value=None
        process._kill=killed.set;process._drain_errors=lambda:None
        process._initialize(.05,.2,{'type':'cancel'},on_stall=lambda _row:(entered.set(),release.wait(2)))
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(killed.wait(1))
            self.assertEqual(process.timeout_kind,'hard')
        finally:
            release.set();process._stop_watchdog()

    def test_stall_and_recovery_callbacks_remain_ordered(self):
        entered=threading.Event();release=threading.Event();observed=[]
        def callback(row):
            observed.append(row)
            if row.get('active'):
                entered.set();release.wait(2)
        process=object.__new__(NativeProcess)
        process.proc=mock.Mock();process.proc.poll.return_value=None
        process._kill=mock.Mock();process._drain_errors=lambda:None
        process._initialize(.05,2,{'type':'cancel'},on_stall=callback)
        try:
            self.assertTrue(entered.wait(1))
            process._touch({'type':'progress'})
            release.set()
            deadline=time.monotonic()+1
            while len(observed)<2 and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual([row.get('active') for row in observed],[True,False])
            self.assertEqual([row.get('sequence') for row in observed],[1,2])
        finally:
            release.set();process._stop_watchdog()

    def test_regular_native_activity_renews_the_idle_timeout(self):
        process = NativeProcess(
            [sys.executable, '-S', '-u', '-c',
             "import json,time\n"
             "for i in range(6):\n"
             " print(json.dumps({'type':'progress','n':i}),flush=True);time.sleep(.2)"],
            # Full-gate process pressure can delay a newly spawned interpreter
            # for several seconds before its first line. Production uses a
            # minutes-long limit; keep this fixture above that startup jitter.
            cwd=tempfile.gettempdir(), env=dict(os.environ), timeout=15,
            cancel_frame={'type':'cancel'})
        self.addCleanup(process.close)
        self.assertEqual([row['n'] for row in process.events()], list(range(6)))
        self.assertFalse(process.timed_out)

    def test_stderr_is_transport_and_cannot_hide_a_soft_stall(self):
        observed = []
        process = NativeProcess(
            [sys.executable, '-S', '-u', '-c',
             "import json,sys,time\n"
             "for i in range(12):\n"
             " print('working',i,file=sys.stderr,flush=True);time.sleep(.03)\n"
             "print(json.dumps({'type':'progress'}),flush=True)"],
            cwd=tempfile.gettempdir(), env=dict(os.environ), timeout=.1, hard_timeout=30,
            cancel_frame={'type':'cancel'}, on_stall=observed.append)
        self.addCleanup(process.close)
        self.assertEqual(list(process.events()), [{'type':'progress'}])
        deadline=time.monotonic()+2
        while len(observed)<2 and time.monotonic()<deadline:time.sleep(.01)
        self.assertIn('working 11', ''.join(process.errors))
        self.assertGreaterEqual(len(observed), 2)
        self.assertEqual([row.get('active') for row in observed[:2]], [True, False])
        self.assertFalse(process.timed_out)

    def test_absolute_ceiling_stops_a_process_that_only_prints_heartbeats(self):
        process = NativeProcess(
            [sys.executable, '-S', '-u', '-c',
             "import json,time\n"
             "while True:\n"
             " print(json.dumps({'type':'progress'}),flush=True);time.sleep(.03)"],
            cwd=tempfile.gettempdir(), env=dict(os.environ), timeout=30,
            hard_timeout=5, cancel_frame={'type':'cancel'})
        self.addCleanup(process.close)
        start = time.monotonic()
        with self.assertRaisesRegex(Conflict, '最长时限'):
            list(process.events())
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 4.5)
        self.assertLess(elapsed, 7)
        self.assertEqual(process.timeout_kind, 'hard')

    def test_stdout_eof_does_not_disarm_the_absolute_ceiling(self):
        process = NativeProcess(
            [sys.executable, '-S', '-u', '-c',
             "import os,sys,time\n"
             "print('ready',file=sys.stderr,flush=True)\n"
             "os.close(1)\n"
             "while True:\n"
             " print('working',file=sys.stderr,flush=True);time.sleep(.03)"],
            cwd=tempfile.gettempdir(), env=dict(os.environ), timeout=2,
            hard_timeout=3, cancel_frame={'type':'cancel'})
        self.addCleanup(process.close)
        start = time.monotonic()
        with self.assertRaisesRegex(Conflict, '最长时限'):
            list(process.events())
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 2.5)
        self.assertLess(elapsed, 5)
        self.assertEqual(process.timeout_kind, 'hard')
        self.assertIsNotNone(process.proc.poll())

    def test_cancel_is_idempotent_and_native_terminal_is_preserved(self):
        process = self.process("import json\nprint(json.dumps({'type':'ready'}),flush=True)\n"
                               "input()\nprint(json.dumps({'type':'interrupted'}))")
        events = process.events()
        self.assertEqual(next(events), {'type':'ready'})
        self.assertTrue(process.cancel()); self.assertTrue(process.cancel())
        self.assertEqual(list(events), [{'type':'interrupted'}])

    def test_startup_cancel_without_native_terminal_remains_unconfirmed(self):
        process = self.process('import time;time.sleep(60)', timeout=10)
        self.assertTrue(process.cancel()); self.assertTrue(process.cancel())
        with self.assertRaisesRegex(Conflict, '异常退出'):
            list(process.events())
        self.assertIsNotNone(process.proc.poll())
        self.assertFalse(process.timed_out)

    def test_forced_kill_is_not_native_completion(self):
        process = self.process('import time;time.sleep(60)')
        process._kill()
        with self.assertRaisesRegex(Conflict, '异常退出'):
            list(process.events())

    def test_cancel_returns_even_when_stdin_is_full(self):
        process = self.process('import time;time.sleep(60)', timeout=10)
        writing = threading.Event()
        def fill_pipe():
            writing.set()
            try: process.send({'payload':'x' * 4_000_000})
            except (OSError, ValueError): pass
        writer = threading.Thread(target=fill_pipe, daemon=True); writer.start()
        self.assertTrue(writing.wait(1)); time.sleep(.1)
        start = time.monotonic()
        process.cancel()
        self.assertLess(time.monotonic() - start, .5)
        process.proc.wait(timeout=5)
        writer.join(timeout=1)
        self.assertFalse(writer.is_alive())
        self.assertNotEqual(process.proc.returncode, 0)


class ClaudeEnvironment(unittest.TestCase):
    def test_bridge_bundle_is_captured_before_source_hot_overwrite(self):
        root = Path(tempfile.mkdtemp()); self.addCleanup(lambda: shutil.rmtree(root))
        source_root = Path(__file__).resolve().parents[1] / 'lib/shepherd'
        for name in ('claude_bridge_snapshot.py','claude_runner.mjs','claude_resident_runner.mjs',
                     'claude_bootstrap.mjs',
                     'claude_store_bridge.mjs','claude_sqlite_store.mjs',
                     'claude_memory_store.mjs','claude_unified_bootstrap.mjs',
                     'claude_sdk_options.mjs','claude_binary_version.mjs',
                     'claude_dispatch.mjs','fallback_receipt.mjs'):
            shutil.copyfile(source_root/name, root/name)
        spec = importlib.util.spec_from_file_location('isolated_bridge_snapshot',
                                                       root/'claude_bridge_snapshot.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        original = (root/'claude_runner.mjs').read_bytes()
        original_dispatch = (root/'claude_dispatch.mjs').read_bytes()
        (root/'claude_runner.mjs').write_text('throw new Error("hot overwrite");\n')
        (root/'claude_dispatch.mjs').write_text('throw new Error("hot dispatch overwrite");\n')
        pinned = Path(module.bridge_path('claude_runner.mjs'))
        self.addCleanup(module._cleanup)
        self.assertEqual(pinned.read_bytes(), original)
        self.assertEqual((pinned.parent/'claude_dispatch.mjs').read_bytes(), original_dispatch)
        self.assertNotEqual(pinned.read_bytes(), (root/'claude_runner.mjs').read_bytes())
        assert_private_file(pinned, 0o500)

    def test_binary_version_probe_uses_exact_executable_with_bounded_output(self):
        root = Path(tempfile.mkdtemp()); self.addCleanup(lambda: shutil.rmtree(root))
        binary = version_cli(root/'claude fixture', '2.1.283')
        module = Path(__file__).resolve().parents[1]/'lib/shepherd/claude_binary_version.mjs'
        completed = subprocess.run([
            shutil.which('node'), '--input-type=module', '-e',
            "const {claudeBinaryVersion}=await import(process.argv[1]); console.log(claudeBinaryVersion(process.argv[2],{}));",
            module.as_uri(), str(binary)], capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), '2.1.283')

    def test_parent_turn_identity_is_removed_but_provider_credentials_survive(self):
        got = clean_claude_parent_env({
            'PATH':'/usr/bin', 'CLAUDE_CONFIG_DIR':'/tmp/parent-turn',
            'CLAUDE_CODE_MESSAGING_TOKEN':'expired', 'CLAUDE_PID':'42',
            'ANTHROPIC_API_KEY':'configured-provider',
            'DESKTRON_SESSION_ID':'public-conversation',
        })
        self.assertEqual(got['PATH'], '/usr/bin')
        self.assertEqual(got['ANTHROPIC_API_KEY'], 'configured-provider')
        self.assertEqual(got['DESKTRON_SESSION_ID'], 'public-conversation')
        self.assertNotIn('CLAUDE_CONFIG_DIR', got)
        self.assertNotIn('CLAUDE_CODE_MESSAGING_TOKEN', got)
        self.assertNotIn('CLAUDE_PID', got)

    def test_runner_uses_process_pinned_bridge_bundle(self):
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
        connector = mock.Mock(node_binary='node', native_cwd='/tmp', store_root=root.name,
                              connector_version=CLAUDE_CODEC_VERSION)
        connector.binding = {'binding_id':'binding'}
        connector.native.root = root.name
        runner = ClaudeRunner(connector)
        source = Path(__file__).resolve().parents[1] / 'lib/shepherd/claude_runner.mjs'
        self.assertNotEqual(Path(runner.bridge).parent, source.parent)
        self.assertEqual(Path(runner.bridge).read_bytes(), source.read_bytes())
        assert_private_file(runner.bridge, 0o500)
        self.assertTrue((Path(runner.bridge).parent/'claude_sqlite_store.mjs').is_file())

    def test_runner_process_receives_clean_parent_environment(self):
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
        connector = mock.Mock(node_binary='node', native_cwd='/tmp', store_root=root.name,
                              connector_version=CLAUDE_CODEC_VERSION)
        connector.binding = {'binding_id':'binding','conversation_id':'conversation'}
        connector.native.load.return_value = []
        connector.native.path = os.path.join(root.name, 'native.sqlite3')
        connector.native.root = root.name
        connector.native_session_id = '11111111-2222-4333-8444-555555555555'
        connector.claude_binary = '/tmp/claude'
        connector.sdk_root = '/tmp/sdk'
        connector._available.return_value = None
        runner = ClaudeRunner(connector)
        process = mock.Mock()
        process.events.return_value = iter([])
        inherited = {
            'CLAUDE_CONFIG_DIR':'/tmp/parent-turn',
            'CLAUDE_CODE_MESSAGING_TOKEN':'expired',
            'ANTHROPIC_API_KEY':'configured-provider',
        }
        with mock.patch.dict(os.environ, inherited, clear=False):
            with mock.patch('shepherd.runners.NativeProcess', return_value=process) as native:
                with self.assertRaisesRegex(Conflict, '执行失败'):
                    runner.run(prompt='q', round_id='r',
                               on_dispatched=lambda **_kwargs: None)
        env = native.call_args.kwargs['env']
        self.assertNotIn('CLAUDE_CONFIG_DIR', env)
        self.assertNotIn('CLAUDE_CODE_MESSAGING_TOKEN', env)
        self.assertEqual(env['ANTHROPIC_API_KEY'], 'configured-provider')
        process.begin_claude_startup.assert_called_once_with()
        payload_path = native.call_args.args[0][-1]
        # The temporary payload is removed after run(); inspect the bridge source
        # and constructor contract instead of depending on a deleted file.
        self.assertFalse(os.path.exists(payload_path))
        bridge = Path(runner.bridge).read_text(encoding='utf-8')
        self.assertIn("CLAUDE_CODE_RETRY_WATCHDOG:'1'", bridge)
        self.assertIn("retryBudgetExpired", bridge)
        source = Path(__file__).resolve().parents[1] / 'lib/shepherd/runners.py'
        self.assertIn("'retryBudgetMs': 60 * 60 * 1000", source.read_text(encoding='utf-8'))

    def test_retry_budget_event_is_forwarded_without_starting_another_query(self):
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
        connector = mock.Mock(node_binary='node', native_cwd='/tmp', store_root=root.name,
                              connector_version=CLAUDE_CODEC_VERSION)
        connector.binding = {'binding_id':'binding','conversation_id':'conversation'}
        connector.native.load.return_value = []
        connector.native.path = os.path.join(root.name, 'native.sqlite3')
        connector.native.root = root.name
        connector.native_session_id = '11111111-2222-4333-8444-555555555555'
        connector.claude_binary = '/tmp/claude'; connector.sdk_root = '/tmp/sdk'
        connector._available.return_value = None
        events = []
        runner = ClaudeRunner(connector, on_event=events.append)
        runner.receipts = mock.Mock()
        process = mock.Mock(proc=mock.Mock())
        process.events.return_value = iter([
            # Missing version is an older bridge and remains one-version compatible.
            {'type':'prepared', 'startSeq':0, 'nativeRevision':''},
            {'type':'retry_budget_expired', 'retryBudgetMs':3600000},
            {'type':'result', 'terminalState':'failed_recoverable', 'startSeq':0, 'endSeq':1,
             'terminalUuid':'terminal', 'nativeRevision':'r'}])
        with mock.patch('shepherd.runners.NativeProcess', return_value=process):
            got = runner.run(prompt='q', round_id='r', on_dispatched=lambda **_kwargs: None)
        self.assertEqual(got['terminal_state'], 'failed_recoverable')
        self.assertEqual(events, [{'type':'system', 'subtype':'retry_budget_expired',
                                   'retry_budget_ms':3600000}])
        process.begin_claude_startup.assert_called_once_with()
        process.begin_claude_response.assert_called_once()
        dispatch_id = process.begin_claude_response.call_args.args[0]
        self.assertRegex(dispatch_id, r'^[0-9a-f-]{36}$')
        process.send.assert_called_once_with({'type':'start'})
        self.assertLess(process.method_calls.index(mock.call.begin_claude_response(dispatch_id)),
                        process.method_calls.index(mock.call.send({'type':'start'})))

    def test_adapter_payload_removes_parent_identity_but_keeps_worker_environment(self):
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
        work = Path(root.name) / 'work'; work.mkdir()
        adapter = ShepherdAdapter(str(Path(root.name) / 'data'), str(work), EventBus())
        binding = {'binding_id':'binding', 'engine':'claude','prompt_snapshot_policy':'claude-static-v1'}
        lease = {'lease_id':'lease', 'generation':1}
        adapter.service = mock.Mock()
        adapter.service.active.return_value = {
            'binding':binding, 'lease':lease, 'state':{'inflight':None}}
        worker = mock.Mock()
        worker.system_body.return_value = 'STATIC_STYLE'
        worker.spawn_env.return_value = {'WORKER_SETTING':'enabled'}
        worker.build_user_frame.return_value = {'type':'user'}
        adapter._worker = mock.Mock(return_value=worker)
        adapter._connector = mock.Mock(return_value=mock.Mock())
        runner = mock.Mock()
        harness = mock.Mock()
        harness.run.return_value = {'terminal_state':'completed'}
        inherited = {
            'CLAUDE_CONFIG_DIR':'/tmp/parent-turn',
            'CLAUDE_CODE_MESSAGING_TOKEN':'expired',
            'ANTHROPIC_BASE_URL':'https://configured-provider.invalid',
            'OTHER_SETTING':'preserved',
        }
        ctx = {'sid':'public', 'seq':3, 'text':'question',
               'opts':{'_grounding':'DYNAMIC_GROUNDING'}, 'saved':[]}
        with mock.patch('engine_adapters.shepherd.ClaudeRunner', return_value=runner) as create, \
             mock.patch('engine_adapters.shepherd.ExecutionHarness', return_value=harness), \
             mock.patch.object(adapter, '_note_proc') as note_proc:
            self.assertIsNone(adapter._turn_dispatch(ctx, [], inherited, None))
            on_process = create.call_args.kwargs['on_process']
            process = mock.Mock()
            on_process(process)
            note_proc.assert_called_once_with('public', process)
            self.assertIs(ctx['proc'], process)
            note_proc.reset_mock()
            ctx['aborted'] = 'interrupt'
            with self.assertRaisesRegex(Conflict, '派发前已取消'):
                on_process(mock.Mock())
            note_proc.assert_not_called()
        env = create.call_args.kwargs['env']
        self.assertNotIn('CLAUDE_CONFIG_DIR', env)
        self.assertNotIn('CLAUDE_CODE_MESSAGING_TOKEN', env)
        self.assertEqual(env['ANTHROPIC_BASE_URL'], 'https://configured-provider.invalid')
        self.assertEqual(env['OTHER_SETTING'], 'preserved')
        self.assertEqual(env['WORKER_SETTING'], 'enabled')
        self.assertEqual(env['DESKTRON_ENGINE'], 'claude')
        self.assertEqual(env['DESKTRON_PUBLIC_ENGINE'], 'shepherd')
        self.assertEqual(create.call_args.kwargs['system_text'], 'STATIC_STYLE')
        self.assertEqual(create.call_args.kwargs['options']['_grounding'], 'DYNAMIC_GROUNDING')
        self.assertNotIn('DYNAMIC_GROUNDING', create.call_args.kwargs['system_text'])

    def test_all_shepherd_engines_register_and_replace_public_app_control_root(self):
        for engine in ('claude', 'codex', 'pi'):
            with self.subTest(engine=engine):
                root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
                work = Path(root.name) / 'work'; work.mkdir()
                adapter = ShepherdAdapter(str(Path(root.name) / 'data'), str(work), EventBus())
                self.addCleanup(adapter.shutdown)
                sid = 'public-' + engine
                self.addCleanup(app_control_capability.revoke, sid)
                binding = {'binding_id':'binding-' + engine, 'engine':engine,
                           'prompt_snapshot_policy':'claude-static-v1'}
                lease = {'lease_id':'lease', 'generation':1}
                adapter.service = mock.Mock()
                adapter.service.active.return_value = {
                    'binding':binding, 'lease':lease, 'state':{'inflight':None}}
                worker = mock.Mock()
                worker.options_catalog.return_value = {'models':[]}
                worker.system_body.return_value = ''
                worker._turn_system_prompt.return_value = 'DYNAMIC_GROUNDING'
                worker.spawn_env.return_value = {}
                worker.build_user_frame.return_value = {'type':'user'}
                worker.build_cmd.return_value = ['engine']
                worker._session_roots.return_value = []
                adapter._worker = mock.Mock(return_value=worker)
                adapter._connector = mock.Mock(return_value=mock.Mock(unified=True))
                harness = mock.Mock()
                harness.run.return_value = {'terminal_state':'completed'}
                ctx = {'sid':sid, 'seq':3, 'text':'question', '_user_text':'question',
                       'opts':{'model':'fixture', '_grounding':'DYNAMIC_GROUNDING'}, 'saved':[]}
                with mock.patch('shepherd.unified_runners.UnifiedRunner') as create, \
                     mock.patch('engine_adapters.shepherd.ExecutionHarness', return_value=harness), \
                     mock.patch.object(adapter, '_env_publish'):
                    self.assertIsNone(adapter._turn_dispatch(ctx, [], {}, None))
                    on_process = create.call_args.kwargs['on_process']
                    old = mock.Mock(pid=51001); old.poll.return_value = None
                    new = mock.Mock(pid=51002); new.poll.return_value = None
                    on_process(old)
                    token = app_control_capability.issue(sid)
                    with mock.patch.object(app_control_capability, '_descends_from',
                                           side_effect=lambda peer, roots: peer in roots):
                        self.assertTrue(app_control_capability.verify(sid, token, old.pid))
                        on_process(new)
                        self.assertFalse(app_control_capability.verify(sid, token, old.pid))
                        self.assertTrue(app_control_capability.verify(sid, token, new.pid))

    def test_adapter_uses_explicit_compaction_when_codex_catalog_omits_window(self):
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
        work = Path(root.name) / 'work'; work.mkdir()
        adapter = ShepherdAdapter(str(Path(root.name) / 'data'), str(work), EventBus())
        self.addCleanup(adapter.shutdown)
        binding = {'binding_id':'binding', 'engine':'codex'}
        lease = {'lease_id':'lease', 'generation':1}
        adapter.service = mock.Mock()
        adapter.service.active.return_value = {
            'binding':binding, 'lease':lease, 'state':{'inflight':None}}
        worker = mock.Mock()
        worker.options_catalog.return_value = {'models':[{'id':'gpt-6-astra'}]}
        worker.system_body.return_value = ''
        worker._turn_system_prompt.return_value = ''
        worker.spawn_env.return_value = {}
        worker._session_roots.return_value = []
        adapter._worker = mock.Mock(return_value=worker)
        adapter._connector = mock.Mock(return_value=mock.Mock(unified=True))
        harness = mock.Mock()
        harness.run.return_value = {'terminal_state':'completed'}
        ctx = {'sid':'public', 'seq':3, 'text':'question', '_user_text':'question',
               'opts':{'model':'gpt-6-astra', 'autocompact':'400000'}, 'saved':[]}
        with mock.patch('shepherd.unified_runners.UnifiedRunner') as create, \
             mock.patch('engine_adapters.shepherd.ExecutionHarness', return_value=harness), \
             mock.patch.object(adapter,'_env_publish') as publish:
            create.return_value.degraded_context={'omitted_rounds':7}
            self.assertIsNone(adapter._turn_dispatch(ctx, [], {}, None))
            self.assertEqual(create.call_args.kwargs['options']['_context_window'], 400000)
            notice=publish.call_args.args[1]
            self.assertEqual(notice['stage'],'context_degraded')
            self.assertEqual(notice['state'],'ready')
            self.assertIn('7 个较早回合',notice['details'][0])
            create.reset_mock();publish.reset_mock()
            create.return_value.degraded_context=None
            unknown = {**ctx, 'opts':{'model':'gpt-6-astra'}}
            self.assertIsNone(adapter._turn_dispatch(unknown, [], {}, None))
            self.assertEqual(create.call_args.kwargs['options']['_context_window'], 0)
            self.assertEqual(publish.call_args.args[1],{
                'state':'ready','stage':'context_degraded'})


class CodexTerminal(unittest.TestCase):
    def runner(self):
        connector = mock.Mock(engine='codex', native_session_id='native',
                              native_cwd='/tmp', home='/tmp/private', binary='codex', store_root='/tmp')
        connector.binding = {'binding_id':'binding'}
        connector.verify.return_value = {'native_revision':'a'*64}
        runner = CodexFamilyRunner(connector)
        runner.receipts = mock.Mock()
        return runner

    def test_real_terminal_required_and_private_home_not_pooled(self):
        for status, expected in [('completed','completed'), ('failed','failed_recoverable'),
                                 ('interrupted','interrupted_confirmed'), ('inProgress',None)]:
            with self.subTest(status=status):
                runner = self.runner()
                def turn(*args, **kwargs):
                    self.assertEqual(kwargs['session_id'], '')
                    self.assertIsNone(kwargs['history_items'])
                    self.assertEqual(args[1]['CODEX_HOME'], '/tmp/private')
                    kwargs['on_turn_started']('native', 'turn')
                    kwargs['on_notification']('turn/completed', {'turn':{'id':'turn','status':status}})
                    return 'native'
                with mock.patch('codex_appserver.run_turn', side_effect=turn):
                    if expected:
                        result = runner.run(prompt='q', round_id='r', conversation_id='public')
                        self.assertEqual(result['terminal_state'], expected)
                    else:
                        with self.assertRaises(Conflict):
                            runner.run(prompt='q', round_id='r', conversation_id='public')
                        runner.receipts.write.assert_not_called()

    def test_active_writer_retries_only_before_dispatch(self):
        runner = self.runner(); calls = []
        def turn(*_args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('active writer is still closing')
            kwargs['on_turn_submitting']('native')
            kwargs['on_turn_started']('native', 'turn')
            kwargs['on_notification']('turn/completed', {
                'turn':{'id':'turn', 'status':'completed'}})
            return 'native'
        with mock.patch('codex_appserver.run_turn', side_effect=turn), \
             mock.patch('shepherd.runners.time.sleep'):
            result = runner.run(prompt='q', round_id='r', conversation_id='public',
                on_dispatched=lambda **_kw: None)
        self.assertEqual(result['terminal_state'], 'completed')
        self.assertEqual(len(calls), 2)

    def test_active_writer_after_dispatch_is_not_replayed(self):
        runner = self.runner(); dispatched = []
        def turn(*_args, **kwargs):
            kwargs['on_turn_submitting']('native')
            raise RuntimeError('active writer after turn submission')
        with mock.patch('codex_appserver.run_turn', side_effect=turn):
            with self.assertRaisesRegex(RuntimeError, 'active writer'):
                runner.run(prompt='q', round_id='r', conversation_id='public',
                    on_dispatched=lambda **kw: dispatched.append(kw))
        self.assertEqual(len(dispatched), 1)

    def test_capacity_continuation_keeps_one_dispatch_and_receipts_final_turn(self):
        runner = self.runner(); dispatched = []; located = []
        def turn(*_args, **kwargs):
            kwargs['on_turn_submitting']('native')
            kwargs['on_turn_started']('native', 'turn-capacity')
            kwargs['on_turn_started']('native', 'turn-final')
            kwargs['on_notification']('turn/completed', {
                'turn':{'id':'turn-final', 'status':'completed'}})
            return 'native'
        with mock.patch('codex_appserver.run_turn', side_effect=turn):
            result = runner.run(prompt='q', round_id='r', conversation_id='public',
                on_dispatched=lambda **kw: dispatched.append(kw),
                on_native_locator=located.append)
        self.assertEqual(result['terminal_state'], 'completed')
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(located, [{'turn_id':'turn-capacity'}, {'turn_id':'turn-final'}])
        locator = runner.receipts.write.call_args.args[2]
        self.assertEqual(locator['turn_id'], 'turn-final')

    def test_cancel_uses_own_connection_and_never_shared_session_pool(self):
        runner = self.runner(); conn = mock.Mock(); runner._connected(conn); runner._turn_started('native', 'turn')
        notified = threading.Event()
        conn.request_nowait.side_effect = lambda *args: notified.set()
        try:
            runner.cancel()
            self.assertTrue(notified.wait(2))
            conn.request_nowait.assert_called_once_with('turn/interrupt', {'threadId':'native','turnId':'turn'})
        finally:
            runner._disconnected()


class PermissionLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); work = root/'work'; work.mkdir()
        self.adapter = ShepherdAdapter(str(root/'data'), str(work), EventBus())
        self.events = []
        self.adapter.bus.publish = self.events.append
        self.adapter._live['public'] = {'proc':mock.Mock(poll=mock.Mock(return_value=None))}
        self.result = []

    def ask(self):
        self.result.append(self.adapter._ask_claude_permission('public',
            {'id':'ask-private-tool','tool':'Bash','input':{'command':'echo ok'}}))

    def start_ask(self):
        thread = threading.Thread(target=self.ask, daemon=True); thread.start()
        deadline = time.monotonic()+2
        while not self.events and time.monotonic()<deadline: time.sleep(.005)
        self.assertTrue(self.events)
        return thread

    def test_approve_updated_input_and_refresh_pending_use_public_sid(self):
        thread = self.start_ask()
        pending = self.adapter._pending_ask('public')
        self.assertEqual(pending[0]['sessionID'], 'public')
        self.assertEqual(self.adapter._pending_ask('other'), [])
        self.adapter.respond_permission('ask-private-tool', True,
            answers={'updatedInput':{'command':'echo reviewed'}})
        thread.join(2); self.assertFalse(thread.is_alive())
        self.assertEqual(self.result[0]['updatedInput'], {'command':'echo reviewed'})
        self.assertTrue(self.result[0]['allow'])
        self.assertEqual(self.adapter._pending_ask('public'), [])
        self.assertEqual(self.events[-1]['type'], 'permission.resolved')

    def test_deny_timeout_and_process_exit_fail_closed(self):
        for mode in ('deny','timeout','exit'):
            with self.subTest(mode=mode):
                self.events.clear(); self.result.clear()
                self.adapter.PERMISSION_TIMEOUT = .05 if mode == 'timeout' else 2
                thread = self.start_ask()
                if mode == 'deny': self.adapter.respond_permission('ask-private-tool', False)
                if mode == 'exit': self.adapter._live['public']['proc'].poll.return_value = 1
                thread.join(2); self.assertFalse(thread.is_alive())
                self.assertFalse(self.result[0]['allow'])
                self.assertEqual(self.adapter._shepherd_approvals, {})

    def test_late_recovery_clears_runner_stall_during_compaction(self):
        ctx = {'sid':'public','msg_id':'msg','parts':{},'order':[],'seq':7,
               'compact_t0':time.time(),'_runner_stall':{'since':time.time()-900},
               '_runner_stall_sequence':1}
        self.adapter._live['public'] = ctx
        with mock.patch.object(self.adapter,'_checkpoint_live') as checkpoint:
            self.adapter._observe_runner_stall(ctx,{'active':False,'sequence':2})
            self.adapter._observe_runner_stall(ctx,{'active':True,'sequence':1,'seconds':901})
        self.assertNotIn('_runner_stall',ctx)
        self.assertEqual(ctx['_runner_stall_sequence'],2)
        checkpoint.assert_called_once_with(ctx,force=True)

    def test_first_response_stall_phase_is_preserved_in_state(self):
        ctx = {'sid':'public','msg_id':'msg','parts':{},'order':[],'seq':8}
        self.adapter._live['public'] = ctx
        with mock.patch.object(self.adapter,'_checkpoint_live'):
            self.adapter._observe_runner_stall(ctx,{'active':True,'sequence':1,'seconds':60,
                'activity':{'kind':'no_progress','phase':'first_response'}})
        self.assertEqual(ctx['_runner_stall']['phase'],'first_response')
        self.assertEqual(self.adapter.turn_activity('public',ctx)['phase'],'first_response')

    def test_concurrent_stall_projection_cannot_overwrite_newer_recovery(self):
        ctx = {'sid':'public','msg_id':'msg','parts':{},'order':[],'seq':8}
        self.adapter._live['public'] = ctx
        entered=threading.Event();release=threading.Event();snapshots=[]
        def checkpoint(_ctx,force=False):
            snapshots.append((_ctx.get('_runner_stall_sequence'),bool(_ctx.get('_runner_stall'))))
            if _ctx.get('_runner_stall_sequence') == 1:
                entered.set();release.wait(2)
        with mock.patch.object(self.adapter,'_checkpoint_live',side_effect=checkpoint):
            first=threading.Thread(target=self.adapter._observe_runner_stall,
                args=(ctx,{'active':True,'sequence':1,'seconds':900}),daemon=True)
            second=threading.Thread(target=self.adapter._observe_runner_stall,
                args=(ctx,{'active':False,'sequence':2}),daemon=True)
            first.start();self.assertTrue(entered.wait(1));second.start()
            time.sleep(.03);self.assertTrue(second.is_alive())
            release.set();first.join(1);second.join(1)
        self.assertFalse(first.is_alive());self.assertFalse(second.is_alive())
        self.assertNotIn('_runner_stall',ctx)
        self.assertEqual(ctx['_runner_stall_sequence'],2)
        self.assertEqual(snapshots,[(1,True),(2,False)])

    def test_finish_never_persists_private_native_sid(self):
        ctx = {'esid':'private-native-session'}
        with mock.patch('engine_adapters.cli.CliAdapter._finish_turn') as finish:
            self.adapter._finish_turn('public', ctx, None)
        self.assertNotIn('esid', finish.call_args.args[1])

    def test_invalid_image_is_rejected_before_accepting_turn(self):
        with mock.patch('engine_adapters.cli.CliAdapter.prompt_async') as prompt:
            with self.assertRaises(InvalidState):
                self.adapter.prompt_async('public', 'question', atts=[{
                    'type':'image','data_b64':'invalid','media_type':'image/png'}])
        prompt.assert_not_called()

    def test_private_sid_never_enters_live_or_checkpoint_and_commit_stays_busy(self):
        ctx = {'sid':'public', 'esid':'private-native-session', 'msg_id':'msg',
               'parts':{}, 'order':[], 'proc':mock.Mock(poll=mock.Mock(return_value=0))}
        self.adapter._live['public'] = ctx
        snapshot = self.adapter.live_message('public')
        self.assertNotIn('engine_sid', snapshot['info'])
        self.assertIs(self.adapter._live['public'], ctx)
        with mock.patch('engine_adapters.cli.CliAdapter._checkpoint_live') as save:
            self.adapter._checkpoint_live(ctx, force=True)
        self.assertNotIn('esid', save.call_args.args[0])
        self.assertEqual(ctx['esid'], 'private-native-session')


if __name__ == '__main__': unittest.main()
