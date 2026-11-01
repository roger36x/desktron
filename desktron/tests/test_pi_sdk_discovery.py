"""CLI-bound SDK discovery and factory compatibility; no model prompts."""
import json
from pathlib import Path
import shutil
import subprocess
from unittest import mock

import pytest

from engine_protocol import isolated_env
from shepherd.pi_connector import pi_sdk_root

LIB = Path(__file__).resolve().parents[1] / 'lib'
PACKAGE = '@earendil-works/pi-coding-agent'


def package(root, name=PACKAGE):
    (root / 'dist').mkdir(parents=True)
    (root / 'package.json').write_text(json.dumps({'name': name, 'type': 'module'}), encoding='utf-8')
    (root / 'dist/index.js').write_text('throw new Error("Do not import UI entrypoint");', encoding='utf-8')
    (root / 'dist/cli.js').touch()
    return root


def managed(tmp_path, version='1.0.2'):
    agent = tmp_path / 'agent'
    (agent / 'bin').mkdir(parents=True)
    (agent / 'bin/pi-launcher.js').touch()
    (agent / 'install').mkdir()
    (agent / 'install/managed-install.json').write_text(json.dumps({
        'kind': 'pi-managed-install', 'schemaVersion': 1, 'layout': 'releases-v1'}), encoding='utf-8')
    (agent / 'install/current-version').write_text(version + '\n', encoding='utf-8')
    return agent


@pytest.mark.parametrize('entry', ['pi', 'pi.cmd', 'pi-launcher.js'])
def test_managed_launcher_finds_current_release_only(tmp_path, entry):
    agent = managed(tmp_path)
    old = package(agent / 'install/releases/0.85.1/node_modules' / PACKAGE)
    current = package(agent / 'install/releases/1.0.2/node_modules' / PACKAGE)
    binary = agent / 'bin' / entry
    binary.touch()
    assert pi_sdk_root(binary) == str(current.resolve())
    with mock.patch('shepherd.pi_connector.shutil.which', return_value=str(binary)):
        assert pi_sdk_root() == str(current.resolve())
    (agent / 'install/current-version').write_text('0.85.1', encoding='utf-8')
    assert pi_sdk_root(binary) == str(old.resolve())


@pytest.mark.parametrize('version', ['', '.', '..', '../other', r'..\other', '/abs', r'C:\abs'])
def test_invalid_managed_pointer_does_not_guess_another_release(tmp_path, version):
    agent = managed(tmp_path, version)
    package(agent / 'install/releases/1.0.2/node_modules' / PACKAGE)
    binary = agent / 'bin/pi.cmd'; binary.touch()
    assert pi_sdk_root(binary) is None


def test_incomplete_or_unrelated_managed_install_is_not_an_sdk(tmp_path):
    agent = managed(tmp_path)
    binary = agent / 'bin/pi.cmd'; binary.touch()
    assert pi_sdk_root(binary) is None
    root = package(agent / 'install/releases/1.0.2/node_modules' / PACKAGE, 'unrelated')
    assert pi_sdk_root(binary) is None
    (root / 'package.json').write_text('[]', encoding='utf-8')
    assert pi_sdk_root(binary) is None


@pytest.mark.parametrize('prefix', ['%dp0%\\', '%~dp0', '%~dp0\\'])
def test_windows_npm_shim_uses_its_target_package(tmp_path, prefix):
    root = package(tmp_path / 'node_modules' / PACKAGE)
    binary = tmp_path / 'pi.cmd'
    binary.write_text('@echo off\nnode "' + prefix + 'node_modules\\' +
                      PACKAGE.replace('/', '\\') + '\\dist\\cli.js" %*\n', encoding='utf-8')
    (tmp_path / 'node.exe').write_bytes(b'MZ')
    assert pi_sdk_root(binary) == str(root.resolve())


