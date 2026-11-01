"""Real product dispatcher with loopback providers and isolated native homes."""
import json
import os
from pathlib import Path
import shutil
import time
from unittest import mock
from contextlib import ExitStack

import codex_appserver as app
from engine_adapters.shepherd import ShepherdAdapter
from shepherd.codex_connector import CodexFamilyConnector
from test_engine_adapters_gui import Recorder
from test_shepherd_image_execution import ImageModel

ROOT = Path(__file__).resolve().parents[2]


class ProductFixture:
    def __init__(self, root, *, input_tokens=None, tool_image=None):
        self.root = Path(root)
        self.work = self.root/'work'
        self.work.mkdir()
        (self.work/'prompts').mkdir()
        (self.work/'prompts/style.md').write_text('')
        self.model = ImageModel(input_tokens=input_tokens or (
            lambda body:max(100,len(json.dumps(body))//4)), tool_image=tool_image)
        handler = self.model.server.RequestHandlerClass
        original_post = handler.do_POST
        model = self.model
        self.before_request = None
        def post(request):
            if self.before_request:
                self.before_request(request.path)
            if not request.path.endswith('/responses/compact'):
                return original_post(request)
            body = json.loads(request.rfile.read(int(request.headers.get('Content-Length') or 0)))
            model.requests.append(body)
            raw = json.dumps({'id':'cmp_fixture','object':'response.compaction','created_at':1,
                'output':[{'type':'compaction','encrypted_content':'fixture-opaque-context'}]}).encode()
            request.send_response(200)
            request.send_header('Content-Type','application/json')
            request.send_header('Content-Length',str(len(raw)))
            request.end_headers()
            request.wfile.write(raw)
        handler.do_POST = post
        self.stack = ExitStack()
        self.stack.callback(self.model.close)
        agent = self.root/'pi'
        agent.mkdir()
        (agent/'models.json').write_text(json.dumps({'providers':{'fixture':{
            'baseUrl':self.model.url,'api':'anthropic-messages','apiKey':'fixture',
            'models':[{'id':'fixture','name':'Fixture','reasoning':False,'input':['text','image'],
                'contextWindow':1000000,'maxTokens':1024,
                'cost':{'input':0,'output':0,'cacheRead':0,'cacheWrite':0}}]}}}))
        self.env = {**os.environ,'PI_CODING_AGENT_DIR':str(agent),
            'DESK_SHEPHERD_CLAUDE_BINARY':os.environ.get('DESK_SHEPHERD_CLAUDE_BINARY') or shutil.which('claude'),
            'DESK_SHEPHERD_CLAUDE_SDK_ROOT':os.environ.get('DESK_SHEPHERD_CLAUDE_SDK_ROOT') or str(Path.home()/'.local/share/desktron/runtime/claude-agent-sdk/node_modules/@anthropic-ai/claude-agent-sdk'),
            'ANTHROPIC_BASE_URL':self.model.url,'ANTHROPIC_API_KEY':'fixture',
            'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1'}
        def argv(binary,opts=None):
            return [binary,'-c','model_provider="fixture"','-c','model="gpt-5.6"',
                '-c','model_providers.fixture.name="Fixture"',
                '-c',f'model_providers.fixture.base_url="{self.model.url}/v1"',
                '-c','model_providers.fixture.wire_api="responses"',
                '-c','model_providers.fixture.requires_openai_auth=false',
                *(['app-server','--stdio'] if os.path.basename(binary)=='codex'
                  else ['app-server','--listen','stdio://'])]
        self.stack.enter_context(mock.patch.object(app,'_appserver_argv',side_effect=argv))
        self.stack.enter_context(mock.patch.object(CodexFamilyConnector,'_seed_private_config'))
        self.bus = Recorder()
        self.adapter = ShepherdAdapter(str(self.root/'data'),str(self.work),self.bus)
        self.adapter._recovery.stop()
        self.stack.callback(self.adapter._recovery.stop)
        self.sid = None
        self.seq = 0

    def close(self):
        try:
            self.adapter.shutdown()
            if self.sid:
                from shepherd.resident_process import drop
                for binding_id in self.adapter.service.coordinator.state(self.sid)['bindings']:
                    # Codex-family hosts use the app-server pool; Claude/Pi
                    # hosts use the resident process pool.
                    app.drop_session('shepherd:' + binding_id)
                    drop(binding_id)
        finally:
            self.stack.close()

    def reopen(self):
        self.adapter._recovery.stop()
        self.adapter = ShepherdAdapter(str(self.root/'data'),str(self.work),self.bus)
        self.adapter._recovery.stop()
        self.stack.callback(self.adapter._recovery.stop)

    def context(self, engine, prompt):
        if self.sid is None:
            self.sid = self.adapter.create_session(opts={'active_engine':engine})['id']
        elif self.adapter.session_engine(self.sid) != engine:
            # Selection is synchronous here; the production dispatch still
            # consumes the pending selection through its normal fence.
            with mock.patch.object(self.adapter,'_schedule_engine_prepare'):
                self.adapter.switch_engine(self.sid,engine)
            self.adapter._before_turn_start(self.sid)
        self.seq += 1
        # The production dispatcher requires explicit Pi dynamic grounding.
        # This fixture bypasses session admission, so provide its synthetic carrier.
        opts = {'model':'fixture','provider':'fixture','effort':'off',
                '_grounding':'Synthetic integration fixture context'} if engine=='pi' else {
            'model':'gpt-5.6','sandbox':'read-only','mode':'bypassPermissions'}
        return {'sid':self.sid,'msg_id':f'msg_{self.seq}','seq':self.seq,
                'parts':{},'order':[],'last_tool':None,'t0':int(time.time()*1000),
                'text':prompt,'_user_text':prompt,'opts':opts,'saved':[]}

    def dispatch(self, engine, ctx):
        worker = self.adapter._worker(engine)
        # Replace model routing/settings only; keep production runner selection,
        # event boundary, translators, permissions, archive and terminal handling.
        with ExitStack() as patch:
            patch.enter_context(mock.patch.object(worker,'spawn_env',return_value=self.env))
            patch.enter_context(mock.patch.object(worker,'system_body',return_value='Synthetic integration fixture'))
            patch.enter_context(mock.patch.object(worker,'options_catalog',return_value={
                'models':[{'id':('fixture/fixture' if engine == 'pi' else ctx['opts']['model']),
                           'contextWindow':1000000}]}))
            if engine=='pi':
                command=[shutil.which('pi'),'--mode','rpc','--offline','--no-extensions',
                         '--extension',str(ROOT/'desktron/lib/engine_adapters/pi_gate.ts'),
                         '--no-approve','--provider','fixture','--model','fixture']
                patch.enter_context(mock.patch.object(worker,'build_cmd',return_value=command))
            self.adapter._live[self.sid] = ctx
            self.adapter._turn_seq[self.sid] = ctx['seq']
            try:
                return self.adapter._turn_dispatch(ctx,None,self.env,None)
            except Exception as exc:
                runner=ctx.get('_shepherd_runner')
                process=getattr(runner,'process',None)
                detail=''.join(process.errors) if process else ''
                raise AssertionError(str(exc)+'\n'+detail[-2500:]) from exc
            finally:
                self.adapter._live.pop(self.sid,None)

    def turn(self, engine, prompt):
        ctx = self.context(engine,prompt)
        error = self.dispatch(engine,ctx)
        return error,ctx

    def compact(self, engine):
        ctx = self.context(engine,'/compact')
        def run(_sid, prompt, _opts, _atts):
            assert prompt == '/compact'
            return self.dispatch(engine,ctx)
        with mock.patch.object(self.adapter,'_run_serial',side_effect=run):
            return self.adapter.compact_session(self.sid),ctx
