"""Lazy bootstrap writes must not bypass Pi encoding or dispatch fences."""
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import pytest
from shepherd.pi_codec import CodecError, encode_round
from shepherd.unified_runners import UnifiedRunner
from shepherd.coordinator import digest


READY = {'type':'protocol_ready','contract':'desk-pi-execute-v1','sessionId':'native'}

ROOT = Path(__file__).resolve().parents[2]

ROUND = {'offset': 1, 'round_id': 'conversation:1', 'entry_hash': 'a' * 64,
         'public': [{'type': 'message', 'role': 'user',
                     'content': [{'type': 'input_text', 'text': 'previous question'}]}]}


@pytest.fixture
def setup_runner(tmp_path):
    connector = mock.Mock()
    connector.store_root = str(tmp_path)
    connector.engine = 'pi'
    connector.binding = {'binding_id': 'binding', 'conversation_id': 'conversation'}
    connector.native_session_id = 'native'
    connector.native_cwd = str(tmp_path)
    connector.encode.side_effect = encode_round
    runtime = mock.Mock()
    runner = UnifiedRunner(connector, options={'model': 'fixture'},
                           command=['pi', '--mode', 'rpc'], env={}, on_runtime=runtime)
    runner.runtime_observer = runtime
    runner.execution = mock.Mock()
    runner.expected_offset = 2
    runner._payload = mock.Mock(return_value=str(tmp_path / 'bootstrap.json'))
    host = mock.Mock()
    host.events.return_value = iter([{'type': 'agent_settled'}])
    dispatch = mock.Mock()
    with ExitStack() as stack:
        stack.enter_context(mock.patch('shepherd.pi_connector.pi_sdk_root', return_value='/fixture/sdk'))
        stack.enter_context(mock.patch('shepherd.pi_connector.PiConnector.availability', return_value={'available':True}))
        stack.enter_context(mock.patch('shepherd.resident_process.enabled', return_value=True))
        acquire = stack.enter_context(mock.patch('shepherd.resident_process.acquire'))
        create = stack.enter_context(mock.patch('shepherd.resident_process.ResidentProcess', return_value=host))
        stack.enter_context(mock.patch('shepherd.resident_process.drop'))
        acquire.return_value = (host, False)
        yield runner, connector, host, dispatch, acquire, create


def test_review_renews_only_after_steered_agent_run_finishes(setup_runner):
    runner, connector, host, dispatch, acquire, create = setup_runner
    runner.options['_execution_review'] = True
    runner._review_request = 'execution-review-1'
    host.events.return_value = iter([
        {'type':'response', 'id':'execution-review-1', 'command':'steer', 'success':True},
        {'type':'agent_start'}, {'type':'agent_end'}, {'type':'agent_settled'},
    ])
    host.review_snapshot.side_effect = [
        {'status':'injected'}, {'status':'injected'}, {'status':'running'}]
    host.accept_review.return_value = {'status':'injected'}
    host.complete_review.return_value = {'status':'running'}
    assert runner._pi('next', [], None, dispatch, None) == 'completed'
    host.accept_review.assert_called_once_with()
    host.complete_review.assert_called_once_with()


@pytest.mark.parametrize('ready', [{}, {**READY,'sessionId':'foreign'}, {**READY,'contract':'future'}])
def test_missing_or_mismatched_ready_never_dispatches(setup_runner, ready):
    from shepherd.store import Conflict
    runner, connector, host, dispatch, acquire, create = setup_runner
    acquire.side_effect = lambda binding,key,through,factory: (factory(),True)
    host.events.return_value = iter([ready])
    with pytest.raises(Conflict, match='初始化未确认'):
        runner._pi('next', [], None, dispatch, None)
    dispatch.assert_not_called()
    host.send.assert_not_called()


@pytest.mark.parametrize('enabled', [False, True])
def test_unified_runner_web_extension_defaults_on_and_follows_explicit_disable(setup_runner, enabled):
    runner, connector, host, dispatch, acquire, create = setup_runner
    runner.command = None
    runner.env = {} if enabled else {'DESKTRON_WEB_TOOLS':'0'}
    acquire.side_effect = lambda binding, key, through, factory: (factory(), True)
    host.events.return_value = iter([READY, {'type':'agent_settled'}])
    assert runner._pi('next', [], None, dispatch, None) == 'completed'
    command = create.call_args.args[0]
    assert ('pi_web.ts' in ' '.join(command)) is enabled
    assert '--no-skills' in command
    assert '--no-context-files' in command
    assert '--skill' not in command


