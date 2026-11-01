"""按钮任务不把命令写进交互 shell，附着后才执行。"""
import ast
import os
from pathlib import Path
import pytest
import select
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def _opener(tmp_path):
    path = ROOT / 'desktron/lib/term_sessions.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_shell_term_open')
    pty = SimpleNamespace(pty_open=mock.Mock(return_value=('task', 123, True)), pty_input=mock.Mock())
    scope = dict(os=os, time=time, threading=threading, REPO_ROOT=str(tmp_path),
                 termpty=pty, _TERMS={}, _TERMS_LOCK=threading.Lock())
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(path), 'exec'), scope)
    return scope['_shell_term_open'], pty


def _pending_starter(pty, platform=None):
    path = ROOT / 'desktron/lib/ui_term_ws.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == '_start_pending_command')
    scope = dict(termpty=pty, _TERMS_LOCK=threading.Lock(),
                 os=SimpleNamespace(name=platform or os.name))
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(path), 'exec'), scope)
    return scope['_start_pending_command']


def _pty_writer(session, write):
    path = ROOT / 'desktron/lib/termpty.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'pty_input')
    fake_os = SimpleNamespace(write=write)
    scope = dict(os=fake_os, _LOCK=threading.Lock(), _PTY={'task': session})
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(path), 'exec'), scope)
    return scope['pty_input']


@pytest.mark.skipif(os.name == 'nt', reason='POSIX shell and pipe select contract')
def test_command_waits_for_attach_and_preserves_runtime_input(tmp_path, monkeypatch):
    startup = tmp_path / 'startup.sh'
    startup.write_text('echo UNEXPECTED_STARTUP; read answer\n')
    monkeypatch.setenv('BASH_ENV', str(startup))
    monkeypatch.setenv('SHELL', '/bin/zsh')
    opener, pty = _opener(tmp_path)
    row = opener(command='printf "TASK_READY\\n"; IFS= read -r reply; printf "REPLY=%s\\n" "$reply"; exit 7')
    assert row['command_pending'] is True
    pty.pty_input.assert_not_called()
    argv, env, cwd = pty.pty_open.call_args.args[:3]
    with subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as proc:
        assert not select.select([proc.stdout], [], [], .1)[0]
        out, err = proc.communicate('\nuser response\n', timeout=5)
    assert proc.returncode == 7
    assert out == 'TASK_READY\nREPLY=user response\n'
    assert not err


def test_plain_shell_still_uses_user_login_shell(tmp_path, monkeypatch):
    monkeypatch.setenv('SHELL', '/bin/zsh')
    opener, pty = _opener(tmp_path)
    row = opener()
    assert 'command_pending' not in row
    import oscompat
    assert pty.pty_open.call_args.args[0] == oscompat.interactive_shell_argv()


def test_only_registered_setup_terminal_passes_deployment_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('DESKTRON_SETUP_TASK_ID', 'inherited-task')
    opener, pty = _opener(tmp_path)
    opener(command='echo safe')
    assert 'DESKTRON_SETUP_TASK_ID' not in pty.pty_open.call_args.args[1]
    opener(command='echo safe', setup_task_id='setup_bound_123')
    assert pty.pty_open.call_args.args[1]['DESKTRON_SETUP_TASK_ID'] == 'setup_bound_123'
    pty.pty_open.reset_mock()
    assert opener(command='echo safe', setup_task_id='../escape') is None
    pty.pty_open.assert_not_called()


def test_real_pty_exit_is_saved_after_browser_detaches(tmp_path, monkeypatch):
    import termpty
    from provision import setup_tasks
    monkeypatch.setattr(setup_tasks, '_path', lambda: str(tmp_path / 'tasks.json'))
    monkeypatch.setattr(setup_tasks, '_owned', {})
    opener, pty = _opener(tmp_path)
    pty.pty_open.side_effect = termpty.pty_open
    sessions = []
    def spawn():
        command = 'Write-Output "TASK_DONE"; exit 7' if os.name == 'nt' else 'printf "TASK_DONE\\n"; exit 7'
        row = opener(command=command)
        sessions.append(row)
        return row
    task = setup_tasks.start('real_pty_request', 'test fixture command', spawn)
    sid = task['terminal_id']
    try:
        assert task['state'] == 'waiting_terminal'
        assert task['attachable']
        callback = mock.Mock()
        sink = lambda data: None
        termpty.pty_attach(sid, sink)
        termpty.pty_set_on_close(sid, callback)
        termpty.pty_detach(sid, sink)
        assert _pending_starter(termpty)(sessions[0], sid)
        # PowerShell cold start on loaded hosted runners exceeded 5 s and then 30 s
        # (runs 37669956043, 37672982426); the loop still exits as soon as the task fails.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            result = setup_tasks.latest()
            if result['state'] == 'failed':
                break
            time.sleep(.02)
        assert result['state'] == 'failed'
        assert result['exit_code'] == 7
        assert not result.get('output')
        assert not result['attachable']
        callback.assert_not_called()
    finally:
        if termpty.pty_info(sid):
            termpty.pty_kill(sid)


