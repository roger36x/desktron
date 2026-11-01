"""Observe the provider request, not just a mocked thread/resume call."""
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

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'desktron/lib'))
import codex_appserver as app
from engine_adapters.codex import CodexAdapter
from shepherd import Service, ExecutionHarness, CodexFamilyRunner
from shepherd.codex_connector import CodexFamilyConnector
from test_codex_appserver import FakeConnection, CATALOG


class PromptProtocol(unittest.TestCase):
    def setUp(self):
        app._drop_all_sessions()
        self.addCleanup(app._drop_all_sessions)

    def test_pooled_turns_refresh_application_context_without_overriding_plan(self):
        with mock.patch.object(app,'_Connection',FakeConnection), \
             mock.patch.object(app,'catalog',return_value=CATALOG):
            for n in (1,2):
                app.run_turn('/work',{},'', 'hello',{'collaboration_mode':'plan'},
                    session_id='private-prompt-test',turn_system_text=f'grounding-{n}')
        calls=FakeConnection.last.calls
        self.assertEqual(sum(m=='thread/start' for m,_ in calls),1)
        turns=[params for method,params in calls if method=='turn/start']
        self.assertEqual([t['additionalContext']['desktron']['value'] for t in turns],
                         ['grounding-1','grounding-2'])
        for turn in turns:
            self.assertEqual(turn['collaborationMode']['mode'],'plan')
            self.assertNotIn('developer_instructions',turn['collaborationMode']['settings'])
            self.assertNotIn('developerInstructions',turn)

    def test_ordinary_turn_unchanged(self):
        with mock.patch.object(app,'_Connection',FakeConnection), \
             mock.patch.object(app,'catalog',return_value=CATALOG):
            app.run_turn('/work',{},'', 'hello',{},system_text='ordinary')
        self.assertNotIn('additionalContext',dict(FakeConnection.last.calls)['turn/start'])


class Model:
    def __init__(self):
        self.requests=[]
        owner=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
                owner.requests.append(body)
                if 'count_tokens' in self.path:
                    raw=b'{"input_tokens":10}';content='application/json'
                else:
                    item={'type':'message','id':'msg_test','role':'assistant','status':'completed',
                          'content':[{'type':'output_text','text':'fixture-ok','annotations':[]}]}
                    response={'id':'resp_test','object':'response','status':'completed','output':[item],
                              'usage':{'input_tokens':10,'output_tokens':2,'total_tokens':12}}
                    events=[{'type':'response.created','response':{**response,'status':'in_progress','output':[]}},
                            {'type':'response.output_item.added','output_index':0,'item':item},
                            {'type':'response.output_item.done','output_index':0,'item':item},
                            {'type':'response.completed','response':response}]
                    raw=''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
                    content='text/event-stream'
                self.send_response(200);self.send_header('Content-Type',content)
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}/v1'
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(3)


@unittest.skipUnless(os.environ.get('DESK_SHEPHERD_NATIVE_PROMPT')=='1','opt-in native instructions')
class NativeInstructions(unittest.TestCase):
    def test_precreated_thread_receives_rules_and_refreshed_grounding(self):
        if not shutil.which('codex'):self.skipTest('codex missing')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);work=root/'work';work.mkdir()
            model=Model();self.addCleanup(model.close)
            def argv(binary,opts=None):
                return [binary,'-c','model_provider="fixture"','-c','model="gpt-5.6"',
                    '-c','model_providers.fixture.name="Fixture"',
                    '-c',f'model_providers.fixture.base_url="{model.url}"',
                    '-c','model_providers.fixture.wire_api="responses"',
                    '-c','model_providers.fixture.requires_openai_auth=false','app-server','--stdio']
            with mock.patch.object(app,'_appserver_argv',side_effect=argv), \
                 mock.patch.object(CodexFamilyConnector,'_seed_private_config'):
                service=Service(root/'state');service.create('instructions','codex',str(work))
                for n in (1,2):
                    active=service.active('instructions');connector=service.connector(active['binding'])
                    text=CodexAdapter.INTENT_RULE+'\n'+CodexAdapter.CHANNEL_RULE+f'\nGROUNDING_REV_{n}'
                    runner=CodexFamilyRunner(connector,timeout=35,options={'model':'gpt-5.6'},system_text=text)
                    lease=active['lease'];start=len(model.requests)
                    result=ExecutionHarness(service.coordinator,connector,runner).run('instructions',
                        lease_id=lease['lease_id'],generation=lease['generation'],prompt=f'hello {n}')
                    self.assertEqual(result['terminal_state'],'completed')
                    payload=json.dumps(model.requests[start:],ensure_ascii=False)
                    self.assertTrue(CodexAdapter.INTENT_RULE[:60] in payload, 'intent rule missing')
                    self.assertTrue(CodexAdapter.CHANNEL_RULE[:60] in payload, 'channel rule missing')
                    self.assertTrue(f'GROUNDING_REV_{n}' in payload, 'dynamic grounding missing')
                    developers=[row for req in model.requests[start:] for row in req.get('input', [])
                                if isinstance(row,dict) and row.get('role')=='developer']
                    self.assertTrue(f'GROUNDING_REV_{n}' in json.dumps(developers), 'not developer context')
                    if n > 1:
                        snapshots=[row for row in developers if 'GROUNDING_REV_' in json.dumps(row)]
                        self.assertTrue(f'GROUNDING_REV_{n}' in json.dumps(snapshots[-1]))
                        self.assertTrue('以本条为准' in json.dumps(snapshots[-1],ensure_ascii=False))
                    source=connector.resolve_source(result['source'])
                    self.assertNotIn('GROUNDING_REV_',json.dumps(source['public']))


if __name__=='__main__':unittest.main()
