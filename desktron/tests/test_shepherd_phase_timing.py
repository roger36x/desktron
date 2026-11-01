"""Passive cold/warm dispatch diagnostics, without provider requests."""
import json
from types import SimpleNamespace
import threading
from unittest import mock

import pytest

from shepherd.phase_timing import PhaseTiming, phase_call
from shepherd.store import Conflict, PromptNotStarted
from shepherd.unified_runners import UnifiedRunner


def test_nested_spans_use_monotonic_clock_and_report_failures_without_payload():
    rows = []
    failure = RuntimeError('synthetic-private-body')
    with mock.patch('shepherd.phase_timing.time.monotonic', side_effect=[1, 2, 3, 4, 5]):
        timing = PhaseTiming(rows.append)
        with pytest.raises(RuntimeError) as caught:
            with timing.span('pi.pool_acquire'):
                with timing.span('pi.spawn'):
                    raise failure
    assert caught.value is failure
    assert [row['state'] for row in rows] == ['begin', 'begin', 'error', 'error']
    assert rows[1]['parent_span'] == rows[0]['span']
    assert rows[2]['duration_ms'] == 1000
    assert rows[3]['duration_ms'] == 3000
    assert rows[3]['elapsed_ms'] == 4000
    assert 'synthetic-private-body' not in json.dumps(rows)
    assert timing.stack == []


@pytest.mark.parametrize('exception', [RuntimeError, KeyboardInterrupt])
def test_diagnostic_errors_do_not_replace_original_failure(exception):
    timing = PhaseTiming(lambda _row: (_ for _ in ()).throw(OSError('trace unavailable')))
    original = exception('original')
    with pytest.raises(exception) as caught:
        timing.call('pi.spawn', lambda: (_ for _ in ()).throw(original))
    assert caught.value is original
    assert timing.call('pi.spawn', lambda: 'ok') == 'ok'


def test_unknown_stage_and_host_metadata_cannot_leak_arbitrary_values():
    rows = []
    timing = PhaseTiming(rows.append)
    assert timing.call('untrusted-stage', lambda: 'private-result') == 'private-result'
    timing.mark('untrusted-stage')
    timing.host({'http_setup': {'elapsed_ms': 5, 'duration_ms': 2, 'secret': 'private'},
                 'private': {'elapsed_ms': 1, 'duration_ms': 1},
                 'sdk_import': {'elapsed_ms': float('nan'), 'duration_ms': 1},
                 'settings_load': {'elapsed_ms': True, 'duration_ms': 1},
                 'session_create': {'elapsed_ms': 4, 'duration_ms': -1}})
    assert rows == [{'event': 'response.phase', 'stage': 'host.http_setup', 'state': 'ok',
                     'clock': 'pi_startup', 'elapsed_ms': 5, 'duration_ms': 2}]
    assert phase_call(SimpleNamespace(), 'pi.spawn', lambda x: x, 3) == 3


class Process:
    def __init__(self, ready):
        self.proc = SimpleNamespace(pid=123)
        self.queue = [ready, {'type': 'agent_settled'}]
        self.sent = []
        self.closed = False

    def events(self):
        while self.queue:
            yield self.queue.pop(0)

    def send(self, frame):
        self.sent.append(frame)

    def close(self):
        self.closed = True

    def finish(self):
        pass

    def begin(self, **kwargs):
        pass

    def end_input(self):
        pass

    def review_snapshot(self):
        return None


def runner_fixture(rows):
    runner = object.__new__(UnifiedRunner)
    runner.connector = SimpleNamespace(engine='pi', native_cwd='.', native_session_id='fixture-session',
        binding={'binding_id': 'fixture-binding'}, encode=lambda _row: [])
    runner.binding = runner.connector.binding
    runner.options = {}
    runner.operation = 'turn'
    runner.command = ['pi', '--mode', 'rpc', '--offline']
    runner.static_system_text = runner.history_hint = ''
    runner.env = {}
    runner.phase_timing = PhaseTiming(rows.append)
    runner._payload = mock.Mock(return_value='bootstrap.json')
    runner.round_id = 'fixture:1'
    runner.cancelled = threading.Event()
    runner.context_tracker = SimpleNamespace(rejected=False)
    runner.checkpoint = None
    runner.user_frame = {'type': 'prompt', 'message': 'synthetic-private-body'}
    runner.on_runtime = mock.Mock()
    runner.on_event = None
    runner.on_process = None
    runner.on_stall = None
    runner.timeout = 10
    return runner


