"""Storage-independent contracts for unified_archive, unified_connector and service."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from shepherd.service import Service
from shepherd.harness import ExecutionHarness
from shepherd.source_resolver import SourceResolver
from shepherd.projector import ReadProjector
from shepherd.unified_archive import UnifiedArchive, _ArchiveConnections
from shepherd.unified_connector import UnifiedConnector
from shepherd.source_receipts import SourceReceipts
from shepherd.store import Conflict, SourceVerificationError
from shepherd.turn_inputs import TurnInputs

ENGINES=('claude','codex','pi')
PUBLIC=[{'type':'function_call','call_id':'c','name':'read','arguments':'{}'},
        {'type':'function_call_output','call_id':'c','output':'historical result'},
        {'type':'message','role':'assistant','content':[{'type':'output_text','text':'done'}]}]


class JournalRunner:
    def __init__(self,connector,public=None):
        self.connector=connector;self.public=PUBLIC if public is None else public
    def run(self,*,round_id,on_dispatched,**kwargs):
        c=self.connector; archive=c.archive
        archive.begin(c.binding,round_id)
        on_dispatched()
        row={'round_id':round_id,'offset':kwargs['expected_offset'],'entry_hash':'a'*64,'public':self.public}
        values=c.encode(row)
        kind='claude_entry' if c.engine=='claude' else 'pi_entry' if c.engine=='pi' else 'codex_item'
        for value in values:
            if c.engine=='pi' and value.get('type')!='custom':value={'type':'message','message':value}
            archive.append(c.binding,round_id,{'type':kind,'value':value})
        revision=archive.finish(c.binding,round_id,'completed')
        c.receipts.write(c.binding,round_id,{'turn_id':round_id,'terminal_state':'completed','native_revision':revision})
        return {'source':c._ref(round_id),'terminal_state':'completed','native_revision':revision}


def _image_transport_receipt():
    from shepherd.image_transport import POLICY_VERSION
    from shepherd.image_variants import variant_key
    return {'policy': POLICY_VERSION, 'derivations': [{
        'policy': POLICY_VERSION, 'source_sha256': 'a' * 64,
        'variant_sha256': 'b' * 64, 'source_mime': 'image/png',
        'variant_mime': 'image/png', 'source_bytes': 4000000,
        'variant_bytes': 1000, 'source_size': [3000, 1500],
        'variant_size': [2000, 1000], 'jpeg_quality': None,
        'variant_key': variant_key(POLICY_VERSION, 'image/png', 'a' * 64)}]}


class UnifiedStorage(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(_ArchiveConnections.close_all)
        self.root=Path(self.temp.name);self.service=Service(self.root,storage='unified')

    def test_claude_compaction_attachments_are_counted_without_payload(self):
        common = {'sessionId':'claude-session','cwd':'/work','version':'2.1.283'}
        source = {'identity':{'engine':'claude'}, 'events':[
            {'type':'claude_entry','value':{**common, 'type':'system',
                'subtype':'compact_boundary','uuid':'boundary','parentUuid':None,
                'logicalParentUuid':'prior','content':'Conversation compacted','level':'info',
                'compactMetadata':{'trigger':'auto','preTokens':100,'postTokens':20,
                    'cumulativeDroppedTokens':80,'durationMs':1,
                    'preservedMessages':{'anchorUuid':'summary'}}}},
            {'type':'claude_entry','value':{**common, 'type':'attachment','uuid':'attachment',
                'parentUuid':'boundary','rendered':[],
                'attachment':{'type':'instructions','files':[{'path':'private'}]}}},
            {'type':'claude_entry','value':{**common, 'type':'user','uuid':'summary',
                'parentUuid':'attachment','isCompactSummary':True,
                'isVisibleInTranscriptOnly':True,
                'message':{'role':'user','content':'private summary'}}},
        ]}
        diagnostics = UnifiedConnector.source_compatibility(source)
        self.assertEqual(diagnostics, [
            {'engine':'claude','action':'ignored_internal_entry',
             'kind':'attachment/instructions','count':1},
            {'engine':'claude','action':'ignored_internal_entry',
             'kind':'system/compact_boundary','count':1},
            {'engine':'claude','action':'ignored_internal_entry',
             'kind':'user/compact_summary','count':1},
        ])
        self.assertNotIn('private', json.dumps(diagnostics))

    def turn(self,engine,seq):
        s=self.service
        if seq==1:s.create('conversation',engine,self.root)
        else:s.switch('conversation',engine,self.root)
        active=s.active('conversation');c=s.connector(active['binding'])
        lease=active['lease']
        row=ExecutionHarness(s.coordinator,c,JournalRunner(c)).run('conversation',
            lease_id=lease['lease_id'],generation=lease['generation'],prompt='question',
            input_public=[{'type':'message','role':'user','content':[{'type':'input_text','text':f'question {seq}'}]}])
        return c,row

    def test_engine_switches_have_one_archive_and_no_native_history(self):
        for seq,engine in enumerate((*ENGINES,*reversed(ENGINES)),1):
            self.turn(engine,seq)
        state=self.service.coordinator.state('conversation')
        projector=ReadProjector(self.service.store,SourceResolver([
            self.service.connector(binding) for binding in state['bindings'].values()]))
        rows=projector.project('conversation')
        self.assertEqual(len(rows),2*len(ENGINES))
        for seq,row in enumerate(rows,1):
            self.assertEqual(row['public'][0]['content'][0]['text'],f'question {seq}')
            self.assertEqual(row['public'][1:],PUBLIC)
        self.assertEqual(len(list(self.root.rglob('history.sqlite3'))),1)
        self.assertFalse((self.root/'native').exists())

    @mock.patch.object(_ArchiveConnections, 'RETAIN_IDLE', True)
    def test_batch_reads_share_connection_preserve_order_and_verify_every_turn(self):
        c,first=self.turn('pi',1)
        c,second=self.turn('pi',2)
        refs=[second['source'],first['source'],second['source']]
        expected=[c.resolve_source(ref) for ref in refs]
        _ArchiveConnections.close_all()
        with mock.patch('shepherd.unified_archive.sqlite3.connect', wraps=sqlite3.connect) as opened:
            self.assertEqual(c.resolve_sources(refs),expected)
            self.assertEqual(c.resolve_sources(refs),expected)
        # After close_all, the first batch opens one non-blocking read-only slot;
        # the second batch reuses it without waiting on writers.
        self.assertEqual(opened.call_count,1)
        with c.archive.db() as db:
            db.execute("UPDATE events SET payload='{}' WHERE turn_id=? AND ordinal=1",
                       (second['source']['entry_id'],))
        with self.assertRaises(SourceVerificationError):c.resolve_sources(refs)

    @mock.patch.object(_ArchiveConnections, 'RETAIN_IDLE', True)
    def test_connection_pool_close_reopens_and_rolls_back_cleanly(self):
        c,_=self.turn('pi',1)
        _ArchiveConnections.close_all()
        with mock.patch('shepherd.unified_archive.sqlite3.connect', wraps=sqlite3.connect) as opened:
            with self.assertRaises(RuntimeError):
                with c.archive.db() as db:
                    db.execute("INSERT INTO context_limits VALUES(?,?,?)", ('rollback','t',1))
                    raise RuntimeError('rollback')
            with c.archive.db() as db:
                self.assertIsNone(db.execute(
                    "SELECT context_key FROM context_limits WHERE context_key='rollback'").fetchone())
            self.assertEqual(opened.call_count,1)
            _ArchiveConnections.close_all()
            with c.archive.db() as db:
                self.assertEqual(db.execute('PRAGMA synchronous').fetchone()[0],2)
            self.assertEqual(opened.call_count,2)

    def test_close_all_does_not_hand_closed_connection_to_waiter(self):
        c,_=self.turn('pi',1)
        _ArchiveConnections.close_all()
        holding=threading.Event();release=threading.Event();result=[]
        def holder():
            with c.archive.db() as db:
                db.execute('SELECT 1').fetchone();holding.set();release.wait(2)
        def waiter():
            with c.archive.db() as db:
                result.append(db.execute('SELECT 1').fetchone()[0])
        first=threading.Thread(target=holder);first.start();self.assertTrue(holding.wait(1))
        second=threading.Thread(target=waiter);second.start()
        deadline=time.monotonic()+1
        while time.monotonic()<deadline:
            with _ArchiveConnections._lock:
                slot=_ArchiveConnections._slots.get(str(c.archive.path))
                if slot and slot.users==2:break
            time.sleep(.005)
        closer=threading.Thread(target=_ArchiveConnections.close_all);closer.start()
        release.set()
        for thread in (first,second,closer):thread.join(2)
        self.assertEqual(result,[1])
        self.assertFalse(any(thread.is_alive() for thread in (first,second,closer)))

    @mock.patch.object(_ArchiveConnections, 'RETAIN_IDLE', True)
    def test_connection_pool_is_bounded_and_busy_overflow_is_ephemeral(self):
        archives=[UnifiedArchive(self.root,'pool-%d'%i) for i in range(3)]
        with mock.patch.object(_ArchiveConnections,'MAX_OPEN',2):
            with archives[0].db(), archives[1].db(), archives[2].db():
                self.assertEqual(len(_ArchiveConnections._slots),2)
                self.assertNotIn(str(archives[2].path),_ArchiveConnections._slots)
            self.assertEqual(len(_ArchiveConnections._slots),2)

    def test_no_idle_cache_policy_releases_handle_after_last_user(self):
        archive = UnifiedArchive(self.root, 'no-idle')
        with mock.patch.object(_ArchiveConnections, 'RETAIN_IDLE', False):
            with archive.db() as db:
                db.execute('SELECT 1')
            self.assertNotIn(str(archive.path), _ArchiveConnections._slots)
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute('SELECT 1')
            # An actual unlink (not gc or a permissive cleanup) proves release.
            archive.path.unlink()

    def test_future_archive_schema_is_rejected_without_rewrite(self):
        archive=UnifiedArchive(self.root,'future')
        archive.directory.mkdir(parents=True)
        db=sqlite3.connect(archive.path)
        db.execute('PRAGMA user_version=99')
        db.commit();db.close()
        archive.path.chmod(0o600)
        with self.assertRaisesRegex(Conflict,'schema is newer'):
            with archive.db():
                pass
        check=sqlite3.connect(archive.path)
        try:self.assertEqual(check.execute('PRAGMA user_version').fetchone()[0],99)
        finally:check.close()

    def test_readonly_schema_rejection_closes_connection(self):
        c,_=self.turn('pi',1)
        c.archive.cursor(c.binding['binding_id'])
        _ArchiveConnections.close_all()
        external=sqlite3.connect(c.archive.path)
        external.execute('PRAGMA user_version=99')
        external.commit();external.close()
        real_connect=sqlite3.connect
        opened=[]
        def connect(*args,**kwargs):
            connection=real_connect(*args,**kwargs);opened.append(connection);return connection
        with mock.patch('shepherd.unified_archive.sqlite3.connect',side_effect=connect):
            with self.assertRaisesRegex(Conflict,'schema is newer'):
                c.archive.cursor(c.binding['binding_id'])
        self.assertTrue(opened)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[-1].execute('SELECT 1')
        external=sqlite3.connect(c.archive.path)
        external.execute('PRAGMA user_version=1')
        external.commit();external.close()

    def test_cached_connection_rejects_future_schema(self):
        c,_=self.turn('pi',1)
        with c.archive.db():
            pass
        external=sqlite3.connect(c.archive.path)
        external.execute('PRAGMA user_version=99')
        external.commit();external.close()
        with self.assertRaisesRegex(Conflict,'schema is newer'):
            with c.archive.db():
                pass
        external=sqlite3.connect(c.archive.path)
        external.execute('PRAGMA user_version=1')
        external.commit();external.close()

    def test_replaced_archive_inode_reopens_connection(self):
        c,_=self.turn('pi',1)
        _ArchiveConnections.close_all()
        with mock.patch('shepherd.unified_archive.sqlite3.connect', wraps=sqlite3.connect) as opened:
            with c.archive.db():
                pass
            old=c.archive.path.with_suffix('.old')
            c.archive.path.replace(old)
            c.archive.path.touch(mode=0o600)
            with c.archive.db() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM turns').fetchone()[0],0)
            self.assertEqual(opened.call_count,2)

    def test_batch_snapshot_does_not_mix_concurrent_writes(self):
        c,first=self.turn('pi',1)
        c,second=self.turn('pi',2)
        ids=[first['source']['entry_id'],second['source']['entry_id']]
        expected=c.archive.read_many(c.binding,ids)
        read=c.archive._read
        def racing(db,binding,turn_id):
            row=read(db,binding,turn_id)
            if turn_id==ids[0]:
                # Model an external writer, which does not participate in this
                # process's per-archive connection slot.
                writer=sqlite3.connect(c.archive.path)
                try:
                    writer.execute("UPDATE events SET payload='{}' WHERE turn_id=? AND ordinal=1",(ids[1],))
                    writer.commit()
                finally:
                    writer.close()
            return row
        with mock.patch.object(c.archive,'_read',side_effect=racing):
            self.assertEqual(c.archive.read_many(c.binding,ids),expected)
        with self.assertRaises(Conflict):c.archive.read_many(c.binding,ids)

    def test_archive_survives_service_and_connector_recreation(self):
        c,row=self.turn('pi',1)
        restarted=Service(self.root,storage='unified')
        rebuilt=restarted.connector(restarted.active('conversation')['binding'])
        self.assertEqual(rebuilt.resolve_source(row['source']),c.resolve_source(row['source']))

    def test_switch_retries_do_not_duplicate_archived_bodies(self):
        c,_=self.turn('codex',1)
        with c.archive.db() as db:before=db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        for engine in ('pi','claude','codex','pi'):
            self.service.switch('conversation',engine,self.root)
        with c.archive.db() as db:after=db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        self.assertEqual(after,before)

    def test_tampering_and_cross_binding_reads_are_rejected(self):
        c,row=self.turn('codex',1)
        wrong={**row['source'],'session_id':'other'}
        with self.assertRaises(SourceVerificationError):c.resolve_source(wrong)
        with c.archive.db() as db:db.execute("UPDATE events SET payload='{}' WHERE ordinal=1")
        with self.assertRaises(SourceVerificationError):c.resolve_source(row['source'])

    def test_foreign_event_type_and_grounding_hash_are_rejected(self):
        import hashlib
        self.service.create('conversation','claude',self.root)
        c=self.service.connector(self.service.active('conversation')['binding'])
        with self.assertRaises(SourceVerificationError):
            c._public({'events':[{'type':'codex_item','value':{}}]})
        marker='<system-reminder data-desktron-grounding="g">\ncurrent\n</system-reminder>'
        entry={'type':'user','uuid':'g','sessionId':c.native_session_id,'origin':{'kind':'human'},
               'message':{'role':'user','content':[{'type':'text','text':marker},{'type':'text','text':'question'}]}}
        events=[{'type':'runtime','grounding_uuid':'g','grounding_sha256':hashlib.sha256(marker.encode()).hexdigest()},
                {'type':'claude_entry','value':entry}]
        self.assertEqual(c._public({'events':events})[0]['content'][0]['text'],'question')
        events[0]['grounding_sha256']='f'*64
        with self.assertRaises(SourceVerificationError):c._public({'events':events})
        events=[{'type':'runtime','grounding_uuid':'g','grounding_sha256':'f'*64},
                {'type':'claude_entry','value':{**entry,'uuid':'other'}}]
        with self.assertRaises(SourceVerificationError):c._public({'events':events})
        assistant={'type':'assistant','uuid':'answer','sessionId':c.native_session_id,
            'message':{'role':'assistant','content':[{'type':'text','text':'answer'}]}}
        events=[{'type':'runtime','grounding_uuid':'g','grounding_sha256':'f'*64},
                {'type':'claude_entry','value':assistant}]
        with self.assertRaises(SourceVerificationError):c._public({'events':events})
        gid='11111111-2222-4333-8444-555555555555'
        anchored_entry={**entry,'uuid':gid}
        anchored_entry['message']={'role':'user','content':[{'type':'text','text':
            f'<system-reminder data-desktron-grounding="{gid}">\nprivate\n</system-reminder>'},
            {'type':'text','text':'question'}]}
        anchor={'type':'native_anchor','attempt_id':'attempt_one','parent_uuid':'',
            'store_revision':'a'*64,'store_size':0,'bridge_protocol':3,
            'claude_version':'2.1.283','grounding_uuid':gid}
        with self.assertRaisesRegex(SourceVerificationError,'metadata missing'):
            c._public({'events':[{'type':'runtime','engine':'claude','model':'','effort':''},
                anchor,{'type':'claude_entry','value':anchored_entry}]})

    def test_claude_task_notification_crash_recovers_without_losing_human_input(self):
        import hashlib
        from shepherd.recovery_controller import RecoveryController
        self.service.create('conversation','claude',self.root)
        active=self.service.active('conversation'); c=self.service.connector(active['binding'])
        lease=active['lease']; question=[{'type':'message','role':'user','content':[
            {'type':'input_text','text':'进展如何'}]}]
        row=self.service.coordinator.begin_round('conversation',lease['lease_id'],lease['generation'],
            attempt_id='attempt_task_notice', input_ref=TurnInputs(self.root).write(
                'conversation','conversation:1',question),
            recovery={'phase':'reserved','boot_id':'old','boundary':c.recovery_boundary()})
        gid='11111111-2222-4333-8444-555555555555'
        c.archive.begin_predispatch(c.binding,row['round_id'],
            {'type':'runtime','model':'opus','effort':'','engine':'claude'},native_anchor={
                'type':'native_anchor','attempt_id':row['attempt_id'],'parent_uuid':'',
                'store_revision':'a'*64,'store_size':0,'bridge_protocol':3,
                'claude_version':'2.1.283','grounding_uuid':gid},grounding_runtime={
                'type':'runtime','grounding_uuid':gid,
                'grounding_sha256':hashlib.sha256((f'<system-reminder data-desktron-grounding="{gid}">\nctx\n</system-reminder>').encode()).hexdigest()},
            attempt_id=row['attempt_id'])
        self.service.coordinator.mark_dispatched('conversation',lease['lease_id'],lease['generation'],
            attempt_id=row['attempt_id'],process={'pid':99999998,'pgid':99999998,'start_token':'old'},
            native_locator={'turn_id':row['round_id']})
        c.archive.append(c.binding,row['round_id'],{'type':'runtime','grounding_uuid':gid,
            'grounding_sha256':hashlib.sha256((f'<system-reminder data-desktron-grounding="{gid}">\nctx\n</system-reminder>').encode()).hexdigest()})
        common={'sessionId':c.native_session_id,'cwd':str(self.root),'version':'2.1.277'}
        c.archive.append(c.binding,row['round_id'],{'type':'claude_entry','value':{
            **common,'type':'queue-operation','operation':'enqueue','content':'task notice'}})
        c.archive.append(c.binding,row['round_id'],{'type':'claude_entry','value':{
            **common,'type':'attachment','uuid':'hook','attachment':{'type':'hook_success','content':'',
            'hookEvent':'SessionStart','hookName':'SessionStart','toolUseID':'hook'}}})
        c.archive.append(c.binding,row['round_id'],{'type':'claude_entry','value':{
            **common,'type':'user','uuid':'notice','origin':{'kind':'task-notification'},
            'promptSource':'system','queueSkipAttachments':True,
            'message':{'role':'user','content':'background task finished'}}})
        self.service.coordinator.mark_unresolved('conversation',lease['lease_id'],lease['generation'],
            attempt_id=row['attempt_id'],reason='native_evidence_incomplete')
        with mock.patch('shepherd.recovery_controller.process_state',return_value='dead'), \
             mock.patch('shepherd.recovery.process_state',return_value='dead'):
            report=RecoveryController(self.service,boot_id='new').recover_guarded(
                'conversation',trigger='startup')
        self.assertEqual(report['outcome'],'recovered_incomplete')
        state=self.service.coordinator.state('conversation')
        self.assertIsNone(state['inflight'])
        self.assertNotEqual(state['active_binding_id'],c.binding_id)
        public=ReadProjector(self.service.store, SourceResolver([c])).project('conversation')[0]['public']
        self.assertEqual(public[0]['content'][0]['text'],'进展如何')
        self.assertIn('background task finished',json.dumps(public,ensure_ascii=False))

    def test_peer_returns_remain_inert_portable_history_with_opaque_source_retained(self):
        from shepherd.source_codec import codex_public
        from shepherd.store import InvalidState
        from test_shepherd_images import roundtrip
        peer = {'type': 'agent_message', 'author': '/root/reviewer', 'recipient': '/root',
                'content': [{'type': 'input_text', 'text': 'review result'},
                            {'type': 'encrypted_content', 'encrypted_content': 'opaque-private'}],
                'future_metadata': True}
        before = copy.deepcopy(peer)
        public = codex_public([peer], version=2)
        self.assertEqual(peer, before)
        self.assertEqual(public[0]['role'], 'assistant')
        self.assertIn('review result', json.dumps(public))
        self.assertNotIn('opaque-private', json.dumps(public))
        for engine in ENGINES:
            self.assertEqual(roundtrip(engine, {'round_id': 'conversation:1', 'offset': 1,
                'entry_hash': 'a'*64, 'public': public}), public)
        for bad in ({**peer, 'author': None}, {**peer, 'content': [{'type': 'future_tool'}]},
                    {**peer, 'content': [{'type': 'encrypted_content', 'encrypted_content': {}}]}):
            with self.subTest(bad=bad), self.assertRaises(InvalidState):
                codex_public([bad], version=2)

    def test_pi_execution_review_prompt_is_filtered_only_by_trusted_hash(self):
        self.service.create('conversation', 'pi', self.root)
        c = self.service.connector(self.service.active('conversation')['binding'])
        prompt = '[Desktron 长任务复核] exact internal prompt'
        events = [
            {'type':'runtime', 'execution_review_prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest()},
            {'type':'pi_entry', 'value':{'type':'message','message':{'role':'user','content':prompt}}},
            {'type':'pi_entry', 'value':{'type':'message','message':{'role':'user','content':prompt+' changed'}}},
            {'type':'pi_entry', 'value':{'type':'message','message':{'role':'assistant','content':'kept'}}},
        ]
        public = c._public({'events':events}, version=4)
        texts = json.dumps(public, ensure_ascii=False)
        self.assertNotIn('exact internal prompt\"', texts)
        self.assertIn('exact internal prompt changed', texts)
        self.assertIn('kept', texts)

    def test_pi_bash_execution_is_past_evidence_not_a_replayable_tool(self):
        from shepherd.source_codec import pi_public
        from shepherd.store import InvalidState
        message = {'role': 'bashExecution', 'command': 'send already happened', 'output': 'done',
                   'exitCode': 0, 'cancelled': False, 'truncated': True,
                   'fullOutputPath': '/must/not/read', 'future_metadata': 1}
        entries = [{'type': 'message', 'message': message}]
        before = copy.deepcopy(entries)
        with mock.patch('builtins.open', side_effect=AssertionError('must not read output path')):
            public = pi_public(entries, version=2)
        self.assertEqual(entries, before)
        self.assertEqual(public[0]['role'], 'assistant')
        self.assertEqual([x['type'] for x in public], ['message'])
        self.assertIn('send already happened', json.dumps(public))
        message['excludeFromContext'] = True
        self.assertEqual(pi_public(entries, version=2), [])
        message['cancelled'] = 'false'
        with self.assertRaises(InvalidState): pi_public(entries, version=2)

    def test_native_compaction_extensions_do_not_change_public_projection(self):
        self.service.create('conversation', 'codex', self.root)
        c = self.service.connector(self.service.active('conversation')['binding'])
        events = [{'type': 'codex_item', 'value': {'type': kind, 'future_metadata': 1,
                   **({'encrypted_content': 'opaque'} if kind == 'compaction' else {})}}
                  for kind in ('compaction', 'compaction_trigger', 'context_compaction')]
        before = copy.deepcopy(events)
        self.assertEqual(c._public({'events': events}), [])
        self.assertEqual(events, before)
        events[0]['value']['encrypted_content'] = 42
        with self.assertRaises(SourceVerificationError): c._public({'events': events})

    def test_terminal_events_are_immutable(self):
        c,_=self.turn('pi',1)
        with self.assertRaises(Conflict):c.archive.append(c.binding,'conversation:1',{'type':'runtime'})
        with self.assertRaises(Conflict):c.archive.finish(c.binding,'conversation:1','unknown')

    def test_dispatch_fence_failure_cancels_reservation_and_preserves_anchor_evidence(self):
        from shepherd.unified_runners import UnifiedRunner
        self.service.create('conversation','codex',self.root)
        active=self.service.active('conversation'); first=self.service.connector(active['binding'])
        lease=active['lease']; runner=UnifiedRunner(first,options={'model':'fixture','effort':'high'})
        def native(_prompt,_rounds,_checkpoint,dispatch,_locate):
            dispatch()
            raise AssertionError('dispatch fence should reject before native execution')
        with mock.patch.object(self.service.coordinator,'mark_dispatched',
                               side_effect=Conflict('lease changed')), \
             mock.patch.object(runner,'_codex',side_effect=native):
            with self.assertRaisesRegex(Exception,'lease changed'):
                ExecutionHarness(self.service.coordinator,first,runner).run(
                    'conversation',lease_id=lease['lease_id'],generation=lease['generation'],
                    prompt='question')
        state=self.service.coordinator.state('conversation')
        self.assertIsNone(state.get('inflight'))
        self.assertEqual(state['committed_offset'],0)
        with first.archive.db() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM turns WHERE turn_id=?',
                                         ('conversation:1',)).fetchone())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM abandoned_turns WHERE turn_id=?',
                                        ('conversation:1',)).fetchone()[0],1)
        self.service.switch('conversation','pi',self.root)
        second=self.service.connector(self.service.active('conversation')['binding'])
        second.archive.begin_predispatch(second.binding,'conversation:1',
            {'type':'runtime','engine':'pi','model':'new','effort':'high'})
        self.assertEqual(second.archive.read(second.binding,'conversation:1')['terminal'],None)

    def test_claude_native_anchor_is_durable_before_dispatch_fence(self):
        from shepherd.unified_runners import UnifiedRunner
        self.service.create('conversation','claude',self.root)
        active=self.service.active('conversation'); connector=self.service.connector(active['binding'])
        lease=active['lease']; runner=UnifiedRunner(connector,options={'model':'fixture'})
        parent='11111111-2222-4333-8444-555555555555'
        revision='c'*64
        observed=[]
        real_mark=self.service.coordinator.mark_dispatched
        def mark(*args,**kwargs):
            source=connector.archive.read(connector.binding,'conversation:1')['source']
            observed.append((copy.deepcopy(source['events']),kwargs['attempt_id']))
            return real_mark(*args,**kwargs)
        def native(_prompt,_rounds,_checkpoint,dispatch,_locate):
            dispatch(native_anchor=runner._claude_native_anchor({
                'parentUuid':parent,'nativeRevision':revision,'startSeq':4,'storeSize':4,
                'bridgeProtocolVersion':3,'claudeVersion':'2.1.283'}))
            connector.archive.append(connector.binding,'conversation:1',{'type':'claude_entry','value':{
                'type':'assistant','uuid':'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee',
                'sessionId':connector.native_session_id,'cwd':connector.native_cwd,
                'parentUuid':parent,'message':{'role':'assistant','content':[]}}})
            return 'completed'
        with mock.patch.object(self.service.coordinator,'mark_dispatched',side_effect=mark), \
             mock.patch.object(runner,'_claude',side_effect=native), \
             mock.patch.object(connector,'resolve_source',return_value={'source':{},'public':[]}):
            ExecutionHarness(self.service.coordinator,connector,runner).run(
                'conversation',lease_id=lease['lease_id'],generation=lease['generation'],
                prompt='question')
        self.assertEqual(len(observed),1)
        events,attempt_id=observed[0]
        anchor=events[1]
        self.assertEqual(anchor['type'],'native_anchor')
        self.assertEqual(anchor['parent_uuid'],parent)
        self.assertEqual(anchor['store_revision'],revision)
        self.assertEqual(anchor['claude_version'],'2.1.283')
        self.assertEqual(anchor['attempt_id'],attempt_id)

    def test_claude_dispatch_fence_cleanup_failure_keeps_reserved_fence(self):
        from shepherd.unified_runners import UnifiedRunner
        self.service.create('conversation','claude',self.root)
        active=self.service.active('conversation');connector=self.service.connector(active['binding'])
        lease=active['lease'];runner=UnifiedRunner(connector)
        def native(_prompt,_rounds,_checkpoint,dispatch,_locate):
            dispatch(native_anchor=runner._claude_native_anchor({
                'parentUuid':'','nativeRevision':'d'*64,'startSeq':0,'storeSize':0,
                'bridgeProtocolVersion':3,'claudeVersion':'2.1.283'}))
        with mock.patch.object(self.service.coordinator,'mark_dispatched',
                               side_effect=Conflict('lease changed')), \
             mock.patch.object(connector.archive,'abandon_predispatch',
                               side_effect=OSError('disk unavailable')), \
             mock.patch.object(runner,'_claude',side_effect=native):
            with self.assertRaisesRegex(Conflict,'cleanup failed'):
                ExecutionHarness(self.service.coordinator,connector,runner).run(
                    'conversation',lease_id=lease['lease_id'],generation=lease['generation'],
                    prompt='question')
        state=self.service.coordinator.state('conversation')
        self.assertEqual(state['inflight']['attempt_id'],runner.attempt_id)
        self.assertEqual(state['inflight']['recovery']['phase'],'reserved')
        self.assertEqual(connector.archive.read(connector.binding,'conversation:1')
                         ['source']['events'][1]['attempt_id'],runner.attempt_id)

    def test_claude_dispatch_fence_failure_abandons_native_anchor(self):
        from shepherd.unified_runners import UnifiedRunner
        self.service.create('conversation','claude',self.root)
        active=self.service.active('conversation'); connector=self.service.connector(active['binding'])
        lease=active['lease']; runner=UnifiedRunner(connector)
        def native(_prompt,_rounds,_checkpoint,dispatch,_locate):
            dispatch(native_anchor=runner._claude_native_anchor({
                'parentUuid':'','nativeRevision':'d'*64,'startSeq':0,'storeSize':0,
                'bridgeProtocolVersion':3,'claudeVersion':'2.1.283'}))
            self.fail('dispatch rejection must stop before start')
        with mock.patch.object(self.service.coordinator,'mark_dispatched',
                               side_effect=Conflict('lease changed')), \
             mock.patch.object(runner,'_claude',side_effect=native):
            with self.assertRaisesRegex(Exception,'lease changed'):
                ExecutionHarness(self.service.coordinator,connector,runner).run(
                    'conversation',lease_id=lease['lease_id'],generation=lease['generation'],
                    prompt='question')
        with connector.archive.db() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM turns WHERE turn_id=?',
                                         ('conversation:1',)).fetchone())
            evidence=db.execute('SELECT events FROM abandoned_turns WHERE turn_id=?',
                                ('conversation:1',)).fetchone()
        events=[row['payload'] for row in json.loads(evidence['events'])]
        self.assertEqual([event['type'] for event in events],['runtime','native_anchor'])

    def test_reserved_crash_abandons_native_anchor_before_same_turn_retry(self):
        from shepherd.recovery_controller import RecoveryController
        self.service.create('conversation','claude',self.root)
        active=self.service.active('conversation');c=self.service.connector(active['binding'])
        lease=active['lease'];coordinator=self.service.coordinator
        row=coordinator.begin_round('conversation',lease['lease_id'],lease['generation'],
            attempt_id='attempt_old',recovery={'phase':'reserved','boot_id':'old',
                                               'boundary':c.recovery_boundary()})
        runtime={'type':'runtime','engine':'claude','model':'fixture','effort':''}
        old={'type':'native_anchor','attempt_id':'attempt_old','parent_uuid':'',
             'store_revision':'a'*64,'store_size':0,'bridge_protocol':3,
             'claude_version':'2.1.283'}
        c.archive.begin_predispatch(c.binding,row['round_id'],runtime,native_anchor=old,
                                    attempt_id='attempt_old')
        report=RecoveryController(self.service,boot_id='new').recover_guarded(
            'conversation',trigger='startup')
        self.assertEqual(report['outcome'],'cancelled_before_dispatch')
        with c.archive.db() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM turns WHERE turn_id=?',
                                         (row['round_id'],)).fetchone())
            evidence=db.execute('SELECT reason,events FROM abandoned_turns WHERE turn_id=?',
                                (row['round_id'],)).fetchone()
        self.assertEqual(evidence['reason'],'reserved_crash_recovery')
        self.assertEqual(json.loads(evidence['events'])[1]['payload']['attempt_id'],'attempt_old')
        retry=coordinator.begin_round('conversation',lease['lease_id'],lease['generation'],
            attempt_id='attempt_new',recovery={'phase':'reserved','boot_id':'new',
                                               'boundary':c.recovery_boundary()})
        self.assertEqual(retry['round_id'],row['round_id'])
        c.archive.begin_predispatch(c.binding,retry['round_id'],runtime,
            native_anchor={**old,'attempt_id':'attempt_new'},attempt_id='attempt_new')
        source=c.archive.read(c.binding,retry['round_id'])['source']
        with self.assertRaisesRegex(SourceVerificationError,'attempt mismatch'):
            c._verify_anchor_attempt(source,'attempt_old')
        c._verify_anchor_attempt(source,'attempt_new')

    def test_image_predispatch_fence_rejection_preserves_all_evidence(self):
        from shepherd.unified_runners import UnifiedRunner
        for grounded in (False, True):
            with self.subTest(grounded=grounded):
                sid = 'image_fence_' + str(int(grounded))
                self.service.create(sid, 'claude', self.root)
                active = self.service.active(sid)
                c = self.service.connector(active['binding']); lease = active['lease']
                runner = UnifiedRunner(c, options={'_grounding': 'fixture' if grounded else ''})
                receipt = _image_transport_receipt()
                def native(_prompt, _rounds, _checkpoint, dispatch, _locate):
                    runner.image_transport.derivations[('image/png', 'a' * 64)] = receipt['derivations'][0]
                    anchor = runner._claude_native_anchor({
                        'parentUuid': '', 'nativeRevision': 'c' * 64, 'startSeq': 0,
                        'storeSize': 0, 'bridgeProtocolVersion': 3, 'claudeVersion': '2.1.283',
                        'groundingUuid': 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee' if grounded else ''})
                    dispatch(native_anchor=anchor)
                    self.fail('rejected dispatch must never start the model')
                with mock.patch.object(self.service.coordinator, 'mark_dispatched',
                                       side_effect=Conflict('lease changed')), \
                     mock.patch.object(runner, '_claude', side_effect=native), \
                     mock.patch('shepherd.unified_runners.NativeProcess',
                                side_effect=AssertionError('no native process')):
                    with self.assertRaisesRegex(Conflict, 'lease changed'):
                        ExecutionHarness(self.service.coordinator, c, runner).run(
                            sid, lease_id=lease['lease_id'], generation=lease['generation'], prompt='question')
                state = self.service.coordinator.state(sid)
                self.assertIsNone(state['inflight'])
                self.assertEqual(state['committed_offset'], 0)
                with c.archive.db() as db:
                    self.assertIsNone(db.execute('SELECT 1 FROM turns WHERE turn_id=?', (sid + ':1',)).fetchone())
                    evidence = db.execute('SELECT * FROM abandoned_turns WHERE turn_id=?', (sid + ':1',)).fetchone()
                events = [r['payload'] for r in json.loads(evidence['events'])]
                self.assertEqual(evidence['count'], 4 if grounded else 3)
                self.assertEqual(events[-1], {'type': 'runtime', 'image_transport': receipt})
                self.assertEqual(events[1]['attempt_id'], runner.attempt_id)
                self.assertEqual(evidence['reason'], 'dispatch_fence_rejected')
                if grounded:
                    self.assertEqual(events[2]['grounding_uuid'], events[1]['grounding_uuid'])

    def test_image_reserved_crash_recovery_releases_turn_without_model(self):
        from shepherd.recovery_controller import RecoveryController
        for grounded in (False, True):
            with self.subTest(grounded=grounded):
                sid = 'image_crash_' + str(int(grounded))
                self.service.create(sid, 'claude', self.root)
                active = self.service.active(sid); c = self.service.connector(active['binding'])
                lease = active['lease']; coordinator = self.service.coordinator
                row = coordinator.begin_round(sid, lease['lease_id'], lease['generation'],
                    attempt_id='attempt_old', recovery={'phase': 'reserved', 'boot_id': 'old',
                                                       'boundary': c.recovery_boundary()})
                runtime = {'type': 'runtime', 'engine': 'claude', 'model': 'fixture'}
                anchor = {'type': 'native_anchor', 'attempt_id': 'attempt_old', 'parent_uuid': '',
                          'store_revision': 'a' * 64, 'store_size': 0, 'bridge_protocol': 3,
                          'claude_version': '2.1.283'}
                grounding = None
                if grounded:
                    anchor['grounding_uuid'] = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
                    grounding = {'type': 'runtime', 'grounding_uuid': anchor['grounding_uuid'],
                                 'grounding_sha256': 'd' * 64}
                receipt = _image_transport_receipt()
                c.archive.begin_predispatch(c.binding, row['round_id'], runtime, native_anchor=anchor,
                    grounding_runtime=grounding, image_transport=receipt, attempt_id='attempt_old')
                before = c.archive.read(c.binding, row['round_id'])['source']['events']
                with mock.patch('shepherd.unified_runners.UnifiedRunner.run',
                                side_effect=AssertionError('no model replay')):
                    report = RecoveryController(self.service, boot_id='new').recover_guarded(sid, trigger='startup')
                self.assertEqual(report['outcome'], 'cancelled_before_dispatch')
                self.assertIsNone(coordinator.state(sid)['inflight'])
                with c.archive.db() as db:
                    self.assertIsNone(db.execute('SELECT 1 FROM turns WHERE turn_id=?', (row['round_id'],)).fetchone())
                    evidence = db.execute('SELECT * FROM abandoned_turns WHERE turn_id=?', (row['round_id'],)).fetchone()
                self.assertEqual([r['payload'] for r in json.loads(evidence['events'])], before)
                self.assertEqual(evidence['reason'], 'reserved_crash_recovery')
                retry = coordinator.begin_round(sid, lease['lease_id'], lease['generation'],
                    attempt_id='attempt_new', recovery={'phase': 'reserved', 'boot_id': 'new',
                                                       'boundary': c.recovery_boundary()})
                self.assertEqual(retry['round_id'], row['round_id'])
                c.archive.begin_predispatch(c.binding, retry['round_id'], runtime,
                    native_anchor={**anchor, 'attempt_id': 'attempt_new'}, grounding_runtime=grounding,
                    image_transport=receipt, attempt_id='attempt_new')

    def test_predispatch_cleanup_rejects_invalid_image_records_and_output_suffix(self):
        receipt = _image_transport_receipt()
        malformed = copy.deepcopy(receipt)
        malformed['derivations'][0]['variant_key'] = 'f' * 64
        suffixes = [
            [{'type': 'runtime', 'image_transport': malformed}],
            [{'type': 'runtime', 'image_transport': None}],
            [{'type': 'runtime', 'image_transport': receipt, 'extra': True}],
            [{'type': 'runtime', 'grounding_uuid': 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee',
              'grounding_sha256': 'a' * 64}],
            [{'type': 'runtime', 'image_transport': receipt}] * 2,
            [{'type': 'runtime', 'image_transport': receipt},
             {'type': 'claude_entry', 'value': {'type': 'assistant'}}],
        ]
        for i, suffix in enumerate(suffixes):
            with self.subTest(case=i):
                sid = 'image_invalid_' + str(i)
                self.service.create(sid, 'claude', self.root)
                c = self.service.connector(self.service.active(sid)['binding'])
                anchor = {'type': 'native_anchor', 'attempt_id': 'attempt_one', 'parent_uuid': '',
                          'store_revision': 'a' * 64, 'store_size': 0, 'bridge_protocol': 3,
                          'claude_version': '2.1.283'}
                c.archive.begin_predispatch(c.binding, sid + ':1', {'type': 'runtime', 'engine': 'claude'},
                                           native_anchor=anchor, attempt_id='attempt_one')
                for event in suffix:
                    c.archive.append(c.binding, sid + ':1', event)
                before = c.archive.read(c.binding, sid + ':1')['source']['events']
                with self.assertRaises(Conflict):
                    c.archive.abandon_predispatch(c.binding, sid + ':1', attempt_id='attempt_one')
                self.assertEqual(c.archive.read(c.binding, sid + ':1')['source']['events'], before)
                with c.archive.db() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM abandoned_turns WHERE turn_id=?',
                                                (sid + ':1',)).fetchone()[0], 0)

    def test_predispatch_rejects_grounding_without_nonempty_native_uuid(self):
        from shepherd.store import InvalidState
        self.service.create('conversation', 'claude', self.root)
        c = self.service.connector(self.service.active('conversation')['binding'])
        anchor = {'type': 'native_anchor', 'attempt_id': 'attempt_one', 'parent_uuid': '',
                  'store_revision': 'a' * 64, 'store_size': 0, 'bridge_protocol': 3,
                  'claude_version': '2.1.283'}
        for optional_uuid in ({}, {'grounding_uuid': None}, {'grounding_uuid': ''}):
            for images in (None, _image_transport_receipt()):
                with self.subTest(optional_uuid=optional_uuid, images=images is not None):
                    with self.assertRaises(InvalidState):
                        c.archive.begin_predispatch(c.binding, 'conversation:1', {'type': 'runtime'},
                            native_anchor={**anchor, **optional_uuid},
                            grounding_runtime={'type': 'runtime', 'grounding_uuid': None,
                                               'grounding_sha256': 'd' * 64},
                            image_transport=images, attempt_id='attempt_one')
                    with c.archive.db() as db:
                        self.assertIsNone(db.execute('SELECT 1 FROM turns WHERE turn_id=?',
                                                     ('conversation:1',)).fetchone())

    def test_grounded_image_cleanup_rejects_missing_mismatched_and_reordered_evidence(self):
        gid = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        grounding = {'type': 'runtime', 'grounding_uuid': gid, 'grounding_sha256': 'd' * 64}
        image = {'type': 'runtime', 'image_transport': _image_transport_receipt()}
        suffixes = [
            [image],
            [{**grounding, 'grounding_uuid': 'cccccccc-dddd-4eee-8fff-000000000000'}, image],
            [{**grounding, 'extra': True}, image],
            [image, grounding],
            [grounding, image, {'type': 'runtime', 'model': 'not predispatch'}],
        ]
        for i, suffix in enumerate(suffixes):
            with self.subTest(case=i):
                sid = 'image_ground_invalid_' + str(i)
                self.service.create(sid, 'claude', self.root)
                c = self.service.connector(self.service.active(sid)['binding'])
                turn = sid + ':1'
                anchor = {'type': 'native_anchor', 'attempt_id': 'attempt_one', 'parent_uuid': '',
                          'store_revision': 'a' * 64, 'store_size': 0, 'bridge_protocol': 3,
                          'claude_version': '2.1.283', 'grounding_uuid': gid}
                c.archive.begin_predispatch(c.binding, turn, {'type': 'runtime', 'engine': 'claude'},
                    native_anchor=anchor, grounding_runtime=grounding, attempt_id='attempt_one')
                # Construct a hash-valid but schema-invalid optional suffix in
                # this temporary store, so rejection is not just hash checking.
                with c.archive.db() as db:
                    tail = db.execute('SELECT hash FROM events WHERE turn_id=? AND ordinal=2', (turn,)).fetchone()[0]
                    db.execute('DELETE FROM events WHERE turn_id=? AND ordinal>2', (turn,))
                    db.execute('UPDATE turns SET count=2,tail_hash=? WHERE turn_id=?', (tail, turn))
                c.archive.append_many(c.binding, turn, suffix)
                before = c.archive.read(c.binding, turn)['source']['events']
                with self.assertRaises(Conflict):
                    c.archive.abandon_predispatch(c.binding, turn, attempt_id='attempt_one')
                self.assertEqual(c.archive.read(c.binding, turn)['source']['events'], before)
                with c.archive.db() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM abandoned_turns WHERE turn_id=?',
                                                (turn,)).fetchone()[0], 0)

    def test_native_process_replays_cancel_after_registration_race(self):
        from shepherd.unified_runners import UnifiedRunner
        runner=object.__new__(UnifiedRunner)
        runner.connector=mock.Mock(native_cwd=str(self.root));runner.env={};runner.timeout=1
        runner.options={};runner.operation='turn';runner.on_process=None;runner.on_stall=None
        runner.execution=mock.Mock();runner.cancelled=threading.Event();runner.cancelled.set()
        process=mock.Mock()
        with mock.patch('shepherd.unified_runners.NativeProcess',return_value=process):
            self.assertIs(runner._native_process(['fixture'],{'type':'cancel'}),process)
        process.cancel.assert_called_once_with()

    def test_claude_oneshot_cancel_after_dispatch_never_sends_start(self):
        from shepherd.unified_runners import UnifiedRunner
        runner=object.__new__(UnifiedRunner);connector=mock.Mock(engine='claude',store_root=str(self.root),
            binding={'conversation_id':'conversation'})
        connector.native_cwd=str(self.root);connector.native_session_id='fixture-session'
        connector.encode.return_value=[];runner.connector=connector
        runner.env={'DESK_SHEPHERD_CLAUDE_BINARY':'/fixture/claude',
                    'DESK_SHEPHERD_CLAUDE_SDK_ROOT':'/fixture/sdk'}
        runner.options={};runner.directory=self.root;runner.system_text='';runner.user_frame=None
        runner.on_permission=None;runner.fallback_receipt=None;runner.cancelled=threading.Event()
        runner.round_id='conversation:1';runner.attempt_id='attempt-one';runner.checkpoint=None
        runner.on_event=None;runner._payload=mock.Mock(return_value='payload.json')
        process=mock.Mock(proc=object())
        process.events.return_value=iter([{'type':'prepared','bridgeProtocolVersion':3,
            'fallbackReceipt':None,'parentUuid':'','nativeRevision':'a'*64,
            'startSeq':0,'storeSize':0,'claudeVersion':'2.1.283','groundingUuid':''}])
        runner._native_process=mock.Mock(return_value=process)
        def dispatch(**_kwargs):runner.cancelled.set()
        with mock.patch('shepherd.claude_connector.ClaudeConnector.availability',
                        return_value={'available':True}), \
             self.assertRaisesRegex(Conflict,'cancelled before start'):
            runner._claude_oneshot('question',[],None,dispatch,None)
        process.cancel.assert_called_once_with();process.close.assert_called_once_with()
        self.assertNotIn(mock.call({'type':'start'}),process.send.call_args_list)

    def test_claude_oneshot_rejects_duplicate_prepared(self):
        from shepherd.unified_runners import UnifiedRunner
        runner=object.__new__(UnifiedRunner);connector=mock.Mock(engine='claude',store_root=str(self.root),
            binding={'conversation_id':'conversation'})
        connector.native_cwd=str(self.root);connector.native_session_id='fixture-session'
        connector.encode.return_value=[];runner.connector=connector
        runner.env={'DESK_SHEPHERD_CLAUDE_BINARY':'/fixture/claude',
                    'DESK_SHEPHERD_CLAUDE_SDK_ROOT':'/fixture/sdk'}
        runner.options={};runner.directory=self.root;runner.system_text='';runner.user_frame=None
        runner.on_permission=None;runner.fallback_receipt=None;runner.cancelled=threading.Event()
        runner.round_id='conversation:1';runner.attempt_id='attempt-one';runner.checkpoint=None
        runner.on_event=None;runner._payload=mock.Mock(return_value='payload.json')
        prepared={'type':'prepared','bridgeProtocolVersion':3,'fallbackReceipt':None,
            'parentUuid':'','nativeRevision':'a'*64,'startSeq':0,'storeSize':0,
            'claudeVersion':'2.1.283','groundingUuid':''}
        process=mock.Mock(proc=object());process.events.return_value=iter([prepared,prepared])
        runner._native_process=mock.Mock(return_value=process)
        with mock.patch('shepherd.claude_connector.ClaudeConnector.availability',
                        return_value={'available':True}), \
             self.assertRaisesRegex(Conflict,'duplicate prepared'):
            runner._claude_oneshot('question',[],None,mock.Mock(),None)
        self.assertEqual(process.send.call_args_list,[mock.call({'type':'start'})])
        process.close.assert_called_once_with()

    def test_claude_oneshot_rejects_archive_payload_before_ack(self):
        from shepherd.unified_runners import UnifiedRunner
        runner=object.__new__(UnifiedRunner);connector=mock.Mock(engine='claude',store_root=str(self.root),
            binding={'conversation_id':'conversation'})
        connector.native_cwd=str(self.root);connector.native_session_id='fixture-session'
        connector.encode.return_value=[];runner.connector=connector
        runner.env={'DESK_SHEPHERD_CLAUDE_BINARY':'/fixture/claude',
                    'DESK_SHEPHERD_CLAUDE_SDK_ROOT':'/fixture/sdk'}
        runner.options={};runner.directory=self.root;runner.system_text='';runner.user_frame=None
        runner.on_permission=None;runner.fallback_receipt=None;runner.cancelled=threading.Event()
        runner.round_id='conversation:1';runner.attempt_id='attempt-one';runner.checkpoint=None
        runner.on_event=None;runner._payload=mock.Mock(return_value='payload.json')
        process=mock.Mock(proc=object())
        process.events.return_value=iter([{'type':'archive','id':'archive-one',
            'entries':[{'cost':1.0}],'entryPayloads':['{"cost":2}']}])
        runner._native_process=mock.Mock(return_value=process)
        with mock.patch('shepherd.claude_connector.ClaudeConnector.availability',
                        return_value={'available':True}), \
             self.assertRaisesRegex(Conflict,'payload receipt'):
            runner._claude_oneshot('question',[],None,mock.Mock(),None)
        process.send.assert_not_called();process.close.assert_called_once_with()

    def test_claude_receipts_require_exact_store_boundaries(self):
        from shepherd.unified_runners import UnifiedRunner
        runner=object.__new__(UnifiedRunner);runner.attempt_id='attempt_one'
        base={'parentUuid':'','nativeRevision':'a'*64,'startSeq':3,'storeSize':3,
              'bridgeProtocolVersion':3,'claudeVersion':'2.1.283'}
        anchor=runner._claude_native_anchor(base)
        captured=['{"type":"user","cost":1}',
                  '{"type":"assistant","cost":1e-7}']
        terminal={'terminalState':'completed','startSeq':3,'endSeq':5,
                  'groundingUuid':'','nativeRevision':'b'*64,
                  'transitionRevision':runner._claude_transition_revision('a'*64,captured)}
        self.assertEqual(runner._claude_terminal_receipt(terminal,anchor,'',captured),'completed')
        for invalid in ({**base,'startSeq':2},
                        {**base,'startSeq':True,'storeSize':True}):
            with self.subTest(invalid=invalid),self.assertRaisesRegex(Conflict,'position mismatch'):
                runner._claude_native_anchor(invalid)
        for invalid in ({**base,'nativeRevision':'A'*64},
                        {**base,'bridgeProtocolVersion':'3'},
                        {**base,'claudeVersion':283}):
            with self.subTest(invalid=invalid),self.assertRaisesRegex(Conflict,'anchor mismatch'):
                runner._claude_native_anchor(invalid,3)
        with self.assertRaisesRegex(Conflict,'anchor mismatch'):
            runner._claude_native_anchor(base,4)
        for invalid in ({**terminal,'startSeq':2},
                        {**terminal,'endSeq':4},
                        {**terminal,'endSeq':True},
                        {**terminal,'terminalState':'future'},
                        {**terminal,'nativeRevision':'B'*64},
                        {**terminal,'transitionRevision':'c'*64},
                        {**terminal,'groundingUuid':'other'}):
            with self.subTest(invalid=invalid),self.assertRaisesRegex(Conflict,'terminal receipt'):
                runner._claude_terminal_receipt(invalid,anchor,'',captured)
        with self.assertRaisesRegex(Conflict,'terminal receipt'):
            runner._claude_terminal_receipt(terminal,anchor,'',captured[:-1])
        entries,payloads=runner._claude_archive_batch({'entries':[{'cost':1.0}],
            'entryPayloads':['{"cost":1.0}']})
        self.assertEqual(entries,[{'cost':1.0}]);self.assertEqual(payloads,['{"cost":1.0}'])
        with self.assertRaisesRegex(Conflict,'payload receipt'):
            runner._claude_archive_batch({'entries':[{'cost':1.0}],
                'entryPayloads':['{"cost":2}']})

    def test_predispatch_anchor_is_idempotent_for_same_binding(self):
        self.service.create('conversation','claude',self.root)
        c=self.service.connector(self.service.active('conversation')['binding'])
        runtime={'type':'runtime','engine':'claude','model':'fixture','effort':'high'}
        native={'type':'native_anchor','attempt_id':'attempt_one',
                'parent_uuid':'11111111-2222-4333-8444-555555555555',
                'store_revision':'a'*64,'store_size':7,
                'bridge_protocol':3,'claude_version':'2.1.283',
                'grounding_uuid':'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'}
        grounding_runtime={'type':'runtime','grounding_uuid':native['grounding_uuid'],
                           'grounding_sha256':'b'*64}
        c.archive.begin_predispatch(c.binding,'conversation:1',runtime,native_anchor=native,
            grounding_runtime=grounding_runtime,attempt_id='attempt_one')
        c.archive.begin_predispatch(c.binding,'conversation:1',runtime,native_anchor=native,
            grounding_runtime=grounding_runtime,attempt_id='attempt_one')
        source=c.archive.read(c.binding,'conversation:1')['source']
        self.assertEqual(source['events'],[runtime,native,grounding_runtime])
        self.assertEqual(c._public(source),[])
        self.assertEqual(c.source_compatibility(source),[])
        with c.archive.db() as db:
            turn=db.execute('SELECT count,terminal FROM turns WHERE turn_id=?',
                            ('conversation:1',)).fetchone()
            self.assertEqual((turn['count'],turn['terminal']),(3,None))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events WHERE turn_id=?',
                                        ('conversation:1',)).fetchone()[0],3)

    def test_predispatch_rejects_corrupted_hash_on_retry_or_abandon(self):
        self.service.create('conversation','claude',self.root)
        c=self.service.connector(self.service.active('conversation')['binding'])
        runtime={'type':'runtime','engine':'claude','model':'fixture','effort':''}
        anchor={'type':'native_anchor','attempt_id':'attempt_one','parent_uuid':'',
                'store_revision':'a'*64,'store_size':0,
                'bridge_protocol':3,'claude_version':'2.1.283'}
        c.archive.begin_predispatch(c.binding,'conversation:1',runtime,
            native_anchor=anchor,attempt_id='attempt_one')
        with c.archive.db() as db:
            db.execute("UPDATE events SET hash=? WHERE turn_id=? AND ordinal=2",
                       ('0'*64,'conversation:1'))
        with self.assertRaisesRegex(Conflict,'hash mismatch'):
            c.archive.begin_predispatch(c.binding,'conversation:1',runtime,
                native_anchor=anchor,attempt_id='attempt_one')
        with self.assertRaisesRegex(Conflict,'hash mismatch'):
            c.archive.abandon_predispatch(c.binding,'conversation:1',
                attempt_id='attempt_one')
        with c.archive.db() as db:
            self.assertIsNotNone(db.execute('SELECT 1 FROM turns WHERE turn_id=?',
                                            ('conversation:1',)).fetchone())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM abandoned_turns').fetchone()[0],0)

    def test_claude_runtime_only_cannot_unlock_current_attempt(self):
        self.service.create('conversation','claude',self.root)
        c=self.service.connector(self.service.active('conversation')['binding'])
        runtime={'type':'runtime','engine':'claude','model':'fixture','effort':''}
        c.archive.begin(c.binding,'conversation:1')
        c.archive.append(c.binding,'conversation:1',runtime)
        with self.assertRaisesRegex(Conflict,'attempt mismatch'):
            c.archive.abandon_predispatch(c.binding,'conversation:1',
                attempt_id='attempt_one')
        self.assertEqual(c.archive.read(c.binding,'conversation:1')['source']['events'],[runtime])

    def test_native_anchor_cross_checks_claude_suffix(self):
        self.service.create('conversation','claude',self.root)
        c=self.service.connector(self.service.active('conversation')['binding'])
        parent='11111111-2222-4333-8444-555555555555'
        runtime={'type':'runtime','engine':'claude','model':'fixture','effort':''}
        anchor={'type':'native_anchor','attempt_id':'attempt_one','parent_uuid':parent,
                'store_revision':'a'*64,'store_size':3,
                'bridge_protocol':3,'claude_version':'2.1.283'}
        entry={'type':'assistant','uuid':'bbbbbbbb-cccc-4ddd-8eee-ffffffffffff',
               'parentUuid':parent,'sessionId':c.native_session_id,'cwd':c.native_cwd,
               'version':'2.1.283','message':{'role':'assistant','content':[]}}
        source={'events':[runtime,anchor,{'type':'claude_entry','value':entry}]}
        self.assertEqual(c._public(source),[])
        for invalid in (
                {**anchor,'parent_uuid':'22222222-3333-4444-8555-666666666666'},):
            with self.subTest(invalid=invalid), self.assertRaises(SourceVerificationError):
                c._public({'events':[runtime,invalid,{'type':'claude_entry','value':entry}]})
        grounded={'type':'runtime','grounding_uuid':'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'}
        with self.assertRaisesRegex(SourceVerificationError,'grounding mismatch'):
            c._public({'events':[runtime,{**anchor,
                'grounding_uuid':'cccccccc-dddd-4eee-8fff-000000000000'},grounded]})

    def test_native_anchor_conflict_and_illegal_fields_fail_closed(self):
        self.service.create('conversation','claude',self.root)
        c=self.service.connector(self.service.active('conversation')['binding'])
        runtime={'type':'runtime','engine':'claude','model':'fixture','effort':''}
        anchor={'type':'native_anchor','attempt_id':'attempt_one','parent_uuid':'',
                'store_revision':'a'*64,'store_size':0,
                'bridge_protocol':3,'claude_version':'2.1.283'}
        c.archive.begin_predispatch(c.binding,'conversation:1',runtime,native_anchor=anchor,
                                    attempt_id='attempt_one')
        with self.assertRaises(Conflict):
            c.archive.begin_predispatch(c.binding,'conversation:1',runtime,
                native_anchor={**anchor,'store_size':1},attempt_id='attempt_one')
        with self.assertRaisesRegex(Conflict,'attempt mismatch'):
            c.archive.begin_predispatch(c.binding,'conversation:2',runtime,
                native_anchor=anchor,attempt_id='attempt_other')
        from shepherd.store import InvalidState
        for invalid in ({**anchor,'prompt':'private'},
                        {**anchor,'attempt_id':''},
                        {**anchor,'parent_uuid':'not a uuid'},
                        {**anchor,'store_revision':'short'},
                        {**anchor,'grounding_uuid':'not a uuid'},
                        {**anchor,'store_size':True},
                        {**anchor,'bridge_protocol':0},
                        {**anchor,'claude_version':''}):
            with self.subTest(invalid=invalid), self.assertRaises(InvalidState):
                c.archive.begin_predispatch(c.binding,'conversation:2',runtime,
                    native_anchor=invalid,attempt_id=invalid.get('attempt_id') or 'attempt_one')
        with self.assertRaises(InvalidState):
            c.archive.append(c.binding,'conversation:1',anchor)

    def test_rejected_dispatch_preserves_runtime_and_native_anchor(self):
        self.service.create('conversation','claude',self.root)
        first=self.service.connector(self.service.active('conversation')['binding'])
        runtime={'type':'runtime','engine':'claude','model':'old','effort':'high'}
        native={'type':'native_anchor','attempt_id':'attempt_old','parent_uuid':'',
                'store_revision':'b'*64,'store_size':0,
                'bridge_protocol':3,'claude_version':'2.1.283'}
        first.archive.begin_predispatch(first.binding,'conversation:1',runtime,
            native_anchor=native,attempt_id='attempt_old')
        first.archive.abandon_predispatch(first.binding,'conversation:1')
        with first.archive.db() as db:
            evidence=db.execute('SELECT * FROM abandoned_turns WHERE turn_id=?',
                                ('conversation:1',)).fetchone()
        self.assertEqual([row['payload'] for row in json.loads(evidence['events'])],
                         [runtime,native])

    def test_rejected_dispatch_preserves_anchor_before_new_binding_reuses_turn(self):
        self.service.create('conversation','codex',self.root)
        first=self.service.connector(self.service.active('conversation')['binding'])
        old_runtime={'type':'runtime','engine':'codex','model':'old','effort':'high'}
        first.archive.begin_predispatch(first.binding,'conversation:1',old_runtime)
        first.archive.abandon_predispatch(first.binding,'conversation:1')
        self.service.switch('conversation','pi',self.root)
        second=self.service.connector(self.service.active('conversation')['binding'])
        new_runtime={'type':'runtime','engine':'pi','model':'new','effort':'high'}
        second.archive.begin_predispatch(second.binding,'conversation:1',new_runtime)
        self.assertEqual(second.archive.read(second.binding,'conversation:1')['source']['events'],
                         [new_runtime])
        with second.archive.db() as db:
            evidence=db.execute('SELECT * FROM abandoned_turns WHERE turn_id=?',
                                ('conversation:1',)).fetchone()
            self.assertIsNotNone(evidence)
            self.assertEqual(json.loads(evidence['identity'])['binding_id'],
                             first.binding['binding_id'])
            self.assertEqual(json.loads(evidence['events'])[0]['payload'],old_runtime)
            self.assertEqual(evidence['reason'],'dispatch_fence_rejected')

    def test_anchor_with_native_evidence_cannot_be_abandoned_or_replaced(self):
        self.service.create('conversation','codex',self.root)
        first=self.service.connector(self.service.active('conversation')['binding'])
        first.archive.begin_predispatch(first.binding,'conversation:1',
            {'type':'runtime','engine':'codex','model':'old','effort':'high'})
        first.archive.append(first.binding,'conversation:1',
            {'type':'codex_item','value':{'type':'message','role':'assistant','content':[]}})
        with self.assertRaisesRegex(Conflict,'turn identity mismatch'):
            first.archive.abandon_predispatch(first.binding,'conversation:1')
        self.service.switch('conversation','pi',self.root)
        second=self.service.connector(self.service.active('conversation')['binding'])
        with self.assertRaisesRegex(Conflict,'turn identity mismatch'):
            second.archive.begin_predispatch(second.binding,'conversation:1',
                {'type':'runtime','engine':'pi','model':'new','effort':'high'})
        with first.archive.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM abandoned_turns').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count FROM turns WHERE turn_id=?',
                                        ('conversation:1',)).fetchone()[0],2)

    def test_working_context_and_terminal_commit_are_atomic(self):
        self.service.create('conversation','codex',self.root)
        c=self.service.connector(self.service.active('conversation')['binding'])
        c.archive.begin(c.binding,'conversation:1')
        c.archive.append(c.binding,'conversation:1',{'type':'runtime'})
        from shepherd.store import InvalidState
        with self.assertRaises(InvalidState):
            c.archive.finish(c.binding,'conversation:1','completed',working_context={
                'key':'context','through_seq':2,'native':{'items':[]}})
        self.assertIsNone(c.archive.read(c.binding,'conversation:1')['terminal'])
        self.assertIsNone(c.archive.checkpoint('context'))
        revision=c.archive.finish(c.binding,'conversation:1','completed',working_context={
            'key':'context','through_seq':1,'native':{'items':[]}})
        self.assertEqual(c.archive.checkpoint('context')['payload']['source_hash'],revision)
        self.assertEqual(c.archive.read(c.binding,'conversation:1')['terminal'],'completed')

    def test_uncommitted_working_context_cache_is_ignored(self):
        c,row=self.turn('pi',1)
        key,_,_=c.context({'model':'fixture'})
        c.archive.checkpoint(key,turn_id=row['round_id'],through_seq=1,payload={
            'native':[],'source_hash':row['source_hash']})
        # The archive seals Working Context before the cross-store Coordinator
        # commit. A later verification failure quarantines the ledger turn but
        # cannot atomically retract the already sealed cache.
        from shepherd.turn_state import turn_hash
        with self.service.store.change('conversation') as state:
            stale=state['turns'][0]
            stale.update(outcome='unknown',history_state='omitted',
                         recovery_reason='commit_verification_failed')
            for field in ('source_ref','source_key','source_hash','public_hash'):
                stale.pop(field,None)
            stale['turn_hash']=turn_hash(stale)
            binding=state['bindings'][c.binding_id]
            binding['imported_turn_hash']=stale['turn_hash']
        same_key,checkpoint,tail=c.context({'model':'fixture'})
        self.assertEqual(same_key,key)
        self.assertIsNone(checkpoint)
        self.assertEqual(len(tail),1)
        self.assertEqual(tail[0]['outcome'],'unknown')
        self.assertEqual(tail[0]['history_state'],'omitted')

    def test_committed_working_context_hash_mismatch_still_fails_closed(self):
        c,row=self.turn('pi',1)
        key,_,_=c.context({'model':'fixture'})
        c.archive.checkpoint(key,turn_id=row['round_id'],through_seq=1,payload={
            'native':[],'source_hash':'f'*64})
        with self.assertRaisesRegex(SourceVerificationError,'source hash mismatch'):
            c.context({'model':'fixture'})

    def test_claude_checkpoint_restart_projects_only_tail(self):
        rows=[]
        for seq in range(1,9):
            c,row=self.turn('claude',seq);rows.append(row)
        key,_,all_rounds=c.context({'model':'fixture'})
        self.assertEqual(len(all_rounds),8)
        checkpoint_native=[
            {'type':'attachment','uuid':'snapshot','attachment':{
                'type':'prompt_snapshot','systemPrompt':['fixture']}},
            {'type':'system','subtype':'compact_boundary','uuid':'boundary','parentUuid':'snapshot'},
            {'type':'user','uuid':'summary','parentUuid':'boundary','isCompactSummary':True,
             'message':{'role':'user','content':'summary'}}]
        c.archive.checkpoint(key,turn_id='conversation:6',through_seq=6,payload={
            'native':checkpoint_native,'source_hash':rows[5]['source_hash']})
        same_key,checkpoint,tail=c.context({'model':'fixture','effort':'different',
                                            '_system_hash':'new-prompt'})
        self.assertEqual(same_key,key)
        self.assertEqual(checkpoint['through_seq'],6)
        self.assertEqual(checkpoint['payload']['native'],checkpoint_native)
        self.assertEqual([row['offset'] for row in tail],[7,8])

    def test_late_checkpoint_cannot_rewind_or_replace_same_boundary(self):
        c,_=self.turn('pi',1)
        c.archive.checkpoint('context',turn_id='conversation:2',through_seq=2,payload={'native':['new']})
        for seq,payload in ((1,{'native':['old']}),(2,{'native':['changed']})):
            with self.assertRaises(Conflict):
                c.archive.checkpoint('context',turn_id=f'conversation:{seq}',through_seq=seq,payload=payload)
        self.assertEqual(c.archive.checkpoint('context')['payload'],{'native':['new']})

    def test_crash_after_context_commit_recovers_receipt_without_model_replay(self):
        from shepherd.recovery import ReceiptRecoveryRunner
        self.service.create('conversation','codex',self.root)
        active=self.service.active('conversation'); lease=active['lease']
        c=self.service.connector(active['binding'])
        key,_,_=c.context({'model':'fixture'})
        from shepherd.recovery import BOOT_ID
        turn=self.service.coordinator.begin_round('conversation',lease['lease_id'],lease['generation'],
            recovery={'phase':'reserved','boot_id':BOOT_ID,'boundary':c.recovery_boundary()})
        self.service.coordinator.mark_dispatched('conversation',lease['lease_id'],lease['generation'],
                                                attempt_id=turn['attempt_id'])
        c.archive.begin(c.binding,'conversation:1')
        c.archive.append(c.binding,'conversation:1',{'type':'codex_item','value':{
            'type':'message','role':'assistant','content':[{'type':'output_text','text':'done'}]}})
        c.archive.finish(c.binding,'conversation:1','completed',working_context={
            'key':key,'through_seq':1,'native':{'items':[]}})
        restarted=Service(self.root,storage='unified')
        rebuilt=restarted.connector(active['binding'])
        with mock.patch('codex_appserver.run_turn',side_effect=AssertionError('must not replay')):
            row=ExecutionHarness(restarted.coordinator,rebuilt,ReceiptRecoveryRunner(rebuilt)).recover(
                'conversation',lease_id=lease['lease_id'],generation=lease['generation'])
        self.assertEqual(row['terminal_state'],'completed')
        self.assertEqual(rebuilt.context({'model':'fixture'})[1]['through_seq'],1)

    def test_incomplete_custom_tool_preserves_source_and_has_unknown_output(self):
        self.service.create('conversation','codex',self.root)
        c=self.service.connector(self.service.active('conversation')['binding'])
        boundary=c.recovery_boundary();c.archive.begin(c.binding,'conversation:1')
        c.archive.append(c.binding,'conversation:1',{'type':'codex_item','value':{
            'type':'custom_tool_call','call_id':'unfinished','name':'exec','input':'text(1)'}})
        before=c.archive.read(c.binding,'conversation:1')
        recovered=c.recover_suffix(boundary,{'turn_id':'conversation:1'})
        self.assertEqual(recovered['unfinished_tool_ids'],['unfinished'])
        self.assertIn('[执行状态未知]',recovered['public'][-1]['output'])
        self.assertEqual(c.archive.read(c.binding,'conversation:1'),before)

    def test_codex_full_history_budget_blocks_before_native_dispatch(self):
        c,row=self.turn('codex',1)
        with self.assertRaisesRegex(Conflict,'上下文窗口未知'):
            c.context({'model':'fixture','_require_context_budget':True})
        with self.assertRaisesRegex(Conflict,'没有可用的降级上下文空间'):
            c.context({'model':'fixture','_context_window':10,
                       '_require_context_budget':True,'_current_tokens':100})
        # Read-only callers may still inspect the selected history without
        # pretending that an unknown window is safe for execution.
        _key,checkpoint,rounds=c.context({'model':'fixture'})
        self.assertIsNone(checkpoint);self.assertEqual(len(rounds),1)

    def test_codex_oversized_tail_uses_bounded_inert_fallback(self):
        c,first=self.turn('codex',1)
        active=self.service.active('conversation');lease=active['lease']
        large=[{'type':'message','role':'user','content':[{
                   'type':'input_text','text':'recent verified question'}]},
               {'type':'function_call','call_id':'large','name':'dangerous_old_tool',
                   'arguments':'{\"secret\":\"'+('x'*40000)+'\"}'},
               {'type':'function_call_output','call_id':'large','output':'sensitive output '*40000},
               {'type':'message','role':'assistant','content':[{
                   'type':'output_text','text':'recent verified answer'}]}]
        second=ExecutionHarness(self.service.coordinator,c,JournalRunner(c,large)).run(
            'conversation',lease_id=lease['lease_id'],generation=lease['generation'],
            prompt='question',input_public=[{'type':'message','role':'user','content':[{
                'type':'input_text','text':'question'}]}])
        # A small target window forces both verified rounds through the
        # extractive fallback. Tool calls and outputs must not survive it.
        key,checkpoint,tail=c.context({'model':'fixture','_context_window':10000,
                                      '_require_context_budget':True,
                                      '_history_hint':'history search, then bounded history read, then verify citation'})
        self.assertEqual(checkpoint['through_seq'],2);self.assertEqual(tail,[])
        native=checkpoint['payload']['native']
        self.assertTrue(native['degraded_checkpoint_id'].startswith('degraded_'))
        self.assertEqual(native['portable_checkpoint_id'],native['degraded_checkpoint_id'])
        self.assertLess(native['estimated_tokens'],1808)
        text=json.dumps(native['items'],ensure_ascii=False)
        self.assertIn('bounded fallback context',text)
        self.assertIn('history search, then bounded history read, then verify citation',text)
        self.assertIn('dangerous_old_tool',text)
        self.assertNotIn('sensitive output',text)
        self.assertNotIn('historical result',text)
        self.assertNotIn('function_call',text)
        # The durable serial ledger and exact archive remain unchanged.
        self.assertEqual(len(self.service.coordinator.state('conversation')['turns']),2)
        self.assertEqual(c.archive.read(c.binding,second['round_id'])['terminal'],'completed')

    def test_degraded_context_can_cover_omitted_boundary_without_source_hash(self):
        self.service.create('conversation','codex',self.root)
        active=self.service.active('conversation');lease=active['lease']
        c=self.service.connector(active['binding'])
        large=[{'type':'message','role':'user','content':[{
                   'type':'input_text','text':'oversized prior question'}]},
               {'type':'function_call','call_id':'large','name':'old_tool','arguments':'{}'},
               {'type':'function_call_output','call_id':'large','output':'x'*100000},
               {'type':'message','role':'assistant','content':[{
                   'type':'output_text','text':'oversized prior answer'}]}]
        ExecutionHarness(self.service.coordinator,c,JournalRunner(c,large)).run(
            'conversation',lease_id=lease['lease_id'],generation=lease['generation'],
            prompt='question',input_public=[{'type':'message','role':'user','content':[{
                'type':'input_text','text':'question'}]}])
        with self.service.store.change('conversation') as state:
            active={'turn_id':'conversation:2','seq':2,'kind':'user_turn',
                    'attempt_id':'attempt_omitted','producer_binding_id':c.binding_id,
                    'producer_generation':state['producer_generation'],'phase':'dispatched',
                    'outcome':'unknown','history_state':'pending','unfinished_tool_ids':[],
                    'started_at':1.0}
            from shepherd.turn_state import settle as settle_turn_row
            omitted=settle_turn_row(active,outcome='unknown',history_state='omitted',
                                    recovery_reason='commit_verification_failed')
            state['turns'].append(omitted)
            state['bindings'][c.binding_id]['imported_through_seq']=2
            state['bindings'][c.binding_id]['imported_turn_hash']=omitted['turn_hash']
        _,checkpoint,tail=c.context({'model':'fixture','_context_window':8700,
            '_require_context_budget':True})
        self.assertEqual(checkpoint['through_seq'],2);self.assertEqual(tail,[])
        self.assertEqual(checkpoint['payload']['source_hash'], omitted['turn_hash'])
        text=json.dumps(checkpoint['payload']['native']['items'],ensure_ascii=False)
        self.assertIn('本轮内容已隔离',text)

    def test_degraded_context_redacts_credentials_in_text_tool_names_and_hint(self):
        c,first=self.turn('codex',1)
        active=self.service.active('conversation');lease=active['lease']
        secret='sk-abcdefghijklmnop';github='ghp_'+'a'*24;aws='AKIA'+'A'*16
        pem='-----BEGIN PRIVATE KEY----- hidden -----END PRIVATE KEY-----'
        large=[{'type':'message','role':'user','content':[{
                   'type':'input_text','text':'API_KEY=secret-value '+secret+' Basic dXNlcjpwYXNz '+github}]},
               {'type':'function_call','call_id':'s','name':aws,'arguments':'{}'},
               {'type':'function_call_output','call_id':'s','output':'x'*100000},
               {'type':'message','role':'assistant','content':[{
                   'type':'output_text','text':'bearer abc.def.ghi '+pem+' https://u:p@example.com'}]}]
        ExecutionHarness(self.service.coordinator,c,JournalRunner(c,large)).run(
            'conversation',lease_id=lease['lease_id'],generation=lease['generation'],
            prompt='question',input_public=[{'type':'message','role':'user','content':[{
                'type':'input_text','text':'question'}]}])
        _,checkpoint,_=c.context({'model':'fixture','_context_window':10000,
            '_require_context_budget':True,'_history_hint':'token=hint-secret retrieve search'})
        text=json.dumps(checkpoint['payload']['native']['items'],ensure_ascii=False)
        for value in (secret,'secret-value','abc.def.ghi','hint-secret','dXNlcjpwYXNz',
                      github,aws,'hidden','u:p@'):
            self.assertNotIn(value,text)
        self.assertIn('[redacted]',text);self.assertIn('retrieve search',text)

    def test_oversized_exact_checkpoint_falls_back_to_recent_verified_round(self):
        c,row=self.turn('codex',1)
        key,_,_=c.context({'model':'fixture'})
        c.archive.checkpoint(key,turn_id=row['round_id'],through_seq=1,payload={
            'native':{'model':'fixture','items':[{'type':'message','role':'assistant',
                'content':[{'type':'output_text','text':'oversized '+('x'*100000)}]}]},
            'source_hash':row['source_hash']})
        _,checkpoint,tail=c.context({'model':'fixture','_context_window':10000,
                                     '_require_context_budget':True})
        text=json.dumps(checkpoint['payload']['native']['items'],ensure_ascii=False)
        self.assertEqual(checkpoint['through_seq'],1);self.assertEqual(tail,[])
        self.assertIn('done',text)
        self.assertIn('tools=read',text)
        self.assertNotIn('historical result',text)
        self.assertNotIn('x'*1000,text)

    def test_codex_compacted_checkpoint_budget_ignores_audit_prefix(self):
        c,row=self.turn('codex',1)
        key,_,_=c.context({'model':'fixture'})
        c.archive.checkpoint(key,turn_id=row['round_id'],through_seq=1,payload={
            'native':{'format':'codex-working-context-v1','compacted':True,
                'model':'fixture','items':[
                    {'type':'message','role':'user','content':[{
                        'type':'input_text','text':'old audit '*50000}]},
                    {'type':'compaction','encrypted_content':'opaque'},
                    {'type':'message','role':'user','content':[{
                        'type':'input_text','text':'retained'}]}]},
            'source_hash':row['source_hash']})
        _,checkpoint,tail=c.context({'model':'fixture','_context_window':20000,
                                    '_require_context_budget':True})
        self.assertEqual(checkpoint['through_seq'],1);self.assertEqual(tail,[])
        with self.assertRaisesRegex(Conflict,'没有可用的降级上下文空间'):
            c.context({'model':'fixture','_context_window':10,
                       '_require_context_budget':True})

    def test_claude_checkpoint_with_oversized_tail_degrades_for_codex(self):
        claude,first=self.turn('claude',1)
        ckey,_,_=claude.context({'model':'opus'})
        common={'sessionId':claude.native_session_id,'cwd':claude.native_cwd,
                'version':'2.1.268','timestamp':'2026-09-15T00:00:00.000Z'}
        boundary={**common,'type':'system','subtype':'compact_boundary','uuid':'b',
            'parentUuid':None,'logicalParentUuid':'prior','content':'Conversation compacted',
            'level':'info','compactMetadata':{'trigger':'manual','preTokens':100,
            'postTokens':20,'cumulativeDroppedTokens':80,'durationMs':1}}
        summary={**common,'type':'user','uuid':'s','parentUuid':'b',
            'isCompactSummary':True,'isVisibleInTranscriptOnly':True,
            'message':{'role':'user','content':'verified earlier summary'}}
        claude.archive.checkpoint(ckey,turn_id=first['round_id'],through_seq=1,
            payload={'native':[boundary,summary],'source_hash':first['source_hash']})
        active=self.service.active('conversation');lease=active['lease']
        large=[{'type':'message','role':'user','content':[{'type':'input_text','text':'latest question'}]},
               {'type':'function_call','call_id':'large','name':'old_tool','arguments':'{}'},
               {'type':'function_call_output','call_id':'large','output':'x'*100000},
               {'type':'message','role':'assistant','content':[{'type':'output_text','text':'latest answer'}]}]
        second=ExecutionHarness(self.service.coordinator,claude,JournalRunner(claude,large)).run(
            'conversation',lease_id=lease['lease_id'],generation=lease['generation'],
            prompt='question',input_public=[{'type':'message','role':'user','content':[{
                'type':'input_text','text':'question'}]}])
        self.service.switch('conversation','codex',self.root)
        target=self.service.connector(self.service.active('conversation')['binding'])
        _,checkpoint,tail=target.context({'model':'fixture','_context_window':10000,
                                         '_require_context_budget':True})
        native=checkpoint['payload']['native'];text=json.dumps(native['items'],ensure_ascii=False)
        self.assertEqual(checkpoint['through_seq'],2);self.assertEqual(tail,[])
        self.assertTrue(native['degraded_checkpoint_id'].startswith('degraded_'))
        self.assertIn('verified earlier summary',text)
        self.assertIn('latest question',text);self.assertIn('latest answer',text)
        self.assertNotIn('x'*1000,text);self.assertNotIn('function_call',text)

    def test_checkpoint_is_scoped_to_model_and_engine(self):
        c,row=self.turn('pi',1)
        key,_,_=c.context({'model':'large','effort':'high'})
        c.archive.checkpoint(key,turn_id='conversation:1',through_seq=1,payload={'native':[],'source_hash':row['source_hash']})
        self.assertIsNotNone(c.context({'model':'large','effort':'high'})[1])
        self.assertIsNotNone(c.context({'model':'large','effort':'low',
            '_context_window':100000,'_system_hash':'current-prompt'})[1])
        self.assertIsNone(c.context({'model':'small','effort':'high'})[1])
        self.service.switch('conversation','claude',self.root)
        other=self.service.connector(self.service.active('conversation')['binding'])
        self.assertIsNone(other.context({'model':'large','effort':'high'})[1])

    def blocked_unified_turn(self):
        self.service.create('conversation','codex',self.root)
        active=self.service.active('conversation'); lease=active['lease']
        c=self.service.connector(active['binding'])
        turn=self.service.coordinator.begin_round('conversation',lease['lease_id'],lease['generation'],
            recovery={'phase':'reserved','boot_id':'old','boundary':c.recovery_boundary()})
        self.service.coordinator.mark_dispatched('conversation',lease['lease_id'],lease['generation'],
            attempt_id=turn['attempt_id'],process={'pid':99999998,'pgid':99999998,'start_token':'old'},
            native_locator={'turn_id':turn['round_id']})
        c.archive.begin(c.binding,turn['round_id'])
        c.archive.append(c.binding,turn['round_id'],{'type':'codex_item','value':{
            'type':'custom_tool_call','call_id':'unfinished','name':'exec','input':'already dispatched'}})
        self.service.coordinator.mark_unresolved('conversation',lease['lease_id'],lease['generation'],
            attempt_id=turn['attempt_id'],reason='native_evidence_incomplete')
        return c,turn

    def test_codex_model_switch_uses_latest_verified_context_without_relabeling(self):
        c,first=self.turn('codex',1)
        akey,_,_=c.context({'model':'sol','provider':'same'})
        c.archive.checkpoint(akey,turn_id=first['round_id'],through_seq=1,payload={
            'native':{'model':'sol','items':[{'type':'compaction','encrypted_content':'opaque-sol'}]},
            'source_hash':first['source_hash']})
        bkey,borrowed,tail=c.context({'model':'astra','provider':'same'})
        self.assertEqual(borrowed['through_seq'],1)
        self.assertEqual(borrowed['payload']['native']['model'],'sol')
        self.assertEqual(tail,[])
        self.assertIsNone(c.archive.checkpoint(bkey))
        # A subsequent successful Astra turn is newer than the old Sol cache.
        c,second=self.turn('codex',2)
        c.archive.checkpoint(bkey,turn_id=second['round_id'],through_seq=2,payload={
            'native':{'model':'astra','items':[{'type':'compaction','encrypted_content':'opaque-astra'}]},
            'source_hash':second['source_hash']})
        _,back,tail=c.context({'model':'sol','provider':'same'})
        self.assertEqual(back['through_seq'],2)
        self.assertEqual(back['payload']['native']['model'],'astra')
        self.assertEqual(tail,[])
        self.assertEqual(c.archive.checkpoint(akey)['through_seq'],1)
        self.assertIsNone(c.context({'model':'astra','provider':'different'})[1])

    def test_newer_claude_portable_replaces_older_codex_cache(self):
        codex,first=self.turn('codex',1)
        key,_,_=codex.context({'model':'fixture'})
        codex.archive.checkpoint(key,turn_id=first['round_id'],through_seq=1,payload={
            'native':{'model':'fixture','items':[{'type':'message','role':'user',
                'content':[{'type':'input_text','text':'old codex cache'}]}]},
            'source_hash':first['source_hash']})
        claude,second=self.turn('claude',2)
        common={'sessionId':claude.native_session_id,'cwd':claude.native_cwd,
                'version':'2.1.268','timestamp':'2026-09-15T00:00:00.000Z'}
        boundary={**common,'type':'system','subtype':'compact_boundary',
            'content':'Conversation compacted','level':'info','uuid':'boundary','parentUuid':None,
            'logicalParentUuid':'prior','compactMetadata':{'trigger':'manual','preTokens':100,
            'postTokens':20,'cumulativeDroppedTokens':80,'durationMs':1}}
        summary={**common,'type':'user','uuid':'summary','parentUuid':'boundary',
            'isCompactSummary':True,'isVisibleInTranscriptOnly':True,
            'message':{'role':'user','content':'new portable summary'}}
        claude.archive.checkpoint('claude-context',turn_id=second['round_id'],through_seq=2,
            payload={'native':[boundary,summary],'source_hash':second['source_hash']})
        self.service.switch('conversation','codex',self.root)
        target=self.service.connector(self.service.active('conversation')['binding'])
        _,checkpoint,tail=target.context({'model':'fixture','_context_window':100000})
        self.assertEqual(checkpoint['through_seq'],2)
        self.assertTrue(checkpoint['payload']['native']['portable_checkpoint_id'].startswith('portable_'))
        self.assertEqual(tail,[])
        with self.assertRaisesRegex(Conflict,'没有可用的降级上下文空间'):
            target.context({'model':'fixture','_context_window':100,
                            '_require_context_budget':True})
        with self.assertRaisesRegex(Conflict,'上下文窗口未知'):
            target.context({'model':'fixture','_require_context_budget':True})

    def test_borrowed_codex_checkpoint_requires_verified_ledger_boundary(self):
        c,row=self.turn('codex',1)
        key,_,_=c.context({'model':'sol'})
        c.archive.checkpoint(key,turn_id=row['round_id'],through_seq=1,payload={
            'native':{'model':'sol','items':[]},'source_hash':'tampered'})
        with self.assertRaises(SourceVerificationError): c.context({'model':'astra'})

    def test_old_unified_quarantine_revalidates_without_resending(self):
        from shepherd.recovery_controller import RecoveryController
        c,turn=self.blocked_unified_turn()
        before=c.archive.read(c.binding,turn['round_id'])
        controller=RecoveryController(self.service,boot_id='new')
        with mock.patch('codex_appserver.run_turn',side_effect=AssertionError('must not execute')):
            report=controller.recover_guarded('conversation',trigger='startup')
        self.assertEqual(report['outcome'],'recovered_incomplete')
        state=self.service.coordinator.state('conversation')
        self.assertIsNone(state['inflight'])
        self.assertEqual(state['rounds'][0]['terminal_state'],'unknown')
        self.assertNotEqual(state['active_binding_id'],c.binding_id)
        self.assertEqual(c.archive.read(c.binding,turn['round_id']),before)
        self.assertEqual(controller.recover_guarded('conversation',trigger='send_before')['outcome'],'not_applicable')

    def test_blocked_peer_return_recovers_automatically_without_native_replay(self):
        from shepherd.recovery_controller import RecoveryController
        c, turn = self.blocked_unified_turn()
        c.archive.append(c.binding, turn['round_id'], {'type': 'codex_item', 'value': {
            'type': 'agent_message', 'author': '/root/reviewer', 'recipient': '/root',
            'content': [{'type': 'input_text', 'text': 'preserved child result'},
                        {'type': 'encrypted_content', 'encrypted_content': 'private'}]}})
        before = c.archive.read(c.binding, turn['round_id'])
        with mock.patch('codex_appserver.run_turn', side_effect=AssertionError('must not replay')):
            report = RecoveryController(self.service).recover_guarded('conversation', trigger='startup')
        self.assertEqual(report['outcome'], 'recovered_incomplete')
        self.assertEqual(c.archive.read(c.binding, turn['round_id']), before)
        state = self.service.coordinator.state('conversation')
        self.assertIsNone(state['inflight'])
        self.assertNotEqual(state['active_binding_id'], c.binding_id)
        resolved = c.resolve_source(state['rounds'][0]['source'])
        self.assertIn('preserved child result', json.dumps(resolved['public']))
        self.assertIn('projected_peer_message', json.dumps(report['compatibility']))
        self.assertIn('private', json.dumps(resolved['source']))
        self.assertNotIn('private', json.dumps(resolved['public']))

    def test_unstaged_terminal_recovers_commit_without_unknown_or_replay(self):
        from shepherd.recovery_controller import RecoveryController
        for has_receipt in (False, True):
            with self.subTest(has_receipt=has_receipt), tempfile.TemporaryDirectory() as root:
                self.root = Path(root)
                self.service = Service(self.root, storage='unified')
                c, turn = self.blocked_unified_turn()
                c.archive.append(c.binding, turn['round_id'], {'type': 'codex_item', 'value': {
                    'type': 'custom_tool_call_output', 'call_id': 'unfinished', 'output': 'done'}})
                revision = c.archive.finish(c.binding, turn['round_id'], 'completed')
                receipt = (c.receipts.write(c.binding, turn['round_id'],
                    c.terminal_locator(turn['round_id'], 'completed', revision)) if has_receipt else None)
                before = c.archive.read(c.binding, turn['round_id'])
                with mock.patch('codex_appserver.run_turn', side_effect=AssertionError('must not replay')), \
                     mock.patch('shepherd.unified_connector.UnifiedConnector.recover_suffix',
                                side_effect=AssertionError('must not downgrade')):
                    report = RecoveryController(self.service).recover_guarded('conversation', trigger='startup')
                self.assertEqual(report['outcome'], 'committed')
                state = self.service.coordinator.state('conversation')
                self.assertIsNone(state['inflight'])
                self.assertEqual(state['rounds'][0]['terminal_state'], 'completed')
                self.assertEqual(state['active_binding_id'], c.binding_id)
                self.assertEqual(c.archive.read(c.binding, turn['round_id']), before)
                after = c.receipts.read(c.binding, turn['round_id'])
                self.assertEqual(after['locator']['terminal_state'], 'completed')
                if receipt:
                    self.assertEqual(after, receipt)

    def staged_blocked_turn(self, input_public=None):
        c, turn = self.blocked_unified_turn()
        if input_public is not None:
            reference = TurnInputs(self.root).write('conversation', turn['round_id'], input_public)
            self.service.coordinator.attach_input_ref(
                'conversation', attempt_id=turn['attempt_id'], input_ref=reference)
            for value in c.encode({'public':input_public, 'round_id':turn['round_id'],
                                   'offset':1, 'entry_hash':'a'*64}):
                c.archive.append(c.binding, turn['round_id'], {'type':'codex_item','value':value})
        c.archive.append(c.binding, turn['round_id'], {'type': 'codex_item', 'value': {
            'type': 'custom_tool_call_output', 'call_id': 'unfinished', 'output': 'already done'}})
        revision = c.archive.finish(c.binding, turn['round_id'], 'completed')
        c.receipts.write(c.binding, turn['round_id'], c.terminal_locator(turn['round_id'], 'completed', revision))
        lease = self.service.active('conversation')['lease']
        self.service.coordinator.source_persisted('conversation', lease['lease_id'], lease['generation'],
            attempt_id=turn['attempt_id'], source=c._ref(turn['round_id']), native_revision=revision,
            terminal_state='completed')
        self.service.coordinator.block_staged_source('conversation', lease['lease_id'], lease['generation'],
            attempt_id=turn['attempt_id'], reason='commit_verification_failed', detail='old decoder')
        return c, turn

    def test_fixed_staged_projection_retries_commit_without_replaying_or_changing_terminal(self):
        from shepherd.recovery_controller import RecoveryController
        c, turn = self.staged_blocked_turn()
        before = c.archive.read(c.binding, turn['round_id'])
        receipt = c.receipts.read(c.binding, turn['round_id'])
        with mock.patch('codex_appserver.run_turn', side_effect=AssertionError('must not replay')):
            report = RecoveryController(self.service).recover_guarded('conversation', trigger='startup')
        self.assertEqual(report['outcome'], 'committed')
        self.assertEqual(c.archive.read(c.binding, turn['round_id']), before)
        self.assertEqual(c.receipts.read(c.binding, turn['round_id']), receipt)
        state = self.service.coordinator.state('conversation')
        self.assertIsNone(state['inflight'])
        self.assertEqual(state['rounds'][0]['terminal_state'], 'completed')

    def test_blocked_large_image_input_recovers_and_cold_history_remains_verifiable(self):
        import base64
        from shepherd.image_content import image_block
        from shepherd.recovery_controller import RecoveryController
        from shepherd.requests import Requests
        from shepherd.coordinator import digest
        # Synthetic JPEG protocol fixture, below the existing per-image quota;
        # the combined base64 projection exceeds the former 16 MiB read limit.
        image = image_block(base64.b64encode(
            b'\xff\xd8\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00'
            + b'x'*(7*1024*1024) + b'\xff\xd9').decode(), 'image/jpeg')
        user = TurnInputs.public_input('two images', [image, image])
        c, turn = self.staged_blocked_turn(input_public=user)
        state = self.service.coordinator.state('conversation')
        reference = state['inflight']['input_ref']
        path = Path(TurnInputs(self.root).reference_path(reference))
        self.assertGreater(path.stat().st_size, 16*1024*1024)
        original = hashlib.sha256(path.read_bytes()).hexdigest()
        receipt = c.receipts.read(c.binding, turn['round_id'])
        requests = Requests(self.root)
        requests.claim('conversation','large_request',{'text':'two images'})
        requests.update('conversation','large_request','uncertain',attempt_id=turn['attempt_id'])
        before_revision = digest(c.archive.read(c.binding, turn['round_id']))
        with mock.patch('codex_appserver.run_turn', side_effect=AssertionError('must not replay')):
            report = RecoveryController(self.service,requests,boot_id='upgraded').recover_guarded(
                'conversation',trigger='startup')
        self.assertEqual(report['outcome'], 'committed')
        self.assertEqual(requests.get('conversation','large_request')['status'], 'finished')
        self.assertEqual(c.receipts.read(c.binding, turn['round_id']), receipt)
        self.assertEqual(digest(c.archive.read(c.binding, turn['round_id'])), before_revision)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original)
        self.assertEqual(TurnInputs(self.root).read(reference), user)
        # Use a fresh Service/Connector, not a resident model or in-memory projection.
        cold = Service(self.root, storage='unified')
        binding = cold.active('conversation')['binding']; reader = cold.connector(binding)
        projected = ReadProjector(cold.store,SourceResolver([reader])).project('conversation')
        user_hashes = [digest(item) for item in projected[0]['public']
                       if item.get('type') == 'message' and item.get('role') == 'user']
        self.assertIn(digest(user[0]), user_hashes)
        self.assertEqual(projected[0]['terminal_state'], 'completed')
        accepted, fresh = requests.claim('conversation','next_request',{'text':'continue'})
        self.assertTrue(fresh)
        self.assertEqual(accepted['status'], 'accepted')

    def test_unstaged_commit_verification_rechecks_after_decoder_fix(self):
        from shepherd.recovery_controller import RecoveryController
        c, turn = self.blocked_unified_turn()
        c.archive.append(c.binding, turn['round_id'], {'type': 'codex_item', 'value': {
            'type': 'custom_tool_call_output', 'call_id': 'unfinished', 'output': 'already done'}})
        revision = c.archive.finish(c.binding, turn['round_id'], 'completed')
        receipt = c.receipts.write(
            c.binding, turn['round_id'], c.terminal_locator(turn['round_id'], 'completed', revision))
        lease = self.service.active('conversation')['lease']
        self.service.coordinator.mark_unresolved('conversation', lease['lease_id'], lease['generation'],
            attempt_id=turn['attempt_id'], reason='commit_verification_failed', detail='old decoder')
        with mock.patch('codex_appserver.run_turn', side_effect=AssertionError('must not replay')):
            report = RecoveryController(self.service).recover_guarded('conversation', trigger='startup')
        self.assertEqual(report['outcome'], 'committed')
        self.assertEqual(c.receipts.read(c.binding, turn['round_id']), receipt)
        state = self.service.coordinator.state('conversation')
        self.assertIsNone(state['inflight'])
        self.assertEqual(state['rounds'][0]['terminal_state'], 'completed')

    def test_staged_recheck_never_reopens_unverified_source(self):
        from shepherd.recovery_controller import RecoveryController
        c, turn = self.staged_blocked_turn()
        with c.archive.db() as db:
            db.execute("UPDATE events SET payload='{}' WHERE turn_id=?", (turn['round_id'],))
        with mock.patch.object(self.service.coordinator, 'source_persisted',
                               side_effect=AssertionError('must not reopen')):
            controller = RecoveryController(self.service)
            report = controller.recover_guarded('conversation', trigger='startup')
            self.assertEqual(report['outcome'], 'needs_reconciliation')
            self.assertEqual(controller.recover_guarded('conversation', trigger='send_before')['action'],
                             'isolation_required')
        self.assertEqual(self.service.coordinator.state('conversation')['committed_offset'], 0)

    def test_quarantine_recheck_keeps_corrupt_source_fenced_and_is_bounded(self):
        from shepherd.recovery_controller import RecoveryController
        c,turn=self.blocked_unified_turn()
        with c.archive.db() as db:
            db.execute("UPDATE events SET payload='{}' WHERE turn_id=?",(turn['round_id'],))
        controller=RecoveryController(self.service,boot_id='new')
        report=controller.recover_guarded('conversation',trigger='startup')
        self.assertEqual(report['outcome'],'needs_reconciliation')
        with mock.patch('shepherd.recovery_controller.CrashRecovery',side_effect=AssertionError('repeated probe')):
            self.assertEqual(controller.recover_guarded('conversation',trigger='send_before')['action'],'isolation_required')
        self.assertEqual(self.service.coordinator.state('conversation')['active_binding_id'],c.binding_id)

    def test_recovery_cannot_adopt_a_previous_turn_locator(self):
        from shepherd.recovery import CrashRecovery
        c, turn = self.blocked_unified_turn()
        prior = 'conversation:0'
        c.archive.begin(c.binding, prior)
        c.archive.append(c.binding, prior, {'type': 'runtime'})
        with self.service.store.change('conversation') as state:
            state['active_turn']['recovery']['native_locator']['turn_id'] = prior
        with mock.patch('codex_appserver.run_turn', side_effect=AssertionError('must not replay')):
            report = CrashRecovery(self.service).recover_one('conversation', allow_current_boot_commit=True)
        self.assertEqual(report['state'], 'needs_reconciliation')
        self.assertIsNone(c.receipts.read(c.binding, turn['round_id'], required=False))
        self.assertEqual(self.service.coordinator.state('conversation')['committed_offset'], 0)

    def test_quarantine_recheck_never_bypasses_live_or_unknown_writer(self):
        from shepherd.recovery_controller import RecoveryController
        self.blocked_unified_turn()
        for status in ('alive','unknown'):
            with self.subTest(status=status), mock.patch('shepherd.recovery_controller.process_state',return_value=status), \
                 mock.patch('shepherd.recovery_controller.CrashRecovery',side_effect=AssertionError('writer still fenced')):
                report=RecoveryController(self.service).recover_guarded('conversation',trigger='startup')
                self.assertEqual(report['action'],'isolation_required')

    def test_legacy_unified_block_without_reason_is_revalidated(self):
        from shepherd.recovery_controller import RecoveryController
        self.blocked_unified_turn()
        with self.service.store.change('conversation') as state:
            state['active_turn'].pop('recovery_reason',None)
        report=RecoveryController(self.service).recover_guarded('conversation',trigger='startup')
        self.assertEqual(report['outcome'],'recovered_incomplete')

    def test_history_cli_reads_pre_checkpoint_turns(self):
        import contextlib, io
        from shepherd.cli import main
        self.turn('codex',1);self.turn('pi',2)
        capture=io.StringIO()
        with contextlib.redirect_stdout(capture):
            self.assertEqual(main(['history','conversation','--root',str(self.root),
                                   '--start','1','--limit','1']),0)
        page=json.loads(capture.getvalue())
        self.assertEqual(page['total'],2)
        self.assertEqual(page['next'],2)
        self.assertEqual(page['turns'][0]['public'][0]['content'][0]['text'],'question 1')

    def test_terminal_archive_recovers_missing_receipt_without_replay(self):
        from shepherd.recovery import ReceiptRecoveryRunner
        c,row=self.turn('pi',1)
        Path(c.receipts.path(c.binding_id,'conversation:1')).unlink()
        recovered=ReceiptRecoveryRunner(c).recover_result('conversation:1')
        self.assertEqual(recovered['terminal_state'],'completed')
        self.assertEqual(recovered['source'],row['source'])
        self.assertEqual(c.resolve_source(row['source'])['public'],PUBLIC)

    def test_product_adapter_selects_unified_runner_connector(self):
        from engine_adapters.shepherd import ShepherdTrialAdapter
        from engine_adapters.bus import EventBus
        from shepherd.trial import set_policy
        data = self.root/'data'
        set_policy(data, create=True, execute=True)
        adapter=ShepherdTrialAdapter(str(data),str(self.root),EventBus())
        session=adapter.create_session(opts={'active_engine':'pi'})
        connector=adapter._connector(adapter.service.active(session['id'])['binding'])
        self.assertIsInstance(connector,UnifiedConnector)

    def test_predispatch_failure_allows_changed_input_and_engine(self):
        from unittest import mock
        from shepherd.unified_runners import UnifiedRunner
        from shepherd.turn_inputs import TurnInputs
        self.service.create('conversation','pi',self.root)
        active=self.service.active('conversation');c=self.service.connector(active['binding'])
        lease=active['lease'];runner=UnifiedRunner(c)
        first=[{'type':'message','role':'user','content':[{'type':'input_text','text':'first'}]}]
        with mock.patch.object(runner,'_pi',side_effect=Conflict('startup unavailable')):
            with self.assertRaises(Conflict):
                ExecutionHarness(self.service.coordinator,c,runner).run('conversation',
                    lease_id=lease['lease_id'],generation=lease['generation'],prompt='first',input_public=first)
        audit=self.service.store.load_raw('conversation')['audit_events'][-1]
        old_ref=audit['legacy_inflight']['input_ref']
        self.service.switch('conversation','codex',self.root)
        active=self.service.active('conversation');c=self.service.connector(active['binding']);lease=active['lease']
        ExecutionHarness(self.service.coordinator,c,JournalRunner(c)).run('conversation',
            lease_id=lease['lease_id'],generation=lease['generation'],prompt='changed',
            input_public=[{'type':'message','role':'user','content':[{'type':'input_text','text':'changed'}]}])
        self.assertEqual(TurnInputs(self.root).read(old_ref),first)

    def test_dead_process_partial_tool_recovers_through_controller(self):
        from shepherd.recovery import CrashRecovery
        from unittest import mock
        self.service.create('conversation','codex',self.root)
        active=self.service.active('conversation');c=self.service.connector(active['binding']);lease=active['lease']
        boundary=c.recovery_boundary()
        turn=self.service.coordinator.begin_round('conversation',lease['lease_id'],lease['generation'],
            recovery={'phase':'reserved','boot_id':'previous','boundary':boundary})
        self.service.coordinator.mark_dispatched('conversation',lease['lease_id'],lease['generation'],
            attempt_id=turn['attempt_id'],process={'pid':99999998,'pgid':99999998,'start_token':'old'},
            native_locator={'turn_id':turn['round_id']})
        c.archive.begin(c.binding,turn['round_id'])
        c.archive.append(c.binding,turn['round_id'],{'type':'codex_item','value':{
            'type':'custom_tool_call','call_id':'unfinished','name':'exec','input':'text(1)'}})
        source=c.archive.read(c.binding,turn['round_id'])
        with mock.patch('shepherd.recovery.process_state',return_value='dead'):
            report=CrashRecovery(Service(self.root),boot_id='next').recover_one('conversation')
        self.assertEqual(report['state'],'recovered_incomplete',report)
        self.assertEqual(c.archive.read(c.binding,turn['round_id']),source)
        row=self.service.coordinator.state('conversation')['rounds'][0]
        self.assertIn('[执行状态未知]',json.dumps(c.resolve_source(row['source'])['public'],ensure_ascii=False))

    def test_native_service_remains_legacy_by_explicit_default(self):
        old=Service(self.root/'old');old.create('old','pi',self.root)
        self.assertFalse(getattr(old.connector(old.active('old')['binding']),'unified',False))
