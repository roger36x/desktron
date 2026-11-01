"""Single-writer transitions for Shepherd's serial-turn state model."""
import copy
import hashlib
import json
import os
import time
import uuid

from .ownership import Ownership
from .store import Conflict, InvalidState, valid_id
from .turn_state import settle as settle_turn_row


def canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def estimate_tokens(value):
    return (len(canonical_bytes(value)) + 2) // 3


class Coordinator:
    def __init__(self, store, ownership=None):
        self.store = store
        self.ownership = ownership or Ownership(store.root + '/ownership')

    def create(self, conversation_id):
        state = self.store.load(conversation_id, create=True, mode='active')
        if state.get('mode') != 'active':
            raise Conflict('既有 Shadow Conversation 不能原地升级为 Active')
        return state

    def state(self, conversation_id):
        return self.store.load(conversation_id)

    def bind(self, conversation_id, engine, native_session_id, *, native_cwd,
             connector_version, binding_id=None, ownership_nonce=None,
             native_revision='', prompt_snapshot_policy='', prompt_snapshot_sha256=''):
        valid_id(engine, 'engine'); valid_id(native_session_id, 'native_session_id')
        valid_id(connector_version, 'connector_version')
        native_cwd = str(native_cwd or '')
        if not os.path.isabs(native_cwd):
            raise InvalidState('native_cwd 必须是绝对路径')
        binding_id = valid_id(binding_id or ('bind_' + uuid.uuid4().hex), 'binding_id')
        nonce = valid_id(ownership_nonce or uuid.uuid4().hex, 'ownership_nonce')
        with self.store.change(conversation_id, create=True, mode='active') as state:
            self._active_mode(state)
            if binding_id in state['bindings']:
                raise Conflict('Shepherd Binding 已存在')
            if any(row.get('engine') == engine and row.get('status') == 'current'
                   for row in state['bindings'].values()):
                raise Conflict('同一引擎已有当前 Binding')
            binding = {
                'binding_id':binding_id, 'conversation_id':conversation_id,
                'owner':'shepherd', 'ownership_nonce':nonce, 'engine':engine,
                'native_session_id':native_session_id, 'native_locator':native_session_id,
                'native_cwd':os.path.realpath(native_cwd),
                'connector_version':connector_version,
                'imported_through_seq':0, 'imported_turn_hash':'',
                'native_revision':str(native_revision or ''),
                'base_checkpoint_id':'', 'status':'current', 'created_at':time.time(),
            }
            if prompt_snapshot_policy:
                binding['prompt_snapshot_policy'] = valid_id(
                    prompt_snapshot_policy, 'prompt_snapshot_policy')
                if (not isinstance(prompt_snapshot_sha256, str)
                        or len(prompt_snapshot_sha256) != 64
                        or any(char not in '0123456789abcdef'
                               for char in prompt_snapshot_sha256)):
                    raise InvalidState('Claude 静态提示摘要无效')
                binding['prompt_snapshot_sha256'] = prompt_snapshot_sha256
            self.ownership.issue(binding)
            state['bindings'][binding_id] = binding
        return binding_id

    def advance(self, conversation_id, binding_id, through_offset, event_hash,
                native_revision, *, ownership_nonce, base_checkpoint_id=None):
        """Advance a Binding import cursor after verified native persistence."""
        with self.store.change(conversation_id) as state:
            binding = self._owned_binding(state, binding_id, ownership_nonce)
            current = binding['imported_through_seq']
            if not isinstance(through_offset, int) or not current <= through_offset <= len(state['turns']):
                raise Conflict('Shepherd Consumer Seq 不能倒退或越过历史边界')
            expected = '' if through_offset == 0 else state['turns'][through_offset - 1]['turn_hash']
            if event_hash != expected:
                raise Conflict('Shepherd Consumer Hash 与 Turn 不一致')
            binding.update(imported_through_seq=through_offset,
                           imported_turn_hash=event_hash,
                           native_revision=str(native_revision or ''))
            if base_checkpoint_id is not None:
                if base_checkpoint_id and base_checkpoint_id not in state['checkpoints']:
                    raise Conflict('Shepherd Checkpoint 不存在')
                binding['base_checkpoint_id'] = str(base_checkpoint_id or '')
        return through_offset

    def acquire(self, conversation_id, binding_id, *, ownership_nonce):
        with self.store.change(conversation_id) as state:
            binding = self._owned_binding(state, binding_id, ownership_nonce)
            if binding.get('status') != 'current':
                raise Conflict('已隔离或退役的 Binding 不能恢复为 Producer')
            if state.get('active_binding_id') or state.get('pending_switch') or state.get('active_turn'):
                raise Conflict('Shepherd 已有 Active Producer、执行回合或正在切换')
            if binding['imported_through_seq'] != len(state['turns']):
                raise Conflict('目标 Consumer 尚未追平')
            state['producer_generation'] += 1
            state['active_binding_id'] = binding_id
            return self._producer_token(state)

    def prepare_switch(self, conversation_id, target_binding_id, *, ownership_nonce,
                       lease_id, generation, plan=None):
        with self.store.change(conversation_id) as state:
            binding = self._owned_binding(state, target_binding_id, ownership_nonce)
            if state.get('pending_switch'):
                raise Conflict('Shepherd 已在切换中')
            if state.get('active_turn'):
                raise Conflict('Shepherd 执行中不能切换 Producer')
            producer = self._producer(state, lease_id, generation)
            if producer['binding_id'] == target_binding_id:
                raise Conflict('目标 Binding 已是 Active Consumer')
            plan = self._switch_plan(plan, binding, len(state['turns']))
            state['producer_generation'] += 1
            pending = {
                'switch_id':'switch_' + uuid.uuid4().hex,
                'source_binding_id':producer['binding_id'],
                'target_binding_id':target_binding_id,
                'through_seq':len(state['turns']),
                'generation':state['producer_generation'], 'state':'catching_up',
                'plan':plan,
                'operation':{'operation_id':'operation_' + uuid.uuid4().hex,
                             'kind':'switch_write','phase':'fenced','updated_at':time.time()},
                'created_at':time.time(),
            }
            state['active_binding_id'] = ''
            state['pending_switch'] = pending
            return self._legacy_switch(pending)

    def prepare_storage_upgrade(self, conversation_id, replacement, *, lease_id,
                                generation, plan=None):
        """Atomically replace a native Producer with an empty unified Consumer.

        A same-engine replacement cannot pass through ``bind`` because the old
        Producer must remain current until it is fenced.  Install, retire and
        fence in one state transaction so a crash exposes either the old active
        Producer or a recoverable pending switch, never two current Bindings.
        """
        replacement = self._normalize_binding(replacement)
        replacement_id = valid_id(replacement.get('binding_id'), 'binding_id')
        self.ownership.verify(replacement, replacement.get('ownership_nonce'))
        with self.store.change(conversation_id) as state:
            self._active_mode(state)
            if state.get('pending_switch') or state.get('active_turn'):
                raise Conflict('Shepherd 执行中或切换中不能升级存储')
            producer = self._producer(state, lease_id, generation)
            old = state['bindings'][producer['binding_id']]
            if (replacement_id in state['bindings']
                    or replacement.get('owner') != 'shepherd'
                    or replacement.get('conversation_id') != conversation_id
                    or replacement.get('engine') != old.get('engine')
                    or not str(replacement.get('connector_version') or '').startswith('unified-v1:')
                    or replacement.get('imported_through_seq') != 0
                    or replacement.get('imported_turn_hash')
                    or replacement.get('status') != 'current'):
                raise Conflict('Shepherd 统一存储替代 Binding 无效')
            switch_plan = self._switch_plan(plan, replacement, len(state['turns']))
            # All legacy same/different-engine Consumers become read-only
            # evidence.  Leaving an inactive native row as ``current`` would
            # let a later engine switch reuse it and silently leave unified
            # storage again.
            for row in state['bindings'].values():
                if (row.get('status') == 'current'
                        and not str(row.get('connector_version') or '').startswith('unified-v1:')):
                    self._mark_binding(row, 'retired', 'storage_upgrade_to_unified')
            state['bindings'][replacement_id] = replacement
            state['producer_generation'] += 1
            pending = {
                'switch_id':'switch_' + uuid.uuid4().hex,
                'source_binding_id':old['binding_id'],
                'target_binding_id':replacement_id,
                'through_seq':len(state['turns']),
                'generation':state['producer_generation'], 'state':'catching_up',
                'plan':switch_plan,
                'operation':{'operation_id':'operation_' + uuid.uuid4().hex,
                             'kind':'switch_write','phase':'fenced','updated_at':time.time()},
                'storage_upgrade':True, 'created_at':time.time(),
            }
            state['active_binding_id'] = ''
            state['pending_switch'] = pending
            state['audit_events'].append({
                'event_id':'audit_' + uuid.uuid4().hex,
                'kind':'storage_upgrade_fenced',
                'source_binding_id':old['binding_id'],
                'target_binding_id':replacement_id,
                'through_seq':len(state['turns']), 'at':time.time()})
            state['audit_events'] = state['audit_events'][-200:]
            return self._legacy_switch(pending)

    def note_switch_operation(self, conversation_id, switch_id, phase, *, writer=None):
        allowed = ('fenced','checkpoint_writing','checkpoint_applied',
                   'append_writing','append_applied')
        if phase not in allowed:
            raise InvalidState('Shepherd Switch Operation 阶段无效')
        with self.store.change(conversation_id) as state:
            pending = self._pending(state, switch_id)
            operation = pending.get('operation') or {
                'operation_id':'operation_' + uuid.uuid4().hex,
                'kind':'switch_write','phase':'fenced','updated_at':time.time()}
            if operation.get('kind') != 'switch_write' or not operation.get('operation_id'):
                raise Conflict('Shepherd Switch Operation 缺失')
            operation.update(phase=phase, updated_at=time.time())
            if writer is not None:
                self._validate_writer(writer)
                operation['writer'] = copy.deepcopy(writer)
            pending['operation'] = operation
            return self._legacy_switch(pending)

    def complete_switch(self, conversation_id, switch_id, *, ownership_nonce):
        with self.store.change(conversation_id) as state:
            pending = self._pending(state, switch_id)
            binding = self._owned_binding(state, pending['target_binding_id'], ownership_nonce)
            if (pending.get('operation') or {}).get('phase') != 'append_applied':
                raise Conflict('Shepherd Switch 尚未完成原生 Append 验证')
            if binding['imported_through_seq'] < pending['through_seq']:
                raise Conflict('目标 Consumer 尚未追平切换边界')
            if binding['imported_through_seq'] != len(state['turns']):
                raise Conflict('切换期间出现新的 Turn，拒绝激活目标 Consumer')
            completed = copy.deepcopy(pending)
            completed.update(state='completed', completed_at=time.time())
            completed['operation']['phase'] = 'completed'
            completed['operation']['updated_at'] = completed['completed_at']
            state['switch_history'] = (state.get('switch_history') or [])[-49:] + [completed]
            state['pending_switch'] = None
            state['active_binding_id'] = binding['binding_id']
            return self._producer_token(state)

    def cancel_switch(self, conversation_id, switch_id):
        with self.store.change(conversation_id) as state:
            self._pending(state, switch_id)
            state['pending_switch'] = None
            return state['producer_generation']

    def tombstone_for_delete(self, conversation_id):
        """Atomically retire public execution without deleting private evidence.

        Callers must hold the product turn/switch guards. Durable Writer claims
        are still verified here so a stale or bypassed caller cannot abandon an
        operation that may still be writing native history.
        """
        with self.store.change(conversation_id, allow_deleted=True) as state:
            if state.get('tombstone'):
                return copy.deepcopy(state['tombstone'])
            active = state.get('active_turn') or {}
            if active and active.get('recovery_state') != 'blocked':
                raise Conflict('Shepherd 会话仍在执行或切换，不能删除')
            if active:
                self._delete_turn_writer_stopped(active)
            for legacy in state.get('legacy_recoveries') or []:
                self._delete_turn_writer_stopped(legacy.get('inflight') or {})
            pending = state.get('pending_switch') or {}
            if pending:
                self._delete_writer_stopped(pending.get('operation') or {})
                retarget = pending.get('retarget') or {}
                retargets = list(pending.get('retarget_history') or [])
                if retarget:
                    retargets.append(retarget)
                for intent in retargets:
                    self._delete_writer_stopped(intent, allow_unstarted=True)
                archived = copy.deepcopy(pending)
                archived.update(state='abandoned_by_delete', abandoned_at=time.time())
                archived_intents = list(archived.get('retarget_history') or [])
                if archived.get('retarget'):
                    archived_intents.append(archived['retarget'])
                for intent in archived_intents:
                    archived_staged = intent.get('binding') if isinstance(intent, dict) else None
                    if isinstance(archived_staged, dict):
                        self._mark_binding(archived_staged, 'quarantined', 'staged_switch_abandoned_by_delete')
                state['switch_history'] = (state.get('switch_history') or [])[-49:] + [archived]
                target = state['bindings'].get(pending.get('target_binding_id'))
                if target and target.get('status') == 'current':
                    self._mark_binding(target, 'quarantined', 'switch_target_abandoned_by_delete')
                for intent in retargets:
                    staged = intent.get('binding') if isinstance(intent, dict) else None
                    staged_id = staged.get('binding_id') if isinstance(staged, dict) else ''
                    if staged_id:
                        if staged_id not in state['bindings']:
                            state['bindings'][staged_id] = copy.deepcopy(staged)
                        if state['bindings'][staged_id].get('status') == 'current':
                            self._mark_binding(state['bindings'][staged_id], 'quarantined', 'retarget_abandoned_by_delete')
                state['pending_switch'] = None
            # A blocked active Turn remains byte-for-byte attributable to its
            # Producer. The tombstone itself fences every future mutation, so
            # invalidating that token would only corrupt retained evidence.
            if not active:
                state['producer_generation'] += 1
                state['active_binding_id'] = ''
            deleted_at = time.time()
            audit_event_id = 'audit_' + uuid.uuid4().hex
            tombstone = {'state':'deleted', 'reason':'user_delete',
                         'deleted_at':deleted_at, 'audit_event_id':audit_event_id}
            if pending.get('switch_id'):
                tombstone['abandoned_switch_id'] = pending['switch_id']
            state['tombstone'] = tombstone
            state['audit_events'].append({
                'event_id':audit_event_id,
                'kind':'public_conversation_deleted',
                'abandoned_switch_id':pending.get('switch_id') or '',
                'at':deleted_at})
            return copy.deepcopy(tombstone)

    @staticmethod
    def _delete_turn_writer_stopped(turn):
        recovery = turn.get('recovery') or {}
        phase = recovery.get('phase') or turn.get('phase') or turn.get('state')
        process = recovery.get('process')
        # Persisted process identity always outranks the phase label. A reserved
        # Turn without identity never crossed dispatch; every later phase must
        # carry exact death evidence.
        if process:
            from .recovery import process_state
            if process_state(process) == 'dead':
                return
            raise Conflict('Shepherd 原生 Writer 尚未确认退出，不能删除')
        if phase == 'reserved':
            return
        raise Conflict('Shepherd 原生 Writer 身份缺失，不能删除')

    @staticmethod
    def _delete_writer_stopped(holder, *, allow_unstarted=False):
        phase = holder.get('phase')
        writer = holder.get('writer') or {}
        if allow_unstarted and phase == 'reserved' and not writer:
            return
        safe_phase = (not allow_unstarted
                      and phase in ('fenced','checkpoint_applied','append_applied'))
        process = writer.get('process')
        from .recovery import BOOT_ID, process_state
        # Persisted identity outranks a possibly stale status label in every
        # phase, including applied phases: PID reuse remains fail-closed.
        if process:
            if process_state(process) == 'dead':
                return
            raise Conflict('Shepherd 原生 Writer 尚未确认退出，不能删除')
        if not writer and safe_phase:
            return
        if writer.get('kind') == 'in_process':
            if writer.get('status') in ('stopped','dead') or writer.get('boot_id') != BOOT_ID:
                return
        elif writer.get('kind') == 'process' and writer.get('status') == 'stopped':
            # Process connectors record stopped without identity only when
            # spawning failed before an external Writer existed.
            return
        raise Conflict('Shepherd 原生 Writer 尚未确认退出，不能删除')

    def reserve_retarget(self, conversation_id, switch_id, engine, native_cwd, *,
                         binding_id, ownership_nonce, boot_id, selection_epoch,
                         replace_existing=False):
        valid_id(engine, 'engine'); valid_id(binding_id, 'binding_id')
        valid_id(ownership_nonce, 'ownership_nonce'); valid_id(boot_id, 'boot_id')
        if not isinstance(selection_epoch, int) or selection_epoch < 0:
            raise InvalidState('Shepherd Retarget Selection Epoch 无效')
        native_cwd = os.path.realpath(str(native_cwd or ''))
        if not os.path.isabs(native_cwd):
            raise InvalidState('Shepherd Retarget 工作目录无效')
        with self.store.change(conversation_id) as state:
            pending = self._pending(state, switch_id)
            if state.get('active_binding_id') or state.get('active_turn'):
                raise Conflict('Shepherd 有 Producer 或执行回合，不能迁移切换目标')
            old = pending.get('retarget')
            value = {'retarget_id':'retarget_' + uuid.uuid4().hex,
                     'engine':engine,'native_cwd':native_cwd,'binding_id':binding_id,
                     'ownership_nonce':ownership_nonce,'selection_epoch':selection_epoch,
                     'phase':'reserved','boot_id':boot_id,'created_at':time.time()}
            if old:
                same = all(old.get(key) == value.get(key) for key in (
                    'engine','native_cwd','binding_id','ownership_nonce','boot_id','selection_epoch'))
                if same: return copy.deepcopy(old)
                if not replace_existing:
                    raise Conflict('Shepherd 已有其他替代 Binding 意图')
                archived = copy.deepcopy(old)
                archived.update(state='abandoned', abandoned_at=time.time())
                pending.setdefault('retarget_history', []).append(archived)
                pending['retarget_history'] = pending['retarget_history'][-20:]
            pending['retarget'] = value
            return copy.deepcopy(value)

    def note_retarget_writer(self, conversation_id, switch_id, retarget_id, writer):
        self._validate_writer(writer)
        with self.store.change(conversation_id) as state:
            intent = self._pending(state, switch_id).get('retarget') or {}
            if intent.get('retarget_id') != retarget_id:
                raise Conflict('Shepherd Retarget 意图已失效')
            intent.update(writer=copy.deepcopy(writer), updated_at=time.time())
            return copy.deepcopy(intent)

    def stage_retarget(self, conversation_id, switch_id, replacement, *, retarget_id):
        replacement = self._normalize_binding(replacement)
        replacement_id = valid_id(replacement.get('binding_id'), 'binding_id')
        self.ownership.verify(replacement, replacement.get('ownership_nonce'))
        with self.store.change(conversation_id) as state:
            intent = self._pending(state, switch_id).get('retarget') or {}
            if (intent.get('retarget_id') != retarget_id
                    or intent.get('binding_id') != replacement_id
                    or intent.get('engine') != replacement.get('engine')
                    or replacement.get('conversation_id') != conversation_id
                    or replacement.get('owner') != 'shepherd'
                    or replacement.get('imported_through_seq') != 0
                    or replacement.get('status') != 'current'):
                raise Conflict('Shepherd 替代 Binding 与持久意图不匹配')
            intent.update(phase='native_created', binding=copy.deepcopy(replacement),
                          updated_at=time.time())
            return copy.deepcopy(intent)

    def retarget_switch(self, conversation_id, switch_id, *, retarget_id,
                        reason='user_selected'):
        with self.store.change(conversation_id) as state:
            pending = self._pending(state, switch_id)
            intent = pending.get('retarget') or {}
            replacement = self._normalize_binding(intent.get('binding') or {})
            replacement_id = valid_id(replacement.get('binding_id'), 'binding_id')
            if intent.get('retarget_id') != retarget_id or intent.get('phase') != 'native_created':
                raise Conflict('Shepherd 替代 Binding 尚未完成持久化')
            self.ownership.verify(replacement, replacement.get('ownership_nonce'))
            if (state.get('active_binding_id') or state.get('active_turn')
                    or replacement_id in state['bindings']
                    or replacement.get('owner') != 'shepherd'
                    or replacement.get('conversation_id') != conversation_id
                    or replacement.get('imported_through_seq') != 0
                    or replacement.get('status') != 'current'):
                raise Conflict('Shepherd 替代切换 Binding 无效')
            old_target = state['bindings'].get(pending['target_binding_id'])
            if not old_target:
                raise Conflict('Shepherd 原切换目标不存在')
            archived = copy.deepcopy(pending)
            archived.update(state='retargeted', completed_at=time.time(),
                            retarget_reason=str(reason or '')[:80],
                            replacement_binding_id=replacement_id)
            archived.pop('retarget', None)
            state['switch_history'] = (state.get('switch_history') or [])[-49:] + [archived]
            self._mark_binding(old_target, 'retargeted', 'switch_retargeted')
            for row in state['bindings'].values():
                if row.get('engine') == replacement.get('engine') and row.get('status') == 'current':
                    self._mark_binding(row, 'retired', 'superseded_by_retarget')
            state['bindings'][replacement_id] = replacement
            state['producer_generation'] += 1
            new_pending = {
                'switch_id':'switch_' + uuid.uuid4().hex,
                'source_binding_id':pending['source_binding_id'],
                'target_binding_id':replacement_id,
                'through_seq':len(state['turns']),
                'generation':state['producer_generation'],'state':'catching_up',
                'plan':self._switch_plan(None, replacement, len(state['turns'])),
                'operation':{'operation_id':'operation_' + uuid.uuid4().hex,
                             'kind':'switch_write','phase':'fenced','updated_at':time.time()},
                'retargeted_from_switch_id':switch_id,'created_at':time.time(),
            }
            state['pending_switch'] = new_pending
            return self._legacy_switch(new_pending)

    def revoke(self, conversation_id, lease_id, generation):
        with self.store.change(conversation_id) as state:
            self._producer(state, lease_id, generation)
            if state.get('active_turn'):
                raise Conflict('Shepherd 执行中不能撤销 Producer')
            state['producer_generation'] += 1
            state['active_binding_id'] = ''
            return state['producer_generation']

    def begin_round(self, conversation_id, lease_id, generation, *, kind='user_turn',
                    attempt_id=None, recovery=None, input_ref=None):
        if kind not in ('user_turn','continuation'):
            raise InvalidState('Shepherd Round kind 无效')
        attempt_id = valid_id(attempt_id or ('attempt_' + uuid.uuid4().hex), 'attempt_id')
        with self.store.change(conversation_id) as state:
            producer = self._producer(state, lease_id, generation)
            if state.get('active_turn'):
                raise Conflict('Shepherd 已有未决执行回合')
            seq = len(state['turns']) + 1
            turn = {'attempt_id':attempt_id,'seq':seq,'turn_id':f'{conversation_id}:{seq}',
                    'kind':kind,'producer_binding_id':producer['binding_id'],
                    'producer_generation':generation,'phase':'reserved',
                    'outcome':'unknown','history_state':'pending',
                    'unfinished_tool_ids':[],'started_at':time.time()}
            if input_ref is not None:
                turn['input_ref'] = copy.deepcopy(input_ref)
            if recovery is not None:
                if (not isinstance(recovery, dict) or recovery.get('phase') != 'reserved'
                        or not recovery.get('boot_id')
                        or not isinstance(recovery.get('boundary'), dict)):
                    raise InvalidState('Shepherd Recovery 起点无效')
                turn['recovery'] = copy.deepcopy(recovery)
            state['active_turn'] = turn
            state['active_turn_id'] = turn['turn_id']
            return self._legacy_active(turn)

    @staticmethod
    def _mark_binding(binding, status, reason, *, attempt_id='', detail=''):
        """Set a Binding status together with why it happened.

        Status alone cannot be attributed after the fact: a quarantined or
        retired Binding used to record no reason at all, so recurring failures
        could not be grouped or counted. Only annotates - the decision to
        quarantine or retire is made by the caller, unchanged.
        """
        if not isinstance(binding, dict):
            return binding
        binding['status'] = status
        binding['status_reason'] = str(reason or 'unspecified')[:120]
        binding['status_at'] = time.time()
        if attempt_id:
            binding['status_attempt_id'] = str(attempt_id)[:120]
        if detail:
            binding['status_detail'] = str(detail)[:240]
        return binding

    @staticmethod
    def _undispatched(turn):
        """True when no dispatch trace exists, so cancelling runs no tool twice.

        mark_dispatched moves turn.phase and recovery.phase together in one
        state transaction and records the process/locator there, so an absent
        recovery block cannot hide a live Writer.  Older or migrated turns may
        carry no recovery block at all; requiring one stranded them as
        permanently uncancellable reserved rounds.
        """
        recovery = turn.get('recovery') or {}
        return (turn.get('phase') == 'reserved'
                and recovery.get('phase') in (None, '', 'reserved')
                and not recovery.get('process')
                and not recovery.get('native_locator'))

    def cancel_reserved(self, conversation_id, *, attempt_id):
        with self.store.change(conversation_id) as state:
            turn = state.get('active_turn') or {}
            if turn.get('attempt_id') != attempt_id or not self._undispatched(turn):
                raise Conflict('Shepherd 回合不能按派发前取消')
            state['audit_events'].append({
                'event_id':'audit_' + uuid.uuid4().hex,'kind':'cancel_before_dispatch',
                'action':'cancel_before_dispatch','binding_id':turn['producer_binding_id'],
                'attempt_id':attempt_id,'legacy_inflight':self._legacy_active(turn),
                'reason':('reserved_without_recovery' if not (turn.get('recovery') or {})
                          else 'reserved'),
                'at':time.time()})
            state['active_turn'] = None; state['active_turn_id'] = ''
            return len(state['turns'])

    def attach_input_ref(self, conversation_id, *, attempt_id, input_ref):
        """Attach an immutable public-input reference to a migrated active turn."""
        with self.store.change(conversation_id) as state:
            turn = self._active(state, attempt_id)
            if turn.get('input_ref'):
                if turn['input_ref'] != input_ref:
                    raise Conflict('Shepherd Turn Input 引用冲突')
                return copy.deepcopy(turn['input_ref'])
            turn['input_ref'] = copy.deepcopy(input_ref)
            return copy.deepcopy(turn['input_ref'])

    def mark_dispatched(self, conversation_id, lease_id, generation, *, attempt_id,
                        process=None, native_locator=None):
        with self.store.change(conversation_id) as state:
            self._producer(state, lease_id, generation)
            turn = self._active(state, attempt_id)
            recovery = turn.get('recovery') or {}
            if turn.get('phase') not in ('reserved','dispatched') or recovery.get('phase') not in ('reserved','dispatched'):
                raise Conflict('Shepherd Inflight Recovery 尚未预留')
            turn['phase'] = 'dispatched'; recovery['phase'] = 'dispatched'
            recovery['dispatched_at'] = recovery.get('dispatched_at') or time.time()
            if process:
                if not isinstance(process, dict): raise InvalidState('Shepherd 原生进程身份无效')
                recovery['process'] = copy.deepcopy(process)
            if native_locator:
                if not isinstance(native_locator, dict): raise InvalidState('Shepherd 原生执行定位无效')
                recovery['native_locator'] = copy.deepcopy(native_locator)
            turn['recovery'] = recovery
            return self._legacy_active(turn)

    def note_native_locator(self, conversation_id, lease_id, generation, *, attempt_id,
                            native_locator):
        with self.store.change(conversation_id) as state:
            self._producer(state, lease_id, generation)
            turn = self._active(state, attempt_id); recovery = turn.get('recovery') or {}
            if (turn.get('phase') != 'dispatched' or recovery.get('phase') != 'dispatched'
                    or not isinstance(native_locator, dict)):
                raise Conflict('Shepherd 原生执行定位不能更新')
            recovery['native_locator'] = copy.deepcopy(native_locator)
            return self._legacy_active(turn)

    def mark_unresolved(self, conversation_id, lease_id, generation, *, attempt_id,
                        reason='', detail=''):
        with self.store.change(conversation_id) as state:
            self._producer(state, lease_id, generation)
            turn = self._active(state, attempt_id)
            if turn.get('recovery_state') != 'source_staged':
                turn['recovery_state'] = 'blocked'; turn['failed_at'] = time.time()
                if reason: turn['recovery_reason'] = str(reason)[:120]
                if detail: turn['recovery_detail'] = str(detail)[:480]

    def block_staged_source(self, conversation_id, lease_id, generation, *, attempt_id,
                            reason='commit_verification_failed', detail=''):
        """Expose an explicit isolation exit without mutating terminal evidence."""
        with self.store.change(conversation_id) as state:
            self._producer(state, lease_id, generation)
            turn = self._active(state, attempt_id)
            if turn.get('recovery_state') == 'blocked':
                return self._legacy_active(turn)
            if turn.get('recovery_state') != 'source_staged':
                raise Conflict('Shepherd 没有待阻断的持久来源')
            turn['recovery_state'] = 'blocked'; turn['failed_at'] = time.time()
            turn['recovery_reason'] = str(reason or 'commit_verification_failed')[:120]
            if detail: turn['recovery_detail'] = str(detail)[:480]
            return self._legacy_active(turn)

    def note_staged_failure(self, conversation_id, lease_id, generation, *, attempt_id,
                            max_retries, reason='commit_verification_failed', detail=''):
        """Persist commit-only retry count so restart cannot reset the budget."""
        with self.store.change(conversation_id) as state:
            self._producer(state, lease_id, generation)
            turn = self._active(state, attempt_id)
            if turn.get('recovery_state') == 'blocked':
                return {'blocked':True, 'attempts':int(turn.get('recovery_attempts') or 0)}
            if turn.get('recovery_state') != 'source_staged':
                raise Conflict('Shepherd 没有待重试的持久来源')
            attempts = int(turn.get('recovery_attempts') or 0) + 1
            turn['recovery_attempts'] = attempts
            turn['recovery_reason'] = str(reason or 'commit_verification_failed')[:120]
            if detail: turn['recovery_detail'] = str(detail)[:480]
            blocked = attempts > max(0, int(max_retries))
            if blocked:
                turn['recovery_state'] = 'blocked'; turn['failed_at'] = time.time()
            return {'blocked':blocked, 'attempts':attempts}

    def omit_legacy_recovery(self, conversation_id, recovery_id, *, input_ref=None,
                             reason='native_source_unavailable'):
        """Insert an explicit omitted boundary for an undecodable v1 gap."""
        with self.store.change(conversation_id) as state:
            item = next((row for row in state.get('legacy_recoveries', [])
                         if row.get('recovery_id') == recovery_id), None)
            if not item:
                raise Conflict('Shepherd Legacy Recovery 不存在')
            insert = item['insert_seq'] - 1
            active = {'turn_id':item['turn_id'],'seq':item['insert_seq'],
                      'kind':(item.get('inflight') or {}).get('kind','user_turn'),
                      'attempt_id':(item.get('inflight') or {}).get('attempt_id',''),
                      'producer_binding_id':item['source_binding_id'],
                      'producer_generation':int((item.get('inflight') or {}).get('generation') or 0),
                      'phase':'dispatched','outcome':'unknown','history_state':'pending',
                      'unfinished_tool_ids':[],'started_at':(item.get('inflight') or {}).get('started_at',0)}
            if input_ref is not None:
                active['input_ref'] = copy.deepcopy(input_ref)
            settled = settle_turn_row(active, outcome='unknown', history_state='omitted',
                                      recovery_reason=reason)
            settled['estimated_tokens'] = 0
            settled['legacy_recovery_id'] = recovery_id
            from .turn_state import turn_hash
            state['turns'].insert(insert, settled)
            for seq, row in enumerate(state['turns'], 1):
                row['seq'] = seq; row['turn_hash'] = turn_hash(row)
            for binding in state['bindings'].values():
                binding.update(imported_through_seq=0, imported_turn_hash='',
                               base_checkpoint_id='')
                self._mark_binding(binding, 'retired', 'legacy_recovery_import')
            resume_id = item.get('resume_binding_id')
            if resume_id in state['bindings']:
                self._mark_binding(state['bindings'][resume_id], 'recovery_required',
                                   'legacy_recovery_resume')
            state['checkpoints'] = {}
            state['legacy_recoveries'] = [row for row in state['legacy_recoveries']
                                          if row.get('recovery_id') != recovery_id]
            state['audit_events'].append({'event_id':'audit_' + uuid.uuid4().hex,
                'kind':'legacy_incomplete_turn_omitted','recovery_id':recovery_id,
                'turn_id':settled['turn_id'],'at':time.time()})
            return self._legacy_turn(settled)

    def settle_legacy_recovery(self, conversation_id, recovery_id, *, source,
                               source_hash, public_hash, unfinished_tool_ids=(),
                               native_revision='', input_ref=None, estimated_tokens=0):
        """Insert one v1 quarantined turn at its original chronological slot."""
        source = self._source(source)
        with self.store.change(conversation_id) as state:
            item = next((row for row in state.get('legacy_recoveries', [])
                         if row.get('recovery_id') == recovery_id), None)
            if not item:
                existing = next((row for row in state['turns']
                                 if row.get('legacy_recovery_id') == recovery_id), None)
                if existing:
                    return self._legacy_turn(existing)
                raise Conflict('Shepherd Legacy Recovery 不存在')
            insert = item['insert_seq'] - 1
            active = {'turn_id':item['turn_id'], 'seq':item['insert_seq'],
                      'kind':(item.get('inflight') or {}).get('kind','user_turn'),
                      'attempt_id':(item.get('inflight') or {}).get('attempt_id',''),
                      'producer_binding_id':item['source_binding_id'],
                      'producer_generation':int((item.get('inflight') or {}).get('generation') or 0),
                      'phase':'dispatched','outcome':'unknown','history_state':'pending',
                      'unfinished_tool_ids':[],'started_at':(item.get('inflight') or {}).get('started_at',0)}
            if input_ref is not None:
                active['input_ref'] = copy.deepcopy(input_ref)
            settled = settle_turn_row(active, source_ref=source,
                source_hash=valid_hash(source_hash,'source_hash'),
                public_hash=valid_hash(public_hash,'public_hash'), outcome='unknown',
                unfinished_tool_ids=unfinished_tool_ids,
                native_revision=native_revision,
                recovery_reason='migrated_preserve_and_end')
            settled['estimated_tokens'] = self._tokens(estimated_tokens)
            settled['legacy_recovery_id'] = recovery_id
            # Insertion changes seq-derived import identities. Keep stable
            # turn_id/source_ref, recompute only Turn metadata fingerprints.
            from .turn_state import turn_hash
            state['turns'].insert(insert, settled)
            for seq, row in enumerate(state['turns'], 1):
                row['seq'] = seq
                row['turn_hash'] = turn_hash(row)
            for binding in state['bindings'].values():
                binding.update(imported_through_seq=0, imported_turn_hash='',
                               base_checkpoint_id='')
                self._mark_binding(binding, 'retired', 'legacy_recovery_import')
            resume_id = item.get('resume_binding_id')
            if resume_id in state['bindings']:
                self._mark_binding(state['bindings'][resume_id], 'recovery_required',
                                   'legacy_recovery_resume')
            state['checkpoints'] = {}
            state['legacy_recoveries'] = [row for row in state['legacy_recoveries']
                                          if row.get('recovery_id') != recovery_id]
            state['audit_events'].append({'event_id':'audit_' + uuid.uuid4().hex,
                'kind':'legacy_incomplete_turn_recovered','recovery_id':recovery_id,
                'turn_id':settled['turn_id'],'unfinished_tool_ids':list(unfinished_tool_ids),
                'at':time.time()})
            return self._legacy_turn(settled)

    def prepare_recovery_binding(self, conversation_id, source_binding_id, replacement):
        """Install a clean same-engine Binding after an incomplete Turn settled."""
        replacement = self._normalize_binding(replacement)
        replacement_id = valid_id(replacement.get('binding_id'), 'binding_id')
        self.ownership.verify(replacement, replacement.get('ownership_nonce'))
        with self.store.change(conversation_id) as state:
            old = state['bindings'].get(source_binding_id)
            if (not old or old.get('status') != 'recovery_required'
                    or state.get('active_binding_id') or state.get('active_turn')
                    or state.get('pending_switch') or replacement_id in state['bindings']
                    or replacement.get('owner') != 'shepherd'
                    or replacement.get('conversation_id') != conversation_id
                    or replacement.get('engine') != old.get('engine')
                    or replacement.get('imported_through_seq') != 0
                    or replacement.get('status') != 'current'):
                raise Conflict('Shepherd Recovery Binding 无效')
            self._mark_binding(old, 'quarantined', 'recovery_rebind')
            state['bindings'][replacement_id] = replacement
            pending = {'switch_id':'switch_' + uuid.uuid4().hex,
                       'source_binding_id':source_binding_id,
                       'target_binding_id':replacement_id,
                       'through_seq':len(state['turns']),
                       'generation':state['producer_generation'],
                       'state':'catching_up',
                       'plan':self._switch_plan(None,replacement,len(state['turns'])),
                       'operation':{'operation_id':'operation_' + uuid.uuid4().hex,
                                    'kind':'switch_write','phase':'fenced','updated_at':time.time()},
                       'recovery_turn_id':state['turns'][-1]['turn_id'],
                       'created_at':time.time()}
            state['pending_switch'] = pending
            return self._legacy_switch(pending)

    def begin_reconciliation(self, conversation_id, replacement, *, attempt_id):
        """Legacy explicit isolation: settle an omitted boundary, then rebind."""
        replacement = self._normalize_binding(replacement)
        replacement_id = valid_id(replacement.get('binding_id'), 'binding_id')
        self.ownership.verify(replacement, replacement.get('ownership_nonce'))
        with self.store.change(conversation_id) as state:
            turn = self._active(state, attempt_id)
            if turn.get('recovery_state') != 'blocked':
                raise Conflict('Shepherd 没有匹配的待核验回合')
            # Isolation changes the Producer, so the same exact Writer fence as
            # deletion must hold inside the state transaction. UI-side stopping
            # alone cannot protect direct service/Coordinator callers.
            recovery = turn.get('recovery') or {}
            process = recovery.get('process')
            if process:
                from .recovery import process_state
                if process_state(process) != 'dead':
                    raise Conflict('Shepherd 原生 Writer 尚未确认退出，不能隔离')
            elif recovery.get('phase') != 'reserved':
                raise Conflict('Shepherd 原生 Writer 身份缺失，不能隔离')
            old = state['bindings'][turn['producer_binding_id']]
            if (replacement_id in state['bindings'] or replacement.get('owner') != 'shepherd'
                    or replacement.get('conversation_id') != conversation_id
                    or replacement.get('engine') != old.get('engine')
                    or replacement.get('imported_through_seq') != 0
                    or replacement.get('status') != 'current'):
                raise Conflict('Shepherd Reconciliation Binding 无效')
            settled = settle_turn_row(turn, outcome='unknown', history_state='omitted',
                                      recovery_reason=turn.get('recovery_reason') or 'source_unavailable')
            state['turns'].append(settled)
            audit = {'event_id':'reconcile_' + uuid.uuid4().hex,'kind':'omit_and_rebind',
                     'action':'preserve_and_end','binding_id':old['binding_id'],
                     'replacement_binding_id':replacement_id,'attempt_id':attempt_id,
                     'legacy_inflight':self._legacy_active(turn),'at':time.time()}
            state['audit_events'].append(audit)
            self._mark_binding(old, 'quarantined', turn.get('recovery_reason') or 'source_unavailable',
                               attempt_id=attempt_id)
            state['bindings'][replacement_id] = replacement
            state['producer_generation'] += 1
            pending = {'switch_id':'switch_' + uuid.uuid4().hex,
                       'source_binding_id':old['binding_id'],'target_binding_id':replacement_id,
                       'through_seq':len(state['turns']),'generation':state['producer_generation'],
                       'state':'catching_up','plan':self._switch_plan(None,replacement,len(state['turns'])),
                       'operation':{'operation_id':'operation_' + uuid.uuid4().hex,
                                    'kind':'switch_write','phase':'fenced','updated_at':time.time()},
                       'reconciliation_id':audit['event_id'],'created_at':time.time()}
            state['active_binding_id'] = ''; state['active_turn'] = None
            state['active_turn_id'] = ''; state['pending_switch'] = pending
            return self._legacy_switch(pending)

    def discard_unactivated_binding(self, conversation_id, binding_id):
        with self.store.change(conversation_id) as state:
            binding = state['bindings'].get(binding_id)
            pending = state.get('pending_switch') or {}
            if (not binding or binding.get('imported_through_seq') != 0
                    or state.get('active_binding_id') == binding_id
                    or binding_id in (pending.get('source_binding_id'),pending.get('target_binding_id'))
                    or any(row.get('producer_binding_id') == binding_id for row in state['turns'])):
                raise Conflict('Shepherd Binding 已被使用，不能回滚')
            del state['bindings'][binding_id]

    def source_persisted(self, conversation_id, lease_id, generation, *, attempt_id,
                         source, native_revision='', terminal_state='completed'):
        source = self._source(source)
        outcome = self._outcome(terminal_state)
        with self.store.change(conversation_id) as state:
            self._producer(state, lease_id, generation)
            turn = self._active(state, attempt_id)
            if (turn.get('producer_binding_id') != state.get('active_binding_id')
                    or turn.get('producer_generation') != generation):
                raise Conflict('Shepherd Inflight Round 已失效')
            turn.update(recovery_state='source_staged', staged_source_ref=source,
                        staged_native_revision=str(native_revision or ''),
                        staged_outcome=outcome, source_staged_at=time.time())
            return self._legacy_active(turn)

    def commit(self, conversation_id, lease_id, generation, *, source, source_hash,
               public_hash, terminal_state='completed', kind='user_turn', attempt_id='',
               estimated_tokens=0, compatibility=()):
        """Compatibility API: settle one serial turn after verified source readback."""
        source = self._source(source); source_key = source['source_key']
        outcome = self._outcome(terminal_state)
        compatibility = self._safe_compatibility(compatibility)
        with self.store.change(conversation_id) as state:
            producer = self._producer(state, lease_id, generation)
            existing = next((row for row in state['turns']
                             if row.get('source_key') == source_key), None)
            if existing:
                if (existing.get('source_hash') == source_hash
                        and existing.get('public_hash') == public_hash
                        and existing.get('outcome') == outcome
                        and existing.get('estimated_tokens') == estimated_tokens
                        and existing.get('attempt_id','') == str(attempt_id or '')):
                    out = self._legacy_turn(existing)
                    if compatibility: out['compatibility'] = copy.deepcopy(compatibility)
                    return out
                raise Conflict('同一 Shepherd 来源内容或收口语义发生变化')
            active = state.get('active_turn')
            if active:
                if (active.get('attempt_id') != str(attempt_id or '')
                        or active.get('recovery_state') != 'source_staged'
                        or active.get('staged_source_ref') != source
                        or active.get('staged_outcome') != outcome
                        or active.get('kind') != kind):
                    raise Conflict('Shepherd 收口与 Active Turn 不一致')
            else:
                seq = len(state['turns']) + 1
                active = {'turn_id':f'{conversation_id}:{seq}','seq':seq,'kind':kind,
                          'attempt_id':str(attempt_id or ''),
                          'producer_binding_id':producer['binding_id'],
                          'producer_generation':generation,'phase':'dispatched',
                          'outcome':'unknown','history_state':'pending',
                          'unfinished_tool_ids':[],'started_at':time.time()}
            settled = settle_turn_row(active, source_ref=source,
                source_hash=valid_hash(source_hash,'source_hash'),
                public_hash=valid_hash(public_hash,'public_hash'), outcome=outcome,
                native_revision=str(active.get('staged_native_revision') or ''),
                settled_at=time.time())
            settled['estimated_tokens'] = self._tokens(estimated_tokens)
            # estimated_tokens is part of the stable turn fingerprint.
            from .turn_state import turn_hash
            settled['turn_hash'] = turn_hash(settled)
            state['turns'].append(settled)
            binding = state['bindings'][producer['binding_id']]
            binding.update(imported_through_seq=settled['seq'],
                           imported_turn_hash=settled['turn_hash'])
            if settled.get('native_revision'):
                binding['native_revision'] = settled['native_revision']
            if compatibility:
                state['audit_events'].append({'event_id':'audit_' + uuid.uuid4().hex,
                    'kind':'source_compatibility','turn_id':settled['turn_id'],
                    'diagnostics':copy.deepcopy(compatibility),'at':time.time()})
                state['audit_events'] = state['audit_events'][-200:]
            state['active_turn'] = None; state['active_turn_id'] = ''
            out = self._legacy_turn(settled)
            if compatibility: out['compatibility'] = copy.deepcopy(compatibility)
            return out

    def settle_recovered(self, conversation_id, lease_id, generation, *, attempt_id,
                         source, source_hash, public_hash, unfinished_tool_ids=(),
                         native_revision='', recovery_reason='native_terminal_unconfirmed',
                         estimated_tokens=0):
        """Settle verified incomplete content as available unknown history."""
        source = self._source(source)
        with self.store.change(conversation_id) as state:
            producer = self._producer(state, lease_id, generation)
            active = self._active(state, attempt_id)
            settled = settle_turn_row(active, source_ref=source,
                source_hash=valid_hash(source_hash,'source_hash'),
                public_hash=valid_hash(public_hash,'public_hash'), outcome='unknown',
                unfinished_tool_ids=unfinished_tool_ids,
                native_revision=native_revision, recovery_reason=recovery_reason)
            settled['estimated_tokens'] = self._tokens(estimated_tokens)
            from .turn_state import turn_hash
            settled['turn_hash'] = turn_hash(settled)
            state['turns'].append(settled)
            binding = state['bindings'][producer['binding_id']]
            binding.update(imported_through_seq=settled['seq'],
                           imported_turn_hash=settled['turn_hash'],
                           native_revision=str(native_revision or binding.get('native_revision') or ''),
                           status='recovery_required')
            # The old native thread contains an uncertain execution tail. Keep
            # its evidence, but make every Producer token stale before recovery
            # creates a clean same-engine Binding.
            state['producer_generation'] += 1
            state['active_binding_id'] = ''
            state['audit_events'].append({'event_id':'audit_' + uuid.uuid4().hex,
                'kind':'incomplete_turn_settled','attempt_id':attempt_id,
                'turn_id':settled['turn_id'],'unfinished_tool_ids':list(unfinished_tool_ids),
                'at':time.time()})
            state['active_turn'] = None; state['active_turn_id'] = ''
            return self._legacy_turn(settled)

    @staticmethod
    def _safe_compatibility(diagnostics):
        """Validate metadata-only compatibility evidence; never retain payloads."""
        if diagnostics in (None, (), []):
            return []
        if not isinstance(diagnostics, (list, tuple)):
            raise InvalidState('Shepherd 兼容诊断格式无效')
        safe = []
        for item in diagnostics[:16]:
            if not isinstance(item, dict) or set(item) != {'engine','action','kind','count'}:
                raise InvalidState('Shepherd 兼容诊断条目无效')
            engine, action, kind = (str(item.get(key) or '') for key in ('engine','action','kind'))
            count = item.get('count')
            if (not 0 < len(engine) <= 32 or not 0 < len(action) <= 64
                    or not 0 < len(kind) <= 96 or not isinstance(count, int)
                    or isinstance(count, bool) or not 0 < count <= 1000000):
                raise InvalidState('Shepherd 兼容诊断条目无效')
            safe.append({'engine':engine,'action':action,'kind':kind,'count':count})
        return safe

    @staticmethod
    def _switch_plan(plan, binding, through_seq):
        if plan is None:
            plan = {'strategy':'incremental',
                    'from_seq':binding['imported_through_seq'] + 1,
                    'through_seq':through_seq}
        else:
            plan = copy.deepcopy(plan)
            for old,new in (('from_offset','from_seq'),('through_offset','through_seq'),
                            ('target_start_offset','target_start_seq'),
                            ('target_start_hash','target_start_turn_hash')):
                if old in plan and new not in plan: plan[new] = plan.pop(old)
            checkpoint = plan.get('checkpoint')
            if isinstance(checkpoint, dict) and 'covers_through_offset' in checkpoint:
                checkpoint = copy.deepcopy(checkpoint)
                checkpoint['covers_through_seq'] = checkpoint.pop('covers_through_offset')
                checkpoint['covers_turn_hash'] = checkpoint.pop('covers_hash','')
                plan['checkpoint'] = checkpoint
        if (not isinstance(plan,dict) or plan.get('strategy') not in ('incremental','checkpoint')
                or not isinstance(plan.get('from_seq'),int)
                or not isinstance(plan.get('through_seq'),int)
                or plan['through_seq'] != through_seq
                or not 1 <= plan['from_seq'] <= through_seq + 1):
            raise InvalidState('Shepherd Switch Plan 范围无效')
        plan['target_start_seq'] = binding['imported_through_seq']
        plan['target_start_turn_hash'] = binding.get('imported_turn_hash','')
        plan['target_start_revision'] = str(binding.get('native_revision') or '')
        return plan

    @staticmethod
    def _legacy_switch(pending):
        out = copy.deepcopy(pending)
        out['through_offset'] = out.get('through_seq',0)
        plan = out.get('plan')
        if isinstance(plan,dict):
            plan['from_offset'] = plan.get('from_seq',1)
            plan['through_offset'] = plan.get('through_seq',out['through_offset'])
            plan['target_start_offset'] = plan.get('target_start_seq',0)
            plan['target_start_hash'] = plan.get('target_start_turn_hash','')
        return out

    @staticmethod
    def _legacy_active(turn):
        out = copy.deepcopy(turn)
        out.update(offset=turn['seq'],round_id=turn['turn_id'],
                   binding_id=turn['producer_binding_id'],
                   generation=turn['producer_generation'])
        out['state'] = ('source_persisted' if turn.get('recovery_state') == 'source_staged'
                        else 'needs_reconciliation' if turn.get('recovery_state') == 'blocked'
                        else 'running')
        if turn.get('staged_source_ref'):
            out['source'] = copy.deepcopy(turn['staged_source_ref'])
            out['native_revision'] = turn.get('staged_native_revision','')
            out['terminal_state'] = Coordinator._legacy_terminal(turn.get('staged_outcome','unknown'))
        if turn.get('recovery_reason'):
            out['reconciliation_reason'] = turn['recovery_reason']
        if turn.get('recovery_detail'):
            out['reconciliation_detail'] = turn['recovery_detail']
        return out

    @staticmethod
    def _legacy_turn(row):
        out = copy.deepcopy(row)
        out.update(offset=row['seq'],round_id=row['turn_id'],entry_hash=row['turn_hash'],
                   terminal_state=Coordinator._legacy_terminal(row['outcome']),
                   committed_at=row.get('settled_at',0))
        if row.get('source_ref') is not None:
            out['source'] = copy.deepcopy(row['source_ref'])
        return out

    @staticmethod
    def _legacy_terminal(outcome):
        return {'completed':'completed','failed':'failed_recoverable',
                'interrupted':'interrupted_confirmed','unknown':'unknown'}[outcome]

    @staticmethod
    def _outcome(terminal):
        try:
            return {'completed':'completed','failed_recoverable':'failed',
                    'interrupted_confirmed':'interrupted','unknown':'unknown'}[terminal]
        except KeyError:
            raise InvalidState('Shepherd 来源终态无效') from None

    @staticmethod
    def _tokens(value):
        if not isinstance(value,int) or value < 0:
            raise InvalidState('Shepherd Token 估算无效')
        return value

    @staticmethod
    def _source(source):
        if not isinstance(source,dict): raise InvalidState('Shepherd 来源引用无效')
        allowed = {'kind','engine','session_id','entry_id','source_key'}
        if set(source) - allowed: raise InvalidState('Shepherd 来源引用含正文或未知字段')
        return {key:valid_id(source.get(key),'source.'+key) for key in allowed}

    @staticmethod
    def _normalize_binding(binding):
        out = copy.deepcopy(binding)
        if 'imported_through_seq' not in out:
            out['imported_through_seq'] = int(out.pop('applied_offset',0) or 0)
        else: out.pop('applied_offset',None)
        if 'imported_turn_hash' not in out:
            out['imported_turn_hash'] = str(out.pop('applied_event_hash','') or '')
        else: out.pop('applied_event_hash',None)
        return out

    @staticmethod
    def _validate_writer(writer):
        if (not isinstance(writer,dict) or writer.get('kind') not in ('in_process','process')
                or writer.get('status') not in ('starting','active','stopped','dead','unknown')
                or not isinstance(writer.get('boot_id'),str)):
            raise InvalidState('Shepherd Writer 无效')

    @staticmethod
    def _active_mode(state):
        if state.get('mode') != 'active':
            raise Conflict('Shadow Conversation 不能生产或推进 Consumer')

    def _owned_binding(self,state,binding_id,ownership_nonce):
        binding = state['bindings'].get(binding_id)
        if (not binding or binding.get('owner') != 'shepherd'
                or binding.get('ownership_nonce') != ownership_nonce
                or binding.get('conversation_id') != state['conversation_id']):
            raise Conflict('Shepherd Binding 所有权校验失败')
        self.ownership.verify(binding,ownership_nonce)
        return binding

    @staticmethod
    def _pending(state,switch_id):
        pending = state.get('pending_switch')
        if not pending or pending.get('switch_id') != switch_id:
            raise Conflict('Shepherd Switch 已失效')
        return pending

    @staticmethod
    def _active(state,attempt_id):
        turn = state.get('active_turn') or {}
        if turn.get('attempt_id') != attempt_id:
            raise Conflict('Shepherd Active Turn 已失效')
        return turn

    def _producer(self,state,token,generation):
        binding_id = state.get('active_binding_id')
        if not binding_id or token != binding_id or generation != state.get('producer_generation'):
            raise Conflict('Shepherd Producer Generation 已失效')
        binding = state['bindings'].get(binding_id)
        if not binding: raise Conflict('Shepherd Producer Binding 不存在')
        self.ownership.verify(binding,binding.get('ownership_nonce'))
        return self._producer_token(state)

    @staticmethod
    def _producer_token(state):
        binding_id = state.get('active_binding_id')
        return ({'lease_id':binding_id,'binding_id':binding_id,
                 'generation':state['producer_generation']} if binding_id else None)


def valid_hash(value,label):
    if (not isinstance(value,str) or len(value) != 64
            or any(c not in '0123456789abcdef' for c in value)):
        raise InvalidState(label + ' 无效')
    return value
