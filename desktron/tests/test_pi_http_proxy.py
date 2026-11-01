"""Pi headless HTTP setup: synthetic settings and loopback traffic only."""
import http.server
import json
from pathlib import Path
import select
import shutil
import socket
import subprocess
import threading

import pytest

from engine_protocol import isolated_env
from shepherd.pi_connector import pi_sdk_root
from shepherd.runner_process import NativeProcess

LIB = Path(__file__).resolve().parents[1] / 'lib'


def node_check(tmp_path, script, *args, env=None):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    result = subprocess.run([node, '--input-type=module', '-e', script, *map(str, args)],
                            env=env or isolated_env(tmp_path), cwd=tmp_path, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, encoding='utf-8', timeout=15, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize('timeout', [0, 12000])
def test_native_http_setup_applies_settings_before_dispatcher_without_logging(tmp_path, timeout):
    root = tmp_path / 'sdk'
    (root / 'dist/core').mkdir(parents=True)
    (root / 'package.json').write_text('{"type":"module"}', encoding='utf-8')
    (root / 'dist/core/http-dispatcher.js').write_text('''
export function applyHttpProxySettings(proxy) {globalThis.steps.push(['proxy',proxy]);}
export function configureHttpDispatcher(timeout) {globalThis.steps.push(['dispatcher',timeout]);}
''', encoding='utf-8')
    script = '''
import {configurePiHttp} from %s;
globalThis.steps=[];
await configurePiHttp(process.argv[1]);
await configurePiHttp(process.argv[1],{
 getGlobalSettings:()=>({httpProxy:'http://127.0.0.1:1'}),
 getHttpIdleTimeoutMs:()=>Number(process.argv[2])});
console.log(JSON.stringify(globalThis.steps));
''' % json.dumps((LIB / 'shepherd/pi_sdk.mjs').as_uri())
    assert node_check(tmp_path, script, root, timeout) == [
        ['dispatcher', None], ['proxy', 'http://127.0.0.1:1'], ['dispatcher', timeout]]


@pytest.mark.parametrize('missing', ['module', 'method', 'settings'])
def test_missing_http_capability_is_not_silently_treated_as_direct_connection(tmp_path, missing):
    root = tmp_path / 'sdk'
    (root / 'dist/core').mkdir(parents=True)
    (root / 'package.json').write_text('{"type":"module"}', encoding='utf-8')
    if missing != 'module':
        exports = 'export const applyHttpProxySettings=()=>{};'
        if missing != 'method':
            exports += 'export const configureHttpDispatcher=()=>{};'
        (root / 'dist/core/http-dispatcher.js').write_text(exports, encoding='utf-8')
    script = '''
import {configurePiHttp} from %s;
try {
 await configurePiHttp(process.argv[1],{getGlobalSettings:()=>({httpProxy:'http://127.0.0.1:1'})});
 console.log(JSON.stringify({accepted:true}));
} catch(error) {console.log(JSON.stringify({missing:error.message}));}
''' % json.dumps((LIB / 'shepherd/pi_sdk.mjs').as_uri())
    result = node_check(tmp_path, script, root)
    if missing == 'settings':
        assert result == {'missing': 'SettingsManager.getHttpIdleTimeoutMs'}
    else:
        assert 'HTTP.configureHttpDispatcher' in result['missing']
        assert 'cannot apply' in result['missing']


@pytest.mark.parametrize('policy', ['', 'manual', 'system', 'environment'])
def test_sdk_without_dispatcher_is_accepted_only_when_no_proxy_is_required(tmp_path, policy):
    root = tmp_path / 'sdk'
    (root / 'dist/core').mkdir(parents=True)
    (root / 'package.json').write_text('{"type":"module"}', encoding='utf-8')
    env = isolated_env(tmp_path)
    if policy in ('manual', 'system'):
        env['DESKTRON_NETWORK_MODE'] = policy
    elif policy == 'environment':
        env['HTTPS_PROXY'] = 'http://127.0.0.1:1'
    script = '''
import {configurePiHttp} from %s;
try {
 const applied = await configurePiHttp(process.argv[1],{getGlobalSettings:()=>({}),getHttpIdleTimeoutMs:()=>0});
 console.log(JSON.stringify({accepted:applied}));
} catch(error) {console.log(JSON.stringify({rejected:true}));}
''' % json.dumps((LIB / 'shepherd/pi_sdk.mjs').as_uri())
    expected = {'accepted': False} if not policy else {'rejected': True}
    assert node_check(tmp_path, script, root, env=env) == expected


@pytest.mark.parametrize('route', ['environment', 'settings', 'environment-precedence', 'bypass'])
def test_real_headless_bridge_proxy_route_before_ready_without_model_request(tmp_path, route):
    sdk = pi_sdk_root()
    node = shutil.which('node')
    if not sdk or not node:
        pytest.skip('Installed Pi SDK and Node required')
    counts = {'origin': 0, 'proxy': 0}

    class Origin(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == '/fixture'
            counts['origin'] += 1
            body = b'loopback-only'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    origin = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Origin)
    origin_port = origin.server_address[1]

    class Proxy(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_CONNECT(self):
            # No forwarding to external hosts, even if the test regresses.
            assert self.path == f'127.0.0.1:{origin_port}'
            counts['proxy'] += 1
            with socket.create_connection(('127.0.0.1', origin_port), timeout=3) as upstream:
                self.send_response(200, 'Connection Established')
                self.end_headers()
                self.wfile.flush()
                peers = (self.connection, upstream)
                while True:
                    ready, _, _ = select.select(peers, [], [], 5)
                    if not ready:
                        return
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        target = upstream if source is self.connection else self.connection
                        target.sendall(data)

        def log_message(self, *args):
            pass

    proxy = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
    servers = (origin, proxy)
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    process = None
    try:
        proxy_url = f'http://127.0.0.1:{proxy.server_address[1]}'
        env = isolated_env(tmp_path)
        env['NO_PROXY'] = '127.0.0.1,localhost' if route == 'bypass' else ''
        if route != 'settings':
            env['HTTP_PROXY'] = env['HTTPS_PROXY'] = proxy_url
        settings = {'httpIdleTimeoutMs': 5000}
        if route in ('settings', 'environment-precedence'):
            # Pi's native semantics preserve explicitly inherited proxy env.
            settings['httpProxy'] = proxy_url if route == 'settings' else 'http://127.0.0.1:1'
        (tmp_path / 'settings.json').write_text(json.dumps(settings), encoding='utf-8')
        extension = tmp_path / 'probe.mjs'
        extension.write_text('export default async function(){'
                             f'const r=await fetch("http://127.0.0.1:{origin_port}/fixture");'
                             'if(await r.text()!=="loopback-only") throw new Error("fixture");}',
                             encoding='utf-8')
        sid = 'b3ff4ce0-9295-47b4-836d-6b98f8a8a01f'
        bootstrap = tmp_path / 'bootstrap.json'
        bootstrap.write_text(json.dumps({'id': sid, 'messages': [], 'entries': None}), encoding='utf-8')
        process = NativeProcess([node, str(LIB / 'shepherd/pi_memory_rpc.mjs'), sdk,
                                 str(bootstrap), '--no-tools', '--extension', str(extension)],
                                cwd=str(tmp_path), env=env, timeout=10, hard_timeout=20,
                                cancel_frame={'type': 'abort'})
        events = process.events()
        ready = next(events)
        from shepherd.phase_timing import HOST_STAGES
        assert set(ready.pop('startup_timing')) == HOST_STAGES
        assert ready == {'type': 'protocol_ready', 'contract': 'desk-pi-execute-v1',
                         'sessionId': sid}
        assert counts == {'origin': 1, 'proxy': 0 if route == 'bypass' else 1}
        process.send({'id': 'offline-check', 'type': 'get_state'})
        response = next(row for row in events if row.get('id') == 'offline-check')
        assert response['success'] and response['data']['messageCount'] == 0
        process.end_input()
        assert not any(row.get('type') == 'agent_start' for row in events)
        assert process.proc.returncode == 0
    finally:
        if process:
            process.close()
        for server in servers:
            server.shutdown()
            server.server_close()
