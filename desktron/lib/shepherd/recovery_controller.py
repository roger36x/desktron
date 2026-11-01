"""Continuous, evidence-only recovery for active Shepherd Conversations.

Recovery never runs a model or resends input.  It may settle a terminal receipt,
or—after exact Writer death—seal a stable native suffix as unknown history.
"""
import copy
import os
import random
import threading
import time

import durable_json
from .recovery import BOOT_ID, CrashRecovery, orphan_reserved, process_state
from .store import valid_id


class RecoveryController:
    def __init__(self, service, requests=None, *, boot_id=BOOT_ID,
                 retry_delays=(0.25, 1.0, 3.0), retry_callback=None,
                 report_callback=None):
        self.service = service
        self.requests = requests
        self.boot_id = str(boot_id)
        self.retry_delays = tuple(max(0.0, float(value)) for value in retry_delays)
        self.retry_callback = retry_callback
        self.report_callback = report_callback
        self._lock = threading.Lock()
        self._timers = {}
        self._stopped = False
        self._rechecked = set()
        self.audit_root = os.path.join(self.service.root, 'recovery-events')

    def recover_guarded(self, conversation_id, *, trigger, retry_count=0):
        """Recover one commit while the caller holds the conversation turn guard."""
        state = self.service.coordinator.state(conversation_id) or {}
        if state.get('tombstone'):
            self._cancel(conversation_id)
            return self._record(conversation_id, trigger, 'deleted', 'none',
                                'deleted', retry_count)
        inflight = copy.deepcopy(state.get('inflight') or {})
        recovery = inflight.get('recovery') or {}
        # Older decoders could quarantine an otherwise intact unified journal.
        # Once per attempt per boot, revalidate its bytes under the existing
        # turn/switch guards. Never un-fence first or retry a native execution.
        binding = (state.get('bindings') or {}).get(inflight.get('binding_id')) or {}
        # During startup the previous writer may still be exiting. Re-probe
        # identity without clearing the fence, replaying, or signalling a PID.
        stopping = (inflight.get('state') == 'needs_reconciliation'
                    and inflight.get('recovery_reason') in
                        ('orphan_process_alive', 'orphan_process_unknown')
                    and recovery.get('phase') == 'dispatched'
                    and not inflight.get('staged_source_ref'))
        if stopping and process_state(recovery.get('process')) != 'dead':
            self._schedule(conversation_id, retry_count + 1)
            return self._record(conversation_id, trigger, 'needs_reconciliation',
                                'evidence_probe', 'waiting_writer_exit', retry_count)
        recheck_id = (conversation_id, inflight.get('attempt_id'))
        verification_reasons = ('commit_verification_failed', 'source_shape_unsupported')
        staged_recheck = (inflight.get('staged_source_ref')
                          and inflight.get('recovery_reason') in verification_reasons)
        verification_recheck = (inflight.get('recovery_reason') in verification_reasons
                                and not inflight.get('staged_source_ref'))
        recheck = stopping or (inflight.get('state') == 'needs_reconciliation'
                   and (staged_recheck or verification_recheck
                        or (inflight.get('recovery_reason','') in ('','native_evidence_incomplete')
                            and not inflight.get('staged_source_ref')))
                   and str(binding.get('connector_version', '')).startswith('unified-v1:')
                   and recovery.get('phase') == 'dispatched'
                   and trigger != 'background_retry'
                   and recheck_id not in self._rechecked
                   and process_state(recovery.get('process')) == 'dead')
        if recheck:
            self._rechecked.add(recheck_id)
        if inflight.get('state') == 'needs_reconciliation' and not recheck:
            self._cancel(conversation_id)
            if self.requests is not None:
                self.requests.reconcile(conversation_id, state)
            return self._record(conversation_id, trigger, 'needs_reconciliation',
                                'isolation_required', 'needs_reconciliation', retry_count,
                                detail=inflight.get('reconciliation_detail') or '')
        # A reserved round never crossed the Coordinator dispatch fence. It may
        # nevertheless have an atomic unified pre-dispatch archive anchor; let
        # recover_one preserve that as abandoned evidence before cancellation.
        eligible = (recheck or inflight.get('state') == 'source_persisted'
                    or orphan_reserved(inflight)
                    or (inflight.get('state') == 'running'
                        and recovery.get('phase') in ('reserved','dispatched')))
        if not eligible:
            self._cancel(conversation_id)
            # A committed turn may outlive its receipt write in this boot.
            # The caller holds both guards; unlinked accepted requests are
            # deliberately excluded by Requests.reconcile.
            if self.requests is not None:
                self.requests.reconcile(conversation_id, state)
            return self._record(conversation_id, trigger, inflight.get('state') or 'ready',
                                'none', 'not_applicable', retry_count)
        report = CrashRecovery(self.service, boot_id=self.boot_id).recover_one(
            conversation_id, allow_current_boot_commit=True, receipt_only=False)
        current = self.service.coordinator.state(conversation_id) or {}
        if self.requests is not None:
            self.requests.reconcile(conversation_id, current)
        outcome = str(report.get('state') or 'unknown')
        if outcome == 'source_persisted':
            lease = current.get('lease') or {}
            failure = self.service.coordinator.note_staged_failure(
                conversation_id, lease.get('lease_id'), lease.get('generation'),
                attempt_id=inflight.get('attempt_id'), max_retries=len(self.retry_delays),
                reason=report.get('reason') or 'commit_verification_failed',
                detail=report.get('detail') or '')
            if failure['blocked']:
                current = self.service.coordinator.state(conversation_id) or {}
                if self.requests is not None:
                    self.requests.reconcile(conversation_id, current)
                self._cancel(conversation_id)
                outcome = 'needs_reconciliation'
                action = 'isolation_required'
            else:
                self._schedule(conversation_id, failure['attempts'])
                action = 'commit_only_retry'
        elif outcome in ('running','needs_reconciliation'):
            # A live or ambiguous Writer remains fenced. A deterministic source
            # verification failure is already blocked and ready for explicit isolation.
            active = (current.get('inflight') or {})
            action = ('isolation_required' if active.get('state') == 'needs_reconciliation'
                      else 'evidence_probe')
            if active.get('recovery_reason') in ('orphan_process_alive','orphan_process_unknown'):
                action = 'evidence_probe'
                self._schedule(conversation_id, retry_count + 1)
        elif outcome in ('settled_incomplete','recovered_incomplete','recovery_rebind_pending'):
            self._cancel(conversation_id)
            action = 'settle_incomplete'
        else:
            self._cancel(conversation_id)
            action = 'settle_terminal'
        return self._record(conversation_id, trigger, inflight.get('state'), action,
                            outcome, retry_count, detail=report.get('detail') or '',
                            compatibility=report.get('compatibility'))

    def stop(self):
        with self._lock:
            self._stopped = True
            timers = list(self._timers.values())
            self._timers.clear()
        for timer in timers:
            timer.cancel()

    def cancel_conversation(self, conversation_id):
        """Cancel deferred probes before a durable delete tombstone is written."""
        self._cancel(conversation_id)

    def _schedule(self, conversation_id, retry_count):
        if retry_count > len(self.retry_delays) or not callable(self.retry_callback):
            return
        with self._lock:
            if self._stopped or conversation_id in self._timers:
                return
            delay = self.retry_delays[retry_count - 1] * random.uniform(0.8, 1.2)
            timer = threading.Timer(delay, self._fire, args=(conversation_id, retry_count))
            timer.daemon = True
            self._timers[conversation_id] = timer
        timer.start()

    def _fire(self, conversation_id, retry_count):
        with self._lock:
            self._timers.pop(conversation_id, None)
            if self._stopped:
                return
        self.retry_callback(conversation_id, retry_count)

    def _cancel(self, conversation_id):
        with self._lock:
            timer = self._timers.pop(conversation_id, None)
        if timer:
            timer.cancel()

    def _record(self, conversation_id, trigger, state, action, outcome,
                retry_count, detail='', compatibility=None):
        row = {'conversation_id': str(conversation_id),
               'trigger': str(trigger or ''),
               'state': str(state or ''), 'action': str(action or ''),
               'outcome': str(outcome or ''),
               'retry_count': int(retry_count), 'at': time.time()}
        if detail:
            row['detail'] = str(detail)[:240]
        if state == 'deleted' or outcome == 'deleted':
            return row
        if isinstance(compatibility, list):
            safe = []
            for item in compatibility[:16]:
                if not isinstance(item, dict):
                    continue
                engine = str(item.get('engine') or '')[:32]
                action = str(item.get('action') or '')[:64]
                kind = str(item.get('kind') or '')[:96]
                count = item.get('count')
                if engine and action and kind and isinstance(count, int) and not isinstance(count, bool) and count > 0:
                    safe.append({'engine':engine, 'action':action,
                                 'kind':kind, 'count':min(count, 1000000)})
            if safe:
                row['compatibility'] = safe
        path = os.path.join(self.audit_root, valid_id(conversation_id) + '.json')
        from .physical_delete import deletion_guard, is_deleted
        with deletion_guard(self.service.root, conversation_id):
            if is_deleted(self.service.root, conversation_id):
                return row
            with durable_json.locked(path + '.transaction'):
                saved = durable_json.read(path)
                events = saved.get('events') if isinstance(saved, dict) else None
                events = list(events) if isinstance(events, list) else []
                events.append(copy.deepcopy(row))
                saved.clear()
                saved.update(version=1, events=events[-200:])
                durable_json.write(path, saved)
        if callable(self.report_callback):
            self.report_callback(copy.deepcopy(row))
        return row
