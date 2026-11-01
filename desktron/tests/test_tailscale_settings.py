"""Synthetic Tailscale setup: private persistence, CAS, loopback and local-only API."""
import io
import json
from types import SimpleNamespace

import pytest
import durable_json
import remote_auth
import tailscale_settings as settings


@pytest.fixture(autouse=True)
def private_config(tmp_path, monkeypatch):
    import credstore
    monkeypatch.setattr(settings, 'config_path', lambda: tmp_path / 'config/tailscale.json')
    monkeypatch.setattr(settings, '_RUNTIME', None)
    monkeypatch.setattr(remote_auth, '_RUNTIME_TOKEN', None)
    monkeypatch.setattr(credstore, '_secrets_read', lambda: {})
    for key in (remote_auth.TOKEN_ENV, remote_auth.BIND_ENV, remote_auth.SERVE_PORT_ENV):
        monkeypatch.delenv(key, raising=False)


def enable(port=7766):
    return settings.save({'enabled': True, 'port': port}, settings.status()['revision'])


def test_disabled_is_no_write_and_no_cli_or_listener():
    assert settings.status() == {'enabled': False, 'port': 7766, 'revision': 'new',
                                 'managed': True, 'active_port': 0, 'restart_required': False}
    assert not settings.config_path().exists()
    assert remote_auth.token() == '' and remote_auth.serve_port(7765) is None
    assert remote_auth.bind_host() == '127.0.0.1'


def test_enable_generates_private_token_but_never_broadens_bind():
    public = enable()
    config = settings.read_config()
    assert config['enabled'] and len(config['token']) == 43
    assert config['token'] not in json.dumps(public) and 'token' not in public
    assert public['restart_required'] and public['active_port'] == 0
    assert settings.status(7766)['restart_required'] is False
    assert remote_auth.serve_port(7765) == 7766
    assert remote_auth.bind_host() == '127.0.0.1'
    assert remote_auth.check('127.0.0.1', {}, '/', proxied=True) == (False, False)
    assert remote_auth.check('127.0.0.1', {'Authorization': 'Bearer ' + config['token']}, '/', proxied=True) == (True, False)
    assert remote_auth.check('127.0.0.1', {}, '/') == (True, False)


def test_disable_fails_closed_and_reenable_keeps_pairing():
    first = enable(); token = settings.read_config()['token']
    disabled = settings.save({'enabled': False, 'port': 7766}, first['revision'], active_port=7766)
    assert disabled['restart_required'] and not disabled['enabled']
    assert remote_auth.token() == '' and remote_auth.serve_port(7765) is None
    assert remote_auth.check('127.0.0.1', {'Authorization': 'Bearer ' + token}, '/', proxied=True) == (False, False)
    enable()
    assert remote_auth.token() == token


@pytest.mark.parametrize('value', [None, {}, {'enabled': True, 'port': True},
    {'enabled': 1, 'port': 7766}, {'enabled': True, 'port': '7766'},
    {'enabled': True, 'port': 80}, {'enabled': True, 'port': 65536},
    {'enabled': True, 'port': 7765}, {'enabled': True, 'port': 7766, 'token': 'forged'}])
def test_invalid_config_never_writes(value):
    with pytest.raises(ValueError):
        settings.save(value, 'new')
    assert not settings.config_path().exists()


def test_stale_revision_and_concurrent_snapshot_cannot_replace_token(monkeypatch):
    first = enable()
    with pytest.raises(durable_json.ConflictError):
        settings.save({'enabled': False, 'port': 7766}, 'new')
    original = durable_json.write
    def race(path, snapshot):
        current = durable_json.read(path)
        current['revision'] = 'a' * 32
        original(path, current)
        return original(path, snapshot)
    monkeypatch.setattr(durable_json, 'write', race)
    with pytest.raises(durable_json.ConflictError):
        settings.save({'enabled': False, 'port': 7766}, first['revision'])
    assert settings.read_config()['enabled']


@pytest.mark.parametrize('raw', ['{}', '{bad', '{"schema":1,"token":"synthetic-secret"}', '[]'])
def test_corrupt_config_is_not_replaced_and_errors_are_redacted(raw):
    path = settings.config_path(); path.parent.mkdir(parents=True)
    path.write_text(raw, encoding='utf-8')
    with pytest.raises(ValueError) as error:
        enable()
    assert 'synthetic-secret' not in str(error.value)
    assert path.read_text(encoding='utf-8') == raw


@pytest.mark.parametrize('legacy', [False, True])
def test_unreadable_runtime_keeps_local_access_without_opening_serve(monkeypatch, legacy):
    if legacy:
        monkeypatch.setenv(remote_auth.TOKEN_ENV, 'legacy-fixture')
        monkeypatch.setenv(remote_auth.SERVE_PORT_ENV, '7766')
    def unreadable():
        raise PermissionError('synthetic private failure')
    monkeypatch.setattr(settings, 'read_config', unreadable)
    with pytest.raises(PermissionError):
        settings.load_runtime()
    assert settings.runtime_config()['failed']
    remote_auth.load_runtime_token()
    assert remote_auth.bind_host() == '127.0.0.1'
    assert remote_auth.serve_port(7765) is None
    assert remote_auth.check('127.0.0.1', {}, '/') == (True, False)
    with pytest.raises(PermissionError):
        settings.status()  # Do not hide the damaged saved state behind a default.
    with pytest.raises(RuntimeError):
        settings.load_runtime()


