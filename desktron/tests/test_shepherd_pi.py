"""Pi 0.85.1 SessionManager connector tests; no model inference."""
import copy
import json
import os
import oscompat
import re
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron' / 'lib'))

from shepherd import Conflict, Coordinator, ReadProjector, Store, SwitchRunner
from shepherd.coordinator import digest
from shepherd.pi_codec import (CODEC_VERSION, HISTORY_PROJECTION_VERSION,
                                MAX_IMPORTED_CALL_ID_LENGTH, CodecError, encode_round)
from shepherd.pi_connector import PiConnector, pi_sdk_root
from shepherd.source_codec import pi_public
from shepherd.store import InvalidState


class PiFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.cwd = self.root / 'work'; self.cwd.mkdir()
        self.store = Store(self.root / 'state'); self.coordinator = Coordinator(self.store)
        self.cid = 'conversation_pi'; self.coordinator.create(self.cid)
        self.sid = '11111111-2222-4333-8444-555555555555'
        self.binding_id = self.coordinator.bind(self.cid, 'pi', self.sid,
            native_cwd=str(self.cwd), connector_version=CODEC_VERSION,
            binding_id='binding_pi', ownership_nonce='nonce_pi')
        self.binding = self.coordinator.state(self.cid)['bindings'][self.binding_id]
        self.sdk = pi_sdk_root(); self.node = shutil.which('node')
        if not self.sdk or not self.node: self.skipTest('Pi SDK and Node required')

    def connector(self):
        return PiConnector(self.binding, 'nonce_pi', store_root=self.root / 'state',
                           sdk_root=self.sdk, node_binary=self.node,
                           ownership=self.coordinator.ownership)

    def round(self, public, offset=1):
        return {'round_id': f'{self.cid}:{offset}', 'offset': offset,
                'entry_hash': str(offset) * 64, 'committed_at': 10, 'public': public}


