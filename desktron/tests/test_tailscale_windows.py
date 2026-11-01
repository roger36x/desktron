"""Windows CLI discovery/UTF-8 and public-ingress refusal; no native CLI calls."""
import json
import os
from types import SimpleNamespace

import pytest
import tailscale_serve as serve


def test_windows_program_files_cli_without_path(tmp_path, monkeypatch):
    cli = tmp_path / 'Tailscale/tailscale.exe'; cli.parent.mkdir(); cli.touch()
    monkeypatch.setattr(serve.shutil, 'which', lambda _: None)
    monkeypatch.setattr(serve, 'os', SimpleNamespace(name='nt', path=os.path,
                        environ={'PROGRAMFILES': str(tmp_path)}))
    assert serve._cli() == str(cli)


def test_native_json_is_utf8_and_error_output_is_not_exposed(monkeypatch):
    seen = []
    def run(argv, **kw):
        seen.append((argv, kw))
        return SimpleNamespace(returncode=0, stdout=json.dumps({'note': '中文'}))
    monkeypatch.setattr(serve.subprocess, 'run', run)
    assert serve._run_json('fixture.exe', 'status') == {'note': '中文'}
    assert seen[0][1]['encoding'] == 'utf-8'
    monkeypatch.setattr(serve.subprocess, 'run', lambda *_a, **_kw:
        SimpleNamespace(returncode=1, stdout='synthetic-private-detail', stderr='synthetic-private-detail'))
    with pytest.raises(RuntimeError) as error:
        serve._run_json('fixture.exe', 'status')
    assert 'synthetic-private-detail' not in str(error.value)


@pytest.mark.parametrize('nested', [False, True])
def test_funnel_never_advertises_pairing_url(monkeypatch, nested):
    public = {'AllowFunnel': {'phone.example.ts.net:443': True}}
    cfg = {'TCP': {'443': {'HTTPS': True}}, 'Web': {'phone.example.ts.net:443': {
        'Handlers': {'/': {'Proxy': 'http://127.0.0.1:7766'}}}}}
    cfg.update({'Foreground': {'fixture': public}} if nested else public)
    assert serve.serve_target(cfg, 'phone.example.ts.net', 7766) == ''
    monkeypatch.setattr(serve, '_cli', lambda: 'synthetic.exe')
    monkeypatch.setattr(serve, '_run_json', lambda _cli, *args:
        {'BackendState': 'Running', 'Self': {'DNSName': 'phone.example.ts.net.'}}
        if args == ('status',) else cfg)
    assert serve.status(7766) == {'state': 'public', 'command': ''}


def test_cli_failure_is_visible_redacted_and_not_a_setup_command(monkeypatch):
    monkeypatch.setattr(serve, '_cli', lambda: 'synthetic.exe')
    def fail(*_):
        raise OSError('synthetic-private-detail')
    monkeypatch.setattr(serve, '_run_json', fail)
    state = serve.status(7766)
    assert state['state'] == 'error' and state['command'] == ''
    assert 'synthetic-private-detail' not in json.dumps(state)


@pytest.mark.parametrize('url', ['http://127.0.0.1:7766/private',
    'http://127.0.0.1:7766/?secret=fixture', 'http://user@localhost:7766/',
    'http://localhost:bad/', 'https://localhost:7766/'])
def test_serve_requires_exact_uncredentialed_root_upstream(url):
    assert serve._proxies_to(url, 7766) is False
