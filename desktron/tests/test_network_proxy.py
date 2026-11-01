"""Network policy tests use temporary files and loopback fixtures, no real configuration."""
import io
import json
import os
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace

import pytest

import durable_json
import network_proxy as network

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def policy(tmp_path, monkeypatch):
    path = tmp_path / 'network.json'
    monkeypatch.setattr(network, 'config_path', lambda: path)
    monkeypatch.setattr(network, 'system_proxies', lambda: ({}, []))
    monkeypatch.setattr(network.sys, 'platform', 'win32')
    for key in list(os.environ):
        if key.startswith('DESKTRON_NETWORK_') or key.startswith('DESKTRON_BROWSER_NETWORK_'):
            monkeypatch.delenv(key)
    return path


def manual(port=7890, host='127.0.0.1'):
    return {'mode': 'manual', 'host': host, 'port': port, 'bypass': ['.internal.example']}


@pytest.mark.parametrize('port', [7890, 10808, 65535, 1])
def test_manual_port_is_not_hardcoded(policy, port):
    before = dict(os.environ)
    state = network.save(manual(port), 'legacy')
    assert dict(os.environ) == before, 'save never mutates live process networking'
    assert state['pending'] and not state['browser_capable']
    assert state['config']['port'] == port
    assert all(host in state['config']['bypass'] for host in network.LOOPBACK)
    env = {'HTTP_PROXY': 'old', 'http_proxy': 'old', 'ALL_PROXY': 'old', 'no_proxy': 'old'}
    network.apply_environment(env)
    assert env['HTTP_PROXY'] == env['http_proxy'] == f'http://127.0.0.1:{port}'
    assert env['HTTPS_PROXY'] == env['https_proxy'] == env['HTTP_PROXY']
    assert 'ALL_PROXY' not in env
    args = network.browser_policy()['args']
    assert f'http=http://127.0.0.1:{port}' in args[0]
    assert f'https=http://127.0.0.1:{port}' in args[0]
    assert 'localhost;127.0.0.1;[::1]' in args[1]
    assert '*.internal.example' in args[1]
    assert '<-loopback>' not in str(args)


def test_absent_config_preserves_legacy_behavior(policy):
    env = {'HTTPS_PROXY': 'legacy', 'NO_PROXY': 'legacy'}
    network.apply_environment(env)
    assert env == {'HTTPS_PROXY': 'legacy', 'NO_PROXY': 'legacy'}
    assert network.browser_policy() == {'args': [], 'revision': 'legacy', 'resolution': 'legacy'}


@pytest.mark.parametrize('change', [
    {'port': 0}, {'port': 65536}, {'port': True}, {'port': '7;bad'},
    {'host': 'http://127.0.0.1'}, {'host': 'user:password@host'},
    {'host': 'x --no-proxy-server'}, {'host': 'bad..host'},
    {'bypass': ['<-loopback>']}, {'bypass': ['*']}, {'bypass': ['x\nY']},
    {'password': 'synthetic'}, {'mode': 'unknown'}])
def test_invalid_or_credential_input_never_persists(policy, change):
    with pytest.raises(ValueError):
        network.save({**manual(), **change}, 'legacy')
    assert not policy.exists()


