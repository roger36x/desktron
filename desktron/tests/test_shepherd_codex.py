"""Codex persistent-thread connector tests with loopback model metadata."""
import copy
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron' / 'lib'))

import codex_appserver as app
from shepherd import (CodexFamilyRunner, Conflict, Coordinator, ExecutionHarness,
                      ReadProjector, Store, SwitchRunner)
from shepherd.codex_codec import (CODEC_VERSIONS, HISTORY_PROJECTION_VERSION,
                                  CodecError, encode_round, normalize_call_ids)
from shepherd.codex_connector import CodexFamilyConnector
from shepherd.coordinator import digest
from shepherd.portable_checkpoint import derive
from shepherd.source_codec import codex_public
from shepherd.service import Service
from shepherd.store import InvalidState
from shepherd.tool_call_ids import history_projection_version
from shepherd.unified_runners import UnifiedRunner


class Loopback:
    def __init__(self):
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), self.handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
    def handler(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def do_POST(self):
                raw = b'{"input_tokens":10}' if 'count_tokens' in self.path else b'{}'
                self.send_response(200); self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
        return Handler
    @property
    def url(self): return f'http://127.0.0.1:{self.server.server_port}/v1'
    def close(self): self.server.shutdown(); self.server.server_close()


class CodexFamilyFixture(unittest.TestCase):
    engine = 'codex'
    def setUp(self):
        if not shutil.which(self.engine): self.skipTest(self.engine + ' missing')
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.cwd=self.root/'work';self.cwd.mkdir()
        self.state=Store(self.root/'state');self.coordinator=Coordinator(self.state)
        self.cid='conversation_'+self.engine;self.coordinator.create(self.cid)
        self.old=self.coordinator.bind(self.cid,'pi','native_pi',native_cwd=str(self.cwd),
            connector_version='pi-v1',binding_id='binding_old',ownership_nonce='nonce_old')
        self.lease=self.coordinator.acquire(self.cid,self.old,ownership_nonce='nonce_old')
        self.loop=Loopback();self.addCleanup(self.loop.close)
        original=app._appserver_argv
        def argv(binary,opts=None):
            return [binary,'-c','model_provider="fixture"','-c','model="gpt-5.6"',
                '-c','model_providers.fixture.name="Local fixture"',
                '-c',f'model_providers.fixture.base_url="{self.loop.url}"',
                '-c','model_providers.fixture.wire_api="responses"',
                '-c','model_providers.fixture.requires_openai_auth=false',
                *(['app-server','--stdio'] if os.path.basename(binary)=='codex' else ['app-server','--listen','stdio://'])]
        self.patch=mock.patch.object(app,'_appserver_argv',side_effect=argv);self.patch.start();self.addCleanup(self.patch.stop)
        config_patch=mock.patch.object(CodexFamilyConnector,'_seed_private_config')
        config_patch.start();self.addCleanup(config_patch.stop)
        self.binding_id='binding_'+self.engine
        created=CodexFamilyConnector.create_native(self.engine,str(self.cwd),store_root=self.state.root,
            binding_id=self.binding_id,binary=shutil.which(self.engine),model='gpt-5.6')
        self.coordinator.bind(self.cid,self.engine,created['native_session_id'],native_cwd=str(self.cwd),
            connector_version=created['connector_version'],binding_id=self.binding_id,
            ownership_nonce='nonce_target',native_revision=created['native_revision'])
        self.binding=self.coordinator.state(self.cid)['bindings'][self.binding_id]
    def connector(self):
        return CodexFamilyConnector(self.binding,'nonce_target',store_root=self.state.root,
            binary=shutil.which(self.engine),ownership=self.coordinator.ownership)
    def public(self):
        marker = self.root / 'must-not-execute'
        return [
          {'type':'message','role':'user','content':[{'type':'input_text','text':'seed-user'}]},
          {'type':'function_call','call_id':'call_1','name':'bash',
           'arguments':json.dumps({'command':f'touch {marker}'})},
          {'type':'function_call_output','call_id':'call_1','output':'historical-result'},
          {'type':'message','role':'assistant','content':[{'type':'output_text','text':'seed-answer'}]},]
    def commit(self):
        native={'source':'one'};public=self.public();src={'kind':'native_round','engine':'pi',
          'session_id':'native_pi','entry_id':'turn_1','source_key':'pi:native_pi:turn_1'}
        row=self.coordinator.commit(self.cid,self.lease['lease_id'],self.lease['generation'],source=src,
          source_hash=digest(native),public_hash=digest(public),estimated_tokens=30)
        return row,native,public


class CodexCodec(unittest.TestCase):
    def test_open_registers_and_stops_writer_when_initialize_fails(self):
        connector = object.__new__(CodexFamilyConnector)
        connector.native_cwd = '/tmp'
        connector.engine = 'codex'
        connector.home = '/tmp/private-home'
        connector.binary = '/tmp/codex'
        process = mock.Mock()
        connection = mock.Mock(proc=process)
        connection.initialize.side_effect = RuntimeError('initialize failed')
        events = []
        def connect(*_args, **kwargs):
            kwargs['on_writer'](process, 'active')
            return connection
        connector._connection = mock.Mock(side_effect=connect)
        connector._env = mock.Mock(return_value={})
        with self.assertRaisesRegex(RuntimeError, 'initialize failed'):
            connector._open(on_writer=lambda proc, status: events.append((proc, status)))
        self.assertEqual(events, [(process, 'active'), (process, 'stopped')])
        connection.close.assert_called_once_with()

    def test_open_closes_spawned_process_when_writer_registration_fails(self):
        connector = object.__new__(CodexFamilyConnector)
        connector.native_cwd = '/tmp'
        connector.engine = 'codex'
        connector.home = '/tmp/private-home'
        connector.binary = '/tmp/codex'
        process = mock.Mock()
        connection = mock.Mock(proc=process)
        events = []
        def connect(*_args, **kwargs):
            callback = kwargs['on_writer']
            try: callback(process, 'active')
            except Exception:
                connection.close()
                callback(process, 'stopped')
                raise
        connector._connection = mock.Mock(side_effect=connect)
        connector._env = mock.Mock(return_value={})
        def register(proc, status):
            events.append((proc, status))
            if status == 'active':
                raise RuntimeError('durable claim failed')
        with self.assertRaisesRegex(RuntimeError, 'durable claim failed'):
            connector._open(on_writer=register)
        connection.close.assert_called_once_with()
        self.assertEqual(events, [(process, 'active'), (process, 'stopped')])
        connection.initialize.assert_not_called()

    def test_compaction_replace_does_not_duplicate_authoritative_markers(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rollout.jsonl'
            row = {'round_id':'conversation:1','offset':1,'entry_hash':'a'*64,
                   'public':[{'type':'message','role':'user','content':[
                       {'type':'input_text','text':'original'}]}]}
            original = encode_round(row)[0]
            compacted = copy.deepcopy(original)
            compacted['id'] = 'msg_regenerated_by_compaction'
            path.write_text('\n'.join(json.dumps(record) for record in [
                {'type':'history_mutation','payload':{'operation':'append','items':[original]}},
                {'type':'history_mutation','payload':{'operation':'replace','items':[compacted]}},
            ]))
            markers = CodexFamilyConnector._markers(path)
            self.assertEqual(markers['1']['count'], 1)
            self.assertEqual(markers['1']['items'][0], original)
            self.assertEqual(CodexFamilyConnector._turn_items(path,
                original['internal_chat_message_metadata_passthrough']['turn_id']), [original])
            revision = CodexFamilyConnector._revision(path)
            path.write_text(path.read_text() + '\n' + json.dumps(
                {'type':'history_mutation','payload':{'operation':'replace','items':[compacted]}}))
            self.assertEqual(CodexFamilyConnector._revision(path), revision)

    def test_empty_binding_proof_rejects_foreign_bootstrap_and_user_items(self):
        auto=lambda role,kinds:{'type':'message','role':role,'content':[],
            'internal_chat_message_metadata_passthrough':{
                'turn_id':'auto-compact-0','content_item_kinds':list(kinds)}}
        bootstrap={'type':'message','role':'assistant','phase':'commentary','content':[],
            'internal_chat_message_metadata_passthrough':{
                'turn_id':'shepherd-bootstrap:v1:binding_codex','content_item_kinds':[]}}
        valid=[auto('developer',('host_skills.instructions','permissions.instructions')),
               auto('user',('environments.environment_context',)),bootstrap]
        self.assertTrue(CodexFamilyConnector._empty_binding_items(valid,'binding_codex'))
        for changed in (
            [*valid[:2],{**bootstrap,'internal_chat_message_metadata_passthrough':{
                'turn_id':'shepherd-bootstrap:v1:other','content_item_kinds':[]}}],
            [*valid,{'type':'message','role':'user','content':[],
                'internal_chat_message_metadata_passthrough':{'turn_id':'old-request'}}],
            valid[:2],
        ):
            with self.subTest(changed=changed):
                self.assertFalse(CodexFamilyConnector._empty_binding_items(changed,'binding_codex'))

    def test_duplicate_marker_in_authoritative_append_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rollout.jsonl'
            row = {'round_id':'conversation:1','offset':1,'entry_hash':'a'*64,
                   'public':[{'type':'message','role':'user','content':[
                       {'type':'input_text','text':'original'}]}]}
            item = encode_round(row)[0]
            path.write_text(json.dumps({'type':'history_mutation','payload':{
                'operation':'append','items':[item,copy.deepcopy(item)]}}))
            with self.assertRaisesRegex(Conflict, '冲突或重复'):
                CodexFamilyConnector._markers(path)

    def test_marker_is_protocol_field_and_tools_are_paired(self):
        row={'round_id':'conversation:1','offset':1,'entry_hash':'a'*64,'public':[
          {'type':'function_call','call_id':'call_1','name':'read','arguments':'{}'},
          {'type':'function_call_output','call_id':'call_1','output':'ok'}]}
        items=encode_round(row)
        self.assertEqual([x['type'] for x in items],['function_call','function_call_output'])
        self.assertTrue(all(set(x).issubset({'type','id','call_id','name','arguments','output',
          'internal_chat_message_metadata_passthrough'}) for x in items))
        self.assertTrue(all(x['internal_chat_message_metadata_passthrough']['turn_id'].startswith('shepherd:v1:') for x in items))
        with self.assertRaises(CodecError): encode_round({**row,'public':row['public'][:1]})

    def test_oversized_call_id_is_stably_remapped_with_its_output(self):
        # Pi's OpenAI subscription adapter can preserve both provider IDs as one
        # 83-character value, while Responses input accepts at most 64.
        original=('call_3D7bUZ7eLFrQFWgz0UEJ4k4r|'
                  'fc_0af2bfaeb884fe00016aaa93b4c9b087d286275f76ef625fe1')
        row={'round_id':'conversation:1','offset':1,'entry_hash':'a'*64,'public':[
          {'type':'function_call','call_id':original,'name':'bash','arguments':'{}'},
          {'type':'function_call_output','call_id':original,'output':'historical result'}]}
        first=encode_round(row);second=encode_round(row)
        ids=[item['call_id'] for item in first]
        self.assertEqual(ids[0],ids[1])
        self.assertEqual(len(ids[0]),64)
        self.assertTrue(ids[0].startswith('call_'))
        self.assertEqual(ids,[item['call_id'] for item in second])
        self.assertNotIn(original,json.dumps(first))

    def test_short_provider_specific_call_id_is_also_mapped(self):
        original='provider|item/with+punctuation'
        row={'round_id':'conversation:1','offset':1,'entry_hash':'a'*64,'public':[
          {'type':'function_call','call_id':original,'name':'read','arguments':'{}'},
          {'type':'function_call_output','call_id':original,'output':'ok'}]}
        ids=[item['call_id'] for item in encode_round(row)]
        self.assertEqual(ids[0],ids[1])
        self.assertRegex(ids[0],r'^call_[0-9a-f]{59}$')

    def test_native_checkpoint_call_ids_use_the_same_boundary_mapping(self):
        original='provider-call|'+'x'*80
        items=[{'type':'function_call','call_id':original,'name':'read','arguments':'{}'},
               {'type':'function_call_output','call_id':original,'output':'ok'}]
        normalized=normalize_call_ids(items)
        self.assertEqual(normalized[0]['call_id'],normalized[1]['call_id'])
        self.assertEqual(len(normalized[0]['call_id']),64)
        self.assertEqual(items[0]['call_id'],original)

    def test_valid_call_id_is_not_rewritten(self):
        original='call_'+'x'*59
        row={'round_id':'conversation:1','offset':1,'entry_hash':'a'*64,'public':[
          {'type':'custom_tool_call','call_id':original,'name':'exec','input':'code'},
          {'type':'custom_tool_call_output','call_id':original,'output':'ok'}]}
        self.assertEqual([item['call_id'] for item in encode_round(row)],
                         [original,original])

    def test_call_id_normalization_collision_fails_closed(self):
        row={'round_id':'conversation:1','offset':1,'entry_hash':'a'*64,'public':[
          {'type':'function_call','call_id':'first','name':'read','arguments':'{}'},
          {'type':'function_call_output','call_id':'first','output':'one'},
          {'type':'function_call','call_id':'second','name':'read','arguments':'{}'},
          {'type':'function_call_output','call_id':'second','output':'two'}]}
        with mock.patch('shepherd.codex_codec._native_call_id',return_value='call_same'), \
             self.assertRaisesRegex(CodecError,'归一化冲突'):
            encode_round(row)

    def test_codex_working_context_key_includes_history_projection_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);service=Service(root,storage='unified')
            service.create('conversation','codex',root)
            connector=service.connector(service.active('conversation')['binding'])
            key,checkpoint,rounds=connector.context({'model':'gpt-5.6-sol','provider':'codex'})
            expected=digest({'engine':'codex','codec':CODEC_VERSIONS['codex'],
                'cwd':connector.native_cwd,'model':'gpt-5.6-sol','provider':'codex',
                'history_projection':HISTORY_PROJECTION_VERSION})
            old=digest({'engine':'codex','codec':CODEC_VERSIONS['codex'],
                'cwd':connector.native_cwd,'model':'gpt-5.6-sol','provider':'codex'})
            self.assertEqual(key,expected)
            self.assertNotEqual(key,old)
            self.assertIsNone(checkpoint)
            self.assertEqual(rounds,[])

    def test_pi_working_context_key_rotates_unsafe_import_projection(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);service=Service(root,storage='unified')
            service.create('conversation','pi',root)
            connector=service.connector(service.active('conversation')['binding'])
            key,checkpoint,rounds=connector.context({'model':'gpt-5.4','provider':'openai'})
            expected=digest({'engine':'pi','codec':connector.native_codec,
                'cwd':connector.native_cwd,'model':'gpt-5.4','provider':'openai',
                'history_projection':history_projection_version('pi')})
            self.assertEqual(key,expected)
            self.assertIsNone(checkpoint)
            self.assertEqual(rounds,[])

    def test_unified_runner_archives_workspace_routing_retry_diagnostics(self):
        connector=mock.Mock()
        connector.binding={'binding_id':'binding','conversation_id':'conversation'}
        connector.engine='codex';connector.native_cwd='/workspace/domain'
        connector.native_session_id='native-session';connector.store_root='/tmp/shepherd-store'
        runner=UnifiedRunner(connector,runtime_roots=['/repo/a','/repo/b'])
        runner.round_id='conversation:1'
        runner._archive_workspace_routing_retry({
            'attempt':1,'max':1,'delay_ms':2000,
            'signal':'turn.workspaceRoutingDiscovery',
            'native_error':{'message':'workspace routing discovery failed',
                            'codexErrorInfo':'other',
                            'additionalDetails':'backend unavailable'}},
            {'thread_id':'thread-native','turn_id':'turn-native'})
        connector.archive.append.assert_called_once_with(
            connector.binding,'conversation:1',{'type':'runtime',
            'workspace_routing_retry':{
                'attempt':1,'max':1,'delay_ms':2000,
                'signal':'turn.workspaceRoutingDiscovery','cwd':'/workspace/domain',
                'runtime_workspace_roots':['/repo/a','/repo/b'],
                'thread_id':'thread-native','turn_id':'turn-native',
                'native_error':{'message':'workspace routing discovery failed',
                                'codexErrorInfo':'other',
                                'additionalDetails':'backend unavailable'}}})

    def test_unresolved_codex_context_key_also_rotates_old_resident(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);service=Service(root,storage='unified')
            service.create('conversation','codex',root)
            connector=service.connector(service.active('conversation')['binding'])
            runner=UnifiedRunner(connector,options={'_context_window':936000})
            with mock.patch.object(runner,'_codex',side_effect=Conflict('native boundary')), \
                 self.assertRaisesRegex(Conflict,'native boundary'):
                runner.run(prompt='hello',round_id='conversation:1',expected_offset=1,
                           on_dispatched=lambda **_kwargs: None)
            expected=digest({'engine':'codex','cwd':connector.native_cwd,'model':'',
                'provider':'','unresolved':True,
                'history_projection':HISTORY_PROJECTION_VERSION})
            self.assertEqual(runner._resident_context_key,expected)


class CodexConnectorCases:
    def test_batch_read_matches_individual_native_sources(self):
        row, _native, public = self.commit()
        connector = self.connector()
        second = {**row, 'offset': 2, 'round_id': self.cid + ':2',
                  'entry_hash': digest('second'), 'public': [public[0], public[-1]]}
        connector.append([{**row, 'public': public}, second], self.binding['native_revision'])
        refs = [{'kind': 'native_round', 'engine': self.engine,
                 'session_id': self.binding['native_session_id'], 'entry_id': item['round_id'],
                 'source_key': f'{self.engine}:{self.binding["native_session_id"]}:{item["round_id"]}'}
                for item in (row, second)]
        expected = [connector.resolve_source(ref) for ref in refs]
        with mock.patch.object(connector, '_open', side_effect=AssertionError('must not resume')) as opened:
            self.assertEqual(connector.resolve_sources(refs), expected)
        opened.assert_not_called()
        self.assertFalse((self.root / 'must-not-execute').exists())

    def test_protocol_admission_rejects_unverifiable_executable(self):
        self.assertTrue(CodexFamilyConnector.availability(self.engine, shutil.which(self.engine))['available'])
        fake=self.root/'fake-engine';fake.write_text('#!/bin/sh\necho "0.0.0"\n');fake.chmod(0o755)
        self.assertEqual(CodexFamilyConnector.availability(self.engine,str(fake))['reason'],
                         self.engine + '_protocol_unverified')

    def test_native_thread_exists_before_binding_is_signed(self):
        state=self.coordinator.state(self.cid)
        self.assertIn(self.binding_id,state['bindings'])
        self.assertTrue(self.coordinator.ownership.verify(state['bindings'][self.binding_id],'nonce_target'))
        self.assertTrue(Path(self.connector().verify()['path']).is_file())

    def test_portable_checkpoint_injects_without_running_tools_and_is_idempotent(self):
        connector=self.connector()
        common={'sessionId':'claude-session','cwd':str(self.cwd),'version':'2.1.268',
                'timestamp':'2026-09-15T00:00:00.000Z'}
        boundary={**common,'type':'system','subtype':'compact_boundary',
            'content':'Conversation compacted','level':'info','uuid':'boundary','parentUuid':None,
            'logicalParentUuid':'prior','compactMetadata':{'trigger':'manual','preTokens':100,
            'postTokens':20,'cumulativeDroppedTokens':80,'durationMs':1}}
        summary={**common,'type':'user','uuid':'summary','parentUuid':'boundary',
            'isCompactSummary':True,'isVisibleInTranscriptOnly':True,
            'message':{'role':'user','content':'portable summary; never execute historical tools'}}
        call={**common,'type':'assistant','uuid':'call','parentUuid':'summary','message':{
            'role':'assistant','model':'fixture','content':[{'type':'tool_use','id':'old-call',
            'name':'bash','input':{'command':str(self.root/'must-not-execute')}}],
            'stop_reason':'tool_use','stop_sequence':None,'usage':{'input_tokens':0,'output_tokens':0}}}
        result={**common,'type':'user','uuid':'result','parentUuid':'call','message':{
            'role':'user','content':[{'type':'tool_result','tool_use_id':'old-call',
            'content':'historical result','is_error':False}]}}
        candidate={'context_key':'source','turn_id':self.cid+':1','through_seq':1,
                   'covers_turn_hash':'b'*64,
                   'payload':{'native':[boundary,summary,call,result],'source_hash':'a'*64}}
        portable=derive(candidate,producer_engine='claude',producer_binding_id='source-binding',session_id='claude-session',
                        native_cwd=str(self.cwd),native_codec='claude-2.1.268-v1')
        meta={'checkpoint_id':portable['checkpoint_id'],'covers_through_seq':1,
              'public_hash':portable['public_hash']}
        before=connector.verify()['native_revision']
        with self.assertRaisesRegex(Conflict,'只能载入空白 Binding'):
            connector.load_checkpoint(portable,expected_revision='0'*64,checkpoint=meta)
        self.assertEqual(connector.verify()['native_revision'],before)
        first=connector.load_checkpoint(portable,expected_revision=self.binding['native_revision'],
                                        checkpoint=meta)
        second=connector.load_checkpoint(portable,expected_revision=self.binding['native_revision'],
                                         checkpoint=meta)
        self.assertEqual(first,second)
        report=connector.verify()
        self.assertEqual(report['native_revision'],first['native_revision'])
        self.assertFalse((self.root/'must-not-execute').exists())
        path=Path(report['path'])
        marker='shepherd-checkpoint:v2:'+portable['checkpoint_id']+':1:'+portable['public_hash']
        batch=connector._checkpoint_batch(path,marker)
        self.assertEqual(len(batch),len(portable['items']))
        self.assertEqual(batch[0]['internal_chat_message_metadata_passthrough']['content_item_kinds'],
                         ['compaction.summary'])

    def test_partial_portable_checkpoint_never_reinjects_batch(self):
        connector=self.connector()
        from shepherd.portable_checkpoint import codex_items
        common={'sessionId':'claude-session','cwd':str(self.cwd),'version':'2.1.268',
                'timestamp':'2026-09-15T00:00:00.000Z'}
        boundary={**common,'type':'system','subtype':'compact_boundary',
            'content':'Conversation compacted','level':'info','uuid':'boundary','parentUuid':None,
            'logicalParentUuid':'prior','compactMetadata':{'trigger':'manual','preTokens':100,
            'postTokens':20,'cumulativeDroppedTokens':80,'durationMs':1}}
        summary={**common,'type':'user','uuid':'summary','parentUuid':'boundary',
            'isCompactSummary':True,'isVisibleInTranscriptOnly':True,
            'message':{'role':'user','content':'summary'}}
        tail={**common,'type':'assistant','uuid':'tail','parentUuid':'summary','message':{
            'role':'assistant','model':'fixture','content':[{'type':'text','text':'tail'}],
            'stop_reason':'end_turn','stop_sequence':None,'usage':{'input_tokens':0,'output_tokens':0}}}
        portable=derive({'context_key':'source','turn_id':self.cid+':1','through_seq':1,
            'covers_turn_hash':'b'*64,'payload':{'native':[boundary,summary,tail],
            'source_hash':'a'*64}},producer_engine='claude',producer_binding_id='source-binding',
            session_id='claude-session',native_cwd=str(self.cwd),native_codec='claude-2.1.268-v1')
        expected=codex_items(portable)
        conn=connector._open()
        try:conn.request('thread/inject_items',{'threadId':connector.native_session_id,
                                                'items':expected[:1]},overall=30)
        finally:conn.close()
        meta={'checkpoint_id':portable['checkpoint_id'],'covers_through_seq':1,
              'public_hash':portable['public_hash']}
        with self.assertRaisesRegex(Conflict,'部分写入'):
            connector.load_checkpoint(portable,checkpoint=meta)

    def test_persistent_append_resume_and_idempotent_retry(self):
        row,_native,_public=self.commit();connector=self.connector()
        first=connector.append([dict(row,public=self.public())],self.binding['native_revision'])
        again=connector.append([dict(row,public=self.public())],self.binding['native_revision'])
        self.assertEqual(first,again)
        report=connector.verify();self.assertEqual(report['thread_id'],self.binding['native_session_id'])
        self.assertEqual(report['native_revision'],first['native_revision'])
        self.assertIn('1',report['markers']);self.assertEqual(report['markers']['1']['count'],4)
        self.assertFalse((self.root/'must-not-execute').exists())
        self.assertTrue(Path(report['path']).is_file())
        expected = os.path.realpath(self.root/'state/native'/self.engine/self.binding_id)
        self.assertEqual(os.path.commonpath((expected, os.path.realpath(report['path']))), expected)

    def test_provider_composite_call_id_is_accepted_by_native_injection(self):
        row,_native,_public=self.commit();connector=self.connector()
        original=('call_3D7bUZ7eLFrQFWgz0UEJ4k4r|'
                  'fc_0af2bfaeb884fe00016aaa93b4c9b087d286275f76ef625fe1')
        public=[
            {'type':'function_call','call_id':original,'name':'read','arguments':'{}'},
            {'type':'function_call_output','call_id':original,'output':'ok'},
        ]
        connector.append([dict(row,public=public)],self.binding['native_revision'])
        report=connector.verify();path=Path(report['path'])
        ids=[item['call_id'] for item in connector._authoritative_items(path)
             if item.get('call_id')]
        self.assertEqual(len(ids),2)
        self.assertEqual(ids[0],ids[1])
        self.assertEqual(len(ids[0]),64)
        self.assertRegex(ids[0],r'^call_[0-9a-f]{59}$')

    def test_resolve_source_round_trips_complete_native_marker_batch(self):
        row,_native,public=self.commit();connector=self.connector()
        connector.append([dict(row,public=public)],self.binding['native_revision'])
        ref={'kind':'native_round','engine':self.engine,
             'session_id':self.binding['native_session_id'],'entry_id':row['round_id'],
             'source_key':f'{self.engine}:{self.binding["native_session_id"]}:{row["round_id"]}'}
        resolved=connector.resolve_source(ref)
        expected=copy.deepcopy(public)
        expected[1]['arguments']=json.dumps(json.loads(expected[1]['arguments']),
                                             ensure_ascii=False,separators=(',',':'))
        self.assertEqual(resolved['public'],expected)
        self.assertEqual(len(resolved['source']['items']),4)
        self.assertEqual(resolved['source']['entry_hash'],row['entry_hash'])
        with self.assertRaises(Conflict):
            connector.resolve_source({**ref,'session_id':'00000000-0000-4000-8000-000000000000'})

    def test_runner_uses_private_thread_and_native_turn_receipt(self):
        connector=self.connector()
        public=self.public()
        turn_id='turn_native_1'
        with mock.patch.object(app,'run_turn',return_value=connector.native_session_id) as run:
            def side_effect(*args,**kwargs):
                self.assertTrue(kwargs['on_turn_submitting'](connector.native_session_id))
                kwargs['on_turn_started'](connector.native_session_id,turn_id)
                kwargs['on_notification']('turn/completed', {
                    'threadId':connector.native_session_id,
                    'turn':{'id':turn_id,'status':'completed'}})
                return connector.native_session_id
            run.side_effect=side_effect
            # Seed the exact native turn items that run_turn would persist.
            items=encode_round({'round_id':self.cid+':1','offset':1,'entry_hash':'a'*64,'public':public})
            for item in items:
                item['internal_chat_message_metadata_passthrough']={'turn_id':turn_id}
            raw=connector._open()
            try: raw.request('thread/inject_items',{'threadId':connector.native_session_id,'items':items},overall=30)
            finally: raw.close()
            self.coordinator.revoke(self.cid,self.lease['lease_id'],self.lease['generation'])
            lease=self.coordinator.acquire(self.cid,self.binding_id,ownership_nonce='nonce_target')
            row=ExecutionHarness(self.coordinator,connector,CodexFamilyRunner(
                connector,options={'model':'gpt-5.6','sandbox':'read-only'})).run(
                self.cid,lease_id=lease['lease_id'],generation=lease['generation'],prompt='question',
                attempt_id='attempt_native')
        resolved=connector.resolve_source(row['source'])
        expected=copy.deepcopy(public);expected[1]['arguments']=json.dumps(
            json.loads(expected[1]['arguments']),ensure_ascii=False,separators=(',',':'))
        self.assertEqual(resolved['public'],expected)
        self.assertEqual(resolved['source']['turn_id'],turn_id)
        self.assertTrue(run.call_args.kwargs['strict_boundary'])
        self.assertIsNone(run.call_args.kwargs['history_items'])
        self.assertEqual(run.call_args.args[2],connector.native_session_id)

    def test_switch_runner_materializes_before_lease_transfer(self):
        row,native,public=self.commit()
        class Resolver:
            def resolve(self,_source):return {'source':native,'public':public}
        connector=self.connector()
        lease=SwitchRunner(self.coordinator,ReadProjector(self.state,Resolver()),connector).switch(
          self.cid,self.binding_id,ownership_nonce='nonce_target',lease_id=self.lease['lease_id'],
          generation=self.lease['generation'])
        state=self.coordinator.state(self.cid)
        self.assertEqual(lease['binding_id'],self.binding_id)
        self.assertEqual(state['bindings'][self.binding_id]['applied_offset'],1)
        self.assertEqual(state['bindings'][self.binding_id]['native_revision'],connector.verify()['native_revision'])

    def test_partial_marker_blocks_without_duplicate_append(self):
        row,_native,_public=self.commit();connector=self.connector();items=encode_round(dict(row,public=self.public()))
        conn=connector._open()
        try:conn.request('thread/inject_items',{'threadId':connector.native_session_id,'items':items[:1]},overall=30)
        finally:conn.close()
        before=connector.verify()
        with self.assertRaises(Conflict): connector.append(
            [dict(row,public=self.public())],before['native_revision'])
        after=connector.verify()
        self.assertEqual(before['native_revision'],after['native_revision'])
        self.assertEqual(before['markers'],after['markers'])

    def test_revision_conflict_does_not_write(self):
        row,_native,_public=self.commit();connector=self.connector();before=connector.verify()
        with self.assertRaises(Conflict):connector.append([dict(row,public=self.public())],'0'*64)
        after=connector.verify()
        self.assertEqual(before['native_revision'],after['native_revision'])
        self.assertEqual(before['markers'],after['markers'])

    def test_rollout_stays_inside_private_binding_home(self):
        row,_native,_public=self.commit();connector=self.connector()
        connector.append([dict(row,public=self.public())],self.binding['native_revision'])
        path=os.path.realpath(connector.verify()['path'])
        home=os.path.realpath(self.root/'state/native'/self.engine/self.binding_id/'home')
        self.assertEqual(os.path.commonpath((home,path)),home)
        global_home=os.path.realpath(Path.home()/'.codex')
        self.assertNotEqual(os.path.commonpath((global_home,path)),global_home)


# 下面一组会启动本机真实 Codex CLI，并创建、注入、读取持久线程。它们保留为
# 发布前原生协议门禁，但不属于每次改动都应执行的离线回归；Codec 与 Mock 契约仍在上方。
@pytest.mark.native
class CodexConnector(CodexConnectorCases,CodexFamilyFixture):
    engine='codex'


if __name__=='__main__':unittest.main()
