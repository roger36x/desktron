"""Admission must not expand the archive before deciding to use bounded history."""
from unittest import mock

import pytest

from shepherd.context_admission import admit
from shepherd.coordinator import digest
from shepherd.harness import ExecutionHarness
from shepherd.projector import ReadProjector
from test_shepherd_context_admission import RejectingRunner, run, setup
from test_shepherd_unified_storage import ENGINES, JournalRunner


@pytest.mark.parametrize('history,expected', [(100, ''), (16384, ''), (16385, 'context_window_unknown')])
def test_unknown_window_caps_even_compatible_checkpoint_history(history, expected):
    plan = admit(window=0, current=1000, history=history, has_history=True,
                 has_checkpoint=True, required=False)
    assert plan.reason == expected
    assert plan.history_budget == 16384


@pytest.mark.parametrize('engine', ENGINES)
def test_unknown_window_with_large_native_checkpoint_is_bounded(tmp_path, engine):
    service, c, lease = setup(tmp_path, engine)
    row = service.coordinator.state('admit')['rounds'][0]
    scope = {'engine': engine, 'codec': c.native_codec, 'cwd': c.native_cwd,
             'model': 'fixture', 'provider': ''}
    from shepherd.tool_call_ids import history_projection_version
    version = history_projection_version(engine)
    if version:
        scope['history_projection'] = version
    public = {'round_id': 'history', 'offset': 1, 'entry_hash': 'a' * 64,
              'public': [{'type': 'message', 'role': 'user', 'content': [
                  {'type': 'input_text', 'text': 'large history ' * 12000}]}]}
    items = c.encode(public)
    # Unified non-Codex native checkpoints are lists, not imported wrappers.
    native = {'model': 'fixture', 'items': items} if engine == 'codex' else items
    c.archive.checkpoint(digest(scope), turn_id=row['round_id'], through_seq=1,
        payload={'native': native, 'source_hash': c._checkpoint_boundary_hash(row)})
    _, checkpoint, rounds = c.context({'model': 'fixture', '_context_admission': True})
    assert c.admission['reason'] == 'context_window_unknown'
    assert c.admission['history_tokens'] <= 16384
    assert checkpoint['payload']['native']['degraded_context']
    assert rounds == []


def append_history(service, c, lease, count=30):
    for index in range(count):
        public = [{'type': 'message', 'role': 'assistant', 'content': [
            {'type': 'output_text', 'text': 'old tool findings ' * 1500}]}]
        ExecutionHarness(service.coordinator, c, JournalRunner(c, public)).run('admit',
            lease_id=lease['lease_id'], generation=lease['generation'], prompt='history')


@pytest.mark.parametrize('engine', ENGINES)
@pytest.mark.parametrize('window', [0, 32000])
def test_large_archive_is_not_fully_projected_before_fallback(tmp_path, engine, window):
    service, c, lease = setup(tmp_path, engine)
    append_history(service, c, lease)
    calls = []
    original = ReadProjector.project
    def observe(projector, conversation_id, start_offset=1, end_offset=None, cancelled=None):
        calls.append((start_offset, end_offset))
        return original(projector, conversation_id, start_offset, end_offset, cancelled)
    with mock.patch.object(ReadProjector, 'project', observe):
        _, checkpoint, rounds = c.context({'model': 'fixture', '_context_window': window,
            '_context_admission': True})
    assert checkpoint['payload']['native']['degraded_context']
    assert not rounds
    assert c.admission['history_tokens'] <= 16384
    total = len(service.coordinator.state('admit')['rounds'])
    assert all((end if end is not None else total) - start + 1 <= 12 for start, end in calls)
    if window == 0:
        # No compatible checkpoint: only the recent fallback suffix is read.
        assert calls == [(total - 11, None)]
    else:
        assert sum((end if end is not None else total) - start + 1 for start, end in calls) < total


@pytest.mark.parametrize('engine', ENGINES)
def test_fitting_history_is_measured_incrementally_not_by_repeated_prefixes(tmp_path, engine):
    from shepherd.context_budget import estimate_context_tokens
    service, c, lease = setup(tmp_path, engine)
    append_history(service, c, lease)
    measured = []
    def estimate(value):
        measured.append(len(value))
        return estimate_context_tokens(value)
    with mock.patch('shepherd.context_budget.estimate_context_tokens', side_effect=estimate):
        _, checkpoint, rounds = c.context({'model': 'fixture', '_context_window': 1000000,
            '_context_admission': True})
    assert checkpoint is None and len(rounds) == 31
    assert len(measured) == len(rounds)
    assert sum(measured) == sum(len(c.encode(row)) for row in rounds)
    exact = estimate_context_tokens([item for row in rounds for item in c.encode(row)])
    assert exact <= c.admission['history_tokens'] <= exact + len(rounds)


@pytest.mark.parametrize('engine', ENGINES)
def test_learned_failure_does_not_expand_old_history_before_fallback(tmp_path, engine):
    service, c, lease = setup(tmp_path, engine)
    run(service, c, lease, RejectingRunner(c, progress=True, error='autocompact_thrashing'))
    append_history(service, c, lease)
    total = len(service.coordinator.state('admit')['rounds'])
    calls = []
    original = ReadProjector.project
    def observe(projector, conversation_id, start_offset=1, end_offset=None, cancelled=None):
        calls.append((start_offset, end_offset))
        return original(projector, conversation_id, start_offset, end_offset, cancelled)
    with mock.patch.object(ReadProjector, 'project', observe):
        c.context({'model': 'fixture', '_context_window': 32000, '_context_admission': True})
    assert c.admission['reason'] == 'context_rejected'
    assert calls == [(total - 11, None)]
