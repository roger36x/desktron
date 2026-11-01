"""Single-producer execution boundary for Project Shepherd.

The harness never trusts an in-memory model response as committed history. A
runner must durably finish a native round and return its source reference; the
owned connector then reads that source back before the Coordinator appends the
Manifest entry.
"""
import copy

from .coordinator import digest, estimate_tokens
from .recovery import BOOT_ID, process_identity
from .store import Conflict, InvalidState, PromptNotStarted
from .turn_inputs import TurnInputs, has_native_user_input
from .phase_timing import phase_call


class ExecutionHarness:
    def __init__(self, coordinator, connector, runner):
        self.coordinator = coordinator
        self.connector = connector
        self.runner = runner

    def run(self, conversation_id, *, lease_id, generation, prompt,
            kind='user_turn', attempt_id='', input_public=None):
        from .context_admission import MAX_CONTEXT_RETRIES
        for retry in range(MAX_CONTEXT_RETRIES + 1):
            try:
                row = self._run_once(conversation_id, lease_id=lease_id, generation=generation,
                    prompt=prompt, kind=kind if retry == 0 else 'continuation',
                    attempt_id=(attempt_id + ':context' + str(retry)) if retry and attempt_id else attempt_id,
                    input_public=input_public)
            except PromptNotStarted as exc:
                if not retry:raise
                # The original request DID start and its failure is committed.
                # Never let a rejected rebuild recall that user's original bubble.
                self.runner.context_retry_error=str(exc)
                return row
            prepare = getattr(self.runner, 'prepare_context_retry', None)
            if (row.get('terminal_state') != 'failed_recoverable' or retry == MAX_CONTEXT_RETRIES
                    or not callable(prepare) or prepare() is not True):
                return row
        return row

    def _run_once(self, conversation_id, *, lease_id, generation, prompt,
                  kind='user_turn', attempt_id='', input_public=None):
        def timed(stage, function, *args, **kwargs):
            return phase_call(self.runner, stage, function, *args, **kwargs)
        state = timed('harness.state', self.coordinator.state, conversation_id)
        lease = state.get('lease') or {}
        binding = (state.get('bindings') or {}).get(lease.get('binding_id'))
        self._verify_active(binding, lease, lease_id, generation)
        if kind not in ('user_turn', 'continuation'):
            raise InvalidState('Shepherd 执行回合 kind 无效')
        next_turn_id = f'{conversation_id}:{state["committed_offset"] + 1}'
        input_ref = None
        try:
            prepare_input = getattr(self.runner, 'prepare_input', None)
            if callable(prepare_input):
                timed('harness.prepare_input', prepare_input)
            boundary = timed('harness.recovery_boundary', self.connector.recovery_boundary)
            if input_public is not None:
                input_ref = timed('harness.input_write', TurnInputs(self.coordinator.store.root).write,
                    conversation_id, next_turn_id, copy.deepcopy(input_public))
            inflight = timed('harness.reserve', self.coordinator.begin_round,
                conversation_id, lease_id, generation, kind=kind,
                attempt_id=attempt_id or None, input_ref=input_ref, recovery={
                    'phase': 'reserved', 'boot_id': BOOT_ID,
                    'boundary': copy.deepcopy(boundary)})
        except Exception as exc:
            if input_ref and getattr(self.connector,'unified',False) is not True:
                try: TurnInputs(self.coordinator.store.root).discard(input_ref)
                except Exception: pass
            raise PromptNotStarted(str(exc) or 'Shepherd 原生请求未开始') from exc
        expected_offset = inflight['offset']
        round_id = inflight['round_id']
        attempt_id = inflight['attempt_id']
        dispatched_once = [False]
        try:
            def dispatched(process=None, native_locator=None):
                cancelled = getattr(self.runner, 'cancelled', None)
                if cancelled is not None and cancelled.is_set():
                    raise Conflict('Shepherd 请求在派发前已取消')
                row = timed('harness.dispatch_fence', self.coordinator.mark_dispatched,
                    conversation_id, lease_id, generation, attempt_id=attempt_id,
                    process=process_identity(process) if process is not None else None,
                    native_locator=native_locator)
                dispatched_once[0] = True
                observe = getattr(self.runner, 'on_timing_dispatched', None)
                if callable(observe):
                    try:
                        observe()
                    except Exception:
                        pass  # Timing is diagnostic and cannot alter delivery.
                return row
            def locate(native_locator):
                return self.coordinator.note_native_locator(
                    conversation_id, lease_id, generation, attempt_id=attempt_id,
                    native_locator=native_locator)
            result = self.runner.run(
                prompt=copy.deepcopy(prompt), conversation_id=conversation_id,
                expected_offset=expected_offset, round_id=round_id,
                binding=copy.deepcopy(binding), lease=copy.deepcopy(lease),
                kind=kind, attempt_id=str(attempt_id or ''),
                on_dispatched=dispatched, on_native_locator=locate)
        except Exception as exc:
            if dispatched_once[0]:
                self.coordinator.mark_unresolved(conversation_id, lease_id, generation,
                                                 attempt_id=attempt_id)
                raise
            if getattr(exc,'preserve_reserved',False):
                raise
            self.coordinator.cancel_reserved(conversation_id, attempt_id=attempt_id)
            if input_ref and getattr(self.connector,'unified',False) is not True:
                try: TurnInputs(self.coordinator.store.root).discard(input_ref)
                except Exception: pass  # Content addressing still prevents reuse conflicts.
            raise PromptNotStarted(str(exc) or 'Shepherd 原生请求未开始') from exc
        if not dispatched_once[0]:
            self.coordinator.mark_unresolved(
                conversation_id, lease_id, generation, attempt_id=attempt_id,
                reason='runner_bypassed_dispatch_fence')
            raise InvalidState('Shepherd Runner 绕过了持久化派发闸')
        try:
            try:
                self._persist_result(conversation_id, binding, lease_id, generation, attempt_id, result)
            except Exception:
                self.coordinator.mark_unresolved(conversation_id, lease_id, generation,
                                                 attempt_id=attempt_id)
                raise
            row=self.recover(conversation_id, lease_id=lease_id, generation=generation)
        except BaseException:
            discard=getattr(self.runner,'discard_resident',None)
            if callable(discard):discard()
            raise
        confirm=getattr(self.runner,'confirm_resident',None)
        if callable(confirm):confirm()
        return row

    def _persist_result(self, conversation_id, binding, lease_id, generation, attempt_id, result):
        if not isinstance(result, dict):
            raise InvalidState('Shepherd Runner 返回格式无效')
        source_ref = result.get('source')
        terminal_state = result.get('terminal_state')
        if terminal_state not in ('completed', 'failed_recoverable', 'interrupted_confirmed'):
            raise InvalidState('Shepherd Runner 未返回可提交终态')
        entry_id = source_ref.get('entry_id') if isinstance(source_ref, dict) else ''
        expected_key = f'{binding["engine"]}:{binding["native_session_id"]}:{entry_id}'
        if (not isinstance(source_ref, dict) or source_ref.get('kind') != 'native_round'
                or source_ref.get('engine') != binding['engine']
                or source_ref.get('session_id') != binding['native_session_id']
                or not isinstance(entry_id, str) or not entry_id
                or source_ref.get('source_key') != expected_key):
            raise Conflict('Shepherd Runner 返回了错误的原生 Round 来源')
        self.coordinator.source_persisted(
            conversation_id, lease_id, generation, attempt_id=attempt_id,
            source=source_ref, native_revision=str(result.get('native_revision') or ''),
            terminal_state=terminal_state)

    def recover(self, conversation_id, *, lease_id, generation):
        """Commit an already-persisted source without running the engine again."""
        state = self.coordinator.state(conversation_id)
        lease = state.get('lease') or {}
        binding = (state.get('bindings') or {}).get(lease.get('binding_id'))
        self._verify_active(binding, lease, lease_id, generation)
        inflight = state.get('inflight') or {}
        if inflight.get('state') == 'needs_reconciliation':
            raise Conflict('Shepherd 上轮已阻断；请先隔离无法验证内容并继续')
        if inflight.get('state') == 'running':
            # A crash may occur after receipt fsync but before coordinator update.
            # Never call run() here, even when the native request is uncertain.
            restore = getattr(self.runner, 'recover_result', None)
            result = restore(inflight['round_id']) if callable(restore) else None
            if result is None:
                self.coordinator.mark_unresolved(conversation_id, lease_id, generation,
                                                 attempt_id=inflight['attempt_id'])
                raise Conflict('Shepherd 上轮原生终态尚未确认；禁止自动重跑，请核验私有来源')
            self._persist_result(conversation_id, binding, lease_id, generation,
                                 inflight['attempt_id'], result)
            inflight = self.coordinator.state(conversation_id)['inflight']
        if (inflight.get('state') != 'source_persisted'
                or inflight.get('binding_id') != binding.get('binding_id')
                or inflight.get('generation') != generation):
            raise Conflict('Shepherd 没有可恢复的持久化来源')
        source_ref = copy.deepcopy(inflight.get('source'))
        verify_attempt = getattr(self.runner, 'verify_recovery_source', None)
        if callable(verify_attempt):
            verify_attempt(inflight['round_id'])
        input_ref = inflight.get('input_ref')
        trusted_input = (TurnInputs(self.coordinator.store.root).read(input_ref)
                         if input_ref else None)
        resolved = (self.connector.resolve_source(source_ref, trusted_public=trusted_input)
                    if getattr(self.connector, 'engine', None) == 'codex'
                    else self.connector.resolve_source(source_ref))
        if not isinstance(resolved, dict) or set(resolved) != {'source', 'public'}:
            raise InvalidState('Shepherd Connector 来源回读格式无效')
        public = resolved['public']
        if not isinstance(public, list):
            raise InvalidState('Shepherd 公共 Round 必须是数组')
        if input_ref and not has_native_user_input(public, resolved['source']):
            public = trusted_input + copy.deepcopy(public)
        classify = getattr(self.connector, 'source_compatibility', None)
        compatibility = classify(resolved['source']) if callable(classify) else []
        return self.coordinator.commit(
            conversation_id, lease_id, generation, source=source_ref,
            source_hash=digest(resolved['source']), public_hash=digest(public),
            terminal_state=inflight.get('terminal_state', 'completed'), kind=inflight['kind'],
            attempt_id=inflight['attempt_id'], estimated_tokens=estimate_tokens(public),
            compatibility=compatibility)

    def _verify_active(self, binding, lease, lease_id, generation):
        if (not binding or binding.get('owner') != 'shepherd'
                or lease.get('lease_id') != lease_id
                or lease.get('generation') != generation
                or generation is None
                or lease.get('binding_id') != getattr(self.connector, 'binding_id', None)
                or binding.get('engine') != getattr(self.connector, 'engine', None)
                or binding.get('native_session_id') != getattr(
                    self.connector, 'native_session_id', None)
                or binding.get('connector_version') != getattr(
                    self.connector, 'connector_version', None)
                or binding.get('ownership_nonce') != getattr(
                    self.connector, 'ownership_nonce', None)):
            raise Conflict('Shepherd Runner 的 Producer Generation 已失效')
        if not callable(getattr(self.runner, 'run', None)):
            raise InvalidState('Shepherd Runner 不可执行')
