"""Opt-in real app-server tools with a synthetic Responses endpoint."""
import http.server
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'desktron/lib'))
import codex_appserver as app
from engine_adapters.bus import EventBus
from engine_adapters.shepherd import ShepherdAdapter
from shepherd import Service, ExecutionHarness, CodexFamilyRunner
from shepherd.codex_connector import CodexFamilyConnector


@unittest.skipUnless(os.environ.get('DESK_SHEPHERD_NATIVE_TOOLS') == '1', 'opt-in native tool protocol')
class NativeTools(unittest.TestCase):
    def run_engine(self, engine):
        if not shutil.which(engine): self.skipTest(engine + ' missing')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); work = root/'work'; work.mkdir()
            fixture = {'mode':'approve', 'sent':set(), 'tools':[], 'requests':[]}
            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *_args): pass
                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
                    rid = 'resp_fixture'; mode = fixture['mode']
                    tools = body.get('tools') or []
                    def flatten(rows):
                        return [item for row in rows for item in
                                ([row] + flatten(row.get('tools') or []))]
                    tools = flatten(tools); fixture['tools'].extend(tools)
                    names = [tool.get('name') or (tool.get('function') or {}).get('name') for tool in tools]
                    fixture['requests'].append((self.path, list(body), names))
                    name = next((n for n in ('exec_command','shell_command','shell','Bash') if n in names), None)
                    if engine == 'codex' and name is None: name = 'exec_command'
                    is_turn = 'fixture-' + mode in json.dumps(body.get('input'))
                    if mode not in fixture['sent'] and name and is_turn:
                        fixture['sent'].add(mode)
                        command = 'printf approved > ' + str(root/mode)
                        args = {'sandbox_permissions':'require_escalated', 'justification':'Synthetic local approval test'}
                        if name == 'exec_command': args['cmd'] = command
                        elif name == 'shell': args['command'] = ['bash','-lc',command]
                        else: args['command'] = command
                        if name == 'Bash': args['dangerouslyDisableSandbox'] = True
                        output = {'type':'function_call','id':'fc_fixture_'+mode, 'call_id':'call_'+mode,
                                  'name':name, 'arguments':json.dumps(args), 'status':'completed'}
                    else:
                        output = {'type':'message','id':'msg_fixture', 'role':'assistant', 'status':'completed',
                                  'content':[{'type':'output_text','text':'done','annotations':[]}]}
                    response = {'id':rid,'object':'response','status':'completed','output':[output],
                                'usage':{'input_tokens':10,'output_tokens':10,'total_tokens':20}}
                    events = [{'type':'response.created','response':{**response,'status':'in_progress','output':[]}},
                        {'type':'response.output_item.added','output_index':0,'item':output},
                        {'type':'response.output_item.done','output_index':0,'item':output},
                        {'type':'response.completed','response':response}]
                    raw = ''.join('event: '+ev['type']+'\ndata: '+json.dumps(ev)+'\n\n' for ev in events).encode()
                    self.send_response(200); self.send_header('Content-Type','text/event-stream')
                    self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
            server = http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                def argv(binary, opts=None):
                    return [binary,'-c','model_provider="fixture"','-c','model="gpt-5.6"',
                        '-c','model_providers.fixture.name="Fixture"',
                        '-c',f'model_providers.fixture.base_url="http://127.0.0.1:{server.server_port}/v1"',
                        '-c','model_providers.fixture.wire_api="responses"',
                        '-c','model_providers.fixture.requires_openai_auth=false',
                        *(['app-server','--stdio'] if os.path.basename(binary)=='codex'
                          else ['app-server','--listen','stdio://'])]
                with mock.patch.object(app,'_appserver_argv',side_effect=argv), \
                     mock.patch.object(CodexFamilyConnector,'_seed_private_config'):
                    service = Service(root/'state'); service.create('conversation',engine,str(work))
                    facade = ShepherdAdapter(str(root/'data'),str(work),EventBus())
                    worker = facade._worker(engine)
                    asks = []
                    def publish(event):
                        if event.get('type') == 'permission.asked':
                            asks.append(event['properties'])
                            if fixture['mode'] == 'cancel': runner.cancel()
                            facade.respond_permission(event['properties']['id'], fixture['mode']=='approve')
                    facade.bus.publish = publish
                    for mode in ('approve','deny','cancel'):
                        fixture['mode'] = mode; asks.clear()
                        events = []
                        active = service.active('conversation'); lease = active['lease']
                        connector = service.connector(active['binding'])
                        runner = CodexFamilyRunner(connector, timeout=30,
                            options={'model':'gpt-5.6','sandbox':'read-only'},
                            on_event=lambda method,params:events.append((method,params)),
                            on_approval=lambda msg,conn:worker._approval_ask('public',msg,conn))
                        result = ExecutionHarness(service.coordinator,connector,runner).run('conversation',
                            lease_id=lease['lease_id'],generation=lease['generation'],prompt='fixture-'+mode)
                        self.assertTrue(asks, 'No tool approval; offered tools: ' +
                            str(fixture['requests']) + ' events=' + json.dumps([
                                (method,params) for method,params in events if
                                method in ('item/completed','error')])[-3000:])
                        self.assertTrue(all(ask['sessionID']=='public' for ask in asks))
                        self.assertEqual((root/mode).exists(), mode=='approve')
                        self.assertEqual(result['terminal_state'],
                            'completed' if mode == 'approve' else 'interrupted_confirmed')
                        self.assertEqual(facade._pending_ask('public'), [])
            finally:
                server.shutdown(); server.server_close(); thread.join(2)

    def test_codex(self): self.run_engine('codex')


if __name__ == '__main__': unittest.main()
