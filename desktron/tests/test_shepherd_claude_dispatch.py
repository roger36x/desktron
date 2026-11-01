"""Claude local handoff / first-progress guards, without online model requests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron/lib'))

from shepherd.execution_budget import ExecutionDeadline
from shepherd.runner_process import NativeProcess
from shepherd.resident_process import ResidentProcess
from shepherd.phase_timing import PhaseTiming
from shepherd.unified_runners import UnifiedRunner, _claude_project_resources_digest

DISPATCH = '11111111-2222-4333-8444-555555555555'


class ClaudeDispatchBudget(unittest.TestCase):
    def setUp(self):
        self.now = 0.
        self.budget = ExecutionDeadline(900, 7200, clock=lambda: self.now)

    def start(self):
        self.budget.begin_claude_response(DISPATCH)

    def phase(self, stage, dispatch=DISPATCH):
        return self.budget.touch({'type': 'claude_dispatch', 'dispatch_id': dispatch, 'stage': stage})

    def test_handoff_without_any_events_is_bounded(self):
        self.start()
        self.assertEqual(self.budget.next_deadline(), (30, 'claude_handoff'))
        self.assertIn('SDK', self.budget.timeout_message('claude_handoff'))

    def test_stderr_and_stage_replays_cannot_renew_handoff(self):
        self.start()
        for tick in (5, 10, 20, 29):
            self.now = tick
            self.phase('start_received')
            self.budget.touch()
        self.assertEqual(self.budget.next_deadline(), (30, 'claude_handoff'))

    def test_expired_handoff_cannot_be_revived_by_late_ack_or_text(self):
        for tick in (30, 31):
            with self.subTest(tick=tick):
                self.setUp();self.start();self.now=tick
                self.phase('input_consumed')
                self.budget.touch({'type':'progress'})
                self.assertEqual(self.budget.next_deadline(),(30,'claude_handoff'))
                self.assertEqual(self.budget.claude_dispatch_stage,'start_sent')
                self.assertIsNone(self.budget.last_progress)

    def test_delayed_input_consumption_gets_full_notice_and_silence_windows(self):
        self.start(); self.now = 29; self.phase('input_consumed')
        self.assertEqual(self.budget._stall_deadline(), 89)
        self.assertEqual(self.budget.wait_deadline, 629)

    def test_consumed_input_warns_at_one_minute_and_times_out_after_ten(self):
        self.start()
        self.now = 4
        self.phase('input_consumed')
        self.assertEqual(self.budget.next_deadline(), (64, 'idle'))
        self.now = 63
        self.assertIsNone(self.budget.mark_soft_stall())
        self.now = 64
        notice = self.budget.mark_soft_stall()
        self.assertEqual(notice['activity']['phase'], 'first_response')
        for tick in (70, 200, 599):
            self.now = tick
            self.phase('input_consumed')
            self.phase('stderr_network')
            self.budget.touch({'type': 'event', 'event': {'type': 'system', 'subtype': 'api_retry'}})
        self.assertEqual(self.budget.next_deadline(), (604, 'claude_response'))
        self.assertIsNone(self.budget.last_progress)

    def test_provider_response_starts_fresh_ten_minute_silence_window(self):
        self.start(); self.phase('input_consumed')
        self.now = 60; self.assertIsNotNone(self.budget.mark_soft_stall())
        self.now = 70; self.assertTrue(self.phase('provider_response_started'))
        self.assertFalse(self.budget.soft_stalled)
        self.assertEqual(self.budget.next_deadline(), (670, 'provider_wait'))
        self.assertIsNone(self.budget.last_progress)

    def test_each_native_retry_refreshes_ten_minute_silence_window(self):
        self.start(); self.phase('input_consumed')
        for tick in (10, 500, 1000):
            self.now = tick; self.phase('provider_retry')
            self.assertEqual(self.budget.wait_deadline, tick + 600)
        self.now = 1060; self.assertIsNone(self.budget.mark_soft_stall())
        self.assertEqual(self.budget.next_deadline(), (1600, 'provider_wait'))
        self.assertIsNone(self.budget.last_progress)

    def test_init_and_wrong_or_late_provider_evidence_cannot_revive_wait(self):
        self.start(); self.phase('input_consumed')
        self.now = 60; self.budget.mark_soft_stall()
        self.budget.touch({'type':'event','event':{'type':'system','subtype':'init'}})
        self.phase('provider_response_started', 'stale')
        self.assertEqual(self.budget.next_deadline(), (600, 'claude_response'))
        self.now = 600; self.phase('provider_retry')
        for event in ({'type':'progress'}, {'type':'permission'}, {'type':'compaction_start'},
                      {'type':'result'}):
            self.budget.touch(event)
        self.assertEqual(self.budget.next_deadline(), (600, 'claude_response'))
        self.assertIsNone(self.budget.last_progress)
        self.assertIn('不必开放完全磁盘访问', self.budget.timeout_message('claude_response'))

    def test_startup_is_bounded_before_dispatch_even_for_compact(self):
        for operation in ('turn', 'compact'):
            with self.subTest(operation=operation):
                self.setUp()
                budget = ExecutionDeadline(900, 7200, operation=operation, clock=lambda:self.now)
                budget.begin_claude_startup()
                self.assertEqual(budget.next_deadline(), (30, 'claude_startup'))
                self.now = 29
                budget.touch({'type':'system','subtype':'init'})
                self.now = 30
                with self.assertRaisesRegex(ValueError, '初始化超时'):
                    budget.begin_claude_response(DISPATCH)
                self.assertEqual(budget.next_deadline(), (30, 'claude_startup'))

    def test_prepared_transitions_startup_to_handoff_without_stale_timer(self):
        self.budget.begin_claude_startup(); self.now = 20; self.start()
        self.assertIsNone(self.budget.claude_startup_deadline)
        self.assertEqual(self.budget.next_deadline(), (50, 'claude_handoff'))
        self.now = 22; self.phase('input_consumed')
        self.now = 31
        self.assertEqual(self.budget.claude_timeout_kind(), '')
        self.assertEqual(self.budget.wait_deadline, 622)
        self.assertEqual(self.budget._stall_deadline(), 82)

    def test_wrong_identity_unknown_and_reordered_stages_are_not_evidence(self):
        self.start()
        self.phase('input_consumed', 'stale')
        self.phase('provider_received')
        self.phase('store_ready')
        self.phase('start_received')
        self.assertEqual(self.budget.next_deadline(), (30, 'claude_handoff'))
        self.assertEqual(self.budget.observation()['claude_dispatch_stage'], 'store_ready')

    def test_real_semantic_progress_restarts_consecutive_silence_window(self):
        self.start()
        self.now = 4
        self.phase('input_consumed')
        self.now = 64
        self.budget.mark_soft_stall()
        self.now = 65
        self.assertTrue(self.budget.touch({'type': 'event', 'event': {
            'type': 'stream_event', 'event': {'type': 'content_block_delta',
            'delta': {'type': 'thinking_delta', 'thinking': 'fixture'}}}}))
        self.now = 70
        self.phase('input_consumed')
        self.assertEqual(self.budget.next_deadline(), (665, 'provider_wait'))
        self.now = 664
        self.assertEqual(self.budget.claude_timeout_kind(), '')
        self.now = 665
        self.assertEqual(self.budget.claude_timeout_kind(), 'provider_wait')

    def test_compaction_and_permissions_restart_consecutive_silence_window(self):
        for event in ({'type': 'compaction_start'}, {'type': 'permission'},
                      {'type': 'tool_execution_start', 'toolCallId': 'tool'}):
            with self.subTest(event=event):
                self.setUp(); self.start(); self.phase('input_consumed')
                self.now = 10; self.budget.touch(event)
                self.assertEqual(self.budget.wait_deadline, 610)
                self.assertEqual(self.budget.wait_kind, 'provider_wait')
        compact = ExecutionDeadline(900, 7200, operation='compact', clock=lambda: 0)
        compact.begin_claude_response(DISPATCH)
        self.assertEqual(compact.next_deadline(), (7200, 'hard'))

    def test_first_wait_is_always_capped_by_hard_limit(self):
        self.budget = ExecutionDeadline(900, 5, clock=lambda: self.now)
        self.start(); self.phase('input_consumed')
        self.assertLessEqual(self.budget.next_deadline()[0], 5)

    def test_second_turn_gets_new_wait_but_default_engines_are_unchanged(self):
        self.assertEqual(self.budget.next_deadline(), (900, 'idle'))
        self.start(); self.phase('input_consumed')
        self.budget.touch({'type': 'progress'})
        self.now = 100
        fresh = ExecutionDeadline(900, 7200, clock=lambda: self.now)
        fresh.begin_claude_response('22222222-2222-4333-8444-555555555555')
        self.assertEqual(fresh.next_deadline(), (130, 'claude_handoff'))


class ClaudeProjectResources(unittest.TestCase):
    def test_settings_claude_md_and_skill_changes_rotate_resident_key(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); settings=root/'.claude/settings.json'; settings.parent.mkdir()
            settings.write_text('{}',encoding='utf-8')
            first=_claude_project_resources_digest(root)
            settings.write_text('{"hooks":{}}',encoding='utf-8')
            second=_claude_project_resources_digest(root)
            skill=root/'.claude/skills/fixture/SKILL.md';skill.parent.mkdir(parents=True)
            skill.write_text('fixture',encoding='utf-8')
            third=_claude_project_resources_digest(root)
            (root/'CLAUDE.md').write_text('rules',encoding='utf-8')
            fourth=_claude_project_resources_digest(root)
        self.assertEqual(len({first,second,third,fourth}),4)


class ClaudeDispatchProcesses(unittest.TestCase):
    def test_resident_and_disposable_hosts_enforce_wait_without_replay(self):
        code = 'import sys,time\nfor line in sys.stdin: time.sleep(1)\n'
        for resident in (False, True):
            with self.subTest(resident=resident):
                kwargs = dict(cwd=tempfile.gettempdir(), env=os.environ.copy(), cancel_frame={'type': 'cancel'})
                if resident:
                    process = ResidentProcess([sys.executable, '-S', '-u', '-c', code], **kwargs)
                    process.begin(timeout=.04, hard_timeout=2)
                else:
                    process = NativeProcess([sys.executable, '-S', '-u', '-c', code], timeout=.04,
                                            hard_timeout=2, **kwargs)
                try:
                    process.begin_claude_response(DISPATCH)
                    process.send({'type': 'start'})
                    started = time.monotonic()
                    with self.assertRaisesRegex(Exception, 'SDK'):
                        list(process.events())
                    self.assertLess(time.monotonic() - started, 1.5)
                finally:
                    process.close()

    def test_startup_watchdog_works_without_consumer_and_rejects_late_prepared(self):
        code = 'import time;time.sleep(30)'
        for resident in (False, True):
            with self.subTest(resident=resident):
                kwargs = dict(cwd=tempfile.gettempdir(), env=os.environ.copy(), cancel_frame={'type':'cancel'})
                if resident:
                    process = ResidentProcess([sys.executable,'-S','-u','-c',code], **kwargs)
                    process.begin(timeout=.05, hard_timeout=2)
                else:
                    process = NativeProcess([sys.executable,'-S','-u','-c',code],
                                            timeout=.05, hard_timeout=2, **kwargs)
                try:
                    process.begin_claude_startup()
                    process.proc.wait(timeout=4)
                    with self.assertRaisesRegex(Exception, '初始化超时'):
                        list(process.events())
                finally: process.close()

    def test_resident_hard_watch_survives_progress_and_blocked_consumer(self):
        code = 'import time;time.sleep(30)'
        process = ResidentProcess([sys.executable,'-S','-u','-c',code],
            cwd=tempfile.gettempdir(),env=os.environ.copy(),cancel_frame={'type':'cancel'})
        self.addCleanup(process.close)
        process.begin(timeout=1,hard_timeout=.6)
        process.begin_claude_startup(); process.begin_claude_response(DISPATCH)
        process._touch({'type':'progress'})
        self.assertEqual(process.budget.wait_kind, 'claude_handoff')
        # No events() calls: callbacks/consumer cannot disarm the absolute limit.
        process.proc.wait(timeout=4)
        with self.assertRaisesRegex(Exception, '最长时限'): process.finish(raise_timeout=True)
        with self.assertRaisesRegex(Exception, '最长时限'): process.begin(timeout=1,hard_timeout=2)

    def test_resident_full_pipe_after_first_progress_is_still_bounded(self):
        process = ResidentProcess([sys.executable,'-S','-u','-c','import time;time.sleep(30)'],
            cwd=tempfile.gettempdir(),env=os.environ.copy(),cancel_frame={'type':'cancel'})
        self.addCleanup(process.close)
        process.begin(timeout=1,hard_timeout=.6); process.begin_claude_response(DISPATCH)
        process._touch({'type':'progress'})
        done = threading.Event()
        def write():
            try: process.send({'type':'fixture','text':'x'*4000000})
            except (OSError,ValueError): pass
            finally: done.set()
        threading.Thread(target=write,daemon=True).start()
        process.proc.wait(timeout=4)
        self.assertTrue(done.wait(1))
        with self.assertRaisesRegex(Exception, '最长时限'): list(process.events())

    def test_old_hard_watch_does_not_kill_a_new_resident_turn(self):
        process = ResidentProcess([sys.executable,'-S','-u','-c','import time;time.sleep(30)'],
            cwd=tempfile.gettempdir(),env=os.environ.copy(),cancel_frame={'type':'cancel'})
        self.addCleanup(process.close)
        process.begin(timeout=1,hard_timeout=.4); process.begin_claude_response(DISPATCH)
        process._touch({'type':'progress'}); process.finish(raise_timeout=True)
        process.begin(timeout=1,hard_timeout=3)
        process.begin_claude_response('22222222-2222-4333-8444-555555555555')
        process._touch({'type':'progress'})
        threading.Event().wait(.65)  # Beyond the old turn's absolute deadline.
        self.assertIsNone(process.proc.poll())
        process.finish(raise_timeout=True)

    def test_finish_rejects_hard_expiry_before_watchdog_is_scheduled(self):
        process = ResidentProcess([sys.executable,'-S','-u','-c','import time;time.sleep(30)'],
            cwd=tempfile.gettempdir(),env=os.environ.copy(),cancel_frame={'type':'cancel'})
        self.addCleanup(process.close)
        process.begin(timeout=5,hard_timeout=2)
        with mock.patch.object(process, '_arm_claude_watch'):
            process.begin_claude_response(DISPATCH)
        process._touch({'type':'claude_dispatch','dispatch_id':DISPATCH,'stage':'input_consumed'})
        process._touch({'type':'permission'})  # Progress renews only to the hard limit.
        process.budget.clock = lambda: process.budget.hard_deadline
        with self.assertRaisesRegex(Exception, '最长时限'): process.finish(raise_timeout=True)
        self.assertTrue(process._timed_out)

    def test_same_resident_reuse_rejects_prior_turn_diagnostics(self):
        code=('import json,sys\nfor line in sys.stdin:\n'
              ' row=json.loads(line);progress=row.pop("progress",False)\n'
              ' print(json.dumps(row),flush=True)\n'
              ' if progress:print(json.dumps({"type":"progress"}),flush=True)\n')
        process=ResidentProcess([sys.executable,'-S','-u','-c',code],
            cwd=tempfile.gettempdir(),env=os.environ.copy(),cancel_frame={'type':'cancel'})
        self.addCleanup(process.close)
        process.begin(timeout=2,hard_timeout=10);process.begin_claude_response(DISPATCH)
        process.send({'type':'claude_dispatch','dispatch_id':DISPATCH,'stage':'input_consumed','progress':True})
        events=process.events();next(events);self.assertEqual(next(events)['type'],'progress');process.finish()
        current='22222222-2222-4333-8444-555555555555'
        process.begin(timeout=2,hard_timeout=10);process.begin_claude_response(current);events=process.events()
        for stage in ('input_consumed','first_native_event','stderr_auth'):
            process.send({'type':'claude_dispatch','dispatch_id':DISPATCH,'stage':stage});next(events)
            self.assertEqual(process.budget.wait_kind,'claude_handoff')
            self.assertEqual(process.budget.claude_dispatch_stage,'start_sent')
            self.assertEqual(process.budget.claude_stderr_category,'')
        process.send({'type':'claude_dispatch','dispatch_id':current,'stage':'input_consumed','progress':True})
        next(events);self.assertEqual(next(events)['type'],'progress')
        process.finish(raise_timeout=True)
        self.assertEqual(process.budget.claude_dispatch_stage,'input_consumed')

    def test_resident_full_control_pipe_cannot_freeze_dispatch_guard(self):
        process=ResidentProcess([sys.executable,'-S','-u','-c','import time;time.sleep(30)'],
            cwd=tempfile.gettempdir(),env=os.environ.copy(),cancel_frame={'type':'cancel'})
        self.addCleanup(process.close);process.begin(timeout=.05,hard_timeout=2)
        process.begin_claude_response(DISPATCH)
        done=threading.Event()
        def write():
            try:process.send({'type':'start','fixture':'x'*4000000})
            except (OSError,ValueError):pass
            finally:done.set()
        writer=threading.Thread(target=write,daemon=True);writer.start()
        process.proc.wait(timeout=3)
        self.assertTrue(done.wait(1))
        with self.assertRaisesRegex(Exception,'SDK'):list(process.events())
        with self.assertRaisesRegex(Exception,'SDK'):process.finish(raise_timeout=True)
        with self.assertRaisesRegex(Exception,'SDK'):process.begin(timeout=1,hard_timeout=2)

    def test_silent_sdk_harness_preserves_unknown_input_without_replay(self):
        from _platform import version_cli
        from shepherd.service import Service
        from shepherd.harness import ExecutionHarness
        from shepherd.claude_connector import ClaudeConnector
        from shepherd.resident_process import drop
        for resident in (False, True):
            for consume in (False, True):
                with self.subTest(resident=resident, consume=consume), tempfile.TemporaryDirectory() as folder:
                    root=Path(folder);work=root/'work';work.mkdir();sdk=root/'sdk';sdk.mkdir()
                    calls=root/'queries'
                    source='''
import {appendFile} from 'node:fs/promises';
export function query({prompt,options}) {
 return {initializationResult:async()=>({commands:[],models:[]}),
 request:async()=>({subtype:'success',response:{version:'2.1.283'}}),
 interrupt:async()=>{},return:async()=>{},
 async *[Symbol.asyncIterator]() {
  if(typeof prompt==='string') {
   await options.sessionStore.append({sessionId:options.sessionId},[{type:'attachment',
    uuid:'11111111-2222-4333-8444-555555555555',sessionId:options.sessionId,
    attachment:{type:'prompt_snapshot',systemPrompt:[options.systemPrompt.prompt]}}]);return;
  }
  await appendFile(%s,'query\\n');
  if(%s)for await(const frame of prompt){await new Promise(()=>{});}
  await new Promise(()=>{});
 }};
}
''' % (json.dumps(str(calls)), 'true' if consume else 'false')
                    (sdk/'sdk.mjs').write_text(source,encoding='utf-8')
                    binary=version_cli(root/'claude','2.1.283')
                    env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','SYSTEMROOT','WINDIR')}
                    env.update(DESK_SHEPHERD_CLAUDE_BINARY=str(binary),DESK_SHEPHERD_CLAUDE_SDK_ROOT=str(sdk))
                    with mock.patch.object(ClaudeConnector,'availability',return_value={'available':True}), \
                         mock.patch('shepherd.resident_process.enabled',return_value=resident), \
                         mock.patch('shepherd.execution_budget._CLAUDE_HANDOFF_SECONDS',.5), \
                         mock.patch('shepherd.execution_budget._CLAUDE_NO_PROGRESS_SECONDS',1):
                        service=Service(root/'state',storage='unified');service.create('silent','claude',work)
                        active=service.active('silent');connector=service.connector(active['binding'])
                        # Keep startup at eight seconds while the two post-dispatch
                        # waits stay sub-second/one-second. Host load can no longer
                        # make initialization race the behavior under test.
                        runner=UnifiedRunner(connector,env=env,timeout=2,options={'mode':'dontAsk'},system_text='fixture')
                        records=[];runner.phase_timing=PhaseTiming(records.append)
                        lease=active['lease'];started=time.monotonic()
                        try:
                            with self.assertRaisesRegex(Exception, 'Claude 连续无原生进展等待超时' if consume else 'SDK'):
                                ExecutionHarness(service.coordinator,connector,runner).run('silent',
                                    lease_id=lease['lease_id'],generation=lease['generation'],prompt='fixture',
                                    input_public=[{'type':'message','role':'user','content':[{'type':'input_text','text':'fixture'}]}])
                            # Native supervisor may spend up to 3s stopping the
                            # owned SDK tree after the waiting deadline expires.
                            self.assertLess(time.monotonic()-started,6)
                            raw=service.store.load_raw('silent')
                            pending=raw['active_turn']
                            self.assertEqual(pending['phase'],'dispatched')
                            self.assertEqual(pending['outcome'],'unknown')
                            self.assertEqual(pending['recovery_state'],'blocked')
                            self.assertTrue(pending['input_ref'])
                            self.assertEqual(calls.read_text(encoding='utf-8').splitlines(),['query'])
                            names={r['stage'] for r in records}
                            self.assertEqual('claude.input_consumed' in names,consume)
                            self.assertNotIn('claude.first_native_event',names)
                        finally:drop(active['binding']['binding_id'])

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'requires POSIX FIFO open')
    def test_blocked_local_open_is_bounded_before_and_after_dispatch(self):
        from _platform import version_cli
        from shepherd.service import Service
        from shepherd.harness import ExecutionHarness
        from shepherd.claude_connector import ClaudeConnector
        from shepherd.resident_process import drop
        for resident in (False, True):
            for stage in ('bootstrap', 'initialization', 'response'):
                with self.subTest(resident=resident, stage=stage), tempfile.TemporaryDirectory() as folder:
                    root=Path(folder); work=root/'work'; work.mkdir(); sdk=root/'sdk'; sdk.mkdir()
                    fifo=root/'blocked-open'; os.mkfifo(fifo)
                    calls=root/'calls'
                    source='''
import {openSync,appendFileSync} from 'node:fs';
const stage=%s, block=()=>openSync(%s,'r');
export function query({prompt,options}) {
 const expected=typeof prompt==='string' ? '[]' : '["project"]';
 if(JSON.stringify(options.settingSources)!==expected)throw new Error('settings source mismatch');
 return {initializationResult:async()=>{if(stage==='initialization')block();return {};},
 request:async()=>({subtype:'success',response:{version:'2.1.283'}}),
 interrupt:async()=>{},return:async()=>{},
 async *[Symbol.asyncIterator]() {
  if(typeof prompt==='string') {
   if(stage==='bootstrap'){appendFileSync(%s,'bootstrap\\n');block();}
   await options.sessionStore.append({sessionId:options.sessionId},[{type:'attachment',
    uuid:'11111111-2222-4333-8444-555555555555',sessionId:options.sessionId,
    attachment:{type:'prompt_snapshot',systemPrompt:[options.systemPrompt.prompt]}}]);return;
  }
  for await(const frame of prompt) {
   appendFileSync(%s,'user\\n');
   yield {type:'system',subtype:'init',claude_code_version:'2.1.283'};
   block();
  }
 }};
}
''' % (json.dumps(stage),json.dumps(str(fifo)),json.dumps(str(calls)),json.dumps(str(calls)))
                    (sdk/'sdk.mjs').write_text(source,encoding='utf-8')
                    binary=version_cli(root/'claude','2.1.283')
                    env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','SYSTEMROOT','WINDIR')}
                    env.update(DESK_SHEPHERD_CLAUDE_BINARY=str(binary),DESK_SHEPHERD_CLAUDE_SDK_ROOT=str(sdk))
                    with mock.patch.object(ClaudeConnector,'availability',return_value={'available':True}), \
                         mock.patch('shepherd.resident_process.enabled',return_value=resident):
                        service=Service(root/'state',storage='unified');service.create('blocked','claude',work)
                        active=service.active('blocked'); connector=service.connector(active['binding'])
                        runner=UnifiedRunner(connector,env=env,timeout=.4,options={'mode':'dontAsk'},system_text='fixture')
                        records=[];runner.phase_timing=PhaseTiming(records.append)
                        lease=active['lease'];started=time.monotonic()
                        try:
                            with self.assertRaisesRegex(Exception, '连续无原生进展等待超时' if stage=='response' else '初始化超时'):
                                ExecutionHarness(service.coordinator,connector,runner).run('blocked',
                                    lease_id=lease['lease_id'],generation=lease['generation'],prompt='fixture',
                                    input_public=[{'type':'message','role':'user','content':[{'type':'input_text','text':'fixture'}]}])
                            self.assertLess(time.monotonic()-started,6)
                            raw=service.store.load_raw('blocked')
                            if stage=='response':
                                self.assertEqual(raw['active_turn']['outcome'],'unknown')
                                self.assertEqual(raw['active_turn']['recovery_state'],'blocked')
                                self.assertTrue(raw['active_turn']['input_ref'])
                                self.assertEqual(calls.read_text(encoding='utf-8').splitlines(),['user'])
                                self.assertIn('claude.first_native_event',{r['stage'] for r in records})
                            else:
                                self.assertIsNone(raw.get('active_turn'))
                                self.assertNotIn('harness.dispatch_fence',{r['stage'] for r in records})
                                if stage=='initialization':
                                    self.assertFalse(calls.exists())
                                else:
                                    self.assertEqual(calls.read_text(encoding='utf-8').splitlines(),['bootstrap'])
                        finally: drop(active['binding']['binding_id'])

    def test_runner_records_only_allowlisted_matching_diagnostics(self):
        runner = object.__new__(UnifiedRunner)
        runner._claude_dispatch_id = DISPATCH
        emitted = []
        runner.phase_timing = PhaseTiming(emitted.append)
        runner._claude_dispatch_observe({'type': 'claude_dispatch', 'dispatch_id': 'stale', 'stage': 'input_consumed'})
        runner._claude_dispatch_observe({'type': 'claude_dispatch', 'dispatch_id': DISPATCH, 'stage': 'input_consumed', 'text': 'private'})
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]['stage'], 'claude.input_consumed')
        self.assertNotIn('private', json.dumps(emitted))

    def test_unified_retry_expiry_is_scoped_bounded_and_projected_once(self):
        runner=object.__new__(UnifiedRunner);runner._claude_dispatch_id=DISPATCH
        events=[];runner.on_event=events.append
        row={'type':'retry_budget_expired','dispatch_id':DISPATCH,'retryBudgetMs':3600000}
        for wrong in ({**row,'dispatch_id':'stale'},{**row,'retryBudgetMs':True},
                      {**row,'retryBudgetMs':3600001}):runner._claude_retry_expired(wrong)
        self.assertEqual(events,[])
        runner._claude_retry_expired(row);runner._claude_retry_expired(row)
        self.assertEqual(events,[{'type':'system','subtype':'retry_budget_expired','retry_budget_ms':3600000}])

    def test_js_diagnostic_module_never_emits_stderr_text(self):
        path = ROOT / 'desktron/lib/shepherd/claude_dispatch.mjs'
        code = '''
import {ClaudeDispatch} from %s;
const rows=[];const d=new ClaudeDispatch(%s,row=>rows.push(row));
d.stderr('HTTP 401 OAuth token revoked private-fixture');
d.stderr('HTTP 401 OAuth token revoked private-fixture');
d.stderr('ECONNRESET https://private.invalid/?secret=fixture');
d.phase('not_allowlisted');
d.native({type:'system',subtype:'api_retry'}); // Before input: not this dispatch.
d.phase('input_consumed');d.native({type:'system',subtype:'init'});
d.native({type:'stream_event',event:{type:'message_start',message:{role:'assistant',id:'fixture',text:'private'}}});
d.native({type:'system',subtype:'api_retry',error:'private'});
d.native({type:'system',subtype:'api_retry',error:'private'});
console.log(JSON.stringify(rows));
''' % (json.dumps(path.as_uri()), json.dumps(DISPATCH))
        result = subprocess.run(['node', '--input-type=module', '-e', code],
                                capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual([r['stage'] for r in rows], ['stderr_auth', 'stderr_network', 'input_consumed',
            'first_native_event', 'provider_response_started', 'provider_retry', 'provider_retry'])
        self.assertNotIn('private', result.stdout)
        self.assertNotIn('secret', result.stdout)

    def test_both_bridges_confirm_sdk_consumption_only_after_start(self):
        from test_shepherd_claude import ClaudeConnectorContract
        for resident in (False, True):
            with self.subTest(resident=resident):
                fixture = ClaudeConnectorContract(); fixture.setUp()
                try:
                    payload, consumed = fixture.bridge_fixture()
                    payload.update(bridgeProtocolVersion=4 if resident else 3, dispatchId=DISPATCH)
                    path = fixture.root / 'dispatch-bootstrap.json'
                    path.write_text(json.dumps(payload), encoding='utf-8')
                    script = ROOT / ('desktron/lib/shepherd/claude_resident_runner.mjs' if resident
                                     else 'desktron/lib/shepherd/claude_runner.mjs')
                    process = subprocess.Popen(['node', str(script), str(path)], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
                    try:
                        if resident:
                            process.stdin.write(json.dumps({'type': 'turn', 'payload': {
                                'prompt': 'fixture', 'dispatchId': DISPATCH}}) + '\n'); process.stdin.flush()
                        self.assertEqual(json.loads(process.stdout.readline())['type'], 'prepared')
                        self.assertFalse(consumed.exists())
                        process.stdin.write('{"type":"start"}\n'); process.stdin.flush()
                        stages=[]
                        for _ in range(5):
                            row=json.loads(process.stdout.readline())
                            self.assertEqual(row['dispatch_id'], DISPATCH)
                            stages.append(row['stage'])
                        self.assertEqual(stages, ['start_received', 'store_begin', 'store_ready',
                                                  'input_queued', 'input_consumed'])
                    finally:
                        if process.poll() is None: process.kill()
                        process.wait(timeout=5)
                        for stream in (process.stdin, process.stdout, process.stderr): stream.close()
                finally: fixture.doCleanups()


if __name__ == '__main__': unittest.main()