def test_direct_clears_all_proxy_variants_and_pi_is_guarded(policy):
    network.save({'mode': 'direct'}, 'legacy')
    env = {k: 'legacy' for k in ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy']}
    network.apply_environment(env)
    assert not any(k.lower() in {'http_proxy', 'https_proxy', 'all_proxy'} for k in env)
    assert env['NO_PROXY'] == env['no_proxy'] == '*'
    assert network.browser_policy()['args'] == ['--no-proxy-server']


def test_system_ignores_inherited_proxy_and_resolves_same_browser_rules(policy, monkeypatch):
    monkeypatch.setattr(network, 'system_proxies', lambda: (
        {'http': 'http://127.0.0.1:12000', 'https': 'http://127.0.0.1:13000'}, ['.corp.example']))
    network.save({'mode': 'system'}, 'legacy')
    env = {'http_proxy': 'wrong', 'ALL_PROXY': 'wrong'}
    network.apply_environment(env)
    assert env['HTTP_PROXY'].endswith(':12000')
    assert env['HTTPS_PROXY'].endswith(':13000')
    assert '.corp.example' in env['NO_PROXY']
    assert 'https=http://127.0.0.1:13000' in network.browser_policy()['args'][0]
    assert 'ALL_PROXY' not in env


def test_resolution_change_is_pending_even_when_config_revision_is_same(policy, monkeypatch):
    state = network.save({'mode': 'system'}, 'legacy')
    env = {}
    network.apply_environment(env)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    browser = network.browser_policy()
    monkeypatch.setenv('DESKTRON_BROWSER_NETWORK_REVISION', browser['revision'])
    monkeypatch.setenv('DESKTRON_BROWSER_NETWORK_RESOLUTION', browser['resolution'])
    assert not network.status()['pending']
    monkeypatch.setattr(network, 'system_proxies', lambda: ({'http': 'http://127.0.0.1:12000'}, []))
    assert network.status()['pending']
    assert network.status()['config']['revision'] == state['config']['revision']


def test_stale_save_does_not_overwrite_user_change(policy):
    first = network.save(manual(), 'legacy')['config']
    network.save(manual(10808), first['revision'])
    with pytest.raises(durable_json.ConflictError):
        network.save(manual(9000), first['revision'])
    assert network.read_config()['port'] == 10808


@pytest.mark.parametrize('raw', ['{}', '{broken', '{"mode":"manual"}'])
def test_corrupt_policy_is_preserved_and_not_direct(policy, raw):
    policy.write_text(raw, encoding='utf-8')
    with pytest.raises(ValueError):
        network.apply_environment({})
    assert policy.read_text(encoding='utf-8') == raw


def test_registry_pac_is_rejected_and_not_logged(policy, monkeypatch):
    # Call original OS reader, not the generic fixture mock.
    class Key:
        def __enter__(self): return self
        def __exit__(self, *_): pass
    reg = SimpleNamespace(HKEY_CURRENT_USER=1, OpenKey=lambda *_: Key(),
                          QueryValueEx=lambda _, name: ('http://synthetic.invalid/pac', 1)
                          if name == 'AutoConfigURL' else (0, 1))
    monkeypatch.setitem(sys.modules, 'winreg', reg)
    import importlib.util
    spec = importlib.util.spec_from_file_location('fixture_network', network.__file__)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    with pytest.raises(ValueError, match='PAC') as err:
        module.system_proxies()
    assert 'synthetic.invalid' not in str(err.value)


def test_proxy_test_connects_only_tcp_and_keeps_website_model_unverified(policy):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0)); listener.listen(1)
    listener.settimeout(5)
    received = []
    def accept():
        conn, _ = listener.accept()
        with conn:
            received.append(conn.recv(1))
    thread = threading.Thread(target=accept); thread.start()
    try:
        result = network.test_connection(manual(listener.getsockname()[1]))
        thread.join(5)
        assert result['proxy_tcp'] == 'passed'
        assert result['webpage'] == result['model'] == 'not_run'
        assert received == [b''], 'no HTTP, model or credential bytes sent'
        assert not policy.exists(), 'testing a draft never saves'
    finally:
        listener.close()


def test_secrets_cannot_override_active_proxy_policy(policy, monkeypatch):
    import credstore
    monkeypatch.setattr(credstore, '_secrets_read', lambda: {
        'https_proxy': 'old', 'HTTP_PROXY': 'old', 'NO_PROXY': 'old', 'SYNTHETIC_KEY': 'value'})
    env = credstore.secrets_env({'DESKTRON_NETWORK_MODE': 'manual', 'HTTPS_PROXY': 'new'})
    assert env['HTTPS_PROXY'] == 'new' and 'https_proxy' not in env and 'HTTP_PROXY' not in env
    assert env['SYNTHETIC_KEY'] == 'value'


