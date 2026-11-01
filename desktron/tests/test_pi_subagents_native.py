"""Upstream extension with real Pi SDK, native child processes and loopback models."""
import http.server
import threading
import time
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron/lib'))
import pi_extensions
import pi_subagents
from shepherd.pi_connector import pi_sdk_root
NATIVE_SUBAGENTS_ROOT = Path(os.environ.get('DESK_PI_SUBAGENTS_RUNTIME_ROOT') or
    f'~/.local/share/desktron/runtime/pi-subagents/{pi_subagents.VERSION}-desk-v2').expanduser()


@pytest.mark.native
@pytest.mark.parametrize('case', ['undeclared', 'declared-missing', 'declared-installed'])
def test_async_chord_requirement_follows_host_manifest(tmp_path, case):
    """Pi 1.2 dropped chord; older hosts declaring it must still fail if it is missing."""
    import shutil
    node = shutil.which('node')
    resolver = Path(pi_subagents.package_root(NATIVE_SUBAGENTS_ROOT)) / 'src/runs/background/runner-aliases.js'
    if not node or not resolver.is_file():
        pytest.skip('prepared fixed pi-subagents and Node required')
    script = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const {HOST_PEER_ALIASES, resolveHostPeerAliases} = await import(pathToFileURL(process.argv[1]).href);
const root = process.argv[2], mode = process.argv[3];
const host = '@earendil-works/pi-coding-agent', chord = '@earendil-works/chord';
function pkg(name, exports, dependencies={}) {
  const dir = name===host ? root : path.join(root, 'node_modules', name);
  fs.mkdirSync(dir, {recursive:true});
  fs.writeFileSync(path.join(dir, 'package.json'), JSON.stringify({name, version:'1.2.0', exports, dependencies}));
  for (const target of Object.values(exports)) fs.writeFileSync(path.join(dir, target), 'export {};');
}
const peers = new Map();
for (const {pkg:name, subpath} of HOST_PEER_ALIASES) {
  const exports = peers.get(name) || {};
  exports[subpath] = './entry-' + Object.keys(exports).length + '.js'; peers.set(name, exports);
}
for (const [name, exports] of peers) pkg(name, exports,
  name===host && mode!=='undeclared' ? {[chord]:'1.1.0'} : {});
