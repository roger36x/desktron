"""LAN settings use only synthetic topology and isolated private files."""
import json
from types import SimpleNamespace

import pytest
import durable_json
import lan_settings as settings
import remote_auth
import tailscale_settings
from test_tailscale_settings import Handler

_REQUIRE_LOCAL = settings._require_local
_CANDIDATES = settings.candidates


@pytest.fixture(autouse=True)
def private_config(tmp_path, monkeypatch):
    import credstore
    monkeypatch.setattr(settings, 'config_path', lambda: tmp_path / 'config/lan.json')
    monkeypatch.setattr(tailscale_settings, 'config_path', lambda: tmp_path / 'config/tailscale.json')
    monkeypatch.setattr(settings, '_RUNTIME', None)
    monkeypatch.setattr(remote_auth, '_RUNTIME_TOKEN', None)
    monkeypatch.setattr(settings, 'candidates', lambda: (['192.168.50.10/24'], ''))
    monkeypatch.setattr(settings, '_require_local', lambda _: None)
    monkeypatch.setattr(credstore, '_secrets_read', lambda: {})
    for key in (remote_auth.TOKEN_ENV, remote_auth.BIND_ENV, remote_auth.SERVE_PORT_ENV):
        monkeypatch.delenv(key, raising=False)


def save(enabled=True, address='192.168.50.10/24', active=''):
    return settings.save({'enabled': enabled, 'interface': address},
                         settings.read_config()['revision'], active_interface=active)


def test_default_no_write_no_listener_no_token():
    state = settings.status()
    assert not state['enabled'] and not state['restart_required']
    assert not settings.config_path().exists()
    assert remote_auth.token() == ''
    assert remote_auth.bind_host() == '127.0.0.1'


def test_save_is_private_and_does_not_activate_runtime():
    public = save()
    config = settings.read_config()
    assert len(config['token']) == 43
    assert config['token'] not in json.dumps(public) and 'token' not in public
    assert public['restart_required'] and public['active_interface'] == ''
    assert remote_auth.lan_interface() is None and remote_auth.token() == ''
    settings.load_runtime()
    assert str(remote_auth.lan_interface()) == '192.168.50.10/24'
    assert remote_auth.bind_host() == '127.0.0.1'
    assert remote_auth.token() == config['token']
    assert not settings.status('192.168.50.10/24')['restart_required']
    assert remote_auth.check('192.168.50.20', {}, '/') == (False, False)
    assert remote_auth.check('192.168.50.20', {'Authorization': 'Bearer ' + config['token']}, '/') == (True, False)
    assert remote_auth.check('10.1.1.1', {'Authorization': 'Bearer ' + config['token']}, '/desk-probe') == (False, False)


def test_tailscale_changes_do_not_replace_active_lan_cookie_until_restart():
    save(); settings.load_runtime(); remote_auth.load_runtime_token()
    token = remote_auth.token()
    headers = {'Cookie': remote_auth.COOKIE + '=' + token}
    first = tailscale_settings.save({'enabled': True, 'port': 7766}, 'new')
    assert tailscale_settings.read_config()['token'] != token
    assert remote_auth.token() == token
    assert remote_auth.check('192.168.50.20', headers, '/') == (True, False)
    tailscale_settings.save({'enabled': False, 'port': 7766}, first['revision'])
    assert remote_auth.token() == token
    assert remote_auth.check('192.168.50.20', headers, '/') == (True, False)
    with pytest.raises(RuntimeError):
        remote_auth.load_runtime_token()


def test_tailscale_disable_preserves_boot_token_when_lan_is_also_active():
    save()
    first = tailscale_settings.save({'enabled': True, 'port': 7766}, 'new')
    settings.load_runtime(); remote_auth.load_runtime_token()
    token = remote_auth.token()
    tailscale_settings.save({'enabled': False, 'port': 7766}, first['revision'])
    assert remote_auth.token() == token
    assert remote_auth.check('192.168.50.20', {'Cookie': remote_auth.COOKIE + '=' + token}, '/') == (True, False)


def test_saved_change_and_disable_cannot_widen_active_policy_or_remove_auth():
    save(); settings.load_runtime(); token = remote_auth.token()
    state = save(address='192.168.0.10/16', active='192.168.50.10/24')
    assert state['restart_required']
    assert str(remote_auth.lan_interface()) == '192.168.50.10/24'
    assert not remote_auth.client_allowed('192.168.60.20')
    state = save(False, active='192.168.50.10/24')
    assert not state['enabled'] and state['restart_required']
    assert remote_auth.token() == token
    assert remote_auth.check('192.168.50.20', {}, '/') == (False, False)
    with pytest.raises(RuntimeError):
        settings.load_runtime()


@pytest.mark.parametrize('address', ['8.8.8.8/24', '192.168.50.10/0', '100.64.1.1/10',
    '169.254.1.1/16', '127.0.0.1/8', '::1/128', '192.168.50.0/24', '192.168.50.255/24',
    '192.168.50.10', 'bad', '', None])
def test_invalid_address_cannot_open_interface(address):
    with pytest.raises(ValueError):
        save(address=address)
    assert not settings.config_path().exists()


@pytest.mark.parametrize('value', [None, {}, {'enabled': 1, 'interface': '192.168.50.10/24'},
    {'enabled': True, 'interface': '192.168.50.10/24', 'token': 'forged'}])
def test_invalid_shape_never_writes(value):
    with pytest.raises(ValueError):
        settings.save(value, 'new')
    assert not settings.config_path().exists()


