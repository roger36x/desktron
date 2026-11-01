"""Installed family engines, loopback provider only; never use user credentials."""
import json
import http.server
import threading
import os
from pathlib import Path
import shutil
import subprocess
from unittest import mock

import pytest
import codex_appserver as app
from engine_adapters import ClaudeAdapter, CodexAdapter, PiAdapter
from engine_adapters.bus import EventBus
from test_shepherd_codex_instructions import Model

ROOT = Path(__file__).resolve().parents[2]
OPTS = {'model': 'gpt-5.6', '_grounding': 'FIXTURE_RULES', '_application_context': 'FIXTURE_PROJECT'}


def fixture_config(url):
    return ['-c', 'model_provider="fixture"', '-c', 'model="gpt-5.6"',
            '-c', 'model_providers.fixture.name="Fixture"',
            '-c', f'model_providers.fixture.base_url="{url}"',
            '-c', 'model_providers.fixture.wire_api="responses"',
            '-c', 'model_providers.fixture.requires_openai_auth=false']


def isolated_env(root):
    home = root / 'home'; home.mkdir()
    (home / 'codex').mkdir(); (home / 'trae').mkdir()
    return {'PATH': os.environ['PATH'], 'HOME': str(home),
            'CODEX_HOME': str(home / 'codex'), 'TRAE_HOME': str(home / 'trae'),
            'TMPDIR': str(root)}


def assert_provider_input(requests):
    assert requests
    text = json.dumps(requests, ensure_ascii=False)
    assert 'FIXTURE_RULES' in text and 'FIXTURE_PROJECT' in text
    users = [item for request in requests for item in request.get('input', [])
             if isinstance(item, dict) and item.get('role') == 'user']
    user_text = json.dumps(users, ensure_ascii=False)
    assert 'USER_RAW_TEXT' in user_text
    assert 'FIXTURE_RULES' not in user_text and 'FIXTURE_PROJECT' not in user_text


@pytest.mark.native
@pytest.mark.parametrize('cls', [CodexAdapter])
def test_installed_appserver_uses_developer_not_user_prefix(cls, tmp_path):
    if not shutil.which(cls.APPSERVER_BIN): pytest.skip('engine missing')
    model = Model()
    worker = cls(str(tmp_path / 'data'), str(ROOT), EventBus())
    def argv(binary, opts=None):
        return [binary, *fixture_config(model.url), 'app-server',
                *(['--stdio'] if cls is CodexAdapter else ['--listen', 'stdio://'])]
    env = isolated_env(tmp_path)
    try:
        with mock.patch.object(app, '_appserver_argv', side_effect=argv):
            for revision in ('FIXTURE_RULES', 'FIXTURE_RULES_UPDATED'):
                app.run_turn(str(tmp_path), env, None, 'USER_RAW_TEXT', OPTS,
                    system_text=worker._turn_system_prompt({**OPTS, '_grounding': revision}),
                    binary=cls.APPSERVER_BIN, timeout=20, hard_timeout=35)
        assert_provider_input(model.requests)
        assert 'FIXTURE_RULES_UPDATED' in json.dumps(model.requests)
    finally:
        app._drop_all_sessions(); model.close()


@pytest.mark.native
def test_installed_codex_exec_config_is_a_real_developer_carrier(tmp_path):
    if not shutil.which('codex'): pytest.skip('engine missing')
    model = Model()
    worker = CodexAdapter(str(tmp_path / 'data'), str(ROOT), EventBus())
    cmd = worker.build_cmd('USER_RAW_TEXT', '', {**OPTS, 'mode': 'danger-full-access'})
    cmd = [*cmd[:-1], *fixture_config(model.url), cmd[-1]]
    try:
        try:
            result = subprocess.run(cmd, cwd=str(tmp_path), env=isolated_env(tmp_path),
                                    text=True, capture_output=True, timeout=35)
        except subprocess.TimeoutExpired as exc:
            pytest.fail('isolated exec timed out; fixture requests=' + str(len(model.requests)) +
                        '; stderr=' + str(exc.stderr or b'')[-1000:])
        assert result.returncode == 0, result.stderr[-1000:]
        assert_provider_input(model.requests)
    finally:
        model.close()


