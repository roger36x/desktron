"""Opt-in real native runners against a loopback multimodal model fixture."""
import base64
import hashlib
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
from shepherd import Service, ExecutionHarness, PiRunner, ClaudeRunner, CodexFamilyRunner, ReadProjector
from shepherd.source_resolver import SourceResolver
from shepherd.claude_connector import ClaudeConnector
from shepherd.codex_connector import CodexFamilyConnector
from shepherd.image_content import image_block, native_content
from test_shepherd_images import fixture_png

PNG = fixture_png(1206, 2622)


class ImageModel:
    def __init__(self, input_tokens=100, tool_image=None, reject=None):
        self.observations = []
        self.tool_requests = 0
        self.requests = []
        outer = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                data = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                body = json.loads(data)
                if 'count_tokens' in self.path:
                    raw = b'{"input_tokens":100}'
                    self.send_response(200); self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(raw))); self.end_headers()
                    self.wfile.write(raw); return
                outer.requests.append(body)
                if reject and reject(body):
                    raw=json.dumps({'type':'error','error':{'type':'invalid_request_error',
                        'code':'context_length_exceeded','message':'prompt is too long: context_length_exceeded'}}).encode()
                    self.send_response(400);self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(raw)));self.end_headers()
                    self.wfile.write(raw);return
                hashes = []
                def visit(value):
                    if isinstance(value, list):
                        for item in value: visit(item)
                    elif isinstance(value, dict):
                        if value.get('type') == 'image':
                            source = value.get('source') or {}
                            if source.get('type') == 'base64':
                                hashes.append(hashlib.sha256(base64.b64decode(source['data'])).hexdigest())
                        elif value.get('type') == 'input_image':
                            url = value.get('image_url') or ''
                            if isinstance(url,str) and url.startswith('data:'):
                                hashes.append(hashlib.sha256(base64.b64decode(url.split(',',1)[1])).hexdigest())
                        else:
                            for item in value.values(): visit(item)
                visit(body.get('messages',body.get('input')))
                outer.observations.append(hashes)
                tokens = input_tokens(body) if callable(input_tokens) else input_tokens
                if 'messages' in body:
                    events = [
                        {'type':'message_start','message':{'id':'msg_fixture','type':'message',
                         'role':'assistant','content':[],'model':body.get('model'),'stop_reason':None,
                         'stop_sequence':None,'usage':{'input_tokens':tokens,'output_tokens':0}}},
                        {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
                        {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'image-fixture-ok'}},
                        {'type':'content_block_stop','index':0},
                        {'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},
                         'usage':{'output_tokens':5}}, {'type':'message_stop'}]
                else:
                    output = {'type':'message','id':'msg_fixture','role':'assistant','status':'completed',
                              'content':[{'type':'output_text','text':'image-fixture-ok','annotations':[]}]}
                    response = {'id':'resp_fixture','object':'response','status':'completed','output':[output],
                                'usage':{'input_tokens':tokens,'output_tokens':5,'total_tokens':tokens+5}}
                    events = [
                        {'type':'response.created','response':{**response,'status':'in_progress','output':[]}},
                        {'type':'response.output_item.added','output_index':0,'item':output},
                        {'type':'response.output_item.done','output_index':0,'item':output},
                        {'type':'response.completed','response':response}]
                if tool_image and 'messages' in body and outer.tool_requests == 0:
                    outer.tool_requests += 1
                    events = [
                        {'type':'message_start','message':{'id':'msg_tool_fixture','type':'message',
                         'role':'assistant','content':[],'model':body.get('model'),'stop_reason':None,
                         'stop_sequence':None,'usage':{'input_tokens':tokens,'output_tokens':0}}},
                        {'type':'content_block_start','index':0,'content_block':{'type':'tool_use',
                         'id':'toolu_image','name':'Read','input':{}}},
                        {'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta',
                         'partial_json':json.dumps({'file_path':str(tool_image)})}},
                        {'type':'content_block_stop','index':0},
                        {'type':'message_delta','delta':{'stop_reason':'tool_use','stop_sequence':None},
                         'usage':{'output_tokens':5}}, {'type':'message_stop'}]
                raw = ''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
                self.send_response(200); self.send_header('Content-Type','text/event-stream')
                self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)


