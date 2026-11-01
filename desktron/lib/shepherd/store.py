"""Durable metadata store for Project Shepherd.

Version 2 stores serial turns and execution fences separately.  Conversation
text remains in native sources; this file contains references and hashes only.
"""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
from contextlib import contextmanager
import copy
import json
import os
import re
import tempfile

from durable_json import locked, read, read_value, write

_VERSION = 2
_ID = re.compile(r'^[A-Za-z0-9_.:-]{1,160}$')


class InvalidState(ValueError):
    pass


class Conflict(RuntimeError):
    pass


class RequestBusy(Conflict):
    """Expected admission refusal, not a server fault or a queued delivery."""
    status = 409
    code = 'session_busy'


class RequestPending(RequestBusy):
    """The previous request is still running or settling; nothing was accepted.

    Unlike an ``uncertain`` receipt this clears without user verification, so
    clients may hold the message and resend once the session reports ready.
    """
    code = 'request_pending'


class SourceVerificationError(Conflict):
    """Persisted native bytes deterministically violate a source contract."""
    pass


class SourceShapeUnsupported(SourceVerificationError):
    """A recognized native record uses a shape the installed parser cannot prove."""
    code = 'source_shape_unsupported'


class PromptNotStarted(Conflict):
    """The native request was not delivered and may be restored to the composer."""
    code = 'prompt_not_started'


def valid_id(value, label='id'):
    if not isinstance(value, str) or value in ('.', '..') or not _ID.fullmatch(value):
        raise InvalidState(f'{label} 无效')
    return value


def initial(conversation_id, mode='shadow'):
    from .turn_state import initial as turn_initial
    return turn_initial(conversation_id, mode)


def validate(state, conversation_id=None):
    from .turn_state import validate as turn_validate
    return turn_validate(state, conversation_id)


def _replace_json(path, value):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.state-', dir=os.path.dirname(path))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        oscompat.fsync_directory(os.path.dirname(path))
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def _legacy_terminal(outcome):
    return {'completed':'completed', 'failed':'failed_recoverable',
            'interrupted':'interrupted_confirmed', 'unknown':'unknown'}.get(outcome, 'unknown')


def _legacy_turn(row):
    out = copy.deepcopy(row)
    out.update(offset=row['seq'], round_id=row['turn_id'],
               entry_hash=row['turn_hash'],
               terminal_state=_legacy_terminal(row['outcome']),
               committed_at=row.get('settled_at', 0))
    if row.get('source_ref') is not None:
        out['source'] = copy.deepcopy(row['source_ref'])
        out['source_key'] = row.get('source_key') or row['source_ref'].get('source_key', '')
    return out


def compatibility_view(state):
    """Temporary read aliases for callers migrating from the v1 ledger API.

    Aliases are never persisted.  New code must use turns/active_turn and
    imported_through_seq directly.
    """
    out = copy.deepcopy(state)
    turns = out.get('turns') or []
    out['committed_offset'] = len(turns)
    out['manifest_head_hash'] = turns[-1]['turn_hash'] if turns else ''
    out['rounds'] = [_legacy_turn(row) for row in turns]
    out['source_index'] = {row.get('source_key'): row['seq'] for row in turns
                           if row.get('source_key')}
    for binding in (out.get('bindings') or {}).values():
        binding['applied_offset'] = binding.get('imported_through_seq', 0)
        binding['applied_event_hash'] = binding.get('imported_turn_hash', '')
    for checkpoint in (out.get('checkpoints') or {}).values():
        checkpoint['covers_through_offset'] = checkpoint.get('covers_through_seq', 0)
        checkpoint['covers_hash'] = checkpoint.get('covers_turn_hash', '')
    pending = out.get('pending_switch')
    if isinstance(pending, dict):
        pending['through_offset'] = pending.get('through_seq', 0)
        plan = pending.get('plan')
        if isinstance(plan, dict):
            plan['from_offset'] = plan.get('from_seq', 1)
            plan['through_offset'] = plan.get('through_seq', pending['through_offset'])
            plan['target_start_offset'] = plan.get('target_start_seq', 0)
            plan['target_start_hash'] = plan.get('target_start_turn_hash', '')
            checkpoint = plan.get('checkpoint')
            if isinstance(checkpoint, dict):
                checkpoint['covers_through_offset'] = checkpoint.get('covers_through_seq', 0)
                checkpoint['covers_hash'] = checkpoint.get('covers_turn_hash', '')
    active = out.get('active_turn')
    if active:
        recovery_state = active.get('recovery_state')
        state_name = ('source_persisted' if recovery_state == 'source_staged' else
                      'needs_reconciliation' if recovery_state == 'blocked' else 'running')
        out['inflight'] = copy.deepcopy(active)
        out['inflight'].update(offset=active['seq'], round_id=active['turn_id'], state=state_name,
                               binding_id=active['producer_binding_id'],
                               generation=active['producer_generation'])
        if active.get('staged_source_ref'):
            out['inflight']['source'] = copy.deepcopy(active['staged_source_ref'])
            out['inflight']['native_revision'] = active.get('staged_native_revision', '')
            out['inflight']['terminal_state'] = _legacy_terminal(
                active.get('staged_outcome', 'unknown'))
    else:
        out['inflight'] = None
    out['reconciliations'] = []
    for row in out.get('audit_events') or []:
        if not row.get('legacy_inflight'):
            continue
        legacy = copy.deepcopy(row)
        legacy['reconciliation_id'] = legacy.get('event_id', '')
        legacy['inflight'] = legacy.pop('legacy_inflight')
        legacy['created_at'] = legacy.get('at', 0)
        out['reconciliations'].append(legacy)
    binding_id = out.get('active_binding_id') or ''
    out['lease'] = ({'lease_id':binding_id, 'binding_id':binding_id,
                     'generation':out.get('producer_generation', 0)}
                    if binding_id else None)
    return out


