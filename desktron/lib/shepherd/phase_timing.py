"""Passive, allowlisted dispatch diagnostics; never serialize business arguments."""
from contextlib import contextmanager
import math
import time


STAGES = frozenset({
    'adapter.enter', 'adapter.ready', 'adapter.require_execution', 'adapter.active',
    'adapter.worker', 'adapter.connector', 'adapter.catalog', 'adapter.system',
    'adapter.spawn_env', 'adapter.command', 'adapter.user_frame', 'adapter.runner_create',
    'harness.recover_prior', 'harness.request_start', 'harness.state', 'harness.prepare_input',
    'harness.recovery_boundary', 'harness.input_write', 'harness.reserve',
    'harness.dispatch_fence', 'runner.verify', 'runner.prepare_input',
    'runner.context', 'runner.resident_drop', 'runner.scratch_sweep',
    'runner.scratch_open', 'runner.predispatch_archive', 'runner.locator',
    'pi.sdk_discovery', 'pi.sdk_probe', 'pi.history_encode',
    'pi.bootstrap_write', 'pi.pool_acquire', 'pi.pool_lock', 'pi.pool_retire',
    'pi.pool_trim', 'pi.spawn', 'pi.process_register',
    'pi.ready_wait', 'pi.fallback_verify', 'pi.prompt_send',
    'claude.start_send', 'claude.start_received', 'claude.store_begin',
    'claude.store_ready', 'claude.input_queued', 'claude.input_consumed',
    'claude.first_native_event', 'claude.stderr_auth', 'claude.stderr_network',
    'claude.stderr_rate_limit', 'claude.stderr_other',
    'claude.provider_response_started', 'claude.provider_retry',
})
HOST_STAGES = frozenset({
    'http_setup', 'sdk_import', 'history_restore', 'settings_load',
    'services_create', 'session_create', 'ready_validate',
})


class PhaseTiming:
    def __init__(self, emit, *, started=None):
        self.emit = emit
        self.started = time.monotonic() if started is None else started
        self.serial = 0
        self.stack = []

    def _emit(self, row):
        try:
            self.emit({'event': 'response.phase', **row})
        except Exception:
            pass  # Diagnostics cannot change delivery or its exception.

    def mark(self, stage):
        if stage in STAGES:
            self._emit({'stage': stage, 'state': 'observed',
                        'elapsed_ms': round((time.monotonic() - self.started) * 1000, 2)})

    @contextmanager
    def span(self, stage):
        if stage not in STAGES:
            yield
            return
        self.serial += 1
        span_id = self.serial
        parent = self.stack[-1] if self.stack else None
        self.stack.append(span_id)
        started = time.monotonic()
        common = {'stage': stage, 'span': span_id, 'parent_span': parent}
        self._emit({**common, 'state': 'begin',
                    'elapsed_ms': round((started - self.started) * 1000, 2)})
        outcome = 'ok'
        try:
            yield
        except BaseException:
            outcome = 'error'
            raise
        finally:
            ended = time.monotonic()
            self.stack.pop()
            self._emit({**common, 'state': outcome,
                        'elapsed_ms': round((ended - self.started) * 1000, 2),
                        'duration_ms': round((ended - started) * 1000, 2)})

    def call(self, stage, function, *args, **kwargs):
        with self.span(stage):
            return function(*args, **kwargs)

    def resident(self, enabled, reused):
        self._emit({'stage': 'pi.resident', 'state': 'observed',
                    'resident': bool(enabled), 'resident_reused': bool(reused),
                    'elapsed_ms': round((time.monotonic() - self.started) * 1000, 2)})

    def host(self, rows):
        # The child clock has its own origin. Never subtract it from parent time.
        if not isinstance(rows, dict):
            return
        for stage in sorted(HOST_STAGES & rows.keys()):
            row = rows[stage]
            if not isinstance(row, dict):
                continue
            elapsed, duration = row.get('elapsed_ms'), row.get('duration_ms')
            if any(type(value) not in (int, float) or not math.isfinite(value)
                   or value < 0 for value in (elapsed, duration)):
                continue
            self._emit({'stage': 'host.' + stage, 'state': 'ok', 'clock': 'pi_startup',
                        'elapsed_ms': round(elapsed, 2), 'duration_ms': round(duration, 2)})


def phase_call(runner, stage, function, *args, **kwargs):
    timing = getattr(runner, 'phase_timing', None)
    if isinstance(timing, PhaseTiming):
        return timing.call(stage, function, *args, **kwargs)
    return function(*args, **kwargs)
