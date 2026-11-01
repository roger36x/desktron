"""Crash recovery for durable Shepherd inflight rounds.

Recovery never sends a prompt.  It may only commit an existing terminal receipt,
or seal a verified append-only native suffix as ``interrupted_confirmed`` after
the exact old process group is proven dead.  Every other case remains fenced for
explicit reconciliation.
"""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import copy
import ctypes
import os
import signal
import time
import uuid

from .coordinator import digest, estimate_tokens
from .source_receipts import SourceReceipts
from .store import (Conflict, InvalidState, SourceShapeUnsupported,
                    SourceVerificationError)
from .turn_inputs import TurnInputs, has_native_user_input

BOOT_ID = 'boot_' + uuid.uuid4().hex

# Only used for reserved rounds that carry no recovery block at all, so
# boot_id cannot fence them.  Never used to judge a live process.
ORPHAN_RESERVED_SECONDS = 600


def orphan_reserved(inflight, *, now=None):
    """Whether a reserved round is an orphan that no Writer can still be holding.

    A reserved round never reached a Writer, so cancelling it replays no tool.
    Rounds written before the recovery block existed (or migrated in) carry no
    recovery block at all, so boot_id cannot fence them; accept them only once
    they are older than a live begin_round->mark_dispatched window.

    Shared by CrashRecovery.recover_one and RecoveryController.recover_guarded:
    the controller's admission gate and the cancel it guards must not drift
    apart, or the gate rejects rounds the action would have cancelled.
    """
    inflight = inflight or {}
    if (inflight.get('recovery') or {}) or inflight.get('phase') != 'reserved':
        return False
    started = float(inflight.get('started_at') or 0)
    return (time.time() if now is None else float(now)) - started > ORPHAN_RESERVED_SECONDS


def _process_start_token(pid):
    """Return a kernel-derived high-resolution creation token when available."""
    pid = int(pid or 0)
    if pid <= 0:
        return ''
    if oscompat.IS_WINDOWS:
        return oscompat.process_start_token(pid)
    if os.path.isfile(f'/proc/{pid}/stat'):
        try:
            # Field 22 is starttime in clock ticks. Parse after the final ')'
            # because comm may contain spaces or parentheses.
            fields = open(f'/proc/{pid}/stat', encoding='utf-8').read().rpartition(') ')[2].split()
            return 'linux-ticks:' + fields[19]
        except (OSError, IndexError, ValueError):
            return ''
    if getattr(os, 'uname', lambda: ('',))()[0] == 'Darwin':
        try:
            libproc = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
            libproc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int,
                                             ctypes.c_uint64, ctypes.c_void_p,
                                             ctypes.c_int]
            libproc.proc_pidinfo.restype = ctypes.c_int
            raw = ctypes.create_string_buffer(136)  # sizeof(struct proc_bsdinfo)
            size = libproc.proc_pidinfo(pid, 3, 0, raw, len(raw))  # PROC_PIDTBSDINFO
            if size == len(raw):
                sec = int.from_bytes(raw.raw[120:128], 'little')
                usec = int.from_bytes(raw.raw[128:136], 'little')
                return f'darwin-tv:{sec}:{usec}'
        except (OSError, ValueError, AttributeError):
            return ''
    return ''


def process_identity(process):
    """Capture enough identity to reject a reused PID after restart."""
    pid = int(getattr(process, 'pid', process) or 0)
    if pid <= 0:
        return {}
    token = _process_start_token(pid)
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = pid
    return {'pid': pid, 'pgid': int(pgid), 'start_token': token}