def test_address_must_be_owned_by_machine(monkeypatch):
    # Exercise the real ownership check with a deterministic failing socket.
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def bind(self, _): raise OSError('synthetic private failure')
    monkeypatch.setattr(settings.socket, 'socket', lambda *_: Socket())
    with pytest.raises(ValueError, match='不属于'):
        _REQUIRE_LOCAL(settings.parse_interface('192.168.50.10/24'))


@pytest.mark.parametrize('legacy', ['loopback', 'all', 'lan:192.168.50.10/24'])
def test_explicit_legacy_bind_remains_authoritative(monkeypatch, legacy):
    monkeypatch.setenv(remote_auth.BIND_ENV, legacy)
    assert not settings.status()['managed']
    with pytest.raises(ValueError, match='DESKTRON_REMOTE_BIND'):
        save()
    assert not settings.config_path().exists()


def test_cas_prevents_stale_and_concurrent_replacement(monkeypatch):
    save()
    with pytest.raises(durable_json.ConflictError):
        settings.save({'enabled': False, 'interface': ''}, 'new')
    original = durable_json.write
    def race(path, snapshot):
        current = durable_json.read(path); current['revision'] = 'a' * 32
        original(path, current)
        return original(path, snapshot)
    monkeypatch.setattr(durable_json, 'write', race)
    with pytest.raises(durable_json.ConflictError):
        save(False)
    assert settings.read_config()['enabled']


@pytest.mark.parametrize('raw', ['{}', '[]', '{broken', '{"token":"synthetic-private"}'])
def test_corruption_preserved_and_fail_closed_even_with_legacy_token(monkeypatch, raw):
    path = settings.config_path(); path.parent.mkdir(parents=True)
    path.write_text(raw, encoding='utf-8')
    monkeypatch.setenv(remote_auth.TOKEN_ENV, 'legacy-fixture')
    with pytest.raises(ValueError, match='配置损坏'):
        settings.load_runtime()
    assert remote_auth.bind_host() == '127.0.0.1'
    assert path.read_text(encoding='utf-8') == raw


def test_windows_detection_filters_non_private_and_reports_failure(monkeypatch):
    import os
    monkeypatch.setattr(settings, 'os', SimpleNamespace(name='nt', environ=os.environ))
    data = [{'IPAddress': '192.168.50.10', 'PrefixLength': 24},
            {'IPAddress': '100.64.0.1', 'PrefixLength': 32},
            {'IPAddress': '127.0.0.1', 'PrefixLength': 8}]
    def run(command, **kwargs):
        assert command[:3] == ['powershell.exe', '-NoProfile', '-NonInteractive']
        assert kwargs['timeout'] == 8 and kwargs['encoding'] == 'utf-8'
        return SimpleNamespace(returncode=0, stdout=json.dumps(data))
    monkeypatch.setattr(settings.subprocess, 'run', run)
    assert _CANDIDATES() == (['192.168.50.10/24'], '')
    monkeypatch.setattr(settings.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(returncode=1))
    values, error = _CANDIDATES()
    assert values == [] and '读取失败' in error


def test_postcommit_failure_is_unknown_not_definite_failure(monkeypatch):
    original = durable_json.write
    def commit_then_fail(path, snapshot):
        original(path, snapshot)
        raise OSError('synthetic private detail')
    monkeypatch.setattr(durable_json, 'write', commit_then_fail)
    with pytest.raises(settings.SaveOutcomeUnknown, match='结果未知'):
        save()
    assert settings.read_config()['enabled']


@pytest.mark.parametrize('h', [Handler(proxied=True), Handler(client='192.0.2.1'),
    Handler(origin='https://cross-site.example')])
def test_api_denies_remote_proxy_and_cross_origin(h):
    from system import api
    for method in ('GET', 'PUT'):
        assert api.handle(h, '/workbench/lan', {}, method)[0] == 403
    assert not settings.config_path().exists()


def test_api_revision_limits_privacy_and_unknown_result(monkeypatch):
    from system import api
    assert api.owns('/workbench/lan', 'PUT')
    h = Handler()
    assert api.handle(h, '/workbench/lan', {}, 'GET')[0] == 200
    raw = json.dumps({'config': {'enabled': True, 'interface': '192.168.50.10/24'}, 'expected_revision': 'new'}).encode()
    code, result = api.handle(h.body(raw), '/workbench/lan', {}, 'PUT')
    assert code == 200 and result['restart_required'] and 'token' not in result
    assert api.handle(h.body(raw), '/workbench/lan', {}, 'PUT')[0] == 409
    for value in (b'[]', b' ' * 1025):
        assert api.handle(h.body(value), '/workbench/lan', {}, 'PUT')[0] == 400
    h.headers['Content-Type'] = 'text/plain'
    assert api.handle(h.body(raw), '/workbench/lan', {}, 'PUT')[0] == 400
    h.headers['Content-Type'] = 'application/json'
    h.headers['Sec-Fetch-Site'] = 'cross-site'
    assert api.handle(h, '/workbench/lan', {}, 'GET')[0] == 403
    del h.headers['Sec-Fetch-Site']
    original = durable_json.write
    def commit_then_fail(path, snapshot):
        original(path, snapshot)
        raise OSError('synthetic private detail')
    monkeypatch.setattr(durable_json, 'write', commit_then_fail)
    raw = json.dumps({'config': {'enabled': False, 'interface': ''}, 'expected_revision': result['revision']}).encode()
    code, result = api.handle(h.body(raw), '/workbench/lan', {}, 'PUT')
    assert code == 503 and result['state'] == 'unknown'
    assert 'synthetic private detail' not in json.dumps(result)
