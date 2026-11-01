"""Real Pi RPC + SessionManager, synthetic loopback provider, private sessions."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'desktron/lib'))
from shepherd import Service, ExecutionHarness, PiRunner
from shepherd.pi_connector import pi_sdk_root


@unittest.skipUnless(shutil.which('pi') and pi_sdk_root(), 'installed Pi required')
class PiExecution(unittest.TestCase):
    def test_tool_approve_deny_and_cancel_in_private_session(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); work = root/'work'; work.mkdir()
            agent = root/'agent'; agent.mkdir()
            server = subprocess.Popen(['node', str(ROOT/'desktron/tests/shepherd/claude-fixture-server.mjs')],
                                      stdout=subprocess.PIPE, text=True)
            def close():
                server.terminate(); server.wait(timeout=5); server.stdout.close()
            self.addCleanup(close)
            port = json.loads(server.stdout.readline())['port']
            (agent/'models.json').write_text(json.dumps({'providers':{'fixture':{
                'baseUrl':f'http://127.0.0.1:{port}', 'api':'anthropic-messages', 'apiKey':'fixture',
                'models':[{'id':'fixture', 'name':'Fixture', 'reasoning':False, 'input':['text'],
                    'contextWindow':200000, 'maxTokens':1024,
                    'cost':{'input':0,'output':0,'cacheRead':0,'cacheWrite':0}}]}}}))
            env = {**os.environ, 'PI_CODING_AGENT_DIR':str(agent),
                   'DESKTRON_PI_PERMISSION_MODE':'default'}
            service = Service(root/'state')
            service.create('conversation', 'pi', str(work))
            command = [shutil.which('pi'), '--mode','rpc','--offline','--no-extensions',
                '--extension',str(ROOT/'desktron/lib/engine_adapters/pi_gate.ts'),
                '--no-approve','--provider','fixture','--model','fixture']
            for mode in ('approve','deny','cancel'):
                with self.subTest(mode=mode):
                    active = service.active('conversation'); lease = active['lease']
                    connector = service.connector(active['binding'])
                    marker = root/mode; asks = []
                    def event(row):
                        if row.get('type') == 'extension_ui_request' and row.get('method') == 'confirm':
                            asks.append(row)
                            if mode == 'cancel': runner.cancel()
                            else: runner.process.send({'type':'extension_ui_response','id':row['id'],
                                                       'confirmed':mode == 'approve'})
                    runner = PiRunner(connector, on_event=event, command=command, env=env, timeout=25)
                    result = ExecutionHarness(service.coordinator, connector, runner).run(
                        'conversation', lease_id=lease['lease_id'], generation=lease['generation'],
                        prompt='permission-fixture:' + str(marker))
                    self.assertTrue(asks)
                    self.assertEqual(marker.exists(), mode == 'approve')
                    self.assertEqual(result['terminal_state'],
                        'interrupted_confirmed' if mode == 'cancel' else 'completed')
                    self.assertIsNone(service.active('conversation')['state']['inflight'])


if __name__ == '__main__': unittest.main()
