"""Product lifecycle for user-visible Shepherd Conversations."""
import hashlib
import json
import os
import shutil
import uuid

from .claude_codec import (CODEC_VERSION as CLAUDE_CODEC,
                           PROMPT_SNAPSHOT_POLICY, static_system_prompt)
from .claude_connector import ClaudeConnector
from .codex_connector import CodexFamilyConnector
from .coordinator import Coordinator, digest, estimate_tokens
from .pi_codec import CODEC_VERSION as PI_CODEC
from .pi_connector import PiConnector
from .projector import ReadProjector
from .source_resolver import SourceResolver
from .source_receipts import SourceReceipts
from .store import Conflict, InvalidState, Store, valid_id
from .switch import SwitchRunner
from .turn_inputs import TurnInputs
from .recovery import BOOT_ID, process_identity, process_state


class Service:
    """Create and recover only Shepherd-owned native bindings."""

    def __init__(self, root, *, binary_lookup=None, claude_system_lookup=None, storage="native"):
        if storage not in ("native","unified"): raise InvalidState("Unknown storage mode")
        self.storage = storage
        self.root = os.path.abspath(os.fspath(root))
        self.store = Store(self.root)
        self.coordinator = Coordinator(self.store)
        self.binary_lookup = binary_lookup or shutil.which
        self.claude_system_lookup = claude_system_lookup

    def _claude_prompt(self, conversation_id, explicit=''):
        text = str(explicit or '')
        has_product_source = callable(self.claude_system_lookup)
        if not text and has_product_source:
            text = str(self.claude_system_lookup(conversation_id) or '')
        # Product-created Claude Bindings always snapshot at least the pinned
        # Bash contract, even when role/style files are temporarily empty.
        return static_system_prompt(text) if text or has_product_source else ''

    def _prompt_identity(self, conversation_id, engine, explicit=''):
        prompt = self._claude_prompt(conversation_id, explicit) if engine == 'claude' else ''
        return (prompt, PROMPT_SNAPSHOT_POLICY,
                hashlib.sha256(prompt.encode()).hexdigest()) if prompt else ('', '', '')

    def _new_binding_record(self, conversation_id, engine, native_cwd,
                            binding_id, nonce, native, *, explicit_prompt=''):
        replacement = {
            'binding_id':binding_id, 'conversation_id':conversation_id,
            'owner':'shepherd', 'ownership_nonce':nonce, 'engine':engine,
            'native_session_id':native['native_session_id'],
            'native_locator':native['native_session_id'],
            'native_cwd':os.path.realpath(native_cwd),
            'connector_version':native['connector_version'],
            'imported_through_seq':0, 'imported_turn_hash':'',
            'native_revision':str(native.get('native_revision') or ''),
            'base_checkpoint_id':'', 'status':'current',
            'created_at':__import__('time').time(),
        }
        if native['connector_version'].startswith('unified-v1:'):
            return replacement, ''
        prompt, policy, prompt_hash = self._prompt_identity(
            conversation_id, engine, explicit_prompt)
        if policy:
            replacement.update(prompt_snapshot_policy=policy,
                               prompt_snapshot_sha256=prompt_hash)
        return replacement, prompt

    def _prepare_replacement_prompt(self, conversation_id, replacement, prompt=''):
        if replacement['connector_version'].startswith('unified-v1:'):
            self.coordinator.ownership.issue(replacement)
            return replacement
        if replacement.get('engine') == 'claude' and not replacement.get('prompt_snapshot_policy'):
            prompt, policy, prompt_hash = self._prompt_identity(conversation_id, 'claude', prompt)
            if policy:
                replacement.update(prompt_snapshot_policy=policy,
                                   prompt_snapshot_sha256=prompt_hash)
        if not prompt and replacement.get('prompt_snapshot_policy'):
            prompt = self._claude_prompt(conversation_id)
        self.coordinator.ownership.issue(replacement)
        if replacement.get('prompt_snapshot_policy'):
            result = self.connector(replacement).ensure_prompt_snapshot(prompt)
            replacement['native_revision'] = str(result.get('native_revision') or '')
        return replacement

    def create(self, conversation_id, engine, native_cwd, *, claude_system_text=''):
        valid_id(conversation_id, 'conversation_id')
        valid_id(engine, 'engine')
        native_cwd = os.path.realpath(str(native_cwd or ''))
        if not os.path.isdir(native_cwd):
            raise InvalidState('Shepherd Conversation 工作目录无效')
        if self.store.load(conversation_id) is not None:
            raise Conflict('Shepherd Conversation 已存在')
        binding_id = self._binding_id(conversation_id, engine)
        nonce = uuid.uuid4().hex
        native = None
        created_state = False
        try:
            self.coordinator.create(conversation_id)
            created_state = True
            if binding_id.startswith('ubind_'):
                native = self._create_native(engine, native_cwd, binding_id)
            elif engine in ('claude', 'pi'):
                native = {'native_session_id': str(uuid.uuid4()),
                          'native_revision': '',
                          'connector_version': CLAUDE_CODEC if engine == 'claude' else PI_CODEC}
            elif engine == 'codex':
                native = CodexFamilyConnector.create_native(
                    engine, native_cwd, store_root=self.root, binding_id=binding_id,
                    binary=self.binary_lookup(engine))
            else:
                raise InvalidState('Shepherd 初始引擎不受支持')
            binding, prompt = self._new_binding_record(
                conversation_id, engine, native_cwd, binding_id, nonce, native,
                explicit_prompt=claude_system_text)
            self._prepare_replacement_prompt(conversation_id, binding, prompt)
            self.coordinator.bind(
                conversation_id, engine, binding['native_session_id'],
                native_cwd=native_cwd, connector_version=binding['connector_version'],
                binding_id=binding_id, ownership_nonce=nonce,
                native_revision=binding['native_revision'],
                prompt_snapshot_policy=binding.get('prompt_snapshot_policy', ''),
                prompt_snapshot_sha256=binding.get('prompt_snapshot_sha256', ''))
            lease = self.coordinator.acquire(
                conversation_id, binding_id, ownership_nonce=nonce)
            return {'conversation_id': conversation_id, 'engine': engine,
                    'binding_id': binding_id, 'lease': lease}
        except Exception:
            if created_state:
                self._rollback_new(conversation_id, binding_id)
            raise

    def recover_legacy_incomplete(self, conversation_id, recovery_id):
        """Recover one v1 preserve_and_end gap without executing the model."""
        state = self.store.load_raw(conversation_id)
        item = next((row for row in state.get('legacy_recoveries', [])
                     if row.get('recovery_id') == recovery_id), None)
        if not item:
            raise Conflict('Shepherd Legacy Recovery 不存在')
        binding = state['bindings'].get(item['source_binding_id'])
        if not binding:
            raise Conflict('Shepherd Legacy Recovery Binding 不存在')
        inflight = item.get('inflight') or {}; recovery = inflight.get('recovery') or {}
        if process_state(recovery.get('process')) != 'dead':
            raise Conflict('Shepherd Legacy Recovery Writer 尚未确认退出')
        connector = self.connector(binding)
        recovered = connector.recover_suffix(
            recovery.get('boundary') or {}, recovery.get('native_locator') or {})
        if (not isinstance(recovered, dict)
                or set(recovered) != {'locator','source','public','unfinished_tool_ids'}):
            raise InvalidState('Shepherd Legacy Recovery 来源格式无效')
        input_public = self._public_input(conversation_id, inflight.get('attempt_id'))
        input_ref = (TurnInputs(self.root).write(
            conversation_id, item['turn_id'], input_public) if input_public else None)
        public = list(recovered['public'])
        if input_public and not any(row.get('type') == 'message' and row.get('role') == 'user'
                                    for row in public if isinstance(row, dict)):
            public = input_public + public
        locator = dict(recovered['locator'])
        locator['terminal_state'] = 'unknown'
        locator['unfinished_tool_ids'] = list(recovered['unfinished_tool_ids'])
        SourceReceipts(self.root).seal(binding, item['turn_id'], locator)
        row = self.coordinator.settle_legacy_recovery(
            conversation_id, recovery_id, source={
                'kind':'native_round','engine':binding['engine'],
                'session_id':binding['native_session_id'],'entry_id':item['turn_id'],
                'source_key':f'{binding["engine"]}:{binding["native_session_id"]}:{item["turn_id"]}'},
            source_hash=digest(recovered['source']), public_hash=digest(public),
            unfinished_tool_ids=recovered['unfinished_tool_ids'],
            native_revision=str(locator.get('native_revision') or ''),
            input_ref=input_ref, estimated_tokens=estimate_tokens(public))
        return self.recover_incomplete(
            conversation_id, item.get('resume_binding_id') or item['source_binding_id']) | {
                'recovered_turn':row}

    def ensure_active_input_ref(self, conversation_id, active_turn):
        if active_turn.get('input_ref'):
            return active_turn['input_ref']
        public = self._public_input(conversation_id, active_turn.get('attempt_id'))
        if not public:
            return None
        reference = TurnInputs(self.root).write(
            conversation_id, active_turn['turn_id'], public)
        return self.coordinator.attach_input_ref(
            conversation_id, attempt_id=active_turn['attempt_id'], input_ref=reference)

    def _public_input(self, conversation_id, attempt_id):
        """Read the user message paired with a legacy attempt from the public projection."""
        try:
            seq = int(str(attempt_id or '').removeprefix('attempt_'))
        except ValueError:
            return []
        path = os.path.join(os.path.dirname(self.root), 'adapter', 'shepherd',
                            str(conversation_id) + '.jsonl')
        try:
            with open(path, encoding='utf-8') as stream:
                for line in stream:
                    row = json.loads(line); info = row.get('info') or {}
                    if info.get('role') != 'user' or int(info.get('seq') or 0) != seq:
                        continue
                    content = []
                    for part in row.get('parts') or []:
                        if part.get('type') == 'text':
                            content.append({'type':'input_text','text':str(part.get('text') or '')})
                        # Legacy public image URLs are references only. Native
                        # suffixes already contain verified image bytes; if the
                        # suffix is empty, text plus an explicit unknown boundary
                        # is safer than dereferencing an unverified URL here.
                    return ([{'type':'message','role':'user','content':content}]
                            if content else [])
        except (OSError, ValueError, TypeError):
            return []
        return []

    def recover_incomplete(self, conversation_id, source_binding_id):
        """Create and catch up a clean same-engine Binding after unknown settlement."""
        state = self.coordinator.state(conversation_id)
        pending = (state or {}).get('pending_switch') or {}
        if pending.get('recovery_turn_id'):
            target_id = pending.get('target_binding_id')
            return self._switch_binding(conversation_id, target_id,
                                        self.active(conversation_id), pending)
        old = (state.get('bindings') or {}).get(source_binding_id)
        if not old or old.get('status') != 'recovery_required':
            raise Conflict('Shepherd 没有待换绑的不完整回合')
        engine, native_cwd = old['engine'], old['native_cwd']
        binding_id = self._binding_id(conversation_id, engine)
        nonce = uuid.uuid4().hex
        native = self._create_native(engine, native_cwd, binding_id)
        replacement = {
            'binding_id':binding_id,'conversation_id':conversation_id,
            'owner':'shepherd','ownership_nonce':nonce,'engine':engine,
            'native_session_id':native['native_session_id'],
            'native_locator':native['native_session_id'],'native_cwd':native_cwd,
            'connector_version':native['connector_version'],
            'imported_through_seq':0,'imported_turn_hash':'',
            'native_revision':str(native.get('native_revision') or ''),
            'base_checkpoint_id':'','status':'current',
            'created_at':__import__('time').time(),
        }
        try:
            self._prepare_replacement_prompt(conversation_id, replacement)
            pending = self.coordinator.prepare_recovery_binding(
                conversation_id, source_binding_id, replacement)
            return self._switch_binding(conversation_id, binding_id,
                                        self.active(conversation_id), pending)
        except Exception:
            current = self.coordinator.state(conversation_id) or {}
            if binding_id not in (current.get('bindings') or {}):
                self._rollback_binding(binding_id)
            raise

    def reconcile_end(self, conversation_id, *, attempt_id):
        """Omit unverifiable content explicitly, then rebuild a clean Binding."""
        state = self.coordinator.state(conversation_id)
        legacy = next((row for row in state.get('legacy_recoveries', [])
                       if (row.get('inflight') or {}).get('attempt_id') == attempt_id), None)
        if legacy:
            public = self._public_input(conversation_id, attempt_id)
            input_ref = (TurnInputs(self.root).write(
                conversation_id, legacy['turn_id'], public) if public else None)
            self.coordinator.omit_legacy_recovery(
                conversation_id, legacy['recovery_id'], input_ref=input_ref)
            return self.recover_incomplete(
                conversation_id, legacy.get('resume_binding_id') or legacy['source_binding_id'])
        inflight = (state or {}).get('inflight') or {}
        pending = (state or {}).get('pending_switch') or {}
        if not inflight and pending.get('reconciliation_id'):
            record = next((row for row in state.get('reconciliations') or []
                           if row.get('reconciliation_id') == pending['reconciliation_id']), None)
            if record and (record.get('inflight') or {}).get('attempt_id') == attempt_id:
                return self.resume_reconciliation(conversation_id)
        if inflight.get('state') != 'needs_reconciliation':
            raise Conflict('Shepherd 没有待核验回合')
        old = state['bindings'].get(inflight.get('binding_id'))
        if not old:
            raise Conflict('Shepherd 待核验 Binding 不存在')
        engine, native_cwd = old['engine'], old['native_cwd']
        binding_id = self._binding_id(conversation_id, engine)
        nonce = uuid.uuid4().hex
        native = self._create_native(engine, native_cwd, binding_id)
        replacement = {
            'binding_id': binding_id, 'conversation_id': conversation_id,
            'owner': 'shepherd', 'ownership_nonce': nonce, 'engine': engine,
            'native_session_id': native['native_session_id'],
            'native_locator': native['native_session_id'],
            'native_cwd': native_cwd,
            'connector_version': native['connector_version'],
            'imported_through_seq': 0, 'imported_turn_hash': '',
            'native_revision': str(native.get('native_revision') or ''),
            'base_checkpoint_id': '', 'status': 'current',
            'created_at': __import__('time').time(),
        }
        try:
            self._prepare_replacement_prompt(conversation_id, replacement)
            pending = self.coordinator.begin_reconciliation(
                conversation_id, replacement, attempt_id=attempt_id)
            current = self.active(conversation_id)
            return self._switch_binding(conversation_id, binding_id, current, pending)
        except Exception:
            current = self.coordinator.state(conversation_id) or {}
            if binding_id not in (current.get('bindings') or {}):
                self._rollback_binding(binding_id)
            raise

    def resume_reconciliation(self, conversation_id):
        state = self.coordinator.state(conversation_id)
        pending = (state or {}).get('pending_switch') or {}
        if not pending.get('reconciliation_id'):
            raise Conflict('Shepherd 没有待恢复的核验切换')
        target_id = pending.get('target_binding_id')
        if target_id not in (state.get('bindings') or {}):
            raise Conflict('Shepherd 核验替代 Binding 不存在')
        return self._switch_binding(
            conversation_id, target_id, self.active(conversation_id), pending)

    def _binding_id(self, conversation_id, engine):
        state = self.store.load_raw(conversation_id)
        bindings = list((state or {}).get('bindings', {}).values())
        # Once the product selects unified storage, every new Binding must stay
        # unified even while historical native Bindings remain as read evidence.
        unified = (self.storage == 'unified'
                   or any(row.get('connector_version','').startswith('unified-v1:')
                          for row in bindings))
        return ('ubind_' if unified else 'bind_') + engine + '_' + uuid.uuid4().hex

    def promote_to_unified(self, conversation_id):
        """Fence an idle native Producer and catch up a unified same-engine one.

        Historical native sources remain immutable and readable.  The target
        stores only its verified cursor, so promotion does not duplicate bodies.
        The pending switch is durable and the method is safe to retry after a
        process crash.
        """
        state = self.coordinator.state(conversation_id)
        if not state:
            raise Conflict('Shepherd Conversation 不存在')
        pending = state.get('pending_switch') or {}
        if pending:
            target_id = pending.get('target_binding_id')
            target = (state.get('bindings') or {}).get(target_id) or {}
            if not (pending.get('storage_upgrade')
                    and target.get('connector_version','').startswith('unified-v1:')):
                raise Conflict('Shepherd 已有其他切换尚未完成')
            return self._switch_binding(
                conversation_id, target_id, self.active(conversation_id), pending)
        current = self.active(conversation_id)
        binding, lease = current['binding'], current['lease']
        if not binding or not lease:
            raise Conflict('Shepherd Conversation 没有 Active Producer')
        if binding.get('connector_version','').startswith('unified-v1:'):
            return current
        if state.get('active_turn'):
            raise Conflict('Shepherd 执行回合尚未确认，不能升级存储')
        engine = binding['engine']
        binding_id = 'ubind_' + engine + '_' + uuid.uuid4().hex
        nonce = uuid.uuid4().hex
        native = self._create_native(engine, binding['native_cwd'], binding_id)
        replacement, prompt = self._new_binding_record(
            conversation_id, engine, binding['native_cwd'], binding_id, nonce, native)
        staged = False
        try:
            self._prepare_replacement_prompt(conversation_id, replacement, prompt)
            source_sessions = {(row['source']['engine'], row['source']['session_id'])
                               for row in state['rounds']
                               if isinstance(row.get('source'), dict)}
            connectors = [self.connector(row) for row in state['bindings'].values()
                          if (row['engine'], row['native_session_id']) in source_sessions]
            projector = ReadProjector(self.store, SourceResolver(connectors))
            target = self.connector(replacement)
            rounds = projector.project(conversation_id)
            SwitchRunner(self.coordinator, projector, target)._preflight(rounds)
            del rounds
            self._backup_storage_upgrade(conversation_id)
            pending = self.coordinator.prepare_storage_upgrade(
                conversation_id, replacement,
                lease_id=lease['lease_id'], generation=lease['generation'])
            staged = True
            return self._switch_binding(
                conversation_id, binding_id, self.active(conversation_id), pending)
        except Exception:
            if not staged:
                self._rollback_binding(binding_id)
            raise

    def _backup_storage_upgrade(self, conversation_id):
        """Keep one metadata-only pre-upgrade snapshot; native bodies stay put."""
        valid_id(conversation_id, 'conversation_id')
        from durable_json import read, write
        path = os.path.join(self.root, 'migrations', 'native-to-unified-v1',
                            conversation_id + '.json')
        snapshot = read(path)
        if snapshot:
            return path
        snapshot.update({'format':'shepherd-native-to-unified-v1',
                         'conversation_id':conversation_id,
                         'state':self.store.load_raw(conversation_id)})
        write(path, snapshot)
        return path

    def _create_native(self, engine, native_cwd, binding_id, on_writer=None):
        if binding_id.startswith('ubind_'):
            from .unified_connector import PREFIX, CODECS
            if engine not in CODECS: raise InvalidState('Unknown unified engine')
            if on_writer: on_writer(status='stopped')
            return {'native_session_id':str(uuid.uuid4()), 'native_revision':'',
                    'connector_version':PREFIX + CODECS[engine]}
        if engine in ('claude', 'pi'):
            return {'native_session_id': str(uuid.uuid4()), 'native_revision':'',
                    'connector_version': CLAUDE_CODEC if engine == 'claude' else PI_CODEC}
        if engine == 'codex':
            return CodexFamilyConnector.create_native(
                engine, native_cwd, store_root=self.root, binding_id=binding_id,
                binary=self.binary_lookup(engine), on_writer=on_writer)
        raise InvalidState('Shepherd 目标引擎不受支持')

    def ensure_binding(self, conversation_id, engine, native_cwd, *, claude_system_text=''):
        state = self.coordinator.state(conversation_id)
        if state is None: raise Conflict('Shepherd Conversation 不存在')
        existing = next((row for row in state['bindings'].values()
                         if row.get('engine') == engine and row.get('status') == 'current'), None)
        if existing:
            if engine == 'claude' and existing.get('prompt_snapshot_policy'):
                connector = self.connector(existing)
                prompt = (connector.prompt_snapshot_text()
                          if connector.native.load()
                          else self._claude_prompt(conversation_id, claude_system_text))
                result = connector.ensure_prompt_snapshot(prompt)
                self.coordinator.advance(conversation_id, existing['binding_id'],
                    existing['imported_through_seq'], existing['imported_turn_hash'],
                    result['native_revision'], ownership_nonce=existing['ownership_nonce'])
            return existing['binding_id']
        binding_id = self._binding_id(conversation_id, engine)
        nonce = uuid.uuid4().hex
        try:
            native = self._create_native(engine, native_cwd, binding_id)
        except Exception:
            self._rollback_binding(binding_id)
            raise
        try:
            binding, prompt = self._new_binding_record(
                conversation_id, engine, native_cwd, binding_id, nonce, native,
                explicit_prompt=claude_system_text)
            self._prepare_replacement_prompt(conversation_id, binding, prompt)
            self.coordinator.bind(conversation_id, engine, binding['native_session_id'],
                native_cwd=native_cwd, connector_version=binding['connector_version'],
                binding_id=binding_id, ownership_nonce=nonce,
                native_revision=binding['native_revision'],
                prompt_snapshot_policy=binding.get('prompt_snapshot_policy', ''),
                prompt_snapshot_sha256=binding.get('prompt_snapshot_sha256', ''))
            return binding_id
        except Exception:
            current = self.coordinator.state(conversation_id) or {}
            if binding_id in (current.get('bindings') or {}):
                self.coordinator.discard_unactivated_binding(conversation_id, binding_id)
            self._rollback_binding(binding_id)
            raise

    def connector(self, binding):
        nonce = binding['ownership_nonce']
        if binding['connector_version'] == 'fork-snapshot-v1':
            from .fork import ForkConnector
            return ForkConnector(binding, store_root=self.root,
                                 ownership=self.coordinator.ownership)
        if binding['connector_version'].startswith('unified-v1:'):
            from .unified_connector import UnifiedConnector
            return UnifiedConnector(binding, nonce, store_root=self.root,
                ownership=self.coordinator.ownership, binary=self.binary_lookup(binding['engine']))
        if binding['engine'] == 'claude':
            return ClaudeConnector(binding, nonce, store_root=self.root,
                                   ownership=self.coordinator.ownership)
        if binding['engine'] == 'pi':
            return PiConnector(binding, nonce, store_root=self.root,
                               ownership=self.coordinator.ownership)
        return CodexFamilyConnector(binding, nonce, store_root=self.root,
                                    binary=self.binary_lookup(binding['engine']),
                                    ownership=self.coordinator.ownership)

    def switch(self, conversation_id, engine, native_cwd, cancelled=None,
               selection_epoch=None, claude_system_text=''):
        current = self.active(conversation_id)
        if current['state'].get('inflight'):
            raise Conflict('Shepherd 执行回合尚未确认，不能切换引擎')
        if current['binding'] and current['binding']['engine'] == engine:
            if (self.storage != 'unified'
                    or current['binding'].get('connector_version','').startswith('unified-v1:')):
                return current
            return self.promote_to_unified(conversation_id)
        pending = current['state'].get('pending_switch')
        if pending:
            target_id = pending['target_binding_id']
            pending_engine = current['state']['bindings'][target_id]['engine']
            if pending_engine != engine:
                # A fenced producer cannot be revived. Explicit user selection
                # migrates the pending target to a fresh Binding instead.
                return self._retarget_pending(
                    conversation_id, pending, engine, native_cwd,
                    selection_epoch=selection_epoch)
        else:
            target_id = self.ensure_binding(conversation_id, engine, native_cwd,
                                            claude_system_text=claude_system_text)
        try:
            return self._switch_binding(conversation_id, target_id, current, pending,
                                        cancelled=cancelled)
        except Exception:
            # Preflight failures preserve the original lease. Post-fence
            # failures retain the target for source-verified recovery.
            state = self.coordinator.state(conversation_id)
            if (target_id not in current['state']['bindings']
                    and not state.get('pending_switch')):
                self.coordinator.discard_unactivated_binding(conversation_id, target_id)
                self._rollback_binding(target_id)
            raise

    def _retarget_pending(self, conversation_id, pending, engine, native_cwd,
                          selection_epoch=None):
        SwitchRunner.ensure_writer_stopped(pending)
        if not isinstance(selection_epoch, int) or selection_epoch < 0:
            raise Conflict('Shepherd Fence 后改选缺少选择代际')
        prior = pending.get('retarget') or {}
        if prior:
            same_selection = (prior.get('engine') == engine
                              and prior.get('selection_epoch') == selection_epoch)
            if same_selection and prior.get('phase') == 'native_created':
                moved = self.coordinator.retarget_switch(
                    conversation_id, pending['switch_id'],
                    retarget_id=prior['retarget_id'])
                return self._switch_binding(
                    conversation_id, prior['binding_id'], self.active(conversation_id), moved)
            writer = prior.get('writer') or {}
            if writer.get('status') not in ('stopped','dead'):
                confirmed_dead = (
                    writer.get('kind') == 'in_process' and writer.get('boot_id') != BOOT_ID
                ) or (
                    writer.get('kind') == 'process'
                    and process_state(writer.get('process')) == 'dead'
                )
                if not confirmed_dead:
                    raise Conflict('Shepherd 替代 Binding Writer 尚未确认退出；保持 Fence')
                writer = {**writer, 'status':'dead'}
                self.coordinator.note_retarget_writer(
                    conversation_id, pending['switch_id'], prior['retarget_id'], writer)
            if same_selection:
                raise Conflict('Shepherd 替代 Binding 创建结果待核验；请重新选择目标引擎')
        binding_id = self._binding_id(conversation_id, engine)
        nonce = uuid.uuid4().hex
        intent = self.coordinator.reserve_retarget(
            conversation_id, pending['switch_id'], engine, native_cwd,
            binding_id=binding_id, ownership_nonce=nonce, boot_id=BOOT_ID,
            selection_epoch=selection_epoch, replace_existing=bool(prior))
        kind = ('process' if engine == 'codex' and not binding_id.startswith('ubind_')
                else 'in_process')
        self.coordinator.note_retarget_writer(conversation_id, pending['switch_id'],
            intent['retarget_id'], {'kind':kind, 'status':'starting', 'boot_id':BOOT_ID})
        last_identity = {}
        def on_writer(process=None, status='active'):
            identity = process_identity(process) if process is not None else {}
            if identity:
                last_identity.clear(); last_identity.update(identity)
            if kind == 'process' and status == 'stopped':
                status = ('dead' if last_identity and process_state(last_identity) == 'dead'
                          else 'unknown')
            writer = {'kind':kind, 'status':status, 'boot_id':BOOT_ID}
            if last_identity:
                writer['process'] = dict(last_identity)
            self.coordinator.note_retarget_writer(
                conversation_id, pending['switch_id'], intent['retarget_id'], writer)
        try:
            native = self._create_native(
                engine, native_cwd, binding_id,
                on_writer=on_writer if kind == 'process' else None)
            replacement = {
                'binding_id':binding_id, 'conversation_id':conversation_id,
                'owner':'shepherd', 'ownership_nonce':nonce, 'engine':engine,
                'native_session_id':native['native_session_id'],
                'native_locator':native['native_session_id'],
                'native_cwd':os.path.realpath(native_cwd),
                'connector_version':native['connector_version'],
                'imported_through_seq':0, 'imported_turn_hash':'',
                'native_revision':str(native.get('native_revision') or ''),
                'base_checkpoint_id':'', 'status':'current',
                'created_at':__import__('time').time(),
            }
            self._prepare_replacement_prompt(conversation_id, replacement)
            if kind == 'in_process':
                on_writer(status='stopped')
            else:
                current = ((self.coordinator.state(conversation_id).get('pending_switch') or {})
                           .get('retarget') or {}).get('writer') or {}
                if current.get('status') != 'dead':
                    raise Conflict('Shepherd 替代 Binding Writer 尚未确认退出；保持 Fence')
            self.coordinator.stage_retarget(
                conversation_id, pending['switch_id'], replacement,
                retarget_id=intent['retarget_id'])
            moved = self.coordinator.retarget_switch(
                conversation_id, pending['switch_id'], retarget_id=intent['retarget_id'])
            return self._switch_binding(
                conversation_id, binding_id, self.active(conversation_id), moved)
        except Exception:
            if kind == 'in_process':
                try: on_writer(status='stopped')
                except Exception: pass
            # Do not delete native paths after a durable retarget reservation:
            # creation may have succeeded even when the caller saw an error.
            raise

    def _switch_binding(self, conversation_id, target_id, current, pending, cancelled=None):
        state = self.coordinator.state(conversation_id)
        target = state['bindings'][target_id]
        source_sessions = {(row['source']['engine'], row['source']['session_id'])
                           for row in state['rounds']
                           if isinstance(row.get('source'), dict)}
        connector = self.connector(target)
        if target.get('prompt_snapshot_policy'):
            prompt = (connector.prompt_snapshot_text()
                      if connector.native.load() else self._claude_prompt(conversation_id))
            if not prompt:
                raise Conflict('Claude 新 Binding 缺少静态提示快照正文')
            result = connector.ensure_prompt_snapshot(prompt)
            if result['native_revision'] != target.get('native_revision', ''):
                self.coordinator.advance(conversation_id, target_id,
                    target['imported_through_seq'], target['imported_turn_hash'],
                    result['native_revision'], ownership_nonce=target['ownership_nonce'])
                state = self.coordinator.state(conversation_id)
                target = state['bindings'][target_id]
                connector = self.connector(target)
        connectors = [self.connector(row) for row in state['bindings'].values()
                      if (row['engine'], row['native_session_id']) in source_sessions]
        runner = SwitchRunner(self.coordinator,
            ReadProjector(self.store, SourceResolver(connectors)), connector)
        lease = current['lease']
        if pending:
            runner.resume_switch(conversation_id, pending['switch_id'],
                                 ownership_nonce=target['ownership_nonce'])
        else:
            if lease is None: raise Conflict('Shepherd Conversation 没有 Active Producer')
            runner.switch(conversation_id, target_id,
                ownership_nonce=target['ownership_nonce'],
                lease_id=lease['lease_id'], generation=lease['generation'],
                cancelled=cancelled)
        return self.active(conversation_id)

    def active(self, conversation_id):
        state = self.coordinator.state(conversation_id)
        if not state or state.get('mode') != 'active':
            raise Conflict('Shepherd Conversation 不存在')
        lease = state.get('lease')
        if not lease:
            return {'state': state, 'binding': None, 'lease': None}
        binding = state['bindings'].get(lease['binding_id'])
        if not binding:
            raise InvalidState('Shepherd Active Binding 缺失')
        return {'state': state, 'binding': binding, 'lease': lease}

    def _rollback_binding(self, binding_id):
        valid_id(binding_id, 'binding_id')
        root = os.path.realpath(self.root)
        for path in (
            os.path.join(self.root, 'ownership', binding_id + '.json'),
            os.path.join(self.root, 'native', 'claude', binding_id + '.sqlite3'),
            os.path.join(self.root, 'native', 'claude', binding_id + '.sqlite3-wal'),
            os.path.join(self.root, 'native', 'claude', binding_id + '.sqlite3-shm'),
            os.path.join(self.root, 'native', 'claude', binding_id + '.sqlite3.bootstrap-config'),
            os.path.join(self.root, 'native', 'pi', binding_id + '.jsonl'),
            os.path.join(self.root, 'native', 'codex', binding_id),
        ):
            real = os.path.realpath(path)
            if os.path.commonpath((root, real)) != root: continue
            if os.path.islink(path):
                os.unlink(path)
                continue
            # Do not follow a redirected ancestor to another binding's data.
            expected = os.path.join(root, os.path.relpath(path, self.root))
            if real != expected: continue
            try:
                if os.path.isdir(real): shutil.rmtree(real)
                else: os.unlink(real)
            except FileNotFoundError: pass

    def _rollback_new(self, conversation_id, binding_id):
        """Remove only paths derived from this newly allocated conversation/binding."""
        root = os.path.realpath(self.root)
        self._rollback_binding(binding_id)
        for path in (os.path.dirname(self.store.path(conversation_id)),):
            real = os.path.realpath(path)
            if os.path.commonpath((root, real)) != root:
                continue
            if real != os.path.join(root, os.path.relpath(path, self.root)):
                continue
            try:
                if os.path.isdir(real): shutil.rmtree(real)
                else: os.unlink(real)
            except FileNotFoundError:
                pass
