import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import lifecycle
import runtime_identity
from system import api


class RestartLifecycle(unittest.TestCase):
    def setUp(self):
        lifecycle._STATE = 'running'; lifecycle._OPERATION = ''
        lifecycle._ACTIVE = 0; lifecycle._CRITICAL.clear(); lifecycle._BLOCKER_PROVIDERS.clear()

    def tearDown(self):
        lifecycle._STATE = 'running'; lifecycle._OPERATION = ''
        lifecycle._ACTIVE = 0; lifecycle._CRITICAL.clear(); lifecycle._BLOCKER_PROVIDERS.clear()

    def test_blocker_rejects_without_closing_admission(self):
        self.assertTrue(lifecycle.begin(critical='external_write'))
        got = lifecycle.prepare_restart('a' * 32, runtime_identity.BOOT_ID)
        self.assertFalse(got['ok']); self.assertEqual(got['blockers'], [{'kind':'external_write','count':1}])
        self.assertTrue(lifecycle.admitted())
        lifecycle.finish(critical='external_write')

    def test_short_local_critical_section_gets_bounded_grace(self):
        self.assertTrue(lifecycle.begin(critical='http_write'))
        thread=threading.Thread(target=lambda:(time.sleep(.05),lifecycle.finish(critical='http_write')))
        thread.start();started=time.monotonic()
        got=lifecycle.prepare_restart('a'*32,runtime_identity.BOOT_ID)
        thread.join()
        self.assertTrue(got['ok']);self.assertGreaterEqual(time.monotonic()-started,.04)

    def test_external_blocker_is_rejected_without_waiting(self):
        self.assertTrue(lifecycle.begin(critical='external_write'));started=time.monotonic()
        got=lifecycle.prepare_restart('a'*32,runtime_identity.BOOT_ID)
        self.assertFalse(got['ok']);self.assertLess(time.monotonic()-started,.2)
        lifecycle.finish(critical='external_write')

    def test_external_blocker_preempts_short_grace(self):
        self.assertTrue(lifecycle.begin(critical='http_write'))
        self.assertTrue(lifecycle.begin(critical='external_write'));started=time.monotonic()
        got=lifecycle.prepare_restart('a'*32,runtime_identity.BOOT_ID)
        self.assertFalse(got['ok']);self.assertLess(time.monotonic()-started,.2)
        self.assertEqual(got['blockers'],[{'kind':'external_write','count':1}])
        lifecycle.finish(critical='http_write');lifecycle.finish(critical='external_write')

    def test_restart_closes_admission_without_waiting_for_recoverable_turn(self):
        self.assertTrue(lifecycle.begin())
        got = lifecycle.prepare_restart('a' * 32, runtime_identity.BOOT_ID)
        self.assertTrue(got['ok']); self.assertFalse(lifecycle.begin())
        lifecycle.finish()

    def test_explicit_host_rejection_reopens_gate(self):
        with mock.patch.object(lifecycle, '_system_blockers', return_value=[]), \
             mock.patch.object(lifecycle, 'submit_host_restart',
                               return_value={'ok':False,'state':'rejected','error':'identity'}):
            got = api.restart_backend('a' * 32, runtime_identity.BOOT_ID)
        self.assertFalse(got['ok']); self.assertTrue(lifecycle.admitted())

    def test_unknown_ack_never_reopens_gate(self):
        with mock.patch.object(lifecycle, '_system_blockers', return_value=[]), \
             mock.patch.object(lifecycle, 'submit_host_restart',
                               return_value={'ok':False,'state':'unknown','error':'lost'}):
            got = api.restart_backend('a' * 32, runtime_identity.BOOT_ID)
        self.assertEqual(got['state'], 'unknown'); self.assertFalse(lifecycle.admitted())

    def test_durable_host_request_and_lost_ack_recovery(self):
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(lifecycle, '_control_root', return_value=root), \
             mock.patch.object(lifecycle, 'HOST_CONTROL_TOKEN', 'token'), \
             mock.patch.object(runtime_identity, 'facts', return_value={
                 'supervised':True,'host_instance_id':'h'*32,'launch_generation':'g'*32,
                 'boot_id':'b'*24,'pid':42}):
            def host():
                request = Path(root) / ('request-' + 'a'*32 + '.json')
                deadline = time.monotonic() + 2
                while not request.exists() and time.monotonic() < deadline: time.sleep(.01)
                value = json.loads(request.read_text())
                self.assertEqual(value['pid'], 42)
                response = Path(root) / ('response-' + 'a'*32 + '.json')
                response.write_text(json.dumps({'operation_id':'a'*32,'state':'accepted','error':None,
                    'host_instance_id':'h'*32,'request_launch_generation':'g'*32,
                    'request_boot_id':'b'*24,'request_pid':42}))
            thread = threading.Thread(target=host); thread.start()
            got = lifecycle.submit_host_restart('a'*32, expires_s=2)
            thread.join()
        self.assertTrue(got['ok']); self.assertEqual(got['state'],'accepted')

    def test_native_update_request_uses_separate_exact_identity_channel(self):
        facts = {'supervised': True, 'supervisor': 'native',
                 'host_instance_id': 'h' * 32, 'launch_generation': 'g' * 32,
                 'boot_id': 'b' * 24, 'pid': 42}
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(lifecycle, '_control_root', return_value=root), \
             mock.patch.object(lifecycle, 'HOST_CONTROL_TOKEN', 'token'), \
             mock.patch.object(runtime_identity, 'facts', return_value=facts):
            def host():
                request = Path(root) / ('update-request-' + 'a' * 32 + '.json')
                deadline = time.monotonic() + 2
                while not request.exists() and time.monotonic() < deadline: time.sleep(.01)
                value = json.loads(request.read_text())
                self.assertEqual(value['pid'], 42); self.assertEqual(value['token'], 'token')
                response = Path(root) / ('update-response-' + 'a' * 32 + '.json')
                response.write_text(json.dumps({'operation_id': 'a' * 32, 'state': 'accepted',
                    'host_instance_id': 'h' * 32, 'request_launch_generation': 'g' * 32,
                    'request_boot_id': 'b' * 24, 'request_pid': 42}))
            thread = threading.Thread(target=host); thread.start()
            got = lifecycle.submit_host_update('a' * 32, expires_s=2)
            thread.join()
        self.assertTrue(got['ok']); self.assertEqual(got['state'], 'accepted')

    def test_unclaimed_native_update_is_cancelled_but_locked_timeout_is_unknown(self):
        facts = {'supervised': True, 'supervisor': 'native',
                 'host_instance_id': 'h' * 32, 'launch_generation': 'g' * 32,
                 'boot_id': 'b' * 24, 'pid': 42}
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(lifecycle, '_control_root', return_value=root), \
             mock.patch.object(lifecycle, 'HOST_CONTROL_TOKEN', 'token'), \
             mock.patch.object(runtime_identity, 'facts', return_value=facts):
            got = lifecycle.submit_host_update('a' * 32, expires_s=.02)
            self.assertEqual(got['state'], 'not_found')
            held = lifecycle.try_update_lock()
            try:
                got = lifecycle.submit_host_update('b' * 32, expires_s=.02)
            finally:
                lifecycle.release_update_lock(held)
        self.assertEqual(got['state'], 'unknown')

    def test_systemd_restart_is_accepted_by_external_exact_pid_helper(self):
        result=mock.Mock(returncode=0,stdout='',stderr='')
        facts={'supervisor':'systemd','boot_id':'boot','pid':42}
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(runtime_identity,'facts',return_value=facts), \
             mock.patch.object(lifecycle,'_control_root',return_value=root), \
             mock.patch.object(lifecycle,'_systemd_unit_exists',return_value=False), \
             mock.patch.object(lifecycle,'_linux_start_token',return_value='123'), \
             mock.patch.object(lifecycle.subprocess,'run',return_value=result) as run:
            got=lifecycle.submit_host_restart('a'*32)
        self.assertTrue(got['ok']);self.assertEqual(got['state'],'accepted')
        argv=run.call_args.args[0]
        self.assertEqual(argv[:5],['systemd-run','--user','--unit='+'desktron-restart-'+'a'*32,
                                   '--on-active=1s','--collect'])
        self.assertIn('--boot',argv);self.assertIn('boot',argv);self.assertIn('--start-token',argv)
        self.assertIn('--port',argv);self.assertIn('--result',argv);self.assertIn('--operation-id',argv)

    def test_systemd_duplicate_operation_reuses_existing_job(self):
        with mock.patch.object(runtime_identity,'facts',return_value={'supervisor':'systemd'}), \
             mock.patch.object(lifecycle,'_systemd_unit_exists',side_effect=[True]) as exists, \
             mock.patch.object(lifecycle.subprocess,'run') as run:
            got=lifecycle.submit_host_restart('a'*32)
        self.assertTrue(got['ok']);self.assertEqual(got['state'],'accepted')
        exists.assert_called_once();run.assert_not_called()

    def test_systemd_rejection_reopens_through_api(self):
        result=mock.Mock(returncode=1,stdout='',stderr='manager unavailable')
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(lifecycle,'_system_blockers',return_value=[]), \
             mock.patch.object(lifecycle,'_control_root',return_value=root), \
             mock.patch.object(runtime_identity,'facts',return_value={'supervisor':'systemd','pid':42,'boot_id':'b'}), \
             mock.patch.object(lifecycle,'_systemd_unit_exists',return_value=False), \
             mock.patch.object(lifecycle,'_systemd_unit_state',return_value='not_found'), \
             mock.patch.object(lifecycle,'_linux_start_token',return_value='123'), \
             mock.patch.object(lifecycle.subprocess,'run',return_value=result):
            got=api.restart_backend('a'*32, runtime_identity.BOOT_ID)
        self.assertFalse(got['ok']);self.assertTrue(lifecycle.admitted())

    def test_systemd_submit_timeout_stays_closed_when_manager_is_unknown(self):
        facts={'supervisor':'systemd','pid':42,'boot_id':runtime_identity.BOOT_ID}
        with mock.patch.object(lifecycle,'_system_blockers',return_value=[]), \
             mock.patch.object(runtime_identity,'facts',return_value=facts), \
             mock.patch.object(lifecycle,'_systemd_unit_exists',return_value=False), \
             mock.patch.object(lifecycle,'_linux_start_token',return_value='123'), \
             mock.patch.object(lifecycle,'_systemd_unit_state',return_value='unknown'), \
             mock.patch.object(lifecycle.subprocess,'run',side_effect=subprocess.TimeoutExpired('systemd-run',5)), \
             mock.patch.object(lifecycle,'reconcile_host_restart') as reconcile:
            got=api.restart_backend('a'*32,runtime_identity.BOOT_ID)
        self.assertEqual(got['state'],'unknown');self.assertFalse(lifecycle.admitted())
        self.assertTrue(reconcile.called)

    def test_systemd_query_timeouts_converge_to_visible_error(self):
        facts={'supervisor':'systemd','pid':42,'boot_id':runtime_identity.BOOT_ID}
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(lifecycle,'_system_blockers',return_value=[]), \
             mock.patch.object(lifecycle,'_control_root',return_value=root), \
             mock.patch.object(runtime_identity,'facts',return_value=facts), \
             mock.patch.object(lifecycle,'_linux_start_token',return_value='123'), \
             mock.patch.object(lifecycle.subprocess,'run',side_effect=subprocess.TimeoutExpired('systemctl',5)), \
             mock.patch.object(lifecycle,'reconcile_host_restart') as reconcile:
            got=api.restart_backend('a'*32,runtime_identity.BOOT_ID)
        self.assertEqual(got['state'],'unknown');self.assertFalse(lifecycle.admitted())
        reconcile.assert_called_once()
        # Exercise the real reconciliation loop with the same underlying timeout.
        with mock.patch.object(lifecycle.subprocess,'run',side_effect=subprocess.TimeoutExpired('systemctl',5)), \
             mock.patch.object(lifecycle.time,'monotonic',side_effect=[0,1]), \
             mock.patch.object(lifecycle.time,'sleep'):
            self.assertEqual(lifecycle.reconcile_host_restart('a'*32,facts,timeout=.1),'restart_error')
        self.assertEqual(lifecycle.status()['state'],'restart_error')

    def test_submit_exception_reopens_when_request_was_never_claimed(self):
        with mock.patch.object(lifecycle,'_system_blockers',return_value=[]), \
             mock.patch.object(lifecycle,'submit_host_restart',side_effect=OSError('disk')) , \
             mock.patch.object(lifecycle,'cancel_unclaimed',return_value=True):
            got=api.restart_backend('a'*32,runtime_identity.BOOT_ID)
        self.assertFalse(got['ok']);self.assertEqual(got['state'],'rejected')

    def test_runtime_status_exposes_restart_error(self):
        lifecycle._STATE='restart_error';lifecycle._OPERATION='a'*32;lifecycle._ERROR='host unavailable'
        got=api.runtime_status()['lifecycle']
        self.assertEqual(got['state'],'restart_error');self.assertEqual(got['error'],'host unavailable')

    def test_background_reconcile_reopens_rejected_ack(self):
        lifecycle._STATE='stopping';lifecycle._OPERATION='a'*32
        facts={'host_instance_id':'h','launch_generation':'g','boot_id':'b','pid':42}
        with mock.patch.object(lifecycle,'_read_response',return_value={
                'operation_id':'a'*32,'state':'rejected'}):
            self.assertEqual(lifecycle.reconcile_host_restart('a'*32,facts,timeout=.1),'rejected')
        self.assertTrue(lifecycle.admitted())

    def test_background_reconcile_surfaces_unknown_state(self):
        lifecycle._STATE='stopping';lifecycle._OPERATION='a'*32
        with mock.patch.object(lifecycle,'_read_response',return_value=None), \
             mock.patch.object(lifecycle.time,'monotonic',side_effect=[0,1]), \
             mock.patch.object(lifecycle.time,'sleep'):
            self.assertEqual(lifecycle.reconcile_host_restart('a'*32,{},timeout=.1),'restart_error')
        self.assertEqual(lifecycle.status()['state'],'restart_error')
        self.assertFalse(lifecycle.admitted())

    def test_old_boot_response_is_not_accepted(self):
        facts={'supervised':True,'supervisor':'native','host_instance_id':'h'*32,
               'launch_generation':'g'*32,'boot_id':'newboot','pid':42}
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(lifecycle,'_control_root',return_value=root), \
             mock.patch.object(lifecycle,'HOST_CONTROL_TOKEN','token'), \
             mock.patch.object(runtime_identity,'facts',return_value=facts):
            response=Path(root)/('response-'+'a'*32+'.json')
            response.write_text(json.dumps({'operation_id':'a'*32,'state':'completed',
                'host_instance_id':'h'*32,'request_launch_generation':'oldgen',
                'request_boot_id':'oldboot','request_pid':41}))
            got=lifecycle.submit_host_restart('a'*32,expires_s=.02)
        self.assertFalse(got['ok']);self.assertEqual(got['state'],'not_found')

    def test_timeout_with_host_lock_is_unknown_not_cancelled(self):
        facts={'supervised':True,'supervisor':'native','host_instance_id':'h'*32,
               'launch_generation':'g'*32,'boot_id':'boot','pid':42}
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(lifecycle,'_control_root',return_value=root), \
             mock.patch.object(lifecycle,'HOST_CONTROL_TOKEN','token'), \
             mock.patch.object(runtime_identity,'facts',return_value=facts):
            held=lifecycle.try_update_lock()
            try: got=lifecycle.submit_host_restart('a'*32,expires_s=.02)
            finally: lifecycle.release_update_lock(held)
        self.assertFalse(got['ok']);self.assertEqual(got['state'],'unknown')

    def test_cross_process_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(lifecycle, '_control_root', return_value=root):
            first = lifecycle.try_update_lock(); self.assertIsNotNone(first)
            self.assertIsNone(lifecycle.try_update_lock())
            lifecycle.release_update_lock(first)
            second = lifecycle.try_update_lock(); self.assertIsNotNone(second)
            lifecycle.release_update_lock(second)

    @unittest.skipIf(os.name == 'nt', 'pass_fds 与继承 flock 是 POSIX 专用契约')
    def test_inherited_lock_survives_parent_fd_close(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(lifecycle,'_control_root',return_value=root):
            fd=lifecycle.try_update_lock();self.assertIsNotNone(fd)
            child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(.5)'],pass_fds=(fd,))
            os.close(fd)
            try:
                self.assertIsNone(lifecycle.try_update_lock())
            finally:
                child.wait(timeout=2)
            again=lifecycle.try_update_lock();self.assertIsNotNone(again)
            lifecycle.release_update_lock(again)


if __name__ == '__main__': unittest.main()
