"""Read model working history, never the append-only public transcript.

Callers must fence the writer and verify the source/coverage before adopting a
snapshot. This module only reads bytes: it does not resume a thread or execute
tools. Native replacement records replace the prefix rather than append to it.
"""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import hashlib
import json
import os
import stat

from .store import SourceVerificationError


class WorkingContextUnavailable(SourceVerificationError):
    """A verified rollout cannot supply a reusable cache; not source corruption.

    Only the post-terminal exporter may recover from this class, after validating
    the independent journal. Identity, bytes, tool pairing and rollback failures
    deliberately remain ordinary SourceVerificationError.
    """
    def __init__(self, reason):
        super().__init__('Shepherd working context unavailable: ' + reason)
        self.reason = reason
        self.raw = None
        self.expanded_raw = None


def budget_items(native):
    """Return the model-visible suffix of a verified Codex working context."""
    if not isinstance(native, dict):
        return list(native or []) if isinstance(native, list) else []
    items = list(native.get('items') or [])
    if (native.get('format') != 'codex-working-context-v1'
            or native.get('compacted') is not True):
        return items
    boundary = next((index for index in range(len(items) - 1, -1, -1)
                     if isinstance(items[index], dict)
                     and items[index].get('type') == 'compaction'), -1)
    return items[boundary:] if boundary >= 0 else items


def read_working_context(path, *, thread_id):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
            raise SourceVerificationError('Shepherd working context is not an owned regular file')
        raw = stream.read()
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise SourceVerificationError('Shepherd working context changed during read')
    if not raw.endswith(b'\n'):
        raise SourceVerificationError('Shepherd working context has an incomplete record')
    try:
        records = [json.loads(line) for line in raw.splitlines()]
    except (ValueError, UnicodeError) as exc:
        raise SourceVerificationError('Shepherd working context contains invalid JSON') from exc
    try:
        snapshot = working_context(records, thread_id=thread_id)
    except WorkingContextUnavailable as exc:
        # Validate the *whole* envelope before permitting cache-only recovery;
        # an early unknown record must not conceal a later identity mismatch.
        if (not records or records[0].get('type') != 'session_meta'
                or any(not isinstance(row, dict) or not isinstance(row.get('type'), str)
                       or not isinstance(row.get('payload'), dict) for row in records)
                or [row['payload'].get('id') for row in records
                    if row.get('type') == 'session_meta'] != [thread_id]):
            raise SourceVerificationError('Shepherd working context thread identity mismatch') from exc
        exc.raw = raw
        raise
    snapshot['rollout_sha256'] = hashlib.sha256(raw).hexdigest()
    return snapshot


def working_context(records, *, thread_id):
    items, model, compacted, seen_meta = [], '', False, False
    incomplete_replacement = False
    unsupported_record = False
    # These records do not replace the model-visible history. Unknown records
    # cannot silently be treated as inert: they may encode a future rollback.
    metadata = {'world_state', 'token_usage_record', 'guardian_assessment',
                'turn_context', 'event_msg', 'inter_agent_communication_metadata'}
    for record in records:
        if (not isinstance(record, dict) or not isinstance(record.get('type'), str)
                or not isinstance(record.get('payload'), dict)):
            raise SourceVerificationError('Shepherd working context record is invalid')
        kind, payload = record.get('type'), record['payload']
        if kind == 'session_meta':
            if seen_meta or payload.get('id') != thread_id:
                raise SourceVerificationError('Shepherd working context thread identity mismatch')
            seen_meta = True
        elif not seen_meta:
            raise SourceVerificationError('Shepherd working context has no session identity')
        elif kind == 'response_item':
            items.append(payload)
            compacted = compacted or payload.get('type') == 'compaction'
        elif kind == 'compacted':
            replacement = payload.get('replacement_history')
            if not isinstance(replacement, list):
                # Some native versions persist the compact notice before the
                # complete history_mutation/replace. Neither notice nor summary
                # text alone proves which tail was retained.
                incomplete_replacement = True
            else:
                items = list(replacement)
                incomplete_replacement = False
            compacted = True
        elif kind == 'history_mutation':
            batch = payload.get('items')
            operation = payload.get('operation', 'append')
            if not isinstance(batch, list) or operation not in ('append', 'replace'):
                raise SourceVerificationError('Shepherd working history mutation is unsupported')
            items = list(batch) if operation == 'replace' else items + batch
            if operation == 'replace': incomplete_replacement = False
            compacted = compacted or operation == 'replace'
        elif kind == 'turn_context':
            model = payload.get('model') or model
        elif kind == 'event_msg' and payload.get('type') in ('thread_rolled_back', 'turn_reverted'):
            raise SourceVerificationError('Shepherd working context rollback requires native restoration')
        elif kind not in metadata:
            # Finish validating known records before classifying this as a cache
            # incompatibility. An unknown notice must not hide later tool or
            # identity corruption. The resulting history is never adopted.
            unsupported_record = True
    if not seen_meta:
        raise SourceVerificationError('Shepherd working context has no session identity')
    if any(not isinstance(item, dict) or not isinstance(item.get('type'), str) for item in items):
        raise SourceVerificationError('Shepherd working context item is invalid')
    # Fresh execution receives current system instructions separately. Historical
    # system/developer messages must not override the current configuration.
    items = [item for item in items if item.get('type') != 'compaction_trigger'
             and not (item.get('type') == 'message' and item.get('role') in ('system', 'developer'))]
    pending = {}
    for item in items:
        kind, call_id = item.get('type'), item.get('call_id')
        if kind in ('function_call', 'custom_tool_call'):
            if not isinstance(call_id, str) or not call_id or call_id in pending:
                raise SourceVerificationError('Shepherd working context tool call identity invalid')
            pending[call_id] = kind + '_output'
        elif kind in ('function_call_output', 'custom_tool_call_output'):
            if not isinstance(call_id, str) or pending.pop(call_id, None) != kind:
                raise SourceVerificationError('Shepherd working context tool output is unpaired')
    if pending:
        raise SourceVerificationError('Shepherd working context contains unfinished tools; native evidence retained')
    if incomplete_replacement:
        raise WorkingContextUnavailable('incomplete_compaction_snapshot')
    if unsupported_record:
        raise WorkingContextUnavailable('unsupported_record')
    return {'format': 'codex-working-context-v1', 'model': model,
            'compacted': compacted, 'items': items}
