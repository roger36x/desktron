"""All installed engines must read back fallback history before real loopback turns."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
from unittest import mock

import pytest
import codex_appserver as app
from shepherd.service import Service
from shepherd.harness import ExecutionHarness
from shepherd.unified_runners import UnifiedRunner
from shepherd.codex_connector import CodexFamilyConnector
from shepherd.resident_process import drop
from test_shepherd_image_execution import ImageModel
from test_shepherd_unified_storage import JournalRunner

ROOT=Path(__file__).resolve().parents[2]


@pytest.mark.native
@pytest.mark.parametrize('rejected', [False, True])
@pytest.mark.parametrize('engine,resident,default_model', [
    ('claude',True,False),('pi',True,False),('codex',True,False),
    ('claude',False,False),('pi',False,False),('codex',True,True)])
def test_fallback_native_readback_checkpoint_and_cold_resume(tmp_path, engine, resident, rejected, default_model):
    work=tmp_path/'work';work.mkdir()
    agent=tmp_path/'pi';agent.mkdir()
    model=ImageModel(input_tokens=lambda body:max(100,len(json.dumps(body))//4),
        reject=(lambda body:json.dumps(body).count('history-pad')>600 or
                '你只负责压缩会话历史' in json.dumps(body,ensure_ascii=False)) if rejected else None)
    claude_binary=os.environ.get('DESK_SHEPHERD_CLAUDE_BINARY') or shutil.which('claude')
    assert claude_binary, 'Claude CLI is required for the native gate'
    claude=Path(claude_binary).resolve()
    assert claude.is_file()
    (agent/'models.json').write_text(json.dumps({'providers':{'fixture':{
        'baseUrl':model.url,'api':'anthropic-messages','apiKey':'fixture',
        'models':[{'id':'fixture','name':'Fixture','reasoning':False,'input':['text'],
          'contextWindow':1000000,'maxTokens':1024,
          'cost':{'input':0,'output':0,'cacheRead':0,'cacheWrite':0}}]}}}))
    isolated_home=tmp_path/'home';isolated_home.mkdir()
    real_sdk=Path.home()/'.local/share/desktron/runtime/claude-agent-sdk/node_modules/@anthropic-ai/claude-agent-sdk'
    isolated_sdk=tmp_path/'sdk';isolated_sdk.mkdir()
    (isolated_sdk/'package.json').write_text((real_sdk/'package.json').read_text())
    # Keep the real SDK and installed CLI, but do not execute the developer's global
    # SessionStart hooks. Hooks are side effects and correctly forbid replay.
    module=json.dumps((real_sdk/'sdk.mjs').as_uri())
    (isolated_sdk/'sdk.mjs').write_text(f'export * from {module};\n'
        f'import {{query as nativeQuery}} from {module};\n'
        'export function query(args) { return nativeQuery({...args, options:{...args.options,'
        'settingSources:[], settings:{disableAllHooks:true}}}); }\n')
    env={**os.environ,'PI_CODING_AGENT_DIR':str(agent),'HOME':str(isolated_home),
        'DESK_SHEPHERD_CLAUDE_BINARY':str(claude),
        'DESK_SHEPHERD_CLAUDE_SDK_ROOT':str(isolated_sdk),
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
    with ExitStack() as cleanup:
        cleanup.callback(model.close)
        cleanup.enter_context(mock.patch('shepherd.resident_process.enabled',return_value=resident))
        cleanup.enter_context(mock.patch.object(app,'_appserver_argv',side_effect=argv))
        cleanup.enter_context(mock.patch.object(CodexFamilyConnector,'_seed_private_config'))
        service=Service(tmp_path/'state',storage='unified')
        service.create('fallback','pi' if engine=='claude' else 'claude',work)
        active=service.active('fallback');c=service.connector(active['binding'])
        public=[{'type':'function_call','call_id':'old','name':'historical_probe','arguments':'{}'},
                {'type':'function_call_output','call_id':'old','output':'OMITTED_TOOL_PAYLOAD '*30000},
                {'type':'message','role':'assistant','content':[{
                    'type':'output_text','text':'The verified recovery decision is keep the purple widget.'+
                        (' history-pad'*2000 if rejected else '')}]}]
        lease=active['lease']
        ExecutionHarness(service.coordinator,c,JournalRunner(c,public)).run('fallback',
            lease_id=lease['lease_id'],generation=lease['generation'],prompt='old question',
            input_public=[{'type':'message','role':'user','content':[{'type':'input_text','text':'Remember the widget decision.'}]}])
        service.switch('fallback',engine,work)
        active=service.active('fallback');c=service.connector(active['binding']);bid=c.binding['binding_id']
        cleanup.callback(drop,bid);cleanup.callback(app.drop_session,'shepherd:'+bid)
        opts={'model':'fixture' if engine=='pi' else 'gpt-5.6',
              'sandbox':'read-only','_context_window':64000}
        if default_model:
            # No selected model, advertised window, or explicit compaction
            # threshold: app-server resolves its own default at native dispatch.
            opts={'sandbox':'read-only'}
        command=None
        if engine=='pi':
            opts.update(provider='fixture',effort='off')
            command=[shutil.which('pi'),'--mode','rpc','--offline','--no-extensions',
                '--extension',str(ROOT/'desktron/lib/engine_adapters/pi_gate.ts'),
                '--extension',str(ROOT/'desktron/lib/engine_adapters/pi_autocompact.ts'),
                '--no-approve','--provider','fixture','--model','fixture']
        for seq in (2,3):
            events=[]
            runner=UnifiedRunner(c,timeout=45,env=env,options=opts,command=command,
                                 on_event=lambda *args:events.append(args),
                                 system_text='Synthetic fallback verification fixture.')
            lease=service.active('fallback')['lease'];prompt=f'continue widget {seq}'
            request_start=len(model.requests)
            try:
                result=ExecutionHarness(service.coordinator,c,runner).run('fallback',
                    lease_id=lease['lease_id'],generation=lease['generation'],prompt=prompt,
                    input_public=[{'type':'message','role':'user','content':[{'type':'input_text','text':prompt}]}])
            except Exception as exc:
                detail=''.join(runner.process.errors) if runner.process else ''
                raise AssertionError(str(exc)+'\n'+detail[-2500:]) from exc
            assert result['terminal_state']=='completed', (vars(runner.context_tracker),
                runner.context_retry_error,runner.context_retries)
            if not rejected:assert bool(runner.degraded_context)==(seq==2 or default_model)
            if seq==3:assert runner.context_retries==0
            if seq==2:
                assert runner.checkpoint is not None
                assert runner.degraded_context['through_seq']==(2 if rejected else 1)
                assert runner.context_retries==(1 if rejected else 0)
            received=json.dumps(model.requests[request_start:])
            assert 'purple widget' in received
            assert 'bounded fallback context' in received
            assert 'OMITTED_TOOL_PAYLOAD' not in received
            assert all(len(json.dumps(request))<200000 for request in model.requests[request_start:])
            # Native schemas are additional; a rejected request and its replacement
            # count separately, rather than pretending recovery made only one call.
            assert model.tool_requests==0
            # Force cold reconstruction: second turn cannot pass using only RAM.
            drop(bid);app.drop_session('shepherd:'+bid)
        assert len(service.coordinator.state('fallback')['turns'])==(4 if rejected else 3)
