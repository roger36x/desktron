#!/usr/bin/env python3
"""Synthetic local model benchmark; never reads user conversations or online models."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'desktron/lib'),str(ROOT/'desktron/tests')]
# Keep Desk metadata isolated even outside pytest's conftest.
os.environ['DESKTRON_DATA_HOME']=tempfile.mkdtemp(prefix='shepherd-bench-data-')
import codex_appserver as app
from shepherd.service import Service
from shepherd.coordinator import digest
from shepherd.harness import ExecutionHarness
from shepherd.unified_runners import UnifiedRunner
from shepherd.runners import ClaudeRunner,PiRunner,CodexFamilyRunner
from shepherd.claude_connector import ClaudeConnector
from shepherd.codex_connector import CodexFamilyConnector
saved_argv=sys.argv
sys.argv=['ui_server.py',str(ROOT/'frontend'),'7999','7998',str(ROOT)]
try:
    from test_shepherd_image_execution import ImageModel
finally:sys.argv=saved_argv


def size(root):
    return sum(p.stat().st_size for p in Path(root).rglob('*') if p.is_file() and not p.is_symlink())


class Meter:
    """Sample process-tree RSS and macOS kernel disk-I/O counters (lower bounds)."""
    def __init__(self,root):
        self.root=root;self.stop=threading.Event();self.peak_rss=0;self.peak_temp=0
        self.io={};self.start={};self.thread=threading.Thread(target=self.loop,daemon=True)
        self.lib=ctypes.CDLL('/usr/lib/libproc.dylib') if sys.platform=='darwin' else None
        if self.lib:
            self.lib.proc_pid_rusage.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_void_p]
        self.thread.start()
    def loop(self):
        while not self.stop.is_set():
            try:
                rows=[list(map(int,line.split())) for line in subprocess.check_output(
                    ['ps','-axo','pid=,ppid=,rss='],text=True).splitlines()]
                ids={os.getpid()}
                for _ in range(8):ids|={pid for pid,parent,rss in rows if parent in ids}
                self.peak_rss=max(self.peak_rss,sum(rss*1024 for pid,_,rss in rows if pid in ids))
                if self.lib:
                    for pid in ids:
                        # Darwin rusage_info_v2: 16-byte UUID followed by 18 uint64s.
                        raw=ctypes.create_string_buffer(160)
                        if self.lib.proc_pid_rusage(pid,2,raw)==0:
                            values=(ctypes.c_uint64*18).from_buffer_copy(raw.raw[16:])
                            key=(pid,values[8]);counts=(values[16],values[17])
                            if key not in self.start:self.start[key]=counts if pid==os.getpid() else (0,0)
                            self.io[key]=counts
                temporary=sum(size(p) for p in (self.root/'state/execution',self.root/'temporary') if p.exists())
                self.peak_temp=max(self.peak_temp,temporary)
            except (OSError,ValueError):pass
            self.stop.wait(.1)
    def finish(self):
        self.stop.set();self.thread.join()
        return {'peak_tree_rss_bytes':self.peak_rss,'peak_temp_bytes':self.peak_temp,
                'sampled_disk_read_bytes':sum(max(0,v[0]-self.start[k][0]) for k,v in self.io.items()),
                'sampled_disk_write_bytes':sum(max(0,v[1]-self.start[k][1]) for k,v in self.io.items())}


def seed(service,count,work):
    service.create('bench','pi',work)
    active=service.active('bench');c=service.connector(active['binding']);rows=[]
    for i in range(1,count+1):
        public=[{'type':'message','role':'user','content':[{'type':'input_text','text':f'Historical question {i}'}]},
            {'type':'function_call','call_id':f'tool{i}','name':'read','arguments':'{}'},
            {'type':'function_call_output','call_id':f'tool{i}','output':f'Record {i}: '+('historical tool result. '*180)},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':f'Historical answer {i}'}]}]
        rows.append({'round_id':f'bench:{i}','offset':i,'entry_hash':digest(public),'public':public})
    if getattr(c,'unified',False):
        for row in rows:
            c.archive.begin(c.binding,row['round_id'])
            for item in c.encode(row):
                if item.get('type')!='custom':item={'type':'message','message':item}
                c.archive.append(c.binding,row['round_id'],{'type':'pi_entry','value':item})
            c.archive.finish(c.binding,row['round_id'],'completed')
    else:c.append(rows,'')
    refs=[{'kind':'native_round','engine':'pi','session_id':c.native_session_id,'entry_id':r['round_id'],
           'source_key':f"pi:{c.native_session_id}:{r['round_id']}"} for r in rows]
    for ref,resolved in zip(refs,c.resolve_sources(refs)):
        row=service.coordinator.commit('bench',active['lease']['lease_id'],active['lease']['generation'],
            source=ref,source_hash=digest(resolved['source']),public_hash=digest(resolved['public']))
    service.coordinator.advance('bench',c.binding_id,count,row['entry_hash'],c.revision(),ownership_nonce=c.ownership_nonce)
    return sum(len(json.dumps(r['public']).encode()) for r in rows)


def case(mode,count,cycles):
    with tempfile.TemporaryDirectory(prefix='shepherd-bench-') as folder:
        root=Path(folder);work=root/'work';work.mkdir();agent=root/'pi';agent.mkdir()
        temporary=root/'temporary';temporary.mkdir()
        model=ImageModel();sdk=str(Path.home()/'.local/share/desktron/runtime/claude-agent-sdk/node_modules/@anthropic-ai/claude-agent-sdk')
        claude=str(Path.home()/'.local/share/desktron/data/claude/versions/2.1.268')
        env={**os.environ,'PI_CODING_AGENT_DIR':str(agent),'ANTHROPIC_BASE_URL':model.url,
             'ANTHROPIC_API_KEY':'fixture','CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1',
             'TMPDIR':str(temporary),'DESK_SHEPHERD_CLAUDE_BINARY':claude,'DESK_SHEPHERD_CLAUDE_SDK_ROOT':sdk}
        (agent/'models.json').write_text(json.dumps({'providers':{'fixture':{'baseUrl':model.url,
            'api':'anthropic-messages','apiKey':'fixture','models':[{'id':'fixture','name':'Fixture',
            'reasoning':False,'input':['text'],'contextWindow':1000000,'maxTokens':1024,
            'cost':{'input':0,'output':0,'cacheRead':0,'cacheWrite':0}}]}}}))
        def argv(binary,opts=None):
            return [binary,'-c','model_provider="fixture"','-c','model="gpt-5.6"',
                '-c','model_providers.fixture.name="Fixture"','-c',f'model_providers.fixture.base_url="{model.url}/v1"',
                '-c','model_providers.fixture.wire_api="responses"','-c','model_providers.fixture.requires_openai_auth=false',
                *(['app-server','--stdio'] if os.path.basename(binary)=='codex' else ['app-server','--listen','stdio://'])]
        which=shutil.which
        meter=None
        try:
            with mock.patch.dict(os.environ,{'TMPDIR':str(temporary)}),mock.patch.object(app,'_appserver_argv',side_effect=argv),mock.patch.object(CodexFamilyConnector,'_seed_private_config'), \
                 mock.patch.object(ClaudeConnector,'default_sdk_root',return_value=sdk), \
                 mock.patch.object(shutil,'which',side_effect=lambda n:claude if n=='claude' else which(n)):
                service=Service(root/'state',storage=mode,claude_system_lookup=lambda _: 'Synthetic benchmark system.')
                corpus=seed(service,count,work);records=[]
                for cycle in range(cycles):
                    for engine in ('claude','codex','pi'):
                        model.requests.clear()
                        meter=Meter(root);switch_start=time.perf_counter()
                        start=switch_start;service.switch('bench',engine,work);switch=time.perf_counter()-start
                        active=service.active('bench');c=service.connector(active['binding'])
                        opts={'model':'fixture','provider':'fixture','effort':'off'} if engine=='pi' else {'model':'gpt-5.6','sandbox':'read-only'}
                        command=[which('pi'),'--mode','rpc','--offline','--no-extensions','--extension',str(ROOT/'desktron/lib/engine_adapters/pi_gate.ts'),'--no-approve','--provider','fixture','--model','fixture'] if engine=='pi' else None
                        first=[];dispatched=[]
                        def event(*args):
                            if len(args)==2:
                                method,value=args
                                text=value.get('delta') if method=='item/agentMessage/delta' else ''
                                item=value.get('item') or {}
                                if method=='item/completed' and item.get('type')=='agentMessage':
                                    text=item.get('text') or text
                            else:
                                value=args[0]
                                delta=(value.get('event') or {}).get('delta') or {}
                                update=value.get('assistantMessageEvent') or {}
                                text=delta.get('text') if delta.get('type')=='text_delta' else (update.get('delta') if update.get('type')=='text_delta' else '')
                            if text and not first:first.append(time.perf_counter())
                        runner_type=UnifiedRunner if mode=='unified' else {'pi':PiRunner,'claude':ClaudeRunner,'codex':CodexFamilyRunner}[engine]
                        kwargs=dict(options=opts,env=env,timeout=60,system_text='Synthetic benchmark system.',on_event=event)
                        if engine=='pi':kwargs['command']=command
                        runner=runner_type(c,**kwargs)
                        real_run=runner.run
                        def run(**kw):
                            fence=kw['on_dispatched']
                            def dispatch(**data):dispatched.append(time.perf_counter());return fence(**data)
                            return real_run(**{**kw,'on_dispatched':dispatch})
                        runner.run=run
                        start=time.perf_counter()
                        result=ExecutionHarness(service.coordinator,c,runner).run('bench',lease_id=active['lease']['lease_id'],
                            generation=active['lease']['generation'],prompt='fixture next question',input_public=[{'type':'message','role':'user','content':[{'type':'input_text','text':'fixture next question'}]}])
                        elapsed=time.perf_counter()-start;metrics=meter.finish()
                        assert result['terminal_state']=='completed' and first,(mode,engine,cycle,result['terminal_state'])
                        received=json.dumps(model.requests)
                        for i in range(1,count+1):
                            assert f'Historical question {i}' in received,(mode,engine,cycle,'missing history',i)
                        records.append({'engine':engine,'cycle':cycle,'switch_seconds':switch,'turn_seconds':elapsed,
                            'dispatch_seconds':dispatched[0]-start,'first_text_seconds':first[0]-start,
                            'switch_to_first_text_seconds':first[0]-switch_start,
                            'persistent_bytes':size(root/'state'),'retained_temporary_bytes':size(temporary),**metrics})
                        print(json.dumps({'engine':engine,'cycle':cycle,'switch_seconds':round(switch,3),
                            'switch_to_first_text_seconds':round(first[0]-switch_start,3)}),flush=True)
                return {'mode':mode,'seed_turns':count,'seed_public_bytes':corpus,'records':records}
        finally:
            if meter is not None:meter.finish()
            model.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--mode',choices=['native','unified'],required=True)
    parser.add_argument('--turns',type=int,default=32);parser.add_argument('--cycles',type=int,default=2)
    parser.add_argument('--output',required=True);args=parser.parse_args()
    if args.turns<1 or args.cycles<1:parser.error('turns and cycles must be positive')
    try:result=case(args.mode,args.turns,args.cycles)
    finally:shutil.rmtree(os.environ['DESKTRON_DATA_HOME'])
    Path(args.output).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'mode':args.mode,'turns':args.turns,'samples':len(result['records'])}))
