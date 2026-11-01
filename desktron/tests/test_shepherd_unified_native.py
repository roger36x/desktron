"""Real pinned native loops against loopback fixtures; no online model quota."""
import base64
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock
import pytest

import codex_appserver as app
from shepherd.service import Service
from shepherd.coordinator import digest
from shepherd.harness import ExecutionHarness
from shepherd.unified_runners import UnifiedRunner
from shepherd.claude_connector import ClaudeConnector, managed_claude_binary
from shepherd.codex_connector import CodexFamilyConnector
from shepherd.source_resolver import SourceResolver
from shepherd.projector import ReadProjector
from test_shepherd_image_execution import ImageModel
from test_shepherd_images import fixture_png
from test_claude_turn_boundary import Bus, ea
from engine_adapters.shepherd_events import EventProjection

ROOT=Path(__file__).resolve().parents[2]


@pytest.mark.native
class UnifiedNative(unittest.TestCase):
    def test_four_engines_switch_and_recreate_without_native_transcripts(self):
        self.chain(False)

    def test_native_tool_image_roundtrip_never_reexecutes_the_tool(self):
        self.chain(True)

    def test_consecutive_pi_turns_reuse_resident_host(self):
        self.assertTrue(shutil.which('pi'),'pi required for native gate')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);work=root/'work';work.mkdir();agent=root/'pi';agent.mkdir()
            model=ImageModel(input_tokens=lambda body:max(100,len(json.dumps(body))//4))
            self.addCleanup(model.close)
            (agent/'models.json').write_text(json.dumps({'providers':{'fixture':{
                'baseUrl':model.url,'api':'anthropic-messages','apiKey':'fixture',
                'models':[{'id':'fixture','name':'Fixture','reasoning':False,'input':['text'],
                    'contextWindow':1000000,'maxTokens':1024,
                    'cost':{'input':0,'output':0,'cacheRead':0,'cacheWrite':0}}]}}}))
            env={**os.environ,'PI_CODING_AGENT_DIR':str(agent)}
            service=Service(root/'state',storage='unified');service.create('resident','pi',work)
            active=service.active('resident');connector=service.connector(active['binding'])
            binding_id=active['binding']['binding_id']
            command=[shutil.which('pi'),'--mode','rpc','--offline','--no-extensions',
                '--extension',str(ROOT/'desktron/lib/engine_adapters/pi_gate.ts'),
                '--no-approve','--provider','fixture','--model','fixture']
            opts={'model':'fixture','provider':'fixture','effort':'off','_context_window':1000000}
            pids=[]
            try:
                for seq in (1,2):
                    runner=UnifiedRunner(connector,timeout=50,env=env,options=opts,command=command)
                    prompt=f'resident question {seq}'
                    lease=active['lease']
                    with mock.patch.object(runner,'_payload',wraps=runner._payload) as payload:
                        row=ExecutionHarness(service.coordinator,connector,runner).run('resident',
                            lease_id=lease['lease_id'],generation=lease['generation'],prompt=prompt,
                            input_public=[{'type':'message','role':'user','content':[{
                                'type':'input_text','text':prompt}]}])
                        self.assertEqual(payload.call_count, 1 if seq == 1 else 0)
                    self.assertEqual(row['terminal_state'],'completed')
                    from shepherd.resident_process import roster
                    pids.append(next(pid for bid,pid,_busy,_at in roster() if bid==binding_id))
                    active=service.active('resident')
                self.assertEqual(pids[0],pids[1])
                self.assertEqual(len(model.requests),2)
                self.assertIn('resident question 1',json.dumps(model.requests[1]))
                self.assertEqual(len(service.coordinator.state('resident')['turns']),2)
                self.assertEqual(list((root/'state/execution').glob('turn-*')),[])
                with connector.archive.db() as db:
                    db.execute("UPDATE events SET payload='{}' WHERE turn_id=? AND ordinal=1",
                               ('resident:1',))
                runner=UnifiedRunner(connector,timeout=50,env=env,options=opts,command=command)
                from shepherd.store import PromptNotStarted
                with self.assertRaises(PromptNotStarted):
                    ExecutionHarness(service.coordinator,connector,runner).run('resident',
                        lease_id=lease['lease_id'],generation=lease['generation'],prompt='must not dispatch')
                self.assertEqual(len(model.requests),2)
            finally:
                from shepherd.resident_process import drop
                drop(binding_id)

    def test_consecutive_claude_turns_reuse_resident_native_query(self):
        claude=os.environ.get('DESK_SHEPHERD_CLAUDE_BINARY') or shutil.which('claude')
        self.assertTrue(claude and Path(claude).is_file(),'Claude CLI required')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);work=root/'work';work.mkdir()
            model=ImageModel(input_tokens=lambda body:max(100,len(json.dumps(body))//4))
            self.addCleanup(model.close)
            env={**os.environ,
                'DESK_SHEPHERD_CLAUDE_BINARY':claude,
                'DESK_SHEPHERD_CLAUDE_SDK_ROOT':str(Path.home()/'.local/share/desktron/runtime/claude-agent-sdk/node_modules/@anthropic-ai/claude-agent-sdk'),
                'ANTHROPIC_BASE_URL':model.url,'ANTHROPIC_API_KEY':'fixture',
                'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1'}
            service=Service(root/'state',storage='unified');service.create('resident','claude',work)
            active=service.active('resident');connector=service.connector(active['binding'])
            binding_id=active['binding']['binding_id'];pids=[];anchors=[];sources=[]
            # This gate verifies protocol reuse. RSS-triggered cache rotation is
            # an independent policy with deterministic coverage elsewhere.
            from shepherd import resident_process
            rss_policy=mock.patch.object(resident_process,'_SOFT_RSS_MB',0)
            rss_policy.start();self.addCleanup(rss_policy.stop)
            try:
                for seq in (1,2):
                    runner=UnifiedRunner(connector,timeout=50,env=env,
                        options={'model':'gpt-5.6','sandbox':'read-only',
                                 '_context_window':1000000},
                        system_text='Synthetic resident fixture')
                    prompt=f'claude resident question {seq}'
                    lease=active['lease']
                    row=ExecutionHarness(service.coordinator,connector,runner).run('resident',
                        lease_id=lease['lease_id'],generation=lease['generation'],prompt=prompt,
                        input_public=[{'type':'message','role':'user','content':[{
                            'type':'input_text','text':prompt}]}])
                    self.assertEqual(row['terminal_state'],'completed')
                    source=connector.archive.read(connector.binding,f'resident:{seq}')['source']
                    sources.append(source)
                    anchors.append(next(event for event in source['events']
                                        if event.get('type')=='native_anchor'))
                    from shepherd.resident_process import roster
                    hosts=[pid for bid,pid,_busy,_at in roster() if bid==binding_id]
                    self.assertTrue(hosts, f'Claude resident missing after committed turn {seq}')
                    pids.append(hosts[0])
                    active=service.active('resident')
                self.assertEqual(pids[0],pids[1])
                first_tail=[event['value'].get('uuid') for event in sources[0]['events']
                            if event.get('type')=='claude_entry' and event['value'].get('uuid')][-1]
                self.assertEqual(anchors[1]['parent_uuid'],first_tail)
                actual=subprocess.run([claude,'--version'],capture_output=True,text=True,
                                      timeout=5,check=True).stdout.split()[0]
                self.assertEqual([anchor['claude_version'] for anchor in anchors],[actual,actual])
                self.assertNotEqual(anchors[0]['store_revision'],anchors[1]['store_revision'])
                self.assertLess(anchors[0]['store_size'],anchors[1]['store_size'])
                self.assertEqual(len(model.requests),2)
                self.assertIn('claude resident question 1',json.dumps(model.requests[1]))
                self.assertEqual(len(service.coordinator.state('resident')['turns']),2)
                self.assertEqual(list((root/'state/execution').glob('turn-*')),[])
            finally:
                from shepherd.resident_process import drop
                drop(binding_id)
            self.assertFalse(list((root/'state/resident/claude').rglob('*.bash-env-*')))

    def chain(self, tools):
        for name in ('pi','codex'):
            self.assertTrue(shutil.which(name),name+' required for complete native gate')
        claude = os.environ.get('DESK_SHEPHERD_CLAUDE_BINARY') or shutil.which('claude')
        self.assertTrue(claude and Path(claude).is_file(),'Claude CLI required')
        # Hosts outlive turns; stop fixture writers before removing their HOME.
        with tempfile.TemporaryDirectory() as folder, ExitStack() as cleanup:
            root=Path(folder);work=root/'work';work.mkdir();agent=root/'pi';agent.mkdir()
            def release_hosts(binding_id):
                from shepherd import resident_process as resident
                with resident._LOCK:
                    native = resident._HOSTS.get(binding_id)
                if native is not None:
                    released = resident.drop(binding_id, native.process)
                    with resident._LOCK:
                        self.assertIsNot(resident._HOSTS.get(binding_id), native,
                                         'Fixture native writer remains fenced')
                    self.assertTrue(released or native.process.proc.poll() is not None)
                session_id = 'shepherd:' + binding_id
                with app._SESSIONS_LOCK:
                    app_host = app._SESSIONS.get(session_id)
                app.drop_session(session_id, app_host)
                if app_host is not None:
                    self.assertIsNotNone(app_host.conn.proc.poll(),
                                         'Fixture app-server is still running')
            def assert_execution_clean():
                # A terminal writer may exit before a detached native descendant
                # releases its process group. Scratch must remain fenced during
                # that bounded cleanup, then be collected without age guessing.
                from shepherd.execution_scratch import sweep
                deadline=time.monotonic()+5
                pending=list((root/'state/execution').glob('turn-*'))
                while pending and time.monotonic()<deadline:
                    time.sleep(.05);sweep(root/'state')
                    pending=list((root/'state/execution').glob('turn-*'))
                self.assertEqual(pending,[])
            image=work/'probe.png';image.write_bytes(base64.b64decode(fixture_png(32,32)))
            model=ImageModel(input_tokens=lambda body:max(100,len(json.dumps(body))//4),
                             tool_image=image if tools else None);self.addCleanup(model.close)
            (agent/'models.json').write_text(json.dumps({'providers':{'fixture':{
                'baseUrl':model.url,'api':'anthropic-messages','apiKey':'fixture',
                'models':[{'id':'fixture','name':'Fixture','reasoning':False,'input':['text','image'],
                    'contextWindow':1000000,'maxTokens':1024,
                    'cost':{'input':0,'output':0,'cacheRead':0,'cacheWrite':0}}]}}}))
            env={**os.environ,'PI_CODING_AGENT_DIR':str(agent),'DESK_SHEPHERD_CLAUDE_BINARY':claude,
                'DESK_SHEPHERD_CLAUDE_SDK_ROOT':str(Path.home()/'.local/share/desktron/runtime/claude-agent-sdk/node_modules/@anthropic-ai/claude-agent-sdk'),
                'ANTHROPIC_BASE_URL':model.url,'ANTHROPIC_API_KEY':'fixture',
                'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1'}
            def argv(binary,opts=None):
                return [binary,'-c','model_provider="fixture"','-c','model="gpt-5.6"',
                    '-c','model_providers.fixture.name="Fixture"',
                    '-c',f'model_providers.fixture.base_url="{model.url}/v1"',
                    '-c','model_providers.fixture.wire_api="responses"',
                    '-c','model_providers.fixture.requires_openai_auth=false',
                    *(['app-server','--stdio'] if os.path.basename(binary)=='codex'
                      else ['app-server','--listen','stdio://'])]
            with mock.patch.object(app,'_appserver_argv',side_effect=argv), \
                 mock.patch.object(CodexFamilyConnector,'_seed_private_config'):
                engines=(('claude','pi','codex') if tools else
                         ('pi','claude','codex','pi','claude','claude','pi','claude','pi','pi','codex','pi'))
                for seq,engine in enumerate(engines,1):
                    with self.subTest(engine=engine,seq=seq):
                        service=Service(root/'state',storage='unified')
                        if seq==1:service.create('chain',engine,work)
                        else:service.switch('chain',engine,work)
                        active=service.active('chain');connector=service.connector(active['binding'])
                        cleanup.callback(release_hosts, active['binding']['binding_id'])
                        opts=({'model':'fixture','provider':'fixture','effort':'off','_context_window':1000000}
                              if engine=='pi' else {'model':'gpt-5.6','sandbox':'read-only',
                                                   '_context_window':1000000})
                        command=([shutil.which('pi'),'--mode','rpc','--offline','--no-extensions',
                            '--extension',str(ROOT/'desktron/lib/engine_adapters/pi_gate.ts'),
                            '--no-approve','--provider','fixture','--model','fixture'] if engine=='pi' else None)
                        if engine=='claude':opts['_grounding']='Synthetic fixture capabilities for the current turn'
                        if tools:opts['mode']='bypassPermissions'
                        events=[]
                        worker={'claude':ea.ClaudeAdapter,'pi':ea.PiAdapter,
                                'codex':ea.CodexAdapter}[engine](
                                    str(root/'ui'),str(work),Bus())
                        ctx={'sid':'adp_native_fixture','msg_id':f'msg_{seq}','parts':{},'order':[],
                             'last_tool':None,'t0':0}
                        warnings=[]
                        projection=EventProjection(engine,worker,ctx,warnings.append)
                        def project(*args):
                            events.append(args[-1])
                            projection(*args)
                        runner=UnifiedRunner(connector,timeout=50,env=env,options=opts,command=command,
                                             on_event=project)
                        if not tools and seq in (9,13):
                            self.assertIsNotNone(connector.context({**opts,'_system_hash':digest(runner.system_text)})[1])
                        prompt='/compact' if seq in (7,11) else f'fixture question {seq}'
                        if not tools and seq in (1,2):prompt += ' archived detail' * 16000
                        lease=active['lease']
                        request_start=len(model.requests)
                        try:
                            row=ExecutionHarness(service.coordinator,connector,runner).run('chain',
                                lease_id=lease['lease_id'],generation=lease['generation'],prompt=prompt,
                                input_public=[{'type':'message','role':'user','content':[{'type':'input_text','text':prompt}]}])
                        except Exception as exc:
                            detail=''.join(runner.process.errors) if runner.process else ''
                            raise AssertionError(str(exc)+'\n'+detail[-3000:]) from exc
                        self.assertEqual(row['terminal_state'],'completed')
                        self.assertEqual(warnings,[])
                        if not tools and 1<seq<=7:
                            received=json.dumps(model.requests[request_start:])
                            for previous in range(1,seq):
                                self.assertIn(f'fixture question {previous}',received)
                        state=service.coordinator.state('chain')
                        projector=ReadProjector(service.store,SourceResolver([
                            service.connector(b) for b in state['bindings'].values()]))
                        history=projector.project('chain')
                        self.assertEqual(len(history),seq)
                        if seq not in (7,11):self.assertIn('image-fixture-ok',json.dumps(history[-1]['public']))
                        if seq in (7,11):self.assertIsNotNone(runner.checkpoint)
                        if seq==7:
                            self.assertTrue(any(e.get('type')=='user' and isinstance(
                                (e.get('message') or {}).get('content'),str) for e in events))
                        self.assertFalse((root/'state/native').exists())
                        assert_execution_clean()
                        if tools and seq==1:
                            image.unlink()
                        if tools:
                            self.assertIn('data:image/png;',json.dumps(history[0]['public']))
                            self.assertEqual(model.tool_requests,1)
                            if seq==1:
                                expected_images=model.observations[-1]
                                self.assertTrue(expected_images)
                            else:
                                self.assertEqual(model.observations[-1],expected_images)
                self.assertFalse(list((agent/'sessions').rglob('*.jsonl')))
                if tools:
                    self.assertTrue(all('fixture question ' in json.dumps(
                        body.get('input',body.get('messages'))) for body in model.requests),
                        'Disposable native execution started an unrelated background model request')
