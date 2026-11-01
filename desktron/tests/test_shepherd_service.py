"""Shepherd Conversation lifecycle is atomic and private."""
import hashlib
from pathlib import Path
import os
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron/lib'))

from shepherd import Conflict, Service
from shepherd.coordinator import digest
from shepherd.codex_connector import CodexFamilyConnector
from shepherd.recovery import CrashRecovery
from shepherd.pi_codec import CODEC_VERSION as PI_CODEC
from shepherd.claude_codec import PROMPT_SNAPSHOT_POLICY, static_system_prompt


class ServiceLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.cwd = self.root / 'work'; self.cwd.mkdir()
        self.service = Service(self.root / 'state')

    def test_claude_and_pi_create_owned_active_binding(self):
        for index, engine in enumerate(('claude', 'pi')):
            cid = f'conversation_{index}'
            result = self.service.create(cid, engine, str(self.cwd))
            active = self.service.active(cid)
            self.assertEqual(active['binding']['engine'], engine)
            self.assertEqual(active['lease']['binding_id'], result['binding_id'])
            self.assertEqual(active['binding']['applied_offset'], 0)
            self.assertTrue(self.service.coordinator.ownership.verify(
                active['binding'], active['binding']['ownership_nonce']))

    def test_product_claude_binding_bootstraps_static_prompt_before_activation(self):
        service = Service(self.root/'prompt-state', claude_system_lookup=lambda _cid:'STATIC_STYLE')
        prompt = static_system_prompt('STATIC_STYLE')
        connector = mock.Mock()
        connector.ensure_prompt_snapshot.return_value = {'native_revision':'a'*64}
        with mock.patch.object(service, 'connector', return_value=connector):
            result = service.create('conversation_prompt', 'claude', str(self.cwd))
        binding = service.coordinator.state('conversation_prompt')['bindings'][result['binding_id']]
        self.assertEqual(binding['prompt_snapshot_policy'], PROMPT_SNAPSHOT_POLICY)
        self.assertEqual(binding['prompt_snapshot_sha256'], hashlib.sha256(prompt.encode()).hexdigest())
        self.assertEqual(binding['native_revision'], 'a'*64)
        connector.ensure_prompt_snapshot.assert_called_once_with(prompt)

    def test_product_claude_bootstrap_failure_rolls_back_conversation(self):
        service = Service(self.root/'failed-prompt-state',
                          claude_system_lookup=lambda _cid:'STATIC_STYLE')
        connector = mock.Mock()
        connector.ensure_prompt_snapshot.side_effect = Conflict('snapshot failed')
        with mock.patch.object(service, 'connector', return_value=connector), \
             self.assertRaisesRegex(Conflict, 'snapshot failed'):
            service.create('conversation_prompt_failed', 'claude', str(self.cwd))
        self.assertIsNone(service.store.load('conversation_prompt_failed'))
        self.assertEqual(list((self.root/'failed-prompt-state/ownership').glob('*.json')), [])

    def test_claude_switch_bootstrap_failure_leaves_no_registered_binding(self):
        service = Service(self.root/'failed-switch-state',
                          claude_system_lookup=lambda _cid:'STATIC_STYLE')
        service.create('conversation_prompt_switch', 'pi', str(self.cwd))
        connector = mock.Mock()
        connector.ensure_prompt_snapshot.side_effect = Conflict('snapshot failed')
        with mock.patch.object(service, 'connector', return_value=connector), \
             self.assertRaisesRegex(Conflict, 'snapshot failed'):
            service.ensure_binding('conversation_prompt_switch', 'claude', str(self.cwd))
        state = service.coordinator.state('conversation_prompt_switch')
        self.assertEqual([row['engine'] for row in state['bindings'].values()], ['pi'])
        receipts = list((self.root/'failed-switch-state/ownership').glob('*.json'))
        self.assertEqual(len(receipts), 1)  # Existing Pi ownership only.

    def test_codex_family_creation_uses_private_factory_result(self):
        made = {'native_session_id':'11111111-2222-4333-8444-555555555555',
                'native_revision':'a'*64,'connector_version':'codex-v1'}
        with mock.patch.object(CodexFamilyConnector, 'create_native', return_value=made) as create:
            result = self.service.create('conversation_codex', 'codex', str(self.cwd))
        create.assert_called_once()
        active = self.service.active('conversation_codex')
        self.assertEqual(active['binding']['native_session_id'], made['native_session_id'])
        self.assertEqual(active['binding']['native_revision'], made['native_revision'])
        self.assertEqual(result['lease']['generation'], 1)

    def test_failure_rolls_back_only_new_private_paths(self):
        keep = self.root / 'state/native/pi/keep.jsonl'
        keep.parent.mkdir(parents=True); keep.write_text('keep')
        with mock.patch.object(self.service.coordinator, 'bind', side_effect=RuntimeError('bind failed')):
            with self.assertRaisesRegex(RuntimeError, 'bind failed'):
                self.service.create('conversation_failed', 'claude', str(self.cwd))
        self.assertIsNone(self.service.store.load('conversation_failed'))
        self.assertTrue(keep.is_file())
        self.assertEqual(list((self.root/'state/ownership').glob('*.json')), [])

    def test_switch_reuses_conversation_and_activates_caught_up_binding(self):
        created = self.service.create('conversation_switch', 'pi', str(self.cwd))
        current = self.service.active('conversation_switch')
        target_binding = {**current['binding'], 'binding_id':'binding_claude',
                          'engine':'claude','native_session_id':'22222222-2222-4333-8444-555555555555',
                          'connector_version':'claude-v1','ownership_nonce':'nonce_claude'}
        fake_target = mock.Mock()
        with mock.patch.object(self.service, 'ensure_binding', return_value='binding_claude'), \
             mock.patch.object(self.service.coordinator, 'state', return_value={
                 **current['state'], 'bindings':{current['binding']['binding_id']:current['binding'],
                                                'binding_claude':target_binding}}), \
             mock.patch.object(self.service, 'connector', return_value=fake_target), \
             mock.patch('shepherd.service.SourceResolver'), \
             mock.patch('shepherd.service.ReadProjector'), \
             mock.patch('shepherd.service.SwitchRunner') as runner:
            runner.return_value.switch.return_value = {'binding_id':'binding_claude'}
            with mock.patch.object(self.service, 'active', side_effect=[current, {
                'state':current['state'],'binding':target_binding,
                'lease':{'binding_id':'binding_claude','generation':2}}]):
                result = self.service.switch('conversation_switch','claude',str(self.cwd))
        self.assertEqual(result['binding']['engine'],'claude')
        args = runner.return_value.switch.call_args
        self.assertEqual(args.args[:2], ('conversation_switch','binding_claude'))
        self.assertEqual(args.kwargs['lease_id'], created['lease']['lease_id'])

    def test_same_engine_switch_is_idempotent(self):
        self.service.create('conversation_same', 'pi', str(self.cwd))
        with mock.patch.object(self.service, 'ensure_binding') as ensure:
            result = self.service.switch('conversation_same','pi',str(self.cwd))
        ensure.assert_not_called(); self.assertEqual(result['binding']['engine'],'pi')

    def test_native_history_promotes_to_unified_without_copying_native_body(self):
        native = Service(self.root/'promotion-state', storage='native')
        created = native.create('conversation_promote', 'pi', str(self.cwd))
        connector = native.connector(native.active('conversation_promote')['binding'])
        public = [
            {'type':'message','role':'user','content':[{'type':'input_text','text':'question'}]},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'answer'}]},
        ]
        row = {'round_id':'conversation_promote:1','offset':1,'entry_hash':'a'*64,
               'committed_at':10,'public':public}
        revision = connector.append([row], '')['native_revision']
        source = {'kind':'native_round','engine':'pi',
                  'session_id':connector.native_session_id,'entry_id':row['round_id'],
                  'source_key':f'pi:{connector.native_session_id}:{row["round_id"]}'}
        resolved = connector.resolve_source(source)
        committed = native.coordinator.commit(
            'conversation_promote', created['lease']['lease_id'], created['lease']['generation'],
            source=source, source_hash=digest(resolved['source']),
            public_hash=digest(resolved['public']))
        native.coordinator.advance('conversation_promote', created['binding_id'], 1,
            committed['entry_hash'], revision,
            ownership_nonce=connector.ownership_nonce)
        native_path = Path(connector.path)
        before = native_path.read_bytes()

        unified = Service(self.root/'promotion-state', storage='unified')
        result = unified.promote_to_unified('conversation_promote')
        active = result['binding']; state = result['state']
        self.assertTrue(active['connector_version'].startswith('unified-v1:'))
        self.assertEqual(active['imported_through_seq'], 1)
        self.assertEqual(state['bindings'][created['binding_id']]['status'], 'retired')
        self.assertIsNone(state['pending_switch'])
        self.assertEqual(native_path.read_bytes(), before)
        backup = self.root/'promotion-state/migrations/native-to-unified-v1/conversation_promote.json'
        self.assertEqual(__import__('json').loads(backup.read_text())['state']['active_binding_id'],
                         created['binding_id'])
        self.assertEqual(unified.promote_to_unified('conversation_promote')['binding'], active)

    def test_promotion_preflight_failure_keeps_native_producer(self):
        native = Service(self.root/'promotion-failed', storage='native')
        native.create('conversation_promote_failed', 'pi', str(self.cwd))
        unified = Service(self.root/'promotion-failed', storage='unified')
        before = unified.active('conversation_promote_failed')
        with mock.patch('shepherd.service.SwitchRunner._preflight',
                        side_effect=Conflict('incompatible')):
            with self.assertRaisesRegex(Conflict, 'incompatible'):
                unified.promote_to_unified('conversation_promote_failed')
        after = unified.active('conversation_promote_failed')
        self.assertEqual(after['lease'], before['lease'])
        self.assertEqual(after['binding'], before['binding'])
        self.assertFalse(any(key.startswith('ubind_') for key in after['state']['bindings']))

    def test_failed_preflight_rolls_back_new_binding_but_keeps_old_lease(self):
        self.service.create('conversation_failed_switch', 'pi', str(self.cwd))
        before = self.service.active('conversation_failed_switch')
        with mock.patch.object(self.service, 'connector'), \
             mock.patch('shepherd.service.SourceResolver'), \
             mock.patch('shepherd.service.SwitchRunner') as runner:
            runner.return_value.switch.side_effect = Conflict('preflight')
            with self.assertRaisesRegex(Conflict, 'preflight'):
                self.service.switch('conversation_failed_switch', 'claude', str(self.cwd))
        after = self.service.active('conversation_failed_switch')
        self.assertEqual(after['lease'], before['lease'])
        self.assertEqual(after['state']['bindings'], before['state']['bindings'])

    def test_fenced_switch_reuses_target_for_recovery(self):
        self.service.create('conversation_pending', 'pi', str(self.cwd))
        current = self.service.active('conversation_pending')
        target = self.service.ensure_binding('conversation_pending', 'claude', str(self.cwd))
        binding = self.service.coordinator.state('conversation_pending')['bindings'][target]
        pending = self.service.coordinator.prepare_switch('conversation_pending', target,
            ownership_nonce=binding['ownership_nonce'], lease_id=current['lease']['lease_id'],
            generation=current['lease']['generation'])
        with mock.patch.object(self.service, 'connector'), \
             mock.patch('shepherd.service.SourceResolver'), \
             mock.patch('shepherd.service.SwitchRunner') as runner:
            self.service.switch('conversation_pending','claude',str(self.cwd))
            runner.return_value.switch.assert_not_called()
            runner.return_value.resume_switch.assert_called_once_with('conversation_pending',
                pending['switch_id'], ownership_nonce=binding['ownership_nonce'])

    def test_explicit_selection_retargets_fenced_switch_to_fresh_binding(self):
        self.service.create('conversation_retarget', 'pi', str(self.cwd))
        current = self.service.active('conversation_retarget')
        target = self.service.ensure_binding('conversation_retarget', 'claude', str(self.cwd))
        target_row = self.service.coordinator.state('conversation_retarget')['bindings'][target]
        pending = self.service.coordinator.prepare_switch(
            'conversation_retarget', target, ownership_nonce=target_row['ownership_nonce'],
            lease_id=current['lease']['lease_id'], generation=current['lease']['generation'])
        with mock.patch.object(self.service, '_create_native', return_value={
                'native_session_id':'33333333-3333-4333-8333-333333333333',
                'native_revision':'','connector_version':PI_CODEC}), \
             mock.patch.object(self.service, '_switch_binding', side_effect=lambda cid,bid,cur,pending: {
                'state':self.service.coordinator.state(cid),
                'binding':self.service.coordinator.state(cid)['bindings'][bid], 'lease':None}):
            got = self.service.switch(
                'conversation_retarget', 'pi', str(self.cwd), selection_epoch=2)
        state = self.service.coordinator.state('conversation_retarget')
        new_target = state['pending_switch']['target_binding_id']
        self.assertNotEqual(new_target, current['binding']['binding_id'])
        self.assertNotEqual(new_target, target)
        self.assertEqual(state['bindings'][target]['status'], 'retargeted')
        self.assertEqual(state['bindings'][new_target]['engine'], 'pi')
        self.assertEqual(got['binding']['binding_id'], new_target)
        self.assertEqual(state['pending_switch']['retargeted_from_switch_id'], pending['switch_id'])

    def test_retarget_to_claude_bootstraps_and_stages_prompt_policy(self):
        service = Service(self.root/'retarget-prompt-state',
                          claude_system_lookup=lambda _cid:'STATIC_STYLE')
        created = service.create('conversation_retarget_prompt','pi',str(self.cwd))
        current = service.active('conversation_retarget_prompt')
        target = service.coordinator.bind('conversation_retarget_prompt','codex','native_codex',
            native_cwd=str(self.cwd),connector_version='codex-v1',
            binding_id='binding_codex',ownership_nonce='nonce_codex')
        pending = service.coordinator.prepare_switch('conversation_retarget_prompt',target,
            ownership_nonce='nonce_codex',lease_id=created['lease']['lease_id'],
            generation=created['lease']['generation'])
        connector = mock.Mock()
        connector.ensure_prompt_snapshot.return_value = {'native_revision':'b'*64}
        connector.binding_id = 'replacement'
        with mock.patch.object(service,'_create_native',return_value={
                'native_session_id':'33333333-3333-4333-8333-333333333333',
                'native_revision':'','connector_version':'claude-2.1.268-v1'}), \
             mock.patch.object(service,'connector',return_value=connector), \
             mock.patch.object(service,'_switch_binding',side_effect=lambda cid,bid,cur,pending: {
                'state':service.coordinator.state(cid),
                'binding':service.coordinator.state(cid)['bindings'][bid], 'lease':None}):
            service.switch('conversation_retarget_prompt','claude',str(self.cwd),selection_epoch=2)
        state = service.coordinator.state('conversation_retarget_prompt')
        replacement = state['bindings'][state['pending_switch']['target_binding_id']]
        self.assertEqual(replacement['prompt_snapshot_policy'], PROMPT_SNAPSHOT_POLICY)
        self.assertEqual(replacement['native_revision'], 'b'*64)
        self.assertEqual(len(replacement['prompt_snapshot_sha256']), 64)
        connector.ensure_prompt_snapshot.assert_called_once()
        self.assertEqual(state['bindings'][target]['status'], 'retargeted')
        self.assertIsNone(current['state'].get('pending_switch'))

    def test_inflight_rejected_before_creating_any_new_binding(self):
        created = self.service.create('conversation_busy', 'pi', str(self.cwd))
        lease = created['lease']
        self.service.coordinator.begin_round('conversation_busy',lease['lease_id'],lease['generation'])
        with mock.patch.object(self.service,'ensure_binding') as ensure:
            with self.assertRaises(Conflict):
                self.service.switch('conversation_busy','codex',str(self.cwd))
        ensure.assert_not_called()

    def test_reconciliation_catchup_resumes_after_second_crash(self):
        created = self.service.create('conversation_reconcile_crash', 'pi', str(self.cwd))
        lease = created['lease']
        row = self.service.coordinator.begin_round(
            'conversation_reconcile_crash', lease['lease_id'], lease['generation'],
            attempt_id='attempt_uncertain',
            recovery={'phase':'reserved','boot_id':'old','boundary':{}})
        self.service.coordinator.mark_dispatched(
            'conversation_reconcile_crash', lease['lease_id'], lease['generation'],
            attempt_id=row['attempt_id'], process={'pid':1,'pgid':1,'start_token':'old'})
        self.service.coordinator.mark_unresolved(
            'conversation_reconcile_crash', lease['lease_id'], lease['generation'],
            attempt_id=row['attempt_id'], reason='native_terminal_unconfirmed')
        append = mock.Mock(side_effect=[RuntimeError('second crash'), {'native_revision':''}])

        def connector(binding):
            return SimpleNamespace(
                binding_id=binding['binding_id'], engine=binding['engine'],
                native_session_id=binding['native_session_id'],
                native_cwd=binding['native_cwd'],
                connector_version=binding['connector_version'],
                ownership_nonce=binding['ownership_nonce'], append=append,
                preflight=lambda rounds, **kwargs: None)

        with mock.patch.object(self.service, 'connector', side_effect=connector), \
             mock.patch('shepherd.recovery.process_state', return_value='dead'):
            with self.assertRaisesRegex(RuntimeError, 'second crash'):
                self.service.reconcile_end(
                    'conversation_reconcile_crash', attempt_id='attempt_uncertain')
            fenced = self.service.coordinator.state('conversation_reconcile_crash')
            self.assertIsNone(fenced['inflight'])
            self.assertIsNone(fenced['lease'])
            self.assertEqual(fenced['bindings'][created['binding_id']]['status'], 'quarantined')
            self.assertTrue(fenced['pending_switch']['reconciliation_id'])
            reports = CrashRecovery(self.service, boot_id='new_boot').scan()
            self.assertEqual(reports[0]['state'], 'pending_switch')
            self.service.resume_reconciliation('conversation_reconcile_crash')
        active = self.service.active('conversation_reconcile_crash')
        self.assertIsNone(active['state']['pending_switch'])
        self.assertNotEqual(active['binding']['binding_id'], created['binding_id'])
        self.assertEqual(active['binding']['applied_offset'], 1)
        self.assertEqual(append.call_count, 2)

    def test_incomplete_turn_rebind_resumes_after_catchup_crash(self):
        created = self.service.create('conversation_incomplete', 'pi', str(self.cwd))
        lease = created['lease']
        source = {'kind':'native_round','engine':'pi',
                  'session_id':self.service.active('conversation_incomplete')['binding']['native_session_id'],
                  'entry_id':'conversation_incomplete:1'}
        source['source_key'] = f'pi:{source["session_id"]}:{source["entry_id"]}'
        turn = self.service.coordinator.begin_round(
            'conversation_incomplete', lease['lease_id'], lease['generation'],
            attempt_id='attempt_unknown', recovery={'phase':'reserved','boot_id':'old','boundary':{}})
        self.service.coordinator.mark_dispatched(
            'conversation_incomplete', lease['lease_id'], lease['generation'],
            attempt_id=turn['attempt_id'])
        self.service.coordinator.settle_recovered(
            'conversation_incomplete', lease['lease_id'], lease['generation'],
            attempt_id=turn['attempt_id'], source=source,
            source_hash='a'*64, public_hash='b'*64,
            unfinished_tool_ids=['call_1'])
        appended = mock.Mock(side_effect=[RuntimeError('catchup crash'), {'native_revision':'rev'}])
        resolved = {'source':{'native':'x'}, 'public':[
            {'type':'message','role':'user','content':[{'type':'input_text','text':'q'}]},
            {'type':'function_call','call_id':'call_1','name':'read','arguments':'{}'},
            {'type':'function_call_output','call_id':'call_1','output':'unknown'}]}
        # Match the hashes that settle_recovered verifies through the projector.
        from shepherd.coordinator import digest
        raw = self.service.store.load_raw('conversation_incomplete')
        raw['turns'][0]['source_hash'] = digest(resolved['source'])
        raw['turns'][0]['public_hash'] = digest(resolved['public'])
        from shepherd.turn_state import turn_hash
        raw['turns'][0]['turn_hash'] = turn_hash(raw['turns'][0])
        raw['bindings'][created['binding_id']]['imported_turn_hash'] = raw['turns'][0]['turn_hash']
        from durable_json import read, write
        snap = read(self.service.store.path('conversation_incomplete')); snap.clear(); snap.update(raw)
        write(self.service.store.path('conversation_incomplete'), snap)
        def connector(binding):
            return SimpleNamespace(
                binding_id=binding['binding_id'], engine=binding['engine'],
                native_session_id=binding['native_session_id'], native_cwd=binding['native_cwd'],
                connector_version=binding['connector_version'], ownership_nonce=binding['ownership_nonce'],
                append=appended, preflight=lambda rounds, **kwargs: None,
                resolve_source=lambda _source: resolved)
        with mock.patch.object(self.service, 'connector', side_effect=connector):
            with self.assertRaisesRegex(RuntimeError, 'catchup crash'):
                self.service.recover_incomplete('conversation_incomplete', created['binding_id'])
            fenced = self.service.coordinator.state('conversation_incomplete')
            self.assertIsNone(fenced['lease'])
            self.assertEqual(fenced['bindings'][created['binding_id']]['status'], 'quarantined')
            self.assertTrue(fenced['pending_switch']['recovery_turn_id'])
            recovered = self.service.recover_incomplete('conversation_incomplete', created['binding_id'])
        self.assertNotEqual(recovered['binding']['binding_id'], created['binding_id'])
        self.assertEqual(recovered['binding']['applied_offset'], 1)
        self.assertIsNone(recovered['state']['pending_switch'])
        self.assertEqual(appended.call_count, 2)

    def test_existing_conversation_is_never_reinitialized(self):
        self.service.create('conversation_existing', 'pi', str(self.cwd))
        before = self.service.coordinator.state('conversation_existing')
        with self.assertRaises(Conflict):
            self.service.create('conversation_existing', 'claude', str(self.cwd))
        self.assertEqual(self.service.coordinator.state('conversation_existing'), before)

    def test_lost_create_race_does_not_rollback_winners_conversation(self):
        real_create = self.service.coordinator.create
        def competing_create(cid):
            real_create(cid)
            raise Conflict('concurrent creator won')
        with mock.patch.object(self.service.coordinator, 'create', side_effect=competing_create):
            with self.assertRaisesRegex(Conflict, 'concurrent creator'):
                self.service.create('conversation_race', 'pi', str(self.cwd))
        self.assertIsNotNone(self.service.coordinator.state('conversation_race'))


if __name__ == '__main__':
    unittest.main()
