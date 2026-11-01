"""Synthetic native failures: compaction loops are not permission to replay work."""
from unittest import mock

import pytest

from shepherd.context_admission import RejectionTracker, context_error
from shepherd.service import Service
from shepherd.store import Conflict
from test_shepherd_context_admission import RejectingRunner, reject, run, setup
from test_shepherd_unified_storage import ENGINES

THRASH = 'Autocompact is thrashing: the context refilled to the limit within 3 turns of the previous compact, 3 times in a row.'


@pytest.mark.parametrize('error', ['autocompact_thrashing', THRASH,
    {'code': 'autocompact_thrashing'}, {'errors': [{'type': 'autocompact_thrashing'}]}])
def test_compaction_thrashing_is_a_context_failure(error):
    assert context_error(error)


@pytest.mark.parametrize('error', ['compaction failed', 'thrashing disk',
    'rate limit: autocompact_thrashing', 'quota exhausted: ' + THRASH,
    {'code': 'rate_limit_error', 'message': THRASH},
    {'type': 'authentication_error', 'message': THRASH},
    {'code': 'quota_exceeded', 'message': 'autocompact_thrashing'},
    {'error': {'code': 429, 'message': THRASH}}])
def test_other_compaction_failures_are_not_context_failures(error):
    assert not context_error(error)


@pytest.mark.parametrize('field', ['errors', 'result'])
@pytest.mark.parametrize('refusal', ['authentication_error', 'rate_limit_error', 'quota_exceeded'])
def test_claude_structured_refusal_wins_over_other_result_fields(field, refusal):
    tracker = RejectionTracker('claude')
    tracker.observe({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
        'error': {'type': refusal}, field: ['autocompact_thrashing'] if field == 'errors' else THRASH})
    assert not tracker.rejected
    assert not tracker.retryable('failed_recoverable')


def test_claude_structured_result_subtype_is_classified():
    tracker = RejectionTracker('claude')
    tracker.observe({'type': 'result', 'subtype': 'autocompact_thrashing', 'is_error': True})
    assert tracker.rejected
    assert tracker.rejection_reason == 'compaction_thrashing'
    assert tracker.retryable('failed_recoverable')


@pytest.mark.parametrize('engine', ENGINES)
@pytest.mark.parametrize('error', ['autocompact_thrashing', THRASH])
def test_thrashing_commits_failure_before_once_only_retry_and_survives_restart(tmp_path, engine, error):
    service, c, lease = setup(tmp_path, engine)
    runner = RejectingRunner(c, error=error)
    result = run(service, c, lease, runner)
    assert result['terminal_state'] == 'completed'
    assert runner.calls == 2
    assert runner.budgets[1] < runner.budgets[0]
    failed = c.archive.read(c.binding, 'admit:2')
    assert failed['terminal'] == 'failed_recoverable'
    events = failed['source']['events']
    diagnostic = next(e['context_admission'] for e in events if 'context_admission' in e)
    assert diagnostic['version'] == 1
    assert diagnostic['history_tokens'] == runner.budgets[0]
    assert diagnostic['planning_window'] == 32000
    assert set(events[0]) <= {'type', 'model', 'effort', 'engine'}
    assert set(diagnostic) == {'version', 'context_key', 'history_tokens', 'reason', 'rejection_reason',
        'planning_window', 'current_tokens', 'checkpoint_through', 'projected_rounds', 'fallback_history_budget'}
    evidence = [e['context_rejection_details'] for e in events if 'context_rejection_details' in e]
    assert evidence == [{'version': 1, 'reason': 'compaction_thrashing',
                         'history_tokens_before': runner.budgets[0]}]
    rebuilt = Service(tmp_path, storage='unified').connector(c.binding)
    rebuilt.context({'model': 'fixture', '_context_window': 32000, '_context_admission': True})
    assert rebuilt.admission['reason'] == 'context_rejected'
    assert rebuilt.admission['rejection_reason'] == 'compaction_thrashing'
    assert rebuilt.admission['history_tokens'] <= runner.budgets[0] // 2


