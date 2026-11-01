"""Missing terminal frames never authorize success, replay or lock release."""
import copy
import os
import queue
import sys
import tempfile
import threading
import unittest
from unittest import mock

from test_codex_appserver import app, CATALOG, FakeConnection
from test_native_lifecycle_projection import NativeRetryProjection
from test_engine_adapters_gui import ea
from shepherd.runners import PiRunner
from shepherd.unified_runners import UnifiedRunner
from shepherd.store import Conflict


class TerminalReadback(unittest.TestCase):
    def run_frames(self, turns, *, waiting=False, strict=False, readback_error=False):
        seen = []
        class Engine(FakeConnection):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.n = 0
                self._awaiting = int(waiting)
            def request(self, method, *args, **kwargs):
                if method == 'thread/read':
                    self.calls.append((method, args[0]))
                    if readback_error: raise app.AppServerTimeout('probe expired')
                    return {'thread': {'id':'thread-1', 'turns':copy.deepcopy(turns)}}
                return super().request(method, *args, **kwargs)
            def read(self, timeout=30):
                self.n += 1
                if self.n == 1:
                    return {'method':'item/completed','params':{'threadId':'thread-1',
                        'turnId':'turn-1','item':{'type':'agentMessage','phase':'final_answer'}}}
                if self.n == 2: raise app.AppServerTimeout('no terminal frame')
                self._awaiting = 0
                return {'method':'turn/completed','params':{'threadId':'thread-1',
                    'turn':{'id':'turn-1','status':'completed'}}}
        with mock.patch.object(app, '_Connection', Engine), mock.patch.object(app, 'catalog', return_value=CATALOG):
            app.run_turn('/tmp', {}, '', 'test', {}, timeout=1, strict_boundary=strict,
                         on_notification=lambda m,p:seen.append((m,p)))
        return seen, Engine.last

    def test_in_progress_or_active_successor_does_not_synthesize_completion(self):
        for rows in ([{'id':'turn-1','status':'inProgress'}],
                     [{'id':'turn-1','status':'completed'},{'id':'turn-2','status':'inProgress'}]):
            seen, conn = self.run_frames(rows)
            self.assertEqual(conn.n, 3)
            self.assertNotIn('terminalSource', seen[-1][1])

    def test_confirmed_readback_has_explicit_source(self):
        seen, conn = self.run_frames([{'id':'turn-1','status':'completed'}])
        self.assertEqual(conn.n, 2)
        self.assertEqual(seen[-1][1]['terminalSource'], 'thread/read')
        self.assertEqual(sum(m == 'turn/start' for m,_ in conn.calls), 1)

    def test_failed_readback_is_failure_not_success(self):
        with self.assertRaises(app.AppServerError):
            self.run_frames([{'id':'turn-1','status':'failed','error':{'message':'native failure'}}])

    def test_unavailable_or_wrong_turn_returns_diagnostic_without_replay(self):
        for error in (False, True):
            with self.assertRaisesRegex(app.AppServerError, 'PROTOCOL'):
                self.run_frames([{'id':'old-turn','status':'completed'}], readback_error=error)
            self.assertEqual(sum(m == 'turn/start' for m,_ in FakeConnection.last.calls), 1)
            self.assertEqual(FakeConnection.last.closed, 1)

    def test_pending_approval_and_strict_mode_do_not_use_silence(self):
        for kwargs in ({'waiting':True}, {'strict':True}):
            seen, conn = self.run_frames([{'id':'turn-1','status':'completed'}], **kwargs)
            self.assertEqual(conn.n, 3)
            self.assertNotIn('thread/read', [m for m,_ in conn.calls])


