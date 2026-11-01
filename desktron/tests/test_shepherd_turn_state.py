"""Serial-turn Shepherd v2 state and v1 migration."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron' / 'lib'))

from shepherd.coordinator import digest
from shepherd.claude_codec import encode_rounds
from shepherd.codex_codec import encode_round as encode_codex_round
from shepherd.pi_codec import encode_round as encode_pi_round
from shepherd.source_codec import UNKNOWN_TOOL_OUTPUT, seal_incomplete_tools
from shepherd.store import InvalidState, Store
from shepherd.turn_state import initial, migrate_v1, settle, turn_hash, validate


class TurnState(unittest.TestCase):
    def active(self):
        return {
            'turn_id':'conversation:1', 'seq':1, 'kind':'user_turn',
            'producer_binding_id':'binding', 'producer_generation':3,
            'phase':'dispatched', 'outcome':'unknown', 'history_state':'pending',
            'attempt_id':'attempt_1', 'unfinished_tool_ids':[],
            'recovery':{'phase':'dispatched'},
        }

    def source(self):
        return {'kind':'native_round','engine':'claude','session_id':'native',
                'entry_id':'conversation:1','source_key':'claude:native:conversation:1'}

    def test_unknown_turn_is_valid_history_and_records_unfinished_tools(self):
        row = settle(self.active(), source_ref=self.source(),
                     source_hash='a'*64, public_hash='b'*64,
                     outcome='unknown', unfinished_tool_ids=['call_2','call_1','call_2'])
        self.assertEqual(row['phase'], 'settled')
        self.assertEqual(row['history_state'], 'available')
        self.assertEqual(row['unfinished_tool_ids'], ['call_2','call_1'])
        state = initial('conversation')
        state['producer_generation'] = 3
        state['bindings']['binding'] = {
            'binding_id':'binding','conversation_id':'conversation','owner':'shepherd',
            'engine':'claude','native_session_id':'native','connector_version':'v1',
            'ownership_nonce':'nonce','native_cwd':'/tmp','native_revision':'',
            'base_checkpoint_id':'','status':'current',
            'imported_through_seq':1,'imported_turn_hash':row['turn_hash']}
        state['active_binding_id'] = 'binding'; state['turns'] = [row]
        validate(state, 'conversation')

    def test_input_reference_is_part_of_turn_identity(self):
        active = self.active()
        active['input_ref'] = {'conversation_id':'conversation','turn_id':'conversation:1',
                               'input_hash':'c'*64}
        row = settle(active, source_ref=self.source(),
                     source_hash='a'*64, public_hash='b'*64, outcome='unknown')
        changed = copy.deepcopy(row)
        changed['input_ref']['input_hash'] = 'd'*64
        changed['turn_hash'] = turn_hash(changed)
        self.assertNotEqual(row['turn_hash'], changed['turn_hash'])

    def test_omitted_turn_keeps_sequence_without_claiming_source(self):
        row = settle(self.active(), outcome='unknown', history_state='omitted',
                     recovery_reason='native source undecodable')
        self.assertNotIn('source_ref', row)
        self.assertEqual(row['history_state'], 'omitted')
        self.assertEqual(row['outcome'], 'unknown')

    def test_turn_hash_is_independent_not_parent_chained(self):
        first = settle(self.active(), source_ref=self.source(),
                       source_hash='a'*64, public_hash='b'*64)
        changed = copy.deepcopy(first); changed['public_hash'] = 'c'*64
        changed['turn_hash'] = turn_hash(changed)
        self.assertNotEqual(first['turn_hash'], changed['turn_hash'])
        self.assertNotIn('parent_hash', first)

    def test_tampered_turn_is_rejected(self):
        row = settle(self.active(), source_ref=self.source(),
                     source_hash='a'*64, public_hash='b'*64)
        state = initial('conversation'); state['turns'] = [row]
        row['outcome'] = 'completed'
        with self.assertRaisesRegex(InvalidState, '指纹'):
            validate(state, 'conversation')


class IncompleteProjection(unittest.TestCase):
    def public(self):
        return [
            {'type':'message','role':'user','content':[{'type':'input_text','text':'inspect'}]},
            {'type':'function_call','call_id':'done','name':'read','arguments':'{}'},
            {'type':'function_call_output','call_id':'done','output':'ok'},
            {'type':'custom_tool_call','call_id':'open','name':'subagent','input':'{}'},
        ]

    def row(self):
        return {'round_id':'conversation:1','offset':1,'entry_hash':'a'*64,
                'public':seal_incomplete_tools(self.public())['public'], 'committed_at':1}

    def test_only_unfinished_calls_receive_unknown_output(self):
        sealed = seal_incomplete_tools(self.public())
        self.assertEqual(sealed['unfinished_tool_ids'], ['open'])
        outputs = [row for row in sealed['public'] if row['type'].endswith('_output')]
        self.assertEqual([row['call_id'] for row in outputs], ['done','open'])
        self.assertEqual(outputs[-1]['output'], UNKNOWN_TOOL_OUTPUT)

    def test_sealed_projection_is_accepted_by_all_history_codecs(self):
        row = self.row()
        binding = {'binding_id':'binding','native_session_id':'native','native_cwd':'/tmp'}
        self.assertTrue(encode_rounds(binding, [row]))
        self.assertTrue(encode_pi_round(row))
        self.assertTrue(encode_codex_round(row))

    def test_duplicate_or_mismatched_tool_results_fail_closed(self):
        public = self.public() + [
            {'type':'custom_tool_call_output','call_id':'done','output':'wrong'}]
        with self.assertRaises(InvalidState):
            seal_incomplete_tools(public)


class V1Migration(unittest.TestCase):
    def old(self):
        source = {'kind':'native_round','engine':'claude','session_id':'native',
                  'entry_id':'conversation:1','source_key':'claude:native:conversation:1'}
        body = {
            'offset':1,'round_id':'conversation:1','parent_hash':'','kind':'user_turn',
            'attempt_id':'attempt_1','producer_generation':1,
            'producer_binding_id':'binding','source':source,'source_key':source['source_key'],
            'source_hash':'a'*64,'public_hash':'b'*64,'estimated_tokens':8,
            'terminal_state':'failed_recoverable','committed_at':10,
        }
        body['entry_hash'] = digest(body)
        return {
            'version':1,'conversation_id':'conversation','mode':'active',
            'committed_offset':1,'manifest_head_hash':body['entry_hash'],
            'producer_generation':1,'active_binding_id':'binding','pending_switch':None,
            'switch_history':[],'inflight':None,
            'bindings':{'binding':{
                'binding_id':'binding','conversation_id':'conversation','owner':'shepherd',
                'engine':'claude','native_session_id':'native','native_cwd':'/tmp',
                'ownership_nonce':'nonce','connector_version':'v1','applied_offset':1,
                'applied_event_hash':body['entry_hash'],'native_revision':'rev',
                'base_checkpoint_id':'','status':'current','created_at':1}},
            'rounds':[body],'checkpoints':{},'source_index':{source['source_key']:1},
            'reconciliations':[],'shadow_cursor':0,'updated_at':11,
        }

    def test_committed_round_becomes_settled_available_turn(self):
        old = self.old(); frozen = json.dumps(old, sort_keys=True)
        new = migrate_v1(old)
        self.assertEqual(json.dumps(old, sort_keys=True), frozen)
        self.assertEqual(new['version'], 2)
        self.assertNotIn('committed_offset', new)
        self.assertNotIn('manifest_head_hash', new)
        self.assertEqual(new['turns'][0]['outcome'], 'failed')
        self.assertEqual(new['turns'][0]['history_state'], 'available')
        binding = new['bindings']['binding']
        self.assertEqual(binding['imported_through_seq'], 1)
        self.assertEqual(binding['imported_turn_hash'], new['turns'][0]['turn_hash'])
        self.assertNotIn('applied_offset', binding)

    def test_unresolved_inflight_stays_pending_without_inference(self):
        old = self.old()
        old['inflight'] = {
            'attempt_id':'attempt_2','offset':2,'round_id':'conversation:2',
            'kind':'user_turn','binding_id':'binding','generation':1,
            'state':'needs_reconciliation','started_at':12,
            'recovery':{'phase':'dispatched','boot_id':'old','boundary':{},
                        'process':{'pid':1,'pgid':1,'start_token':'x'}},
            'reconciliation_reason':'native_terminal_unconfirmed',
        }
        new = migrate_v1(old)
        self.assertEqual(len(new['turns']), 1)
        self.assertEqual(new['active_turn_id'], 'conversation:2')
        self.assertEqual(new['active_turn']['phase'], 'dispatched')
        self.assertEqual(new['active_turn']['history_state'], 'pending')
        self.assertEqual(new['active_turn']['outcome'], 'unknown')

    def test_tampered_v1_manifest_is_rejected_before_migration(self):
        old = self.old()
        old['rounds'][0]['public_hash'] = '0' * 64
        with self.assertRaisesRegex(InvalidState, 'v1 Manifest 内容'):
            migrate_v1(old)

    def test_preserve_and_end_becomes_deferred_gap_recovery(self):
        old = self.old()
        # A later committed turn exists because v1 resumed from the prior offset.
        first = old['rounds'][0]
        second = {**copy.deepcopy(first), 'offset':2, 'round_id':'conversation:2',
                  'parent_hash':first['entry_hash'], 'attempt_id':'attempt_after',
                  'source':{**first['source'], 'entry_id':'conversation:2',
                            'source_key':'claude:native:conversation:2'},
                  'source_key':'claude:native:conversation:2', 'committed_at':30}
        second['entry_hash'] = digest({k:v for k,v in second.items() if k != 'entry_hash'})
        old['rounds'].append(second); old['committed_offset'] = 2
        old['manifest_head_hash'] = second['entry_hash']; old['source_index'][second['source_key']] = 2
        old['bindings']['binding']['applied_offset'] = 2
        old['bindings']['binding']['applied_event_hash'] = second['entry_hash']
        old['reconciliations'] = [{
            'reconciliation_id':'reconcile_gap','action':'preserve_and_end',
            'binding_id':'binding','replacement_binding_id':'binding',
            'created_at':25,'inflight':{
                'attempt_id':'attempt_gap','offset':2,'round_id':'conversation:2',
                'kind':'user_turn','binding_id':'binding','generation':1,
                'state':'needs_reconciliation','started_at':20,
                'recovery':{'phase':'dispatched','boot_id':'old','boundary':{}}}}]
        migrated = migrate_v1(old)
        self.assertEqual(migrated['active_binding_id'], '')
        self.assertEqual(migrated['producer_generation'], 2)
        self.assertEqual(migrated['bindings']['binding']['status'], 'recovery_required')
        self.assertEqual(migrated['legacy_recoveries'][0]['insert_seq'], 2)
        self.assertEqual(migrated['legacy_recoveries'][0]['turn_id'],
                         'conversation:recovered:attempt_gap')

    def test_pending_switch_hashes_are_remapped_to_turn_hashes(self):
        old = self.old()
        old['active_binding_id'] = ''
        target = {**copy.deepcopy(old['bindings']['binding']),
                  'binding_id':'target','native_session_id':'native_target',
                  'engine':'pi','connector_version':'pi-v1',
                  'ownership_nonce':'target_nonce','applied_offset':1}
        old['bindings']['target'] = target
        old['pending_switch'] = {
            'switch_id':'switch_1','source_binding_id':'binding','target_binding_id':'target',
            'through_offset':1,'generation':1,'state':'catching_up',
            'plan':{'strategy':'incremental','from_offset':2,'through_offset':1,
                    'target_start_offset':1,'target_start_hash':old['rounds'][0]['entry_hash'],
                    'target_start_revision':'rev'},
            'operation':{'operation_id':'operation_1','kind':'switch_write',
                         'phase':'append_applied','updated_at':1},'created_at':1}
        migrated = migrate_v1(old)
        plan = migrated['pending_switch']['plan']
        self.assertEqual(plan['target_start_turn_hash'], migrated['turns'][0]['turn_hash'])
        self.assertEqual(migrated['bindings']['target']['imported_turn_hash'],
                         migrated['turns'][0]['turn_hash'])

    def test_store_migration_is_atomic_backed_up_and_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root); path = Path(store.path('conversation'))
            path.parent.mkdir(parents=True)
            body = json.dumps(self.old(), ensure_ascii=False, sort_keys=False, indent=1) + '\n'
            path.write_bytes(body.encode('utf-8'))
            migrated = store.load_raw('conversation')
            self.assertEqual(migrated['version'], 2)
            self.assertEqual(Path(str(path) + '.v1.backup').read_bytes(), body.encode())
            disk = json.loads(path.read_text())
            self.assertNotIn('committed_offset', disk)
            self.assertNotIn('rounds', disk)
            first = path.read_bytes()
            self.assertEqual(store.load_raw('conversation'), migrated)
            self.assertEqual(path.read_bytes(), first)

    def test_damaged_v1_is_not_rewritten_or_backed_up(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root); path = Path(store.path('conversation'))
            path.parent.mkdir(parents=True)
            old = self.old(); old['rounds'][0]['public_hash'] = '0' * 64
            body = json.dumps(old, ensure_ascii=False, sort_keys=True)
            path.write_text(body)
            with self.assertRaises(InvalidState):
                store.load_raw('conversation')
            self.assertEqual(path.read_text(), body)
            self.assertFalse(Path(str(path) + '.v1.backup').exists())

    def test_migration_is_idempotent(self):
        once = migrate_v1(self.old())
        self.assertEqual(migrate_v1(once), once)


if __name__ == '__main__':
    unittest.main()
