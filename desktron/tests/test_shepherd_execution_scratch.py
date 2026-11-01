"""Only owned, stopped execution_scratch is eligible for garbage collection."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from shepherd.execution_scratch import ExecutionScratch, sweep
from shepherd.store import Conflict


class ScratchCases(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def create(self): return ExecutionScratch(self.root,{'binding_id':'owned'},'conversation:1')

    def test_normal_completion_cleans_only_own_directory(self):
        other=self.root/'execution';other.mkdir(mode=0o700)
        foreign=other/'turn-unregistered';foreign.mkdir(mode=0o700)
        with self.create() as scratch:
            (scratch.path/'history').write_text('fixture')
            self.assertEqual(sweep(self.root)['removed'],0)
        self.assertFalse(scratch.path.exists());self.assertTrue(foreign.exists())

    def test_uncommitted_working_context_is_not_collected(self):
        with self.create() as scratch:
            scratch.retain_context()
            (scratch.path/'context').write_text('native evidence')
        self.assertTrue(scratch.path.exists())
        self.assertEqual(sweep(self.root)['removed'],0)
        with self.create() as committed:
            committed.retain_context()
            committed.retain_context(False)
        self.assertFalse(committed.path.exists())

    def test_killed_host_is_reaped_after_restart(self):
        code="""import sys,time
from shepherd.execution_scratch import ExecutionScratch
s=ExecutionScratch(sys.argv[1],{'binding_id':'owned'},'conversation:1')
print(s.path,flush=True)
time.sleep(60)
"""
        proc=subprocess.Popen([sys.executable,'-c',code,str(self.root)],stdout=subprocess.PIPE,
                              text=True,start_new_session=True,env=os.environ.copy())
        self.addCleanup(lambda:proc.poll() is None and proc.kill())
        path=Path(proc.stdout.readline().strip())
        self.assertEqual(sweep(self.root)['removed'],0)
        proc.kill();proc.wait();proc.stdout.close()
        self.assertEqual(sweep(self.root)['removed'],1)
        self.assertFalse(path.exists())

    def test_registered_live_child_survives_release_then_is_reaped(self):
        proc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],
                              start_new_session=True)
        def cleanup():
            if proc.poll() is None: proc.kill()
            proc.wait()
        self.addCleanup(cleanup)
        scratch=self.create();scratch.spawning();scratch.register(proc)
        scratch.__exit__(None,None,None)
        self.assertTrue(scratch.path.exists())
        self.assertEqual(sweep(self.root)['removed'],0)
        proc.kill();proc.wait()
        self.assertEqual(sweep(self.root)['removed'],1)

    def test_released_resident_child_does_not_pin_turn_scratch(self):
        proc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],
                              start_new_session=True)
        def cleanup():
            if proc.poll() is None: proc.kill()
            proc.wait()
        self.addCleanup(cleanup)
        scratch=self.create();scratch.spawning();scratch.register(proc)
        scratch.release_process(proc)
        scratch.__exit__(None,None,None)
        self.assertFalse(scratch.path.exists())
        self.assertIsNone(proc.poll())

    def test_release_uses_registered_identity_when_live_resampling_disappears(self):
        identity={'pid':123,'pgid':123,'start_token':'exact'}
        process=object();scratch=self.create();scratch.spawning()
        with mock.patch('shepherd.execution_scratch.process_identity',return_value=identity) as probe:
            scratch.register(process)
            probe.side_effect=AssertionError('release must not resample an exiting process')
            scratch.release_process(process)
        self.assertEqual(probe.call_count,1)
        scratch.__exit__(None,None,None)
        self.assertFalse(scratch.path.exists())

    def test_same_process_reported_again_in_one_turn_is_accepted_once(self):
        # Codex capacity retry reports the same resident app-server twice per turn.
        identity={'pid':123,'pgid':123,'start_token':'exact'}
        process=object();scratch=self.create();scratch.spawning()
        with mock.patch('shepherd.execution_scratch.process_identity',return_value=identity):
            scratch.register(process)
            scratch.spawning()
            scratch.register(process)
        saved=json.loads((scratch.path/'owner.json').read_text())
        self.assertEqual(len(saved['processes']),1)
        self.assertFalse(saved['spawning'])
        scratch.release_process(process)
        scratch.__exit__(None,None,None)
        self.assertFalse(scratch.path.exists())

    def test_reregistration_with_changed_identity_still_fails(self):
        process=object();scratch=self.create();scratch.spawning()
        with mock.patch('shepherd.execution_scratch.process_identity',
                        return_value={'pid':123,'pgid':123,'start_token':'first'}):
            scratch.register(process)
        with mock.patch('shepherd.execution_scratch.process_identity',
                        return_value={'pid':123,'pgid':123,'start_token':'second'}):
            with self.assertRaisesRegex(Conflict,'already registered'):
                scratch.register(process)
        scratch.data['processes'][0]['released']=True;scratch._save()
        scratch.__exit__(None,None,None)

    def test_release_rejects_another_object_with_the_same_pid(self):
        identity={'pid':123,'pgid':123,'start_token':'exact'}
        process=object();scratch=self.create();scratch.spawning()
        with mock.patch('shepherd.execution_scratch.process_identity',return_value=identity):
            scratch.register(process)
        with self.assertRaisesRegex(Conflict,'身份不匹配'):
            scratch.release_process(object())
        scratch.data['processes'][0]['released']=True;scratch._save()
        scratch.__exit__(None,None,None)

    def test_child_liveness_unknown_and_spawn_window_retain_files(self):
        for mode in ('alive','unknown','pending'):
            with self.subTest(mode=mode):
                scratch=self.create()
                scratch.data['processes']=[{'pid':123,'pgid':123,'start_token':'exact'}]
                scratch.data['spawning']=mode=='pending';scratch._save()
                with mock.patch('shepherd.execution_scratch.process_state',return_value=mode if mode!='pending' else 'dead'):
                    scratch.__exit__(None,None,None)
                    self.assertTrue(scratch.path.exists())
                    self.assertEqual(sweep(self.root)['removed'],0)
                scratch.data['spawning']=False;scratch._save()
                with mock.patch('shepherd.execution_scratch.process_state',return_value='dead'):
                    self.assertGreaterEqual(sweep(self.root)['removed'],1)

    def test_symlinks_and_forged_directory_identity_are_not_followed(self):
        outside=self.root/'outside';outside.mkdir();(outside/'keep').write_text('keep')
        scratch=self.create();path=scratch.path
        (path/'link').symlink_to(outside,target_is_directory=True)
        scratch.__exit__(None,None,None)
        self.assertTrue((outside/'keep').exists())
        (self.root/'execution'/'turn-foreign').symlink_to(outside,target_is_directory=True)
        self.assertEqual(sweep(self.root)['removed'],0)
        scratch=self.create();scratch.data['inode']+=1;scratch._save();os.close(scratch.fd)
        with mock.patch('shepherd.execution_scratch.process_state',return_value='dead'):
            self.assertEqual(sweep(self.root)['removed'],0)
        self.assertTrue(scratch.path.exists())
