"""Shepherd physical deletion erases content without reopening replay boundaries."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from engine_adapters.bus import EventBus
from engine_adapters.shepherd import ShepherdAdapter
from shepherd.execution_scratch import (ExecutionScratch, _scratch_binding_from_name,
                                         _scratch_prefix)
from shepherd.physical_delete import mark_deleted, purge
from shepherd.store import Conflict
from shepherd import resident_process as resident
import codex_appserver as codex_app

_argv=sys.argv
sys.argv=['ui_server.py','/tmp/frontend','7999','7998','/tmp/workspace']
try:
    from session import api as session_api
finally:
    sys.argv=_argv


class PhysicalDelete(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.workspace=root/'workspace';self.workspace.mkdir()
        (self.workspace/'prompts').mkdir();(self.workspace/'prompts/style.md').write_text('')
        self.data=root/'data'

    def adapter(self):
        return ShepherdAdapter(str(self.data),str(self.workspace),EventBus())

    @staticmethod
    def handler(owner):
        return SimpleNamespace(_adapter_owning=lambda _sid:owner)

    def wait_locked(self,path):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            if path.exists():
                fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
                try:
                    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:return
                    else:fcntl.flock(fd,fcntl.LOCK_UN)
                finally:os.close(fd)
            time.sleep(.02)
        self.fail('writer lock was not acquired: '+str(path))

    @staticmethod
    def scratch_name(binding_id, suffix='fixture'):
        return _scratch_prefix(binding_id) + suffix

    def scratch_fixture(self,root,name,binding_id,**updates):
        directory=root/'execution';directory.mkdir(mode=0o700);directory.chmod(0o700)
        path=directory/name;path.mkdir(mode=0o700);path.chmod(0o700)
        info=path.stat()
        data={'kind':'shepherd-execution-v1','name':name,'inode':info.st_ino,
              'device':info.st_dev,'host':{},'binding_id':binding_id,
              'processes':[],'spawning':False,'released':True}
        data.update(updates)
        owner=path/'owner.json';owner.write_text(json.dumps(data));owner.chmod(0o600)
        lease=path/'lease.lock';lease.touch(mode=0o600);lease.chmod(0o600)
        return path,data

    def test_delete_api_reports_failure_and_same_action_retries_until_purge_finishes(self):
        adapter=self.adapter()
        sid=adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        original=__import__('shepherd.physical_delete',fromlist=['purge']).purge
        with mock.patch('shepherd.physical_delete.purge',side_effect=OSError('injected')), \
             mock.patch.object(session_api,'_term_kill') as kill:
            result=session_api._delete_session_permanently(self.handler(adapter),sid)
        self.assertEqual(500,result[1]);self.assertIn(sid,adapter.index)
        kill.assert_not_called()
        self.assertTrue((Path(adapter.shepherd_root)/'deleted'/(sid+'.json')).is_file())
        with mock.patch('shepherd.physical_delete.purge',side_effect=original), \
             mock.patch.object(session_api,'_term_kill'):
            result=session_api._delete_session_permanently(self.handler(adapter),sid)
        self.assertIsNone(result);self.assertNotIn(sid,adapter.index)
        self.assertFalse((Path(adapter.shepherd_root)/sid).exists())

    def test_startup_retries_purge_after_marker_commit(self):
        adapter=self.adapter()
        sid=adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        state=adapter.service.store.load_raw(sid)
        bid=next(iter(state['bindings']))
        secret=Path(adapter.shepherd_root)/'sources'/bid/'secret.json'
        secret.parent.mkdir(parents=True,exist_ok=True);secret.write_text('delete me')
        with mock.patch('shepherd.physical_delete.purge',side_effect=OSError('injected')), \
             self.assertRaisesRegex(Conflict,'会话未删除；请重试'):
            adapter.delete_session(sid)
        self.assertIn(sid,adapter.index)
        self.assertTrue((Path(adapter.shepherd_root)/sid/'state.json').exists())
        self.assertTrue((Path(adapter.shepherd_root)/'deleted'/(sid+'.json')).exists())
        restored=self.adapter()
        self.assertNotIn(sid,restored.index)
        self.assertFalse((Path(restored.shepherd_root)/sid).exists())
        self.assertFalse(secret.exists())
        self.assertEqual([],restored._history_cleanup_warnings)

    def test_startup_cleanup_failure_keeps_public_retry_entry_without_recovery(self):
        adapter=self.adapter()
        sid=adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        adapter.service.coordinator.tombstone_for_delete(sid)
        mark_deleted(adapter.shepherd_root,sid)
        adapter.shutdown()
        with mock.patch('shepherd.physical_delete.purge',side_effect=OSError('still busy')):
            restored=self.adapter()
        self.addCleanup(restored.shutdown)
        self.assertIn(sid,restored.index)
        self.assertIn(sid,restored._pending_physical_deletes)
        self.assertEqual([],restored._recovery_reports)
        self.assertEqual('physical_cleanup_pending',
                         restored._history_cleanup_warnings[0]['code'])

    def test_delete_api_stops_real_resident_and_codex_pool_writers_before_success(self):
        adapter=self.adapter()
        sid=adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        active=adapter.service.active(sid);pi_bid=active['binding']['binding_id']
        from shepherd.unified_connector import CODECS, PREFIX
        codex_bid='ubind_codex_delete_api'
        adapter.service.coordinator.bind(sid,'codex','11111111-2222-4333-8444-555555555555',
            native_cwd=str(self.workspace),connector_version=PREFIX+CODECS['codex'],
            binding_id=codex_bid,ownership_nonce='delete_api_nonce')
        root=Path(adapter.shepherd_root)
        pi_home=root/'resident'/'claude'/pi_bid/'home';pi_home.mkdir(parents=True)
        pi_lock=root/'resident'/'writer-locks'/(pi_bid+'.lock')
        host=resident.ResidentProcess(
            [sys.executable,'-u','-c','import time;time.sleep(120)'],
            cwd=str(self.workspace),env=os.environ.copy(),cancel_frame={'type':'abort'},
            lock_path=str(pi_lock))
        got,created=resident.acquire(pi_bid,'delete-api',0,lambda:host)
        self.assertTrue(created);self.assertIs(got,host)
        self.assertTrue(resident.release(pi_bid,host,0));self.wait_locked(pi_lock)

        codex_home=root/'resident'/'codex-family'/'codex'/codex_bid/'home'
        codex_home.mkdir(parents=True)
        from owned_process import writer_lock_path
        codex_lock=Path(writer_lock_path(root/'adapter'/'codex', 'shepherd:' + codex_bid))
        codex_lock.parent.mkdir(parents=True)
        code=('import oscompat;import fcntl,os,sys,time;fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600);'
              'fcntl.flock(fd,fcntl.LOCK_EX);time.sleep(120)')
        codex_proc=subprocess.Popen([sys.executable,'-c',code,str(codex_lock)])
        class LiveConnection:
            def __init__(self,proc):self.proc=proc;self.closed=False
            def close(self):
                self.closed=True
                if self.proc.poll() is None:self.proc.terminate()
                self.proc.wait(timeout=5)
        connection=LiveConnection(codex_proc)
        session_id='shepherd:'+codex_bid
        entry=codex_app._SessionConnection(connection,('delete-api',),recap_on_evict=False)
        with codex_app._SESSIONS_LOCK:codex_app._SESSIONS[session_id]=entry
        self.wait_locked(codex_lock)
        try:
            with mock.patch.object(session_api,'_term_kill'):
                result=session_api._delete_session_permanently(self.handler(adapter),sid)
            self.assertIsNone(result)
            self.assertIsNotNone(host.proc.poll())
            self.assertIsNotNone(codex_proc.poll())
            self.assertNotIn(pi_bid,{row[0] for row in resident.roster()})
            self.assertNotIn(session_id,codex_app.live_session_ids())
            self.assertFalse(pi_home.exists())
            self.assertFalse(codex_home.exists())
            self.assertNotIn(sid,adapter.index)
        finally:
            resident.drop(pi_bid)
            codex_app.drop_session(session_id)
            if codex_proc.poll() is None:
                codex_proc.kill();codex_proc.wait(timeout=5)

    def test_late_request_mutations_do_not_recreate_receipts(self):
        adapter=self.adapter()
        sid=adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        adapter.requests.claim(sid,'request-one',{'text':'hello'})
        adapter.delete_session(sid)
        receipts=Path(adapter.shepherd_root)/'requests'/sid
        self.assertFalse(receipts.exists())
        self.assertEqual('conversation_deleted',
                         adapter.requests.update(sid,'request-one','started')['reason'])
        self.assertEqual('conversation_deleted',
                         adapter.requests.cancel(sid,'request-one')['reason'])
        adapter.requests.settle(sid,'request-one',{},error='late')
        self.assertFalse(receipts.exists())

    def test_all_engine_binding_paths_are_erased(self):
        root=Path(self.tmp.name)/'all-engines';root.mkdir()
        sid='adp_physical_all';bindings={}
        for engine in ('claude','pi','codex'):
            bid='bind_'+engine+'_physical'
            bindings[bid]={'binding_id':bid,'conversation_id':sid,'engine':engine}
        state={'conversation_id':sid,'bindings':bindings,'pending_switch':None}
        mark_deleted(root,sid)
        for bid,row in bindings.items():
            engine=row['engine']
            common=[root/'sources'/bid/'receipt',root/'ownership'/(bid+'.json'),
                    root/'resident'/'writer-locks'/(bid+'.lock'),
                    root/'resident'/'claude'/bid/'content',
                    root/'resident'/'codex-family'/engine/bid/'content']
            for path in common:
                path.parent.mkdir(parents=True,exist_ok=True);path.write_text('secret')
            if engine=='claude':
                paths=[root/'native'/'claude'/(bid+'.sqlite3'),
                       root/'native'/'claude'/(bid+'.sqlite3-wal')]
                payload=root/'native'/'claude'/'.shepherd-claude-probe.json'
                payload.parent.mkdir(parents=True,exist_ok=True)
                payload.write_text(json.dumps({'dbPath':str(paths[0]),'prompt':'secret'}))
                payload.chmod(0o600)
            elif engine=='pi':
                paths=[root/'native'/'pi'/(bid+'.jsonl'),root/'native'/'pi'/bid/'content']
            else:
                paths=[root/'native'/engine/bid/'content']
            for path in paths:
                path.parent.mkdir(parents=True,exist_ok=True);path.write_text('secret')
        purge(root,sid,state)
        for bid,row in bindings.items():
            engine=row['engine']
            self.assertFalse((root/'sources'/bid).exists())
            self.assertFalse((root/'resident'/'claude'/bid).exists())
            self.assertFalse((root/'resident'/'codex-family'/engine/bid).exists())
            self.assertFalse((root/'native'/engine/bid).exists())
            self.assertFalse((root/'native'/engine/(bid+'.jsonl')).exists())
            self.assertFalse((root/'native'/engine/(bid+'.sqlite3')).exists())
        self.assertFalse((root/'native'/'claude'/'.shepherd-claude-probe.json').exists())

    def test_execution_scratch_name_encodes_exact_binding(self):
        root=Path(self.tmp.name)/'encoded-scratch';root.mkdir()
        bid='bind_encoded_scratch'
        with ExecutionScratch(root,{'binding_id':bid},'turn:1') as scratch:
            self.assertTrue(scratch.path.name.startswith(self.scratch_name(bid,'')))
            self.assertEqual(bid,_scratch_binding_from_name(scratch.path.name))

    def test_empty_binding_delete_does_not_scan_unrelated_broken_scratch(self):
        root=Path(self.tmp.name)/'empty-bindings';root.mkdir()
        sid='adp_empty_bindings_delete';state={'conversation_id':sid,'bindings':{}}
        scratch=root/'execution'/'turn-unrelated';scratch.mkdir(parents=True,mode=0o700)
        scratch.chmod(0o700)
        owner=scratch/'owner.json'
        owner.write_text('{broken json');owner.chmod(0o600)
        (root/sid).mkdir()
        mark_deleted(root,sid)
        purge(root,sid,state)
        self.assertTrue(scratch.exists())
        self.assertFalse((root/sid).exists())

    def test_unrelated_scratch_ownership_mismatch_does_not_block_delete(self):
        root=Path(self.tmp.name)/'unrelated-scratch';root.mkdir()
        sid='adp_unrelated_scratch';bid='bind_target_delete'
        state={'conversation_id':sid,'bindings':{bid:{'binding_id':bid,
            'conversation_id':sid,'engine':'pi'}}}
        directory=root/'execution';directory.mkdir(mode=0o700);directory.chmod(0o700)
        scratch=directory/'turn-unrelated';scratch.mkdir(mode=0o700);scratch.chmod(0o700)
        owner=scratch/'owner.json'
        owner.write_text(json.dumps({'kind':'shepherd-execution-v1','name':'wrong-name',
            'inode':0,'device':0,'host':{},'binding_id':'bind_other_conversation',
            'processes':[],'spawning':False,'released':True}))
        owner.chmod(0o600)
        (root/sid).mkdir()
        mark_deleted(root,sid)
        purge(root,sid,state)
        self.assertTrue(scratch.exists())
        self.assertFalse((root/sid).exists())

    def test_unrelated_invalid_legacy_binding_hint_does_not_block_delete(self):
        root=Path(self.tmp.name)/'invalid-binding-hint';root.mkdir()
        sid='adp_invalid_binding_hint';bid='bind_invalid_binding_hint'
        state={'conversation_id':sid,'bindings':{bid:{'binding_id':bid,
            'conversation_id':sid,'engine':'pi'}}}
        directory=root/'execution';directory.mkdir(mode=0o700);directory.chmod(0o700)
        scratch=directory/'turn-invalid';scratch.mkdir(mode=0o700);scratch.chmod(0o700)
        owner=scratch/'owner.json'
        owner.write_text(json.dumps({'binding_id':'../not-a-binding'}));owner.chmod(0o600)
        (root/sid).mkdir();mark_deleted(root,sid)
        purge(root,sid,state)
        self.assertTrue(scratch.exists());self.assertFalse((root/sid).exists())

    def test_unrelated_encoded_prefix_collision_does_not_block_delete(self):
        root=Path(self.tmp.name)/'encoded-prefix-collision';root.mkdir()
        sid='adp_encoded_prefix_collision';bid='bind_prefix'
        state={'conversation_id':sid,'bindings':{bid:{'binding_id':bid,
            'conversation_id':sid,'engine':'pi'}}}
        directory=root/'execution';directory.mkdir(mode=0o700);directory.chmod(0o700)
        scratch=directory/self.scratch_name('bind_prefix_extra','broken')
        scratch.mkdir(mode=0o700);scratch.chmod(0o700)
        owner=scratch/'owner.json';owner.write_text('{broken json');owner.chmod(0o600)
        (root/sid).mkdir();mark_deleted(root,sid)
        purge(root,sid,state)
        self.assertTrue(scratch.exists());self.assertFalse((root/sid).exists())

    def test_target_encoded_scratch_invalid_hint_still_blocks_delete(self):
        root=Path(self.tmp.name)/'target-invalid-binding-hint';root.mkdir()
        sid='adp_target_invalid_binding_hint';bid='bind_target_invalid_binding_hint'
        state={'conversation_id':sid,'bindings':{bid:{'binding_id':bid,
            'conversation_id':sid,'engine':'pi'}}}
        directory=root/'execution';directory.mkdir(mode=0o700);directory.chmod(0o700)
        scratch=directory/self.scratch_name(bid,'invalid')
        scratch.mkdir(mode=0o700);scratch.chmod(0o700)
        owner=scratch/'owner.json'
        owner.write_text(json.dumps({'binding_id':'../not-a-binding'}));owner.chmod(0o600)
        lease=scratch/'lease.lock';lease.touch(mode=0o600);lease.chmod(0o600)
        (root/sid).mkdir();mark_deleted(root,sid)
        with self.assertRaisesRegex(Conflict,'ownership metadata invalid'):
            purge(root,sid,state)
        self.assertTrue(scratch.exists());self.assertTrue((root/sid).exists())

    def test_target_scratch_ownership_mismatch_still_blocks_delete(self):
        root=Path(self.tmp.name)/'target-scratch';root.mkdir()
        sid='adp_target_scratch';bid='bind_target_scratch'
        state={'conversation_id':sid,'bindings':{bid:{'binding_id':bid,
            'conversation_id':sid,'engine':'pi'}}}
        directory=root/'execution';directory.mkdir(mode=0o700);directory.chmod(0o700)
        scratch=directory/self.scratch_name(bid,'mismatch')
        scratch.mkdir(mode=0o700);scratch.chmod(0o700)
        owner=scratch/'owner.json'
        owner.write_text(json.dumps({'kind':'shepherd-execution-v1','name':'wrong-name',
            'inode':0,'device':0,'host':{},'binding_id':bid,
            'processes':[],'spawning':False,'released':True}))
        owner.chmod(0o600)
        lease=scratch/'lease.lock';lease.touch(mode=0o600);lease.chmod(0o600)
        (root/sid).mkdir()
        mark_deleted(root,sid)
        with self.assertRaisesRegex(Conflict,'scratch ownership mismatch'):
            purge(root,sid,state)
        self.assertTrue(scratch.exists())
        self.assertTrue((root/sid).exists())

    def test_target_scratch_is_reread_only_after_lease_lock(self):
        root=Path(self.tmp.name)/'scratch-lock-order';root.mkdir()
        sid='adp_scratch_lock_order';bid='bind_scratch_lock_order'
        state={'conversation_id':sid,'bindings':{bid:{'binding_id':bid,
            'conversation_id':sid,'engine':'pi'}}}
        scratch,_data=self.scratch_fixture(root,self.scratch_name(bid,'lock-order'),bid)
        (root/sid).mkdir();mark_deleted(root,sid)
        from shepherd.execution_scratch import _read as original_read
        locked_during_read=[]
        def assert_locked(path):
            probe=os.open(path/'lease.lock',os.O_RDWR|os.O_NOFOLLOW)
            try:
                try:fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:locked_during_read.append(True)
                else:self.fail('scratch metadata was reread before the lease lock')
            finally:os.close(probe)
            return original_read(path)
        with mock.patch('shepherd.execution_scratch._read',side_effect=assert_locked):
            purge(root,sid,state)
        self.assertEqual([True],locked_during_read)
        self.assertFalse(scratch.exists())

    def test_target_scratch_lease_blocks_delete(self):
        root=Path(self.tmp.name)/'scratch-lease';root.mkdir()
        sid='adp_scratch_lease';bid='bind_scratch_lease'
        state={'conversation_id':sid,'bindings':{bid:{'binding_id':bid,
            'conversation_id':sid,'engine':'pi'}}}
        scratch,_data=self.scratch_fixture(root,self.scratch_name(bid,'lease'),bid)
        fd=os.open(scratch/'lease.lock',os.O_RDWR|os.O_NOFOLLOW)
        fcntl.flock(fd,fcntl.LOCK_EX)
        (root/sid).mkdir();mark_deleted(root,sid)
        try:
            with self.assertRaisesRegex(Conflict,'临时目录仍被占用'):
                purge(root,sid,state)
        finally:os.close(fd)
        self.assertTrue(scratch.exists());self.assertTrue((root/sid).exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX mode bits are not a Windows ACL contract')
    def test_target_scratch_non_private_lease_blocks_delete(self):
        root=Path(self.tmp.name)/'scratch-lease-permission';root.mkdir()
        sid='adp_scratch_lease_permission';bid='bind_scratch_lease_permission'
        state={'conversation_id':sid,'bindings':{bid:{'binding_id':bid,
            'conversation_id':sid,'engine':'pi'}}}
        scratch,_data=self.scratch_fixture(root,self.scratch_name(bid,'permission'),bid)
        (scratch/'lease.lock').chmod(0o644)
        (root/sid).mkdir();mark_deleted(root,sid)
        with self.assertRaisesRegex(Conflict,'Unowned Shepherd scratch path'):
            purge(root,sid,state)
        self.assertTrue(scratch.exists());self.assertTrue((root/sid).exists())

    def test_symlinked_execution_directory_cannot_escape_root(self):
        root=Path(self.tmp.name)/'scratch-symlink-root';root.mkdir()
        outside=Path(self.tmp.name)/'scratch-symlink-outside';outside.mkdir()
        sid='adp_scratch_symlink';bid='bind_scratch_symlink'
        state={'conversation_id':sid,'bindings':{bid:{'binding_id':bid,
            'conversation_id':sid,'engine':'pi'}}}
        keep=outside/'keep';keep.write_text('keep')
        (root/'execution').symlink_to(outside,target_is_directory=True)
        (root/sid).mkdir();mark_deleted(root,sid)
        with self.assertRaisesRegex(Conflict,'越过私有目录|不能经过符号链接'):
            purge(root,sid,state)
        self.assertEqual('keep',keep.read_text());self.assertTrue((root/sid).exists())

    def test_target_scratch_invalid_process_row_blocks_delete(self):
        root=Path(self.tmp.name)/'scratch-process-shape';root.mkdir()
        sid='adp_scratch_process_shape';bid='bind_scratch_process_shape'
        state={'conversation_id':sid,'bindings':{bid:{'binding_id':bid,
            'conversation_id':sid,'engine':'pi'}}}
        scratch,_data=self.scratch_fixture(
            root,self.scratch_name(bid,'invalid-process'),bid,processes=[None])
        (root/sid).mkdir();mark_deleted(root,sid)
        with self.assertRaisesRegex(Conflict,'scratch ownership mismatch'):
            purge(root,sid,state)
        self.assertTrue(scratch.exists());self.assertTrue((root/sid).exists())

    def test_writer_lock_blocks_content_removal(self):
        adapter=self.adapter()
        sid=adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        state=adapter.service.store.load_raw(sid);bid=next(iter(state['bindings']))
        mark_deleted(adapter.shepherd_root,sid)
        lock=Path(adapter.shepherd_root)/'resident'/'writer-locks'/(bid+'.lock')
        lock.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(lock,os.O_CREAT|os.O_RDWR,0o600);fcntl.flock(fd,fcntl.LOCK_EX)
        try:
            with self.assertRaisesRegex(Conflict,'Writer 锁尚未释放'):
                purge(adapter.shepherd_root,sid,state)
            self.assertTrue((Path(adapter.shepherd_root)/sid/'state.json').exists())
        finally:
            fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)
        purge(adapter.shepherd_root,sid,state)
        self.assertFalse((Path(adapter.shepherd_root)/sid).exists())

    def test_symlinked_delete_marker_directory_is_rejected(self):
        root=Path(self.tmp.name)/'marker-root';root.mkdir()
        outside=Path(self.tmp.name)/'marker-outside';outside.mkdir()
        (root/'deleted').symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(Conflict,'标记目录不能是符号链接'):
            mark_deleted(root,'adp_marker_escape')
        self.assertEqual([],list(outside.iterdir()))

    def test_symlinked_parent_cannot_escape_private_root(self):
        adapter=self.adapter()
        sid=adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        state=adapter.service.store.load_raw(sid);bid=next(iter(state['bindings']))
        mark_deleted(adapter.shepherd_root,sid)
        root=Path(adapter.shepherd_root);outside=Path(self.tmp.name)/'outside';outside.mkdir()
        witness=outside/bid;witness.mkdir();(witness/'keep').write_text('safe')
        sources=root/'sources'
        if sources.exists():
            for child in sources.iterdir():
                if child.name==bid:
                    import shutil;shutil.rmtree(child)
            if not any(sources.iterdir()):sources.rmdir()
        sources.symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(Conflict,'符号链接|越过私有目录'):
            purge(adapter.shepherd_root,sid,state)
        self.assertEqual('safe',(witness/'keep').read_text())
        self.assertTrue((root/sid/'state.json').exists())


if __name__=='__main__':unittest.main()