class Store:
    def __init__(self, root):
        self.root = os.path.abspath(os.fspath(root))
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    def path(self, conversation_id):
        return os.path.join(self.root, valid_id(conversation_id, 'conversation_id'), 'state.json')

    def conversation_ids(self):
        out = []
        try:
            names = os.listdir(self.root)
        except FileNotFoundError:
            return out
        for name in names:
            try:
                valid_id(name, 'conversation_id')
            except InvalidState:
                continue
            if os.path.isfile(self.path(name)) and not os.path.islink(self.path(name)):
                out.append(name)
        return sorted(out)

    def _migrate_locked(self, path, state, conversation_id):
        if state.get('version') == _VERSION:
            validate(state, conversation_id)
            return state
        if state.get('version') != 1:
            raise InvalidState('Shepherd 状态版本不受支持')
        from .turn_state import migrate_v1
        migrated = migrate_v1(state)
        validate(migrated, conversation_id)
        backup = path + '.v1.backup'
        if not os.path.exists(backup):
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            # Preserve exact bytes for rollback and forensic comparison; a
            # re-serialized dict is semantically equal but not a true backup.
            with open(path, 'rb') as source:
                original = source.read()
            fd, temporary = tempfile.mkstemp(prefix='.v1-backup-', dir=os.path.dirname(path))
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(original)
                    stream.flush(); os.fsync(stream.fileno())
                os.replace(temporary, backup)
            finally:
                try: os.unlink(temporary)
                except FileNotFoundError: pass
        # A schema migration is a whole-document replacement, not a merge.
        # Merging disjoint v1/v2 keys would leave two competing state models.
        _replace_json(path, migrated)
        # Callers of change() must keep a Snapshot so durable_json can detect
        # concurrent edits after the schema replacement.
        return read(path)

    def load_raw(self, conversation_id, create=False, mode='shadow', allow_deleted=False):
        path = self.path(conversation_id)
        from .physical_delete import deletion_guard, is_deleted
        deleted = is_deleted(self.root, conversation_id)
        if deleted and not allow_deleted:
            if create:
                raise Conflict('Shepherd Conversation 已删除')
            return None
        state = read_value(path)
        if not state:
            if not create:
                return None
            with deletion_guard(self.root, conversation_id):
                if is_deleted(self.root, conversation_id):
                    raise Conflict('Shepherd Conversation 已删除')
                with locked(path + '.coord'):
                    state = read(path)
                    if not state:
                        state.update(initial(conversation_id, mode))
                        state['updated_at'] = __import__('time').time()
                        write(path, state)
        if state.get('version') != _VERSION:
            with locked(path + '.coord'):
                state = read(path)
                state = self._migrate_locked(path, state, conversation_id)
        validate(state, conversation_id)
        # Fresh parses and locked create/migrate snapshots are call-owned;
        # their nested values are not shared with another reader or cache.
        return dict(state)

    def load(self, conversation_id, create=False, mode='shadow'):
        state = self.load_raw(conversation_id, create=create, mode=mode)
        return compatibility_view(state) if state is not None else None

    @contextmanager
    def change(self, conversation_id, create=False, mode='shadow', allow_deleted=False):
        path = self.path(conversation_id)
        from .physical_delete import deletion_guard, is_deleted
        with deletion_guard(self.root, conversation_id):
            if is_deleted(self.root, conversation_id):
                raise Conflict('Shepherd Conversation 已删除')
            with locked(path + '.coord'):
                state = read(path)
                if not state:
                    if not create:
                        raise InvalidState('Shepherd Conversation 不存在')
                    state.update(initial(conversation_id, mode))
                elif state.get('version') != _VERSION:
                    state = self._migrate_locked(path, state, conversation_id)
                validate(state, conversation_id)
                if state.get('tombstone') and not allow_deleted:
                    raise Conflict('Shepherd Conversation 已删除')
                yield state
                # Compatibility aliases are read-only. Remove any legacy fields a
                # pre-v2 caller or hand-edited test snapshot may have introduced.
                for key in ('lease','committed_offset','manifest_head_hash','rounds',
                            'source_index','inflight','reconciliations'):
                    state.pop(key, None)
                state['updated_at'] = __import__('time').time()
                validate(state, conversation_id)
                write(path, state)