class PiCodec(PiFixture):
    def test_plain_and_tool_messages_preserve_pairing(self):
        messages = encode_round(self.round([
            {'type':'message','role':'user','content':[{'type':'input_text','text':'question'}]},
            {'type':'function_call','call_id':'call_1','name':'read','arguments':'{"path":"/tmp/x"}'},
            {'type':'function_call_output','call_id':'call_1','output':'result'},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'done'}]},
        ]))
        self.assertEqual([row['role'] for row in messages], ['user','assistant','toolResult','assistant'])
        self.assertEqual(messages[1]['content'][0]['id'], messages[2]['toolCallId'])
        self.assertEqual(messages[1]['stopReason'], 'toolUse')

    def test_imported_provider_id_is_stably_aliased_and_round_trips(self):
        original=('call_3D7bUZ7eLFrQFWgz0UEJ4k4r|'
                  'fc_0af2bfaeb884fe00016aaa93b4c9b087d286275f76ef625fe1')
        public=[
            {'type':'function_call','call_id':original,'name':'read','arguments':'{}'},
            {'type':'function_call_output','call_id':original,'output':'ok'},
        ]
        row=self.round(public)
        first=encode_round(row);second=encode_round(row)
        marker=first[0]
        call=next(block for message in first if message.get('role')=='assistant'
                  for block in message.get('content') or [] if block.get('type')=='toolCall')
        result=next(message for message in first if message.get('role')=='toolResult')
        self.assertEqual(marker['customType'],'shepherd-tool-call-ids-v1')
        self.assertEqual(call['id'],result['toolCallId'])
        self.assertLessEqual(len(call['id']),MAX_IMPORTED_CALL_ID_LENGTH)
        self.assertRegex(call['id'],r'^[A-Za-z0-9_-]+$')
        self.assertNotEqual(call['id'],original)
        self.assertEqual(first,second)
        connector=self.connector();connector.append([row],'')
        ref={'kind':'native_round','engine':'pi','session_id':self.sid,
             'entry_id':row['round_id'],'source_key':f'pi:{self.sid}:{row["round_id"]}'}
        self.assertEqual(connector.resolve_source(ref)['public'],public)

    def test_custom_tool_alias_and_semantics_round_trip_together(self):
        original='custom/provider|'+('x'*80)
        public=[
            {'type':'custom_tool_call','call_id':original,'name':'functions.exec',
             'input':'text(1);'},
            {'type':'custom_tool_call_output','call_id':original,
             'output':'[工具执行失败] denied'},
        ]
        messages=encode_round(self.round(public))
        entries=[item if item.get('type')=='custom' else {'type':'message','message':item}
                 for item in messages]
        self.assertEqual(pi_public(entries,version=2),public)

    def test_imported_call_id_alias_collision_fails_closed(self):
        public=[
            {'type':'function_call','call_id':'first','name':'read','arguments':'{}'},
            {'type':'function_call_output','call_id':'first','output':'one'},
            {'type':'function_call','call_id':'second','name':'read','arguments':'{}'},
            {'type':'function_call_output','call_id':'second','output':'two'},
        ]
        with mock.patch('shepherd.tool_call_ids.protocol_call_id',return_value='call_same'), \
             self.assertRaisesRegex(CodecError,'归一化冲突'):
            encode_round(self.round(public))

    def test_invalid_or_unused_source_id_mapping_fails_closed(self):
        marker={'type':'custom','customType':'shepherd-tool-call-ids-v1',
                'data':{'ids':[{'native_id':'call_safe','public_id':'provider|id'}]}}
        with self.assertRaisesRegex(InvalidState,'映射未完整配对'):
            pi_public([marker],version=2)
        duplicate={'type':'custom','customType':'shepherd-tool-call-ids-v1','data':{'ids':[
            {'native_id':'same','public_id':'one'}, {'native_id':'same','public_id':'two'}]}}
        with self.assertRaisesRegex(InvalidState,'映射冲突'):
            pi_public([duplicate],version=2)
        self.assertEqual(HISTORY_PROJECTION_VERSION,'pi-history-v2')

    def test_user_only_confirmed_interrupt_gets_nonsemantic_persistence_boundary(self):
        messages = encode_round(self.round([
            {'type':'message','role':'user','content':[{'type':'input_text','text':'interrupted'}]}]))
        self.assertEqual([message['role'] for message in messages], ['user','assistant'])
        self.assertEqual(messages[1]['stopReason'], 'aborted')
        connector = self.connector(); connector.append([self.round([
            {'type':'message','role':'user','content':[{'type':'input_text','text':'interrupted'}]}])], '')
        self.assertEqual(connector.verify()['roles'], ['user','assistant'])

    def test_incomplete_tool_and_system_messages_fail_closed(self):
        with self.assertRaises(CodecError): encode_round(self.round([
            {'type':'function_call','call_id':'c','name':'bash','arguments':'{}'}]))
        with self.assertRaises(CodecError): encode_round(self.round([
            {'type':'message','role':'system','content':[{'type':'text','text':'x'}]}]))