def test_direct_cli_keeps_its_sdk_even_when_another_release_is_current(tmp_path):
    agent = managed(tmp_path)
    old = package(agent / 'install/releases/0.85.1/node_modules' / PACKAGE)
    package(agent / 'install/releases/1.0.2/node_modules' / PACKAGE)
    assert pi_sdk_root(old / 'dist/cli.js') == str(old.resolve())


def test_missing_cli_does_not_guess_from_home():
    with mock.patch('shepherd.pi_connector.shutil.which', return_value=None):
        assert pi_sdk_root() is None


def test_installed_pi_bridge_initializes_and_reads_state_without_dispatch(tmp_path):
    from shepherd.runner_process import NativeProcess
    sdk = pi_sdk_root()
    node = shutil.which('node')
    if not sdk or not node:
        pytest.skip('Installed Pi SDK and Node required')
    sid = 'b3ff4ce0-9295-47b4-836d-6b98f8a8a01f'
    bootstrap = tmp_path / 'bootstrap.json'
    bootstrap.write_text(json.dumps({'id': sid, 'messages': [], 'entries': None}), encoding='utf-8')
    process = NativeProcess([node, str(LIB / 'shepherd/pi_memory_rpc.mjs'), sdk,
                             str(bootstrap), '--no-tools'], cwd=str(tmp_path),
                            env=isolated_env(tmp_path), timeout=10, hard_timeout=15,
                            cancel_frame={'type': 'abort'})
    try:
        events = process.events()
        ready = next(events)
        startup = ready.pop('startup_timing')
        assert ready == {'type': 'protocol_ready', 'contract': 'desk-pi-execute-v1',
                         'sessionId': sid}
        from shepherd.phase_timing import HOST_STAGES
        assert set(startup) == HOST_STAGES
        assert all(set(row) == {'elapsed_ms', 'duration_ms'} and
                   0 <= row['duration_ms'] <= row['elapsed_ms']
                   for row in startup.values())
        process.send({'id': 'offline-check', 'type': 'get_state'})
        response = next(row for row in events if row.get('id') == 'offline-check')
        assert response['success'] and response['command'] == 'get_state'
        assert response['data']['sessionId'] == sid
        assert response['data']['isStreaming'] is False
        assert response['data']['messageCount'] == 0
        process.end_input()
        remaining = list(events)
        assert not any(row.get('type') == 'agent_start' for row in remaining)
        assert process.proc.returncode == 0
    finally:
        process.close()


@pytest.mark.parametrize('location', ['sdk', 'services', 'missing'])
def test_factory_location_is_capability_checked_without_importing_ui(tmp_path, location):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    root = package(tmp_path / 'sdk')
    (root / 'dist/core').mkdir()
    exports = ('export const createAgentSessionServices=()=>"services"; '
               'export const createAgentSessionFromServices=()=>"session";')
    (root / 'dist/core/sdk.js').write_text(exports if location == 'sdk' else
                                         'export const createAgentSession=()=>{};', encoding='utf-8')
    if location == 'services':
        (root / 'dist/core/agent-session-services.js').write_text(exports, encoding='utf-8')
    script = '''
import {loadPiFactory} from %s;
try {
  const factory=await loadPiFactory(process.argv[1]);
  console.log(JSON.stringify({services:factory.createAgentSessionServices(),
    session:factory.createAgentSessionFromServices()}));
} catch(error) {console.log(JSON.stringify({missing:error.message}));}
''' % json.dumps((LIB / 'shepherd/pi_sdk.mjs').as_uri())
    result = subprocess.run([node, '--input-type=module', '-e', script, str(root)],
                            cwd=tmp_path, env=isolated_env(tmp_path), stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, encoding='utf-8', timeout=10, check=True)
    report = json.loads(result.stdout)
    if location == 'missing':
        assert report == {'missing': 'SDK.createAgentSessionServices'}
    else:
        assert report == {'services': 'services', 'session': 'session'}
