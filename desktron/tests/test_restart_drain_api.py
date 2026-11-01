"""Host-supervised restart closes admission without draining conversations."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron/lib'))

import lifecycle
import runtime_identity
import self_update
from system import api


class Handler(SimpleNamespace):
    def _send_json(self, body, code=200): return body, code


class RestartApi(unittest.TestCase):
    def setUp(self):
        lifecycle._STATE='running'; lifecycle._OPERATION=''
        lifecycle._ACTIVE=0; lifecycle._CRITICAL.clear(); lifecycle._BLOCKER_PROVIDERS.clear()
        self_update._ACTIVE_WORK=0
        self_update._DRAINING=False; self_update._EXTERNAL_DRAINING=False

    def tearDown(self):
        lifecycle._STATE='running'; lifecycle._OPERATION=''
        lifecycle._ACTIVE=0; lifecycle._CRITICAL.clear(); lifecycle._BLOCKER_PROVIDERS.clear()
        self_update._ACTIVE_WORK=0
        self_update._DRAINING=False; self_update._EXTERNAL_DRAINING=False

    def test_orchestrator_header_only_works_from_loopback(self):
        local=SimpleNamespace(client_address=('127.0.0.1',1),headers={'X-Desktron-Orchestrator':'deploy'})
        remote=SimpleNamespace(client_address=('10.0.0.2',1),headers={'X-Desktron-Orchestrator':'deploy'})
        self.assertTrue(api._local_orchestrator(local));self.assertFalse(api._local_orchestrator(remote))

    def test_route_rejects_remote_and_accepts_local(self):
        body=json.dumps({'operation_id':'a'*32,'expected_boot_id':'boot'}).encode()
        remote=Handler(client_address=('10.0.0.2',1),headers={},rfile=io.BytesIO(body))
        self.assertEqual(api.handle(remote,'/workbench/restart',{},'POST')[1],403)
        local=Handler(client_address=('127.0.0.1',1),
            headers={'X-Desktron-Orchestrator':'deploy','Content-Length':str(len(body))},rfile=io.BytesIO(body))
        with mock.patch.object(api,'restart_backend',return_value={'ok':True}) as restart:
            self.assertEqual(api.handle(local,'/workbench/restart',{},'POST')[1],200)
        restart.assert_called_once_with('a'*32, 'boot')

    def test_stale_boot_is_rejected_before_gate(self):
        result=api.restart_backend('a'*32,'stale')
        self.assertFalse(result['ok']);self.assertTrue(lifecycle.admitted())

    def test_ordinary_turn_does_not_block_restart(self):
        self.assertTrue(self_update.begin_work(restart_blocking=False))
        with mock.patch.object(lifecycle,'_system_blockers',return_value=[]), \
             mock.patch.object(lifecycle,'submit_host_restart',return_value={'ok':True,'state':'accepted'}):
            result=api.restart_backend('a'*32, runtime_identity.BOOT_ID)
        self.assertTrue(result['ok']);self.assertEqual(result['restart_contract'],4)
        self.assertFalse(self_update.begin_work(restart_blocking=False))
        self_update.finish_work(restart_blocking=False)

    def test_no_shepherd_scan_or_wait_contract_remains(self):
        source=(ROOT/'desktron/lib/system/api.py').read_text(encoding='utf-8')
        for old in ('_drain_shepherd_adapter','_RESTART_DRAIN_TARGETS',"op == 'status'",'restart_fence'):
            self.assertNotIn(old,source)

    def test_explicit_blocker_rejects_and_reopens_admission(self):
        self.assertTrue(lifecycle.begin(critical='external_write'))
        with mock.patch.object(lifecycle,'_system_blockers',return_value=[]), \
             mock.patch.object(lifecycle,'submit_host_restart') as submit:
            result=api.restart_backend('a'*32, runtime_identity.BOOT_ID)
        self.assertFalse(result['ok']);self.assertTrue(lifecycle.admitted());submit.assert_not_called()
        lifecycle.finish(critical='external_write')

    def test_host_rejection_reopens_admission(self):
        with mock.patch.object(lifecycle,'_system_blockers',return_value=[]), \
             mock.patch.object(lifecycle,'submit_host_restart',return_value={
                 'ok':False,'state':'rejected','error':'identity_mismatch'}):
            result=api.restart_backend('a'*32, runtime_identity.BOOT_ID)
        self.assertFalse(result['ok']);self.assertTrue(lifecycle.admitted())

    def test_lost_ack_keeps_admission_closed(self):
        with mock.patch.object(lifecycle,'_system_blockers',return_value=[]), \
             mock.patch.object(lifecycle,'submit_host_restart',return_value={
                 'ok':False,'state':'unknown','error':'lost'}):
            result=api.restart_backend('a'*32, runtime_identity.BOOT_ID)
        self.assertEqual(result['state'],'unknown');self.assertFalse(lifecycle.admitted())

    def test_unsupervised_backend_rejects_without_permanent_gate(self):
        with mock.patch.object(lifecycle,'_system_blockers',return_value=[]), \
             mock.patch.object(lifecycle,'submit_host_restart',return_value={
                 'ok':False,'error':'当前 Desk 不由新版原生宿主监督'}):
            result=api.restart_backend('a'*32, runtime_identity.BOOT_ID)
        self.assertFalse(result['ok']);self.assertTrue(lifecycle.admitted())

    def test_concurrent_operation_is_rejected(self):
        lifecycle._STATE='stopping';lifecycle._OPERATION='a'*32
        result=api.restart_backend('b'*32, runtime_identity.BOOT_ID)
        self.assertFalse(result['ok']);self.assertEqual(lifecycle._OPERATION,'a'*32)

    def test_storage_promotion_endpoint_stays_local(self):
        remote=Handler(client_address=('10.0.0.2',1),headers={})
        self.assertEqual(api.handle(remote,'/workbench/shepherd-unified',{},'POST')[1],403)


if __name__=='__main__': unittest.main()