def ready_frame():
    return {'type': 'protocol_ready', 'contract': 'desk-pi-execute-v1',
            'sessionId': 'fixture-session', 'startup_timing': {
                'services_create': {'duration_ms': 2, 'elapsed_ms': 5}}}


@pytest.mark.parametrize('resident_reused', [False, True])
def test_cold_and_warm_pi_preserve_dispatch_order_and_skip_unobserved_stages(resident_reused):
    rows = []
    runner = runner_fixture(rows)
    process = Process(ready_frame())
    dispatch = mock.Mock(side_effect=lambda **_kw: rows.append({'dispatch': True}))
    runner._native_process = mock.Mock(return_value=process)
    from contextlib import ExitStack
    with ExitStack() as stack:
        stack.enter_context(mock.patch('shepherd.pi_connector.pi_sdk_root', return_value='sdk'))
        stack.enter_context(mock.patch('shepherd.pi_connector.PiConnector.availability',
                                       return_value={'available': True}))
        stack.enter_context(mock.patch('shepherd.resident_process.drop'))
        if resident_reused:
            runner.execution = SimpleNamespace(register=lambda _proc: None)
            runner.expected_offset = 2
            process.queue = [{'type': 'agent_settled'}]
            stack.enter_context(mock.patch('shepherd.resident_process.enabled', return_value=True))
            stack.enter_context(mock.patch('shepherd.resident_process.acquire', return_value=(process, False)))
        assert runner._pi('question', [], None, dispatch, None) == 'completed'
    assert dispatch.call_count == 1
    assert process.sent[-1] == runner.user_frame
    assert runner.on_runtime.call_args.args[0]['resident_reused'] is resident_reused
    stages = {row.get('stage') for row in rows}
    assert ('pi.ready_wait' in stages) is not resident_reused
    assert ('pi.bootstrap_write' in stages) is not resident_reused
    assert ('host.services_create' in stages) is not resident_reused
    if not resident_reused:
        at = rows.index({'dispatch': True})
        assert any(row.get('stage') == 'pi.ready_wait' and row.get('state') == 'ok'
                   for row in rows[:at])
    assert 'synthetic-private-body' not in json.dumps(rows)
    observed = next(row for row in rows if row.get('stage') == 'pi.resident')
    assert observed['resident_reused'] is resident_reused


@pytest.mark.parametrize('frame', [{}, {'type': 'protocol_ready', 'contract': 'wrong'}])
def test_failed_readiness_never_dispatches_and_records_error(frame):
    rows = []
    runner = runner_fixture(rows)
    process = Process(frame)
    dispatch = mock.Mock()
    with mock.patch('shepherd.pi_connector.pi_sdk_root', return_value='sdk'), \
         mock.patch('shepherd.pi_connector.PiConnector.availability', return_value={'available': True}), \
         mock.patch('shepherd.resident_process.drop'), \
         mock.patch.object(runner, '_native_process', return_value=process):
        with pytest.raises(Conflict):
            runner._pi('question', [], None, dispatch, None)
    dispatch.assert_not_called()
    assert process.closed
    assert any(row.get('stage') == 'pi.ready_wait' and row.get('state') == 'error' for row in rows)


