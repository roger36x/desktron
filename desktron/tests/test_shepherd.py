"""Project Shepherd metadata, fencing, checkpoints and switching."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron' / 'lib'))

from shepherd import (Checkpoints, Conflict, Coordinator, InvalidState,
                      Ownership, ReadProjector, Store, SwitchRunner)
from shepherd.coordinator import digest


def source(seq='1'):
    return {'kind': 'native_round', 'engine': 'claude', 'session_id': 'native_1',
            'entry_id': seq, 'source_key': 'claude:native_1:' + seq}


class ActiveFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.coordinator = Coordinator(self.store)
        self.cid = 'conversation_1'
        self.coordinator.create(self.cid)
        self.binding = self.coordinator.bind(
            self.cid, 'claude', 'native_1', native_cwd=self.tmp.name, connector_version='claude-v1',
            binding_id='binding_1', ownership_nonce='secret_nonce')

    def acquire(self):
        return self.coordinator.acquire(self.cid, self.binding, ownership_nonce='secret_nonce')

    def commit(self, lease, seq='1', estimated_tokens=0):
        return self.coordinator.commit(
            self.cid, lease['lease_id'], lease['generation'], source=source(seq),
            source_hash=digest({'native': seq}), public_hash=digest({'public': seq}),
            estimated_tokens=estimated_tokens)



class ShepherdCore(ActiveFixture, unittest.TestCase):
    def test_commit_is_contiguous_and_active_binding_advances_without_replay(self):
        lease = self.acquire()
        first = self.commit(lease)
        second = self.commit(lease, '2')
        state = self.coordinator.state(self.cid)
        self.assertEqual([r['offset'] for r in state['rounds']], [1, 2])
        self.assertNotIn('parent_hash', second)
        self.assertEqual(state['bindings'][self.binding]['imported_through_seq'], 2)
        self.assertEqual(state['bindings'][self.binding]['imported_turn_hash'], second['entry_hash'])

    def test_duplicate_source_is_idempotent_but_changed_content_conflicts(self):
        lease = self.acquire()
        first = self.commit(lease)
        self.assertEqual(self.commit(lease), first)
        with self.assertRaises(Conflict):
            self.coordinator.commit(self.cid, lease['lease_id'], lease['generation'],
                source=source(), source_hash=digest({'changed': True}), public_hash=digest({'public': '1'}))
        with self.assertRaises(Conflict):
            self.coordinator.commit(self.cid, lease['lease_id'], lease['generation'],
                source=source(), source_hash=digest({'native': '1'}), public_hash=digest({'public': '1'}),
                terminal_state='failed_recoverable')

    def test_old_generation_cannot_commit_after_revoke(self):
        lease = self.acquire()
        self.coordinator.revoke(self.cid, lease['lease_id'], lease['generation'])
        with self.assertRaises(Conflict):
            self.commit(lease)

    def test_producer_token_is_derived_and_never_persisted_as_lease(self):
        producer = self.acquire()
        self.assertEqual(producer['lease_id'], self.binding)
        raw = json.loads(Path(self.store.path(self.cid)).read_text())
        self.assertNotIn('lease', raw)
        self.assertEqual(raw['active_binding_id'], self.binding)
        self.assertEqual(raw['producer_generation'], producer['generation'])

    def test_legacy_lease_is_removed_by_next_locked_state_change(self):
        producer = self.acquire()
        path = Path(self.store.path(self.cid))
        raw = json.loads(path.read_text())
        raw['lease'] = {'lease_id':'lease_legacy','binding_id':self.binding,
                        'generation':producer['generation'],'acquired_at':1}
        path.write_text(json.dumps(raw))
        loaded = self.coordinator.state(self.cid)
        self.assertEqual(loaded['lease']['lease_id'], self.binding)
        self.coordinator.begin_round(
            self.cid, self.binding, producer['generation'], attempt_id='attempt_migrate')
        migrated = json.loads(path.read_text())
        self.assertNotIn('lease', migrated)
        self.assertNotIn('inflight', migrated)
        self.assertNotIn('lease_id', migrated['active_turn'])

    def test_lagging_consumer_cannot_acquire_and_offset_never_moves_back(self):
        lease = self.acquire(); first = self.commit(lease)
        self.coordinator.revoke(self.cid, lease['lease_id'], lease['generation'])
        second = self.coordinator.bind(self.cid, 'pi', 'native_2', native_cwd=self.tmp.name, connector_version='pi-v1',
            binding_id='binding_2', ownership_nonce='other_nonce')
        with self.assertRaises(Conflict):
            self.coordinator.acquire(self.cid, second, ownership_nonce='other_nonce')
        self.coordinator.advance(self.cid, second, 1, first['entry_hash'], 'revision-1',
                                 ownership_nonce='other_nonce')
        self.coordinator.acquire(self.cid, second, ownership_nonce='other_nonce')
        with self.assertRaises(Conflict):
            self.coordinator.advance(self.cid, second, 0, '', 'revision-0', ownership_nonce='other_nonce')

    def test_switch_fences_old_producer_until_target_is_caught_up(self):
        old = self.acquire(); first = self.commit(old)
        target = self.coordinator.bind(self.cid, 'pi', 'native_2', native_cwd=self.tmp.name, connector_version='pi-v1',
            binding_id='binding_2', ownership_nonce='other_nonce')
        pending = self.coordinator.prepare_switch(self.cid, target, ownership_nonce='other_nonce',
            lease_id=old['lease_id'], generation=old['generation'])
        state = self.coordinator.state(self.cid)
        self.assertIsNone(state['lease'])
        self.assertEqual(state['active_binding_id'], '')
        self.assertEqual(state['pending_switch']['switch_id'], pending['switch_id'])
        with self.assertRaises(Conflict):
            self.commit(old, '2')
        with self.assertRaises(Conflict):
            self.coordinator.complete_switch(self.cid, pending['switch_id'], ownership_nonce='other_nonce')
        self.coordinator.advance(self.cid, target, 1, first['entry_hash'], 'revision-1',
                                 ownership_nonce='other_nonce')
        with self.assertRaisesRegex(Conflict, 'Append'):
            self.coordinator.complete_switch(
                self.cid, pending['switch_id'], ownership_nonce='other_nonce')
        self.coordinator.note_switch_operation(
            self.cid, pending['switch_id'], 'append_applied')
        current = self.coordinator.complete_switch(self.cid, pending['switch_id'], ownership_nonce='other_nonce')
        self.assertEqual(current['binding_id'], target)
        self.assertEqual(current['generation'], pending['generation'])

    def test_cancelled_or_reloaded_switch_never_revives_old_lease(self):
        old = self.acquire()
        target = self.coordinator.bind(self.cid, 'pi', 'native_2', native_cwd=self.tmp.name, connector_version='pi-v1',
            binding_id='binding_2', ownership_nonce='other_nonce')
        pending = self.coordinator.prepare_switch(self.cid, target, ownership_nonce='other_nonce',
            lease_id=old['lease_id'], generation=old['generation'])
        restored = Coordinator(Store(self.tmp.name))
        self.assertEqual(restored.state(self.cid)['pending_switch']['switch_id'], pending['switch_id'])
        restored.cancel_switch(self.cid, pending['switch_id'])
        state = restored.state(self.cid)
        self.assertIsNone(state['pending_switch']); self.assertIsNone(state['lease'])
        with self.assertRaises(Conflict):
            self.commit(old)

    def test_delete_tombstone_abandons_fenced_switch_and_preserves_evidence(self):
        old = self.acquire(); first = self.commit(old)
        target = self.coordinator.bind(
            self.cid, 'pi', 'native_2', native_cwd=self.tmp.name,
            connector_version='pi-v1', binding_id='binding_2', ownership_nonce='other_nonce')
        pending = self.coordinator.prepare_switch(
            self.cid, target, ownership_nonce='other_nonce',
            lease_id=old['lease_id'], generation=old['generation'])
        tombstone = self.coordinator.tombstone_for_delete(self.cid)
        state = self.coordinator.state(self.cid)
        self.assertEqual(tombstone['state'], 'deleted')
        self.assertEqual(tombstone['abandoned_switch_id'], pending['switch_id'])
        self.assertIsNone(state['pending_switch'])
        self.assertEqual(state['switch_history'][-1]['state'], 'abandoned_by_delete')
        self.assertEqual(state['bindings'][target]['status'], 'quarantined')
        self.assertEqual(state['rounds'][0]['source_hash'], first['source_hash'])
        self.assertEqual(self.coordinator.tombstone_for_delete(self.cid), tombstone)
        with self.assertRaisesRegex(Conflict, '已删除'):
            self.coordinator.acquire(
                self.cid, self.binding, ownership_nonce='secret_nonce')

    def test_delete_tombstone_requires_linked_audit_evidence(self):
        self.acquire()
        self.coordinator.tombstone_for_delete(self.cid)
        path = Path(self.store.path(self.cid))
        raw = json.loads(path.read_text())
        raw['audit_events'] = []
        path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(InvalidState, '墓碑证据不一致'):
            Store(self.tmp.name).load(self.cid)

    def test_delete_tombstone_rejects_active_turn_without_blocked_evidence(self):
        lease = self.acquire()
        self.coordinator.begin_round(
            self.cid, lease['lease_id'], lease['generation'], attempt_id='attempt_running')
        with self.assertRaisesRegex(Conflict, '仍在执行或切换'):
            self.coordinator.tombstone_for_delete(self.cid)
        self.assertIsNone(self.coordinator.state(self.cid).get('tombstone'))

    def test_delete_tombstone_preserves_blocked_reserved_turn_evidence(self):
        lease = self.acquire()
        self.coordinator.begin_round(
            self.cid, lease['lease_id'], lease['generation'], attempt_id='attempt_blocked')
        self.coordinator.mark_unresolved(
            self.cid, lease['lease_id'], lease['generation'],
            attempt_id='attempt_blocked', reason='source_unavailable')
        before = self.coordinator.state(self.cid)['active_turn']
        self.coordinator.tombstone_for_delete(self.cid)
        state = self.coordinator.state(self.cid)
        self.assertEqual(state['active_turn'], before)
        self.assertEqual(state['tombstone']['state'], 'deleted')

    def test_delete_tombstone_rejects_blocked_reserved_turn_with_live_identity(self):
        lease = self.acquire()
        self.coordinator.begin_round(
            self.cid, lease['lease_id'], lease['generation'], attempt_id='attempt_blocked',
            recovery={'phase':'reserved','boot_id':'boot_old','boundary':{},
                      'process':{'pid':42,'pgid':42,'start_token':'same'}})
        self.coordinator.mark_unresolved(
            self.cid, lease['lease_id'], lease['generation'],
            attempt_id='attempt_blocked', reason='source_unavailable')
        with mock.patch('shepherd.recovery.process_state', return_value='alive'), \
             self.assertRaisesRegex(Conflict, 'Writer 尚未确认退出'):
            self.coordinator.tombstone_for_delete(self.cid)

    def test_delete_tombstone_accepts_blocked_reserved_turn_with_dead_identity(self):
        lease = self.acquire()
        self.coordinator.begin_round(
            self.cid, lease['lease_id'], lease['generation'], attempt_id='attempt_blocked',
            recovery={'phase':'reserved','boot_id':'boot_old','boundary':{},
                      'process':{'pid':42,'pgid':42,'start_token':'old'}})
        self.coordinator.mark_unresolved(
            self.cid, lease['lease_id'], lease['generation'],
            attempt_id='attempt_blocked', reason='source_unavailable')
        with mock.patch('shepherd.recovery.process_state', return_value='dead'):
            self.coordinator.tombstone_for_delete(self.cid)
        self.assertEqual(self.coordinator.state(self.cid)['tombstone']['state'], 'deleted')

    def test_delete_tombstone_rejects_blocked_dispatched_turn_without_identity(self):
        lease = self.acquire()
        row = self.coordinator.begin_round(
            self.cid, lease['lease_id'], lease['generation'], attempt_id='attempt_blocked',
            recovery={'phase':'reserved','boot_id':'boot_old','boundary':{}})
        self.coordinator.mark_dispatched(
            self.cid, lease['lease_id'], lease['generation'], attempt_id=row['attempt_id'])
        self.coordinator.mark_unresolved(
            self.cid, lease['lease_id'], lease['generation'],
            attempt_id=row['attempt_id'], reason='source_unavailable')
        with self.assertRaisesRegex(Conflict, 'Writer 身份缺失'):
            self.coordinator.tombstone_for_delete(self.cid)

    def test_delete_tombstone_accepts_blocked_dispatched_turn_with_dead_identity(self):
        lease = self.acquire()
        row = self.coordinator.begin_round(
            self.cid, lease['lease_id'], lease['generation'], attempt_id='attempt_blocked',
            recovery={'phase':'reserved','boot_id':'boot_old','boundary':{}})
        self.coordinator.mark_dispatched(
            self.cid, lease['lease_id'], lease['generation'], attempt_id=row['attempt_id'],
            process={'pid':42,'pgid':42,'start_token':'old'})
        self.coordinator.mark_unresolved(
            self.cid, lease['lease_id'], lease['generation'],
            attempt_id=row['attempt_id'], reason='source_unavailable')
        with mock.patch('shepherd.recovery.process_state', return_value='dead'):
            self.coordinator.tombstone_for_delete(self.cid)
        self.assertEqual(self.coordinator.state(self.cid)['tombstone']['state'], 'deleted')

    def test_delete_tombstone_rejects_legacy_dispatched_writer_without_identity(self):
        lease = self.acquire()
        self.coordinator.revoke(self.cid, lease['lease_id'], lease['generation'])
        with self.store.change(self.cid) as state:
            state['legacy_recoveries'].append({
                'recovery_id':'legacy_1','source_binding_id':self.binding,
                'resume_binding_id':self.binding,'insert_seq':1,
                'inflight':{'attempt_id':'attempt_legacy',
                            'recovery':{'phase':'dispatched','boot_id':'old'}},
                'created_at':1})
        with self.assertRaisesRegex(Conflict, 'Writer 身份缺失'):
            self.coordinator.tombstone_for_delete(self.cid)

    def test_delete_tombstone_accepts_legacy_writer_with_dead_identity(self):
        lease = self.acquire()
        self.coordinator.revoke(self.cid, lease['lease_id'], lease['generation'])
        with self.store.change(self.cid) as state:
            state['legacy_recoveries'].append({
                'recovery_id':'legacy_1','source_binding_id':self.binding,
                'resume_binding_id':self.binding,'insert_seq':1,
                'inflight':{'attempt_id':'attempt_legacy','recovery':{
                    'phase':'dispatched','boot_id':'old',
                    'process':{'pid':42,'pgid':42,'start_token':'old'}}},
                'created_at':1})
        with mock.patch('shepherd.recovery.process_state', return_value='dead'):
            self.coordinator.tombstone_for_delete(self.cid)
        state = self.coordinator.state(self.cid)
        self.assertEqual(state['tombstone']['state'], 'deleted')
        self.assertEqual(state['legacy_recoveries'][0]['recovery_id'], 'legacy_1')

    def test_delete_tombstone_rejects_current_in_process_switch_writer(self):
        old = self.acquire()
        target = self.coordinator.bind(
            self.cid, 'pi', 'native_2', native_cwd=self.tmp.name,
            connector_version='pi-v1', binding_id='binding_2', ownership_nonce='other_nonce')
        pending = self.coordinator.prepare_switch(
            self.cid, target, ownership_nonce='other_nonce',
            lease_id=old['lease_id'], generation=old['generation'])
        self.coordinator.note_switch_operation(
            self.cid, pending['switch_id'], 'append_writing',
            writer={'kind':'in_process','status':'active','boot_id':'boot_current'})
        with mock.patch('shepherd.recovery.BOOT_ID', 'boot_current'), \
             self.assertRaisesRegex(Conflict, 'Writer 尚未确认退出'):
            self.coordinator.tombstone_for_delete(self.cid)
        self.assertIsNone(self.coordinator.state(self.cid).get('tombstone'))

    def test_delete_tombstone_accepts_dead_process_switch_writer(self):
        old = self.acquire()
        target = self.coordinator.bind(
            self.cid, 'pi', 'native_2', native_cwd=self.tmp.name,
            connector_version='pi-v1', binding_id='binding_2', ownership_nonce='other_nonce')
        pending = self.coordinator.prepare_switch(
            self.cid, target, ownership_nonce='other_nonce',
            lease_id=old['lease_id'], generation=old['generation'])
        self.coordinator.note_switch_operation(
            self.cid, pending['switch_id'], 'append_writing', writer={
                'kind':'process','status':'unknown','boot_id':'boot_old',
                'process':{'pid':42,'pgid':42,'start_token':'old'}})
        with mock.patch('shepherd.recovery.process_state', return_value='dead'):
            self.coordinator.tombstone_for_delete(self.cid)
        self.assertEqual(self.coordinator.state(self.cid)['tombstone']['state'], 'deleted')

    def test_delete_tombstone_rejects_stale_dead_label_for_live_process(self):
        old = self.acquire()
        target = self.coordinator.bind(
            self.cid, 'pi', 'native_2', native_cwd=self.tmp.name,
            connector_version='pi-v1', binding_id='binding_2', ownership_nonce='other_nonce')
        pending = self.coordinator.prepare_switch(
            self.cid, target, ownership_nonce='other_nonce',
            lease_id=old['lease_id'], generation=old['generation'])
        self.coordinator.note_switch_operation(
            self.cid, pending['switch_id'], 'append_applied', writer={
                'kind':'process','status':'dead','boot_id':'boot_old',
                'process':{'pid':42,'pgid':42,'start_token':'same'}})
        with mock.patch('shepherd.recovery.process_state', return_value='alive'), \
             self.assertRaisesRegex(Conflict, 'Writer 尚未确认退出'):
            self.coordinator.tombstone_for_delete(self.cid)

    def test_delete_tombstone_archives_native_created_retarget_as_quarantined(self):
        old = self.acquire()
        target = self.coordinator.bind(
            self.cid, 'pi', 'native_2', native_cwd=self.tmp.name,
            connector_version='pi-v1', binding_id='binding_2', ownership_nonce='other_nonce')
        pending = self.coordinator.prepare_switch(
            self.cid, target, ownership_nonce='other_nonce',
            lease_id=old['lease_id'], generation=old['generation'])
        replacement = {
            'binding_id':'binding_3','conversation_id':self.cid,'owner':'shepherd',
            'ownership_nonce':'third_nonce','engine':'codex','native_session_id':'native_3',
            'native_locator':'native_3','native_cwd':self.tmp.name,
            'connector_version':'codex-v1','imported_through_seq':0,
            'imported_turn_hash':'','native_revision':'','base_checkpoint_id':'',
            'status':'current','created_at':1}
        intent = self.coordinator.reserve_retarget(
            self.cid, pending['switch_id'], 'codex', self.tmp.name,
            binding_id='binding_3', ownership_nonce='third_nonce', boot_id='boot_old',
            selection_epoch=1)
        self.coordinator.note_retarget_writer(
            self.cid, pending['switch_id'], intent['retarget_id'],
            {'kind':'in_process','status':'stopped','boot_id':'boot_old'})
        self.coordinator.ownership.issue(replacement)
        self.coordinator.stage_retarget(
            self.cid, pending['switch_id'], replacement, retarget_id=intent['retarget_id'])
        self.coordinator.tombstone_for_delete(self.cid)
        archived = self.coordinator.state(self.cid)['switch_history'][-1]
        self.assertEqual(archived['retarget']['binding']['status'], 'quarantined')
        self.assertEqual(archived['retarget']['binding']['native_session_id'], 'native_3')

    def test_delete_tombstone_checks_and_archives_retarget_history(self):
        old = self.acquire()
        target = self.coordinator.bind(
            self.cid, 'pi', 'native_2', native_cwd=self.tmp.name,
            connector_version='pi-v1', binding_id='binding_2', ownership_nonce='other_nonce')
        pending = self.coordinator.prepare_switch(
            self.cid, target, ownership_nonce='other_nonce',
            lease_id=old['lease_id'], generation=old['generation'])
        replacement = {
            'binding_id':'binding_3','conversation_id':self.cid,'owner':'shepherd',
            'ownership_nonce':'third_nonce','engine':'codex','native_session_id':'native_3',
            'native_locator':'native_3','native_cwd':self.tmp.name,
            'connector_version':'codex-v1','imported_through_seq':0,
            'imported_turn_hash':'','native_revision':'','base_checkpoint_id':'',
            'status':'current','created_at':1}
        first = self.coordinator.reserve_retarget(
            self.cid, pending['switch_id'], 'codex', self.tmp.name,
            binding_id='binding_3', ownership_nonce='third_nonce', boot_id='boot_old',
            selection_epoch=1)
        self.coordinator.note_retarget_writer(
            self.cid, pending['switch_id'], first['retarget_id'], {
                'kind':'process','status':'unknown','boot_id':'boot_old',
                'process':{'pid':42,'pgid':42,'start_token':'old'}})
        self.coordinator.ownership.issue(replacement)
        self.coordinator.stage_retarget(
            self.cid, pending['switch_id'], replacement, retarget_id=first['retarget_id'])
        self.coordinator.reserve_retarget(
            self.cid, pending['switch_id'], 'claude', self.tmp.name,
            binding_id='binding_4', ownership_nonce='fourth_nonce', boot_id='boot_old',
            selection_epoch=2, replace_existing=True)
        with mock.patch('shepherd.recovery.process_state', return_value='alive'), \
             self.assertRaisesRegex(Conflict, 'Writer 尚未确认退出'):
            self.coordinator.tombstone_for_delete(self.cid)
        with mock.patch('shepherd.recovery.process_state', return_value='dead'):
            self.coordinator.tombstone_for_delete(self.cid)
        state = self.coordinator.state(self.cid)
        self.assertEqual(state['bindings']['binding_3']['status'], 'quarantined')
        archived = state['switch_history'][-1]['retarget_history'][0]
        self.assertEqual(archived['binding']['status'], 'quarantined')

    def test_explicit_retarget_never_revives_fenced_binding(self):
        old = self.acquire()
        target = self.coordinator.bind(
            self.cid, 'pi', 'native_2', native_cwd=self.tmp.name,
            connector_version='pi-v1', binding_id='binding_2', ownership_nonce='other_nonce')
        pending = self.coordinator.prepare_switch(
            self.cid, target, ownership_nonce='other_nonce',
            lease_id=old['lease_id'], generation=old['generation'])
        replacement = {
            'binding_id':'binding_3','conversation_id':self.cid,'owner':'shepherd',
            'ownership_nonce':'third_nonce','engine':'codex','native_session_id':'native_3',
            'native_locator':'native_3','native_cwd':self.tmp.name,
            'connector_version':'codex-v1','applied_offset':0,'applied_event_hash':'',
            'native_revision':'','base_checkpoint_id':'','status':'current','created_at':1,
        }
        intent = self.coordinator.reserve_retarget(
            self.cid, pending['switch_id'], 'codex', self.tmp.name,
            binding_id='binding_3', ownership_nonce='third_nonce', boot_id='boot_test',
            selection_epoch=1)
        self.coordinator.note_retarget_writer(
            self.cid, pending['switch_id'], intent['retarget_id'],
            {'kind':'in_process','status':'stopped','boot_id':'boot_test'})
        self.coordinator.ownership.issue(replacement)
        self.coordinator.stage_retarget(
            self.cid, pending['switch_id'], replacement, retarget_id=intent['retarget_id'])
        moved = self.coordinator.retarget_switch(
            self.cid, pending['switch_id'], retarget_id=intent['retarget_id'])
        state = self.coordinator.state(self.cid)
        self.assertIsNone(state['lease'])
        self.assertEqual(state['bindings'][target]['status'], 'retargeted')
        self.assertEqual(state['bindings']['binding_3']['status'], 'current')
        self.assertEqual(moved['target_binding_id'], 'binding_3')
        self.assertNotEqual(moved['generation'], old['generation'])
        self.assertEqual(state['switch_history'][-1]['state'], 'retargeted')

    def test_retarget_creation_intent_is_durable_and_not_silently_replaced(self):
        old = self.acquire()
        target = self.coordinator.bind(
            self.cid, 'pi', 'native_2', native_cwd=self.tmp.name,
            connector_version='pi-v1', binding_id='binding_2', ownership_nonce='other_nonce')
        pending = self.coordinator.prepare_switch(
            self.cid, target, ownership_nonce='other_nonce',
            lease_id=old['lease_id'], generation=old['generation'])
        first = self.coordinator.reserve_retarget(
            self.cid, pending['switch_id'], 'codex', self.tmp.name,
            binding_id='binding_3', ownership_nonce='third_nonce', boot_id='boot_test',
            selection_epoch=1)
        self.assertEqual(self.coordinator.reserve_retarget(
            self.cid, pending['switch_id'], 'codex', self.tmp.name,
            binding_id='binding_3', ownership_nonce='third_nonce', boot_id='boot_test',
            selection_epoch=1), first)
        with self.assertRaises(Conflict):
            self.coordinator.reserve_retarget(
                self.cid, pending['switch_id'], 'claude', self.tmp.name,
                binding_id='binding_4', ownership_nonce='fourth_nonce', boot_id='boot_test',
                selection_epoch=1)
        state = self.coordinator.state(self.cid)
        self.assertEqual(state['pending_switch']['retarget']['binding_id'], 'binding_3')
        self.assertIsNone(state['lease'])

    def test_wrong_owner_cannot_write_binding(self):
        with self.assertRaises(Conflict):
            self.coordinator.acquire(self.cid, self.binding, ownership_nonce='wrong')

    def test_missing_or_tampered_ownership_receipt_blocks_write(self):
        receipt = Path(self.coordinator.ownership.path(self.binding))
        original = receipt.read_bytes()
        receipt.unlink()
        with self.assertRaises(Conflict):
            self.coordinator.acquire(self.cid, self.binding, ownership_nonce='secret_nonce')
        receipt.write_bytes(original); receipt.chmod(0o600)
        value = json.loads(receipt.read_text()); value['native_session_id'] = 'other_native'
        receipt.write_text(json.dumps(value)); receipt.chmod(0o600)
        with self.assertRaises(Conflict):
            self.coordinator.acquire(self.cid, self.binding, ownership_nonce='secret_nonce')

    def test_binding_id_cannot_be_reused_by_another_conversation(self):
        other = Coordinator(self.store, self.coordinator.ownership)
        other.create('conversation_2')
        with self.assertRaises(Conflict):
            other.bind('conversation_2', 'claude', 'native_other', native_cwd=self.tmp.name, connector_version='claude-v1',
                       binding_id=self.binding, ownership_nonce='other_nonce')
        self.assertEqual(other.state('conversation_2')['bindings'], {})

    def test_state_detects_turn_tampering(self):
        lease = self.acquire(); self.commit(lease)
        path = Path(self.store.path(self.cid))
        state = json.loads(path.read_text())
        state['turns'][0]['public_hash'] = '0' * 64
        path.write_text(json.dumps(state))
        with self.assertRaises(InvalidState):
            self.store.load(self.cid)

    def test_manifest_rejects_message_body_in_source_reference(self):
        lease = self.acquire()
        bad = {**source(), 'text': 'must not be stored'}
        with self.assertRaises(InvalidState):
            self.coordinator.commit(self.cid, lease['lease_id'], lease['generation'], source=bad,
                source_hash=digest({}), public_hash=digest({}))


class CheckpointPlanning(ActiveFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.lease = self.acquire()
        self.rows = [self.commit(self.lease, str(index), estimated_tokens=100)
                     for index in range(1, 6)]
        self.checkpoints = Checkpoints(self.store)

    def checkpoint(self, through, tokens=80, portability='portable', codec='portable-v1'):
        return self.checkpoints.register(self.cid, through, source={
            'kind': 'checkpoint', 'engine': 'claude', 'session_id': 'native_1',
            'entry_id': str(through), 'source_key': f'checkpoint:native_1:{through}'},
            source_hash=digest({'summary': through}), public_hash=digest({'portable': through}),
            estimated_tokens=tokens, projection_codec_version=codec,
            portability=portability, checkpoint_id=f'checkpoint_{through}_{portability}')

    def test_short_tail_uses_incremental_even_when_checkpoint_exists(self):
        self.checkpoint(4)
        plan = self.checkpoints.plan(self.cid, 4, working_limit=500, reserve_tokens=100)
        self.assertEqual(plan['strategy'], 'incremental')
        self.assertEqual(plan['from_offset'], 5)

    def test_long_tail_uses_latest_compatible_portable_checkpoint(self):
        self.checkpoint(2, tokens=70); latest = self.checkpoint(4, tokens=80)
        self.checkpoint(5, tokens=10, portability='local')
        plan = self.checkpoints.plan(self.cid, 0, working_limit=300, reserve_tokens=50,
                                     projection_codec_version='portable-v1')
        self.assertEqual(plan['strategy'], 'checkpoint')
        self.assertEqual(plan['checkpoint']['checkpoint_id'], latest['checkpoint_id'])
        self.assertEqual(plan['from_offset'], 5)

    def test_incompatible_or_oversized_checkpoint_blocks_switch(self):
        self.checkpoint(4, tokens=250, codec='other-v1')
        plan = self.checkpoints.plan(self.cid, 0, working_limit=300, reserve_tokens=100,
                                     projection_codec_version='portable-v1')
        self.assertEqual(plan['strategy'], 'blocked')

    def test_checkpoint_does_not_rewrite_round_offsets(self):
        before = self.coordinator.state(self.cid)
        checkpoint = self.checkpoint(3)
        self.assertEqual(self.checkpoint(3), checkpoint)
        after = self.coordinator.state(self.cid)
        self.assertEqual(before['rounds'], after['rounds'])
        self.assertEqual(after['committed_offset'], 5)
        self.assertEqual(checkpoint['covers_hash'], self.rows[2]['entry_hash'])


class FakeResolver:
    def __init__(self, values): self.values = values
    def resolve(self, source_ref): return copy.deepcopy(self.values[source_ref['source_key']])


class FakeConnector:
    def __init__(self, binding_id, engine, native_session_id, connector_version, ownership_nonce,
                 fail=False, native_cwd=None):
        self.binding_id = binding_id; self.engine = engine
        self.native_session_id = native_session_id; self.native_cwd = native_cwd
        self.connector_version = connector_version
        self.ownership_nonce = ownership_nonce; self.fail = fail; self.calls = []; self.checkpoint_calls = []
    def preflight(self, rounds, **kwargs):
        pass
    def load_checkpoint(self, public, expected_revision='', checkpoint=None):
        self.checkpoint_calls.append((copy.deepcopy(public), copy.deepcopy(checkpoint), expected_revision))
        if self.fail: raise RuntimeError('native append failed')
        return {'native_revision': 'checkpoint-rev'}
    def append(self, rounds, expected_revision=''):
        self.calls.append((copy.deepcopy(rounds), expected_revision))
        if self.fail: raise RuntimeError('native append failed')
        return {'native_revision': ('rev-' + str(len(rounds))) if rounds else expected_revision}


class ProjectionAndSwitch(ActiveFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.old_lease = self.acquire()
        self.round = self.commit(self.old_lease, estimated_tokens=100)
        self.target = self.coordinator.bind(
            self.cid, 'pi', 'native_2', native_cwd=self.tmp.name, connector_version='pi-v1',
            binding_id='binding_2', ownership_nonce='other_nonce')
        self.values = {source()['source_key']: {
            'source': {'native': '1'}, 'public': {'public': '1'}}}

    def runner(self, fail=False):
        connector = FakeConnector(self.target, 'pi', 'native_2', 'pi-v1', 'other_nonce', fail,
                                  native_cwd=os.path.realpath(self.tmp.name))
        projector = ReadProjector(self.store, FakeResolver(self.values))
        return SwitchRunner(self.coordinator, projector, connector), connector

    def test_read_projection_verifies_source_and_public_hashes(self):
        got = ReadProjector(self.store, FakeResolver(self.values)).project(self.cid)
        self.assertEqual(got[0]['round_id'], self.round['round_id'])
        self.assertEqual(got[0]['public'], {'public': '1'})
        changed = copy.deepcopy(self.values); changed[source()['source_key']]['source'] = {'native': 'changed'}
        with self.assertRaises(Conflict):
            ReadProjector(self.store, FakeResolver(changed)).project(self.cid)

    def test_switch_runner_catches_up_then_grants_target_lease(self):
        runner, connector = self.runner()
        lease = runner.switch(self.cid, self.target, ownership_nonce='other_nonce',
                              lease_id=self.old_lease['lease_id'], generation=self.old_lease['generation'])
        self.assertEqual(lease['binding_id'], self.target)
        self.assertEqual(connector.calls[0][0][0]['offset'], 1)
        state = self.coordinator.state(self.cid)
        self.assertEqual(state['bindings'][self.target]['native_revision'], 'rev-1')
        self.assertIsNone(state['pending_switch'])

    def test_cancelled_prepare_keeps_original_producer_before_fence(self):
        runner, connector = self.runner()
        with self.assertRaisesRegex(Conflict, '更新的选择取消'):
            runner.switch(self.cid, self.target, ownership_nonce='other_nonce',
                lease_id=self.old_lease['lease_id'], generation=self.old_lease['generation'],
                cancelled=lambda: True)
        state = self.coordinator.state(self.cid)
        self.assertEqual(state['lease']['lease_id'], self.old_lease['lease_id'])
        self.assertIsNone(state['pending_switch'])
        self.assertEqual(connector.calls, [])

    def test_checkpoint_plan_loads_snapshot_then_only_its_tail(self):
        checkpoint_source = {'kind': 'checkpoint', 'engine': 'claude', 'session_id': 'native_1',
            'entry_id': 'cp1', 'source_key': 'checkpoint:native_1:cp1'}
        checkpoint_public = {'summary': 'portable'}
        checkpoints = Checkpoints(self.store)
        registered = checkpoints.register(self.cid, 1, source=checkpoint_source,
            source_hash=digest({'summary': 'native'}), public_hash=digest(checkpoint_public),
            estimated_tokens=20, projection_codec_version='portable-v1', checkpoint_id='checkpoint_cp1')
        self.values[checkpoint_source['source_key']] = {
            'source': {'summary': 'native'}, 'public': checkpoint_public}
        runner, connector = self.runner(); runner.checkpoints = checkpoints
        lease = runner.switch(self.cid, self.target, ownership_nonce='other_nonce',
            lease_id=self.old_lease['lease_id'], generation=self.old_lease['generation'],
            working_limit=50, reserve_tokens=10, projection_codec_version='portable-v1')
        self.assertEqual(lease['binding_id'], self.target)
        self.assertEqual(connector.checkpoint_calls, [(checkpoint_public, registered, '')])
        self.assertEqual(connector.calls, [([], 'checkpoint-rev')])
        self.assertEqual(registered['covers_through_offset'], 1)

    def test_blocked_budget_does_not_fence_current_producer(self):
        runner, _connector = self.runner(); runner.checkpoints = Checkpoints(self.store)
        with self.assertRaises(Conflict):
            runner.switch(self.cid, self.target, ownership_nonce='other_nonce',
                lease_id=self.old_lease['lease_id'], generation=self.old_lease['generation'],
                working_limit=50, reserve_tokens=10)
        state = self.coordinator.state(self.cid)
        self.assertEqual(state['lease']['lease_id'], self.old_lease['lease_id'])
        self.assertIsNone(state['pending_switch'])

    def test_checkpoint_unsupported_is_rejected_before_fence(self):
        checkpoint_source = {'kind': 'checkpoint', 'engine': 'claude', 'session_id': 'native_1',
            'entry_id': 'cp1', 'source_key': 'checkpoint:native_1:cp1'}
        checkpoints = Checkpoints(self.store)
        checkpoints.register(self.cid, 1, source=checkpoint_source,
            source_hash=digest({'summary':'native'}), public_hash=digest({'summary':'portable'}),
            estimated_tokens=20, projection_codec_version='portable-v1', checkpoint_id='checkpoint_cp1')
        self.values[checkpoint_source['source_key']] = {
            'source': {'summary':'native'}, 'public': {'summary':'portable'}}
        runner, connector = self.runner(); runner.checkpoints = checkpoints
        connector.load_checkpoint = None
        with self.assertRaisesRegex(Conflict, '不支持 Portable Checkpoint'):
            runner.switch(self.cid, self.target, ownership_nonce='other_nonce',
                lease_id=self.old_lease['lease_id'], generation=self.old_lease['generation'],
                working_limit=100, reserve_tokens=10, projection_codec_version='portable-v1')
        state = self.coordinator.state(self.cid)
        self.assertEqual(state['lease']['lease_id'], self.old_lease['lease_id'])
        self.assertIsNone(state['pending_switch'])

    def test_native_append_landed_before_metadata_crash_resumes_idempotently(self):
        class LandedThenCrashed(FakeConnector):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs); self.landed = None
            def append(self, rounds, expected_revision=''):
                self.calls.append((copy.deepcopy(rounds), expected_revision))
                signature = [(row['offset'], row['entry_hash']) for row in rounds]
                if self.landed is None:
                    self.landed = signature
                    raise RuntimeError('crash after native append')
                self.assert_same = signature == self.landed
                return {'native_revision':'landed-revision'}
        connector = LandedThenCrashed(self.target, 'pi', 'native_2', 'pi-v1', 'other_nonce',
                                      native_cwd=os.path.realpath(self.tmp.name))
        runner = SwitchRunner(self.coordinator,
            ReadProjector(self.store, FakeResolver(self.values)), connector)
        with self.assertRaisesRegex(RuntimeError, 'crash after native append'):
            runner.switch(self.cid, self.target, ownership_nonce='other_nonce',
                          lease_id=self.old_lease['lease_id'], generation=self.old_lease['generation'])
        fenced = self.coordinator.state(self.cid)
        self.assertEqual(fenced['pending_switch']['operation']['phase'], 'append_writing')
        lease = runner.resume_switch(self.cid, fenced['pending_switch']['switch_id'],
                                     ownership_nonce='other_nonce')
        self.assertTrue(connector.assert_same)
        self.assertEqual(lease['binding_id'], self.target)
        self.assertEqual(self.coordinator.state(self.cid)['bindings'][self.target]['native_revision'],
                         'landed-revision')

    def test_missing_writer_claim_never_allows_retarget_or_resume(self):
        pending = self.coordinator.prepare_switch(
            self.cid, self.target, ownership_nonce='other_nonce',
            lease_id=self.old_lease['lease_id'], generation=self.old_lease['generation'])
        path = Path(self.store.path(self.cid))
        state = json.loads(path.read_text())
        state['pending_switch'].pop('operation', None)
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(InvalidState, 'Operation'):
            self.coordinator.state(self.cid)

    def test_process_preflight_failure_before_spawn_is_retryable(self):
        class PreSpawnFailure(FakeConnector):
            writer_kind = 'process'
            writer_notifications = True
            def append(self, rounds, expected_revision='', on_writer=None):
                self.calls.append((copy.deepcopy(rounds), expected_revision))
                raise RuntimeError('failed before spawn')
        connector = PreSpawnFailure(self.target, 'pi', 'native_2', 'pi-v1', 'other_nonce',
                                    native_cwd=os.path.realpath(self.tmp.name))
        runner = SwitchRunner(self.coordinator,
            ReadProjector(self.store, FakeResolver(self.values)), connector)
        with self.assertRaisesRegex(RuntimeError, 'failed before spawn'):
            runner.switch(self.cid, self.target, ownership_nonce='other_nonce',
                lease_id=self.old_lease['lease_id'], generation=self.old_lease['generation'])
        pending = self.coordinator.state(self.cid)['pending_switch']
        self.assertEqual(pending['operation']['writer']['status'], 'stopped')
        recovered = FakeConnector(self.target, 'pi', 'native_2', 'pi-v1', 'other_nonce',
                                  native_cwd=os.path.realpath(self.tmp.name))
        producer = SwitchRunner(self.coordinator,
            ReadProjector(self.store, FakeResolver(self.values)), recovered).resume_switch(
                self.cid, pending['switch_id'], ownership_nonce='other_nonce')
        self.assertEqual(producer['binding_id'], self.target)

    def test_append_failure_leaves_durable_fence_and_no_producer(self):
        runner, _connector = self.runner(fail=True)
        with self.assertRaisesRegex(RuntimeError, 'native append failed'):
            runner.switch(self.cid, self.target, ownership_nonce='other_nonce',
                          lease_id=self.old_lease['lease_id'], generation=self.old_lease['generation'])
        state = Coordinator(Store(self.tmp.name)).state(self.cid)
        self.assertIsNone(state['lease'])
        self.assertEqual(state['pending_switch']['target_binding_id'], self.target)

    def test_checkpoint_native_write_landed_before_advance_resumes_from_plan(self):
        checkpoint_source = {'kind': 'checkpoint', 'engine': 'claude', 'session_id': 'native_1',
            'entry_id': 'cp1', 'source_key': 'checkpoint:native_1:cp1'}
        checkpoints = Checkpoints(self.store)
        checkpoint = checkpoints.register(self.cid, 1, source=checkpoint_source,
            source_hash=digest({'summary':'native'}), public_hash=digest({'summary':'portable'}),
            estimated_tokens=20, projection_codec_version='portable-v1', checkpoint_id='checkpoint_cp1')
        self.values[checkpoint_source['source_key']] = {
            'source': {'summary':'native'}, 'public': {'summary':'portable'}}
        class CheckpointLanded(FakeConnector):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs); self.landed = False
            def load_checkpoint(self, public, expected_revision='', checkpoint=None):
                self.checkpoint_calls.append((copy.deepcopy(public), copy.deepcopy(checkpoint), expected_revision))
                if not self.landed:
                    self.landed = True
                    raise RuntimeError('crash after checkpoint write')
                return {'native_revision':'checkpoint-rev'}
        connector = CheckpointLanded(self.target, 'pi', 'native_2', 'pi-v1', 'other_nonce',
                                     native_cwd=os.path.realpath(self.tmp.name))
        runner = SwitchRunner(self.coordinator,
            ReadProjector(self.store, FakeResolver(self.values)), connector, checkpoints)
        with self.assertRaisesRegex(RuntimeError, 'crash after checkpoint write'):
            runner.switch(self.cid, self.target, ownership_nonce='other_nonce',
                lease_id=self.old_lease['lease_id'], generation=self.old_lease['generation'],
                working_limit=50, reserve_tokens=10, projection_codec_version='portable-v1')
        fenced = self.coordinator.state(self.cid)
        pending = fenced['pending_switch']
        self.assertEqual(pending['plan']['checkpoint']['checkpoint_id'], checkpoint['checkpoint_id'])
        self.assertEqual(pending['operation']['phase'], 'checkpoint_writing')
        lease = runner.resume_switch(self.cid, pending['switch_id'], ownership_nonce='other_nonce')
        self.assertEqual(lease['binding_id'], self.target)
        self.assertEqual(len(connector.checkpoint_calls), 2)
        target = self.coordinator.state(self.cid)['bindings'][self.target]
        self.assertEqual(target['base_checkpoint_id'], checkpoint['checkpoint_id'])

    def test_checkpoint_tail_advanced_before_completion_resumes_commit(self):
        self.round = self.commit(self.old_lease, '2', estimated_tokens=100)
        checkpoint_source = {'kind':'checkpoint','engine':'claude','session_id':'native_1',
            'entry_id':'cp1','source_key':'checkpoint:native_1:cp1'}
        checkpoints = Checkpoints(self.store)
        checkpoints.register(self.cid, 1, source=checkpoint_source,
            source_hash=digest({'summary':'native'}), public_hash=digest({'summary':'portable'}),
            estimated_tokens=20, projection_codec_version='portable-v1',
            checkpoint_id='checkpoint_cp1')
        self.values[source('2')['source_key']] = {
            'source':{'native':'2'}, 'public':{'public':'2'}}
        self.values[checkpoint_source['source_key']] = {
            'source':{'summary':'native'}, 'public':{'summary':'portable'}}
        connector = FakeConnector(self.target, 'pi', 'native_2', 'pi-v1', 'other_nonce',
                                  native_cwd=os.path.realpath(self.tmp.name))
        runner = SwitchRunner(self.coordinator,
            ReadProjector(self.store, FakeResolver(self.values)), connector, checkpoints)
        original = self.coordinator.note_switch_operation
        failed = {'done':False}
        def crash_after_advance(cid, switch_id, phase, **kwargs):
            if phase == 'append_applied' and not failed['done']:
                failed['done'] = True
                raise RuntimeError('crash after tail advance')
            return original(cid, switch_id, phase, **kwargs)
        with mock.patch.object(self.coordinator, 'note_switch_operation', side_effect=crash_after_advance):
            with self.assertRaisesRegex(RuntimeError, 'crash after tail advance'):
                runner.switch(self.cid, self.target, ownership_nonce='other_nonce',
                    lease_id=self.old_lease['lease_id'], generation=self.old_lease['generation'],
                    working_limit=150, reserve_tokens=10,
                    projection_codec_version='portable-v1')
        fenced = self.coordinator.state(self.cid)
        self.assertEqual(fenced['bindings'][self.target]['applied_offset'], 2)
        recovered = FakeConnector(self.target, 'pi', 'native_2', 'pi-v1', 'other_nonce',
                                  native_cwd=os.path.realpath(self.tmp.name))
        producer = SwitchRunner(self.coordinator,
            ReadProjector(self.store, FakeResolver(self.values)), recovered).resume_switch(
                self.cid, fenced['pending_switch']['switch_id'], ownership_nonce='other_nonce')
        self.assertEqual(producer['binding_id'], self.target)
        self.assertEqual(recovered.calls, [([], 'rev-1')])

    def test_checkpoint_success_tail_failure_resumes_after_checkpoint(self):
        # Add a second round so the checkpoint leaves a non-empty tail.
        self.round = self.commit(self.old_lease, '2', estimated_tokens=100)
        checkpoint_source = {'kind': 'checkpoint', 'engine': 'claude', 'session_id': 'native_1',
            'entry_id': 'cp1', 'source_key': 'checkpoint:native_1:cp1'}
        checkpoints = Checkpoints(self.store)
        checkpoint = checkpoints.register(self.cid, 1, source=checkpoint_source,
            source_hash=digest({'summary': 'native'}), public_hash=digest({'summary': 'portable'}),
            estimated_tokens=20, projection_codec_version='portable-v1', checkpoint_id='checkpoint_cp1')
        self.values[source('2')['source_key']] = {'source': {'native': '2'}, 'public': {'public': '2'}}
        self.values[checkpoint_source['source_key']] = {
            'source': {'summary': 'native'}, 'public': {'summary': 'portable'}}

        class TailFails(FakeConnector):
            def append(self, rounds, expected_revision=''):
                self.calls.append((copy.deepcopy(rounds), expected_revision))
                raise RuntimeError('tail failed')
        failed = TailFails(self.target, 'pi', 'native_2', 'pi-v1', 'other_nonce',
                           native_cwd=os.path.realpath(self.tmp.name))
        runner = SwitchRunner(self.coordinator, ReadProjector(self.store, FakeResolver(self.values)),
                              failed, checkpoints)
        with self.assertRaisesRegex(RuntimeError, 'tail failed'):
            runner.switch(self.cid, self.target, ownership_nonce='other_nonce',
                lease_id=self.old_lease['lease_id'], generation=self.old_lease['generation'],
                working_limit=150, reserve_tokens=10, projection_codec_version='portable-v1')
        state = self.coordinator.state(self.cid)
        target = state['bindings'][self.target]
        self.assertEqual(target['applied_offset'], checkpoint['covers_through_offset'])
        self.assertEqual(target['base_checkpoint_id'], checkpoint['checkpoint_id'])
        self.assertEqual(target['native_revision'], 'checkpoint-rev')
        self.assertIsNone(state['lease'])

        recovered = FakeConnector(self.target, 'pi', 'native_2', 'pi-v1', 'other_nonce',
                                  native_cwd=os.path.realpath(self.tmp.name))
        resumed = SwitchRunner(Coordinator(Store(self.tmp.name)),
            ReadProjector(Store(self.tmp.name), FakeResolver(self.values)), recovered)
        lease = resumed.resume_switch(self.cid, state['pending_switch']['switch_id'],
                                      ownership_nonce='other_nonce')
        self.assertEqual(lease['binding_id'], self.target)
        self.assertEqual([row['offset'] for row in recovered.calls[0][0]], [2])
        self.assertEqual(recovered.calls[0][1], 'checkpoint-rev')

    def test_connector_identity_mismatch_fails_before_fencing_old_producer(self):
        runner, connector = self.runner(); connector.native_session_id = 'someone_else'
        with self.assertRaises(Conflict):
            runner.switch(self.cid, self.target, ownership_nonce='other_nonce',
                          lease_id=self.old_lease['lease_id'], generation=self.old_lease['generation'])
        state = self.coordinator.state(self.cid)
        self.assertEqual(state['lease']['lease_id'], self.old_lease['lease_id'])
        self.assertIsNone(state['pending_switch'])


if __name__ == '__main__':
    unittest.main()