def test_real_pty_start_preserves_output_and_runtime_input(tmp_path, monkeypatch):
    import termpty
    from provision import setup_tasks
    monkeypatch.setattr(setup_tasks, '_path', lambda: str(tmp_path / 'tasks.json'))
    monkeypatch.setattr(setup_tasks, '_owned', {})
    opener, pty = _opener(tmp_path)
    pty.pty_open.side_effect = termpty.pty_open
    command = ('Write-Output "TASK_READY"; $reply = [Console]::In.ReadLine(); '
               'Write-Output "REPLY=$reply"; exit 7') if os.name == 'nt' else (
               'printf "TASK_READY\\n"; IFS= read -r reply; printf "REPLY=%s\\n" "$reply"; exit 7')
    sessions = []
    def spawn():
        row = opener(command=command)
        sessions.append(row)
        return row
    task = setup_tasks.start('real_pty_input_request', 'test fixture command', spawn)
    sid = task['terminal_id']
    output = bytearray()
    try:
        assert task['state'] == 'waiting_terminal'
        assert termpty.pty_attach(sid, output.extend)
        time.sleep(.1)
        assert b'TASK_READY' not in output
        start = _pending_starter(termpty)
        assert start(sessions[0], sid)
        assert not start(sessions[0], sid)
        deadline = time.monotonic() + 10
        while b'TASK_READY' not in output and time.monotonic() < deadline:
            time.sleep(.02)
        assert b'TASK_READY' in output
        signal = b'\r' if os.name == 'nt' else b'\n'
        assert termpty.pty_input(sid, b'synthetic reply' + signal)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = setup_tasks.latest()
            if result['state'] == 'failed':
                break
            time.sleep(.02)
        assert result['exit_code'] == 7
        assert b'REPLY=synthetic reply' in output
        assert output.count(b'TASK_READY') == 1
    finally:
        if termpty.pty_info(sid):
            termpty.pty_kill(sid)


@pytest.mark.parametrize('platform, signal', [('nt', b'\r'), ('posix', b'\n')])
def test_start_signal_is_platform_specific_and_sent_only_once(platform, signal):
    pty = SimpleNamespace(pty_input=mock.Mock(return_value=True))
    start = _pending_starter(pty, platform)
    row = {'command_pending': True}
    assert start(row, 'task') is True
    assert row == {}
    assert start(row, 'task') is False
    assert start({'command_starting': True, 'command_pending': True}, 'task') is False
    assert start({}, 'plain-shell') is False
    pty.pty_input.assert_called_once_with('task', signal)


@pytest.mark.parametrize('platform, signal', [('nt', b'\r'), ('posix', b'\n')])
def test_failed_first_write_keeps_command_pending_for_next_attach(platform, signal):
    pty = SimpleNamespace(pty_input=mock.Mock(return_value=False))
    start = _pending_starter(pty, platform)
    row = {'command_pending': True}
    assert start(row, 'task') is False
    assert row == {'command_pending': True}
    pty.pty_input.return_value = True
    assert start(row, 'task') is True
    assert 'command_pending' not in row
    assert pty.pty_input.call_args_list == [mock.call('task', signal), mock.call('task', signal)]


def test_pty_input_reports_success_and_failure():
    write = mock.Mock(side_effect=lambda _fd, data: len(data))
    assert _pty_writer({'master': 7}, write)('task', b'go') is True
    assert _pty_writer({'master': 7}, mock.Mock(side_effect=OSError('closed')))('task', b'go') is False
    assert _pty_writer({'master': 7}, write)('missing', b'go') is False


def test_conflicting_command_and_prefill_is_rejected(tmp_path):
    opener, pty = _opener(tmp_path)
    assert opener(command='echo test', init='other') is None
    pty.pty_open.assert_not_called()