@pytest.mark.parametrize('trace_broken', [False, True])
def test_harness_timing_does_not_change_committed_turn_or_dispatch_count(tmp_path, trace_broken):
    from shepherd.service import Service
    from shepherd.harness import ExecutionHarness
    from test_shepherd_unified_storage import JournalRunner
    service = Service(tmp_path, storage='unified')
    service.create('fixture', 'pi', tmp_path)
    active = service.active('fixture')
    connector = service.connector(active['binding'])
    runner = JournalRunner(connector)
    rows = []
    def emit(row):
        if trace_broken:
            raise OSError('trace unavailable')
        rows.append(row)
    runner.phase_timing = PhaseTiming(emit)
    runner.on_timing_dispatched = mock.Mock()
    lease = active['lease']
    result = ExecutionHarness(service.coordinator, connector, runner).run('fixture',
        lease_id=lease['lease_id'], generation=lease['generation'], prompt='fixture',
        input_public=[{'type': 'message', 'role': 'user',
                       'content': [{'type': 'input_text', 'text': 'synthetic-private-body'}]}])
    assert result['terminal_state'] == 'completed'
    runner.on_timing_dispatched.assert_called_once()
    if not trace_broken:
        completed = [row['stage'] for row in rows if row['state'] == 'ok']
        assert completed == ['harness.state', 'harness.recovery_boundary', 'harness.input_write',
                             'harness.reserve', 'harness.dispatch_fence']
        assert 'synthetic-private-body' not in json.dumps(rows)


@pytest.mark.parametrize('mode', ['cold', 'reuse', 'retire', 'busy'])
def test_real_pool_reports_lock_trim_and_retirement_without_changing_exclusivity(mode):
    from shepherd import resident_process as pool
    rows = []
    timing = PhaseTiming(rows.append)
    process = SimpleNamespace(proc=SimpleNamespace(poll=lambda: None))
    create = mock.Mock(return_value=process)
    with mock.patch.dict(pool._HOSTS, {}, clear=True), mock.patch.object(pool, '_trim') as trim:
        if mode != 'cold':
            entry = pool._Entry(process, 'old' if mode == 'retire' else 'key', 0)
            entry.busy = mode == 'busy'
            pool._HOSTS['fixture'] = entry
        def drop(binding, _process):
            pool._HOSTS.pop(binding)
            return True
        with mock.patch.object(pool, 'drop', side_effect=drop) as retire:
            if mode == 'busy':
                with pytest.raises(Conflict):
                    pool.acquire('fixture', 'key', 0, create, timing=timing)
                create.assert_not_called()
                assert rows[-1]['stage'] == 'pi.pool_lock' and rows[-1]['state'] == 'error'
                return
            returned, created = pool.acquire('fixture', 'key', 0, create, timing=timing)
            assert returned is process and created is (mode != 'reuse')
            assert create.call_count == (0 if mode == 'reuse' else 1)
            assert trim.call_count == (0 if mode == 'reuse' else 1)
            assert retire.call_count == (1 if mode == 'retire' else 0)
            stages = [row['stage'] for row in rows if row['state'] == 'ok']
            assert stages == (['pi.pool_lock'] if mode == 'reuse' else
                              ['pi.pool_lock', 'pi.pool_retire', 'pi.pool_trim'] if mode == 'retire'
                              else ['pi.pool_lock', 'pi.pool_trim'])


def test_harness_failure_before_reservation_keeps_original_error_and_no_dispatch(tmp_path):
    from shepherd.service import Service
    from shepherd.harness import ExecutionHarness
    from test_shepherd_unified_storage import JournalRunner
    service = Service(tmp_path, storage='unified')
    service.create('fixture', 'pi', tmp_path)
    active = service.active('fixture')
    connector = service.connector(active['binding'])
    runner = JournalRunner(connector)
    rows = []
    runner.phase_timing = PhaseTiming(rows.append)
    runner.on_timing_dispatched = mock.Mock()
    lease = active['lease']
    original = Conflict('synthetic-private-body')
    with mock.patch.object(connector, 'recovery_boundary', side_effect=original):
        with pytest.raises(PromptNotStarted) as caught:
            ExecutionHarness(service.coordinator, connector, runner).run('fixture',
                lease_id=lease['lease_id'], generation=lease['generation'], prompt='fixture')
    assert caught.value.__cause__ is original
    runner.on_timing_dispatched.assert_not_called()
    assert not service.coordinator.state('fixture')['inflight']
    assert rows[-1]['stage'] == 'harness.recovery_boundary' and rows[-1]['state'] == 'error'
    assert 'synthetic-private-body' not in json.dumps(rows)
