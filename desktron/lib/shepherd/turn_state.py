"""Version 2 Shepherd state: serial turns, not a commit ledger.

The model separates execution certainty from historical availability.  A settled
turn may have an unknown outcome and still be safe model history.  Native text
never lives here; turns contain only source references and fingerprints.
"""
import copy
import hashlib
import json
import os
import time

from .store import InvalidState, valid_id

VERSION = 2
OUTCOMES = frozenset(('completed', 'failed', 'interrupted', 'unknown'))
HISTORY_STATES = frozenset(('pending', 'available', 'omitted'))
PHASES = frozenset(('reserved', 'dispatched', 'settled'))
BINDING_STATUSES = frozenset(('current', 'retired', 'retargeted',
                              'quarantined', 'recovery_required'))
WRITER_KINDS = frozenset(('in_process', 'process'))
WRITER_STATUSES = frozenset(('starting', 'active', 'stopped', 'dead', 'unknown'))
_SOURCE_KEYS = frozenset(('kind', 'engine', 'session_id', 'entry_id', 'source_key'))
_HEX = frozenset('0123456789abcdef')


def _valid_hash(value, label):
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in _HEX for char in value)):
        raise InvalidState(label + ' 无效')
    return value


def _validate_source(source, label='Shepherd 来源'):
    if not isinstance(source, dict) or set(source) != _SOURCE_KEYS:
        raise InvalidState(label + '引用无效')
    for key in _SOURCE_KEYS:
        valid_id(source.get(key), f'{label}.{key}')
    return source


def _validate_writer(writer, label):
    if writer is None:
        return
    if (not isinstance(writer, dict)
            or writer.get('kind') not in WRITER_KINDS
            or writer.get('status') not in WRITER_STATUSES
            or not isinstance(writer.get('boot_id'), str)):
        raise InvalidState(label + ' Writer 无效')
    process = writer.get('process')
    if process is not None and not isinstance(process, dict):
        raise InvalidState(label + ' Writer 进程身份无效')