class PiConnectorContract(PiFixture):
    def test_session_manager_processes_strip_legacy_web_search_key(self):
        connector = self.connector()
        dirty = {'PATH': os.environ.get('PATH', ''),
                 'VOLCENGINE_WEB_SEARCH_API_KEY': 'legacy-secret'}
        stream = mock.Mock()
        stream.events.return_value = iter([{
            'storeContract': 'desk-pi-store-v1', 'sessionId': self.sid,
            'sourceEntries': [],
        }])
        with mock.patch.dict(os.environ, dirty, clear=True), \
                mock.patch('shepherd.runner_process.NativeProcess', return_value=stream) as native:
            self.assertEqual(connector._run('resolve-batch', '-', str(self.root), None),
                             {'sources': [[]]})
        stream_env = native.call_args.kwargs['env']
        self.assertNotIn('VOLCENGINE_WEB_SEARCH_API_KEY', stream_env)

        completed = mock.Mock(returncode=0, stdout=json.dumps({
            'storeContract': 'desk-pi-store-v1', 'sessionId': self.sid,
        }))
        with mock.patch.dict(os.environ, dirty, clear=True), \
                mock.patch('shepherd.pi_connector.subprocess.run', return_value=completed) as run:
            connector._run('verify', '-', str(self.root), None)
        process_env = run.call_args.kwargs['env']
        self.assertNotIn('VOLCENGINE_WEB_SEARCH_API_KEY', process_env)

    def test_reasoning_marker_survives_sdk_without_entering_model_messages(self):
        connector = self.connector()
        public = [
            {'type':'reasoning','summary':[{'type':'summary_text','text':'inspect first'}]},
            {'type':'custom_tool_call','call_id':'c','name':'exec','input':'text(1);'},
            {'type':'custom_tool_call_output','call_id':'c','output':'[工具执行失败] denied'},
        ]
        row = self.round(public)
        result = connector.append([row], '')
        self.assertEqual(connector.append([row], '')['native_revision'], result['native_revision'])
        ref={'kind':'native_round','engine':'pi','session_id':self.sid,'entry_id':row['round_id'],
             'source_key':f'pi:{self.sid}:{row["round_id"]}'}
        self.assertEqual(connector.resolve_source(ref)['public'], public)
        report=connector.verify()
        self.assertEqual(report['messages'], 3)
        self.assertNotIn('custom', report['roles'])

    def test_append_reopen_idempotency_and_private_path(self):
        connector = self.connector()
        row = self.round([
            {'type':'message','role':'user','content':[{'type':'input_text','text':'seed-user'}]},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'seed-answer'}]},
        ])
        result = connector.append([row], '')
        self.assertEqual(result['native_revision'], connector.revision())
        self.assertEqual(connector.append([row], '')['native_revision'], result['native_revision'])
        report = connector.verify()
        self.assertEqual(report['sessionId'], self.sid)
        self.assertEqual(report['roles'], ['user','assistant'])
        if oscompat.IS_WINDOWS:
            self.assertFalse(oscompat.loose_mode(os.stat(connector.path)))
        else:
            self.assertEqual(os.stat(connector.path).st_mode & 0o777, 0o600)
        self.assertEqual(Path(connector.path).parent, self.root / 'state/native/pi')

    def test_resolve_source_round_trips_marker_delimited_entries(self):
        connector = self.connector()
        public = [
            {'type':'message','role':'user','content':[{'type':'input_text','text':'question'}]},
            {'type':'function_call','call_id':'call_1','name':'read','arguments':'{"path":"/tmp/x"}'},
            {'type':'function_call_output','call_id':'call_1','output':'result'},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'answer'}]},
        ]
        row = self.round(public)
        connector.append([row], '')
        ref = {'kind':'native_round','engine':'pi','session_id':self.sid,
               'entry_id':row['round_id'],
               'source_key':f'pi:{self.sid}:{row["round_id"]}'}
        resolved = connector.resolve_source(ref)
        self.assertEqual(resolved['public'], public)
        self.assertEqual(resolved['source']['entries'][0]['customType'], 'shepherd-round-start')
        self.assertEqual(resolved['source']['entries'][-1]['customType'], 'shepherd-round-commit')
        with self.assertRaises(Conflict): connector.resolve_source({**ref,'entry_id':'missing'})

    def test_tool_pair_reopens_without_execution(self):
        connector = self.connector()
        marker = self.root / 'must-not-exist'
        row = self.round([
            {'type':'message','role':'user','content':[{'type':'input_text','text':'seed-user'}]},
            {'type':'function_call','call_id':'call_1','name':'bash',
             'arguments':json.dumps({'command':f'touch {marker}'})},
            {'type':'function_call_output','call_id':'call_1','output':'historical-tool-result'},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'done'}]},
        ])
        connector.append([row], '')
        report = connector.verify()
        self.assertEqual(report['roles'], ['user','assistant','toolResult','assistant'])
        self.assertFalse(marker.exists())

    def test_checkpoint_is_typed_and_reopens_as_compaction_summary(self):
        connector = self.connector()
        checkpoint = {'checkpoint_id':'checkpoint_1','covers_through_offset':8,
                      'covers_hash':'a'*64,'estimated_tokens':100}
        result = connector.load_checkpoint({'summary':'portable-checkpoint'}, checkpoint=checkpoint,
                                           expected_revision='')
        self.assertEqual(result['native_revision'], connector.revision())
        report = connector.verify()
        self.assertEqual(report['roles'], ['compactionSummary'])
        self.assertEqual([row['customType'] for row in report['markers']], ['shepherd-checkpoint'])

    def test_recovery_ignores_only_nonsemantic_pi_entries_with_diagnostics(self):
        entries = [
            {'type':'message','message':{'role':'user','content':'new request'}},
            {'type':'compaction','summary':'derived prior context','tokensBefore':250000},
            {'type':'context_edit','targetId':'entry','replacement':None},
            {'type':'label','targetId':'entry','label':'bookmark'},
            {'type':'session_info','name':'renamed'},
            {'type':'model_change','provider':'example-gw','modelId':'model'},
            {'type':'thinking_level_change','thinkingLevel':'high'},
            {'type':'custom','customType':'loop-todo','data':{'todos':[]}},
            {'type':'custom','customType':'future-extension-state','data':{'nested':True}},
            {'type':'message','message':{'role':'assistant','content':[{'type':'text','text':'answer'}]}},
        ]
        diagnostics = PiConnector.source_compatibility(
            {'codec':CODEC_VERSION, 'entries':entries})
        self.assertEqual(diagnostics, [
            {'engine':'pi','action':'ignored_custom_state',
             'kind':'future-extension-state','count':1},
            {'engine':'pi','action':'ignored_custom_state','kind':'loop-todo','count':1},
            {'engine':'pi','action':'ignored_internal_entry','kind':'compaction','count':1},
            {'engine':'pi','action':'ignored_internal_entry','kind':'context_edit','count':1},
            {'engine':'pi','action':'ignored_internal_entry','kind':'label','count':1},
            {'engine':'pi','action':'ignored_internal_entry','kind':'model_change','count':1},
            {'engine':'pi','action':'ignored_internal_entry','kind':'session_info','count':1},
            {'engine':'pi','action':'ignored_internal_entry',
             'kind':'thinking_level_change','count':1},
        ])
        self.assertEqual([item['role'] for item in pi_public(entries, version=2,
                                                              allow_incomplete=True)],
                         ['user','assistant'])

    def test_legacy_projection_rejects_unknown_or_content_bearing_pi_entries(self):
        for entry in (
            {'type':'branch_summary','summary':'changes model context'},
            {'type':'custom_message','customType':'extension','content':'injected'},
            {'type':'future_delta','content':'possibly semantic'},
            {'type':'custom','customType':'','data':{}},
        ):
            with self.subTest(entry=entry), self.assertRaises(Conflict):
                PiConnector.source_compatibility(
                    {'codec':CODEC_VERSION, 'entries':[entry]}, version=2)

    def test_real_session_manager_recovers_compaction_and_context_edit_as_nonsemantic_evidence(self):
        connector = self.connector()
        connector.bootstrap()
        boundary = connector.recovery_boundary()
        lines = Path(connector.path).read_text().splitlines()
        parent = json.loads(lines[-1])['id']
        compaction = {'type':'compaction','id':'ca11ab1e','parentId':parent,
                      'timestamp':'2026-09-13T00:00:00.000Z',
                      'summary':'derived prior context','firstKeptEntryId':parent,
                      'tokensBefore':250000}
        edit = {'type':'context_edit','id':'ed17','parentId':'ca11ab1e',
                'timestamp':'2026-09-13T00:00:01.000Z','targetId':parent,
                'replacement':None}
        with open(connector.path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(compaction, ensure_ascii=False) + '\n')
            stream.write(json.dumps(edit, ensure_ascii=False) + '\n')
        recovered = connector.recover_suffix(boundary)
        self.assertEqual(recovered['source']['entries'], [compaction, edit])
        self.assertEqual(recovered['public'], [])
        self.assertEqual(recovered['compatibility'], [
            {'engine':'pi','action':'ignored_internal_entry','kind':'compaction','count':1},
            {'engine':'pi','action':'ignored_internal_entry','kind':'context_edit','count':1},
        ])

    def test_revision_conflict_and_ownership_failure_do_not_replace_file(self):
        connector = self.connector()
        row = self.round([{'type':'message','role':'user','content':[{'type':'input_text','text':'q'}]},
                          {'type':'message','role':'assistant','content':[{'type':'output_text','text':'a'}]}])
        connector.append([row], ''); before = Path(connector.path).read_bytes()
        other = self.round([{'type':'message','role':'user','content':[{'type':'input_text','text':'other'}]},
                            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'x'}]}], 2)
        with self.assertRaises(Conflict): connector.append([other], '')
        self.assertEqual(before, Path(connector.path).read_bytes())
        Path(self.coordinator.ownership.path(self.binding_id)).unlink()
        with self.assertRaises(Conflict): connector.append([other], connector.revision())
        self.assertEqual(before, Path(connector.path).read_bytes())

    def test_concurrent_same_revision_allows_only_one_distinct_round(self):
        connector = self.connector(); results, errors = [], []
        def run(index):
            row = self.round([{'type':'message','role':'user','content':[{'type':'input_text','text':str(index)}]},
                              {'type':'message','role':'assistant','content':[{'type':'output_text','text':'ok'}]}], index)
            try: results.append(connector.append([row], ''))
            except Exception as exc: errors.append(exc)
        threads = [threading.Thread(target=run,args=(n,)) for n in (1,2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(results), 1); self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], Conflict)
        self.assertEqual(connector.verify()['roles'], ['user','assistant'])

    def test_capabilities_and_binding_are_gated(self):
        self.assertTrue(PiConnector.availability(self.sdk, self.node)['available'])
        fake = self.root / 'fake'; fake.mkdir(); (fake/'package.json').write_text('{"version":"0.85.2"}')
        self.assertEqual(PiConnector.availability(fake, self.node)['reason'], 'pi_protocol_unverified')
        bad = {**self.binding,'native_cwd':str(self.root/'other')}
        with self.assertRaises(Conflict):
            PiConnector(bad,'nonce_pi',store_root=self.root/'state',sdk_root=self.sdk,
                        node_binary=self.node,ownership=self.coordinator.ownership)

    def test_switch_runner_materializes_and_activates_pi(self):
        # Recreate conversation with Claude as old producer and Pi as empty target.
        old = self.coordinator.bind(self.cid,'claude','native_claude',native_cwd=str(self.cwd),
            connector_version='claude-v1',binding_id='binding_claude',ownership_nonce='nonce_claude')
        lease = self.coordinator.acquire(self.cid,old,ownership_nonce='nonce_claude')
        source = {'kind':'native_round','engine':'claude','session_id':'native_claude',
                  'entry_id':'turn_1','source_key':'claude:native_claude:turn_1'}
        native={'native':'one'}; public=[
            {'type':'message','role':'user','content':[{'type':'input_text','text':'question'}]},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'answer'}]}]
        self.coordinator.commit(self.cid,lease['lease_id'],lease['generation'],source=source,
            source_hash=digest(native),public_hash=digest(public),estimated_tokens=20)
        class Resolver:
            def resolve(self,_source): return {'source':native,'public':public}
        connector=self.connector()
        next_lease=SwitchRunner(self.coordinator,ReadProjector(self.store,Resolver()),connector).switch(
            self.cid,self.binding_id,ownership_nonce='nonce_pi',lease_id=lease['lease_id'],
            generation=lease['generation'])
        state=self.coordinator.state(self.cid)
        self.assertEqual(next_lease['binding_id'],self.binding_id)
        self.assertEqual(state['bindings'][self.binding_id]['applied_offset'],1)
        self.assertEqual(connector.verify()['roles'],['user','assistant'])


if __name__ == '__main__': unittest.main()