def test_route_local_security_and_revision_conflict(policy):
    from system import api
    class Handler:
        headers = {'Content-Type': 'application/json'}
        def _lan_local_request(self): return self.local
        def _send_json(self, value, code=200): return code, value
    h = Handler(); h.local = False
    assert api.handle(h, '/workbench/network-proxy', {}, 'GET')[0] == 403
    h.local = True; h.headers['Sec-Fetch-Site'] = 'cross-site'
    assert api.handle(h, '/workbench/network-proxy', {}, 'PUT')[0] == 403
    h.headers.pop('Sec-Fetch-Site')
    assert api.owns('/workbench/network-proxy/test', 'POST')
    raw = json.dumps({'config': manual(), 'expected_revision': 'legacy'}).encode()
    h.headers['Content-Length'] = str(len(raw)); h.rfile = io.BytesIO(raw)
    assert api.handle(h, '/workbench/network-proxy', {}, 'PUT')[0] == 200
    h.rfile = io.BytesIO(raw)
    assert api.handle(h, '/workbench/network-proxy', {}, 'PUT')[0] == 409


@pytest.mark.skipif(os.name != 'nt', reason='Windows fixed-command terminal integration')
def test_install_terminal_applies_frozen_policy_only_after_attach(policy, tmp_path, monkeypatch):
    from test_setup_command_terminal import _opener
    monkeypatch.setenv('DESKTRON_NETWORK_MODE', 'manual')
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:12000')
    opener, pty = _opener(tmp_path)
    row = opener(command='winget install --id Synthetic.Fixture -e')
    argv, env = pty.pty_open.call_args.args[:2]
    script = argv[-1]
    assert script.index('ReadLine') < script.index('DESKTRON_NETWORK_SCRIPT') < script.index('Invoke-Expression')
    assert env['DESKTRON_SETUP_COMMAND'].endswith(' --proxy "$env:HTTPS_PROXY"')
    assert Path(env['DESKTRON_NETWORK_SCRIPT']).is_file()
    assert row['command_pending'] and 'catch' in script
    pty.pty_input.assert_not_called()


@pytest.mark.parametrize('mode', ['manual', 'direct', 'system'])
def test_powershell_policy_covers_http_https_and_loopback(tmp_path, mode):
    if os.name != 'nt':
        pytest.skip('Windows PowerShell required')
    import subprocess
    import oscompat
    from engine_protocol import isolated_env
    env = isolated_env(tmp_path)
    for key in list(env):
        if key.lower() in network.PROXY_KEYS:
            env.pop(key)
    env['DESKTRON_NETWORK_MODE'] = mode
    env['HTTP_PROXY'] = 'http://127.0.0.1:12000' if mode != 'direct' else ''
    env['HTTPS_PROXY'] = 'http://127.0.0.1:13000' if mode != 'direct' else ''
    env['NO_PROXY'] = 'localhost,127.0.0.1,::1,.internal.example' if mode != 'direct' else '*'
    env['DESKTRON_NETWORK_SCRIPT'] = str(ROOT / 'desktron/lib/network-proxy.ps1')
    script = '''& $env:DESKTRON_NETWORK_SCRIPT;
$p = [System.Net.WebRequest]::DefaultWebProxy;
@{http=$p.GetProxy([uri]'http://fixture.invalid/').AbsoluteUri;
https=$p.GetProxy([uri]'https://fixture.invalid/').AbsoluteUri;
local=$p.IsBypassed([uri]'http://127.0.0.1:7765/');
suffix=$p.IsBypassed([uri]'http://sub.internal.example/')} | ConvertTo-Json -Compress'''
    result = subprocess.run(oscompat.command_shell_argv(script), env=env, capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=25)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value['local'] and value['suffix']
    assert value['http'] == ('http://fixture.invalid/' if mode == 'direct' else 'http://127.0.0.1:12000/')
    assert value['https'] == ('https://fixture.invalid/' if mode == 'direct' else 'http://127.0.0.1:13000/')


def test_powershell_web_download_uses_proxy_and_bypasses_localhost(tmp_path):
    if os.name != 'nt':
        pytest.skip('Windows PowerShell required')
    import http.server
    import subprocess
    import oscompat
    from engine_protocol import isolated_env
    seen = []
    class Fixture(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = b'SYNTHETIC_DOWNLOAD_OK'
            self.send_response(200); self.send_header('Content-Length', str(len(body)))
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *_): pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = isolated_env(tmp_path)
    for key in list(env):
        if key.lower() in network.PROXY_KEYS: env.pop(key)
    env.update(DESKTRON_NETWORK_MODE='manual', HTTP_PROXY=f'http://127.0.0.1:{server.server_port}',
               HTTPS_PROXY=f'http://127.0.0.1:{server.server_port}', NO_PROXY='localhost,127.0.0.1,::1',
               DESKTRON_NETWORK_SCRIPT=str(ROOT / 'desktron/lib/network-proxy.ps1'))
    script = ("$ErrorActionPreference='Stop'; & $env:DESKTRON_NETWORK_SCRIPT; "
              "(Invoke-WebRequest -UseBasicParsing -Uri 'http://proxy-fixture.invalid/download').Content; "
              f"(Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:{server.server_port}/local').Content")
    try:
        result = subprocess.run(oscompat.command_shell_argv(script), env=env, capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=25)
        assert result.returncode == 0, result.stderr
        assert result.stdout.count('SYNTHETIC_DOWNLOAD_OK') == 2
        assert seen == ['http://proxy-fixture.invalid/download', '/local']
    finally:
        server.shutdown(); server.server_close()


