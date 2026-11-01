"""Bounded catch-up orchestration over engine-specific connectors."""
import time

from .recovery import BOOT_ID, process_identity, process_state
from .store import Conflict, InvalidState


class SwitchRunner:
    def __init__(self, coordinator, projector, connector, checkpoints=None):
        self.coordinator = coordinator
        self.projector = projector
        self.connector = connector
        self.checkpoints = checkpoints

    def switch(self, conversation_id, target_binding_id, *, ownership_nonce,
               lease_id, generation, working_limit=None, reserve_tokens=0,
               current_tokens=0, projection_codec_version=None, cancelled=None):
        state = self.coordinator.state(conversation_id)
        binding = (state.get('bindings') or {}).get(target_binding_id)
        self._connector_matches(binding, ownership_nonce)
        plan = self._plan(conversation_id, binding, working_limit, reserve_tokens,
                          current_tokens, projection_codec_version)
        if plan['strategy'] == 'blocked':
            raise Conflict(plan['reason'])
        if plan['strategy'] == 'checkpoint' and binding['imported_through_seq'] != 0:
            raise Conflict('Portable Checkpoint 只能载入新的空白 Binding')
        if plan['strategy'] == 'checkpoint' and not callable(getattr(self.connector, 'load_checkpoint', None)):
            raise Conflict('目标 Connector 不支持 Portable Checkpoint')
        # Resolve and verify every source before fencing the old producer. Native
        # writes still happen only after the fence.
        checkpoint = (self.projector.checkpoint(plan['checkpoint'])
                      if plan['strategy'] == 'checkpoint' else None)
        start, end = plan['from_seq'], plan['through_seq']
        rounds = (self.projector.project(conversation_id, start, end, cancelled=cancelled)
                  if start <= end else [])
        self._preflight(rounds, checkpoint=checkpoint, checkpoint_meta=plan.get('checkpoint'))
        if callable(cancelled) and cancelled():
            raise Conflict('Shepherd 引擎准备已被更新的选择取消')
        pending = self.coordinator.prepare_switch(
            conversation_id, target_binding_id, ownership_nonce=ownership_nonce,
            lease_id=lease_id, generation=generation, plan=plan)
        # The post-fence read deliberately verifies fresh evidence. Release the
        # preflight projection first instead of retaining two complete histories.
        del rounds, checkpoint, state
        # First execution and crash recovery use the same idempotent state
        # machine. A post-write crash retries with the persisted plan; connector
        # marker/UUID verification proves whether the exact operation landed.
        return self.resume_switch(conversation_id, pending['switch_id'],
                                  ownership_nonce=ownership_nonce)

    def resume_switch(self, conversation_id, switch_id, *, ownership_nonce):
        """Resume a fenced switch from the target Binding's verified offset."""
        state = self.coordinator.state(conversation_id)
        pending = state.get('pending_switch')
        if not pending or pending.get('switch_id') != switch_id:
            raise Conflict('Shepherd Switch 已失效')
        binding = state['bindings'].get(pending['target_binding_id'])
        self._connector_matches(binding, ownership_nonce)
        plan = pending.get('plan')
        operation = pending.get('operation')
        if not isinstance(plan, dict) or not isinstance(operation, dict):
            raise Conflict('Shepherd 旧版切换缺少持久计划或 Writer Claim；保持 Fence')
        if plan.get('through_seq') != pending.get('through_seq'):
            raise Conflict('Shepherd Switch Plan 与 Fence 边界不一致')
        self._ensure_writer_stopped(pending)
        if plan.get('strategy') == 'checkpoint':
            checkpoint_meta = plan.get('checkpoint') or {}
            cover = checkpoint_meta.get('covers_through_seq')
            checkpoint = self.projector.checkpoint(checkpoint_meta)
            self._preflight([], checkpoint=checkpoint, checkpoint_meta=checkpoint_meta)
            if binding['imported_through_seq'] < cover:
                if (binding['imported_through_seq'] != plan.get('target_start_seq')
                        or binding.get('imported_turn_hash', '') != plan.get('target_start_turn_hash', '')):
                    raise Conflict('Shepherd Checkpoint 恢复起点不匹配')
                self._begin_write(conversation_id, switch_id, 'checkpoint_writing')
                result = self._write(
                    conversation_id, switch_id, 'checkpoint_writing',
                    self.connector.load_checkpoint, checkpoint,
                    checkpoint=checkpoint_meta,
                    expected_revision=plan.get('target_start_revision', ''))
                revision = self._revision(result)
                self.coordinator.advance(
                    conversation_id, binding['binding_id'], cover,
                    checkpoint_meta['covers_turn_hash'], revision,
                    ownership_nonce=ownership_nonce,
                    base_checkpoint_id=checkpoint_meta['checkpoint_id'])
                self.coordinator.note_switch_operation(
                    conversation_id, switch_id, 'checkpoint_applied')
                binding = self.coordinator.state(conversation_id)['bindings'][binding['binding_id']]
            else:
                applied = binding['imported_through_seq']
                raw = self.coordinator.store.load_raw(conversation_id)
                expected_hash = ('' if applied == 0 else
                                 raw['turns'][applied - 1]['turn_hash'])
                if (applied > pending['through_seq']
                        or binding.get('base_checkpoint_id') != checkpoint_meta.get('checkpoint_id')
                        or binding.get('imported_turn_hash', '') != expected_hash):
                    raise Conflict('Shepherd Checkpoint 恢复阶段不一致')
        elif plan.get('strategy') != 'incremental':
            raise Conflict('Shepherd Switch Plan 策略不受支持')
        start, end = binding['imported_through_seq'] + 1, pending['through_seq']
        rounds = self.projector.project(conversation_id, start, end) if start <= end else []
        self._preflight(rounds)
        self._begin_write(conversation_id, switch_id, 'append_writing')
        result = self._write(
            conversation_id, switch_id, 'append_writing', self.connector.append,
            rounds, expected_revision=binding.get('native_revision', ''))
        revision = self._revision(result)
        if rounds:
            last = rounds[-1]
            self.coordinator.advance(conversation_id, binding['binding_id'], last['seq'],
                                     last['turn_hash'], revision, ownership_nonce=ownership_nonce)
        elif revision != binding.get('native_revision', ''):
            raise Conflict('Shepherd 空增量写入改变了原生 Revision')
        self.coordinator.note_switch_operation(conversation_id, switch_id, 'append_applied')
        return self.coordinator.complete_switch(conversation_id, switch_id,
                                                ownership_nonce=ownership_nonce)

    def _begin_write(self, conversation_id, switch_id, phase):
        kind = str(getattr(self.connector, 'writer_kind', 'in_process'))
        if kind not in ('in_process', 'process'):
            raise Conflict('Shepherd Connector Writer 类型无效')
        self.coordinator.note_switch_operation(
            conversation_id, switch_id, phase,
            writer={'kind':kind, 'status':'starting', 'boot_id':BOOT_ID})

    def _write(self, conversation_id, switch_id, phase, method, *args, **kwargs):
        kind = str(getattr(self.connector, 'writer_kind', 'in_process'))
        last_identity = {}
        def on_writer(process=None, status='active'):
            identity = process_identity(process) if process is not None else {}
            if identity:
                last_identity.clear(); last_identity.update(identity)
            if kind == 'process' and status == 'stopped':
                # close() may return just before the kernel publishes process-group
                # death. Wait briefly for the same exact identity; ambiguity still
                # remains fenced and is never converted to stopped.
                if last_identity:
                    deadline = time.monotonic() + 2
                    state = process_state(last_identity)
                    while state != 'dead' and time.monotonic() < deadline:
                        time.sleep(.02)
                        state = process_state(last_identity)
                    status = 'dead' if state == 'dead' else 'unknown'
                else:
                    status = 'stopped'
            writer = {'kind':kind, 'status':status, 'boot_id':BOOT_ID}
            if last_identity:
                writer['process'] = dict(last_identity)
            self.coordinator.note_switch_operation(
                conversation_id, switch_id, phase, writer=writer)
        supports = bool(getattr(self.connector, 'writer_notifications', False))
        if supports:
            kwargs['on_writer'] = on_writer
        elif kind == 'process':
            raise Conflict('Shepherd 外部 Writer 缺少身份通知')
        else:
            on_writer(status='active')
        try:
            result = method(*args, **kwargs)
        finally:
            if kind == 'in_process' or not last_identity:
                # A process connector promises to notify immediately after
                # spawning. Returning/raising without an active callback proves
                # it failed before spawn; this is retryable, not unknown.
                on_writer(status='stopped')
        # A successful network call is not proof that its helper process has
        # released native write ownership. Do not advance/grant a Producer until
        # the persisted writer claim is demonstrably stopped.
        pending = (self.coordinator.state(conversation_id) or {}).get('pending_switch') or {}
        self._ensure_writer_stopped(pending)
        return result

    @staticmethod
    def ensure_writer_stopped(pending):
        return SwitchRunner._ensure_writer_stopped(pending)

    @staticmethod
    def _ensure_writer_stopped(pending):
        operation = pending.get('operation')
        if not isinstance(operation, dict):
            raise Conflict('Shepherd 旧版切换缺少 Writer Claim；保持 Fence')
        phase = operation.get('phase')
        if phase in ('fenced','checkpoint_applied','append_applied'):
            return
        if phase not in ('checkpoint_writing','append_writing'):
            raise Conflict('Shepherd 原生 Writer 阶段无效；保持切换 Fence')
        writer = operation.get('writer') or {}
        if writer.get('status') in ('stopped','dead'):
            return
        if writer.get('kind') == 'in_process' and writer.get('boot_id') != BOOT_ID:
            return
        if writer.get('kind') == 'process':
            state = process_state(writer.get('process'))
            if state == 'dead':
                return
            raise Conflict('Shepherd 旧原生 Writer 尚未确认退出；保持切换 Fence')
        raise Conflict('Shepherd 原生 Writer 状态不明；保持切换 Fence')

    def _preflight(self, rounds, **kwargs):
        check = getattr(self.connector, 'preflight', None)
        if not callable(check):
            raise Conflict('目标 Connector 缺少历史兼容预检，未开始切换')
        try:
            check(rounds, **kwargs)
        except (ValueError, InvalidState) as exc:
            raise Conflict('目标引擎历史格式不兼容：' + str(exc)) from exc

    def _plan(self, conversation_id, binding, working_limit, reserve_tokens,
              current_tokens, projection_codec_version):
        state = self.coordinator.store.load_raw(conversation_id)
        if working_limit is None:
            return {'strategy':'incremental','from_seq':binding['imported_through_seq'] + 1,
                    'through_seq':len(state['turns'])}
        if self.checkpoints is None:
            raise InvalidState('Shepherd 缺少 Checkpoint Planner')
        return self.checkpoints.plan(conversation_id, binding['imported_through_seq'],
            working_limit=working_limit, reserve_tokens=reserve_tokens,
            current_tokens=current_tokens, projection_codec_version=projection_codec_version)

    @staticmethod
    def _revision(result):
        if not isinstance(result, dict) or not isinstance(result.get('native_revision'), str):
            raise InvalidState('Shepherd Connector 未返回 native_revision')
        return result['native_revision']

    def _connector_matches(self, binding, ownership_nonce):
        if not binding or binding.get('owner') != 'shepherd':
            raise Conflict('Shepherd 目标 Binding 不存在')
        expected = {
            'binding_id': binding['binding_id'],
            'engine': binding['engine'],
            'native_session_id': binding['native_session_id'],
            'native_cwd': binding['native_cwd'],
            'connector_version': binding['connector_version'],
            'ownership_nonce': ownership_nonce,
        }
        actual = {key: getattr(self.connector, key, None) for key in expected}
        if actual != expected:
            raise Conflict('Shepherd Connector 与 Binding 所有权不匹配')
