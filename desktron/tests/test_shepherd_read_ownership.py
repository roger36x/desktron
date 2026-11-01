"""Fresh read ownership must preserve creation, migration and corruption fences."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest import mock

import pytest
from shepherd.store import Store, InvalidState
import test_shepherd_turn_state as fixtures


def test_nested_caller_edits_do_not_mutate_other_reads_or_disk(tmp_path):
    store = Store(tmp_path)
    first = store.load_raw('conversation', create=True)
    second = store.load_raw('conversation')
    first['audit_events'].append({'caller': {'edit': True}})
    first['bindings']['caller'] = {'nested': []}
    assert store.load_raw('conversation') == second
    with store.change('conversation') as state:
        state['shadow_cursor'] = 3
    assert store.load_raw('conversation')['shadow_cursor'] == 3
    assert second['shadow_cursor'] == 0


@pytest.mark.parametrize('migration', [False, True])
def test_concurrent_readers_recheck_create_and_migration_under_lock(tmp_path, migration):
    store = Store(tmp_path)
    path = Path(store.path('conversation'))
    original = json.dumps(fixtures.V1Migration().old(), indent=2) + '\n'
    if migration:
        path.parent.mkdir()
        path.write_text(original)
    from shepherd import store as module
    read_value = module.read_value
    gate = Barrier(4)
    def racing_read(filename):
        value = read_value(filename)
        gate.wait(timeout=10)
        return value
    with mock.patch.object(module, 'read_value', side_effect=racing_read):
        with ThreadPoolExecutor(max_workers=4) as pool:
            states = list(pool.map(lambda _: store.load_raw('conversation', create=True), range(4)))
    assert all(type(state) is dict and state == states[0] for state in states)
    assert states[0]['version'] == 2
    states[0]['audit_events'].append({'caller': 'edit'})
    assert states[1] == store.load_raw('conversation')
    if migration:
        assert Path(str(path) + '.v1.backup').read_text() == original
        with store.change('conversation') as state:
            state['shadow_cursor'] = 1
        assert store.load_raw('conversation')['shadow_cursor'] == 1


def test_fresh_read_still_validates_every_turn_fingerprint(tmp_path):
    store = Store(tmp_path)
    path = Path(store.path('conversation'))
    path.parent.mkdir()
    path.write_text(json.dumps(fixtures.V1Migration().old()))
    state = store.load_raw('conversation')
    state['turns'][0]['public_hash'] = '0' * 64
    path.write_text(json.dumps(state))
    with pytest.raises(InvalidState, match='指纹'):
        store.load_raw('conversation')
