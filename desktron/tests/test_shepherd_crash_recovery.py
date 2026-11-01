"""Abnormal-start recovery never replays a possibly delivered Shepherd turn."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from shepherd import Coordinator, Store, TurnInputs
from shepherd.coordinator import digest
from shepherd.recovery import CrashRecovery, process_state
from shepherd.recovery_controller import RecoveryController
from shepherd.source_receipts import SourceReceipts
from shepherd.store import (Conflict, SourceShapeUnsupported,
                            SourceVerificationError)


class Connector:
    engine = 'pi'; binding_id = 'binding_pi'; native_session_id = 'native_pi'
    connector_version = 'pi-v1'; ownership_nonce = 'nonce_pi'
    # Generic fixture models a Connector that has an engine-native terminal
    # proof. Real Claude/Pi connectors intentionally leave this disabled.
    supports_interrupted_recovery = True

    def __init__(self, binding, root):
        self.binding = binding; self.store_root = root
        self.interrupted = None; self.resolve_calls = []

    def recovery_boundary(self):
        return {'start_seq': 1, 'native_revision': 'a' * 64}

    def recover_suffix(self, boundary, native_locator):
        self.interrupted = ('recover_suffix', boundary, native_locator)
        if native_locator.get('invalid'):
            raise Conflict('incomplete native suffix')
        public = [
            {'type':'message','role':'user','content':[{'type':'input_text','text':'preserved'}]},
            {'type':'function_call','call_id':'open','name':'read','arguments':'{}'},
            {'type':'function_call_output','call_id':'open','output':'done'},
            {'type':'function_call','call_id':'unfinished','name':'read','arguments':'{}'},
            {'type':'function_call_output','call_id':'unfinished','output':
             '[执行状态未知] 本次执行意外终止，无法确认该工具是否完成；不得自动重试或假定成功/失败。'},
        ]
        return {'locator':{'start_seq':1,'end_seq':3,'terminal_state':'unknown',
                           'native_revision':'b'*64,'unfinished_tool_ids':['unfinished']},
                'source':{'codec':'pi-v1','entries':[{'type':'message'}]},
                'public':public,'unfinished_tool_ids':['unfinished']}

    def interrupted_locator(self, round_id, boundary, native_locator):
        self.interrupted = (round_id, boundary, native_locator)
        if native_locator.get('invalid'):
            raise Conflict('incomplete native suffix')
        return {'start_seq': 1, 'end_seq': 3,
                'terminal_state': 'interrupted_confirmed',
                'native_revision': 'b' * 64}

    def resolve_source(self, source):
        self.resolve_calls.append(source)
        return {'source': {'codec':'pi-v1','entries':[{'type':'message'}]},
                'public': [{'type':'message','role':'user',
                            'content':[{'type':'input_text','text':'preserved'}]}]}


class Service:
    def __init__(self, root):
        self.root = root; self.store = Store(root); self.coordinator = Coordinator(self.store)
        self.connectors = {}

    def connector(self, binding):
        return self.connectors[binding['binding_id']]


class CrashRecoveryCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.service = Service(self.tmp.name); self.cid = 'crash_conversation'
        c = self.service.coordinator; c.create(self.cid)
        self.binding_id = c.bind(self.cid, 'pi', 'native_pi', native_cwd=self.tmp.name,
            connector_version='pi-v1', binding_id='binding_pi', ownership_nonce='nonce_pi')
        self.lease = c.acquire(self.cid, self.binding_id, ownership_nonce='nonce_pi')
        binding = c.state(self.cid)['bindings'][self.binding_id]
        self.connector = Connector(binding, self.tmp.name)
        self.service.connectors[self.binding_id] = self.connector

    def begin(self, phase='dispatched', native_locator=None):
        c = self.service.coordinator
        row = c.begin_round(self.cid, self.lease['lease_id'], self.lease['generation'],
            attempt_id='attempt_crash', recovery={'phase':'reserved', 'boot_id':'old_boot',
                'boundary':self.connector.recovery_boundary()})
        if phase == 'dispatched':
            c.mark_dispatched(self.cid, self.lease['lease_id'], self.lease['generation'],
                attempt_id=row['attempt_id'], process={'pid':99999998,'pgid':99999998,
                                                       'start_token':'old'},
                native_locator=native_locator or {'turn_id':'native_turn'})
        return row

    def test_reserved_crash_is_cancelled_without_native_read_or_replay(self):
        self.begin(phase='reserved')
        report = CrashRecovery(self.service, boot_id='new_boot').recover_one(self.cid)
        self.assertEqual(report['state'], 'cancelled_before_dispatch')
        state = self.service.coordinator.state(self.cid)
        self.assertIsNone(state['inflight'])
        self.assertEqual(state['committed_offset'], 0)
        self.assertEqual(state['reconciliations'][-1]['action'], 'cancel_before_dispatch')
        self.assertEqual(self.connector.resolve_calls, [])

    def test_terminal_receipt_is_committed_without_interrupted_inference(self):
        row = self.begin()
        SourceReceipts(self.tmp.name).write(self.connector.binding, row['round_id'], {
            'start_seq':1, 'end_seq':3, 'terminal_state':'completed',
            'native_revision':'b'*64})
        report = CrashRecovery(self.service, boot_id='new_boot').recover_one(self.cid)
        self.assertEqual(report['state'], 'committed')
        self.assertEqual(report['terminal_state'], 'completed')
        self.assertIsNone(self.connector.interrupted)
        self.assertIsNone(self.service.coordinator.state(self.cid)['inflight'])

    def test_current_boot_source_persisted_can_commit_without_replay(self):
        row = self.begin()
        source = {'kind':'native_round','engine':'pi','session_id':'native_pi',
                  'entry_id':row['round_id'],
                  'source_key':f'pi:native_pi:{row["round_id"]}'}
        self.service.coordinator.source_persisted(
            self.cid, self.lease['lease_id'], self.lease['generation'],
            attempt_id=row['attempt_id'], source=source,
            native_revision='b'*64, terminal_state='completed')
        ordinary = CrashRecovery(self.service, boot_id='old_boot').recover_one(self.cid)
        self.assertEqual(ordinary['state'], 'source_persisted')
        report = CrashRecovery(self.service, boot_id='old_boot').recover_one(
            self.cid, allow_current_boot_commit=True)
        self.assertEqual(report['state'], 'committed')
        self.assertEqual(self.service.coordinator.state(self.cid)['committed_offset'], 1)

    def test_source_persisted_deterministic_verification_failure_blocks_immediately(self):
        row = self.begin()
        source = {'kind':'native_round','engine':'pi','session_id':'native_pi',
                  'entry_id':row['round_id'],
                  'source_key':f'pi:native_pi:{row["round_id"]}'}
        self.service.coordinator.source_persisted(
            self.cid, self.lease['lease_id'], self.lease['generation'],
            attempt_id=row['attempt_id'], source=source,
            native_revision='b'*64, terminal_state='completed')
        self.connector.resolve_source = mock.Mock(
            side_effect=SourceVerificationError('unsupported persisted block'))
        report = CrashRecovery(self.service, boot_id='new_boot').recover_one(self.cid)
        self.assertEqual(report['state'], 'needs_reconciliation')
        state = self.service.coordinator.state(self.cid)
        self.assertEqual(state['inflight']['state'], 'needs_reconciliation')
        self.assertEqual(state['inflight']['terminal_state'], 'completed')
        self.assertEqual(state['inflight']['source'], source)

    def test_source_shape_unsupported_has_distinct_persisted_reason(self):
        row = self.begin()
        source = {'kind':'native_round','engine':'pi','session_id':'native_pi',
                  'entry_id':row['round_id'],
                  'source_key':f'pi:native_pi:{row["round_id"]}'}
        self.service.coordinator.source_persisted(
            self.cid, self.lease['lease_id'], self.lease['generation'],
            attempt_id=row['attempt_id'], source=source,
            native_revision='b'*64, terminal_state='completed')
        self.connector.resolve_source = mock.Mock(side_effect=SourceShapeUnsupported(
            'Claude Shepherd 压缩形状不受支持（Claude Code 2.1.283）'))
        report = CrashRecovery(self.service, boot_id='new_boot').recover_one(self.cid)
        self.assertEqual(report['state'], 'needs_reconciliation')
        self.assertEqual(report['reason'], 'source_shape_unsupported')
        inflight = self.service.coordinator.state(self.cid)['inflight']
        self.assertEqual(inflight['recovery_reason'], 'source_shape_unsupported')
        self.assertIn('Claude Code 2.1.283', inflight['recovery_detail'])

    def test_source_persisted_readback_failure_stays_commit_only(self):
        row = self.begin()
        source = {'kind':'native_round','engine':'pi','session_id':'native_pi',
                  'entry_id':row['round_id'],
                  'source_key':f'pi:native_pi:{row["round_id"]}'}
        self.service.coordinator.source_persisted(
            self.cid, self.lease['lease_id'], self.lease['generation'],
            attempt_id=row['attempt_id'], source=source,
            native_revision='b'*64, terminal_state='completed')
        self.connector.resolve_source = mock.Mock(side_effect=Conflict('temporary readback failure'))
        with mock.patch.object(self.connector, 'interrupted_locator') as interrupted:
            report = CrashRecovery(self.service, boot_id='new_boot').recover_one(self.cid)
        self.assertEqual(report['state'], 'source_persisted')
        self.assertEqual(report['reason'], 'commit_verification_failed')
        interrupted.assert_not_called()
        state = self.service.coordinator.state(self.cid)
        self.assertEqual(state['inflight']['state'], 'source_persisted')
        self.assertEqual(state['inflight']['terminal_state'], 'completed')

    def test_dead_process_without_suffix_recovery_remains_fenced(self):
        self.begin(native_locator={'turn_id':'native_turn'})
        self.connector.recover_suffix = None
        with mock.patch('shepherd.recovery.process_state', return_value='dead'):
            report = CrashRecovery(self.service, boot_id='new_boot').recover_one(self.cid)
        self.assertEqual(report['state'], 'needs_reconciliation')
        self.assertEqual(report['reason'], 'native_suffix_recovery_unavailable')
        self.assertEqual(self.service.coordinator.state(self.cid)['committed_offset'], 0)

    def test_dead_process_with_empty_suffix_keeps_input_and_unknown_boundary(self):
        reference = TurnInputs(self.tmp.name).write(self.cid, self.cid + ':1', [
            {'type':'message','role':'user','content':[{'type':'input_text','text':'question'}]}])
        c = self.service.coordinator
        row = c.begin_round(self.cid, self.lease['lease_id'], self.lease['generation'],
            attempt_id='attempt_empty', input_ref=reference,
            recovery={'phase':'reserved','boot_id':'old_boot','boundary':self.connector.recovery_boundary()})
        c.mark_dispatched(self.cid, self.lease['lease_id'], self.lease['generation'],
            attempt_id=row['attempt_id'], process={'pid':99999998,'pgid':99999998,'start_token':'old'},
            native_locator={'turn_id':'native_turn'})
        self.connector.recover_suffix = lambda *_args: {
            'locator':{'start_seq':1,'end_seq':1,'terminal_state':'unknown',
                       'native_revision':'a'*64,'unfinished_tool_ids':[]},
            'source':{'codec':'pi-v1','entries':[]},'public':[],
            'unfinished_tool_ids':[]}
        with mock.patch('shepherd.recovery.process_state', return_value='dead'):
            report = CrashRecovery(self.service, boot_id='new_boot').recover_one(self.cid)
        self.assertEqual(report['state'], 'settled_incomplete')
        raw = self.service.store.load_raw(self.cid)
        self.assertEqual(raw['turns'][0]['input_ref'], reference)
        self.assertEqual(raw['turns'][0]['outcome'], 'unknown')

    def test_dead_process_seals_unknown_turn_without_replay(self):
        row = self.begin(native_locator={'turn_id':'native_turn'})
        with mock.patch('shepherd.recovery.process_state', return_value='dead'):
            report = CrashRecovery(self.service, boot_id='new_boot').recover_one(self.cid)
        self.assertEqual(report['state'], 'settled_incomplete')
        self.assertEqual(report['terminal_state'], 'unknown')
        self.assertEqual(report['unfinished_tool_ids'], ['unfinished'])
        self.assertEqual(self.connector.interrupted[0], 'recover_suffix')
        receipt = SourceReceipts(self.tmp.name).read(self.connector.binding, row['round_id'])
        self.assertEqual(receipt['locator']['terminal_state'], 'unknown')
        self.assertEqual(receipt['locator']['unfinished_tool_ids'], ['unfinished'])
        state = self.service.coordinator.state(self.cid)
        self.assertEqual(state['committed_offset'], 1)
        self.assertEqual(state['rounds'][0]['terminal_state'], 'unknown')
        self.assertEqual(state['bindings'][self.binding_id]['status'], 'recovery_required')
        self.assertIsNone(state['lease'])

    def test_existing_unknown_receipt_recomputes_compatibility_after_restart(self):
        row = self.begin()
        SourceReceipts(self.tmp.name).write(self.connector.binding, row['round_id'], {
            'start_seq':1, 'end_seq':3, 'terminal_state':'unknown',
            'native_revision':'b'*64, 'unfinished_tool_ids':[]})
        diagnostic = [{'engine':'pi','action':'ignored_internal_entry',
                       'kind':'compaction','count':1}]
        self.connector.source_compatibility = mock.Mock(return_value=diagnostic)
        report = CrashRecovery(self.service, boot_id='new_boot').recover_one(self.cid)
        self.assertEqual(report['state'], 'settled_incomplete')
        self.assertEqual(report['compatibility'], diagnostic)
        self.connector.source_compatibility.assert_called_once_with(
            {'codec':'pi-v1','entries':[{'type':'message'}]})

    def test_unknown_receipt_does_not_bypass_writer_identity(self):
        row = self.begin()
        SourceReceipts(self.tmp.name).write(self.connector.binding, row['round_id'], {
            'start_seq': 1, 'end_seq': 3, 'terminal_state': 'unknown',
            'native_revision': 'b'*64, 'unfinished_tool_ids': []})
        for writer in ('alive', 'unknown'):
            with self.subTest(writer=writer), \
                 mock.patch('shepherd.recovery.process_state', return_value=writer), \
                 mock.patch.object(self.connector, 'resolve_source', side_effect=AssertionError('must remain fenced')):
                report = CrashRecovery(self.service, boot_id='new_boot').recover_one(self.cid)
            self.assertEqual(report['reason'], 'orphan_process_' + writer)
            self.assertEqual(self.service.coordinator.state(self.cid)['committed_offset'], 0)

    def test_alive_reused_or_incomplete_evidence_never_commits(self):
        for process, locator, reason in (
            ('alive', {'turn_id':'native'}, 'orphan_process_alive'),
            ('unknown', {'turn_id':'native'}, 'orphan_process_unknown'),
            ('dead', {'invalid':True}, 'native_evidence_incomplete')):
            with self.subTest(process=process):
                # Fresh state per branch.
                self.tearDown(); self.setUp(); self.begin(native_locator=locator)
                with mock.patch('shepherd.recovery.process_state', return_value=process):
                    report = CrashRecovery(self.service, boot_id='new_boot').recover_one(self.cid)
                self.assertEqual(report['state'], 'needs_reconciliation')
                self.assertEqual(report['reason'], reason)
                state = self.service.coordinator.state(self.cid)
                self.assertEqual(state['committed_offset'], 0)
                self.assertEqual(state['inflight']['state'], 'needs_reconciliation')

    def test_scan_can_be_restricted_to_live_public_conversations(self):
        self.begin(native_locator={'turn_id':'native_turn'})
        reports = CrashRecovery(self.service, boot_id='new_boot').scan([])
        self.assertEqual(reports, [])
        self.assertEqual(self.service.coordinator.state(self.cid)['inflight']['state'], 'running')

    def test_scan_failure_is_isolated_and_original_fence_remains(self):
        self.begin(native_locator={'turn_id':'native_turn'})
        original = self.service.coordinator.state
        def state(conversation_id):
            if conversation_id == self.cid:
                raise Conflict('manifest unreadable')
            return original(conversation_id)
        with mock.patch.object(self.service.coordinator, 'state', side_effect=state):
            reports = CrashRecovery(self.service, boot_id='new_boot').scan()
        self.assertEqual(reports[0]['state'], 'needs_reconciliation')
        self.assertEqual(reports[0]['reason'], 'startup_scan_failed')
        persisted = original(self.cid)
        self.assertEqual(persisted['inflight']['state'], 'running')
        self.assertEqual(persisted['committed_offset'], 0)

    def test_repeated_scan_is_idempotent_and_never_executes(self):
        self.begin(native_locator={'turn_id':'native_turn'})
        with mock.patch('shepherd.recovery.process_state', return_value='dead'):
            first = CrashRecovery(self.service, boot_id='new_boot').scan()
            second = CrashRecovery(self.service, boot_id='new_boot').scan()
        self.assertEqual(first[0]['state'], 'settled_incomplete')
        self.assertEqual(second, [{'conversation_id':self.cid,
            'state':'recovery_binding_required','binding_id':self.binding_id}])
        self.assertEqual(self.service.coordinator.state(self.cid)['committed_offset'], 1)

    def test_process_identity_rejects_reused_pid(self):
        identity = {'pid':123, 'pgid':123, 'start_token':'old'}
        with mock.patch('os.killpg'), mock.patch('os.kill'), \
             mock.patch('os.getpgid', return_value=123), \
             mock.patch('shepherd.recovery._process_start_token', return_value='new'):
            self.assertEqual(process_state(identity), 'unknown')

    def test_process_identity_rejects_pid_moved_to_another_group(self):
        identity = {'pid':123, 'pgid':123, 'start_token':'old'}
        with mock.patch('os.killpg'), mock.patch('os.kill'), \
             mock.patch('os.getpgid', return_value=456):
            self.assertEqual(process_state(identity), 'unknown')

    def test_process_identity_rejects_live_group_without_original_leader(self):
        identity = {'pid':123, 'pgid':123, 'start_token':'old'}
        with mock.patch('os.killpg'), \
             mock.patch('os.kill', side_effect=ProcessLookupError):
            self.assertEqual(process_state(identity), 'unknown')


class ContinuousRecovery(unittest.TestCase):
    setUp = CrashRecoveryCases.setUp
    begin = CrashRecoveryCases.begin

    def test_controller_recovers_receipt_written_before_source_persisted_state(self):
        row = self.begin()
        SourceReceipts(self.tmp.name).write(self.connector.binding, row['round_id'], {
            'start_seq':1, 'end_seq':3, 'terminal_state':'completed',
            'native_revision':'b'*64})
        controller = RecoveryController(self.service, boot_id='old_boot', retry_delays=())
        got = controller.recover_guarded(self.cid, trigger='send_before')
        self.assertEqual(got['outcome'], 'committed')
        self.assertEqual(self.service.coordinator.state(self.cid)['committed_offset'], 1)

    def test_controller_missing_receipt_settles_dead_writer_without_replay(self):
        self.begin()
        with mock.patch.object(self.connector, 'interrupted_locator') as interrupted:
            got = RecoveryController(self.service, boot_id='old_boot', retry_delays=()).recover_guarded(
                self.cid, trigger='send_before')
        self.assertEqual(got['action'], 'settle_incomplete')
        self.assertEqual(got['outcome'], 'settled_incomplete')
        interrupted.assert_not_called()
        state = self.service.coordinator.state(self.cid)
        self.assertIsNone(state['inflight'])
        self.assertEqual(state['rounds'][0]['terminal_state'], 'unknown')

    def test_compatibility_diagnostics_are_audited_without_entering_receipt(self):
        row = self.begin()
        original = self.connector.recover_suffix
        def recover(*args):
            result = original(*args)
            result['compatibility'] = [{'engine':'pi',
                'action':'ignored_internal_entry','kind':'compaction','count':2}]
            return result
        self.connector.recover_suffix = recover
        reports = []
        got = RecoveryController(self.service, boot_id='old_boot', retry_delays=(),
                                 report_callback=reports.append).recover_guarded(
                                     self.cid, trigger='send_before')
        self.assertEqual(got['compatibility'], [{'engine':'pi',
            'action':'ignored_internal_entry','kind':'compaction','count':2}])
        self.assertEqual(reports, [got])
        receipt = SourceReceipts(self.tmp.name).read(self.connector.binding, row['round_id'])
        self.assertNotIn('compatibility', receipt['locator'])

    def test_blocked_turn_never_reopens_receipt_or_source(self):
        row = self.begin()
        self.service.coordinator.mark_unresolved(
            self.cid, self.lease['lease_id'], self.lease['generation'],
            attempt_id=row['attempt_id'], reason='native_evidence_incomplete')
        self.connector.resolve_source = mock.Mock(side_effect=AssertionError('blocked source reopened'))
        controller = RecoveryController(self.service, boot_id='old_boot', retry_delays=())
        for trigger in ('send_before','background_retry','startup'):
            got = controller.recover_guarded(self.cid, trigger=trigger)
            self.assertEqual(got['action'], 'isolation_required')
            self.assertEqual(got['outcome'], 'needs_reconciliation')
        self.connector.resolve_source.assert_not_called()
        self.assertEqual(self.service.coordinator.state(self.cid)['inflight']['state'],
                         'needs_reconciliation')

    def test_source_persisted_retry_exhaustion_becomes_explicit_isolation(self):
        row = self.begin()
        source = {'kind':'native_round','engine':'pi','session_id':'native_pi',
                  'entry_id':row['round_id'],
                  'source_key':f'pi:native_pi:{row["round_id"]}'}
        self.service.coordinator.source_persisted(
            self.cid, self.lease['lease_id'], self.lease['generation'],
            attempt_id=row['attempt_id'], source=source,
            native_revision='b'*64, terminal_state='completed')
        self.connector.resolve_source = mock.Mock(side_effect=Conflict('deterministic projection failure'))
        controller = RecoveryController(self.service, boot_id='old_boot', retry_delays=())
        got = controller.recover_guarded(self.cid, trigger='send_before')
        self.assertEqual(got['action'], 'isolation_required')
        self.assertEqual(got['outcome'], 'needs_reconciliation')
        state = self.service.coordinator.state(self.cid)
        self.assertEqual(state['inflight']['state'], 'needs_reconciliation')
        self.assertEqual(state['inflight']['terminal_state'], 'completed')
        self.assertEqual(state['inflight']['source'], source)
        self.assertEqual(state['inflight']['recovery_reason'], 'commit_verification_failed')

    def test_source_persisted_transient_failure_retries_before_isolation(self):
        row = self.begin()
        source = {'kind':'native_round','engine':'pi','session_id':'native_pi',
                  'entry_id':row['round_id'],
                  'source_key':f'pi:native_pi:{row["round_id"]}'}
        self.service.coordinator.source_persisted(
            self.cid, self.lease['lease_id'], self.lease['generation'],
            attempt_id=row['attempt_id'], source=source,
            native_revision='b'*64, terminal_state='completed')
        self.connector.resolve_source = mock.Mock(side_effect=Conflict('temporary readback failure'))
        callback = mock.Mock()
        first = RecoveryController(self.service, boot_id='old_boot',
                                   retry_delays=(60,), retry_callback=callback)
        got = first.recover_guarded(self.cid, trigger='send_before')
        first.stop()
        self.assertEqual(got['action'], 'commit_only_retry')
        self.assertEqual(got['outcome'], 'source_persisted')
        state = self.service.coordinator.state(self.cid)
        self.assertEqual(state['inflight']['state'], 'source_persisted')
        self.assertEqual(state['inflight']['recovery_attempts'], 1)
        # A fresh controller simulates a Desk restart. The durable attempt count
        # prevents an endless new retry budget on every process lifetime.
        second = RecoveryController(self.service, boot_id='new_boot',
                                    retry_delays=(60,), retry_callback=mock.Mock())
        after = second.recover_guarded(self.cid, trigger='startup')
        second.stop()
        self.assertEqual(after['action'], 'isolation_required')
        self.assertEqual(after['outcome'], 'needs_reconciliation')
        self.assertEqual(self.service.coordinator.state(self.cid)['inflight']['state'],
                         'needs_reconciliation')

    def test_controller_commits_once_and_settles_only_linked_request(self):
        from shepherd.requests import Requests
        row = self.begin()
        source = {'kind':'native_round','engine':'pi','session_id':'native_pi',
                  'entry_id':row['round_id'],
                  'source_key':f'pi:native_pi:{row["round_id"]}'}
        self.service.coordinator.source_persisted(
            self.cid, self.lease['lease_id'], self.lease['generation'],
            attempt_id=row['attempt_id'], source=source,
            native_revision='b'*64, terminal_state='completed')
        requests = Requests(self.tmp.name, 'old_boot')
        requests.claim(self.cid, 'request_old', {})
        requests.update(self.cid, 'request_old', 'started', attempt_id=row['attempt_id'])
        reports = []
        controller = RecoveryController(self.service, requests, boot_id='old_boot',
                                        retry_delays=(), report_callback=reports.append)
        got = controller.recover_guarded(self.cid, trigger='send_before')
        self.assertEqual(got['outcome'], 'committed')
        self.assertEqual(requests.get(self.cid, 'request_old')['status'], 'finished')
        again = controller.recover_guarded(self.cid, trigger='switch_before')
        self.assertEqual(again['outcome'], 'not_applicable')
        self.assertEqual(self.service.coordinator.state(self.cid)['committed_offset'], 1)
        self.assertEqual([row['action'] for row in reports], ['settle_terminal', 'none'])

    def test_ready_controller_repairs_committed_receipt_in_same_boot(self):
        from shepherd.requests import Requests
        row = self.begin()
        source = {'kind':'native_round', 'engine':'pi', 'session_id':'native_pi',
                  'entry_id':row['round_id'],
                  'source_key':f'pi:native_pi:{row["round_id"]}'}
        self.service.coordinator.source_persisted(
            self.cid, self.lease['lease_id'], self.lease['generation'],
            attempt_id=row['attempt_id'], source=source,
            native_revision='b'*64, terminal_state='completed')
        controller = RecoveryController(self.service, boot_id='old_boot', retry_delays=())
        self.assertEqual(controller.recover_guarded(self.cid, trigger='turn_finished')['outcome'],
                         'committed')
        requests = Requests(self.tmp.name, 'old_boot')
        requests.claim(self.cid, 'leftover', {})
        requests.update(self.cid, 'leftover', 'started', attempt_id=row['attempt_id'])
        controller.requests = requests
        with mock.patch('shepherd.recovery_controller.CrashRecovery.recover_one') as recover:
            got = controller.recover_guarded(self.cid, trigger='admission_before')
        recover.assert_not_called()
        self.assertEqual(got['outcome'], 'not_applicable')
        self.assertEqual(requests.get(self.cid, 'leftover')['status'], 'finished')
        self.assertEqual(requests.get(self.cid, 'leftover')['reason'], 'completed')
        self.assertTrue(requests.claim(self.cid, 'next', {})[1])

    def test_controller_does_not_settle_fresh_unlinked_request(self):
        from shepherd.requests import Requests
        requests = Requests(self.tmp.name, 'old_boot')
        requests.claim(self.cid, 'request_new', {})
        controller = RecoveryController(self.service, requests, boot_id='old_boot', retry_delays=())
        controller.recover_guarded(self.cid, trigger='admission_before')
        self.assertEqual(requests.get(self.cid, 'request_new')['status'], 'accepted')

    def test_controller_and_offline_scan_skip_tombstoned_conversation(self):
        self.service.coordinator.tombstone_for_delete(self.cid)
        controller = RecoveryController(self.service, boot_id='old_boot', retry_delays=())
        got = controller.recover_guarded(self.cid, trigger='background_retry')
        self.assertEqual(got['outcome'], 'deleted')
        self.assertEqual(CrashRecovery(self.service, boot_id='old_boot').scan(), [])
        self.assertEqual(
            CrashRecovery(self.service, boot_id='old_boot').recover_one(self.cid)['state'],
            'deleted')


class ReconciliationTransition(unittest.TestCase):
    def test_preserve_and_end_atomically_quarantines_old_binding(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        c = Coordinator(Store(tmp.name)); cid = 'reconcile_conversation'; c.create(cid)
        old = c.bind(cid, 'pi', 'native_old', native_cwd=tmp.name,
            connector_version='pi-v1', binding_id='binding_old', ownership_nonce='nonce_old')
        lease = c.acquire(cid, old, ownership_nonce='nonce_old')
        row = c.begin_round(cid, lease['lease_id'], lease['generation'],
            attempt_id='attempt_old', recovery={'phase':'reserved','boot_id':'old',
                                                'boundary':{}})
        c.mark_dispatched(cid, lease['lease_id'], lease['generation'],
            attempt_id=row['attempt_id'], process={'pid':1,'pgid':1,'start_token':'x'})
        c.mark_unresolved(cid, lease['lease_id'], lease['generation'],
                          attempt_id=row['attempt_id'], reason='ambiguous')
        replacement = {
            'binding_id':'binding_new','conversation_id':cid,'owner':'shepherd',
            'ownership_nonce':'nonce_new','engine':'pi','native_session_id':'native_new',
            'native_locator':'native_new','native_cwd':tmp.name,
            'connector_version':'pi-v1','applied_offset':0,'applied_event_hash':'',
            'native_revision':'','base_checkpoint_id':'','status':'current','created_at':1,
        }
        c.ownership.issue(replacement)
        before = c.store.load_raw(cid)
        for status in ('alive', 'unknown'):
            with self.subTest(status=status), \
                 mock.patch('shepherd.recovery.process_state', return_value=status), \
                 self.assertRaisesRegex(Conflict, '尚未确认退出'):
                c.begin_reconciliation(cid, replacement, attempt_id='attempt_old')
            self.assertEqual(c.store.load_raw(cid), before)
        with c.store.change(cid) as state:
            state['active_turn']['recovery'].pop('process')
        missing = c.store.load_raw(cid)
        with self.assertRaisesRegex(Conflict, '身份缺失'):
            c.begin_reconciliation(cid, replacement, attempt_id='attempt_old')
        self.assertEqual(c.store.load_raw(cid), missing)
        with c.store.change(cid) as state:
            state['active_turn']['recovery']['process'] = {'pid':1,'pgid':1,'start_token':'x'}
        with mock.patch('shepherd.recovery.process_state', return_value='dead') as probe:
            pending = c.begin_reconciliation(cid, replacement, attempt_id='attempt_old')
        probe.assert_called_once_with({'pid':1,'pgid':1,'start_token':'x'})
        state = c.state(cid)
        self.assertIsNone(state['inflight']); self.assertIsNone(state['lease'])
        self.assertEqual(state['bindings']['binding_old']['status'], 'quarantined')
        self.assertEqual(pending['target_binding_id'], 'binding_new')
        self.assertEqual(state['reconciliations'][0]['inflight']['attempt_id'], 'attempt_old')


if __name__ == '__main__': unittest.main()