def test_legacy_port_is_read_only_and_legacy_token_preserved(monkeypatch):
    monkeypatch.setenv(remote_auth.TOKEN_ENV, 'legacy-fixture')
    monkeypatch.setenv(remote_auth.SERVE_PORT_ENV, '7766')
    assert not settings.status(7766)['managed']
    with pytest.raises(ValueError, match='DESKTRON_SERVE_PORT'):
        enable()
    assert not settings.config_path().exists()
    assert remote_auth.bind_host() == '127.0.0.1'
    assert remote_auth.token() == 'legacy-fixture'


def test_whitespace_environment_uses_same_legacy_fallback_as_listener(monkeypatch):
    import credstore
    monkeypatch.setenv(remote_auth.SERVE_PORT_ENV, ' ')
    monkeypatch.setattr(credstore, '_secrets_read', lambda: {
        remote_auth.SERVE_PORT_ENV: '7766', remote_auth.TOKEN_ENV: 'legacy-fixture'})
    assert settings.legacy_port() == '7766'
    assert remote_auth.serve_port(7765) == 7766
    assert remote_auth.bind_host() == '127.0.0.1'
    assert not settings.status(7766)['managed']
    with pytest.raises(ValueError, match='DESKTRON_SERVE_PORT'):
        enable()


@pytest.mark.parametrize('failure', ['after_replace', 'readback'])
def test_postcommit_failure_is_an_unknown_outcome(monkeypatch, failure):
    if failure == 'after_replace':
        original = durable_json.write
        def committed_then_error(path, snapshot):
            original(path, snapshot)
            raise OSError('synthetic failure after replace')
        monkeypatch.setattr(durable_json, 'write', committed_then_error)
    else:
        def unreadable_status(*_):
            raise OSError('synthetic readback failure')
        monkeypatch.setattr(settings, 'status', unreadable_status)
    with pytest.raises(settings.SaveOutcomeUnknown, match='结果未知'):
        settings.save({'enabled': True, 'port': 7766}, 'new')
    assert settings.read_config()['enabled']


def test_existing_nonloopback_policy_is_not_silently_changed(monkeypatch):
    monkeypatch.setenv(remote_auth.BIND_ENV, 'all')
    monkeypatch.setenv(remote_auth.TOKEN_ENV, 'legacy-fixture')
    with pytest.raises(ValueError, match='loopback'):
        enable()
    assert remote_auth.bind_host() == '0.0.0.0'
    assert not settings.config_path().exists()


class Handler:
    def __init__(self, *, proxied=False, client='127.0.0.1', origin=None):
        self.server = SimpleNamespace(server_address=('127.0.0.1', 7765), serve_port=0, serve_proxy=proxied)
        self.client_address = (client, 100)
        self.headers = {'Host': '127.0.0.1:7765', 'Content-Type': 'application/json'}
        if origin:
            self.headers['Origin'] = origin
        self.rfile = io.BytesIO()
    def _lan_local_request(self):
        from ui_remote_gate import UiRemoteGateMixin
        return UiRemoteGateMixin._lan_local_request(self)
    def _serve_proxied(self):
        return self.server.serve_proxy
    def _send_json(self, value, code=200):
        return code, value
    def body(self, raw):
        self.rfile = io.BytesIO(raw); self.headers['Content-Length'] = str(len(raw))
        return self


@pytest.mark.parametrize('h', [Handler(proxied=True), Handler(client='192.0.2.1'),
    Handler(origin='https://cross-site.example')])
def test_remote_and_cross_origin_cannot_read_or_write(h):
    from system import api
    for method in ('GET', 'PUT'):
        assert api.handle(h, '/workbench/tailscale', {}, method)[0] == 403
    assert not settings.config_path().exists()


def test_api_conflict_redaction_fetch_site_and_size_limits():
    from system import api
    assert api.owns('/workbench/tailscale', 'PUT')
    h = Handler()
    assert api.handle(h, '/workbench/tailscale', {}, 'GET')[0] == 200
    raw = json.dumps({'config': {'enabled': True, 'port': 7766}, 'expected_revision': 'new'}).encode()
    code, body = api.handle(h.body(raw), '/workbench/tailscale', {}, 'PUT')
    assert code == 200 and body['restart_required'] and 'token' not in body
    assert api.handle(h.body(raw), '/workbench/tailscale', {}, 'PUT')[0] == 409
    assert api.handle(h.body(b'[]'), '/workbench/tailscale', {}, 'PUT')[0] == 400
    assert api.handle(h.body(b' ' * 1025), '/workbench/tailscale', {}, 'PUT')[0] == 400
    h.headers['Content-Type'] = 'text/plain'
    assert api.handle(h.body(raw), '/workbench/tailscale', {}, 'PUT')[0] == 400
    h.headers['Sec-Fetch-Site'] = 'cross-site'
    assert api.handle(h, '/workbench/tailscale', {}, 'GET')[0] == 403


def test_api_marks_postcommit_failure_unknown_without_disclosing_token(monkeypatch):
    from system import api
    original = durable_json.write
    def committed_then_error(path, snapshot):
        original(path, snapshot)
        raise OSError('synthetic private detail')
    monkeypatch.setattr(durable_json, 'write', committed_then_error)
    raw = json.dumps({'config': {'enabled': True, 'port': 7766}, 'expected_revision': 'new'}).encode()
    code, body = api.handle(Handler().body(raw), '/workbench/tailscale', {}, 'PUT')
    assert code == 503 and body['state'] == 'unknown'
    assert '结果未知' in body['error'] and 'synthetic private detail' not in json.dumps(body)
    assert settings.read_config()['token'] not in json.dumps(body)
