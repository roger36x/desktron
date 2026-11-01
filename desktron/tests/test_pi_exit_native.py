"""Real Pi JSONL survives an interrupted compaction; only loopback model I/O."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

import pytest
from engine_adapters.pi import PiAdapter
from test_pi_engine import Bus
from test_shepherd_image_execution import ImageModel


@pytest.mark.native
@unittest.skipUnless(shutil.which('pi'), 'installed Pi required')
class PiExitNative(unittest.TestCase):
    def test_pending_compaction_can_be_killed_then_resumed_without_replacing_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); agent = root/'agent'; agent.mkdir()
            (agent/'settings.json').write_text(json.dumps({'compaction':{'keepRecentTokens':64}}))
            model = ImageModel(); self.addCleanup(model.close)
            (agent/'models.json').write_text(json.dumps({'providers':{'fixture':{
                'baseUrl':model.url, 'api':'anthropic-messages', 'apiKey':'fixture',
                'models':[{'id':'fixture','name':'Fixture','reasoning':False,'input':['text'],
                    'contextWindow':200000,'maxTokens':1024,
                    'cost':{'input':0,'output':0,'cacheRead':0,'cacheWrite':0}}]}}}))
            env = {**os.environ, 'PI_CODING_AGENT_DIR':str(agent)}
            ad = PiAdapter(str(root/'state'), str(root), Bus())
            sid = ad.create_session()['id']; history = root/'session.jsonl'
            cmd = [shutil.which('pi'),'--mode','rpc','--offline','--no-extensions',
                   '--no-skills','--no-context-files','--no-tools','--no-approve',
                   '--provider','fixture','--model','fixture','--session',str(history)]
            def start():
                proc = ad._spawn_process(sid, cmd, cwd=root, env=env, text=True,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                events = queue.Queue()
                def read():
                    try:
                        for line in proc.stdout:
                            try: events.put(json.loads(line))
                            except ValueError: pass
                    finally: events.put(None)
                threading.Thread(target=read, daemon=True).start()
                return proc, events
            def send(proc, frame):
                proc.stdin.write(json.dumps(frame)+'\n'); proc.stdin.flush()
            def settled(events):
                end = time.monotonic()+25
                while time.monotonic() < end:
                    event = events.get(timeout=max(.01,end-time.monotonic()))
                    self.assertIsNotNone(event, 'Pi exited without settled boundary')
                    if event.get('type') == 'agent_settled': return
                self.fail('Pi did not settle')
            def stop(proc):
                proc.kill(); proc.wait(timeout=5)
                for stream in (proc.stdin,proc.stdout,proc.stderr): stream.close()
            proc, events = start()
            try:
                send(proc, {'type':'prompt','message':'Remember fixture-742. '+ 'archived detail ' * 1000})
                settled(events)
                send(proc, {'type':'prompt','message':'Say hello again. '+ 'recent detail ' * 100})
                settled(events)
            finally: stop(proc)
            before = history.read_bytes()
            self.assertIn(b'fixture-742', before)
            entered = threading.Event(); release = threading.Event()
            handler = model.server.RequestHandlerClass; original = handler.do_POST
            def pending(request):
                entered.set(); release.wait(15)
                try: original(request)
                except (BrokenPipeError, ConnectionResetError): pass
            handler.do_POST = pending
            proc, events = start()
            try:
                send(proc, {'type':'compact','id':'pending-summary'})
                reached = entered.wait(5)
                diagnostic = []
                while not events.empty(): diagnostic.append(events.get_nowait())
                self.assertTrue(reached, f'compaction never reached model: {diagnostic[-4:]}')
                # No summary response has arrived: terminate the entire owned
                # writer group as a quit fallback would, not a successful compact.
            finally:
                stop(proc); handler.do_POST = original; release.set()
            rows = [json.loads(line) for line in history.read_text().splitlines()]
            self.assertFalse(any(row.get('type')=='compaction' for row in rows))
            self.assertTrue(history.read_bytes().startswith(before))
            proc, events = start()
            try:
                send(proc, {'type':'prompt','message':'Continue after the interrupted compaction.'})
                settled(events)
            finally: stop(proc)
            self.assertTrue(history.read_bytes().startswith(before))
            self.assertEqual(model.tool_requests, 0)
