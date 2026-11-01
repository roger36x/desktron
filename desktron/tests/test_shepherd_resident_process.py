import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from shepherd import resident_process as resident
from shepherd.store import Conflict


class _Proc:
    def __init__(self, pid): self.pid=pid;self.dead=False
    def poll(self): return 0 if self.dead else None


class _Host:
    def __init__(self, pid): self.proc=_Proc(pid);self.closed=False
    def close(self): self.closed=True;self.proc.dead=True


class ResidentProcessOwnership(unittest.TestCase):
    def test_eof_preserves_watchdog_timeout_marked_during_process_wait(self):
        import queue
        process = object.__new__(resident.ResidentProcess)
        process._state_lock = threading.Lock()
        process._timed_out = ''
        process.budget = mock.Mock()
        process.budget.next_deadline.return_value = (time.monotonic() + 10, 'hard')
        process.lines = queue.Queue()
        process.lines.put(None)
        process.proc = mock.Mock(returncode=15)
        def watchdog_finished(**kwargs):
            process._timed_out = 'Claude SDK handoff timeout'
        process.proc.wait.side_effect = watchdog_finished
        with self.assertRaisesRegex(Conflict, 'SDK handoff timeout'):
            list(process.events())

    def test_turn_budget_does_not_inherit_previous_compaction(self):
        process = object.__new__(resident.ResidentProcess)
        process.proc = mock.Mock()
        process.proc.poll.return_value = None
        process._state_lock = threading.Lock()
        process._active = False
        process._timed_out = ''
        process._cancel_timer = None
        process.begin(timeout=900, hard_timeout=7200)
        first = process.budget
        process._touch({'type':'tool_execution_start', 'toolCallId':'fixture'})
        process._touch({'type':'compaction_start'})
        self.assertTrue(first.compacting)
        process.finish()
        process.begin(timeout=900, hard_timeout=7200)
        self.assertIsNot(process.budget, first)
        self.assertFalse(process.budget.compacting)
        self.assertIsNone(process.budget.wait_deadline)
        self.assertEqual(process.budget.observation()['active_tools'], 0)
        self.assertIsNone(process.budget.observation()['progress_age_seconds'])
        self.assertEqual(process.budget.next_deadline()[1], 'idle')

    def test_resident_idle_window_is_soft_and_recovers(self):
        observed=[]
        code=("import json,sys,time\n"
              "for line in sys.stdin:\n"
              " row=json.loads(line)\n"
              " if row.get('type')=='turn':\n"
              "  time.sleep(.35)\n"
              "  print(json.dumps({'type':'progress'}),flush=True)\n")
        process=resident.ResidentProcess(
            [sys.executable,'-S','-u','-c',code],cwd=tempfile.gettempdir(),
            env=os.environ.copy(),cancel_frame={'type':'abort'})
        self.addCleanup(process.close)
        process.begin(timeout=.1,hard_timeout=30,on_stall=observed.append)
        process.send({'type':'turn'})
        events=process.events()
        self.assertEqual(next(events),{'type':'progress'})
        deadline=time.monotonic()+2
        while len(observed)<2 and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual([row.get('active') for row in observed],[True,False])
        self.assertEqual([row.get('sequence') for row in observed],[1,2])
        process.finish()

    def test_stdout_eof_does_not_disarm_the_absolute_ceiling(self):
        process = resident.ResidentProcess(
            [sys.executable, '-S', '-u', '-c', 'import os,time;os.close(1);time.sleep(30)'],
            cwd=tempfile.gettempdir(), env=os.environ.copy(), cancel_frame={'type':'cancel'})
        self.addCleanup(process.close)
        process.begin(timeout=.04, hard_timeout=.2)
        started = time.monotonic()
        with self.assertRaisesRegex(Conflict, '达到最长时限'):
            list(process.events())
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNotNone(process.proc.wait(timeout=3))

    def test_constructor_uses_owned_supervisor_and_protected_kill(self):
        proc=mock.Mock();proc.stdin=mock.Mock();proc.stdout=mock.Mock();proc.stderr=mock.Mock()
        with mock.patch('shepherd.resident_process.subprocess.Popen',return_value=proc) as spawn, \
             mock.patch('owned_process.command',return_value=['owned','native']) as command, \
             mock.patch('owned_process.protect',side_effect=lambda value:value) as protect, \
             mock.patch('shepherd.resident_process.threading.Thread.start'):
            got=resident.ResidentProcess(['native'],cwd='/tmp',env={},cancel_frame={'type':'abort'})
        command.assert_called_once_with(['native'],lock_path='',cwd='/tmp')
        protect.assert_called_once_with(proc)
        self.assertNotIn('cwd',spawn.call_args.kwargs)
        self.assertTrue(spawn.call_args.kwargs['start_new_session']);self.assertIs(got.proc,proc)

    def test_force_kill_uses_protected_supervisor_contract(self):
        process=object.__new__(resident.ResidentProcess);process.proc=mock.Mock()
        process.proc.poll.return_value=None
        process._kill_lock=threading.Lock()
        process._kill()
        process.proc.kill.assert_called_once_with()

    @unittest.skipUnless(hasattr(select,'KQ_NOTE_TRACK'),'requires kernel descendant tracking')
    def test_close_kills_true_double_fork_descendant(self):
        with tempfile.TemporaryDirectory() as root:
            pidfile=str(Path(root)/'daemon.pid')
            code=r'''import os,sys,time
if os.fork()==0:
 os.setsid()
 if os.fork()==0:
  open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(120)
 os._exit(0)
time.sleep(120)
'''
            process=resident.ResidentProcess(
                [sys.executable,'-c',code,pidfile],cwd=root,env=os.environ.copy(),
                cancel_frame={'type':'abort'},lock_path=str(Path(root)/'writer.lock'))
            daemon=None
            try:
                deadline=time.monotonic()+3
                value=''
                while time.monotonic()<deadline:
                    try:value=Path(pidfile).read_text().strip()
                    except OSError:value=''
                    if value.isdigit():break
                    time.sleep(.01)
                daemon=int(value)
                process.close()
                deadline=time.monotonic()+3
                while time.monotonic()<deadline:
                    try:os.kill(daemon,0)
                    except OSError:break
                    time.sleep(.05)
                else:self.fail(f'double-fork descendant survived: {daemon}')
            finally:
                if process.proc.poll() is None:process._kill()
                if daemon:
                    try:os.kill(daemon,9)
                    except OSError:pass