def test_managed_download_never_uses_legacy_fallback_proxy(tmp_path, monkeypatch):
    from provision import setup_runtime
    monkeypatch.setenv('DESKTRON_NETWORK_MODE', 'direct')
    monkeypatch.setenv('DESKTRON_SETUP_PROXY', 'http://synthetic.invalid:12000')
    calls = []
    class Opener:
        def open(self, *_a, **_kw): raise OSError('synthetic failure')
    def build(*args):
        calls.append(args); return Opener()
    monkeypatch.setattr(setup_runtime.urllib.request, 'build_opener', build)
    with pytest.raises(RuntimeError):
        setup_runtime._download('http://synthetic.invalid/fixture', tmp_path / 'fixture')
    assert calls == [()]


@pytest.mark.parametrize('mode', ['manual', 'system', 'direct'])
def test_pi_private_proxy_cannot_override_managed_policy(tmp_path, mode):
    from test_pi_http_proxy import node_check
    from engine_protocol import isolated_env
    root = tmp_path / 'sdk'; (root / 'dist/core').mkdir(parents=True)
    (root / 'package.json').write_text('{"type":"module"}', encoding='utf-8')
    (root / 'dist/core/http-dispatcher.js').write_text('''
export function applyHttpProxySettings(value){globalThis.applied=value??null;}
export function configureHttpDispatcher(){}
''', encoding='utf-8')
    env = isolated_env(tmp_path); env['DESKTRON_NETWORK_MODE'] = mode
    script = '''import {configurePiHttp} from %s;
await configurePiHttp(process.argv[1],{getGlobalSettings:()=>({httpProxy:'http://synthetic.invalid:1'}),getHttpIdleTimeoutMs:()=>12000});
console.log(JSON.stringify({applied:globalThis.applied}));''' % json.dumps((ROOT / 'desktron/lib/shepherd/pi_sdk.mjs').as_uri())
    assert node_check(tmp_path, script, root, env=env) == {'applied': None}


def test_windows_all_webviews_use_one_frozen_policy():
    app = (ROOT / 'desktop/windows/src/app.rs').read_text(encoding='utf-8')
    views = (ROOT / 'desktop/windows/src/views.rs').read_text(encoding='utf-8')
    native = (ROOT / 'desktop/windows/src/network.rs').read_text(encoding='utf-8')
    assert '.with_additional_browser_args(&browser_args)' in app
    assert views.count('.with_additional_browser_args(&self.browser_args)') == 2
    assert 'DESKTRON_BROWSER_NETWORK_RESOLUTION' in native
    assert '.arg("--native")' in native


def test_settings_use_existing_style_and_no_automatic_restart():
    ui = (ROOT / 'frontend/app-settings-network.js').read_text(encoding='utf-8')
    core = (ROOT / 'frontend/app-settings-core.js').read_text(encoding='utf-8')
    bundle = (ROOT / 'desktron/lib/frontend_bundle.py').read_text(encoding='utf-8')
    for token in ['sm-field', 'sm-flabel', 'sm-input', 'sm-select', 'sm-save', 'sm-link-btn', 'sm-prov-line']:
        assert token in ui
    assert 'renderNetworkSection(network)' in core and '_routeSettingsSection(network, "network")' in core
    assert 'gTools.appendChild(network)' in core and 'app-settings-network.js' in bundle
    assert '/workbench/restart' not in ui and '草稿已保留' in ui
    assert 'role' in ui and 'aria-live' in ui
