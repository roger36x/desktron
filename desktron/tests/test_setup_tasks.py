"""任务状态不依赖页面；仅用假的 PTY，不执行安装或读取真实配置。"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from provision import setup_tasks as tasks


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, '_path', lambda: str(tmp_path / 'tasks.json'))
    monkeypatch.setattr(tasks, '_owned', {})
    live = {}
    monkeypatch.setattr(tasks, '_terminal', lambda sid: live.get(sid))
    return live


def test_repeated_request_and_reopened_page_do_not_spawn_twice(isolated):
    calls = []
    def spawn():
        calls.append(1)
        isolated['t1'] = {'pid': 99}
        return {'id': 't1'}
    a = tasks.start('request_123', 'lark-cli auth login', spawn)
    b = tasks.start('request_123', 'lark-cli auth login', spawn)
    c = tasks.start('new_page_456', 'lark-cli auth login', spawn)
    assert len(calls) == 1
    assert a['id'] == b['id'] == c['id']
    assert tasks.latest()['attachable']


def test_restart_cannot_reuse_same_terminal_id(isolated, monkeypatch):
    isolated['t1'] = {'pid': 99}
    tasks.start('request_123', 'command', lambda: {'id': 't1'})
    monkeypatch.setattr(tasks, '_BOOT', 'another-process')
    state = tasks.latest()
    assert state['state'] == 'interrupted'
    assert not state['attachable']


def test_exit_result_survives_browser_detach_without_output(isolated):
    isolated['t1'] = {'pid': 99}
    tasks.start('request_123', 'command', lambda: {'id': 't1'})
    tasks.finish_terminal('t1', {'code': 1, 'sig': None}, b'ok\ntoken=SECRET\nfailed')
    state = tasks.latest()
    assert state['state'] == 'failed'
    assert state['exit_code'] == 1
    assert 'SECRET' not in json.dumps(state)
    assert not state.get('output')   # 登录终端的输出可能含一次性验证码,一律不留
    assert not state['attachable']


def test_unknown_exit_is_not_success(isolated):
    tasks.start('request_123', 'command', lambda: {'id': 't1'})
    tasks.finish_terminal('t1', {'code': None, 'sig': None}, b'')
    assert tasks.latest()['state'] == 'interrupted'


def test_request_identity_cannot_change_command(isolated):
    tasks.start('request_123', 'one', lambda: {'id': 't1'})
    with pytest.raises(ValueError):
        tasks.start('request_123', 'two', lambda: {'id': 't2'})


def test_failed_registration_never_starts_command(isolated):
    calls = []
    with patch.object(tasks, 'write', side_effect=OSError('full')):
        with pytest.raises(OSError):
            tasks.start('request_123', 'command', lambda: calls.append(1))
    assert not calls


def test_auth_resume_only_keeps_known_secret_free_commands(isolated):
    task = tasks.start('request_auth', 'lark-cli auth login', lambda: {'id': 't1'})
    assert task['auth_command'] == 'lark-cli auth login'
    assert tasks._description('codex login')['auth_command'] == 'codex login'
    assert tasks._description('lark-cli auth login --token SECRET')['auth_command'] == ''


@pytest.mark.parametrize('raw', ['{broken', '{"request": {}}', '{"request": null}',
                               '{"request": {"id": "request", "state": []}}'])
def test_unreadable_tasks_never_become_permission_to_start(isolated, raw):
    from pathlib import Path
    path = Path(tasks._path())
    path.write_text(raw)
    calls = []
    with pytest.raises(ValueError):
        tasks.start('request_123', 'command', lambda: calls.append(1))
    assert not calls
    assert path.read_text() == raw
