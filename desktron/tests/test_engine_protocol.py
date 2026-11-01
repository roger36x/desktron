"""Release labels cannot authorize/deny execution; actual capabilities can.

Exercises engine_sdk_probe.mjs and shepherd/pi_runtime_contract.mjs through the
same isolated subprocess contract used by the connectors.
"""
import json
from pathlib import Path
import shutil
import subprocess
from unittest import mock

import pytest

import engine_protocol as protocol
from shepherd.claude_connector import ClaudeConnector
from shepherd.pi_connector import PiConnector, pi_sdk_root
from shepherd.store import Conflict


def sdk_fixture(root, version):
    root.mkdir(exist_ok=True)
    (root / 'package.json').write_text(json.dumps({'version': version}))
    (root / 'sdk.mjs').write_text('''
export const getSessionMessages=async(id,options)=>options.sessionStore.load({sessionId:id});
export const query=()=>({initializationResult:async()=>({commands:[],models:[]}),
 interrupt:async()=>{},close:()=>{}});
''')
    return root


@pytest.mark.parametrize('version', ['0.3.268', '9.999.0', 'preview', ''])
def test_claude_sdk_label_never_decides_admission(tmp_path, version):
    if not shutil.which('node'): pytest.skip('Node unavailable')
    root = sdk_fixture(tmp_path / 'sdk', version)
    binary = tmp_path / 'claude'; binary.touch()
    # The fixture implements the SDK handshake; the selected CLI is never used
    # by it. Real CLI handshake/dispatch is covered by the native test suite.
    status = ClaudeConnector.availability(root, str(binary))
    assert status['available'], status
    (root / 'sdk.mjs').write_text('export const query=()=>{};')
    status = ClaudeConnector.availability(root, str(binary))
    assert not status['available']
    assert status['reason'] == 'claude_protocol_incompatible'
    assert 'SDK.getSessionMessages' in status['missing_capabilities']


def test_current_pi_modules_with_unknown_package_label_and_changed_store(tmp_path):
    sdk = pi_sdk_root()
    if not sdk or not shutil.which('node'): pytest.skip('Pi SDK unavailable')
    root = tmp_path / 'pi'; root.mkdir()
    (root / 'package.json').write_text('{"version":"99.999.0"}')
    (root / 'dist').symlink_to(Path(sdk) / 'dist', target_is_directory=True)
    assert PiConnector.availability(root)['available']
    (root / 'dist').unlink(); (root / 'dist').mkdir()
    (root / 'dist/core').mkdir()
    (root / 'dist/core/session-manager.js').write_text('export class SessionManager {}', encoding='utf-8')
    status = PiConnector.availability(root)
    assert status['reason'] == 'pi_protocol_incompatible'
    assert status['sdk_version'] == '99.999.0'


def test_pi_store_identity_failures_are_separately_named():
    source = (Path(__file__).parents[1] / 'lib/shepherd/pi_runtime_contract.mjs').read_text()
    for capability in ('SessionManager.session-id', 'SessionManager.cwd', 'SessionManager.format-v3'):
        assert capability in source
    assert 'SessionManager.identity-and-format' not in source


@pytest.mark.parametrize('engine', ['claude', 'pi'])
@pytest.mark.parametrize('mode,reason', [('timeout','probe_timeout'), ('oserror','probe_failed'),
                                        ('malformed','protocol_unverified')])
def test_sdk_probe_errors_are_explicit_and_do_not_claim_version_incompatibility(tmp_path, engine, mode, reason):
    root = sdk_fixture(tmp_path / 'sdk', 'future')
    node = tmp_path / 'node'; node.touch()
    error = {'timeout':subprocess.TimeoutExpired('probe',15), 'oserror':OSError(),
             'malformed':ValueError()}[mode]
    with mock.patch.object(protocol, 'run_probe', side_effect=error):
        status = protocol.sdk_availability(engine, root, str(node), str(node))
    assert status['reason'] == engine + '_' + reason
    assert not status['available'] and '消息未派发' in status['message']


def test_probe_environment_carries_neither_credentials_nor_parent_session(tmp_path):
    with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY':'synthetic', 'NODE_OPTIONS':'--require bad',
        'CLAUDE_CODE_SESSION_ID':'parent', 'OPENAI_API_KEY':'synthetic', 'PI_CODING_AGENT_DIR':'/user'}):
        env = protocol.isolated_env(tmp_path)
    assert not {'ANTHROPIC_API_KEY','OPENAI_API_KEY','NODE_OPTIONS','CLAUDE_CODE_SESSION_ID'} & env.keys()
    assert env['HOME'] == env['CLAUDE_CONFIG_DIR'] == env['PI_CODING_AGENT_DIR'] == str(tmp_path)


@pytest.mark.parametrize('engine', ['pi','claude'])
def test_unproven_sdk_blocks_unified_runner_before_dispatch(tmp_path, engine):
    from shepherd.service import Service
    from shepherd.unified_runners import UnifiedRunner
    service = Service(tmp_path / 'state', storage='unified')
    service.create('conversation', engine, tmp_path)
    runner = UnifiedRunner(service.connector(service.active('conversation')['binding']))
    dispatch = mock.Mock()
    method = runner._pi if engine == 'pi' else runner._claude_oneshot
    connector = PiConnector if engine == 'pi' else ClaudeConnector
    with mock.patch.object(connector, 'availability', return_value=protocol.failure(
            engine, 'protocol_incompatible', '缺少测试能力')), \
         mock.patch.object(runner, '_native_process') as spawn:
        with pytest.raises(Conflict, match='缺少测试能力'):
            method('hello', [], None, dispatch, mock.Mock())
    dispatch.assert_not_called(); spawn.assert_not_called()