def process_state(identity):
    """Return alive/dead/unknown without treating PID reuse as the old process."""
    if not isinstance(identity, dict):
        return 'unknown'
    pid = identity.get('pid')
    pgid = identity.get('pgid')
    token = str(identity.get('start_token') or '')
    if not isinstance(pid, int) or pid <= 0 or not isinstance(pgid, int) or pgid <= 0:
        return 'unknown'
    try:
        os.killpg(pgid, 0)
        group_alive = True
    except ProcessLookupError:
        group_alive = False
    except PermissionError:
        return 'unknown'
    if not group_alive:
        return 'dead'
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        # The old leader is gone. A live group may contain its descendants or
        # may have reused the PGID; exact identity can no longer be proven.
        return 'unknown'
    except PermissionError:
        return 'unknown'
    try:
        if os.getpgid(pid) != pgid:
            return 'unknown'
    except (ProcessLookupError, PermissionError):
        return 'unknown'
    if not token:
        return 'unknown'
    current = _process_start_token(pid)
    if not current:
        return 'unknown'
    # A live process group plus a reused leader PID is ambiguous: it may still
    # contain descendants of the old runner, or be an unrelated reused group.
    # Neither automatic sealing nor signalling is safe.
    return 'alive' if current == token else 'unknown'


def terminate_recorded_process(identity, timeout=3):
    """Stop only the exact recorded process group; fail closed on ambiguity."""
    state = process_state(identity)
    if state == 'dead':
        return True
    if state != 'alive':
        return False
    pgid = identity.get('pgid')
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except (PermissionError, TypeError):
        return False
    deadline = time.monotonic() + max(0, timeout)
    while time.monotonic() < deadline:
        if process_state(identity) == 'dead':
            return True
        time.sleep(.05)
    # The start token was checked immediately before signalling; only that
    # private process group is eligible for the bounded hard stop.
    if process_state(identity) != 'alive':
        return process_state(identity) == 'dead'
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except (PermissionError, TypeError):
        return False
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if process_state(identity) == 'dead':
            return True
        time.sleep(.05)
    return False


def complete_tool_pairs(public):
    """Fail closed when a persisted suffix ends inside a tool call."""
    if not isinstance(public, list) or not public:
        return False
    pending, seen = {}, set()
    for item in public:
        if not isinstance(item, dict):
            return False
        kind = item.get('type')
        if kind in ('function_call', 'custom_tool_call'):
            call_id = str(item.get('call_id') or '')
            if not call_id or call_id in seen:
                return False
            seen.add(call_id)
            pending[call_id] = kind
        elif kind in ('function_call_output', 'custom_tool_call_output'):
            call_id = str(item.get('call_id') or '')
            if call_id not in pending or kind != pending[call_id] + '_output':
                return False
            del pending[call_id]
    return not pending


class ReceiptRecoveryRunner:
    """Read receipts only; ``run`` is an explicit tripwire against replay."""
    def __init__(self, connector, *, attempt_id=None):
        self.connector = connector
        self.binding = connector.binding
        self.attempt_id = attempt_id
        self.receipts = SourceReceipts(connector.store_root)

    def run(self, **_kwargs):
        raise AssertionError('crash recovery must never execute a native turn')

    def _verify_attempt(self, round_id):
        verify = getattr(self.connector, 'verify_recovery_attempt', None)
        if callable(verify) and getattr(self.connector,'unified',False):
            verify(round_id,self.attempt_id)

    def recover_result(self, round_id):
        receipt = self.receipts.read(self.binding, round_id, required=False)
        if receipt is not None:
            self._verify_attempt(round_id)
        restore = getattr(self.connector, 'recover_terminal_receipt', None)
        if receipt is None and callable(restore):
            receipt = (restore(round_id,expected_attempt_id=self.attempt_id)
                       if getattr(self.connector,'unified',False) else restore(round_id))
        locator = (receipt or {}).get('locator') or {}
        terminal = locator.get('terminal_state')
        if terminal not in ('completed', 'failed_recoverable', 'interrupted_confirmed'):
            return None
        return {'source': self.source(round_id), 'terminal_state': terminal,
                'native_revision': str(locator.get('native_revision') or '')}

    def unknown_receipt(self, round_id):
        receipt = self.receipts.read(self.binding, round_id, required=False)
        if receipt is not None:
            self._verify_attempt(round_id)
        locator = (receipt or {}).get('locator') or {}
        return copy.deepcopy(locator) if locator.get('terminal_state') == 'unknown' else None

    def verify_recovery_source(self, round_id):
        self._verify_attempt(round_id)

    def source(self, round_id):
        engine = self.binding['engine']
        session_id = self.binding['native_session_id']
        return {'kind': 'native_round', 'engine': engine, 'session_id': session_id,
                'entry_id': round_id,
                'source_key': f'{engine}:{session_id}:{round_id}'}


