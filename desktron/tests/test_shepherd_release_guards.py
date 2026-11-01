"""Release regressions for v2 validation and exact writer shutdown evidence."""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'lib'))
from shepherd import Coordinator, Conflict, InvalidState, Store, SwitchRunner
from shepherd.turn_state import turn_hash, validate


def fixture(tmp_path):
    coordinator = Coordinator(Store(tmp_path))
    coordinator.create('conversation')
    binding = coordinator.bind('conversation', 'pi', 'native', native_cwd=str(tmp_path),
        connector_version='v1', binding_id='binding', ownership_nonce='nonce')
    return coordinator, binding


@pytest.mark.parametrize('status', ['retired', 'quarantined', 'recovery_required', 'retargeted'])
def test_disabled_binding_cannot_become_producer(tmp_path, status):
    coordinator, binding = fixture(tmp_path)
    with coordinator.store.change('conversation') as state:
        state['bindings'][binding]['status'] = status
    with pytest.raises(Conflict, match='Producer'):
        coordinator.acquire('conversation', binding, ownership_nonce='nonce')
    assert coordinator.state('conversation')['active_binding_id'] == ''


@pytest.mark.parametrize('change', [
    lambda turn: turn.update(attempt_id=''),
    lambda turn: turn['recovery'].update(boot_id=''),
    lambda turn: turn['recovery'].update(phase='dispatched'),
    lambda turn: turn['recovery'].update(process=[]),
])
def test_active_turn_recovery_identity_is_validated(tmp_path, change):
    coordinator, binding = fixture(tmp_path)
    token = coordinator.acquire('conversation', binding, ownership_nonce='nonce')
    coordinator.begin_round('conversation', binding, token['generation'], attempt_id='attempt',
        recovery={'phase':'reserved', 'boot_id':'boot_test', 'boundary':{}})
    state = coordinator.store.load_raw('conversation')
    change(state['active_turn'])
    with pytest.raises(InvalidState):
        validate(state)


@pytest.mark.parametrize('change', [
    lambda turn: turn['source_ref'].update(session_id='../outside'),
    lambda turn: turn.update(source_hash='z' * 64),
    lambda turn: turn.update(source_key='mismatched'),
])
def test_rehashed_invalid_source_is_rejected(tmp_path, change):
    coordinator, binding = fixture(tmp_path)
    token = coordinator.acquire('conversation', binding, ownership_nonce='nonce')
    coordinator.commit('conversation', binding, token['generation'],
        source={'kind':'native_round','engine':'pi','session_id':'native',
                'entry_id':'turn','source_key':'pi:native:turn'},
        source_hash='a'*64, public_hash='b'*64)
    state = coordinator.store.load_raw('conversation')
    change(state['turns'][0])
    state['turns'][0]['turn_hash'] = turn_hash(state['turns'][0])
    state['bindings'][binding]['imported_turn_hash'] = state['turns'][0]['turn_hash']
    with pytest.raises(InvalidState):
        validate(state)


@pytest.mark.parametrize('states, clock, succeeds', [
    (['alive', 'dead'], [0, 0], True),
    (['unknown', 'unknown'], [0, 3], False),
])
def test_switch_waits_for_exact_writer_death_without_releasing_unknown(states, clock, succeeds):
    class CoordinatorStub:
        pending = {'operation': {'kind':'switch_write', 'phase':'append_writing'}}

        def note_switch_operation(self, _cid, _switch, phase, *, writer):
            self.pending = {'operation': {'kind':'switch_write', 'phase':phase,
                                          'writer':copy.deepcopy(writer)}}

        def state(self, _cid):
            return {'pending_switch':copy.deepcopy(self.pending)}

    coordinator = CoordinatorStub()
    runner = SwitchRunner(coordinator, None,
                          SimpleNamespace(writer_kind='process', writer_notifications=True))
    identity = {'pid':123, 'pgid':123, 'start_token':'exact'}

    def append(*, on_writer):
        on_writer(SimpleNamespace(pid=123), 'active')
        on_writer(None, 'stopped')
        return {'native_revision':'revision'}

    with mock.patch('shepherd.switch.process_identity', return_value=identity), \
            mock.patch('shepherd.switch.process_state', side_effect=states) as probe, \
            mock.patch('shepherd.switch.time.monotonic', side_effect=clock), \
            mock.patch('shepherd.switch.time.sleep'):
        if succeeds:
            assert runner._write('conversation', 'switch', 'append_writing', append) == {
                'native_revision':'revision'}
        else:
            with pytest.raises(Conflict, match='Writer'):
                runner._write('conversation', 'switch', 'append_writing', append)
        assert all(call.args == (identity,) for call in probe.call_args_list)
        status = coordinator.pending['operation']['writer']['status']
        assert status == ('dead' if succeeds else 'unknown')
