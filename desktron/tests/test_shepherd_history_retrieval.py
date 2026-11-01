"""Verified search, bounded reads, cursors and citations for Shepherd history."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import copy
import tempfile
import threading
import unittest
from unittest import mock

from shepherd.cli import main
from shepherd.coordinator import digest
from shepherd.harness import ExecutionHarness
from shepherd.history_prompt import history_prompt
from shepherd.history_retrieval import HistoryError, HistoryRetrieval
from shepherd.service import Service
from shepherd.store import Conflict
from test_shepherd_unified_storage import JournalRunner


class HistoryRetrievalTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'shepherd';self.root.mkdir()
        self.service=Service(self.root,storage='unified')

    def turn(self,engine,public,conversation='conversation'):
        state=self.service.coordinator.state(conversation)
        if state is None:self.service.create(conversation,engine,self.root)
        elif self.service.active(conversation)['binding']['engine']!=engine:
            self.service.switch(conversation,engine,self.root)
        active=self.service.active(conversation);connector=self.service.connector(active['binding'])
        return ExecutionHarness(self.service.coordinator,connector,JournalRunner(connector,public)).run(
            conversation,lease_id=active['lease']['lease_id'],generation=active['lease']['generation'],
            prompt='question',input_public=[{'type':'message','role':'user','content':[{
                'type':'input_text','text':'question'}]}])

    def history(self):
        first=[{'type':'message','role':'user','content':[{'type':'input_text','text':'讨论量子香蕉计划'}]},
               {'type':'function_call','call_id':'c','name':'read_secret','arguments':'{"token":"private"}'},
               {'type':'function_call_output','call_id':'c','output':'secret tool output'},
               {'type':'message','role':'assistant','content':[{'type':'output_text','text':'决定采用蓝色方案'}]}]
        second=[{'type':'message','role':'user','content':[{'type':'input_text','text':'继续香蕉计划'}]},
                {'type':'message','role':'assistant','content':[{
                    'type':'output_text','text':'第二阶段完成，复核量子香蕉'}]}]
        self.turn('claude',first);self.turn('codex',second)

    def test_cross_engine_search_indexes_safe_text_and_revalidates_citation(self):
        self.history();r=HistoryRetrieval(self.root)
        result=r.search('量子香蕉',conversation='conversation')
        self.assertEqual([1,2],sorted({hit['seq'] for hit in result['hits']}))
        self.assertEqual([2],[hit['seq'] for hit in r.search(
            '第二阶段',conversation='conversation',role='assistant')['hits']])
        self.assertTrue(r.search('香蕉',conversation='conversation')['hits'])
        self.assertEqual([],r.search('secret tool output',conversation='conversation')['hits'])
        tools=r.search('read_secret',conversation='conversation',role='tool')['hits']
        self.assertEqual(1,len(tools));self.assertEqual('tool_name',tools[0]['kind'])
        verified=r.verify(result['hits'][0]['citation'])
        self.assertTrue(verified['ok']);self.assertIn(verified['role'],('user','assistant'))

    def test_cached_search_spans_index_without_synchronous_refresh(self):
        self.history()
        self.turn('pi',[{'type':'message','role':'assistant','content':[{
            'type':'output_text','text':'另一段也讨论量子香蕉'}]}],conversation='other')
        r=HistoryRetrieval(self.root);r.sync('conversation');r.sync('other')
        with mock.patch.object(r,'_sync_locked',side_effect=AssertionError('must stay async')):
            result=r.search_cached('量子香蕉',limit=20)
        self.assertEqual([1,2],sorted({hit['seq'] for hit in result['hits']
                                      if hit['conversation']=='conversation'}))
        self.assertEqual({'conversation','other'},
                         {hit['conversation'] for hit in result['hits']})
        without_refreshing=r.search_cached('量子香蕉',limit=20,exclude=('other',))
        self.assertEqual({'conversation'},
                         {hit['conversation'] for hit in without_refreshing['hits']})

    def test_omitted_turns_are_counted_but_never_indexed_or_cited(self):
        self.history();r=HistoryRetrieval(self.root);r.sync('conversation')
        with self.service.store.change('conversation') as state:
            row=state['turns'][0]
            row['history_state']='omitted';row['recovery_reason']='fixture'
            row.pop('source_ref',None);row.pop('source_key',None)
            row.pop('source_hash',None);row.pop('public_hash',None)
            row['turn_hash']=digest({k:v for k,v in row.items() if k not in ('turn_hash','settled_at')})
            for following in state['turns'][1:]:
                following['turn_hash']=digest({k:v for k,v in following.items()
                                               if k not in ('turn_hash','settled_at')})
            for binding in state['bindings'].values():
                if binding['imported_through_seq']:
                    binding['imported_turn_hash']=state['turns'][binding['imported_through_seq']-1]['turn_hash']
        r.sync('conversation')
        hits=r.search('量子香蕉',conversation='conversation')['hits']
        self.assertNotIn(1,{hit['seq'] for hit in hits})
        self.assertIn(2,{hit['seq'] for hit in hits})
        read=r.read('conversation',start=1,end=1)
        self.assertEqual([],read['records'])
        self.assertEqual(1,r.catalog()['conversations'][0]['omitted'])

    def test_corrupt_index_does_not_block_authoritative_read_or_verify(self):
        self.history();r=HistoryRetrieval(self.root)
        hit=r.search('量子香蕉',conversation='conversation')['hits'][0]
        Path(r.path).write_bytes(b'not sqlite')
        read=r.read('conversation',start=1,end=1,max_tokens=4000,max_bytes=30000)
        verified=r.verify(hit['citation'])
        self.assertTrue(read['records']);self.assertTrue(verified['ok'])
        with self.assertRaises(Exception):r.search('量子香蕉',conversation='conversation')

    def test_index_is_candidate_cache_and_tampered_candidate_is_never_returned(self):
        self.history();r=HistoryRetrieval(self.root);r.sync('conversation')
        with r._db() as db:
            db.execute("UPDATE history_docs SET text=? WHERE conversation=? AND seq=1",
                       ('伪造量子香蕉结论','conversation'))
        result=r.search('量子香蕉',conversation='conversation')
        self.assertNotIn(1,{hit['seq'] for hit in result['hits']})
        self.assertIn(2,{hit['seq'] for hit in result['hits']})
        self.assertTrue(any(row['code']=='candidate_stale' for row in result['warnings']))

    def test_invalid_candidates_do_not_consume_verified_result_limit(self):
        self.history();r=HistoryRetrieval(self.root);r.sync('conversation')
        with r._db() as db:
            row=db.execute("SELECT * FROM history_docs WHERE conversation=? AND seq=1 LIMIT 1",
                           ('conversation',)).fetchone()
            db.execute("UPDATE history_docs SET text=? WHERE conversation=? AND seq=1",
                       ('量子香蕉伪造缓存','conversation'))
            for index in range(25):
                values=list(row);values[0]='量子香蕉伪造缓存 '+str(index)
                db.execute('INSERT INTO history_docs VALUES(?,?,?,?,?,?,?,?,?,?,?)',values)
        result=r.search('量子香蕉',conversation='conversation',limit=1)
        self.assertEqual(1,len(result['hits']))
        self.assertEqual(2,result['hits'][0]['seq'])
        self.assertGreaterEqual(sum(x['code']=='candidate_stale' for x in result['warnings']),1)

    def test_tampered_citation_fails_verified_read(self):
        self.history();r=HistoryRetrieval(self.root)
        hit=r.search('量子香蕉',conversation='conversation')['hits'][0]
        body=__import__('base64').urlsafe_b64decode(
            hit['citation'][5:]+'='*(-len(hit['citation'][5:])%4))
        value=json.loads(body);value['item_hash']='f'*64
        bad='shr1.'+__import__('base64').urlsafe_b64encode(
            json.dumps(value,separators=(',',':')).encode()).decode().rstrip('=')
        with self.assertRaises(HistoryError):r.verify(bad)

    def test_bounded_read_keeps_snapshot_and_splits_one_large_item(self):
        text='recent detail '*20000
        self.turn('claude',[{'type':'message','role':'user','content':[{'type':'input_text','text':text}]}])
        r=HistoryRetrieval(self.root)
        page=r.read('conversation',max_tokens=1000,max_bytes=12000,max_items=3)
        self.assertTrue(page['truncated']);self.assertLessEqual(page['usage']['estimated_tokens'],1000)
        self.assertLessEqual(len(json.dumps(page,ensure_ascii=False).encode()),12000)
        self.turn('claude',[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'new turn'}]}])
        next_page=r.read('conversation',cursor=page['next_cursor'],max_tokens=1000,max_bytes=12000)
        self.assertEqual(1,next_page['snapshot']['head_seq'],'cursor must keep original snapshot')
        self.assertTrue(next_page['records']);self.assertGreater(next_page['records'][0]['text_offset'],0)

    def test_complete_page_has_no_empty_followup_cursor(self):
        self.turn('claude',[{'type':'message','role':'user','content':[{
            'type':'input_text','text':'small'}]}])
        page=HistoryRetrieval(self.root).read('conversation',max_turns=10,
            max_tokens=4000,max_bytes=30000,max_items=10)
        self.assertFalse(page['truncated']);self.assertIsNone(page['next_cursor'])

    def test_max_items_is_global_and_tiny_budget_never_repeats_cursor(self):
        self.history();r=HistoryRetrieval(self.root)
        page=r.read('conversation',max_items=1,max_turns=10,
                    max_tokens=4000,max_bytes=30000)
        self.assertEqual(1,len(page['records']));self.assertTrue(page['next_cursor'])
        second=r.read('conversation',cursor=page['next_cursor'],max_items=1,
                      max_tokens=4000,max_bytes=30000)
        self.assertNotEqual(page['next_cursor'],second.get('next_cursor'))
        with mock.patch.object(r,'_fit',return_value=None):
            with self.assertRaisesRegex(HistoryError,'预算不足'):
                r.read('conversation',max_tokens=512,max_bytes=4096)

    def test_around_obeys_range_and_text_mode_omits_tool_payload(self):
        self.history();r=HistoryRetrieval(self.root)
        result=r.around('conversation',2,before=1,after=0,max_tokens=4000,max_bytes=30000)
        self.assertEqual({1,2},{row['seq'] for row in result['records']})
        raw=json.dumps(result,ensure_ascii=False)
        self.assertNotIn('private',raw);self.assertNotIn('secret tool output',raw)
        self.assertIn('read_secret',raw)
        audit=r.around('conversation',1,before=0,after=0,
                       max_tokens=4000,max_bytes=30000)
        body=json.dumps(audit,ensure_ascii=False)
        self.assertNotIn('private',body);self.assertNotIn('secret tool output',body)
        self.assertIn('read_secret',body)
        self.assertEqual('text',audit['mode']);self.assertFalse(audit['sensitive'])

    def test_message_and_tool_name_credentials_are_redacted_in_index_and_read(self):
        self.turn('claude',[{'type':'message','role':'user','content':[{
            'type':'input_text','text':'API_KEY=secret-value bearer abc.def.ghi sk-abcdefghijklmnop'}]},
            {'type':'function_call','call_id':'secret','name':'sk-abcdefghijklmnop','arguments':'{}'},
            {'type':'function_call_output','call_id':'secret','output':'done'}])
        r=HistoryRetrieval(self.root);r.sync('conversation')
        with r._db() as db:
            indexed='\n'.join(row[0] for row in db.execute('SELECT text FROM history_docs'))
        page=json.dumps(r.read('conversation',max_tokens=4000,max_bytes=30000),ensure_ascii=False)
        for raw in ('secret-value','abc.def.ghi','sk-abcdefghijklmnop'):
            self.assertNotIn(raw,indexed);self.assertNotIn(raw,page)
        self.assertIn('[redacted]',indexed);self.assertIn('[redacted]',page)

    def test_ngram_delete_uses_btree_mapping_not_fts_scan(self):
        self.history();r=HistoryRetrieval(self.root);r.sync('conversation')
        with r._db() as db:
            plan=db.execute('EXPLAIN QUERY PLAN SELECT docid FROM history_doc_map '
                            'WHERE conversation=? AND seq>=?',('conversation',2)).fetchall()
        self.assertTrue(any('history_doc_map_conversation_seq' in row[3] for row in plan),plan)

    def test_full_delete_uses_mapping_and_preserves_other_conversation(self):
        self.history();r=HistoryRetrieval(self.root);r.sync('conversation')
        with r._db() as db:
            statements=[];db.set_trace_callback(statements.append)
            r._delete_documents(db,'conversation')
            self.assertEqual(0,db.execute('SELECT count(*) FROM history_docs').fetchone()[0])
        deletes=[sql.lower() for sql in statements if sql.lower().startswith('delete from history_docs ')]
        self.assertTrue(deletes)
        self.assertTrue(all('where rowid=' in sql for sql in deletes),deletes)

    def test_text_index_caps_warn_and_leave_full_authoritative_read_available(self):
        from shepherd import history_retrieval as module
        content='检索前缀'+('中间内容'*40)+'尾部事实'
        self.turn('claude',[{'type':'message','role':'assistant','content':content}])
        with mock.patch.object(module,'MAX_DOCUMENT_CHARS',40), \
             mock.patch.object(module,'MAX_CONVERSATION_CHARS',48):
            r=HistoryRetrieval(self.root);result=r.search('检索前缀',conversation='conversation')
            self.assertTrue(result['hits'])
            self.assertTrue(any(w['code']=='index_partial' for w in result['warnings']))
            with r._db() as db:
                used,count=db.execute('SELECT sum(text_chars),count(*) FROM history_doc_map').fetchone()
                maximum=db.execute('SELECT max(length(text)) FROM history_docs').fetchone()[0]
            self.assertLessEqual(used,48);self.assertLessEqual(maximum,40)
            self.turn('claude',[{'type':'message','role':'assistant','content':'新增正文'*40}])
            r.sync('conversation')
            with r._db() as db:
                self.assertLessEqual(db.execute('SELECT sum(text_chars) FROM history_doc_map').fetchone()[0],48)
            tail=r.search('尾部事实',conversation='conversation')
            self.assertEqual([],tail['hits'])
            self.assertTrue(any(w['code']=='index_partial' for w in tail['warnings']))
        self.assertIn('尾部事实',json.dumps(r.read('conversation'),ensure_ascii=False))
        self.assertTrue(r.verify(result['hits'][0]['citation'])['ok'])

    def test_cli_output_obeys_actual_serialized_budget_and_rejects_tiny_requests(self):
        self.turn('claude',[{'type':'message','role':'assistant','content':'长段历史内容'*4000}])
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            rc=main(['retrieve','read','conversation','--root',str(self.root),
                     '--max-tokens','3000','--max-bytes','12000'])
        self.assertEqual(0,rc)
        encoded=output.getvalue().strip().encode()
        self.assertLessEqual(len(encoded),12000)
        self.assertEqual(len(encoded),json.loads(encoded)['usage']['serialized_bytes'])
        for budget in ({'max_tokens':100},{'max_bytes':200}):
            with self.assertRaises(HistoryError) as caught:
                HistoryRetrieval(self.root).read('conversation',**budget)
            self.assertEqual('budget_too_small',caught.exception.code)

    def test_document_count_cap_is_explicit_and_read_still_works(self):
        from shepherd import history_retrieval as module
        self.history()
        with mock.patch.object(module,'MAX_CONVERSATION_DOCUMENTS',1):
            r=HistoryRetrieval(self.root);result=r.search('第二阶段',conversation='conversation')
            self.assertEqual([],result['hits'])
            self.assertTrue(any(w['code']=='index_partial' for w in result['warnings']))
            with r._db() as db:
                self.assertEqual(1,db.execute('SELECT count(*) FROM history_doc_map').fetchone()[0])
        self.assertIn('第二阶段',json.dumps(r.read('conversation',start=2),ensure_ascii=False))

    def test_unsupported_trigram_is_explicit_instead_of_silent_search_change(self):
        r=HistoryRetrieval(self.root)
        original=r._db
        class Proxy:
            def __init__(self,db):self.db=db
            def execute(self,sql,*args):
                if "tokenize='trigram'" in sql:
                    raise sqlite3.OperationalError('no such tokenizer: trigram')
                return self.db.execute(sql,*args)
        @contextlib.contextmanager
        def unsupported():
            with original() as db:yield Proxy(db)
        with mock.patch.object(r,'_db',side_effect=unsupported):
            with self.assertRaises(HistoryError) as caught:r._schema()
        self.assertEqual('tokenizer_unavailable',caught.exception.code)

    def test_corrupt_index_delete_is_durable_and_cleanup_failure_visible(self):
        self.history();r=HistoryRetrieval(self.root);r.sync('conversation')
        r.path.write_bytes(b'not sqlite')
        with self.assertLogs('shepherd.history_retrieval',level='WARNING'):
            result=r.tombstone_and_remove('conversation',self.service.coordinator)
        self.assertEqual('deleted',result['state'])
        with self.assertRaises(HistoryError):r.read('conversation')
        with self.assertLogs('shepherd.history_retrieval',level='WARNING'):
            self.assertEqual('index_cleanup_pending',r.cleanup_deleted('conversation')['code'])

    def test_ngram_index_handles_high_entropy_text_without_duplicate_rows(self):
        text=''.join(chr(0x4e00+(i%2000)) for i in range(20000))
        self.turn('claude',[{'type':'message','role':'user','content':[{
            'type':'input_text','text':text}]}])
        r=HistoryRetrieval(self.root);r.sync('conversation')
        with r._db() as db:
            grams=db.execute('SELECT count(*) FROM history_ngrams').fetchone()[0]
            duplicates=db.execute('SELECT count(*)-count(DISTINCT gram||":"||docid) '
                                  'FROM history_ngrams').fetchone()[0]
        self.assertLessEqual(grams,len(text)-1);self.assertEqual(0,duplicates)

    def test_ngram_limit_marks_partial_short_query_index(self):
        from shepherd import history_retrieval as module
        text='模型'+''.join(chr(0x4e00+(i%100)) for i in range(300))+'末尾目标'
        self.turn('claude',[{'type':'message','role':'user','content':[{
            'type':'input_text','text':text}]}])
        with mock.patch.object(module,'MAX_NGRAMS_PER_DOCUMENT',32):
            r=HistoryRetrieval(self.root);result=r.search('模型',conversation='conversation')
        self.assertTrue(result['hits'])
        self.assertTrue(any(row['code']=='short_query_partial' for row in result['warnings']))

    def test_failed_rebuild_rolls_back_old_visible_index(self):
        self.history();r=HistoryRetrieval(self.root);r.sync('conversation')
        with r._db() as db:
            before=[tuple(row) for row in db.execute('SELECT text,seq,item_ordinal FROM history_docs')]
            db.execute('DELETE FROM history_heads WHERE conversation=?',('conversation',))
        original=r._insert_document;calls=[0]
        def broken(*args,**kwargs):
            calls[0]+=1
            if calls[0]>1:raise Conflict('fixture crash')
            return original(*args,**kwargs)
        with mock.patch.object(r,'_insert_document',side_effect=broken):
            with self.assertRaises(Conflict):r.sync('conversation')
        with r._db() as db:
            after=[tuple(row) for row in db.execute('SELECT text,seq,item_ordinal FROM history_docs')]
        self.assertEqual(before,after)

    def test_concurrent_sync_is_idempotent_and_keeps_one_copy(self):
        self.history();r=HistoryRetrieval(self.root);errors=[]
        def run():
            try:r.sync('conversation')
            except Exception as exc:errors.append(exc)
        threads=[threading.Thread(target=run) for _ in range(4)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual([],errors)
        with r._db() as db:
            count=db.execute("SELECT count(*) FROM history_docs WHERE conversation=?",
                             ('conversation',)).fetchone()[0]
            distinct=db.execute("SELECT count(*) FROM (SELECT DISTINCT seq,item_ordinal "
                                "FROM history_docs WHERE conversation=?)",('conversation',)).fetchone()[0]
        self.assertEqual(count,distinct)

    def test_broken_state_warns_without_deleting_previous_index(self):
        self.history();r=HistoryRetrieval(self.root);r.sync('conversation')
        path=self.root/'conversation'/'state.json';original=path.read_text(encoding='utf-8');path.write_text('{bad', encoding='utf-8')
        try:
            catalog=r.catalog()
            self.assertTrue(any(row['conversation']=='conversation' for row in catalog['warnings']))
            with r._db() as db:
                self.assertGreater(db.execute('SELECT count(*) FROM history_docs WHERE conversation=?',
                                              ('conversation',)).fetchone()[0],0)
        finally:path.write_text(original, encoding='utf-8')

    def test_malformed_metadata_and_citation_are_isolated(self):
        self.history();r=HistoryRetrieval(self.root)
        meta=self.root.parent/'adapter'/'shepherd';meta.mkdir(parents=True,exist_ok=True)
        (meta/'sessions.json').write_text(json.dumps({'conversation':{'time':'bad'},
                                                      'other':'corrupt'}), encoding='utf-8')
        catalog=r.catalog()
        self.assertEqual('conversation',catalog['conversations'][0]['conversation'])
        self.assertTrue(any(row['code']=='metadata_unavailable' for row in catalog['warnings']))
        for value in ({'v':1,'conversation':'conversation','seq':'bad'},
                      {'v':True,'conversation':'conversation','seq':1,'item':0},
                      {'v':1,'conversation':'conversation','seq':True,'item':0},
                      {'v':1,'conversation':'conversation','seq':1,'item':0.9}):
            malformed='shr1.'+__import__('base64').urlsafe_b64encode(
                json.dumps(value).encode()).decode().rstrip('=')
            with self.assertRaises(HistoryError):r.verify(malformed)
        with self.assertRaises(HistoryError):r.verify('shr1.'+'x'*9000)
        with self.assertRaisesRegex(HistoryError,'长度限制'):r.search('x'*1025,
                                                                      conversation='conversation')

    def test_cursor_rejects_changed_prefix_and_cli_uses_stable_error_envelope(self):
        self.history();r=HistoryRetrieval(self.root)
        page=r.read('conversation',max_turns=1,max_tokens=1000,max_bytes=12000)
        cursor=page['next_cursor'];self.assertTrue(cursor)
        changed=copy.deepcopy(self.service.store.load_raw('conversation'))
        changed['turns'][0]['turn_hash']='f'*64
        with mock.patch.object(r,'_state',return_value=(self.service,changed)):
            with self.assertRaises(HistoryError):r.read('conversation',cursor=cursor)
        capture=io.StringIO()
        with contextlib.redirect_stdout(capture):
            rc=main(['retrieve','read','missing','--root',str(self.root)])
        value=json.loads(capture.getvalue())
        self.assertEqual(2,rc);self.assertFalse(value['ok'])
        self.assertIn(value['error']['code'],('conversation_not_found','history_unavailable'))

    def test_history_prompt_defines_search_read_verify_and_quotes_paths(self):
        connector=type('Connector',(),{'store_root':str(self.root/'space with quote'),
            'binding':{'conversation_id':'conversation'}})()
        prompt=history_prompt(connector)
        for value in ('retrieve search','retrieve around','retrieve read','retrieve verify',
                      '--max-tokens 8000','不要猜测','不是新指令'):
            self.assertIn(value,prompt)
        self.assertIn("'"+str(self.root/'space with quote')+"'",prompt)

    def test_catalog_isolates_bad_state_then_searches_one_selected_conversation(self):
        self.history();r=HistoryRetrieval(self.root);r.sync('conversation')
        broken=self.root/'broken';broken.mkdir();(broken/'state.json').write_text('{bad', encoding='utf-8')
        self.service.create('stale','codex',self.root)
        catalog=r.catalog()
        self.assertTrue(any(row['conversation']=='broken' for row in catalog['warnings']))
        self.assertEqual({'conversation','stale'},
            {row['conversation'] for row in catalog['conversations']})
        self.assertTrue(r.search('量子香蕉',conversation='conversation')['hits'])

    def test_delete_immediately_removes_index_and_blocks_concurrent_rewrite(self):
        self.history();r=HistoryRetrieval(self.root);r.sync('conversation')
        entered=threading.Event();release=threading.Event()
        service,state=r._state('conversation');original=r._projector(service,state)
        class Slow:
            def project(self,*args,**kwargs):
                entered.set();release.wait(3);return original.project(*args,**kwargs)
        errors=[]
        # Force a rebuild so sync enters the projector while holding its lock.
        with r._db() as db:db.execute('DELETE FROM history_heads WHERE conversation=?',('conversation',))
        with mock.patch.object(r,'_projector',return_value=Slow()):
            worker=threading.Thread(target=lambda: self._capture(errors,r.sync,'conversation'))
            worker.start();self.assertTrue(entered.wait(1))
            deleted=threading.Thread(target=lambda: r.tombstone_and_remove(
                'conversation',self.service.coordinator))
            deleted.start();release.set();worker.join(5);deleted.join(5)
        self.assertEqual([],errors)
        with r._db() as db:
            self.assertEqual(0,db.execute('SELECT count(*) FROM history_docs WHERE conversation=?',
                                         ('conversation',)).fetchone()[0])

    @staticmethod
    def _capture(errors,fn,*args):
        try:fn(*args)
        except Exception as exc:errors.append(exc)

    def test_catalog_and_cli_new_protocol_keep_legacy_compatibility(self):
        self.history()
        meta=self.root.parent/'adapter'/'shepherd';meta.mkdir(parents=True,exist_ok=True)
        (meta/'sessions.json').write_text(json.dumps({'conversation':{
            'title':'历史检索会话','domain':'desk-iter','selected_engine':'codex',
            'time':{'updated':123000}}},ensure_ascii=False), encoding='utf-8')
        capture=io.StringIO()
        with contextlib.redirect_stdout(capture):
            self.assertEqual(main(['retrieve','catalog','--root',str(self.root)]),0)
        catalog=json.loads(capture.getvalue());self.assertTrue(catalog['ok'])
        self.assertEqual('conversation',catalog['conversations'][0]['conversation'])
        self.assertEqual('历史检索会话',catalog['conversations'][0]['title'])
        self.assertEqual('desk-iter',catalog['conversations'][0]['domain'])
        self.assertEqual('codex',catalog['conversations'][0]['engine'])
        capture=io.StringIO()
        with contextlib.redirect_stdout(capture):
            self.assertEqual(main(['retrieve','search','conversation','--root',str(self.root),
                                   '--query','量子香蕉']),0)
        self.assertTrue(json.loads(capture.getvalue())['hits'])
        capture=io.StringIO()
        with contextlib.redirect_stdout(capture):
            self.assertEqual(main(['history','conversation','--root',str(self.root),
                                   '--start','1','--limit','1']),0)
        legacy=json.loads(capture.getvalue())
        self.assertEqual(1,legacy['turns'][0]['seq']);self.assertEqual(2,legacy['next'])
        self.assertNotIn('secret tool output',capture.getvalue())
        self.assertNotIn('"token":"private"',capture.getvalue())
        # Action words remain legal legacy conversation IDs because modern
        # retrieval has its own unambiguous `retrieve` command.
        self.service.create('search','codex',self.root)
        capture=io.StringIO()
        with contextlib.redirect_stdout(capture):
            self.assertEqual(main(['history','search','--root',str(self.root)]),0)
        self.assertEqual('search',json.loads(capture.getvalue())['conversation'])

    def test_incremental_sync_and_deleted_conversation_cleanup(self):
        self.history();r=HistoryRetrieval(self.root);first=r.sync('conversation')
        self.turn('pi',[{'type':'message','role':'assistant','content':[{
            'type':'output_text','text':'第三阶段检索增量'}]}])
        second=r.sync('conversation')
        self.assertEqual(first['mode'],'rebuild');self.assertEqual(second['mode'],'incremental')
        self.assertEqual([3],[hit['seq'] for hit in r.search(
            '检索增量',conversation='conversation')['hits']])
        self.service.coordinator.tombstone_for_delete('conversation')
        r.catalog()
        with r._db() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM history_heads WHERE conversation=?',
                                         ('conversation',)).fetchone())

    def test_invalid_cursor_and_index_initialization_return_stable_cli_error(self):
        self.history();capture=io.StringIO()
        with contextlib.redirect_stdout(capture):
            rc=main(['retrieve','read','conversation','--root',str(self.root),
                     '--cursor','shrc1.bad'])
        result=json.loads(capture.getvalue())
        self.assertEqual(2,rc);self.assertEqual('invalid_locator',result['error']['code'])
        if os.name == 'nt':
            # ACL roots have no POSIX permission bits. Exercise the same CLI
            # rejection with an unsafe linked index, not a fabricated chmod.
            unsafe=self.root/'unsafe';unsafe.mkdir()
            outside=self.root/'outside-index';outside.mkdir()
            (unsafe/'retrieval').symlink_to(outside, target_is_directory=True)
        else:
            unsafe=self.root/'unsafe';unsafe.mkdir();os.chmod(unsafe,0o777)
        if os.name == 'nt':
            capture=io.StringIO()
            with contextlib.redirect_stdout(capture):
                rc=main(['retrieve','catalog','--root',str(unsafe)])
            result=json.loads(capture.getvalue())
            self.assertEqual(2,rc);self.assertEqual('unsafe_index_path',result['error']['code'])
            return
        bad=unsafe/'retrieval';bad.mkdir();(bad/'history.sqlite3').write_text('', encoding='utf-8')
        os.chmod(bad/'history.sqlite3',0o644)
        capture=io.StringIO()
        with contextlib.redirect_stdout(capture):
            rc=main(['retrieve','catalog','--root',str(unsafe)])
        result=json.loads(capture.getvalue())
        self.assertEqual(2,rc);self.assertEqual('unsafe_index_path',result['error']['code'])

    def test_checkpoint_busy_blocks_claim_of_physical_removal(self):
        self.history();r=HistoryRetrieval(self.root);r.sync('conversation')
        class Result:
            def __init__(self,rows=()):self.rows=list(rows)
            def fetchone(self):return self.rows[0] if self.rows else None
            def __iter__(self):return iter(self.rows)
        class DB:
            def execute(self,sql,*args):
                if 'wal_checkpoint' in sql:return Result([(1,0,0)])
                return Result()
            def executemany(self,*args):return Result()
        @contextlib.contextmanager
        def fake_db():yield DB()
        with mock.patch.object(r,'_schema'),mock.patch.object(r,'_db',side_effect=fake_db), \
             self.assertRaisesRegex(HistoryError,'仍有读者'):
            r._remove_locked('conversation',vacuum=True)

    def test_index_permissions_are_private(self):
        self.history();r=HistoryRetrieval(self.root);r.sync('conversation')
        from _platform import assert_private_file, assert_private_directory
        assert_private_file(r.path)
        assert_private_directory(r.index_root)


if __name__=='__main__':unittest.main()
