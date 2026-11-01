"""UTF-8 protocol pipes must not inherit a Windows GBK locale.

Only synthetic local children/bridge replies; no SDK, credentials or models.
"""
import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import subprocess
from unittest import mock

import pytest

from provision.install_check import isolated_env
from codex_appserver import _Connection
from shepherd.claude_connector import ClaudeConnector
from shepherd.pi_connector import PiConnector
from shepherd.resident_process import ResidentProcess
from shepherd.runner_process import NativeProcess

ROOT = Path(__file__).resolve().parents[2]
TEXT = "中文\u2410😀"


def _env(tmp_path):
    env = isolated_env(ROOT, tmp_path)
    # Do not let the test harness's UTF-8 mode mask a missing explicit codec.
    env.update(PYTHONUTF8='0', PYTHONIOENCODING='gbk')
    return env


def _bridge(tmp_path, reply):
    script = tmp_path / 'fixture_bridge.py'
    script.write_text('import sys\nsys.stdout.buffer.write(' +
                      repr(json.dumps(reply, ensure_ascii=False).encode('utf-8')) + ')\n',
                      encoding='utf-8')
    return str(script)


@pytest.mark.parametrize('resident', [False, True])
def test_json_stdio_roundtrip_under_gbk_locale(tmp_path, resident):
    code = ("import json,sys\n"
            "row=json.loads(sys.stdin.buffer.readline().decode('utf-8'))\n"
            "sys.stdout.buffer.write(json.dumps(row,ensure_ascii=False).encode('utf-8')+b'\\n')\n"
            "sys.stdout.buffer.flush()\n")
    with mock.patch('subprocess._text_encoding', return_value='gbk'):
        command = [sys.executable, '-u', '-c', code]
        kwargs = dict(cwd=str(tmp_path), env=_env(tmp_path), cancel_frame={'type': 'abort'})
        process = (ResidentProcess(command, **kwargs) if resident else
                   NativeProcess(command, timeout=5, hard_timeout=10, **kwargs))
        try:
            if resident:
                process.begin(timeout=5, hard_timeout=10)
            frame = {'type': 'fixture', 'text': TEXT}
            process.send(frame)
            assert next(process.events()) == frame
            assert process.proc.stdin.encoding.lower().replace('-', '') == 'utf8'
            assert process.proc.stdout.encoding.lower().replace('-', '') == 'utf8'
        finally:
            process.close()


def test_codex_json_stdin_under_gbk_locale(tmp_path):
    code = ("import json,sys\n"
            "row=json.loads(sys.stdin.buffer.readline().decode('utf-8'))\n"
            "sys.stdout.buffer.write(json.dumps(row,ensure_ascii=False).encode('utf-8')+b'\\n')\n"
            "sys.stdout.buffer.flush()\n")
    with mock.patch('subprocess._text_encoding', return_value='gbk'), \
            mock.patch('codex_appserver._appserver_argv', return_value=[sys.executable, '-u', '-c', code]):
        # Read through the transport, not stdout directly: on Windows its
        # bounded reader thread owns the pipe.
        connection = _Connection(cwd=str(tmp_path), env=_env(tmp_path))
        try:
            connection.notify('fixture', {'text': TEXT})
            frame = connection.read(timeout=5)
            assert frame['params']['text'] == TEXT
            assert connection.proc.stdin.encoding.lower().replace('-', '') == 'utf8'
        finally:
            connection.close()


def test_pi_store_reply_under_gbk_locale(tmp_path):
    reply = {'storeContract': 'desk-pi-store-v1', 'sessionId': 'fixture', 'text': TEXT}
    connector = SimpleNamespace(node_binary=sys.executable, bridge=_bridge(tmp_path, reply),
                                sdk_root='fixture-sdk', native_cwd=str(tmp_path),
                                native_session_id='fixture', directory=str(tmp_path))
    env = _env(tmp_path)
    with mock.patch('subprocess._text_encoding', return_value='gbk'), \
            mock.patch('shepherd.pi_connector.without_legacy_web_secrets', return_value=env):
        assert PiConnector._run(connector, 'verify', '-', str(tmp_path), None) == reply


def test_shepherd_text_readers_and_protocol_pipes_pin_utf8():
    root = ROOT / 'desktron/lib/shepherd'
    for path in root.glob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            keywords = {item.arg: item.value for item in node.keywords}
            is_reader = node.func.attr == 'read_text'
            is_protocol = (path.name in {'runner_process.py', 'resident_process.py',
                                        'pi_connector.py', 'claude_connector.py'} and
                           node.func.attr in {'Popen', 'run'} and
                           isinstance(keywords.get('text'), ast.Constant) and
                           keywords['text'].value is True)
            if is_reader or is_protocol:
                assert isinstance(keywords.get('encoding'), ast.Constant), (path.name, node.lineno)
                assert keywords['encoding'].value == 'utf-8', (path.name, node.lineno)


def test_policy_and_runner_initialization_under_gbk_locale(tmp_path):
    # Fresh interpreter: importing a module already cached by other tests would
    # miss the exact context_policy.json failure seen before every new turn.
    code = r'''
import pathlib, sys
from unittest import mock
assert sys.flags.utf8_mode == 0
original = pathlib.Path.read_text
def read_text(path, encoding=None, errors=None):
    return original(path, encoding=encoding or 'gbk', errors=errors)
with mock.patch.object(pathlib.Path, 'read_text', read_text):
    from shepherd.context_admission import context_error
    from shepherd.unified_runners import UnifiedRunner
    assert context_error('\u4e0a\u4e0b\u6587\u8d85\u9650')
    connector = mock.Mock(engine='pi', store_root=sys.argv[1], native_cwd=sys.argv[1])
    connector.binding = {'binding_id': 'fixture', 'conversation_id': 'fixture'}
    UnifiedRunner(connector, options={'model': 'fixture'}, command=['unused'], env={})
sys.stdout.buffer.write(b'policy-and-runner-ready')
'''
    result = subprocess.run(
        [sys.executable, '-X', 'utf8=0', '-c', code, str(tmp_path)],
        cwd=str(tmp_path), env=_env(tmp_path), capture_output=True,
        text=True, encoding='utf-8', timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'policy-and-runner-ready'


def test_claude_store_reply_under_gbk_locale(tmp_path):
    reply = {'storeContract': 'desk-claude-store-v1', 'revision': 'fixture',
             'shepherdIds': [], 'text': TEXT}
    connector = SimpleNamespace(node_binary=sys.executable, bridge=_bridge(tmp_path, reply),
                                sdk_root='fixture-sdk', native_cwd=str(tmp_path),
                                native_session_id='fixture',
                                native=SimpleNamespace(path='fixture.db', revision=lambda: 'fixture'))
    with mock.patch('subprocess._text_encoding', return_value='gbk'), \
            mock.patch.dict(os.environ, _env(tmp_path), clear=True):
        assert ClaudeConnector._verify(connector, []) == reply
