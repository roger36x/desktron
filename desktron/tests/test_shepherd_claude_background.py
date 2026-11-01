"""Claude task receipts are not user-turn terminals; loopback-only native regression."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron/lib'))
from shepherd.resident_process import ResidentProcess

BRIDGE = ROOT / 'desktron/lib/shepherd'


def test_task_result_contract():
    subprocess.run(['node', str(ROOT / 'desktron/tests/shepherd/claude-task-result.test.mjs')],
                   check=True, capture_output=True, text=True, timeout=15)


class Model:
    """Actual CLI executes only the synthetic Bash command returned by this server."""
    def __init__(self, command=None, background=False):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if 'count_tokens' in self.path:
                    raw = b'{"input_tokens":100}'
                    content_type = 'application/json'
                else:
                    outer.requests.append(body)
                    tool = command is not None and len(outer.requests) == 1
                    block = ({'type':'tool_use','id':'toolu_probe','name':'Bash','input':{}}
                             if tool else {'type':'text','text':''})
                    delta = ({'type':'input_json_delta','partial_json':json.dumps({
                        'command':command, 'timeout':20000,
                        **({'run_in_background':True} if background else {})})}
                             if tool else {'type':'text_delta','text':'OK'})
                    events = [
                        {'type':'message_start','message':{'id':'msg_'+uuid.uuid4().hex,
                         'type':'message','role':'assistant','content':[], 'model':body['model'],
                         'stop_reason':None,'stop_sequence':None,
                         'usage':{'input_tokens':100,'output_tokens':0}}},
                        {'type':'content_block_start','index':0,'content_block':block},
                        {'type':'content_block_delta','index':0,'delta':delta},
                        {'type':'content_block_stop','index':0},
                        {'type':'message_delta','delta':{'stop_reason':'tool_use' if tool else 'end_turn',
                         'stop_sequence':None},'usage':{'output_tokens':5}},
                        {'type':'message_stop'}]
                    raw = ''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n'
                                  for e in events).encode()
                    content_type = 'text/event-stream'
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


def orphan_entries(sid, cwd):
    base = {'sessionId':sid,'cwd':str(cwd),'version':'2.1.278','isSidechain':False,
            'userType':'external','timestamp':'2026-09-22T00:00:00.000Z'}

    def entry(role, content, **extra):
        return {**base,'type':role,'uuid':str(uuid.uuid4()),'message':{
            'role':role,'content':content, **({'id':'msg_'+uuid.uuid4().hex,'type':'message',
                'model':'fixture','stop_reason':'end_turn'} if role=='assistant' else {})}, **extra}

    return [entry('user','Old synthetic request'),
        entry('assistant',[{'type':'tool_use','id':'toolu_old','name':'Bash',
            'input':{'command':'exit 7','run_in_background':True}}]),
        entry('user',[{'type':'tool_result','tool_use_id':'toolu_old',
            'content':'Command running in background with ID: bg_old'}],
            toolUseResult={'backgroundTaskId':'bg_old','stdout':'','stderr':'','interrupted':False}),
        entry('assistant',[{'type':'text','text':'Waiting for background task.'}])]


@pytest.mark.native
@pytest.mark.parametrize('resident', [True, False])
@pytest.mark.parametrize('case', ['orphan', 'explicit', 'foreground', 'long'])
def test_background_boundary_real_sdk(tmp_path, resident, case):
    cli = os.environ.get('DESK_SHEPHERD_CLAUDE_BINARY') or shutil.which('claude')
    sdk = os.environ.get('DESK_SHEPHERD_CLAUDE_SDK_ROOT') or str(Path.home() /
        '.local/share/desktron/runtime/claude-agent-sdk/node_modules/@anthropic-ai/claude-agent-sdk')
    assert cli and Path(sdk, 'sdk.mjs').is_file(), 'Claude runtime required for native gate'
    seconds = 11 if case == 'long' else 1  # exceed native auto-background threshold
    command = None if case == 'orphan' else f'printf before; sleep {seconds}; printf failed; exit 7'
    model = Model(command, background=case == 'explicit')
    sid = str(uuid.uuid4())
    entries = orphan_entries(sid, tmp_path) if case == 'orphan' else []
    payload = {'unified':True,'bridgeProtocolVersion':4 if resident else 3,
        'entries':entries,'sessionId':sid,'sdkRoot':sdk,'claudeBinary':cli,
        'dbPath':str(tmp_path/'memory'),'cwd':str(tmp_path),
        'systemPrompt':'Synthetic protocol test. Follow only the current user request.',
        'permissionMode':'bypassPermissions',
        'bashCwdPolicy':{'codec':'claude-2.1.268-v1','mode':'reset_each_call'},
        'env':{'CLAUDE_CONFIG_DIR':str(tmp_path/'config'),'ANTHROPIC_BASE_URL':model.url,
            'ANTHROPIC_API_KEY':'local-fixture','ANTHROPIC_AUTH_TOKEN':'',
            'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1',
            # Host policy must override caller settings, even with bypass permissions.
            'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS':'0'},
        'prompt':'Current human request'}
    path = tmp_path/'bootstrap.json'
    path.write_text(json.dumps(payload))
    filename = 'claude_resident_runner.mjs' if resident else 'claude_runner.mjs'
    host = ResidentProcess(['node', str(BRIDGE/filename), str(path)],cwd=str(tmp_path),
        env=os.environ.copy(),cancel_frame={'type':'cancel'},close_frame={'type':'shutdown'})
    captured = []
    results = []
    try:
        for seq in range(2 if resident else 1):
            host.begin(timeout=15,hard_timeout=60)
            if resident:
                host.send({'type':'turn','payload':{'prompt':f'Current human request {seq}'}})
            for row in host.events():
                if row['type'] == 'prepared':
                    host.send({'type':'start'})
                elif row['type'] == 'archive':
                    captured.extend(row['entries'])
                    host.send({'type':'archive_ack','id':row['id']})
                elif row['type'] == 'event' and row['event']['type'] == 'result':
                    results.append(row['event'])
                elif row['type'] == 'result':
                    assert row['terminalState'] == 'completed', row
                    assert row['terminalUuid']
                    assert any(e.get('uuid') == row['terminalUuid'] for e in captured)
                    break
            host.finish()
        assert len(results) == (2 if resident else 1)
        assert all(r.get('origin',{}).get('kind') == 'human' for r in results)
        human = [e for e in captured if e.get('origin',{}).get('kind') == 'human']
        assert len(human) == len(results)
        if case == 'orphan':
            assert any(e.get('origin',{}).get('kind') == 'task-notification' for e in captured)
            assert len(model.requests) == len(results)  # no model call for orphan notification
        else:
            tool_results = [e.get('toolUseResult') for e in captured if e.get('toolUseResult')]
            assert tool_results
            assert not any(isinstance(r,dict) and r.get('backgroundTaskId') for r in tool_results)
            if case == 'explicit':
                assert 'run_in_background' in json.dumps(tool_results)
                assert 'InputValidationError' in json.dumps(tool_results)
            else:
                assert 'failed' in json.dumps(tool_results)  # nonzero Bash result remains visible
                assert any(p.get('is_error') for e in captured
                    for p in e.get('message',{}).get('content',[]) if isinstance(p,dict)
                    and p.get('type') == 'tool_result')
            tools = model.requests[0].get('tools', [])
            assert not any(t.get('name') == 'Monitor' for t in tools)
            bash = next(t for t in tools if t.get('name') == 'Bash')
            assert 'run_in_background' not in bash['input_schema'].get('properties',{})
    except Exception as exc:
        raise AssertionError(f'{exc}; bridge stderr: {"".join(host.errors)[-3000:]}') from exc
    finally:
        host.close()
        model.close()


@pytest.mark.native
def test_orphan_notification_commits_through_unified_archive(tmp_path, monkeypatch):
    from shepherd.service import Service
    from shepherd.harness import ExecutionHarness
    from shepherd.unified_runners import UnifiedRunner
    from shepherd import resident_process

    cli = os.environ.get('DESK_SHEPHERD_CLAUDE_BINARY') or shutil.which('claude')
    sdk = os.environ.get('DESK_SHEPHERD_CLAUDE_SDK_ROOT') or str(Path.home() /
        '.local/share/desktron/runtime/claude-agent-sdk/node_modules/@anthropic-ai/claude-agent-sdk')
    model = Model()
    service = Service(tmp_path/'state', storage='unified')
    work = tmp_path/'work'
    work.mkdir()
    service.create('notification', 'claude', work)
    active = service.active('notification')
    connector = service.connector(active['binding'])
    bid = active['binding']['binding_id']
    env = {**os.environ,'DESK_SHEPHERD_CLAUDE_BINARY':cli,
           'DESK_SHEPHERD_CLAUDE_SDK_ROOT':sdk,'ANTHROPIC_BASE_URL':model.url,
           'ANTHROPIC_API_KEY':'fixture','ANTHROPIC_AUTH_TOKEN':'',
           'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1'}
    monkeypatch.setattr(resident_process, '_SOFT_RSS_MB', 0)
    first_source = None
    hosts = []
    try:
        for seq in (1, 2, 3):
            if seq == 3:
                assert resident_process.drop(bid)  # force cold reconstruction from real archive
            runner = UnifiedRunner(connector, timeout=30, env=env,
                options={'model':'gpt-5.6','mode':'bypassPermissions','_context_window':1000000},
                system_text='Synthetic notification fixture')
            if seq == 1:
                original_payload = runner._payload

                def seed(payload):
                    payload['entries'] = orphan_entries(connector.native_session_id, work)
                    return original_payload(payload)

                monkeypatch.setattr(runner, '_payload', seed)
            prompt = f'Current human request {seq}'
            lease = active['lease']
            row = ExecutionHarness(service.coordinator, connector, runner).run('notification',
                lease_id=lease['lease_id'], generation=lease['generation'], prompt=prompt,
                input_public=[{'type':'message','role':'user',
                               'content':[{'type':'input_text','text':prompt}]}])
            assert row['terminal_state'] == 'completed'
            resolved = connector.resolve_source(row['source'])
            assert prompt in json.dumps(resolved['public'])
            source = connector.resolve_source(connector._ref('notification:1'))
            if first_source is None:
                first_source = source
                assert any(e.get('value',{}).get('origin',{}).get('kind') == 'task-notification'
                           for e in source['source']['events'])
            else:
                assert source == first_source  # no rewriting committed notification evidence
            hosts.append(next(pid for binding,pid,_,_ in resident_process.roster() if binding == bid))
            active = service.active('notification')
        assert hosts[0] == hosts[1] and hosts[1] != hosts[2]
        assert len(model.requests) == 3
        assert len(service.coordinator.state('notification')['turns']) == 3
    finally:
        resident_process.drop(bid)
        model.close()