@pytest.mark.parametrize('engine', ENGINES)
@pytest.mark.parametrize('case', ['progress', 'exhausted', 'compact', 'cancel', 'permission', 'approval'])
def test_thrashing_does_not_relax_replay_guards(tmp_path, engine, case):
    service, c, lease = setup(tmp_path, engine)
    runner = RejectingRunner(c, failures=5, progress=case == 'progress', error='autocompact_thrashing')
    original = runner.native
    if case in ('permission', 'approval'):
        # Exercise the real guarded callback, not only the tracker's boolean.
        from shepherd.unified_runners import UnifiedRunner
        callbacks = UnifiedRunner(c, **{'on_' + case: lambda *args: False})
        callback = getattr(callbacks, 'on_' + case)
    def native(*args):
        terminal = original(*args)
        if case == 'cancel':
            runner.cancelled.set()
        if case in ('permission', 'approval'):
            callbacks.context_tracker = runner.context_tracker
            callback({})
        return terminal
    method = {'claude': '_claude', 'pi': '_pi', 'codex': '_codex'}[engine]
    with mock.patch.object(runner, method, side_effect=native):
        result = run(service, c, lease, runner, '/compact' if case == 'compact' else 'continue')
    assert result['terminal_state'] == 'failed_recoverable'
    assert runner.calls == (2 if case == 'exhausted' else 1)
    limit = c.archive.context_limit(c.admission['context_key'])
    assert (limit is None) == (case == 'cancel')


@pytest.mark.parametrize('engine', ENGINES)
def test_unknown_terminal_never_learns_or_retries_thrashing(tmp_path, engine):
    service, c, lease = setup(tmp_path, engine)
    runner = RejectingRunner(c, error='autocompact_thrashing')
    original = runner.native
    def uncertain(*args):
        original(*args)
        raise Conflict('transport lost after native event')
    method = {'claude': '_claude', 'pi': '_pi', 'codex': '_codex'}[engine]
    with mock.patch.object(runner, method, side_effect=uncertain):
        with pytest.raises(Conflict, match='transport lost'):
            run(service, c, lease, runner)
    assert runner.calls == 1
    assert c.archive.context_limit(c.admission['context_key']) is None


@pytest.mark.parametrize('engine', ENGINES)
def test_native_recovery_success_clears_thrashing(engine):
    runner = mock.Mock()
    runner.connector.engine = engine
    tracker = RejectionTracker(engine)
    runner.on_event = tracker.observe
    reject(runner, 'autocompact_thrashing')
    assert tracker.rejected
    if engine == 'claude':
        tracker.observe({'type': 'result', 'subtype': 'success', 'is_error': False})
    elif engine == 'pi':
        tracker.observe({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'stop'}})
    else:
        tracker.observe('turn/completed', {'turn': {'status': 'completed'}})
    assert not tracker.rejected
    assert not tracker.rejection_reason