@unittest.skipUnless(os.environ.get('DESK_SHEPHERD_NATIVE_IMAGES') == '1', 'opt-in native image protocol')
class NativeImages(unittest.TestCase):
    def test_four_engine_chain_reopens_images_without_upload_dependency(self):
        for name in ('pi','claude','codex'):
            if not shutil.which(name): self.skipTest(name+' missing')
        sdk = os.environ.get('DESK_SHEPHERD_CLAUDE_SDK_ROOT') or str(
            Path.home()/'.local/share/desktron/runtime/claude-agent-sdk/node_modules/@anthropic-ai/claude-agent-sdk')
        claude=os.environ.get('DESK_SHEPHERD_CLAUDE_BINARY') or shutil.which('claude')
        if not ClaudeConnector.availability(sdk_root=sdk,claude_binary=claude)['available']: self.skipTest('locked Claude SDK required')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); work = root/'work'; work.mkdir(); agent = root/'pi'; agent.mkdir()
            model = ImageModel(); self.addCleanup(model.close)
            (agent/'models.json').write_text(json.dumps({'providers':{'fixture':{
                'baseUrl':model.url, 'api':'anthropic-messages','apiKey':'fixture',
                'models':[{'id':'fixture','name':'Fixture','reasoning':False,'input':['text','image'],
                    'contextWindow':1000000,'maxTokens':1024,
                    'cost':{'input':0,'output':0,'cacheRead':0,'cacheWrite':0}}]}}}))
            def argv(binary,opts=None):
                return [binary,'-c','model_provider="fixture"','-c','model="gpt-5.6"',
                    '-c','model_providers.fixture.name="Fixture"',
                    '-c',f'model_providers.fixture.base_url="{model.url}/v1"',
                    '-c','model_providers.fixture.wire_api="responses"',
                    '-c','model_providers.fixture.requires_openai_auth=false',
                    *(['app-server','--stdio'] if os.path.basename(binary)=='codex'
                      else ['app-server','--listen','stdio://'])]
            expected_hash = hashlib.sha256(base64.b64decode(PNG)).hexdigest()
            original_status = CodexFamilyConnector._round_status
            def checked_status(markers,offset,entry_hash,items):
                status = original_status(markers,offset,entry_hash,items)
                if status == 'partial':
                    self.fail('Native image injection changed the persisted payload')
                return status
            which=shutil.which
            with mock.patch.object(shutil,'which',side_effect=lambda name:claude if name=='claude' else which(name)), \
                 mock.patch.object(app,'_appserver_argv',side_effect=argv), \
                 mock.patch.object(CodexFamilyConnector,'_seed_private_config'), \
                 mock.patch.object(CodexFamilyConnector,'_round_status',side_effect=checked_status), \
                 mock.patch.object(ClaudeConnector,'default_sdk_root',return_value=sdk):
                service = Service(root/'state'); service.create('images','pi',str(work))
                for offset,engine in enumerate(('pi','claude','codex','pi'),1):
                    with self.subTest(engine=engine,offset=offset):
                        # New Service instance exercises persisted state rather than memory caches.
                        service = Service(root/'state')
                        prior = service.coordinator.state('images')
                        imported_from = next((b['applied_offset']+1 for b in prior['bindings'].values()
                                              if b['engine']==engine),1)
                        service.switch('images',engine,str(work))
                        active=service.active('images'); lease=active['lease']; connector=service.connector(active['binding'])
                        if offset > 1:
                            sources = {(r['source']['engine'],r['source']['session_id']) for r in active['state']['rounds']}
                            readers = [service.connector(b) for b in active['state']['bindings'].values()
                                       if (b['engine'],b['native_session_id']) in sources]
                            rounds = ReadProjector(service.store,SourceResolver(readers)).project('images',imported_from)
                            revision = active['binding']['native_revision']
                            self.assertEqual(connector.append(rounds,revision)['native_revision'],revision)
                        content=[{'type':'input_text','text':'image check'},image_block(PNG,'image/png')]
                        start=len(model.observations)
                        upload=root/'upload.png'; upload.write_bytes(base64.b64decode(PNG))
                        if engine=='pi':
                            command=[shutil.which('pi'),'--mode','rpc','--offline','--no-extensions',
                                '--extension',str(ROOT/'desktron/lib/engine_adapters/pi_gate.ts'),
                                '--no-approve','--provider','fixture','--model','fixture']
                            runner=PiRunner(connector,timeout=40,command=command,
                                env={**os.environ,'PI_CODING_AGENT_DIR':str(agent)},
                                user_frame={'type':'prompt','message':'image check',
                                    'images':[{'type':'image','data':PNG,'mimeType':'image/png'}]})
                        elif engine=='claude':
                            runner=ClaudeRunner(connector,timeout=60,env={
                                'CLAUDE_CONFIG_DIR':str(root/'claude-config'),'ANTHROPIC_BASE_URL':model.url,
                                'ANTHROPIC_API_KEY':'fixture','CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1'},
                                user_frame={'type':'user','message':{'role':'user',
                                    'content':native_content(content,'claude')}})
                        else:
                            runner=CodexFamilyRunner(connector,timeout=45,
                                options={'model':'gpt-5.6','sandbox':'read-only'},image_paths=[str(upload)])
                        row=ExecutionHarness(service.coordinator,connector,runner).run('images',
                            lease_id=lease['lease_id'],generation=lease['generation'],prompt='image check')
                        self.assertEqual(row['terminal_state'],'completed')
                        seen=model.observations[start:]
                        self.assertTrue(any(len(hashes)>=offset for hashes in seen), (engine,seen))
                        if offset == 1: self.assertIn([expected_hash],seen)
                        upload.unlink()  # Native source must contain bytes, not this temporary path.
                        resolved=connector.resolve_source(row['source'])
                        images=[b for m in resolved['public'] if m.get('type')=='message'
                                for b in m.get('content',[]) if b.get('type')=='input_image']
                        self.assertEqual(len(images),1)
                        self.assertTrue(images[0]['image_url'].startswith('data:image/'))
                        state=service.active('images')['state']
                        self.assertEqual(state['committed_offset'],offset)
                        self.assertIsNone(state['inflight'])
                        self.assertNotIn(PNG,json.dumps(state))
                        receipts = list((root/'state/sources').rglob('*.json'))
                        self.assertEqual(len(receipts),offset)
                        for receipt in receipts:
                            self.assertNotIn('data:image/',receipt.read_text())


if __name__ == '__main__': unittest.main()