class CrashRecovery:
    def __init__(self, service, *, boot_id=BOOT_ID):
        self.service = service
        self.coordinator = service.coordinator
        self.boot_id = str(boot_id)

    def scan(self, conversation_ids=None, *, recover_inflight=True):
        """Recover private state only for explicitly live public Conversations.

        A caller may omit ``conversation_ids`` for offline maintenance/tests. The
        product adapter always passes its public index, so a deleted public entry
        is never resurrected merely because its private evidence is retained.
        """
        reports = []
        ids = (self.service.store.conversation_ids() if conversation_ids is None
               else sorted(set(str(value) for value in conversation_ids)))
        for conversation_id in ids:
            try:
                state = self.coordinator.state(conversation_id)
                if (state or {}).get('tombstone'):
                    continue
                pending = (state or {}).get('pending_switch') or {}
                if pending:
                    # Native writes resume later under the adapter's turn/switch
                    # guards. Discovery identifies whether this is a normal
                    # engine switch or an incomplete-turn recovery rebind.
                    reports.append({'conversation_id': conversation_id,
                        'state': 'recovery_rebind_pending' if pending.get('recovery_turn_id') else 'pending_switch',
                        'switch_id': pending.get('switch_id') or ''})
                elif state and state.get('inflight'):
                    inflight = state.get('inflight') or {}
                    reports.append(self.recover_one(conversation_id) if recover_inflight else {
                        'conversation_id':conversation_id,
                        'state':inflight.get('state') or 'running',
                        'reason':'startup_recovery_pending'})
                else:
                    legacy = (state or {}).get('legacy_recoveries') or []
                    if legacy:
                        reports.append({'conversation_id':conversation_id,
                            'state':'legacy_recovery_required',
                            'recovery_id':legacy[0].get('recovery_id') or ''})
                        continue
                    old = next((row for row in (state or {}).get('bindings', {}).values()
                                if row.get('status') == 'recovery_required'), None)
                    if old:
                        reports.append({'conversation_id':conversation_id,
                            'state':'recovery_binding_required',
                            'binding_id':old.get('binding_id') or ''})
            except Exception as exc:
                reports.append({'conversation_id': conversation_id,
                    'state': 'needs_reconciliation', 'reason': 'startup_scan_failed',
                    'detail': str(exc)[:240]})
        return reports

    def recover_one(self, conversation_id, *, allow_current_boot_commit=False,
                    receipt_only=False):
        state = self.coordinator.state(conversation_id)
        if (state or {}).get('tombstone'):
            return {'conversation_id': conversation_id, 'state': 'deleted'}
        inflight = copy.deepcopy((state or {}).get('inflight') or {})
        if not inflight:
            return {'conversation_id': conversation_id, 'state': 'ready'}
        lease = (state or {}).get('lease') or {}
        binding = (state.get('bindings') or {}).get(inflight.get('binding_id'))
        if not binding or lease.get('binding_id') != binding.get('binding_id'):
            return self._unresolved(conversation_id, inflight, 'binding_or_lease_mismatch')
        recovery = inflight.get('recovery') or {}
        # A guarded caller in the current boot may inspect evidence, but never
        # bypasses process_state: without a receipt, a live/ambiguous Writer is
        # still fenced below. This also closes the receipt-fsync→state-update gap.
        current_boot_safe = bool(allow_current_boot_commit)
        if recovery.get('boot_id') == self.boot_id and not current_boot_safe:
            return {'conversation_id': conversation_id,
                    'state': inflight.get('state') or 'running'}
        stale_reserved = orphan_reserved(inflight)
        if recovery.get('phase') == 'reserved' or stale_reserved:
            if getattr(self.service, 'connector', None) and binding \
                    and str(binding.get('connector_version') or '').startswith('unified-v1:'):
                connector = self.service.connector(binding)
                connector.archive.abandon_predispatch(binding, inflight['round_id'],
                    reason='reserved_crash_recovery', attempt_id=inflight.get('attempt_id'),
                    missing_ok=True)
            self.coordinator.cancel_reserved(
                conversation_id, attempt_id=inflight.get('attempt_id'))
            return {'conversation_id': conversation_id,
                    'state': 'cancelled_before_dispatch',
                    'reason': 'reserved_without_recovery' if stale_reserved else 'reserved'}
        connector = self.service.connector(binding)
        runner = ReceiptRecoveryRunner(connector,attempt_id=inflight.get('attempt_id'))
        from .harness import ExecutionHarness
        harness = ExecutionHarness(self.coordinator, connector, runner)
        persisted = inflight.get('state') == 'source_persisted'
        if (allow_current_boot_commit and getattr(connector, 'unified', False)
                and inflight.get('state') == 'needs_reconciliation'):
            writer = process_state(recovery.get('process'))
            if writer != 'dead':
                return self._unresolved(conversation_id, inflight, 'orphan_process_' + writer)
            try:
                # Revalidate before reopening only the commit stage. A decoder
                # upgrade can repair a projection, never authority or terminal.
                if not inflight.get('staged_source_ref'):
                    # The archive may already have a verified native terminal
                    # even if receipt fsync or staging failed. Never downgrade
                    # that terminal to unknown merely because state is blocked.
                    connector.archive.verify_recovery_boundary(binding, inflight['round_id'],
                        recovery.get('boundary') or {})
                result = runner.recover_result(inflight['round_id'])
                if inflight.get('staged_source_ref') and (
                        not result or result['source'] != inflight.get('source')
                        or result['terminal_state'] != inflight.get('terminal_state')
                        or result['native_revision'] != inflight.get('native_revision')):
                    raise SourceVerificationError('Shepherd staged recovery receipt mismatch')
                if result is not None:
                    connector.resolve_source(result['source'])
                    connector.terminal_locator(inflight['round_id'], result['terminal_state'],
                                               result['native_revision'])
                    self.coordinator.source_persisted(conversation_id, lease['lease_id'], lease['generation'],
                        attempt_id=inflight['attempt_id'], source=result['source'],
                        native_revision=result['native_revision'], terminal_state=result['terminal_state'])
                    persisted = True
            except Exception as exc:
                return self._unresolved(conversation_id, inflight,
                    ('source_shape_unsupported' if isinstance(exc, SourceShapeUnsupported)
                     else 'commit_verification_failed'), detail=str(exc)[:240])
        unknown = runner.unknown_receipt(inflight['round_id'])
        if unknown is not None:
            # A prior unknown receipt is evidence, not a licence to adopt a live
            # or ambiguously owned Writer after an interrupted recovery.
            writer = process_state(recovery.get('process'))
            if writer != 'dead':
                return self._unresolved(conversation_id, inflight, 'orphan_process_' + writer)
            try:
                return self._settle_unknown(conversation_id, inflight, binding, lease,
                                            connector, runner.source(inflight['round_id']), unknown)
            except Exception as exc:
                return self._unresolved(conversation_id, inflight,
                                        'unknown_receipt_readback_failed', detail=str(exc)[:240])
        if receipt_only and not persisted and runner.recover_result(inflight['round_id']) is None:
            return {'conversation_id':conversation_id,
                    'state':inflight.get('state') or 'running',
                    'reason':'terminal_receipt_not_found'}
        try:
            row = harness.recover(conversation_id, lease_id=lease['lease_id'],
                                  generation=lease['generation'])
            report = {'conversation_id': conversation_id, 'state': 'committed',
                      'offset': row['offset'], 'terminal_state': row['terminal_state']}
            if row.get('compatibility'):
                report['compatibility'] = copy.deepcopy(row['compatibility'][:16])
            return report
        except Exception as exc:
            current = self.coordinator.state(conversation_id) or {}
            current_inflight = copy.deepcopy(current.get('inflight') or {})
            if persisted or current_inflight.get('state') == 'source_persisted':
                # A terminal receipt is already bound. Deterministic source
                # contract failures need an explicit isolation exit immediately;
                # dependency/readback failures remain commit-only and retryable.
                if isinstance(exc, SourceVerificationError):
                    reason = ('source_shape_unsupported'
                              if isinstance(exc, SourceShapeUnsupported)
                              else 'commit_verification_failed')
                    current_lease = current.get('lease') or {}
                    self.coordinator.block_staged_source(
                        conversation_id, current_lease.get('lease_id'),
                        current_lease.get('generation'),
                        attempt_id=current_inflight.get('attempt_id'),
                        reason=reason, detail=str(exc)[:240])
                    return {'conversation_id':conversation_id,
                            'state':'needs_reconciliation',
                            'reason':reason,
                            'detail':str(exc)[:240]}
                return {'conversation_id': conversation_id,
                        'state': 'source_persisted',
                        'reason': 'commit_verification_failed',
                        'detail': str(exc)[:240]}
            # Missing, stale or undecodable receipts continue through evidence
            # inspection only during abnormal-start recovery. Continuous recovery
            # is receipt-only and leaves uncertain execution fenced unchanged.
            if receipt_only:
                return {'conversation_id':conversation_id,
                        'state':current_inflight.get('state') or 'running',
                        'reason':'commit_verification_failed',
                        'detail':str(exc)[:240]}
        state = self.coordinator.state(conversation_id)
        inflight = copy.deepcopy((state or {}).get('inflight') or {})
        recovery = inflight.get('recovery') or {}
        phase = recovery.get('phase')
        if phase != 'dispatched':
            return self._unresolved(conversation_id, inflight,
                                    'dispatch_not_durably_confirmed')
        old_process = process_state(recovery.get('process'))
        if old_process != 'dead':
            return self._unresolved(conversation_id, inflight,
                                    'orphan_process_' + old_process)
        recover_suffix = getattr(connector, 'recover_suffix', None)
        if not callable(recover_suffix):
            return self._unresolved(conversation_id, inflight,
                                    'native_suffix_recovery_unavailable')
        try:
            ensure_input = getattr(self.service, 'ensure_active_input_ref', None)
            input_ref = (ensure_input(conversation_id, inflight)
                         if callable(ensure_input) else inflight.get('input_ref'))
            trusted_input = (TurnInputs(self.service.root).read(input_ref)
                             if input_ref else None)
            args = (copy.deepcopy(recovery.get('boundary') or {}),
                    copy.deepcopy(recovery.get('native_locator') or {}))
            recovered = (recover_suffix(*args, trusted_public=trusted_input,
                                         expected_attempt_id=inflight.get('attempt_id'))
                         if getattr(connector,'unified',False)
                         else recover_suffix(*args, trusted_public=trusted_input)
                         if binding.get('engine') == 'codex'
                         else recover_suffix(*args))
            required = {'locator','source','public','unfinished_tool_ids'}
            allowed = required | {'compatibility'}
            compatibility = recovered.get('compatibility', []) if isinstance(recovered, dict) else []
            valid_diagnostic = lambda item: (isinstance(item, dict)
                and set(item) == {'engine','action','kind','count'}
                and all(isinstance(item.get(key), str) and 0 < len(item[key]) <= limit
                        for key, limit in (('engine',32),('action',64),('kind',96)))
                and isinstance(item.get('count'), int) and not isinstance(item.get('count'), bool)
                and 0 < item['count'] <= 1000000)
            if (not isinstance(recovered, dict)
                    or not required.issubset(recovered) or set(recovered) - allowed
                    or not isinstance(recovered['public'], list)
                    or not isinstance(recovered['unfinished_tool_ids'], list)
                    or not isinstance(compatibility, list)
                    or len(compatibility) > 16
                    or any(not valid_diagnostic(item) for item in compatibility)):
                raise InvalidState('Shepherd 不完整来源恢复格式无效')
            locator = copy.deepcopy(recovered['locator'])
            if getattr(connector, 'unified', False):
                from .unified_archive import UnifiedArchive
                source = recovered['source']
                if (source.get('turn_id') != inflight['round_id']
                        or locator.get('turn_id') != inflight['round_id']
                        or source.get('identity') != UnifiedArchive.identity(binding)
                        or locator.get('native_revision') != digest(source)):
                    raise SourceVerificationError('Shepherd recovery source does not match active turn')
            locator['terminal_state'] = 'unknown'
            locator['unfinished_tool_ids'] = copy.deepcopy(recovered['unfinished_tool_ids'])
            SourceReceipts(self.service.root).seal(binding, inflight['round_id'], locator)
            return self._settle_unknown(
                conversation_id, inflight, binding, lease, connector,
                runner.source(inflight['round_id']), locator,
                resolved={'source':recovered['source'],'public':recovered['public']},
                compatibility=compatibility)
        except Exception as exc:
            reason = ('source_shape_unsupported'
                      if isinstance(exc, SourceShapeUnsupported)
                      else 'native_evidence_incomplete')
            return self._unresolved(conversation_id, inflight,
                                    reason, detail=str(exc)[:240])

    def _settle_unknown(self, conversation_id, inflight, binding, lease, connector,
                        source_ref, locator, resolved=None, compatibility=None):
        ensure_input = getattr(self.service, 'ensure_active_input_ref', None)
        input_ref = (ensure_input(conversation_id, inflight)
                     if callable(ensure_input) else inflight.get('input_ref'))
        trusted_input = (TurnInputs(self.service.root).read(input_ref)
                         if input_ref else None)
        if resolved is None:
            resolved = (connector.resolve_source(source_ref, trusted_public=trusted_input)
                        if binding.get('engine') == 'codex'
                        else connector.resolve_source(source_ref))
        if (not isinstance(resolved, dict) or set(resolved) != {'source','public'}
                or not isinstance(resolved['public'], list)):
            raise InvalidState('Shepherd unknown 来源回读格式无效')
        if compatibility is None:
            classify = getattr(connector, 'source_compatibility', None)
            compatibility = classify(resolved['source']) if callable(classify) else []
        public = copy.deepcopy(resolved['public'])
        if input_ref and not has_native_user_input(public, resolved['source']):
            public = trusted_input + public
        unfinished = locator.get('unfinished_tool_ids') or []
        row = self.coordinator.settle_recovered(
            conversation_id, lease['lease_id'], lease['generation'],
            attempt_id=inflight['attempt_id'], source=source_ref,
            source_hash=digest(resolved['source']), public_hash=digest(public),
            unfinished_tool_ids=unfinished,
            native_revision=str(locator.get('native_revision') or ''),
            estimated_tokens=estimate_tokens(public))
        report = {'conversation_id':conversation_id,'state':'settled_incomplete',
                  'offset':row['offset'],'terminal_state':'unknown',
                  'unfinished_tool_ids':copy.deepcopy(unfinished),
                  'binding_id':binding['binding_id']}
        if compatibility:
            report['compatibility'] = copy.deepcopy(compatibility[:16])
        rebind = getattr(self.service, 'recover_incomplete', None)
        if callable(rebind):
            try:
                active = rebind(conversation_id, binding['binding_id'])
                report.update(state='recovered_incomplete',
                              replacement_binding_id=(active.get('binding') or {}).get('binding_id',''))
            except Exception as exc:
                # The Turn is already durable history. Never roll it back or
                # reactivate the uncertain native thread; startup resumes rebind.
                report.update(state='recovery_rebind_pending', detail=str(exc)[:240])
        return report

    def _unresolved(self, conversation_id, inflight, reason, detail=''):
        state = self.coordinator.state(conversation_id) or {}
        lease = state.get('lease') or {}
        try:
            self.coordinator.mark_unresolved(
                conversation_id, lease.get('lease_id'), lease.get('generation'),
                attempt_id=inflight.get('attempt_id'), reason=reason, detail=detail)
        except Exception:
            pass
        return {'conversation_id': conversation_id, 'state': 'needs_reconciliation',
                'reason': reason, 'detail': detail}
