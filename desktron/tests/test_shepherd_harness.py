"""Execution Harness commits only durably re-read native rounds."""
import copy
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron/lib'))

from shepherd import (Conflict, Coordinator, ExecutionHarness, InvalidState,
                      PromptNotStarted, Store, TurnInputs)
from shepherd.coordinator import digest, estimate_tokens


class Connector:
    engine = 'pi'; binding_id = 'binding_pi'; native_session_id = 'native_pi'
    connector_version = 'pi-v1'; ownership_nonce = 'nonce_pi'

    def __init__(self, values=None, fail=False):
        self.values = values or {'source': {'native': 'durable'}, 'public': [
            {'type':'message','role':'user','content':[{'type':'input_text','text':'q'}]},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'a'}]},
        ]}
        self.fail = fail; self.seen = []

    def recovery_boundary(self):
        return {'start_seq': 0, 'native_revision': ''}

    def resolve_source(self, source):
        self.seen.append(copy.deepcopy(source))
        if self.fail: raise RuntimeError('native read failed')
        return copy.deepcopy(self.values)


class Runner:
    def __init__(self, source=None, fail=False):
        self.source = source; self.fail = fail; self.calls = []

    def run(self, **kwargs):
        self.calls.append(copy.deepcopy({key:value for key,value in kwargs.items()
                                        if key not in ('on_dispatched','on_native_locator')}))
        if kwargs.get('on_dispatched'):
            kwargs['on_dispatched']()
        if self.fail: raise RuntimeError('engine failed')
        source = self.source or {
            'kind':'native_round','engine':'pi','session_id':'native_pi',
            'entry_id':kwargs['round_id'],
            'source_key':f'pi:native_pi:{kwargs["round_id"]}'}
        return {'source': source, 'terminal_state': 'completed'}


class HarnessContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name); self.coordinator = Coordinator(self.store)
        self.cid = 'conversation_harness'; self.coordinator.create(self.cid)
        self.binding = self.coordinator.bind(self.cid, 'pi', 'native_pi',
            native_cwd=self.tmp.name, connector_version='pi-v1',
            binding_id='binding_pi', ownership_nonce='nonce_pi')
        self.lease = self.coordinator.acquire(self.cid, self.binding,
                                               ownership_nonce='nonce_pi')

    def harness(self, connector=None, runner=None):
        return ExecutionHarness(self.coordinator, connector or Connector(), runner or Runner())

    def test_native_readback_is_hash_source_and_round_id_is_coordinator_owned(self):
        connector, runner = Connector(), Runner()
        row = self.harness(connector, runner).run(
            self.cid, lease_id=self.lease['lease_id'], generation=self.lease['generation'],
            prompt={'text':'question'}, attempt_id='attempt_1')
        self.assertEqual(row['offset'], 1)
        self.assertEqual(row['round_id'], self.cid + ':1')
        self.assertEqual(row['source_hash'], digest(connector.values['source']))
        self.assertEqual(row['public_hash'], digest(connector.values['public']))
        self.assertEqual(row['estimated_tokens'], estimate_tokens(connector.values['public']))
        self.assertEqual(runner.calls[0]['round_id'], self.cid + ':1')
        self.assertEqual(connector.seen, [row['source']])

    def test_dispatch_timing_observer_runs_after_durable_fence_and_cannot_break_delivery(self):
        runner = Runner()
        observed = []
        runner.on_timing_dispatched = lambda: observed.append(
            self.coordinator.state(self.cid)['inflight']['state'])
        row = self.harness(runner=runner).run(
            self.cid, lease_id=self.lease['lease_id'], generation=self.lease['generation'],
            prompt='question', attempt_id='attempt_timing')
        self.assertEqual(row['offset'], 1)
        self.assertEqual(observed, ['running'])

        second = Runner()
        second.on_timing_dispatched = lambda: (_ for _ in ()).throw(RuntimeError('diagnostic'))
        row = self.harness(runner=second).run(
            self.cid, lease_id=self.lease['lease_id'], generation=self.lease['generation'],
            prompt='question', attempt_id='attempt_timing_failure')
        self.assertEqual(row['offset'], 2)

    def test_user_input_is_persisted_by_reference_before_dispatch(self):
        user = [{'type':'message','role':'user',
                 'content':[{'type':'input_text','text':'question'}]}]
        row = self.harness().run(
            self.cid, lease_id=self.lease['lease_id'], generation=self.lease['generation'],
            prompt='question', input_public=user, attempt_id='attempt_input')
        raw = self.store.load_raw(self.cid)
        reference = raw['turns'][0]['input_ref']
        self.assertEqual(TurnInputs(self.tmp.name).read(reference), user)
        self.assertNotIn('question', Path(self.store.path(self.cid)).read_text())
        self.assertEqual(row['offset'], 1)

    def test_aggregate_input_quota_rejects_before_reservation_or_dispatch(self):
        runner = Runner()
        user = [{'type':'message','role':'user',
                 'content':[{'type':'input_text','text':'over budget'}]}]
        with mock.patch('shepherd.turn_inputs.MAX_INPUT_PUBLIC_BYTES', 1):
            with self.assertRaisesRegex(PromptNotStarted, '输入.*超过'):
                self.harness(runner=runner).run(
                    self.cid, lease_id=self.lease['lease_id'], generation=self.lease['generation'],
                    prompt='question', input_public=user, attempt_id='too_large')
        state = self.coordinator.state(self.cid)
        self.assertIsNone(state['inflight'])
        self.assertEqual(state['committed_offset'], 0)
        self.assertEqual(runner.calls, [])
        self.assertFalse((Path(self.tmp.name) / 'inputs').exists())

    def test_large_image_input_recovers_staged_completion_without_replay(self):
        user = [{'type':'message','role':'user','content':[
            {'type':'input_text','text':'two images'},
            {'type':'input_image','image_url':'data:image/png;base64,' + 'A'*(9*1024*1024)},
            {'type':'input_image','image_url':'data:image/png;base64,' + 'B'*(9*1024*1024)}]}]
        connector = Connector(values={'source':{'native':'durable'}, 'public':user + [
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'done'}]}]})
        runner = Runner(); harness = self.harness(connector, runner)
        # Reproduce the old reader rejecting an input its writer had accepted.
        with mock.patch.object(TurnInputs, 'read', side_effect=InvalidState('old input read limit')):
            with self.assertRaisesRegex(InvalidState, 'old input read limit'):
                harness.run(self.cid, lease_id=self.lease['lease_id'],
                    generation=self.lease['generation'], prompt='look',
                    input_public=user, attempt_id='large_images')
        staged = self.coordinator.state(self.cid)['inflight']
        self.assertEqual(staged['state'], 'source_persisted')
        self.assertEqual(staged['terminal_state'], 'completed')
        ref = staged['input_ref']; path = Path(TurnInputs(self.tmp.name).reference_path(ref))
        original = path.read_bytes()
        with mock.patch.object(runner, 'run', side_effect=AssertionError('must not replay')):
            row = harness.recover(self.cid, lease_id=self.lease['lease_id'],
                                  generation=self.lease['generation'])
        self.assertEqual(row['terminal_state'], 'completed')
        self.assertEqual(row['input_ref'], ref)
        self.assertEqual(row['source'], staged['source'])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(TurnInputs(self.tmp.name).read(ref), user)
        self.assertEqual(len(runner.calls), 1)
        self.assertIsNone(self.coordinator.state(self.cid)['inflight'])
        next_row = harness.run(self.cid, lease_id=self.lease['lease_id'],
            generation=self.lease['generation'], prompt='next', attempt_id='next')
        self.assertEqual(next_row['offset'], 2)
        self.assertEqual(len(runner.calls), 2)

    def test_engine_failure_leaves_durable_inflight_and_cannot_reexecute(self):
        runner = Runner(fail=True)
        with self.assertRaisesRegex(RuntimeError, 'engine failed'):
            self.harness(runner=runner).run(
                self.cid, lease_id=self.lease['lease_id'], generation=self.lease['generation'],
                prompt='q', attempt_id='attempt_failed')
        state = self.coordinator.state(self.cid)
        self.assertEqual(state['committed_offset'], 0)
        self.assertEqual(state['inflight']['state'], 'needs_reconciliation')
        with self.assertRaises(Conflict):
            self.harness(runner=runner).run(
                self.cid, lease_id=self.lease['lease_id'], generation=self.lease['generation'],
                prompt='q', attempt_id='attempt_retry')
        self.assertEqual(len(runner.calls), 1)

    def test_cancel_before_dispatch_releases_reservation_without_execution(self):
        runner = Runner(); runner.cancelled = threading.Event(); runner.cancelled.set()
        user = [{'type':'message','role':'user',
                 'content':[{'type':'input_text','text':'q'}]}]
        with self.assertRaisesRegex(PromptNotStarted, '派发前已取消') as raised:
            self.harness(runner=runner).run(self.cid, lease_id=self.lease['lease_id'],
                generation=self.lease['generation'], prompt='q', input_public=user,
                attempt_id='cancelled')
        self.assertEqual(raised.exception.code, 'prompt_not_started')
        state = self.coordinator.state(self.cid)
        self.assertIsNone(state['inflight'])
        self.assertEqual(state['committed_offset'], 0)
        self.assertEqual(state['lease'], self.lease)
        inputs = TurnInputs(self.tmp.name)
        ref = {'conversation_id':self.cid,'turn_id':self.cid+':1',
               'input_hash':inputs._hash(user)}
        self.assertFalse(Path(inputs.reference_path(ref)).exists())

    def test_failed_cancel_keeps_input_evidence_for_the_still_active_turn(self):
        user = [{'type':'message','role':'user','content':[{'type':'input_text','text':'keep'}]}]
        class BeforeDispatch(Runner):
            def run(self, **_kwargs): raise RuntimeError('bridge failed')
        original = self.coordinator.cancel_reserved
        with mock.patch.object(self.coordinator, 'cancel_reserved',
                               side_effect=Conflict('state changed')):
            with self.assertRaisesRegex(Conflict, 'state changed'):
                self.harness(runner=BeforeDispatch()).run(
                    self.cid, lease_id=self.lease['lease_id'], generation=self.lease['generation'],
                    prompt='keep', input_public=user, attempt_id='keep')
        ref = self.coordinator.state(self.cid)['inflight']['input_ref']
        self.assertEqual(TurnInputs(self.tmp.name).read(ref), user)
        original(self.cid, attempt_id='keep')

    def test_predispatch_failure_does_not_block_reused_turn_id(self):
        user1 = [{'type':'message','role':'user','content':[{'type':'input_text','text':'first'}]}]
        user2 = [{'type':'message','role':'user','content':[{'type':'input_text','text':'second'}]}]
        class BeforeDispatch(Runner):
            def run(self, **_kwargs):
                raise RuntimeError('bridge failed before prepared')
        with self.assertRaises(PromptNotStarted):
            self.harness(runner=BeforeDispatch()).run(
                self.cid, lease_id=self.lease['lease_id'], generation=self.lease['generation'],
                prompt='first', input_public=user1, attempt_id='first')
        row = self.harness().run(self.cid, lease_id=self.lease['lease_id'],
            generation=self.lease['generation'], prompt='second', input_public=user2,
            attempt_id='second')
        self.assertEqual(row['turn_id'], self.cid + ':1')
        self.assertEqual(TurnInputs(self.tmp.name).read(
            self.coordinator.state(self.cid)['rounds'][0]['input_ref']), user2)

    def test_recovery_preserves_terminal_and_native_revision(self):
        for terminal in ('failed_recoverable', 'interrupted_confirmed'):
            with self.subTest(terminal=terminal):
                # Independent durable conversation for each failure mode.
                cid = 'recover_' + terminal
                self.coordinator.create(cid)
                self.coordinator.bind(cid, 'pi', 'native_pi', binding_id=cid,
                    native_cwd=self.tmp.name, connector_version='pi-v1', ownership_nonce='nonce_pi')
                lease = self.coordinator.acquire(cid, cid, ownership_nonce='nonce_pi')
                connector = Connector(fail=True); connector.binding_id = cid
                class TerminalRunner(Runner):
                    def run(self, **kwargs):
                        return {**super().run(**kwargs), 'terminal_state':terminal,
                                'native_revision':'a' * 64}
                runner = TerminalRunner()
                harness = ExecutionHarness(self.coordinator, connector, runner)
                with self.assertRaisesRegex(RuntimeError, 'native read failed'):
                    harness.run(cid, lease_id=lease['lease_id'], generation=lease['generation'], prompt='q')
                connector.fail = False
                # Simulate process restart by re-opening the coordinator/store.
                recovered = ExecutionHarness(Coordinator(Store(self.tmp.name)), connector, runner)
                row = recovered.recover(cid, lease_id=lease['lease_id'], generation=lease['generation'])
                self.assertEqual(row['terminal_state'], terminal)
                self.assertEqual(len(runner.calls), 1)
                self.assertEqual(self.coordinator.state(cid)['bindings'][cid]['native_revision'], 'a' * 64)

    def test_receipt_recovers_crash_before_coordinator_update_without_rerun(self):
        self.coordinator.begin_round(self.cid, self.lease['lease_id'],
            self.lease['generation'], attempt_id='crashed')
        runner = Runner()
        result = runner.run(round_id=self.cid + ':1')
        runner.recover_result = lambda _round_id: result
        row = self.harness(runner=runner).recover(self.cid,
            lease_id=self.lease['lease_id'], generation=self.lease['generation'])
        self.assertEqual(row['offset'], 1)
        self.assertEqual(len(runner.calls), 1)

    def test_uncertain_delivery_stays_fenced_not_running_or_retried(self):
        self.coordinator.begin_round(self.cid, self.lease['lease_id'],
            self.lease['generation'], attempt_id='uncertain')
        runner = Runner()
        with self.assertRaisesRegex(Conflict, '禁止自动重跑'):
            self.harness(runner=runner).recover(self.cid,
                lease_id=self.lease['lease_id'], generation=self.lease['generation'])
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.coordinator.state(self.cid)['inflight']['state'], 'needs_reconciliation')

    def test_native_read_failure_recovers_without_running_engine_again(self):
        connector, runner = Connector(fail=True), Runner()
        harness = self.harness(connector=connector, runner=runner)
        with self.assertRaisesRegex(RuntimeError, 'native read failed'):
            harness.run(self.cid, lease_id=self.lease['lease_id'],
                        generation=self.lease['generation'], prompt='q',
                        attempt_id='attempt_persisted')
        state = self.coordinator.state(self.cid)
        self.assertEqual(state['committed_offset'], 0)
        self.assertEqual(state['inflight']['state'], 'source_persisted')
        connector.fail = False
        row = harness.recover(self.cid, lease_id=self.lease['lease_id'],
                              generation=self.lease['generation'])
        self.assertEqual(row['offset'], 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertIsNone(self.coordinator.state(self.cid)['inflight'])

    def test_resident_host_is_confirmed_only_after_manifest_commit(self):
        class ResidentRunner(Runner):
            def __init__(self):
                super().__init__(); self.confirmed=0; self.discarded=0
            def confirm_resident(self): self.confirmed+=1
            def discard_resident(self): self.discarded+=1
        runner=ResidentRunner()
        self.harness(runner=runner).run(
            self.cid,lease_id=self.lease['lease_id'],generation=self.lease['generation'],prompt='q')
        self.assertEqual((runner.confirmed,runner.discarded),(1,0))

    def test_resident_host_is_discarded_when_manifest_commit_cannot_finish(self):
        class ResidentRunner(Runner):
            def __init__(self):
                super().__init__(); self.discarded=0
            def discard_resident(self): self.discarded+=1
        connector=Connector(fail=True);runner=ResidentRunner()
        with self.assertRaisesRegex(RuntimeError,'native read failed'):
            self.harness(connector=connector,runner=runner).run(
                self.cid,lease_id=self.lease['lease_id'],generation=self.lease['generation'],prompt='q')
        self.assertEqual(runner.discarded,1)

    def test_compatibility_is_audited_after_normal_commit(self):
        connector = Connector()
        connector.source_compatibility = lambda source: [{
            'engine':'claude','action':'ignored_internal_entry',
            'kind':'system/compact_boundary','count':1}]
        row = self.harness(connector=connector).run(
            self.cid, lease_id=self.lease['lease_id'], generation=self.lease['generation'],
            prompt='q', attempt_id='compatibility')
        self.assertEqual(row['compatibility'][0]['kind'], 'system/compact_boundary')
        audit = self.coordinator.state(self.cid)['audit_events'][-1]
        self.assertEqual(audit['kind'], 'source_compatibility')
        self.assertEqual(audit['turn_id'], row['turn_id'])
        self.assertNotIn('source', audit)
        self.assertNotIn('public', audit)

    def test_runner_must_report_verified_terminal_state(self):
        class MissingTerminal(Runner):
            def run(self, **kwargs):
                result = super().run(**kwargs)
                result.pop('terminal_state')
                return result
        with self.assertRaises(InvalidState):
            self.harness(runner=MissingTerminal()).run(
                self.cid, lease_id=self.lease['lease_id'], generation=self.lease['generation'],
                prompt='q', attempt_id='attempt_no_terminal')
        self.assertEqual(self.coordinator.state(self.cid)['inflight']['state'], 'needs_reconciliation')

    def test_wrong_lease_connector_or_source_is_rejected(self):
        with self.assertRaises(Conflict):
            self.harness().run(self.cid, lease_id='old', generation=self.lease['generation'], prompt='q')
        connector = Connector(); connector.binding_id = 'other'
        with self.assertRaises(Conflict):
            self.harness(connector=connector).run(
                self.cid, lease_id=self.lease['lease_id'], generation=self.lease['generation'], prompt='q')
        wrong = {'kind':'native_round','engine':'pi','session_id':'native_pi',
                 'entry_id':'native_turn_99',
                 'source_key':'pi:someone_else:native_turn_99'}
        with self.assertRaises(Conflict):
            self.harness(runner=Runner(wrong)).run(
                self.cid, lease_id=self.lease['lease_id'], generation=self.lease['generation'], prompt='q')
        self.assertEqual(self.coordinator.state(self.cid)['committed_offset'], 0)

    def test_execution_inflight_blocks_switch_and_revoke(self):
        self.coordinator.begin_round(self.cid, self.lease['lease_id'],
                                     self.lease['generation'], attempt_id='attempt_running')
        target = self.coordinator.bind(self.cid, 'claude', 'native_claude',
            native_cwd=self.tmp.name, connector_version='claude-v1',
            binding_id='binding_claude', ownership_nonce='nonce_claude')
        with self.assertRaises(Conflict):
            self.coordinator.prepare_switch(self.cid, target, ownership_nonce='nonce_claude',
                lease_id=self.lease['lease_id'], generation=self.lease['generation'])
        with self.assertRaises(Conflict):
            self.coordinator.revoke(self.cid, self.lease['lease_id'], self.lease['generation'])

    def test_stale_generation_cannot_execute_second_round(self):
        self.coordinator.revoke(self.cid, self.lease['lease_id'], self.lease['generation'])
        with self.assertRaises(Conflict):
            self.harness().run(self.cid, lease_id=self.lease['lease_id'],
                               generation=self.lease['generation'], prompt='q')


if __name__ == '__main__':
    unittest.main()
