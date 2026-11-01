"""Quit/SIGKILL must not leave a Pi writer or a permanent Shepherd wait."""
import fcntl
from _platform import readline
import io
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from test_pi_engine import Bus, Proc
from engine_adapters.pi import PiAdapter
from owned_process import command, protect
import test_shepherd_crash_recovery as recovery_fixtures
from shepherd.recovery_controller import RecoveryController
import test_shepherd_unified_storage as storage_fixtures
from shepherd.unified_runners import UnifiedRunner
from shepherd.harness import ExecutionHarness
from shepherd.runner_process import NativeProcess


class OwnedProcessTests(unittest.TestCase):
    def test_shepherd_constructor_failure_stops_registered_writer(self):
        registered = []
        with mock.patch.object(NativeProcess, '_initialize', side_effect=OSError('thread unavailable')):
            with self.assertRaisesRegex(OSError, 'thread unavailable'):
                NativeProcess([sys.executable,'-c','import time;time.sleep(120)'],
                    cwd=os.getcwd(),env=os.environ.copy(),timeout=10,cancel_frame={'type':'abort'},
                    on_process=registered.append)
        self.assertEqual(len(registered), 1)
        self.assertIsNotNone(registered[0].poll())
        self.assertTrue(all(s.closed for s in (registered[0].stdin, registered[0].stdout, registered[0].stderr)))

    def test_parent_sigkill_releases_writer_lock_and_allows_new_process(self):
        with tempfile.TemporaryDirectory() as root:
            lock = str(Path(root)/'writer.lock')
            lib = str(Path(__file__).resolve().parents[1]/'lib')
            parent_code = '''
import json, subprocess, sys
sys.path.insert(0, sys.argv[1])
from owned_process import command
p = subprocess.Popen(command([sys.executable, '-c',
    'import time; print("ready", flush=True); time.sleep(120)'], lock_path=sys.argv[2]),
    start_new_session=True, stdout=subprocess.PIPE, text=True)
assert p.stdout.readline().strip() == 'ready'
print(json.dumps({'guardian':p.pid}), flush=True)
p.wait()
'''
            parent = subprocess.Popen([sys.executable, '-c', parent_code, lib, lock],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            guardian = None
            try:
                guardian = json.loads(readline(parent.stdout, 8))['guardian']
                with open(lock, 'a') as handle:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                parent.kill(); parent.wait(timeout=3)
                # A new writer can acquire only after the old native process
                # (which inherits the fd) has exited. No real model involved.
                fresh = subprocess.run(command([sys.executable, '-c', 'print("new writer")'],
                                               lock_path=lock), start_new_session=True,
                                       capture_output=True, text=True, timeout=5)
                self.assertEqual(fresh.returncode, 0, fresh.stderr)
                self.assertEqual(fresh.stdout.strip(), 'new writer')
            finally:
                if parent.poll() is None: parent.kill()
                parent.communicate(timeout=3)
                if guardian:
                    try: os.killpg(guardian, signal.SIGKILL)
                    except ProcessLookupError: pass

    def test_native_stdio_exit_status_and_exclusive_lock(self):
        with tempfile.TemporaryDirectory() as root:
            lock = str(Path(root)/'writer.lock')
            p = protect(subprocess.Popen(command([sys.executable, '-c',
                'import time; print("ready", flush=True); time.sleep(120)'], lock_path=lock),
                start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            try:
                self.assertEqual(readline(p.stdout, 5).strip(), 'ready')
                denied = subprocess.run(command([sys.executable, '-c', 'print("BAD")'], lock_path=lock),
                    start_new_session=True, capture_output=True, text=True, timeout=5)
                self.assertEqual(denied.returncode, 1)
                self.assertNotIn('BAD', denied.stdout)
            finally:
                p.kill(); p.communicate(timeout=3)
            result = subprocess.run(command([sys.executable, '-c',
                'import sys; print(input()); sys.exit(7)'], lock_path=lock), start_new_session=True,
                input='hello\n', capture_output=True, text=True, timeout=5)
            self.assertEqual((result.returncode, result.stdout), (7, 'hello\n'))


class OrdinaryOwnedWriterTests(unittest.TestCase):
    def test_base_spawn_wraps_claude_style_writer_with_parent_guard_and_lock(self):
        from engine_adapters.cli_turn import TurnRunnerMixin
        from types import SimpleNamespace
        adapter = SimpleNamespace(dir='/tmp/adapter')
        proc = mock.Mock(pid=42)
        with mock.patch('engine_adapters.cli_turn.subprocess.Popen', return_value=proc) as spawn, \
             mock.patch('owned_process.protect', side_effect=lambda value:value) as protect:
            got = TurnRunnerMixin._spawn_process(adapter, 'adp_test', ['claude'], cwd='/tmp')
        self.assertIs(got, proc); protect.assert_called_once_with(proc)
        argv = spawn.call_args.args[0]
        self.assertIn('owned_process.py', argv[1])
        self.assertIn(os.path.join('/tmp/adapter', 'adp_test.native-writer.lock'), argv)
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])


    def test_protected_kill_asks_supervisor_to_cleanup_before_hard_fallback(self):
        proc=mock.Mock(pid=42);proc.poll.return_value=None;proc.wait.return_value=0
        from owned_process import protect
        with mock.patch('owned_process._start_token', return_value='token'), \
             mock.patch('owned_process.os.kill') as signal_pid, \
             mock.patch('owned_process.os.killpg') as hard:
            protect(proc).kill()
        signal_pid.assert_called_once_with(42,signal.SIGTERM);hard.assert_not_called()

    def test_protected_kill_rejects_reused_pid(self):
        proc=mock.Mock(pid=42);proc.poll.return_value=None
        from owned_process import protect
        with mock.patch('owned_process._start_token', side_effect=['old','new']), \
             mock.patch('owned_process.os.kill') as signal_pid:
            protect(proc).kill()
        signal_pid.assert_not_called()

    def test_supervisor_collects_detached_descendant_groups(self):
        from owned_process import _descendant_process_groups
        rows = '100 1 100\n101 100 100\n102 101 102\n103 102 102\n200 1 200\n'
        with mock.patch('owned_process.subprocess.run', return_value=mock.Mock(stdout=rows)), \
             mock.patch('owned_process.os.getpgrp', return_value=100):
            self.assertEqual(_descendant_process_groups(100), {102})

    def test_supervisor_kills_detached_groups_before_own_group(self):
        from owned_process import _kill_owned_tree
        with mock.patch('owned_process._descendant_process_groups', return_value={102, 103}), \
             mock.patch('owned_process.os.getpgrp', return_value=100), \
             mock.patch('owned_process.os.killpg') as kill:
            _kill_owned_tree(100)
        self.assertEqual({call.args[0] for call in kill.call_args_list}, {100, 102, 103})

    @unittest.skipUnless(hasattr(select,'KQ_NOTE_TRACK'),'requires kernel descendant tracking')
    def test_parent_sigkill_kills_true_double_fork_daemon_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as root:
            lock=str(Path(root)/'writer.lock');pidfile=str(Path(root)/'daemon.pid')
            lib=str(Path(__file__).resolve().parents[1]/'lib')
            child_code=r'''import os,sys,time
pidfile=sys.argv[1]
if os.fork()==0:
 os.setsid()
 if os.fork()==0:
  # Publish only a complete PID, not an empty newly-created file.
  with open(pidfile+'.tmp','w') as f:f.write(str(os.getpid()))
  os.replace(pidfile+'.tmp',pidfile)
  time.sleep(120)
 os._exit(0)
print('ready',flush=True)
time.sleep(120)
'''
            parent_code=r'''import subprocess,sys,time
sys.path.insert(0,sys.argv[1]);from owned_process import command
p=subprocess.Popen(command([sys.executable,'-c',sys.argv[4],sys.argv[3]],lock_path=sys.argv[2]),
 start_new_session=True,stdout=subprocess.PIPE,text=True)
print(p.stdout.readline(),end='',flush=True)
time.sleep(120)
'''
            parent=subprocess.Popen([sys.executable,'-c',parent_code,lib,lock,pidfile,child_code],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            daemon=None
            try:
                self.assertEqual(parent.stdout.readline().strip(),'ready')
                deadline=time.monotonic()+3
                while time.monotonic()<deadline and not Path(pidfile).exists():time.sleep(.01)
                daemon=int(Path(pidfile).read_text())
                parent.kill();parent.wait(timeout=3)
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    try:os.kill(daemon,0)
                    except OSError:break
                    time.sleep(.05)
                else:self.fail(f'double-fork daemon survived: {daemon}')
                fresh=subprocess.run(command([sys.executable,'-c','print("fresh")'],lock_path=lock),
                    start_new_session=True,capture_output=True,text=True,timeout=5)
                self.assertEqual((fresh.returncode,fresh.stdout.strip()),(0,'fresh'))
            finally:
                if parent.poll() is None:parent.kill()
                parent.communicate(timeout=3)
                if daemon:
                    try:os.kill(daemon,signal.SIGKILL)
                    except OSError:pass

    @unittest.skipUnless(hasattr(select,'KQ_NOTE_TRACK'),'requires kernel descendant tracking')
    def test_explicit_sigterm_kills_true_double_fork_daemon_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as root:
            lock=str(Path(root)/'writer.lock');pidfile=str(Path(root)/'daemon.pid')
            child_code=r'''import os,sys,time
if os.fork()==0:
 os.setsid()
 if os.fork()==0:
  # Publish only a complete PID, not an empty newly-created file.
  with open(sys.argv[1]+'.tmp','w') as f:f.write(str(os.getpid()))
  os.replace(sys.argv[1]+'.tmp',sys.argv[1]);time.sleep(120)
 os._exit(0)
print('ready',flush=True);time.sleep(120)
'''
            proc=protect(subprocess.Popen(command([sys.executable,'-c',child_code,pidfile],lock_path=lock),
                start_new_session=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
            daemon=None
            try:
                self.assertEqual(proc.stdout.readline().strip(),'ready')
                deadline=time.monotonic()+3
                while time.monotonic()<deadline and not Path(pidfile).exists():time.sleep(.01)
                daemon=int(Path(pidfile).read_text())
                proc.kill();proc.communicate(timeout=3)
                deadline=time.monotonic()+3
                while time.monotonic()<deadline:
                    try:os.kill(daemon,0)
                    except OSError:break
                    time.sleep(.05)
                else:self.fail(f'double-fork daemon survived SIGTERM: {daemon}')
                fresh=subprocess.run(command([sys.executable,'-c','print("fresh")'],lock_path=lock),
                    start_new_session=True,capture_output=True,text=True,timeout=5)
                self.assertEqual((fresh.returncode,fresh.stdout.strip()),(0,'fresh'))
            finally:
                if proc.poll() is None:proc.kill()
                if daemon:
                    try:os.kill(daemon,signal.SIGKILL)
                    except OSError:pass

    def test_parent_sigkill_kills_detached_helper_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as root:
            lock=str(Path(root)/'writer.lock');lib=str(Path(__file__).resolve().parents[1]/'lib')
            parent_code=r'''
import json,os,subprocess,sys,time
sys.path.insert(0,sys.argv[1]);from owned_process import command
child_code="""import json,os,subprocess,sys,time
helper=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],start_new_session=True)
print(json.dumps({'writer':os.getpid(),'helper':helper.pid}),flush=True)
time.sleep(120)
"""
p=subprocess.Popen(command([sys.executable,'-c',child_code],lock_path=sys.argv[2]),
                   start_new_session=True,stdout=subprocess.PIPE,text=True)
print(p.stdout.readline(),end='',flush=True);time.sleep(120)
'''
            parent=subprocess.Popen([sys.executable,'-c',parent_code,lib,lock],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            ids=json.loads(parent.stdout.readline())
            try:
                parent.kill();parent.wait(timeout=3)
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    alive=[]
                    for pid in (ids['writer'],ids['helper']):
                        try:os.kill(pid,0);alive.append(pid)
                        except OSError:pass
                    if not alive:break
                    time.sleep(.05)
                self.assertEqual(alive,[])
                fresh=subprocess.run(command([sys.executable,'-c','print("fresh")'],lock_path=lock),
                    start_new_session=True,capture_output=True,text=True,timeout=5)
                self.assertEqual((fresh.returncode,fresh.stdout.strip()),(0,'fresh'))
            finally:
                if parent.poll() is None:parent.kill()
                for pid in (ids['writer'],ids['helper']):
                    try:os.kill(pid,signal.SIGKILL)
                    except OSError:pass


class PiExitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.bus = Bus(); self.ad = PiAdapter(self.tmp.name, self.tmp.name, self.bus)
        self.sid = self.ad.create_session()['id']

    def test_shutdown_seals_admission_and_includes_control_processes(self):
        proc = mock.Mock(pid=91234); proc.poll.return_value = None
        self.ad._pi_processes[proc] = self.sid
        with mock.patch.object(self.ad, '_soft_interrupt') as abort, mock.patch('os.killpg') as kill:
            self.ad.shutdown(timeout=0)
            abort.assert_called_once_with(self.sid, proc)
            kill.assert_called_once_with(91234, signal.SIGKILL)
            self.ad.shutdown(timeout=0)
            self.assertEqual(kill.call_count, 1)
        with self.assertRaisesRegex(RuntimeError, '退出'):
            self.ad.prompt_async(self.sid, 'not sent')
        with mock.patch.object(self.ad, '_run') as run:
            with self.assertRaisesRegex(RuntimeError, '退出'):
                self.ad._run_serial(self.sid, 'queued before quit')
            run.assert_not_called()
        with mock.patch('engine_adapters.pi.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(RuntimeError, '退出'):
                self.ad._spawn_process(self.sid, ['pi'])
            spawn.assert_not_called()
        self.assertEqual(self.ad.messages(self.sid), [])

    def test_blocked_abort_pipe_does_not_block_shutdown_caller(self):
        self.ad._shutting_down = True
        entered = threading.Event(); release = threading.Event()
        proc = Proc()
        def blocked(_text):
            entered.set(); release.wait(3)
        proc.stdin = mock.Mock(); proc.stdin.write.side_effect = blocked
        try:
            started = time.monotonic()
            self.assertTrue(self.ad._soft_interrupt(self.sid, proc))
            self.assertLess(time.monotonic()-started, .5)
            self.assertTrue(entered.wait(1))
        finally:
            release.set()

    def test_control_rpc_cannot_race_prompt_or_spawn_during_shutdown(self):
        guard = threading.Lock(); self.ad._turn_locks[self.sid] = guard
        with guard, mock.patch.object(self.ad, '_rpc_once_locked') as run:
            _, err = self.ad._rpc_once(self.sid, [{'type':'compact'}])
            self.assertIn('不能同时', err); run.assert_not_called()
        self.ad.shutdown(timeout=0)
        with mock.patch.object(self.ad, '_rpc_once_locked') as run:
            _, err = self.ad._rpc_once(self.sid, [{'type':'compact'}])
            self.assertIn('退出', err); run.assert_not_called()

    def test_crash_snapshot_does_not_restore_queue_or_compaction_as_live(self):
        snap = {'info':{'id':'old','role':'assistant','running':True,'started':100,
                        'beat':200,'retry':{'active':True},'compacting':100,
                        'activity':{'kind':'provider_queue'},'native_queue':{'active':True}},
                'parts':[{'type':'tool','state':{'status':'running','output':'partial'}}]}
        with mock.patch.object(self.ad, '_read_live_checkpoint', return_value=snap):
            got = self.ad._recovered_live_message(self.sid)
        self.assertTrue(got['info']['interrupted'])
        for key in ('retry','compacting','activity','native_queue'):
            self.assertNotIn(key, got['info'])
        self.assertEqual(self.ad.turn_state(self.sid)['state'], 'ready')
        self.assertEqual(got['parts'][0]['state']['status'], 'error')


class ShepherdExitTests(unittest.TestCase):
    setUp = recovery_fixtures.CrashRecoveryCases.setUp
    begin = recovery_fixtures.CrashRecoveryCases.begin

    def test_startup_wait_can_recover_after_exact_writer_death_without_replay(self):
        self.begin()
        controller = RecoveryController(self.service, boot_id='new', retry_delays=(60,))
        self.addCleanup(controller.stop)
        with mock.patch('shepherd.recovery.process_state', return_value='alive'):
            first = controller.recover_guarded(self.cid, trigger='startup')
        self.assertEqual(first['action'], 'evidence_probe')
        self.assertIsNotNone(self.service.coordinator.state(self.cid)['inflight'])
        with mock.patch('shepherd.recovery_controller.process_state', return_value='unknown'), \
             mock.patch('shepherd.recovery_controller.CrashRecovery') as recovery:
            waiting = controller.recover_guarded(self.cid, trigger='background_retry', retry_count=1)
            self.assertEqual(waiting['outcome'], 'waiting_writer_exit')
            recovery.assert_not_called()
        with mock.patch('shepherd.recovery_controller.process_state', return_value='dead'), \
             mock.patch('shepherd.recovery.process_state', return_value='dead'):
            done = controller.recover_guarded(self.cid, trigger='send_before')
        self.assertEqual(done['outcome'], 'settled_incomplete')
        state = self.service.coordinator.state(self.cid)
        self.assertIsNone(state['inflight'])
        self.assertEqual(state['rounds'][0]['terminal_state'], 'unknown')
        self.assertEqual(controller.recover_guarded(self.cid, trigger='send_before')['outcome'], 'not_applicable')

    def test_restart_retries_old_transient_fence_but_never_corrupt_source(self):
        row = self.begin()
        c = self.service.coordinator
        for reason, expected in [('orphan_process_unknown', 'settled_incomplete')]:
            c.mark_unresolved(self.cid,self.lease['lease_id'],self.lease['generation'],
                              attempt_id=row['attempt_id'],reason=reason)
            controller = RecoveryController(self.service, boot_id='another_boot', retry_delays=())
            result = controller.recover_guarded(self.cid, trigger='startup')
            self.assertEqual(result['outcome'], expected)

    def test_unverifiable_evidence_has_explicit_isolation_not_auto_retry(self):
        row = self.begin()
        self.service.coordinator.mark_unresolved(self.cid,self.lease['lease_id'],self.lease['generation'],
            attempt_id=row['attempt_id'],reason='commit_verification_failed')
        controller = RecoveryController(self.service, boot_id='new', retry_delays=())
        with mock.patch('shepherd.recovery_controller.CrashRecovery') as recover:
            report = controller.recover_guarded(self.cid, trigger='startup')
        recover.assert_not_called()
        self.assertEqual(report['action'], 'isolation_required')

    def test_new_parser_rechecks_unsupported_source_shape_once_per_boot(self):
        row = self.begin()
        self.service.coordinator.mark_unresolved(self.cid,self.lease['lease_id'],self.lease['generation'],
            attempt_id=row['attempt_id'],reason='source_shape_unsupported')
        with self.service.coordinator.store.change(self.cid) as state:
            state['bindings'][self.binding_id]['connector_version'] = 'unified-v1:pi-v1'
        controller = RecoveryController(self.service, boot_id='new', retry_delays=())
        recovered = {'state':'recovered_incomplete'}
        with mock.patch('shepherd.recovery_controller.process_state', return_value='dead'), \
             mock.patch('shepherd.recovery_controller.CrashRecovery') as recovery:
            recovery.return_value.recover_one.return_value = recovered
            first = controller.recover_guarded(self.cid, trigger='startup')
            second = controller.recover_guarded(self.cid, trigger='startup')
        self.assertEqual(first['outcome'], 'recovered_incomplete')
        self.assertEqual(second['action'], 'isolation_required')
        recovery.return_value.recover_one.assert_called_once()


class UnifiedDispatchExitTests(unittest.TestCase):
    setUp = storage_fixtures.UnifiedStorage.setUp

    def test_exit_just_after_dispatch_has_durable_anchor_and_can_continue(self):
        self.service.create('conversation', 'pi', self.root)
        active = self.service.active('conversation')
        c = self.service.connector(active['binding']); lease = active['lease']
        runner = UnifiedRunner(c, options={'model':'fixture'})
        def crash(_prompt, _rounds, _checkpoint, dispatch, _locate):
            dispatch(process=mock.Mock(pid=99999998))
            state = self.service.coordinator.state('conversation')
            row = state['inflight']
            self.assertEqual(row['recovery']['native_locator']['turn_id'], row['round_id'])
            self.assertEqual(c.archive.read(c.binding, row['round_id'])['source']['events'][0]['type'], 'runtime')
            raise RuntimeError('simulated Desk exit before first native response')
        with mock.patch.object(runner, '_pi', side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, 'simulated Desk exit'):
                ExecutionHarness(self.service.coordinator, c, runner).run('conversation',
                    lease_id=lease['lease_id'], generation=lease['generation'], prompt='already accepted',
                    input_public=[{'type':'message','role':'user','content':[
                        {'type':'input_text','text':'already accepted'}]}])
        with mock.patch('shepherd.recovery.process_state', return_value='dead'), \
             mock.patch('shepherd.recovery_controller.process_state', return_value='dead'):
            report = RecoveryController(self.service, boot_id='new').recover_guarded(
                'conversation', trigger='startup')
        self.assertEqual(report['outcome'], 'recovered_incomplete')
        state = self.service.coordinator.state('conversation')
        self.assertIsNone(state['inflight'])
        self.assertEqual(state['rounds'][0]['terminal_state'], 'unknown')
        self.assertNotEqual(state['active_binding_id'], c.binding_id)
