"""Independent Shepherd branches from verified, settled history.

Snapshots are explicitly public history, never fabricated native execution
evidence. The new Producer has a fresh native identity and no inherited grants.
"""
import copy
import time
import uuid

from .coordinator import digest
from .projector import ReadProjector
from .source_resolver import SourceResolver
from .store import Conflict, InvalidState
from .turn_inputs import TurnInputs
from .turn_state import turn_hash
from .unified_archive import UnifiedArchive

CODEC = 'fork-snapshot-v1'


class ForkConnector:
    def __init__(self, binding, *, store_root, ownership):
        self.binding = dict(binding)
        self.engine = binding['engine']
        self.native_session_id = binding['native_session_id']
        self.ownership = ownership
        self.archive = UnifiedArchive(store_root, binding['conversation_id'])
        self.ownership.verify(binding, binding['ownership_nonce'])

    def reference(self, turn_id):
        return {'kind': 'fork_snapshot', 'engine': self.engine,
                'session_id': self.native_session_id, 'entry_id': turn_id,
                'source_key': f'{self.engine}:{self.native_session_id}:{turn_id}'}

    def resolve_source(self, reference, **_kwargs):
        self.ownership.verify(self.binding, self.binding['ownership_nonce'])
        if reference != self.reference(reference.get('entry_id')):
            raise Conflict('Shepherd fork source identity mismatch')
        source = self.archive.fork_snapshot(self.binding, reference['entry_id'])
        return {'source': source, 'public': copy.deepcopy(source['public'])}


def copy_history(service, source_id, target_id, through_seq):
    """Caller holds the source turn/switch guards; target is new and unpublished."""
    source = service.store.load_raw(source_id)
    if (not source or source.get('active_turn') or source.get('pending_switch')
            or source.get('legacy_recoveries') or not source.get('active_binding_id')):
        raise Conflict('Shepherd 会话仍在执行、切换或恢复，暂不能分支')
    if type(through_seq) is not int or not 1 <= through_seq <= len(source['turns']):
        raise InvalidState('Shepherd 分支消息边界无效')
    target = service.store.load_raw(target_id)
    if (not target or target['turns'] or target.get('active_turn')
            or not target.get('active_binding_id') or source_id == target_id):
        raise Conflict('Shepherd 分支目标必须是独立空会话')
    producer = target['bindings'][target['active_binding_id']]
    if not producer['connector_version'].startswith('unified-v1:'):
        raise Conflict('Shepherd 分支需要统一存储')
    projector = ReadProjector(service.store, SourceResolver([
        service.connector(binding) for binding in source['bindings'].values()]))
    archive = UnifiedArchive(service.root, target_id)
    bindings, turns, binding_ids = {}, [], []
    try:
        # Bounded source reads, with full hash validation before each snapshot.
        for start in range(1, through_seq + 1, ReadProjector.BATCH_TURNS):
            projected = projector.project(source_id, start,
                min(through_seq, start + ReadProjector.BATCH_TURNS - 1))
            for public_row in projected:
                old = source['turns'][public_row['seq'] - 1]
                seq = old['seq']; turn_id = f'{target_id}:{seq}'
                engine = (old.get('source_ref') or {}).get('engine') or producer['engine']
                if engine not in bindings:
                    bid = 'fbind_' + uuid.uuid4().hex
                    binding = {**copy.deepcopy(producer), 'binding_id': bid,
                        'engine': engine, 'native_session_id': str(uuid.uuid4()),
                        'ownership_nonce': uuid.uuid4().hex, 'connector_version': CODEC,
                        'status': 'retired', 'native_revision': '',
                        'imported_through_seq': 0, 'imported_turn_hash': '',
                        'base_checkpoint_id': '', 'created_at': time.time()}
                    binding['native_locator'] = binding['native_session_id']
                    service.coordinator.ownership.issue(binding)
                    binding_ids.append(bid); bindings[engine] = binding
                binding = bindings[engine]
                row = copy.deepcopy(old)
                row.update(turn_id=turn_id, producer_binding_id=binding['binding_id'])
                row.pop('input_ref', None)
                if old['history_state'] == 'available':
                    payload = {'codec': CODEC, 'identity': archive.identity(binding),
                        'turn_id': turn_id, 'origin': {
                            'conversation_id': source_id, 'turn_id': old['turn_id'],
                            'turn_hash': old['turn_hash'], 'source_hash': old['source_hash'],
                            'public_hash': old['public_hash']}, 'public': public_row['public']}
                    archive.fork_snapshot(binding, turn_id, payload=payload)
                    ref = ForkConnector(binding, store_root=service.root,
                        ownership=service.coordinator.ownership).reference(turn_id)
                    row.update(source_ref=ref, source_key=ref['source_key'],
                               source_hash=digest(payload))
                elif old.get('input_ref'):
                    row['input_ref'] = TurnInputs(service.root).write(target_id,
                        turn_id, TurnInputs(service.root).read(old['input_ref']))
                row['turn_hash'] = turn_hash(row)
                turns.append(row)
        _copy_checkpoints(service, source_id, target_id, source['turns'], turns)
        revision = turns[-1]['turn_hash']
        archive.advance(producer['binding_id'], through_seq, revision, '')
        with service.store.change(target_id) as state:
            if state['turns'] or state.get('active_turn') or state.get('pending_switch'):
                raise Conflict('Shepherd 分支目标已改变')
            state['turns'] = turns
            state['bindings'].update({b['binding_id']: b for b in bindings.values()})
            state['bindings'][producer['binding_id']].update(
                imported_through_seq=through_seq, imported_turn_hash=revision,
                native_revision=revision)
            state['audit_events'].append({'kind': 'conversation_forked',
                'source_conversation_id': source_id, 'through_seq': through_seq,
                'source_turn_hash': source['turns'][through_seq - 1]['turn_hash'],
                'at': time.time()})
        # A fork must remain usable with only its own sources.
        verifier = ReadProjector(service.store, SourceResolver([
            service.connector(b) for b in service.store.load_raw(target_id)['bindings'].values()
        ]))
        for start in range(1, through_seq + 1, ReadProjector.BATCH_TURNS):
            verifier.project(target_id, start,
                min(through_seq, start + ReadProjector.BATCH_TURNS - 1))
        return {'through_seq': through_seq, 'turn_id': turns[-1]['turn_id']}
    except Exception:
        # The adapter deletes the unpublished target including its archive.
        # Receipts issued before publication are not yet in its state manifest.
        for bid in binding_ids:
            service._rollback_binding(bid)
        raise


def _copy_checkpoints(service, source_id, target_id, original, turns):
    """Retain compression; never copy a checkpoint beyond the selected reply."""
    source = UnifiedArchive(service.root, source_id)
    if not source.path.exists():
        return
    target = UnifiedArchive(service.root, target_id)
    for saved in source.checkpoint_candidates():
        through = saved['through_seq']
        if type(through) is not int or not 1 <= through <= len(turns):
            continue
        old = original[through - 1]
        if old['outcome'] != 'completed' or old['history_state'] != 'available':
            continue
        payload = copy.deepcopy(saved['payload'])
        if (saved['turn_id'] != old['turn_id']
                or payload.get('source_hash') != old['source_hash']):
            raise Conflict('Shepherd 分支检查点来源不匹配')
        payload['source_hash'] = turns[through - 1]['source_hash']
        target.checkpoint(saved['context_key'], turn_id=turns[through - 1]['turn_id'],
                          through_seq=through, payload=payload)