class MaintenanceProjection(unittest.TestCase):
    setUp = NativeRetryProjection.setUp
    make = NativeRetryProjection.make
    start = NativeRetryProjection.start

    def test_engines_compaction_snapshots_match_and_do_not_settle(self):
        for cls in (ea.PiAdapter, ea.ClaudeAdapter, ea.CodexAdapter):
            ad, ctx, bus = self.make(cls)
            if cls is ea.PiAdapter:
                emit = lambda end:ad.handle_event({'type':'compaction_end' if end else 'compaction_start',
                    **({'result':{'summary':'test'}} if end else {'reason':'threshold'})},ctx)
            elif cls is ea.ClaudeAdapter:
                emit = lambda end:ad.handle_event({'type':'system','subtype':'status',
                    'status':None if end else 'compacting','compact_result':'success' if end else ''},ctx)
            else:
                emit = lambda end:ad._app_event('item/completed' if end else 'item/started',
                    {'item':{'type':'contextCompaction','id':'compact'}},ctx)
            emit(False)
            started = ad.turn_state(ctx['sid'])['compacting']
            for snap in (ad.live_message(ctx['sid']), ad._read_live_checkpoint(ctx['sid'])):
                self.assertEqual(snap['info']['compacting'], started)
            emit(True)
            self.assertNotIn('compacting', ad.turn_state(ctx['sid']))
            self.assertFalse(ad.turn_state(ctx['sid'])['compact']['running'])
            self.assertNotIn('compacting', ad._read_live_checkpoint(ctx['sid'])['info'])
            self.assertNotIn('_close_stdin',ctx)

    def test_compaction_retry_does_not_overwrite_request_retry(self):
        ad, ctx, bus = self.make(ea.PiAdapter)
        self.start(ad,ctx)
        ad.handle_event({'type':'compaction_start','reason':'overflow'},ctx)
        ad.handle_event({'type':'summarization_retry_scheduled','attempt':8,'maxAttempts':9,'delayMs':1000},ctx)
        self.assertEqual(ctx['retry']['attempt'],2)
        self.assertEqual(ad.turn_state(ctx['sid'])['retry']['attempt'],8)
        ad.handle_event({'type':'summarization_retry_finished'},ctx)
        self.assertEqual(ad.turn_state(ctx['sid'])['retry']['attempt'],2)
        self.assertNotIn('engine_error',ctx)

    def test_native_queue_has_counts_only_and_provider_queue_survives_refresh(self):
        ad, ctx, bus = self.make(ea.PiAdapter)
        ad.handle_event({'type':'queue_update','steering':['private input'], 'followUp':['next']},ctx)
        ad._pi_queue(ctx,{'phase':'queue_begin','position':3})
        for snap in (ad.turn_state(ctx['sid']),ad.live_message(ctx['sid'])['info'],
                     ad._read_live_checkpoint(ctx['sid'])['info']):
            self.assertEqual(snap['native_queue'],{'steering':1,'follow_up':1})
            self.assertEqual(snap['activity']['kind'],'provider_queue')
        ad._pi_queue(ctx,{'phase':'queue_end'})
        self.assertNotEqual(ad._read_live_checkpoint(ctx['sid'])['info']['activity']['kind'],'provider_queue')

    def test_compact_without_success_evidence_does_not_report_success(self):
        ad,ctx,bus=self.make(ea.PiAdapter)
        ad.handle_event({'type':'compaction_start'},ctx)
        ad.handle_event({'type':'compaction_end'},ctx)
        state=ad.turn_state(ctx['sid'])['compact']
        self.assertEqual(state['result'],'failed')
        self.assertNotIn('engine_error',ctx)

    def test_live_get_after_process_exit_does_not_release_turn(self):
        ad,ctx,_=self.make(ea.PiAdapter)
        ctx['proc']=mock.Mock();ctx['proc'].poll.return_value=1
        self.assertTrue(ad.live_message(ctx['sid'])['info']['running'])
        self.assertIs(ad._live[ctx['sid']],ctx)

    def test_restart_does_not_resurrect_queue_retry_or_compaction(self):
        ad,ctx,_=self.make(ea.PiAdapter)
        self.start(ad,ctx)
        ad.handle_event({'type':'compaction_start'},ctx)
        ad._live.clear()
        row=ad.live_message(ctx['sid'])
        self.assertTrue(row['info']['interrupted'])
        self.assertFalse(row['info']['running'])
        for key in ('retry','activity','compacting','native_queue'):
            self.assertNotIn(key,row['info'])

    def test_real_stdio_eof_needs_native_settlement(self):
        for cls, terminal in ((ea.PiAdapter, {'type':'agent_settled'}),
                              (ea.ClaudeAdapter, {'type':'result','is_error':False})):
            for finish in (False,True):
                ad,ctx,_=self.make(cls)
                ad.session_cwd=lambda _:self.tmp.name
                ad._note_proc=mock.Mock()
                code='import sys,json;sys.stdin.readline();'
                if finish:code+='print('+repr(__import__('json').dumps(terminal))+',flush=True)'
                result=ad._turn_oneshot(ctx,[sys.executable,'-u','-c',code],dict(os.environ),{'type':'prompt'})
                if finish:self.assertIsNone(result)
                else:self.assertEqual(ctx['engine_failure']['code'],'NATIVE_TERMINAL_MISSING')
                self.assertEqual(ctx['proc'].poll(),0)

    def test_accepted_or_partially_written_request_is_not_resent(self):
        for flush_error in (False,True):
            ad,ctx,_=self.make(ea.PiAdapter)
            ep=mock.Mock()
            ep.lines=queue.Queue();ep.lines.put(None)
            ep.proc.poll.return_value=0
            ep.tail_err.return_value=''
            if flush_error:ep.proc.stdin.flush.side_effect=BrokenPipeError('closed')
            ad.persist_on=lambda:True
            ad._persist_get=mock.Mock(return_value=ep)
            ad._persist_take=mock.Mock(return_value=True)
            ad._turn_oneshot=mock.Mock()
            with mock.patch('engine_adapters.session_naming.sync_pipe'), \
                 mock.patch('engine_adapters.cli_turn._persist_drop'), \
                 mock.patch('engine_adapters.cli_turn._TurnWatch'):
                self.assertTrue(ad._turn_dispatch(ctx,['pi'],{}, {'type':'prompt','message':'once'}))
            ad._turn_oneshot.assert_not_called()
            ep.proc.stdin.write.assert_called_once()
            ep.reader_lock.release.assert_called_once()

    def test_outcome_is_persisted_before_ready_is_visible(self):
        ad,ctx,bus=self.make(ea.CodexAdapter)
        ad.index[ctx['sid']]={}
        ctx['native_terminal']={'source':'turn/completed','status':'interrupted'}
        seen=[]
        append=ad._append_msg
        def capture(sid,row):
            seen.append(ad.turn_state(sid)['state'])
            append(sid,row)
        ad._append_msg=capture
        ad._bg_publish=mock.Mock();ad._recap_arm=mock.Mock()
        ad._finish_turn(ctx['sid'],ctx,None)
        self.assertEqual(seen,['running'])
        row=ad.messages(ctx['sid'])[-1]['info']
        self.assertTrue(row['interrupted'])
        self.assertEqual(row['terminal']['status'],'interrupted')
        self.assertEqual(ad.turn_state(ctx['sid'])['state'],'ready')
        self.assertTrue(next(e['properties'] for e in bus.events if e['type']=='session.idle')['interrupted'])