def test_unified_runner_preserves_prebuilt_web_command_without_provider_secret(setup_runner):
    runner, connector, host, dispatch, acquire, create = setup_runner
    runner.command = ['pi', '--mode', 'rpc', '--extension', '/fixture/pi_web.ts',
                      '--skill', '/fixture/skills']
    runner.env = {'DESKTRON_WEB_TOOLS':'1'}
    acquire.side_effect = lambda binding, key, through, factory: (factory(), True)
    host.events.return_value = iter([READY, {'type':'agent_settled'}])
    assert runner._pi('next', [], None, dispatch, None) == 'completed'
    first_key = acquire.call_args.args[1]
    first_command = create.call_args.args[0]
    assert '/fixture/pi_web.ts' in first_command
    assert '--no-skills' in first_command
    assert '--no-context-files' in first_command
    assert '--skill' not in first_command
    assert '/fixture/skills' not in first_command

    acquire.reset_mock(); create.reset_mock(); host.events.return_value = iter([READY, {'type':'agent_settled'}])
    runner.env['VOLCENGINE_WEB_SEARCH_API_KEY'] = 'legacy-fixture'
    assert runner._pi('next', [], None, dispatch, None) == 'completed'
    second_key = acquire.call_args.args[1]
    assert first_key == second_key
    assert 'legacy-fixture' not in second_key
    assert 'VOLCENGINE_WEB_SEARCH_API_KEY' not in create.call_args.kwargs['env']


def test_minimal_custom_command_receives_real_grounding_and_isolation(setup_runner):
    from grounding import builder
    from pi_extensions import appended_system_prompts
    runner, connector, host, dispatch, acquire, create = setup_runner
    # Read repo-owned grounding, not the live workspace registry (whose
    # deskcfg import requires server argv and may read user state).
    domains = mock.Mock()
    domains._grounding_index.return_value = ''
    with mock.patch.object(builder, '_domains', return_value=domains):
        grounding = builder.constant(str(ROOT), 'desk-iter', 'pi')
    assert 'memory/scripts/mem_read.py' in grounding
    runner.static_system_text = grounding
    runner.system_text = grounding + '\n\n' + runner.history_hint
    runner.command = ['pi', '--mode', 'rpc', '--skill', '/fixture/skills']
    acquire.side_effect = lambda binding, key, through, factory: (factory(), True)
    host.events.return_value = iter([READY, {'type':'agent_settled'}])
    assert runner._pi('next', [], None, dispatch, None) == 'completed'
    command = create.call_args.args[0]
    assert '--no-skills' in command
    assert '--no-context-files' in command
    assert '--skill' not in command
    assert any('memory/scripts/mem_read.py' in value for value in appended_system_prompts(command))


def test_system_prompt_hash_matches_final_pi_prefix(setup_runner):
    runner, connector, host, dispatch, acquire, create = setup_runner
    runner.command += ['--append-system-prompt', 'PRIVATE_SYSTEM_PREFIX']
    expected = digest('PRIVATE_SYSTEM_PREFIX\n\n' + runner.history_hint)
    assert runner.system_prompt_hash() == expected
    assert 'PRIVATE_SYSTEM_PREFIX' not in runner.system_prompt_hash()


def test_hot_host_skips_bootstrap_write_but_encodes_history(setup_runner):
    runner, connector, host, dispatch, acquire, create = setup_runner
    assert runner._pi('next question', [ROUND], None, dispatch, None) == 'completed'
    connector.encode.assert_called_once_with(ROUND)
    runner._payload.assert_not_called()
    create.assert_not_called()
    assert acquire.call_args.args[:1] == ('binding',)
    assert acquire.call_args.args[2] == 1
    dispatch.assert_called_once_with(process=host.proc)
    runner.runtime_observer.assert_called_once_with(
        {'resident': True, 'resident_reused': True})
    host.finish.assert_called_once()


def test_runtime_observer_failure_cannot_block_dispatch(setup_runner):
    runner, connector, host, dispatch, acquire, create = setup_runner
    runner.on_runtime = mock.Mock(side_effect=RuntimeError('diagnostic'))
    assert runner._pi('next question', [], None, dispatch, None) == 'completed'
    dispatch.assert_called_once_with(process=host.proc)