if (mode==='declared-installed') pkg(chord, {'.':'./index.js','./context':'./context.js'});
const result = resolveHostPeerAliases(root);
assert.deepEqual(result.missing, mode==='declared-missing' ? [chord, chord+'/context'] : []);
for (const {specifier, optional} of HOST_PEER_ALIASES) if (!optional) assert(result.aliases[specifier], specifier);
'''
    result = subprocess.run([node, '--input-type=module', '-e', script, str(resolver),
        str(tmp_path / 'host'), case], capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr


class SubagentModel:
    """Real parent tool selection, child read, then return; no external requests."""
    def __init__(self):
        self.requests = []
        self.child_gate = threading.Event()
        self.parent_args = None
        self.child_barrier = None
        self.overlap = set()
        outer = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if 'count_tokens' in self.path:
                    self.send_response(200); self.end_headers()
                    self.wfile.write(b'{"input_tokens":100}'); return
                outer.requests.append(body)
                parent = any(t['name'] == 'subagent' for t in body.get('tools', []))
                if not parent:
                    outer.child_gate.wait(15)
                last = body['messages'][-1]['content']
                text = json.dumps(last, ensure_ascii=False)
                returned = any(b.get('type') == 'tool_result' for b in last) if isinstance(last,list) else False
                if not parent and not returned and outer.child_barrier:
                    outer.overlap.add(threading.get_ident())
                    waiter = outer.child_barrier
                    if callable(waiter):
                        waiter()
                    else:
                        # Native Pi child startup can be cold and serialized by the SDK on busy
                        # developer machines. The assertion below only needs to prove overlap,
                        # not enforce a 12s wall-clock startup budget.
                        waiter.wait(45)
                notification = parent and 'Pi 子代理结果' in text
                returned = returned or notification
                failure = not parent and 'fixture-failure' in text
                cancel = not parent and 'fixture-cancel' in text
                first_response_timeout = not parent and 'fixture-first-response-timeout' in text
                runtime_timeout = not parent and 'fixture-runtime-timeout' in text
                if failure:
                    self.send_response(400); self.end_headers()
                    self.wfile.write(b'{"type":"error","error":{"type":"invalid_request_error","message":"fixture-rejected"}}'); return
                tool = 'subagent' if parent else 'read'
                args = ({'agent':'desk-scout','task':'Read fixture.txt and report fixture-answer.',
                         'context':'fork' if 'context=fork' in text else 'fresh'} if parent else {'path':'fixture.txt'})
                if parent and outer.parent_args is not None: args = outer.parent_args
                block = ({'type':'text','text':''} if returned or cancel else
                         {'type':'tool_use','id':'tool_'+str(len(outer.requests)), 'name':tool,'input':{}})
                delta = ({'type':'text_delta','text':'fixture-parent-consumed' if notification else 'fixture-answer'} if returned or cancel else
                         {'type':'input_json_delta','partial_json':json.dumps(args)})
                events = [
                    {'type':'message_start','message':{'id':'fixture','type':'message','role':'assistant',
                      'model':body['model'],'content':[],'stop_reason':None,'usage':{'input_tokens':100,'output_tokens':0}}},
                    {'type':'content_block_start','index':0,'content_block':block},
                    {'type':'content_block_delta','index':0,'delta':delta},
                    {'type':'content_block_stop','index':0},
                    {'type':'message_delta','delta':{'stop_reason':'end_turn' if returned or cancel else 'tool_use',
                      'stop_sequence':None},'usage':{'output_tokens':5}}, {'type':'message_stop'}]
                self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
                try:
                    for i, event in enumerate(events):
                        self.wfile.write(('event: '+event['type']+'\ndata: '+json.dumps(event)+'\n\n').encode())
                        self.wfile.flush()
                        if first_response_timeout and i == 0: time.sleep(2)
                        if runtime_timeout and i == 0: time.sleep(12)
                        if cancel and i == 2: time.sleep(2)
                except (BrokenPipeError, ConnectionResetError): pass
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1',0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)



@pytest.mark.native
@pytest.mark.parametrize('case', ['sync','sync-fork','async','async-fork','sync-parallel','async-parallel',
                                  'partial-failure','async-partial-failure','cancel-async','cancel-tool','cancel-sync','sequential','duplicate','model-sync','model-async','model-failure','registry-race',
                                  'sync-timeout','async-timeout','sync-parallel-timeout','async-parallel-timeout',
                                  'sync-idle','async-idle'])
def test_upstream_extension_lifecycle(tmp_path,monkeypatch,case):
    from types import SimpleNamespace
    import pi_children,pi_subagents
    monkeypatch.setitem(sys.modules,'workbench',SimpleNamespace(
        _wb_state_read=lambda:{'engine_opts':{}}))
    import importlib.util
    spec=importlib.util.spec_from_file_location('child_view_native',ROOT/'desktron/lib/engine_adapters/pi_subagent_view.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    PiSubagentViewMixin=module.PiSubagentViewMixin
    if not pi_sdk_root() or not pi_subagents.available(NATIVE_SUBAGENTS_ROOT):
        pytest.skip('prepared fixed Pi and pi-subagents required')
    monkeypatch.setattr(pi_subagents,'home',lambda:str(NATIVE_SUBAGENTS_ROOT))
    server=SubagentModel()
    async_=case.startswith('async') or case in ('model-async','cancel-async','cancel-tool')
    if not async_:server.child_gate.set()
    (tmp_path/'fixture.txt').write_text('fixture-file-evidence')
    provider=tmp_path/'provider.ts'
    extra=''
    provider.write_text('''export default function(pi){pi.registerProvider('example-gw',{
      baseUrl:%s,apiKey:'fixture',api:'anthropic-messages',models:[{id:'fixture',name:'Fixture',
      reasoning:false,input:['text'],contextWindow:200000,maxTokens:1024,
      cost:{input:0,output:0,cacheRead:0,cacheWrite:0}}]});%s}'''%(json.dumps(server.url),extra))
    adapter=SimpleNamespace(dir=str(tmp_path),workspace='/different/adapter/root',
        index={'test_parent':{'domain':'fixture-without-cwd'}},binary='pi')
    adapter.session_options=lambda _sid,_fallback:{}
    delivered=[]
    adapter.prompt_async=lambda sid,text,opts,**kwargs: (
        delivered.append({'sid':sid,'text':text,'opts':opts,**kwargs}) or {'status':'accepted'})
    adapter.request_status=lambda *_args:{'status':'finished'}
    # Parent cwd has no index.cwd: the original Shepherd failure must not return.
    env=pi_children.environment(adapter)
    # Real PiAdapter.binary is a command name. Absolute fixture paths masked
    # the missing host package root that broke only detached async launches.
    assert env['PI_SUBAGENTS_PI_CODING_AGENT_PACKAGE_ROOT'] == pi_sdk_root()
    bridge=pi_children._brokers[os.path.realpath(adapter.dir)]
    task={'agent':'desk-scout','task':'Read fixture.txt and report fixture-answer.',
          'context':'fork' if case.endswith('fork') else 'fresh'}
    params={'mode':'async' if async_ else 'sync',**task}
    batch='parallel' in case or case in ('partial-failure','async-partial-failure')
    if batch:
        params={'mode':params['mode'],'tasks':[{**task,'agent':a} for a in ('desk-scout','desk-reviewer','desk-oracle')]}
        expected_parallel = 3
        server.child_barrier=threading.Barrier(expected_parallel)
    if case in ('partial-failure','async-partial-failure'):
        server.child_barrier=None;params['tasks'][1]['task']='fixture-failure'
    if case=='cancel-sync':params['task']='fixture-cancel'
    if case=='model-failure':params['task']='fixture-failure'
    deadline_env={}
    if case.endswith('-timeout') or case.endswith('-idle'):
        # The fixture child sleeps 12s without output. Desk, not the model, sets
        # deadlines: -timeout exercises the 6s total, -idle the 5s no-progress stop.
        # A model-supplied timeoutMs must be ignored.
        params['timeoutMs']=600000
        for t in params.get('tasks',[params]):t['task']='fixture-runtime-timeout'
        deadline_env=({'DESKTRON_PI_SUBAGENT_TOTAL_MS':'6000'} if case.endswith('-timeout') else
                      {'DESKTRON_PI_SUBAGENT_IDLE_MS':'5000'})
        if case.endswith('-idle'):monkeypatch.setattr(pi_children,'IDLE_MS',5000)
    # This matrix covers independent notifications; required dependencies use
    # test_pi_child_join_native.py and stay within the original parent call.
    if async_:params['required']=False
    server.parent_args=params
    run_env={**os.environ,**env,'PI_SDK':pi_sdk_root(),'PROVIDER':str(provider),
        'WRAPPER':str(ROOT/'desktron/lib/engine_adapters/pi_children.ts'),
        'DESKTRON_PI_SUBAGENTS_PROVIDER':str(provider),'DESKTRON_SESSION_ID':'test_parent',
        'PI_CODING_AGENT_DIR':str(tmp_path/'agent'),'PARAMS':json.dumps(params),'COUNT':'4' if case=='sequential' else '1',
        **deadline_env}
    if case in ('partial-failure','cancel-sync','model-failure'):run_env['EXPECT_ERROR']='1'
    if (case.endswith('-timeout') or case.endswith('-idle')) and not async_:run_env['EXPECT_ERROR']='1'
    if case=='cancel-sync':run_env['CANCEL']='1'
    if case=='duplicate':run_env['DUPLICATE']='1'
    if case=='registry-race':run_env['STALE_REGISTRY']='1'
    if case.startswith('model-'):run_env['MODEL_DRIVEN']='1'
    view=PiSubagentViewMixin();view.dir=adapter.dir;view.index=adapter.index;view._live={}
    try:
        process=subprocess.Popen(['node',str(ROOT/'desktron/tests/pi-subagents/upstream.mjs')],cwd=tmp_path,
                                 env=run_env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        view._live={'test_parent':{'proc':process,'_pi_tool':{'native-fixture-'+str(i):'part' for i in range(4)}}}
        deadline=time.monotonic()+70
        observed=[]
        try:
            while True:
                try:
                    stdout,stderr=process.communicate(timeout=.1);break
                except subprocess.TimeoutExpired:
                    if time.monotonic()>deadline:raise
                    if not case.startswith('model-'):
                        observed.extend(view.subagents('test_parent'))
            result=subprocess.CompletedProcess(process.args,process.returncode,stdout,stderr)
        finally:
            if process.poll() is None:process.terminate();process.wait(timeout=10)
        assert all(r.get('outcome')!='gone' for r in observed),observed
        assert result.returncode==0,result.stderr[-5000:]+result.stdout[-4000:]
        if case in ('cancel-async','cancel-tool'):
            deadline=time.monotonic()+8
            while not server.requests and time.monotonic()<deadline:time.sleep(.05)
            assert server.requests,'cancel must reach a running child'
            if case=='cancel-tool':
                launch=json.loads(next(tmp_path.glob('subagents/*/_calls/*/launch.json')).read_text())
                for action in ('status','cancel'):
                    response=subprocess.run(['node',str(ROOT/'desktron/tests/pi-subagents/upstream.mjs')],cwd=tmp_path,
                        env={**run_env,'PARAMS':json.dumps({'action':action,'run_id':launch['id']})},text=True,capture_output=True,timeout=20)
                    assert response.returncode==0,response.stderr
            else:bridge.cancel('test_parent')
        if async_:
            assert not delivered,'parent must return before the blocked child'
            if case not in ('cancel-async','cancel-tool'):server.child_gate.set()
        deadline=time.monotonic()+25
        count=4 if case=='sequential' else 1
        while time.monotonic()<deadline:
            bridge.refresh()
            final=[json.loads(p.read_text()) for p in tmp_path.glob('subagents/*/_calls/*/delivery.json')]
            delivery_done = all(r.get('delivery') in (
                ('inline','delivered','suppressed','blocked') if async_ else ('inline',)) for r in final)
            if len(final)==count and all(r.get('status')=='done' for r in final) and delivery_done:break
            time.sleep(.05)
        assert len(final)==count,final
        expected='failed' if case in ('partial-failure','async-partial-failure','model-failure') else 'stopped' if case in ('cancel-sync','cancel-async','cancel-tool') else 'completed'
        if case.endswith('-timeout') or case.endswith('-idle'):expected='failed'
        assert all(r['outcome']==expected for r in final),final
        launches=[json.loads(p.read_text()) for p in tmp_path.glob('subagents/*/_calls/*/launch.json')]
        assert len(launches)==count,launches
        assert all(r['status']=='done' and r['outcome']==expected for r in launches),launches
        assert all(c['status']=='done' for r in launches for c in r.get('children',[])),launches
        if case.endswith('-timeout'):
            assert all('timed out' in r['result'].lower() or 'timeout' in r['result'].lower() for r in final),final
        if case.endswith('-idle'):
            assert all('无进展' in r['result'] for r in final),final
            assert all(c.get('error_code')=='IDLE_TIMEOUT' for r in launches for c in r.get('children',[])),launches
        expected_notifications = ((count + 2) // 3
            if async_ and case not in ('cancel-async','cancel-tool') else 0)
        assert len(delivered)==expected_notifications
        states=[json.loads(p.read_text()) for p in tmp_path.glob('subagents/test_parent/*/state.json')]
        assert len(states)==(3 if batch else count),states
        assert all(s['status']=='done' for s in states),states
        assert len(view.subagents('test_parent'))==len(states)
        if not case.endswith(('-timeout','-idle')) and case not in ('partial-failure','async-partial-failure','cancel-sync','cancel-async','cancel-tool','model-failure'):
            assert all('fixture-answer' in r['result'] for r in final)
        if case in ('partial-failure','async-partial-failure'):
            assert sorted(s['outcome'] for s in states)==['completed','completed','failed']
        if batch and server.child_barrier:assert len(server.overlap)>=expected_parallel
        children=[r for r in server.requests if not any(t['name']=='subagent' for t in r.get('tools',[]))]
        assert all({t['name'] for t in r.get('tools',[])}=={'read','grep','find','ls'} for r in children)
        if case.endswith('fork'):assert 'upstream-parent-context' in json.dumps(children[0]['messages'])
        if case=='duplicate':assert len(children)==2
    finally:
        server.child_gate.set();bridge.close();server.close()

