"""只用隔离索引，验证读取失败不等于会话删除。"""
from types import SimpleNamespace
from unittest.mock import Mock
import threading
import time

import pytest
import term
from session import api as routes
import ui_contract


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    indexes = {name: {} for name in term.GUI_ENGINES}
    monkeypatch.setattr(term, 'DATA_HOME', str(tmp_path))
    monkeypatch.setattr(term, '_adapter', lambda name: SimpleNamespace(index=indexes[name]))
    monkeypatch.setattr(term, '_term_list', lambda: [])
    monkeypatch.setattr(term, '_term_agents_annotate', lambda rows: rows)
    monkeypatch.setattr(term, '_live_map', lambda: {})
    monkeypatch.setattr(term, '_TERMS', {})
    monkeypatch.setattr(term, '_term_meta_read', lambda: {})
    monkeypatch.setattr(term, '_gui_live_sids', lambda: set())
    monkeypatch.setattr(term, '_gui_resident_sids', lambda: set())
    monkeypatch.setattr(term, '_gui_busy_unread', lambda *a: (False, False))
    monkeypatch.setattr(term, '_codex_sessions', lambda *a: [])
    monkeypatch.setattr(term, '_claude_session_by_id', lambda *a: None)
    monkeypatch.setattr(term, '_display_title', lambda sid, eng, row, *a: row.get('title', ''))
    monkeypatch.setattr(term, '_transcript_cwd', lambda *a: '')
    monkeypatch.setattr(term, '_session_branch_label', lambda *a: '')
    return indexes


def test_broken_engine_index_is_partial_not_empty_success(isolated, monkeypatch):
    def adapter(name):
        if name == 'claude':
            raise ValueError('private path and data must not leak')
        return SimpleNamespace(index=isolated[name])
    monkeypatch.setattr(term, '_adapter', adapter)
    result = term._term_snapshot()
    assert result['complete'] is False
    assert result['issues'] == [{'source': 'claude', 'code': 'index_unavailable'}]
    assert 'private' not in str(result)


def test_meta_failure_is_reported_and_source_changes_with_data_root(isolated, monkeypatch, tmp_path):
    first = term._term_snapshot()
    assert first['complete'] and first['sessions'] == []
    monkeypatch.setattr(term, '_term_meta_read', Mock(side_effect=OSError('disk')))
    broken = term._term_snapshot()
    assert not broken['complete'] and broken['source'] == first['source']
    monkeypatch.setattr(term, 'DATA_HOME', str(tmp_path / 'other'))
    assert term._term_snapshot()['source'] != first['source']


@pytest.mark.parametrize('engine', term.GUI_ENGINES)
def test_registration_gap_preserves_started_sessions_for_every_engine(isolated, engine):
    isolated[engine]['adp_test'] = {'engine_sid': 'engine-test', 'title': 'kept',
                                  'domain': 'work', 'time': {'created': 10}}
    result = term._term_snapshot()
    assert result['complete'], result
    assert any(row['session_id'] == 'adp_test' for row in result['sessions'])


def test_closed_registration_stays_closed(isolated, monkeypatch):
    isolated['pi']['adp_test'] = {'engine_sid': 'engine-test', 'domain': 'work'}
    monkeypatch.setattr(term, '_term_meta_read', lambda: {'engine-test': {'engine': 'pi', 'closed': True}})
    assert term._term_snapshot()['sessions'] == []


def test_corrupt_file_then_repair_recovers_without_recreating_sessions(isolated, monkeypatch, tmp_path):
    from durable_json import read, write
    path = tmp_path / 'index.json'
    snapshot = read(path)
    snapshot['adp_existing'] = {'id': 'adp_existing', 'title': 'kept'}
    write(path, snapshot)
    good_bytes = path.read_bytes()
    monkeypatch.setattr(term, '_adapter', lambda name: SimpleNamespace(
        index=read(path) if name == 'pi' else isolated[name]))
    first = term._term_snapshot()
    assert first['complete'] and first['sessions'][0]['id'] == 'adp_existing'
    path.write_bytes(b'{broken')
    failed = term._term_snapshot()
    assert not failed['complete']
    assert path.read_bytes() == b'{broken'  # 读取失败不写空、不自动回滚历史。
    path.write_bytes(good_bytes)
    restored = term._term_snapshot()
    assert restored['complete'] and restored['sessions'] == first['sessions']
    assert restored['source'] == first['source']