class AnthropicFixture:
    def __init__(self):
        self.requests = []
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
                owner.requests.append(payload)
                if 'count_tokens' in self.path:
                    raw = b'{"input_tokens":10}'
                    content_type = 'application/json'
                else:
                    events = [
                        {'type':'message_start', 'message':{'id':'msg_fixture', 'type':'message',
                         'role':'assistant', 'content':[], 'model':payload.get('model'),
                         'stop_reason':None, 'stop_sequence':None,
                         'usage':{'input_tokens':10, 'output_tokens':0}}},
                        {'type':'content_block_start', 'index':0, 'content_block':{'type':'text', 'text':''}},
                        {'type':'content_block_delta', 'index':0, 'delta':{'type':'text_delta', 'text':'fixture-ok'}},
                        {'type':'content_block_stop', 'index':0},
                        {'type':'message_delta', 'delta':{'stop_reason':'end_turn', 'stop_sequence':None},
                         'usage':{'output_tokens':2}}, {'type':'message_stop'}]
                    raw = ''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n'
                                  for event in events).encode()
                    content_type = 'text/event-stream'
                self.send_response(200); self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)


@pytest.mark.native
@pytest.mark.parametrize('cls', [ClaudeAdapter, PiAdapter])
def test_installed_anthropic_transports_receive_context_only_in_system(cls, tmp_path):
    binary = 'claude' if cls is ClaudeAdapter else 'pi'
    if not shutil.which(binary): pytest.skip('engine missing')
    fixture = AnthropicFixture()
    worker = cls(str(tmp_path / 'data'), str(ROOT), EventBus())
    env = isolated_env(tmp_path)
    opts = {**OPTS, 'model':'fixture', 'mode':'dontAsk'}
    if cls is ClaudeAdapter:
        env.update(CLAUDE_CONFIG_DIR=str(tmp_path / 'claude'), ANTHROPIC_BASE_URL=fixture.url,
                   ANTHROPIC_API_KEY='synthetic-test-only', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
        cmd = worker.build_cmd('USER_RAW_TEXT', '', opts)
        request = json.dumps(worker.build_user_frame('USER_RAW_TEXT', [])) + '\n'
    else:
        agent = tmp_path / 'pi'; agent.mkdir()
        (agent / 'models.json').write_text(json.dumps({'providers':{'fixture':{
            'baseUrl':fixture.url, 'api':'anthropic-messages', 'apiKey':'synthetic-test-only',
            'models':[{'id':'fixture', 'name':'Fixture', 'reasoning':False, 'input':['text'],
                       'contextWindow':200000, 'maxTokens':1024,
                       'cost':{'input':0, 'output':0, 'cacheRead':0, 'cacheWrite':0}}]}}}))
        env['PI_CODING_AGENT_DIR'] = str(agent)
        # No extensions in the native fixture: check the installed system carrier
        # without loading tools, user skills, credentials or background providers.
        cmd = [shutil.which('pi'), '--mode', 'json', '--print', '--offline', '--no-extensions',
               '--no-context-files', '--no-skills', '--no-tools', '--provider', 'fixture', '--model', 'fixture',
               '--append-system-prompt', worker._turn_system_prompt(opts), '--', 'USER_RAW_TEXT']
        request = None
    try:
        result = subprocess.run(cmd, cwd=str(tmp_path), env=env, input=request,
                                text=True, capture_output=True, timeout=35)
        assert result.returncode == 0, result.stderr[-1000:]
        assert fixture.requests
        systems = [row.get('system') for row in fixture.requests]
        assert 'FIXTURE_RULES' in json.dumps(systems) and 'FIXTURE_PROJECT' in json.dumps(systems)
        users = [row for request in fixture.requests for row in request.get('messages', [])
                 if row.get('role') == 'user']
        user_text = json.dumps(users)
        assert 'USER_RAW_TEXT' in user_text
        assert 'FIXTURE_RULES' not in user_text and 'FIXTURE_PROJECT' not in user_text
    finally:
        fixture.close()


@pytest.mark.native
@pytest.mark.parametrize('cls', [ClaudeAdapter, CodexAdapter, PiAdapter])
def test_unified_native_refresh_preserves_user_and_source_boundaries(cls, tmp_path):
    from contextlib import ExitStack
    from shepherd import Service, ExecutionHarness
    from shepherd.codex_connector import CodexFamilyConnector
    from shepherd.unified_runners import UnifiedRunner
    from shepherd.resident_process import drop
    binary = 'pi' if cls is PiAdapter else 'claude' if cls is ClaudeAdapter else cls.APPSERVER_BIN
    if not shutil.which(binary): pytest.skip('engine missing')
    anthropic = cls in (ClaudeAdapter, PiAdapter)
    fixture = AnthropicFixture() if anthropic else Model()
    env = isolated_env(tmp_path)
    opts = {**OPTS, 'model':'fixture' if anthropic else 'gpt-5.6', 'mode':'dontAsk',
            '_context_carrier':'system'}
    command = None
    worker = cls(str(tmp_path / 'data'), str(ROOT), EventBus())
    if cls is ClaudeAdapter:
        env.update(CLAUDE_CONFIG_DIR=str(tmp_path / 'claude'), ANTHROPIC_BASE_URL=fixture.url,
                   ANTHROPIC_API_KEY='synthetic-test-only', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
                   DESK_SHEPHERD_CLAUDE_BINARY=shutil.which('claude'),
                   DESK_SHEPHERD_CLAUDE_SDK_ROOT=os.environ.get('DESK_SHEPHERD_CLAUDE_SDK_ROOT') or
                   str(Path.home() / '.local/share/desktron/runtime/claude-agent-sdk/node_modules/@anthropic-ai/claude-agent-sdk'))
    elif cls is PiAdapter:
        agent = tmp_path / 'pi'; agent.mkdir()
        (agent / 'models.json').write_text(json.dumps({'providers':{'fixture':{
            'baseUrl':fixture.url, 'api':'anthropic-messages', 'apiKey':'synthetic-test-only',
            'models':[{'id':'fixture', 'name':'Fixture', 'reasoning':False, 'input':['text'],
                       'contextWindow':200000, 'maxTokens':1024,
                       'cost':{'input':0, 'output':0, 'cacheRead':0, 'cacheWrite':0}}]}}}))
        env['PI_CODING_AGENT_DIR'] = str(agent)
        command = [shutil.which('pi'), '--mode','rpc','--offline','--no-extensions',
                   '--provider','fixture','--model','fixture','--no-context-files','--no-skills']
    binding_id = ''
    def argv(binary, opts=None):
        return [binary, *fixture_config(fixture.url), 'app-server',
                *(['--stdio'] if cls is CodexAdapter else ['--listen','stdio://'])]
    try:
        with ExitStack() as stack:
            if not anthropic:
                stack.enter_context(mock.patch.object(app, '_appserver_argv', side_effect=argv))
                stack.enter_context(mock.patch.object(CodexFamilyConnector, '_seed_private_config'))
            service = Service(tmp_path / 'state', storage='unified')
            service.create('context-fixture', cls.name, str(tmp_path))
            for number, rules in enumerate(('FIXTURE_RULES', 'FIXTURE_RULES_UPDATED'), 1):
                active = service.active('context-fixture'); lease = active['lease']
                connector = service.connector(active['binding'])
                binding_id = active['binding']['binding_id']
                options = {**opts, '_grounding':rules}
                runner = UnifiedRunner(connector, options=options, env=env, timeout=20,
                    system_text=worker._turn_system_prompt(options), command=command)
                start = len(fixture.requests)
                result = ExecutionHarness(service.coordinator, connector, runner).run('context-fixture',
                    lease_id=lease['lease_id'], generation=lease['generation'], prompt='USER_RAW_TEXT')
                assert result['terminal_state'] == 'completed'
                requests = fixture.requests[start:]
                assert requests and rules in json.dumps(requests)
                public = json.dumps(connector.resolve_source(result['source'])['public'])
                assert 'FIXTURE_RULES' not in public and 'FIXTURE_PROJECT' not in public
                users = [row for request in requests for row in
                         request.get('messages' if anthropic else 'input', [])
                         if isinstance(row, dict) and row.get('role') == 'user']
                assert 'USER_RAW_TEXT' in json.dumps(users)
                assert 'FIXTURE_RULES' not in json.dumps(users)
    finally:
        if binding_id: drop(binding_id)
        app._drop_all_sessions(); fixture.close()
