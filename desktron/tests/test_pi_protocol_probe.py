"""Pi admission cold-start budget/diagnostics. Synthetic state, no model prompts."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from unittest import mock

import pytest

import engine_protocol as protocol
from shepherd.pi_connector import pi_sdk_root

LIB = Path(__file__).resolve().parents[1] / 'lib'


@pytest.fixture
def pi_sdk(tmp_path):
    # No skip: delivery cannot claim the real probe without its dependencies.
    node = shutil.which('node')
    selected = pi_sdk_root()
    assert node and selected, 'Managed Node and selected Pi SDK are required'
    installed = Path(selected)
    root = tmp_path / 'synthetic-sdk'
    root.mkdir()
    (root / 'package.json').write_text(json.dumps({'type': 'module', 'version': 'probe-fixture'}), encoding='utf-8')

    def write(name, source):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding='utf-8')

    for name in ('dist/core/session-manager.js', 'dist/cli/args.js', 'dist/modes/rpc/rpc-mode.js',
                 'dist/config.js', 'dist/core/output-guard.js', 'dist/core/settings-manager.js',
                 'dist/core/http-dispatcher.js'):
        write(name, f'export * from {json.dumps((installed / name).as_uri())};')
    # Only this synthetic wrapper is delayed/mutated. Installed SDK untouched.
    factory = f'''import {{loadPiFactory}} from {json.dumps((LIB / 'shepherd/pi_sdk.mjs').as_uri())};
const factory = await loadPiFactory({json.dumps(str(installed))});
export const createAgentSessionServices = (...args) => factory.createAgentSessionServices(...args);
export const createAgentSessionFromServices = (...args) => factory.createAgentSessionFromServices(...args);
'''
    return root, node, write, factory


def test_cold_pi_probe_over_old_fifteen_second_budget_passes_full_contract(pi_sdk):
    root, node, write, factory = pi_sdk
    write('dist/core/sdk.js', 'await new Promise(resolve => setTimeout(resolve, 16200));\n' + factory)
    status = protocol.sdk_availability('pi', root, node)
    assert status['available'], status
    assert status['contract'] == 'desk-pi-sdk-v1'
    assert status['probe_timeout_s'] == 45
    assert status['probe_phase'] == 'done'
    assert 16200 <= status['probe_elapsed_ms'] < 45000
    assert status['sdk_version'] == 'probe-fixture'  # Label does not grant admission.


def test_pi_probe_still_rejects_changed_capability_without_version_cache(pi_sdk):
    root, node, write, factory = pi_sdk
    write('dist/core/sdk.js', factory)
    assert protocol.sdk_availability('pi', root, node)['available']
    write('dist/core/sdk.js', 'export const createAgentSessionServices = () => {};')
    status = protocol.sdk_availability('pi', root, node)
    assert status['reason'] == 'pi_protocol_incompatible'
    assert 'SDK.createAgentSessionFromServices' in status['missing_capabilities']
    assert status['probe_phase'] == 'imports'


def test_pi_probe_hang_is_bounded_and_phase_is_visible(pi_sdk):
    root, node, write, _ = pi_sdk
    write('dist/core/sdk.js', 'await new Promise(() => {setInterval(() => {}, 100);});')
    start = time.monotonic()
    with mock.patch.dict(protocol.SDK_PROBE_TIMEOUTS, pi=3):
        status = protocol.sdk_availability('pi', root, node)
    assert status['reason'] == 'pi_probe_timeout'
    assert status['probe_phase'] == 'imports'
    assert status['probe_timeout_s'] == 3
    assert '加载 SDK 模块' in status['message'] and '消息未派发' in status['message']
    assert time.monotonic() - start < 9  # Includes the owned child's bounded cleanup.


@pytest.mark.parametrize('busy_attempts', [1, 99])
def test_probe_home_cleanup_failure_never_rewrites_timeout(tmp_path, busy_attempts):
    # Windows CI: a just-killed child kept a file open, rmtree raised WinError 32 and
    # the timeout was reported as "probe could not start" (run 37669956043).
    (tmp_path / 'package.json').write_text('{}', encoding='utf-8')
    calls = []
    real_rmtree = shutil.rmtree
    def rmtree(path, *args, **kwargs):
        calls.append(path)
        if len(calls) <= busy_attempts:
            raise PermissionError(13, 'file in use', path)
        return real_rmtree(path, *args, **kwargs)
    timeout = subprocess.TimeoutExpired(['node'], 3)
    with mock.patch.object(protocol, 'run_probe', side_effect=timeout), \
            mock.patch.object(protocol.shutil, 'rmtree', side_effect=rmtree), \
            mock.patch.object(protocol.time, 'sleep'):
        status = protocol.sdk_availability('pi', tmp_path, sys.executable)
    assert status['reason'] == 'pi_probe_timeout'
    leftover = Path(calls[0])
    assert leftover.exists() == (busy_attempts >= 20)
    real_rmtree(leftover, ignore_errors=True)


@pytest.mark.parametrize('engine,budget', [('pi', 45), ('claude', 15)])
@pytest.mark.parametrize('storage_only', [False, True])
def test_sdk_budget_is_explicit_for_both_probe_modes(tmp_path, engine, budget, storage_only):
    (tmp_path / 'package.json').write_text('{}', encoding='utf-8')
    with mock.patch.object(protocol, 'run_probe', return_value={
            'available': True, 'contract': f'desk-{engine}-sdk-v1'}) as probe:
        assert protocol.sdk_availability(engine, tmp_path, sys.executable, sys.executable,
                                         storage_only=storage_only)['available']
    assert probe.call_args.kwargs == {'timeout': budget}


def test_windows_probe_profile_and_temp_do_not_fall_back_to_real_user(tmp_path):
    home = str(tmp_path)
    with mock.patch.dict(os.environ, {'USERPROFILE': 'synthetic-real-profile',
            'APPDATA': 'synthetic-private-config', 'LOCALAPPDATA': 'synthetic-private-cache',
            'TEMP': 'synthetic-shared-temp', 'NODE_OPTIONS': '--require synthetic-secret',
            'OPENAI_API_KEY': 'synthetic-secret'}), mock.patch.object(protocol.os, 'name', 'nt'):
        # Mocking os.name changes pathlib's platform selection on POSIX.
        with mock.patch.object(protocol, 'Path', type(tmp_path)):
            env = protocol.isolated_env(tmp_path)
    assert env['USERPROFILE'] == env['HOME'] == env['TEMP'] == env['TMP'] == home
    assert env['APPDATA'] == str(tmp_path / 'appdata')
    assert env['LOCALAPPDATA'] == str(tmp_path / 'localappdata')
    assert 'synthetic-secret' not in json.dumps(env)
    assert 'synthetic-real-profile' not in json.dumps(env)


@pytest.mark.parametrize('payload', [
    'not json', '[]', '{"phase": []}',
    '{"phase":"synthetic-secret","elapsed_ms":1}',
    '{"phase":"imports","elapsed_ms":true}',
    '{"phase":"imports","elapsed_ms":-1}',
    '{"phase":"imports","elapsed_ms":300001}',
    '{"phase":"imports","elapsed_ms": "synthetic-secret"}',
    'x' * 4097,
])
def test_progress_rejects_untrusted_or_oversize_fields(tmp_path, payload):
    (tmp_path / 'engine-probe-progress.json').write_text(payload, encoding='utf-8')
    assert protocol.probe_progress(tmp_path) == {}


def test_progress_only_retains_fixed_phase_and_integer_timing(tmp_path):
    (tmp_path / 'engine-probe-progress.json').write_text(json.dumps({
        'phase': 'imports', 'elapsed_ms': 17000, 'error': 'synthetic-secret',
        'configuration': {'token': 'synthetic-secret'}}), encoding='utf-8')
    assert protocol.probe_progress(tmp_path) == {'probe_phase': 'imports', 'probe_phase_started_ms': 17000}


def test_timeout_retains_progress_before_disposable_state_is_removed(tmp_path):
    # One synthetic owned child: no SDK, engine CLI, requests, or native stderr.
    script = tmp_path / 'probe.py'
    script.write_text('''import json, os, time
from pathlib import Path
Path(os.environ['DESKTRON_PROTOCOL_PROGRESS']).write_text(
    json.dumps({'phase': 'imports', 'elapsed_ms': 42}), encoding='utf-8')
time.sleep(30)
''', encoding='utf-8')
    with pytest.raises(subprocess.TimeoutExpired) as error:
        protocol.run_probe([sys.executable, str(script)], tmp_path, timeout=2)
    assert error.value.desk_progress == {'probe_phase': 'imports', 'probe_phase_started_ms': 42}
    assert 2000 <= error.value.desk_elapsed_ms < 8000


def test_progress_write_failure_does_not_change_sdk_admission(pi_sdk, tmp_path):
    root, node, write, factory = pi_sdk
    write('dist/core/sdk.js', factory)
    env = protocol.isolated_env(tmp_path)
    env['DESKTRON_PROTOCOL_PROGRESS'] = str(tmp_path / 'missing' / 'progress.json')
    with mock.patch.object(protocol, 'isolated_env', return_value=env):
        status = protocol.sdk_availability('pi', root, node)
    assert status['available'], status
    assert 'probe_phase' not in status


def test_owned_probe_cleans_up_child_if_protection_fails(tmp_path):
    child = mock.Mock()
    child.poll.return_value = None
    with mock.patch.object(protocol.subprocess, 'Popen', return_value=child), \
         mock.patch.object(protocol, 'protect', side_effect=OSError('synthetic failure')):
        with pytest.raises(OSError):
            protocol.run_probe([sys.executable], tmp_path)
    child.kill.assert_called_once_with()
    child.wait.assert_called_once_with(timeout=5)


@pytest.mark.parametrize('reason', ['probe_timeout', 'protocol_incompatible'])
def test_failed_pi_probe_still_blocks_native_spawn_and_message_dispatch(tmp_path, reason):
    from shepherd.pi_connector import PiConnector
    from shepherd.service import Service
    from shepherd.store import Conflict
    from shepherd.unified_runners import UnifiedRunner

    service = Service(tmp_path / 'state', storage='unified')
    service.create('synthetic-probe-session', 'pi', tmp_path)
    runner = UnifiedRunner(service.connector(service.active('synthetic-probe-session')['binding']))
    dispatch = mock.Mock()
    with mock.patch.object(PiConnector, 'availability', return_value=protocol.failure(
            'pi', reason, 'synthetic probe failure')), \
         mock.patch.object(runner, '_native_process') as spawn:
        with pytest.raises(Conflict, match='消息未派发'):
            runner._pi('synthetic input', [], None, dispatch, mock.Mock())
    dispatch.assert_not_called()
    spawn.assert_not_called()


@pytest.mark.parametrize('phase', ['synthetic-secret', [], {}])
def test_timeout_diagnostic_never_echoes_native_command_or_error(tmp_path, phase):
    (tmp_path / 'package.json').write_text('{}', encoding='utf-8')
    error = subprocess.TimeoutExpired(['synthetic-secret'], 45, stderr=b'synthetic-secret')
    error.desk_progress = {'probe_phase': phase, 'probe_phase_started_ms': 1}
    with mock.patch.object(protocol, 'run_probe', side_effect=error):
        status = protocol.sdk_availability('pi', tmp_path, sys.executable)
    assert status['reason'] == 'pi_probe_timeout'
    assert 'synthetic-secret' not in json.dumps(status)
    assert 'probe_phase' not in status