@pytest.mark.parametrize('engine', ENGINES)
def test_normal_text_does_not_trigger_thrashing_recovery(engine):
    tracker = RejectionTracker(engine)
    if engine == 'claude':
        tracker.observe({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': THRASH}]}})
    elif engine == 'pi':
        tracker.observe({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'stop',
            'content': [{'type': 'text', 'text': THRASH}]}})
    else:
        tracker.observe('item/completed', {'item': {'type': 'agentMessage', 'text': THRASH}})
    assert tracker.progress and not tracker.rejected


@pytest.mark.parametrize('engine', ENGINES)
def test_sealed_failure_is_not_retry_permission_until_coordinator_commit(tmp_path, engine):
    from shepherd.harness import ExecutionHarness
    service, c, lease = setup(tmp_path, engine)
    runner = RejectingRunner(c, error='autocompact_thrashing')
    with mock.patch.object(service.coordinator, 'commit', side_effect=Conflict('commit_verification_failed')):
        with pytest.raises(Conflict, match='commit_verification_failed'):
            run(service, c, lease, runner)
    assert runner.calls == 1
    # Raw sealed evidence exists, but admission must refuse an uncommitted boundary.
    assert c.archive.context_limit(c.admission['context_key'])['reason'] == 'compaction_thrashing'
    with pytest.raises(Conflict, match='coverage mismatch'):
        c.context({'model': 'fixture', '_context_admission': True})
    recovered = ExecutionHarness(service.coordinator, c, runner).recover('admit',
        lease_id=lease['lease_id'], generation=lease['generation'])
    assert recovered['terminal_state'] == 'failed_recoverable'
    assert runner.calls == 1  # Commit-only recovery never runs the request again.
    c.context({'model': 'fixture', '_context_admission': True})
    assert c.admission['reason'] == 'context_rejected'


@pytest.mark.parametrize('engine', ENGINES)
@pytest.mark.parametrize('after_append', [False, True])
def test_admission_diagnostic_failure_after_fence_never_dispatches_or_replays(tmp_path, engine, after_append):
    from shepherd.harness import ExecutionHarness
    service, c, lease = setup(tmp_path, engine)
    runner = RejectingRunner(c, error='autocompact_thrashing')
    original = c.archive.append
    def append(binding, turn_id, event):
        if 'context_admission' in event:
            if after_append:
                original(binding, turn_id, event)
            raise Conflict('diagnostic write lost before bridge ACK')
        return original(binding, turn_id, event)
    with mock.patch.object(c.archive, 'append', side_effect=append):
        with pytest.raises(Conflict, match='diagnostic write lost'):
            run(service, c, lease, runner)
    assert runner.calls == 0
    assert c.archive.context_limit(c.admission['context_key']) is None
    assert service.coordinator.state('admit')['inflight']['state'] == 'needs_reconciliation'
    with pytest.raises(Conflict, match='阻断'):
        ExecutionHarness(service.coordinator, c, runner).recover('admit',
            lease_id=lease['lease_id'], generation=lease['generation'])
    assert runner.calls == 0


@pytest.mark.parametrize('engine', ENGINES)
def test_legacy_rejection_receipts_remain_readable(tmp_path, engine):
    service, c, lease = setup(tmp_path, engine)
    original = c.archive.append
    def legacy(binding, turn_id, event):
        event = {key: value for key, value in event.items() if key != 'context_rejection_details'}
        return original(binding, turn_id, event)
    with mock.patch.object(c.archive, 'append', side_effect=legacy):
        run(service, c, lease, RejectingRunner(c, progress=True))
    rebuilt = Service(tmp_path, storage='unified').connector(c.binding)
    rebuilt.context({'model': 'fixture', '_context_admission': True})
    assert rebuilt.admission['reason'] == 'context_rejected'
    assert rebuilt.admission['rejection_reason'] == 'context_window_exceeded'


def test_local_compact_dispatch_success_does_not_erase_native_failure():
    tracker = RejectionTracker('claude')
    tracker.observe({'type': 'system', 'subtype': 'status',
                     'compact_result': 'failed', 'compact_error': 'autocompact_thrashing'})
    tracker.observe({'type': 'result', 'subtype': 'success', 'local_command': 'compact'})
    assert tracker.rejection_reason == 'compaction_thrashing'


@pytest.mark.parametrize('event', [
    {'type': 'system', 'subtype': 'hook_started'},
    {'type': 'attachment', 'attachment': {'type': 'hook_success'}},
    {'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'partial'}}},
    {'type': 'stream_event', 'event': {'type': 'content_block_start', 'content_block': {'type': 'tool_use', 'name': 'Bash'}}},
])
def test_claude_partial_output_and_hooks_forbid_replay(event):
    tracker = RejectionTracker('claude')
    tracker.observe(event)
    tracker.observe({'type': 'result', 'is_error': True, 'errors': ['autocompact_thrashing']})
    assert tracker.rejected and not tracker.retryable('failed_recoverable')