class PiRunnerAuthority(unittest.TestCase):
    def test_both_runners_require_settled_even_after_failed_or_aborted_message(self):
        for unified in (False,True):
            for stop in ('error','aborted','stop'):
                with self.subTest(unified=unified,stop=stop), tempfile.TemporaryDirectory() as root:
                    connector=mock.Mock(store_root=root,native_cwd=root,path=root+'/native')
                    connector.binding={'binding_id':'test','conversation_id':'test'}
                    process=mock.Mock()
                    process.events.return_value=iter([{'type':'message_end','message':{
                        'role':'assistant','stopReason':stop}}])
                    if unified:
                        runner=UnifiedRunner(connector,command=['pi'])
                        runner._native_process=mock.Mock(return_value=process)
                        runner._payload=mock.Mock(return_value='payload')
                        runner.round_id='round'
                        process.events.return_value=iter([{'type':'protocol_ready',
                            'contract':'desk-pi-execute-v1','sessionId':connector.native_session_id},
                            {'type':'message_end','message':{'role':'assistant','stopReason':stop}}])
                        with mock.patch('shepherd.pi_connector.pi_sdk_root',return_value='/sdk'), \
                             mock.patch('shepherd.pi_connector.PiConnector.availability', return_value={'available':True}):
                            with self.assertRaisesRegex(Conflict,'settled'):
                                runner._pi('test',[],None,lambda **_:None,None)
                    else:
                        runner=PiRunner(connector,command=['pi'])
                        with mock.patch('shepherd.runners.NativeProcess',return_value=process):
                            with self.assertRaisesRegex(Conflict,'settled'):
                                runner.run(prompt='test',round_id='round',on_dispatched=lambda **_:None)
                    process.close.assert_called_once()