def digest(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def turn_hash(turn):
    return digest({key: value for key, value in turn.items()
                   if key not in ('turn_hash', 'settled_at')})


def initial(conversation_id, mode='active'):
    if mode not in ('active', 'shadow'):
        raise InvalidState('Shepherd mode 无效')
    return {
        'version': VERSION,
        'conversation_id': valid_id(conversation_id, 'conversation_id'),
        'mode': mode,
        'producer_generation': 0,
        'active_binding_id': '',
        'active_turn_id': '',
        'active_turn': None,
        'pending_switch': None,
        'switch_history': [],
        'bindings': {},
        'turns': [],
        'checkpoints': {},
        'audit_events': [],
        'legacy_recoveries': [],
        'tombstone': None,
        'shadow_cursor': 0,
        'updated_at': 0,
    }


def validate(state, conversation_id=None):
    if not isinstance(state, dict) or state.get('version') != VERSION:
        raise InvalidState('Shepherd v2 状态版本不受支持')
    cid = valid_id(state.get('conversation_id'), 'conversation_id')
    if conversation_id is not None and cid != conversation_id:
        raise InvalidState('Shepherd 会话身份不匹配')
    if state.get('mode') not in ('active', 'shadow'):
        raise InvalidState('Shepherd mode 无效')
    tombstone = state.get('tombstone')
    if tombstone is not None:
        if (not isinstance(tombstone, dict)
                or tombstone.get('state') != 'deleted'
                or tombstone.get('reason') != 'user_delete'
                or not isinstance(tombstone.get('deleted_at'), (int, float))):
            raise InvalidState('Shepherd 删除墓碑无效')
        valid_id(tombstone.get('audit_event_id'), 'tombstone.audit_event_id')
        switch_id = tombstone.get('abandoned_switch_id')
        if switch_id:
            valid_id(switch_id, 'tombstone.abandoned_switch_id')
    turns = state.get('turns')
    bindings = state.get('bindings')
    checkpoints = state.get('checkpoints')
    switch_history = state.get('switch_history')
    audit_events = state.get('audit_events')
    legacy_recoveries = state.get('legacy_recoveries')
    if (not isinstance(turns, list) or not isinstance(bindings, dict)
            or not isinstance(checkpoints, dict) or not isinstance(switch_history, list)
            or not isinstance(audit_events, list) or not isinstance(legacy_recoveries, list)):
        raise InvalidState('Shepherd v2 状态结构无效')
    turn_ids = set()
    source_keys = set()
    for seq, row in enumerate(turns, 1):
        _validate_turn(row, cid, seq, settled=True)
        if row['turn_id'] in turn_ids:
            raise InvalidState('Shepherd Turn ID 重复')
        turn_ids.add(row['turn_id'])
        if row['history_state'] == 'available':
            key = row['source_ref']['source_key']
            if key in source_keys:
                raise InvalidState('Shepherd Turn 来源重复')
            source_keys.add(key)
    active = state.get('active_turn')
    active_id = state.get('active_turn_id')
    if active is None:
        if active_id:
            raise InvalidState('Shepherd Active Turn 指针悬空')
    else:
        _validate_turn(active, cid, len(turns) + 1, settled=False)
        if active_id != active.get('turn_id'):
            raise InvalidState('Shepherd Active Turn 指针不匹配')
    generation = state.get('producer_generation')
    if not isinstance(generation, int) or generation < 0:
        raise InvalidState('Shepherd Producer Generation 无效')
    active_binding = state.get('active_binding_id')
    if not isinstance(active_binding, str) or active_binding and active_binding not in bindings:
        raise InvalidState('Shepherd Active Binding 无效')
    if active and (active.get('producer_binding_id') != active_binding
                   or active.get('producer_generation') != generation):
        raise InvalidState('Shepherd Active Turn Producer 无效')
    current_engines = set()
    for binding_id, row in bindings.items():
        valid_id(binding_id, 'binding_id')
        if (not isinstance(row, dict) or row.get('binding_id') != binding_id
                or row.get('conversation_id') != cid or row.get('owner') != 'shepherd'):
            raise InvalidState('Shepherd Binding 无效')
        for key in ('engine','native_session_id','connector_version','ownership_nonce'):
            valid_id(row.get(key), 'binding.' + key)
        if (not isinstance(row.get('native_cwd'), str)
                or not os.path.isabs(row['native_cwd'])):
            raise InvalidState('Shepherd Binding 工作目录无效')
        status = row.get('status')
        if status not in BINDING_STATUSES:
            raise InvalidState('Shepherd Binding 状态无效')
        if status == 'current':
            if row['engine'] in current_engines:
                raise InvalidState('Shepherd 同引擎存在多个当前 Binding')
            current_engines.add(row['engine'])
        imported = row.get('imported_through_seq')
        if not isinstance(imported, int) or not 0 <= imported <= len(turns):
            raise InvalidState('Shepherd Binding 追平游标无效')
        expected = '' if imported == 0 else turns[imported - 1]['turn_hash']
        if row.get('imported_turn_hash', '') != expected:
            raise InvalidState('Shepherd Binding 追平指纹无效')
        base = row.get('base_checkpoint_id')
        if base and base not in checkpoints:
            raise InvalidState('Shepherd Binding Checkpoint 不存在')
        if not isinstance(row.get('native_revision', ''), str):
            raise InvalidState('Shepherd Binding Revision 无效')
        policy = row.get('prompt_snapshot_policy')
        prompt_hash = row.get('prompt_snapshot_sha256')
        if bool(policy) != bool(prompt_hash):
            raise InvalidState('Shepherd Binding 静态提示身份不完整')
        if policy:
            valid_id(policy, 'binding.prompt_snapshot_policy')
            if (not isinstance(prompt_hash, str) or len(prompt_hash) != 64
                    or any(char not in '0123456789abcdef' for char in prompt_hash)):
                raise InvalidState('Shepherd Binding 静态提示摘要无效')
            if row.get('engine') != 'claude':
                raise InvalidState('非 Claude Binding 不能声明提示快照策略')
    if active_binding and (bindings[active_binding]['status'] != 'current'
                           or bindings[active_binding]['imported_through_seq'] != len(turns)):
        raise InvalidState('Shepherd Active Producer 已隔离或尚未追平')
    pending = state.get('pending_switch')
    if pending is not None:
        if (not isinstance(pending, dict)
                or pending.get('source_binding_id') not in bindings
                or pending.get('target_binding_id') not in bindings
                or pending.get('source_binding_id') == pending.get('target_binding_id')
                or pending.get('generation') != generation
                or pending.get('state') != 'catching_up'
                or not isinstance(pending.get('through_seq'), int)
                or not 0 <= pending['through_seq'] <= len(turns)
                or active_binding or active):
            raise InvalidState('Shepherd Pending Switch 无效')
        valid_id(pending.get('switch_id'), 'switch_id')
        plan = pending.get('plan'); operation = pending.get('operation')
        if (not isinstance(plan, dict)
                or plan.get('strategy') not in ('incremental','checkpoint')
                or not isinstance(plan.get('from_seq'), int)
                or not isinstance(plan.get('through_seq'), int)
                or plan['through_seq'] != pending['through_seq']
                or not 1 <= plan['from_seq'] <= pending['through_seq'] + 1
                or not isinstance(plan.get('target_start_seq'), int)
                or not 0 <= plan['target_start_seq'] <= pending['through_seq']
                or not isinstance(plan.get('target_start_turn_hash', ''), str)
                or not isinstance(plan.get('target_start_revision', ''), str)):
            raise InvalidState('Shepherd Pending Switch Plan 无效')
        if plan['strategy'] == 'checkpoint':
            checkpoint = plan.get('checkpoint')
            if (not isinstance(checkpoint, dict)
                    or checkpoint.get('checkpoint_id') not in checkpoints
                    or checkpoint.get('covers_through_seq') != plan['from_seq'] - 1):
                raise InvalidState('Shepherd Pending Switch Checkpoint 无效')
        if (not isinstance(operation, dict)
                or operation.get('kind') != 'switch_write'
                or operation.get('phase') not in ('fenced','checkpoint_writing',
                    'checkpoint_applied','append_writing','append_applied')
                or not isinstance(operation.get('operation_id'), str)):
            raise InvalidState('Shepherd Pending Switch Operation 无效')
        _validate_writer(operation.get('writer'), 'Shepherd Pending Switch')
        retarget = pending.get('retarget')
        if retarget is not None:
            if (not isinstance(retarget, dict)
                    or retarget.get('phase') not in ('reserved','native_created')
                    or not isinstance(retarget.get('selection_epoch'), int)
                    or retarget['selection_epoch'] < 0):
                raise InvalidState('Shepherd Pending Retarget 无效')
            for key in ('retarget_id','engine','binding_id','ownership_nonce','boot_id'):
                valid_id(retarget.get(key), 'retarget.' + key)
            if not isinstance(retarget.get('native_cwd'), str) or not os.path.isabs(retarget['native_cwd']):
                raise InvalidState('Shepherd Pending Retarget 工作目录无效')
            _validate_writer(retarget.get('writer'), 'Shepherd Pending Retarget')
            if retarget['phase'] == 'native_created':
                replacement = retarget.get('binding')
                if (not isinstance(replacement, dict)
                        or replacement.get('binding_id') != retarget['binding_id']
                        or replacement.get('conversation_id') != cid):
                    raise InvalidState('Shepherd Pending Retarget Binding 无效')
    for checkpoint_id, row in checkpoints.items():
        valid_id(checkpoint_id, 'checkpoint_id')
        cover = row.get('covers_through_seq') if isinstance(row, dict) else None
        if (row.get('checkpoint_id') != checkpoint_id or not isinstance(cover, int)
                or not 0 <= cover <= len(turns)
                or row.get('portability') not in ('local','portable')
                or not isinstance(row.get('estimated_tokens'), int)
                or row['estimated_tokens'] < 0):
            raise InvalidState('Shepherd Checkpoint 无效')
        _validate_source(row.get('source'), 'Shepherd Checkpoint 来源')
        _valid_hash(row.get('source_hash'), 'Shepherd Checkpoint Source Hash')
        _valid_hash(row.get('public_hash'), 'Shepherd Checkpoint Public Hash')
        valid_id(row.get('projection_codec_version'), 'projection_codec_version')
        expected = '' if cover == 0 else turns[cover - 1]['turn_hash']
        if row.get('covers_turn_hash', '') != expected:
            raise InvalidState('Shepherd Checkpoint 覆盖指纹无效')
    for row in audit_events:
        if not isinstance(row, dict) or not isinstance(row.get('kind'), str):
            raise InvalidState('Shepherd Audit Event 无效')
        if row.get('event_id'):
            valid_id(row['event_id'], 'audit.event_id')
    if tombstone:
        audit = next((row for row in audit_events
                      if row.get('event_id') == tombstone['audit_event_id']), None)
        abandoned = tombstone.get('abandoned_switch_id') or ''
        archived = next((row for row in switch_history
                         if row.get('switch_id') == abandoned), None) if abandoned else None
        if (not audit or audit.get('kind') != 'public_conversation_deleted'
                or (audit.get('abandoned_switch_id') or '') != abandoned
                or (abandoned and (not archived
                    or archived.get('state') != 'abandoned_by_delete'))
                or state.get('pending_switch')):
            raise InvalidState('Shepherd 删除墓碑证据不一致')
    seen_recoveries = set()
    for row in legacy_recoveries:
        if (not isinstance(row, dict) or not row.get('recovery_id')
                or row['recovery_id'] in seen_recoveries
                or row.get('source_binding_id') not in bindings
                or row.get('resume_binding_id') not in bindings
                or not isinstance(row.get('insert_seq'), int)
                or not 1 <= row['insert_seq'] <= len(turns) + 1
                or not isinstance(row.get('inflight'), dict)):
            raise InvalidState('Shepherd Legacy Recovery 无效')
        valid_id(row['recovery_id'], 'legacy_recovery_id')
        seen_recoveries.add(row['recovery_id'])
    if legacy_recoveries and active_binding:
        raise InvalidState('Shepherd 历史恢复期间不能持有 Active Producer')
    return state


def _validate_turn(row, conversation_id, seq, *, settled):
    if not isinstance(row, dict) or row.get('seq') != seq:
        raise InvalidState('Shepherd Turn 顺序不连续')
    valid_id(row.get('turn_id'), 'turn_id')
    phase = row.get('phase')
    history = row.get('history_state')
    outcome = row.get('outcome')
    if phase not in PHASES or history not in HISTORY_STATES or outcome not in OUTCOMES:
        raise InvalidState('Shepherd Turn 状态无效')
    if settled:
        if phase != 'settled' or history == 'pending':
            raise InvalidState('Shepherd 历史 Turn 尚未收口')
        if row.get('turn_hash') != turn_hash(row):
            raise InvalidState('Shepherd Turn 指纹不匹配')
        if history == 'available':
            source = row.get('source_ref')
            _validate_source(source, 'Shepherd Turn 来源')
            if row.get('source_key') != source['source_key']:
                raise InvalidState('Shepherd Turn 来源索引不匹配')
            for key in ('source_hash', 'public_hash'):
                _valid_hash(row.get(key), 'Shepherd Turn ' + key)
    elif phase == 'settled' or history != 'pending' or outcome != 'unknown':
        raise InvalidState('Shepherd Active Turn 状态无效')
    if not settled:
        valid_id(row.get('attempt_id'), 'attempt_id')
        recovery = row.get('recovery')
        if recovery:
            if (not isinstance(recovery, dict) or recovery.get('phase') != phase
                    or not isinstance(recovery.get('boot_id'), str) or not recovery['boot_id']):
                raise InvalidState('Shepherd Active Turn Recovery 无效')
            for key in ('boundary', 'native_locator', 'process'):
                if key in recovery and not isinstance(recovery[key], dict):
                    raise InvalidState('Shepherd Active Turn Recovery 证据无效')
    if not isinstance(row.get('unfinished_tool_ids', []), list):
        raise InvalidState('Shepherd 未完成工具列表无效')
    input_ref = row.get('input_ref')
    if input_ref is not None:
        if (not isinstance(input_ref, dict)
                or set(input_ref) != {'conversation_id','turn_id','input_hash'}
                or input_ref.get('conversation_id') != conversation_id
                or input_ref.get('turn_id') != row.get('turn_id')
                or not isinstance(input_ref.get('input_hash'), str)
                or len(input_ref['input_hash']) != 64):
            raise InvalidState('Shepherd Turn Input 引用无效')


def validate_v1(old):
    """Validate the evidence-bearing v1 fields before converting the schema."""
    if not isinstance(old, dict) or old.get('version') != 1:
        raise InvalidState('Shepherd v1 状态无效')
    cid = valid_id(old.get('conversation_id'), 'conversation_id')
    rounds = old.get('rounds'); bindings = old.get('bindings')
    checkpoints = old.get('checkpoints'); sources = old.get('source_index')
    if (not isinstance(rounds, list) or not isinstance(bindings, dict)
            or not isinstance(checkpoints, dict) or not isinstance(sources, dict)):
        raise InvalidState('Shepherd v1 状态结构无效')
    if old.get('committed_offset') != len(rounds):
        raise InvalidState('Shepherd v1 committed_offset 不连续')
    parent = ''; expected_sources = {}
    for offset, row in enumerate(rounds, 1):
        if (not isinstance(row, dict) or row.get('offset') != offset
                or row.get('round_id') != f'{cid}:{offset}'
                or row.get('parent_hash') != parent):
            raise InvalidState('Shepherd v1 Manifest 链断裂')
        expected = digest({key:value for key,value in row.items() if key != 'entry_hash'})
        if row.get('entry_hash') != expected:
            raise InvalidState('Shepherd v1 Manifest 内容校验失败')
        source_key = row.get('source_key')
        if not isinstance(source_key, str) or not source_key or source_key in expected_sources:
            raise InvalidState('Shepherd v1 来源索引无效')
        for key in ('source_hash','public_hash'):
            value = row.get(key)
            if (not isinstance(value, str) or len(value) != 64
                    or any(c not in '0123456789abcdef' for c in value)):
                raise InvalidState('Shepherd v1 Round Hash 无效')
        if row.get('terminal_state') not in (
                'completed','failed_recoverable','interrupted_confirmed'):
            raise InvalidState('Shepherd v1 Round 终态无效')
        expected_sources[source_key] = offset; parent = expected
    if old.get('manifest_head_hash') != parent or sources != expected_sources:
        raise InvalidState('Shepherd v1 Manifest Head 或来源索引不匹配')
    committed = len(rounds)
    for binding_id, row in bindings.items():
        valid_id(binding_id, 'binding_id')
        applied = row.get('applied_offset') if isinstance(row, dict) else None
        if (row.get('binding_id') != binding_id or row.get('conversation_id') != cid
                or row.get('owner') != 'shepherd' or not isinstance(applied, int)
                or not 0 <= applied <= committed):
            raise InvalidState('Shepherd v1 Binding 无效')
    inflight = old.get('inflight')
    if inflight is not None:
        if (not isinstance(inflight, dict)
                or inflight.get('binding_id') not in bindings
                or inflight.get('offset') != committed + 1
                or inflight.get('round_id') != f'{cid}:{committed + 1}'
                or inflight.get('state') not in ('running','source_persisted','needs_reconciliation')):
            raise InvalidState('Shepherd v1 Inflight Round 无效')
    return old


def migrate_v1(old):
    """Pure, deterministic v1→v2 conversion; never mutates *old*.

    An unresolved v1 inflight stays active. Migration does not infer process
    death, native completion, or historical availability.
    """
    if not isinstance(old, dict):
        raise InvalidState('Shepherd v1 状态无效')
    if old.get('version') == VERSION:
        out = copy.deepcopy(old)
        return validate(out, out.get('conversation_id'))
    validate_v1(old)
    cid = valid_id(old.get('conversation_id'), 'conversation_id')
    out = initial(cid, old.get('mode', 'active'))
    out['producer_generation'] = int(old.get('producer_generation') or 0)
    out['active_binding_id'] = str(old.get('active_binding_id') or '')
    out['shadow_cursor'] = int(old.get('shadow_cursor') or 0)
    out['updated_at'] = old.get('updated_at') or 0
    terminal = {'completed':'completed', 'failed_recoverable':'failed',
                'interrupted_confirmed':'interrupted'}
    old_entry_to_turn = {}
    for seq, row in enumerate(old.get('rounds') or [], 1):
        turn = {
            'turn_id': f'{cid}:{seq}', 'seq': seq,
            'kind': row.get('kind', 'user_turn'),
            'producer_binding_id': row.get('producer_binding_id', ''),
            'producer_generation': int(row.get('producer_generation') or 0),
            'phase': 'settled',
            'outcome': terminal.get(row.get('terminal_state'), 'unknown'),
            'history_state': 'available',
            'source_ref': copy.deepcopy(row.get('source')),
            'source_key': str(row.get('source_key') or ''),
            'source_hash': str(row.get('source_hash') or ''),
            'public_hash': str(row.get('public_hash') or ''),
            'native_revision': str(row.get('native_revision') or ''),
            'estimated_tokens': int(row.get('estimated_tokens') or 0),
            'unfinished_tool_ids': [],
            'attempt_id': str(row.get('attempt_id') or ''),
            'settled_at': row.get('committed_at') or 0,
            'legacy_entry_hash': str(row.get('entry_hash') or ''),
        }
        turn['turn_hash'] = turn_hash(turn)
        out['turns'].append(turn)
        old_entry_to_turn[turn['legacy_entry_hash']] = turn['turn_hash']
    for binding_id, row in (old.get('bindings') or {}).items():
        binding = copy.deepcopy(row)
        imported = int(binding.pop('applied_offset', 0) or 0)
        binding.pop('applied_event_hash', None)
        binding['imported_through_seq'] = imported
        binding['imported_turn_hash'] = '' if imported == 0 else out['turns'][imported - 1]['turn_hash']
        out['bindings'][binding_id] = binding
    for checkpoint_id, row in (old.get('checkpoints') or {}).items():
        checkpoint = copy.deepcopy(row)
        cover = int(checkpoint.pop('covers_through_offset', 0) or 0)
        checkpoint.pop('covers_hash', None)
        checkpoint['covers_through_seq'] = cover
        checkpoint['covers_turn_hash'] = '' if cover == 0 else out['turns'][cover - 1]['turn_hash']
        out['checkpoints'][checkpoint_id] = checkpoint
    out['switch_history'] = [_migrate_switch(row) for row in old.get('switch_history') or []]
    if old.get('pending_switch'):
        out['pending_switch'] = _migrate_switch(old['pending_switch'])
        plan = out['pending_switch'].get('plan') or {}
        start = plan.get('target_start_seq', 0)
        plan['target_start_turn_hash'] = '' if start == 0 else out['turns'][start - 1]['turn_hash']
        checkpoint = plan.get('checkpoint')
        if isinstance(checkpoint, dict):
            checkpoint_id = checkpoint.get('checkpoint_id')
            if checkpoint_id in out['checkpoints']:
                plan['checkpoint'] = copy.deepcopy(out['checkpoints'][checkpoint_id])
    inflight = old.get('inflight')
    if inflight:
        phase = ((inflight.get('recovery') or {}).get('phase') or 'reserved')
        phase = 'dispatched' if phase == 'dispatched' else 'reserved'
        active = {
            'turn_id': f'{cid}:{len(out["turns"]) + 1}',
            'seq': len(out['turns']) + 1,
            'kind': inflight.get('kind', 'user_turn'),
            'producer_binding_id': inflight.get('binding_id', ''),
            'producer_generation': int(inflight.get('generation') or 0),
            'phase': phase, 'outcome': 'unknown', 'history_state': 'pending',
            'attempt_id': str(inflight.get('attempt_id') or ''),
            'input_ref': copy.deepcopy(inflight.get('input_ref')),
            'recovery': copy.deepcopy(inflight.get('recovery') or {}),
            'unfinished_tool_ids': [],
            'started_at': inflight.get('started_at') or 0,
        }
        if inflight.get('source'):
            active['staged_source_ref'] = copy.deepcopy(inflight['source'])
            active['staged_native_revision'] = str(inflight.get('native_revision') or '')
            active['staged_outcome'] = terminal.get(inflight.get('terminal_state'), 'unknown')
        reason = inflight.get('reconciliation_reason')
        if reason:
            active['recovery_reason'] = str(reason)
        out['active_turn'] = active
        out['active_turn_id'] = active['turn_id']
    for row in old.get('reconciliations') or []:
        inflight_row = copy.deepcopy(row.get('inflight') or {})
        event_id = str(row.get('reconciliation_id') or '')
        out['audit_events'].append({
            'event_id': event_id,
            'kind': str(row.get('action') or 'legacy_reconciliation'),
            'attempt_id': str(inflight_row.get('attempt_id') or ''),
            'at': row.get('created_at') or 0,
        })
        # Old preserve_and_end skipped the entire uncertain turn. Keep it as a
        # deferred recovery item so v2 can verify and insert the native suffix
        # at its chronological position instead of silently preserving the gap.
        if row.get('action') == 'preserve_and_end' and inflight_row:
            attempt = str(inflight_row.get('attempt_id') or '')
            if attempt and not any(turn.get('attempt_id') == attempt for turn in out['turns']):
                at = float(inflight_row.get('started_at') or row.get('created_at') or 0)
                insert_seq = 1 + sum(float(turn.get('settled_at') or 0) < at
                                     for turn in out['turns'])
                recovery_id = event_id or ('legacy_recovery_' + attempt)
                valid_id(recovery_id, 'legacy_recovery_id')
                pending_target = ((old.get('pending_switch') or {}).get('target_binding_id')
                                  if (old.get('pending_switch') or {}).get('reconciliation_id') == event_id
                                  else '')
                out['legacy_recoveries'].append({
                    'recovery_id': recovery_id,
                    'source_binding_id': inflight_row.get('binding_id') or '',
                    'resume_binding_id': str(old.get('active_binding_id') or
                                             row.get('replacement_binding_id') or pending_target or ''),
                    'insert_seq': insert_seq,
                    'turn_id': f'{cid}:recovered:{attempt}',
                    'inflight': inflight_row,
                    'created_at': row.get('created_at') or 0,
                })
    if out['legacy_recoveries']:
        active_id = out.get('active_binding_id')
        resume_ids = {row.get('resume_binding_id') for row in out['legacy_recoveries']}
        for binding_id in resume_ids | {active_id}:
            if binding_id in out['bindings']:
                out['bindings'][binding_id]['status'] = 'recovery_required'
        # A v1 reconciliation switch imported only the old committed prefix;
        # retaining it would race or duplicate the v2 full-history rebuild.
        if (out.get('pending_switch') or {}).get('reconciliation_id'):
            out['audit_events'].append({'event_id':'', 'kind':'legacy_pending_switch_superseded',
                                        'at':old.get('updated_at') or 0})
            out['pending_switch'] = None
        out['producer_generation'] += 1
        out['active_binding_id'] = ''
    return validate(out, cid)


def _migrate_switch(row):
    out = copy.deepcopy(row)
    if 'through_offset' in out:
        out['through_seq'] = out.pop('through_offset')
    plan = out.get('plan')
    if isinstance(plan, dict):
        for old, new in (('from_offset','from_seq'), ('through_offset','through_seq'),
                         ('target_start_offset','target_start_seq')):
            if old in plan:
                plan[new] = plan.pop(old)
        plan.pop('target_start_hash', None)
        checkpoint = plan.get('checkpoint')
        if isinstance(checkpoint, dict) and 'covers_through_offset' in checkpoint:
            checkpoint['covers_through_seq'] = checkpoint.pop('covers_through_offset')
            checkpoint['covers_turn_hash'] = checkpoint.pop('covers_hash', '')
    return out


def settle(active, *, source_ref=None, source_hash='', public_hash='', outcome='unknown',
           history_state='available', unfinished_tool_ids=(), native_revision='',
           recovery_reason='', settled_at=None):
    """Return an immutable settled turn from an active reservation."""
    if not isinstance(active, dict) or active.get('phase') not in ('reserved', 'dispatched'):
        raise InvalidState('Shepherd Active Turn 不能收口')
    if outcome not in OUTCOMES or history_state not in ('available', 'omitted'):
        raise InvalidState('Shepherd Turn 收口语义无效')
    row = copy.deepcopy(active)
    unfinished = list(dict.fromkeys(str(value) for value in unfinished_tool_ids if value))
    row.update(phase='settled', outcome=outcome, history_state=history_state,
               unfinished_tool_ids=unfinished,
               native_revision=str(native_revision or ''),
               recovery_reason=str(recovery_reason or '')[:240],
               settled_at=time.time() if settled_at is None else settled_at)
    row.pop('recovery', None)
    row.pop('staged_source_ref', None)
    row.pop('staged_native_revision', None)
    row.pop('staged_outcome', None)
    if history_state == 'available':
        row.update(source_ref=copy.deepcopy(source_ref),
                   source_key=str((source_ref or {}).get('source_key') or ''),
                   source_hash=str(source_hash or ''), public_hash=str(public_hash or ''))
    else:
        row.pop('source_ref', None)
        row.pop('source_key', None)
        row.pop('source_hash', None)
        row.pop('public_hash', None)
    row['turn_hash'] = turn_hash(row)
    turn_id = str(row.get('turn_id') or '')
    conversation_id = (turn_id.split(':recovered:', 1)[0]
                       if ':recovered:' in turn_id else turn_id.rsplit(':', 1)[0])
    _validate_turn(row, conversation_id, row['seq'], settled=True)
    return row
