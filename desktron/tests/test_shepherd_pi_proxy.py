"""Shepherd Pi host must use the user's Pi httpProxy like the Pi CLI does.

2026-10-04: direct egress to api.openai.com was blocked while the Pi CLI reached it
through settings.httpProxy. The Shepherd host skipped that setup, so every
openai/gpt-6.1-sol turn ended with "Connection error.".
"""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


SHEPHERD = Path(__file__).resolve().parents[1] / 'lib' / 'shepherd'
HOST = (SHEPHERD / 'pi_memory_rpc.mjs').read_text(encoding='utf-8')
SDK = (SHEPHERD / 'pi_sdk.mjs').read_text(encoding='utf-8')


def test_proxy_dispatcher_is_installed_before_session_services():
    settings = HOST.index('SettingsManager.inMemory(localSettings.getGlobalSettings())')
    configure = HOST.index('await configurePiHttp(root, settingsManager)')
    services = HOST.index('sdk.createAgentSessionServices(')
    assert settings < configure < services
    load = SDK.index("join(root, 'dist/core/http-dispatcher.js')")
    apply = SDK.index('http.applyHttpProxySettings(')
    dispatcher = SDK.index('http.configureHttpDispatcher(settingsManager.getHttpIdleTimeoutMs())')
    assert load < apply < dispatcher
    assert 'settingsManager.getGlobalSettings().httpProxy' in SDK


def test_configured_proxy_that_cannot_be_applied_fails_loudly():
    assert "Pi httpProxy is configured but this Pi SDK cannot apply it" in SDK


@pytest.mark.skipif(not shutil.which('node'), reason='Node required for isolated host test')
@pytest.mark.parametrize('proxy,mode,expected', [
    ('http://127.0.0.1:9', 'valid', ['apply', 'dispatcher', 'services']),
    ('http://127.0.0.1:9', 'missing', []),
    ('http://127.0.0.1:9', 'exports', []),
    ('http://127.0.0.1:9', 'apply-error', ['apply']),
    ('http://127.0.0.1:9', 'dispatcher-error', ['apply', 'dispatcher']),
    ('', 'missing', ['services']),
])
def test_proxy_behavior_before_services_in_offline_host(tmp_path, proxy, mode, expected):
    # Run the exact production host with fake SDK/contract modules. No provider,
    # credential, network request or native history is available to this process.
    host = tmp_path / 'pi_memory_rpc.mjs'
    host.write_text(HOST)
    (tmp_path / 'pi_sdk.mjs').write_text(SDK)
    shutil.copyfile(SHEPHERD / 'pi_compaction_budget.mjs', tmp_path / 'pi_compaction_budget.mjs')
    (tmp_path / 'fallback_receipt.mjs').write_text('export const verifyFallback = () => {};')
    (tmp_path / 'pi_runtime_contract.mjs').write_text(
        'export class CapabilityMismatch extends Error {}\n'
        'export function requireMethods(value, names, label) { for (const n of names) '
        'if (typeof value?.[n] !== "function") throw new CapabilityMismatch(`${label}.${n}`); }\n'
        'export const verifyPiStore = () => {}; export const verifyPiSession = () => {};')
    def module(path, text):
        file = tmp_path / 'sdk/dist' / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
    module('core/session-manager.js', '''export const SessionManager = {
      inMemory: () => ({appendMessage(){}, appendCustomEntry(){}, _persist(){}})};''')
    module('cli/args.js', 'export const parseArgs = () => ({diagnostics:[],unknownFlags:{}});')
    module('config.js', 'export const getAgentDir = () => "offline-test";')
    module('core/settings-manager.js', '''export const SettingsManager = {
      create: () => ({getGlobalSettings: () => ({httpProxy: ''' + json.dumps(proxy) + '''})}),
      inMemory: g => ({getGlobalSettings: () => g, getHttpIdleTimeoutMs: () => 1234,
        getCompactionReserveTokens: () => 0, getCompactionKeepRecentTokens: () => 0})};''')
    module('core/sdk.js', '''export async function createAgentSessionServices() {
      console.log(JSON.stringify({step:'services'}));
      return {resourceLoader:{getExtensions:()=>({errors:[]})}};
    }
    export async function createAgentSessionFromServices() {return {session:{
      model:{provider:'offline',id:'test-model',contextWindow:262144},subscribe(){}}};}''')
    module('core/output-guard.js', 'export const writeRawStdout = () => {};')
    module('modes/rpc/rpc-mode.js', 'export async function runRpcMode() {}')
    if mode != 'missing':
        dispatcher = 'export const irrelevant = true;' if mode == 'exports' else '''
        export function applyHttpProxySettings(value) {
          console.log(JSON.stringify({step:'apply',value}));
          if (MODE === 'apply-error') throw new Error('test apply rejected');
        }
        export function configureHttpDispatcher(value) {
          console.log(JSON.stringify({step:'dispatcher',value}));
          if (MODE === 'dispatcher-error') throw new Error('test dispatcher rejected');
        }'''.replace('MODE', json.dumps(mode))
        module('core/http-dispatcher.js', dispatcher)
    bootstrap = tmp_path / 'bootstrap.json'
    bootstrap.write_text(json.dumps({'id':'test-session','entries':[],'messages':[]}))
    # No inherited proxy variables or Desk network policy: only Pi's own httpProxy decides.
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in {'http_proxy', 'https_proxy', 'all_proxy'} and k != 'DESKTRON_NETWORK_MODE'}
    result = subprocess.run([shutil.which('node'), str(host), str(tmp_path / 'sdk'), str(bootstrap)],
                            input='', capture_output=True, text=True, cwd=tmp_path, timeout=10, env=env)
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert [row['step'] for row in rows] == expected
    if mode == 'valid' or not proxy:
        assert result.returncode == 0 and result.stderr == ''
        if proxy:
            assert rows[0]['value'] == proxy and rows[1]['value'] == 1234
    else:
        assert result.returncode != 0
        assert 'Shepherd Pi startup failed:' in result.stderr
        assert not any(row['step'] == 'services' for row in rows)
