"""One real Pi prompt joins background children before settlement (loopback only)."""
import http.server
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron/lib'))
import pi_children
import pi_subagents
from shepherd.pi_connector import pi_sdk_root
from test_pi_subagents_native import NATIVE_SUBAGENTS_ROOT


class JoinModel:
    def __init__(self, case):
        self.gate = threading.Event()
        self.waiting_parent = threading.Event()
        self.parent_requests = 0
        self.child_requests = 0
        self.case = case
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if 'count_tokens' in self.path:
                    self.send_response(200); self.end_headers(); self.wfile.write(b'{"input_tokens":100}'); return
                parent = any(tool['name'] == 'subagent' for tool in body.get('tools', []))
                history = json.dumps(body.get('messages', []), ensure_ascii=False)
                tool, args, text = None, None, 'child-evidence-marker'
                if parent:
                    outer.parent_requests += 1
                    if outer.parent_requests == 1:
                        tool, args = 'subagent', {'mode': 'async', 'agent': 'desk-scout', 'task': 'Read fixture.txt and return child-evidence-marker.'}
                        if outer.case == 'parallel-failure':
                            args = {'mode': 'async', 'tasks': [
                                {'agent': agent, 'task': 'fixture-fail-one' if index == 1 else 'Read fixture.txt and return child-evidence-marker.'}
                                for index, agent in enumerate(['desk-scout', 'desk-reviewer', 'desk-oracle'])]}
                    elif outer.parent_requests == 2:
                        tool, args = 'read', {'path': 'fixture.txt'}
                    elif outer.parent_requests == 3:
                        outer.waiting_parent.set()
                        if outer.case == 'wait':
                            rid = re.search(r'pi_[a-f0-9]{32}', history).group()
                            tool, args = 'subagent', {'action': 'wait', 'run_id': rid}
                        else:
                            text = 'stage-only-awaiting-review'
                    else:
                        assert 'child-evidence-marker' in history or 'fixture-child-failure' in history
                        text = 'joined-final-integrated'
                else:
                    outer.child_requests += 1
                    outer.gate.wait(60)
                    if outer.case == 'failure' or (outer.case == 'parallel-failure' and 'fixture-fail-one' in history):
                        self.send_response(400); self.end_headers()
                        self.wfile.write(b'{"type":"error","error":{"type":"invalid_request_error","message":"fixture-child-failure"}}'); return
                    last = body['messages'][-1]['content']
                    returned = isinstance(last, list) and any(block.get('type') == 'tool_result' for block in last)
                    if not returned: tool, args = 'read', {'path': 'fixture.txt'}
                block = {'type': 'tool_use', 'id': 'call_' + str(time.time_ns()), 'name': tool, 'input': {}} if tool else {'type': 'text', 'text': ''}
                delta = {'type': 'input_json_delta', 'partial_json': json.dumps(args)} if tool else {'type': 'text_delta', 'text': text}
                events = [
                    {'type': 'message_start', 'message': {'id': 'fixture', 'type': 'message', 'role': 'assistant', 'model': body['model'], 'content': [], 'stop_reason': None, 'usage': {'input_tokens': 100, 'output_tokens': 0}}},
                    {'type': 'content_block_start', 'index': 0, 'content_block': block},
                    {'type': 'content_block_delta', 'index': 0, 'delta': delta},
                    {'type': 'content_block_stop', 'index': 0},
                    {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use' if tool else 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 5}},
                    {'type': 'message_stop'}]
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
                try:
                    for event in events:
                        self.wfile.write(('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode()); self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError): pass
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.gate.set(); self.server.shutdown(); self.server.server_close(); self.thread.join(3)


@pytest.mark.native
@pytest.mark.parametrize('case', ['auto', 'wait', 'failure', 'parallel-failure', 'cancel', 'file-backed'])
def test_parent_joins_before_settlement_without_notification(tmp_path, monkeypatch, case):
    sdk = pi_sdk_root()
    if not sdk or not pi_subagents.available(NATIVE_SUBAGENTS_ROOT):
        pytest.skip('prepared fixed Pi SDK and pi-subagents required')
    monkeypatch.setattr(pi_subagents, 'home', lambda: str(NATIVE_SUBAGENTS_ROOT))
    monkeypatch.setitem(sys.modules, 'workbench', SimpleNamespace(_wb_state_read=lambda: {'engine_opts': {}}))
    model = JoinModel(case)
    adapter = SimpleNamespace(dir=str(tmp_path), index={'test_parent': {}}, binary='pi')
    adapter.session_options = lambda *_: {}
    delivered = []
    adapter.prompt_async = lambda *a, **k: delivered.append(k) or {'status': 'finished'}
    adapter.request_status = lambda *_: {'status': 'finished'}
    env = pi_children.environment(adapter)
    bridge = pi_children._brokers[os.path.realpath(adapter.dir)]
    (tmp_path / 'fixture.txt').write_text('fixture-independent-work')
    provider = tmp_path / 'provider.ts'
    provider.write_text('''export default function(pi){pi.registerProvider('desk-trae',{
      baseUrl:%s,apiKey:'fixture',api:'anthropic-messages',models:[{id:'fixture',name:'Fixture',
      reasoning:false,input:['text'],contextWindow:200000,maxTokens:1024,
      cost:{input:0,output:0,cacheRead:0,cacheWrite:0}}]});}''' % json.dumps(model.url))
    run_env = {**os.environ, **env, 'PI_SDK': sdk, 'PROVIDER': str(provider),
        'WRAPPER': str(ROOT / 'desktron/lib/engine_adapters/pi_children.ts'),
        'DESKTRON_PI_SUBAGENTS_PROVIDER': str(provider), 'DESKTRON_SESSION_ID': 'test_parent',
        'PI_CODING_AGENT_DIR': str(tmp_path / 'agent'), 'CASE': case}
    proc = subprocess.Popen(['node', str(ROOT / 'desktron/tests/pi-subagents/join.mjs')],
        cwd=tmp_path, env=run_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert model.waiting_parent.wait(45), 'parent did not reach the dependency boundary'
        assert proc.poll() is None, 'parent settled before dependency completion'
        assert not delivered
        launches = list((tmp_path / 'subagents/test_parent/_calls').glob('*/launch.json'))
        assert len(launches) == 1
        launch = json.loads(launches[0].read_text())
        assert launch['deliveryPolicy'] == 'join'
        assert launch['parentInvocationId']
        if case == 'cancel':
            bridge.cancel('test_parent', suppress_pending=True)
            (tmp_path / 'cancel-parent').write_text('cancel')
        else:
            model.gate.set()
        stdout, stderr = proc.communicate(timeout=80)
        assert proc.returncode == 0, stderr + stdout
        result = json.loads(stdout.strip().splitlines()[-1])
        assert result['settled'] == 1
        if case == 'cancel':
            assert result['joined'] == result['waits'] == 0
            assert 'joined-final' not in result['final']
        else:
            assert result['final'] == 'joined-final-integrated'
            assert result['waits'] == (1 if case == 'wait' else 0)
            assert result['joined'] == (0 if case == 'wait' else 1)
            ledger = json.loads(launches[0].with_name('join.json').read_text())
            assert ledger['state'] == ('consumed' if case == 'file-backed' else 'observed')
            assert ledger['archive_confirmed'] == (case == 'file-backed')
            if case in ('failure', 'parallel-failure'): assert 'fixture-child-failure' in str(result['joinedText'])
            if case == 'parallel-failure':
                final = json.loads(launches[0].read_text())
                assert sorted(child['outcome'] for child in final['children']) == ['completed', 'completed', 'failed']
                assert str(result['joinedText']).count('child-evidence-marker') >= 2
        bridge.refresh(); bridge.refresh()
        assert not delivered
        # A backend recovery must not convert a joined result into a new prompt.
        recovered = pi_children.PiChildren(adapter)
        try:
            recovered.refresh()
            assert not delivered
        finally:
            recovered.close()
    finally:
        model.gate.set()
        if proc.poll() is None:
            (tmp_path / 'cancel-parent').write_text('cancel')
            bridge.cancel('test_parent', suppress_pending=True)
            proc.communicate(timeout=20)
        bridge.close(); model.close()