def test_term_meta_mutations_are_serialized_without_lost_updates(isolated, monkeypatch):
    state = {}
    monkeypatch.setattr(term, '_term_meta_read', lambda: dict(state))
    monkeypatch.setattr(term, '_term_meta_write', lambda value: state.clear() or state.update(value))
    barrier = threading.Barrier(3)
    def worker(key):
        barrier.wait()
        def update(meta):
            time.sleep(.01)
            meta[key] = {'engine': 'pi'}
            return True
        term._term_meta_mutate(update)
    threads = [threading.Thread(target=worker, args=(key,)) for key in ('a', 'b')]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert set(state) == {'a', 'b'}


def test_catalog_scan_context_is_cleared_after_failure(isolated, monkeypatch):
    def fail(issues, static=False):
        term._TERM_SCAN_CONTEXT.reverse = {'stale': {}}
        raise RuntimeError('broken source')
    monkeypatch.setattr(term, '_term_persisted_scan', fail)
    term._TERM_CATALOG.update(revision=0, built_revision=-1, built_at=0.0,
                              stamp=None, rows=[], issues=set())
    with pytest.raises(RuntimeError):
        term._term_persisted()
    assert term._TERM_SCAN_CONTEXT.reverse is None


def test_static_catalog_reused_but_dynamic_state_is_overlaid_each_request(isolated, monkeypatch):
    builds, overlays = [], []
    monkeypatch.setattr(term, '_term_persisted_scan',
                        lambda issues, static=False: builds.append(static) or [
                            {'id': 'engine-test', 'engine': 'pi', 'time': 1}])
    def dynamic(rows, issues):
        overlays.append(1)
        return [{**rows[0], 'busy': len(overlays) == 2}]
    monkeypatch.setattr(term, '_term_dynamic_overlay', dynamic)
    term._TERM_CATALOG.update(revision=0, built_revision=-1, built_at=0.0,
                              stamp=None, rows=[], issues=set())
    assert term._term_persisted()[0]['busy'] is False
    assert term._term_persisted()[0]['busy'] is True
    assert builds == [True]
    term._term_catalog_invalidate()
    term._term_persisted()
    assert builds == [True, True]


def test_all_engine_session_listing_reads_meta_once(isolated, monkeypatch):
    calls = []
    adapters = {name: SimpleNamespace(list_sessions=lambda: [
        {'id': 'adp-' + name, 'engine_sid': 'native-' + name,
         'time': {'updated': 1}}]) for name in term.GUI_ENGINES}
    monkeypatch.setattr(ui_contract, 'GUI_ENGINES', tuple(adapters))
    monkeypatch.setattr(ui_contract, '_adapter', lambda name: adapters[name])
    monkeypatch.setattr(ui_contract, '_shepherd', Mock(side_effect=ValueError('disabled')))
    monkeypatch.setattr(ui_contract, '_shepherd_trial', Mock(side_effect=ValueError('disabled')))
    monkeypatch.setattr(ui_contract, '_term_meta_read',
                        lambda: calls.append(1) or {name: {'domain': 'work'}
                                                   for name in ('native-' + x for x in adapters)})
    rows = ui_contract.UiContractMixin._sessions_listing()
    assert len(rows) == len(adapters)
    assert calls == [1]
    assert all(row['domain'] == 'work' for row in rows)


def test_route_negotiates_integrity_and_old_clients_reject_partial(isolated):
    h = SimpleNamespace(_send_json=lambda body, code=200: (body, code))
    from unittest.mock import patch
    partial = {'sessions': [], 'complete': False, 'source': 's', 'issues': []}
    with patch.object(routes, '_term_snapshot', return_value=partial):
        assert routes.handle(h, '/workbench/terminals', 'integrity=1', 'GET') == (partial, 200)
        assert routes.handle(h, '/workbench/terminals', '', 'GET')[1] == 503
        partial['complete'] = True
        assert routes.handle(h, '/workbench/terminals', '', 'GET') == ([], 200)
