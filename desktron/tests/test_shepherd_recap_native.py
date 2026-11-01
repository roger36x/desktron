"""Opt-in native SDK/CLI recap against a loopback fixture, without online quota."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'desktron/lib'))
import codex_appserver as app
from engine_adapters.bus import EventBus
from engine_adapters.shepherd import ShepherdAdapter
from engine_adapters.recap import _RecapRun
from shepherd import ExecutionHarness, PiRunner, ClaudeRunner, CodexFamilyRunner
from shepherd.unified_runners import UnifiedRunner
from shepherd.claude_connector import ClaudeConnector
from shepherd.codex_connector import CodexFamilyConnector
from test_shepherd_image_execution import ImageModel


@unittest.skipUnless(os.environ.get('DESK_SHEPHERD_NATIVE_RECAP')=='1','opt-in native recap')
class NativeRecap(unittest.TestCase):
    def test_four_native_sources_are_unchanged_after_recap(self):
        sdk=str(Path.home()/'.local/share/desktron/runtime/claude-agent-sdk/node_modules/@anthropic-ai/claude-agent-sdk')
        claude=os.environ.get('DESK_SHEPHERD_CLAUDE_BINARY') or str(Path.home()/'.local/share/desktron/data/claude/versions/2.1.268')
        if not ClaudeConnector.availability(sdk_root=sdk,claude_binary=claude)['available']:
            self.skipTest('pinned Claude SDK required')
        for engine in ('pi','claude','codex'):
            if not shutil.which(engine): self.skipTest(engine+' missing')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);work=root/'work';work.mkdir();agent=root/'agent';agent.mkdir()
            model=ImageModel();self.addCleanup(model.close)
            (agent/'models.json').write_text(json.dumps({'providers':{'fixture':{
                'baseUrl':model.url,'api':'anthropic-messages','apiKey':'fixture',
                'models':[{'id':'fixture','name':'Fixture','reasoning':False,'input':['text'],
                          'contextWindow':1000000,'maxTokens':1024,
                          'cost':{'input':0,'output':0,'cacheRead':0,'cacheWrite':0}}]}}}))
            env={**os.environ,'PI_CODING_AGENT_DIR':str(agent),
                 'CLAUDE_CONFIG_DIR':str(root/'claude-config'),'ANTHROPIC_BASE_URL':model.url,
                 'DESK_SHEPHERD_CLAUDE_BINARY':claude,'DESK_SHEPHERD_CLAUDE_SDK_ROOT':sdk,
                 'ANTHROPIC_API_KEY':'fixture','CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1'}
            def argv(binary, opts=None):
                return [binary,'-c','model_provider="fixture"','-c','model="gpt-5.6"',
                    '-c','model_providers.fixture.name="Fixture"',
                    '-c',f'model_providers.fixture.base_url="{model.url}/v1"',
                    '-c','model_providers.fixture.wire_api="responses"',
                    '-c','model_providers.fixture.requires_openai_auth=false',
                    *(['app-server','--stdio'] if os.path.basename(binary)=='codex'
                      else ['app-server','--listen','stdio://'])]
            which=shutil.which
            with mock.patch.object(shutil,'which',side_effect=lambda name:claude if name=='claude' else which(name)), \
                 mock.patch.object(app,'_appserver_argv',side_effect=argv), \
                 mock.patch.object(CodexFamilyConnector,'_seed_private_config'), \
                 mock.patch.object(ClaudeConnector,'default_sdk_root',return_value=sdk):
                ad=ShepherdAdapter(str(root/'data'),str(work),EventBus())
                for engine in ('pi','claude','codex'):
                    with self.subTest(engine=engine):
                        sid=ad.create_session(opts={'active_engine':engine})['id']
                        active=ad.service.active(sid);connector=ad._connector(active['binding'])
                        worker=ad._worker(engine)
                        worker.spawn_env=lambda opts:env
                        worker.system_body=lambda opts:''
                        opts={'model':'fixture','provider':'fixture','effort':'off'} if engine=='pi' else {'model':'gpt-5.6'}
                        if engine=='pi':
                            runner=UnifiedRunner(connector,timeout=40,env=env,options=opts,command=[shutil.which('pi'),
                                '--mode','rpc','--offline','--no-extensions',
                                '--extension',str(ROOT/'desktron/lib/engine_adapters/pi_gate.ts'),
                                '--no-approve','--provider','fixture','--model','fixture'])
                        elif engine=='claude': runner=UnifiedRunner(connector,timeout=50,env=env,options=opts)
                        else: runner=UnifiedRunner(connector,timeout=45,env=env,options=opts)
                        lease=active['lease']
                        ExecutionHarness(ad.service.coordinator,connector,runner).run(sid,
                            lease_id=lease['lease_id'],generation=lease['generation'],prompt='summarize this fixture')
                        before=ad.service.active(sid)
                        manifest=ad.service.store.load(sid)
                        source=before['state']['rounds'][0]['source']
                        durable=connector.resolve_source(source)
                        def ordinary_history():
                            return {str(p):p.read_bytes() for directory in
                                    (root/'claude-config/projects',agent/'sessions')
                                    for p in directory.rglob('*.jsonl')}
                        ordinary=ordinary_history()
                        snap=ad._recap_snapshot(sid,{'seq':1,'opts':opts})
                        run=_RecapRun(sid,1)
                        answer=ad.recap_generate(sid,run,snap)
                        self.assertEqual(answer,'image-fixture-ok')
                        self.assertEqual(ad.service.store.load(sid),manifest)
                        self.assertEqual(connector.resolve_source(source),durable)
                        self.assertEqual(ad.service.active(sid),before)
                        self.assertEqual(ordinary_history(),ordinary)
                        self.assertNotIn('engine_sid',ad.index[sid])


if __name__=='__main__': unittest.main()