class ResidentPoolCases(unittest.TestCase):
    def setUp(self):
        with resident._LOCK:
            self.old=resident._HOSTS
            resident._HOSTS={}
        self.addCleanup(self.restore)

    def restore(self):
        for binding_id in list(resident._HOSTS):resident.drop(binding_id)
        with resident._LOCK:resident._HOSTS=self.old

    def test_reuses_only_matching_idle_context(self):
        made=[]
        def create():
            host=_Host(100+len(made));made.append(host);return host
        first,created=resident.acquire('binding','key',3,create)
        self.assertTrue(created);self.assertTrue(resident.release('binding',first,4))
        second,created=resident.acquire('binding','key',4,create)
        self.assertFalse(created);self.assertIs(second,first)
        self.assertEqual(len(made),1)
        resident.release('binding',second,5)

    def test_dead_supervisor_with_inherited_lock_keeps_retiring_fence(self):
        with tempfile.TemporaryDirectory() as root:
            lock=str(Path(root)/'writer.lock')
            holder=subprocess.Popen([sys.executable,'-c',
                'import oscompat;import fcntl,os,sys,time;f=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600);'
                'fcntl.flock(f,fcntl.LOCK_EX);print("locked",flush=True);time.sleep(120)',lock],
                stdout=subprocess.PIPE,text=True)
            self.assertEqual(holder.stdout.readline().strip(),'locked')
            host=_Host(211);host.proc.dead=True;host.lock_path=lock
            with resident._LOCK:
                entry=resident._Entry(host,'old',1);entry.busy=False
                resident._HOSTS['binding']=entry
            try:
                self.assertFalse(resident.drop('binding',host))
                with self.assertRaisesRegex(Conflict,'禁止并发 Writer'):
                    resident.acquire('binding','new',1,lambda:_Host(212))
                holder.kill();holder.wait(timeout=3)
                replacement,created=resident.acquire('binding','new',1,lambda:_Host(212))
                self.assertTrue(created);self.assertEqual(replacement.proc.pid,212)
                resident.release('binding',replacement,2)
            finally:
                if holder.poll() is None:holder.kill();holder.wait(timeout=3)

    def test_missing_writer_lock_never_proves_tree_release(self):
        host=_Host(213);host.proc.dead=True;host.lock_path='/missing/private/writer.lock'
        with resident._LOCK:
            entry=resident._Entry(host,'old',1);entry.busy=False
            resident._HOSTS['binding']=entry
        self.assertFalse(resident.drop('binding',host))
        with self.assertRaisesRegex(Conflict,'禁止并发 Writer'):
            resident.acquire('binding','new',1,lambda:_Host(214))

    def test_context_or_option_change_replaces_host(self):
        made=[]
        def create():
            host=_Host(200+len(made));made.append(host);return host
        first,_=resident.acquire('binding','old',1,create)
        resident.release('binding',first,2)
        second,created=resident.acquire('binding','new',2,create)
        self.assertTrue(created);self.assertIsNot(second,first);self.assertTrue(first.closed)
        resident.release('binding',second,3)

    def test_busy_host_is_never_shared(self):
        host,_=resident.acquire('binding','key',0,lambda:_Host(300))
        with self.assertRaises(Conflict):
            resident.acquire('binding','key',0,lambda:_Host(301))
        resident.release('binding',host,1)


if __name__=='__main__':unittest.main()
