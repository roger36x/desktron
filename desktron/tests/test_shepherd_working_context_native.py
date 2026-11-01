"""Installed Codex-family loops, loopback model, private disposable state only."""
import json
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import pytest
import codex_appserver as app
from shepherd.service import Service
from shepherd.harness import ExecutionHarness
from shepherd.unified_runners import UnifiedRunner
from shepherd.codex_connector import CodexFamilyConnector
from test_shepherd_image_execution import ImageModel


@pytest.mark.native
class NativeWorkingContext(unittest.TestCase):
    def test_compaction_survives_disposable_codex_process(self):
        self.check_engine('codex')

    def test_codex_repeated_model_switch_does_not_expand_compacted_history(self):
        self.check_engine('codex',models=('gpt-5.6','gpt-6-astra','gpt-5.6','gpt-6-astra'))

    def test_codex_snapshot_failure_rebuilds_without_replaying_completed_turn(self):
        self.check_engine('codex', inject_cache_failure=True)

    def test_snapshot_persistence_failures_close_writer_but_keep_rollout_and_blobs(self):
        for stage in ('raw', 'expanded', 'journal'):
            with self.subTest(stage=stage):
                self.check_engine('codex', inject_cache_failure=True, evidence_failure=stage)

    def check_engine(self, engine, models=('gpt-5.6','gpt-5.6'), inject_cache_failure=False,
                     evidence_failure=None):
        self.addCleanup(app._drop_all_sessions)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); work=root/'work'; work.mkdir()
            model=ImageModel(); self.addCleanup(model.close)
            # The native client may choose the Responses compact endpoint.
            handler=model.server.RequestHandlerClass
            def post(request):
                body=json.loads(request.rfile.read(int(request.headers.get('Content-Length') or 0)))
                model.requests.append(body)
                if request.path.endswith('/responses/compact'):
                    raw=json.dumps({'id':'cmp_fixture','object':'response.compaction','created_at':1,
                        'output':[{'type':'compaction','encrypted_content':'fixture-opaque-context'}]}).encode()
                    request.send_response(200);request.send_header('Content-Type','application/json')
                else:
                    answer='old-large-assistant-context '*1500 if len(model.requests)==1 else 'native summary or answer'
                    item={'type':'message','id':'msg_fixture','role':'assistant','status':'completed',
                          'content':[{'type':'output_text','text':answer,'annotations':[]}]}
                    response={'id':'resp_fixture','object':'response','status':'completed','output':[item],
                              'usage':{'input_tokens':100,'output_tokens':100,'total_tokens':200}}
                    events=[{'type':'response.created','response':{**response,'status':'in_progress','output':[]}},
                            {'type':'response.output_item.added','output_index':0,'item':item},
                            {'type':'response.output_item.done','output_index':0,'item':item},
                            {'type':'response.completed','response':response}]
                    raw=''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
                    request.send_response(200);request.send_header('Content-Type','text/event-stream')
                request.send_header('Content-Length',str(len(raw)));request.end_headers()
                request.wfile.write(raw)
            handler.do_POST=post
            def argv(binary,opts=None):
                return [binary,'-c','model_provider="fixture"','-c','model="gpt-5.6"',
                    '-c','model_providers.fixture.name="Fixture"',
                    '-c',f'model_providers.fixture.base_url="{model.url}/v1"',
                    '-c','model_providers.fixture.wire_api="responses"',
                    '-c','model_providers.fixture.requires_openai_auth=false',
                    *(['app-server','--stdio'] if os.path.basename(binary)=='codex'
                      else ['app-server','--listen','stdio://'])]
            original=app.run_turn
            compacted=[]
            cache_injected=[]
            native_turns=[]
            def compact_after_first(*args,**kwargs):
                started=kwargs['on_turn_started']
                def turn_started(thread_id, turn_id):
                    native_turns.append((thread_id, turn_id))
                    return started(thread_id, turn_id)
                kwargs['on_turn_started']=turn_started
                attached={}
                callback=kwargs['on_connection']; event=kwargs['on_notification']
                def connection(conn):
                    attached['conn']=conn; callback(conn)
                def notification(method,params):
                    event(method,params)
                    if method!='turn/completed' or compacted:return
                    compacted.append(True)
                    conn=attached['conn']
                    done=[]
                    def compact_event(method,params):
                        if (method=='thread/compacted' or
                                (method=='item/completed' and (params.get('item') or {}).get('type')=='contextCompaction')):
                            done.append(True)
                    conn.request('thread/compact/start',{'threadId':params['threadId']},
                                 compact_event,overall=30)
                    while not done:
                        msg=conn.read(30)
                        compact_event(msg.get('method'),msg.get('params') or {})
                snapshot=kwargs['on_working_context']
                def export(thread_id, path):
                    if inject_cache_failure and not cache_injected:
                        # Fault injection only after the real native terminal and
                        # read barrier. The ordinary runner must retain the exact
                        # rejected bytes before destroying this private HOME.
                        blob = b'fixture external evidence must survive cleanup'
                        sha = hashlib.sha256(blob).hexdigest()
                        target = Path(path).with_suffix('.artifacts') / 'rollout-blobs' / 'sha256' / sha[:2] / sha
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(blob)
                        notice = {'type': 'fixture_new_metadata', 'payload': {
                            'text': f'fixture-rollout-blob:v1:sha256:{sha}:{len(blob)}'}}
                        with open(path, 'ab') as stream:
                            stream.write((json.dumps(notice) + '\n').encode())
                            stream.flush(); os.fsync(stream.fileno())
                        cache_injected.append(True)
                    return snapshot(thread_id, path)
                kwargs.update(on_connection=connection,on_notification=notification,on_working_context=export)
                return original(*args,**kwargs)
            opts={'model':'gpt-5.6','sandbox':'read-only','_context_window':1000000};pids=[];processes=[]
            with mock.patch.object(app,'_appserver_argv',side_effect=argv), \
                 mock.patch.object(CodexFamilyConnector,'_seed_private_config'), \
                 mock.patch.object(app,'catalog',return_value={
                     'models':[{'id':name,'model':name} for name in dict.fromkeys(models)],'modes':[]}), \
                 mock.patch.object(app,'run_turn',side_effect=compact_after_first):
                service=Service(root/'state',storage='unified')
                service.create('conversation',engine,work)
                for seq,selected in enumerate(models,1):
                    grounding=f'fixture-current-grounding-{engine}'
                    opts={**opts,'model':selected,'_grounding':grounding}
                    service=Service(root/'state',storage='unified')
                    active=service.active('conversation');c=service.connector(active['binding'])
                    runner=UnifiedRunner(c,options=opts,system_text=grounding,timeout=45,
                                         on_process=lambda process:(pids.append(process.pid), processes.append(process)))
                    prompt='first question' if seq==1 else 'new question after compression'
                    start=len(model.requests)
                    if evidence_failure:
                        save = c.archive.retain_context_evidence
                        append = c.archive.append
                        attempts = []
                        def fail_save(*args):
                            attempts.append(True)
                            if len(attempts) == (2 if evidence_failure == 'expanded' else 1) and evidence_failure != 'journal':
                                raise OSError('fixture disk full')
                            return save(*args)
                        def fail_append(*args):
                            if evidence_failure == 'journal' and args[-1].get('context_recovery'):
                                raise OSError('fixture disk full')
                            return append(*args)
                        with mock.patch.object(c.archive, 'retain_context_evidence', side_effect=fail_save), \
                             mock.patch.object(c.archive, 'append', side_effect=fail_append), \
                             self.assertRaisesRegex(OSError, 'fixture disk full'):
                            ExecutionHarness(service.coordinator,c,runner).run('conversation',
                                lease_id=active['lease']['lease_id'], generation=active['lease']['generation'],
                                prompt=prompt)
                        retained = list(c.archive.directory.glob('native-evidence-*'))
                        self.assertEqual(len(retained), 1)
                        rollouts = list(retained[0].rglob('*.jsonl'))
                        self.assertTrue(any(b'fixture_new_metadata' in path.read_bytes() for path in rollouts))
                        self.assertTrue(any(path.read_bytes() == b'fixture external evidence must survive cleanup'
                                            for path in retained[0].rglob('*') if path.is_file()))
                        self.assertNotIn('shepherd:' + c.binding_id, app._SESSIONS)
                        self.assertTrue(processes)
                        self.assertTrue(all(process.poll() is not None for process in processes))
                        self.assertEqual(len(native_turns), 1)
                        return
                    row=ExecutionHarness(service.coordinator,c,runner).run('conversation',
                        lease_id=active['lease']['lease_id'],generation=active['lease']['generation'],prompt=prompt,
                        input_public=[{'type':'message','role':'user','content':[{'type':'input_text','text':prompt}]}])
                    self.assertEqual(row['terminal_state'],'completed')
                    if inject_cache_failure and seq == 1:
                        self.assertIsNone(runner.checkpoint)
                        evidence = runner.history_recovery['evidence']
                        preserved = c.archive.directory / evidence['file']
                        self.assertIn(b'fixture_new_metadata', preserved.read_bytes())
                    else:
                        self.assertIsNotNone(runner.checkpoint)
                        self.assertEqual(runner.checkpoint.get('model'),selected)
                    self.assertEqual(list((root/'state/execution').glob('turn-*')),[])
                    self.assertIn(grounding,json.dumps(model.requests[start:]))
                    if seq>=2 and not inject_cache_failure:
                        body=json.dumps(model.requests[start:])
                        self.assertIn('new question after compression',body)
                        self.assertFalse('old-large-assistant-context '*100 in body,
                                         'Compacted assistant history was expanded again')
                if inject_cache_failure:
                    # Compaction can make multiple model subrequests; count
                    # dispatched native turns, not provider request totals.
                    self.assertEqual(len(native_turns), len(models), 'must not replay completed native turn')
                    self.assertIn(b'fixture_new_metadata', preserved.read_bytes())
                    self.assertIsNone(service.coordinator.state('conversation')['inflight'])
                elif len(set(models)) == 1:
                    self.assertEqual(len(set(pids)),1,'normal turns must reuse one app-server process')
                app._drop_all_sessions()