@pytest.mark.parametrize('public', [
    [{'type': 'function_call_output', 'call_id': 'missing', 'output': 'value'}],
    [{'type': 'function_call', 'call_id': 'unfinished', 'name': 'read', 'arguments': '{}'}],
    [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'unsupported'}]}],
])
def test_hot_path_still_rejects_malformed_history_before_acquire(setup_runner, public):
    runner, connector, host, dispatch, acquire, create = setup_runner
    with pytest.raises(CodecError):
        runner._pi('next', [{**ROUND, 'public': public}], None, dispatch, None)
    acquire.assert_not_called()
    dispatch.assert_not_called()
    runner.execution.spawning.assert_not_called()
    runner._payload.assert_not_called()


def test_cold_host_writes_complete_checkpoint_and_tail_before_spawn(setup_runner):
    runner, connector, host, dispatch, acquire, create = setup_runner
    checkpoint = {'payload': {'native': [{'type': 'session', 'id': 'native'}]}}
    stages = []
    host.events.return_value = iter([READY, {'type':'agent_settled'}])
    runner._payload.side_effect = lambda payload: stages.append('payload') or '/fixture/bootstrap.json'
    runner.execution.spawning.side_effect = lambda: stages.append('spawning')
    acquire.side_effect = lambda binding, key, through, factory: (factory(), True)
    assert runner._pi('next', [ROUND], checkpoint, dispatch, None) == 'completed'
    assert stages == ['payload', 'spawning']
    payload = runner._payload.call_args.args[0]
    assert payload == {'id': 'native', 'messages': encode_round(ROUND),
                       'entries': checkpoint['payload']['native'], 'compact': False}
    assert '/fixture/bootstrap.json' in create.call_args.args[0]
    assert '/fixture/bootstrap.json' not in acquire.call_args.args[1]
    runner.runtime_observer.assert_called_once_with(
        {'resident': True, 'resident_reused': False})


@pytest.mark.parametrize('compact', [False, True])
def test_nonresident_and_manual_compaction_still_build_bootstrap(setup_runner, compact):
    runner, connector, host, dispatch, acquire, create = setup_runner
    runner._native_process = mock.Mock(return_value=host)
    prompt = '/compact' if compact else 'next'
    host.events.return_value = iter([READY, {'type':'agent_settled'}])
    if compact:
        runner.checkpoint = [{'saved': True}]
        host.events.return_value = iter([READY, {'type': 'memory_compact_done', 'status': 'completed'}])
    with mock.patch('shepherd.resident_process.enabled', return_value=False):
        assert runner._pi(prompt, [ROUND], None, dispatch, None) == 'completed'
    runner._payload.assert_called_once()
    assert runner._payload.call_args.args[0]['compact'] is compact
    acquire.assert_not_called()
    runner.runtime_observer.assert_called_once_with(
        {'resident': False, 'resident_reused': False})
    host.close.assert_called_once()


def test_bootstrap_write_failure_does_not_spawn_or_dispatch(setup_runner):
    runner, connector, host, dispatch, acquire, create = setup_runner
    acquire.side_effect = lambda binding, key, through, factory: (factory(), True)
    runner._payload.side_effect = OSError('fixture disk full')
    with pytest.raises(OSError, match='disk full'):
        runner._pi('next', [ROUND], None, dispatch, None)
    runner.execution.spawning.assert_not_called()
    create.assert_not_called()
    dispatch.assert_not_called()


@pytest.mark.parametrize('prepared', [{}, {'type':'fallback_prepared'},
    {'type':'fallback_prepared','fallbackReceipt':{'checkpoint_id':'wrong'}}])
def test_fallback_receipt_failure_cannot_dispatch(setup_runner, prepared):
    from shepherd.store import Conflict
    runner, connector, host, dispatch, acquire, create = setup_runner
    # UnifiedRunner evicts any resident before a degraded generation. Exercise
    # the reachable cold-host path whose bootstrap must prove exact fallback.
    acquire.side_effect=lambda binding,key,through,factory:(factory(),True)
    runner.fallback_receipt={'checkpoint_id':'degraded_fixture','text_sha256':'a'*64}
    host.events.return_value=iter([READY, prepared])
    checkpoint={'payload':{'native':{'degraded_checkpoint_id':'degraded_fixture','items':[]}}}
    with pytest.raises(Conflict):runner._pi('next',[],checkpoint,dispatch,None)
    dispatch.assert_not_called()
    host.send.assert_not_called()
    host.finish.assert_called_once()
