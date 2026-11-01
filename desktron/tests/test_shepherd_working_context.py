"""Working history is not the public transcript, and is never replayed."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from shepherd.codex_working_context import budget_items, working_context, read_working_context
from shepherd.store import SourceVerificationError
from shepherd.migrated_context import migrated_context
from shepherd.store import PromptNotStarted


def record(kind, payload):
    return {'type': kind, 'payload': payload}


def text(value, role='user'):
    return {'type': 'message', 'role': role,
            'content': [{'type': 'input_text', 'text': value}]}


class WorkingContextTests(unittest.TestCase):
    def setUp(self):
        self.meta = record('session_meta', {'id': 'native-id'})

    def read(self, records):
        return working_context([self.meta, *records], thread_id='native-id')

    def test_background_memory_is_disabled_only_for_disposable_context_export(self):
        import codex_appserver as app
        for engine in ('codex',):
            self.assertNotIn('features.memories=false',app._appserver_argv(engine,{}))
            args=app._appserver_argv(engine,{'_working_context_export':True})
            self.assertIn('features.memories=false',args)
            self.assertIn('features.memory_self_evolution=false',args)

    def test_compaction_replaces_prefix_and_keeps_exact_native_tail(self):
        opaque = {'type': 'compaction', 'encrypted_content': 'native-only'}
        call = {'type': 'custom_tool_call', 'call_id': 'c', 'name': 'exec', 'input': 'code'}
        result = {'type': 'custom_tool_call_output', 'call_id': 'c', 'output': 'already executed'}
        snapshot = self.read([
            record('response_item', text('old large transcript')),
            record('compacted', {'replacement_history': [opaque, text('retained')]}),
            record('response_item', call), record('response_item', result),
            record('turn_context', {'model': 'model-a'}),
        ])
        self.assertEqual(snapshot['items'], [opaque, text('retained'), call, result])
        self.assertEqual(snapshot['model'], 'model-a')
        self.assertTrue(snapshot['compacted'])

    def test_last_compaction_wins_without_duplicating_older_tail(self):
        snapshot = self.read([
            record('compacted', {'replacement_history': [text('summary1')]}),
            record('response_item', text('tail1')),
            record('compacted', {'replacement_history': [text('summary2')]}),
            record('response_item', text('tail2')),
        ])
        self.assertEqual(snapshot['items'], [text('summary2'), text('tail2')])

    def test_budget_uses_last_native_compaction_boundary(self):
        old = text('old audit history')
        first = {'type':'compaction', 'encrypted_content':'first'}
        second = {'type':'compaction', 'encrypted_content':'second'}
        tail = text('retained tail')
        native = {'format':'codex-working-context-v1', 'compacted':True,
                  'items':[old, first, old, second, tail]}
        self.assertEqual(budget_items(native), [second, tail])
        self.assertEqual(budget_items({'format':'codex-working-context-v1',
            'compacted':False, 'items':[old, tail]}), [old, tail])
        for invalid in (1, 'true', {'value':True}):
            with self.subTest(compacted=invalid):
                self.assertEqual(budget_items({'format':'codex-working-context-v1',
                    'compacted':invalid, 'items':[old, second, tail]}), [old, second, tail])

    def test_replace_is_not_an_append_and_current_instructions_are_separate(self):
        snapshot = self.read([
            record('history_mutation', {'operation': 'append', 'items': [text('old')]}),
            record('history_mutation', {'operation': 'replace', 'items': [text('summary'), text('stale', 'developer')]}),
            record('history_mutation', {'operation': 'append', 'items': [text('tail')]}),
        ])
        self.assertEqual(snapshot['items'], [text('summary'), text('tail')])

    def test_unknown_replacements_and_missing_coverage_fail_closed(self):
        for bad in (
            record('compacted', {'message': 'not a complete replacement'}),
            record('history_mutation', {'operation': 'rollback', 'items': []}),
            record('future_context_replace', {}),
            record('event_msg', {'type': 'thread_rolled_back'}),
        ):
            with self.subTest(bad=bad), self.assertRaises(SourceVerificationError):
                self.read([bad])

    def test_peer_metadata_is_inert_and_preserves_message_history(self):
        peer = {'type': 'agent_message', 'author': '/root/child', 'recipient': '/root',
                'content': [{'type': 'input_text', 'text': 'child result'}]}
        snapshot = self.read([
            record('inter_agent_communication_metadata', {'future_field': True}),
            record('response_item', peer), record('response_item', text('tail'))])
        self.assertEqual(snapshot['items'], [peer, text('tail')])

    def test_cache_recovery_requires_whole_file_identity_and_no_rollback(self):
        from shepherd.codex_working_context import WorkingContextUnavailable
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rollout.jsonl'
            for tail, recoverable in (
                ([], True),
                ([record('session_meta', {'id': 'foreign'})], False),
                ([record('event_msg', {'type': 'thread_rolled_back'})], False),
                ([record('history_mutation', {'operation': 'replace', 'items': []})], True),
                ([record('history_mutation', {'operation': 'rollback', 'items': []})], False),
                ([{'type': 'invalid', 'payload': None}], False),
                ([record('response_item', {'type': 'function_call', 'call_id': 'unfinished'})], False),
                ([record('response_item', {'type': 'function_call_output', 'call_id': 'orphan'})], False)):
                rows = [self.meta, record('future_metadata', {}), *tail]
                raw = ''.join(json.dumps(r) + '\n' for r in rows).encode()
                path.write_bytes(raw)
                with self.subTest(tail=tail), self.assertRaises(SourceVerificationError) as error:
                    read_working_context(path, thread_id='native-id')
                self.assertEqual(isinstance(error.exception, WorkingContextUnavailable), recoverable)
                if recoverable:
                    self.assertEqual(error.exception.raw, raw)
                self.assertEqual(path.read_bytes(), raw)

    def test_identity_is_required_and_unique(self):
        for records in ([], [record('response_item', text('unowned'))],
                        [self.meta, self.meta], [record('session_meta', {'id': 'other'})]):
            with self.subTest(records=records), self.assertRaises(SourceVerificationError):
                working_context(records, thread_id='native-id')

    def test_unfinished_tools_are_not_sent_and_compact_controls_are_not_replayed(self):
        for item in ({'type':'function_call','call_id':'c','name':'read','arguments':'{}'},
                     {'type':'function_call_output','call_id':'c','output':'orphan'}):
            with self.assertRaises(SourceVerificationError):
                self.read([record('response_item',item)])
        snapshot=self.read([record('response_item',{'type':'compaction_trigger'}),
                            record('response_item',text('tail'))])
        self.assertEqual(snapshot['items'],[text('tail')])

    def test_read_does_not_mutate_source_and_rejects_partial_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rollout.jsonl'
            raw = (json.dumps(self.meta) + '\n').encode()
            path.write_bytes(raw)
            snapshot = read_working_context(path, thread_id='native-id')
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(len(snapshot['rollout_sha256']), 64)
            path.write_bytes(raw + b'{')
            with self.assertRaises(SourceVerificationError):
                read_working_context(path, thread_id='native-id')


class MigratedContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.connector = SimpleNamespace(store_root=str(self.root), engine='codex')
        self.binding = {'binding_id': 'old', 'native_session_id': 'native-id',
                        'engine': 'codex', 'status': 'current', 'ownership_nonce': 'nonce',
                        'connector_version': 'native', 'native_cwd': '/work',
                        'imported_through_seq': 1, 'native_revision': 'revision'}
        self.turn = {'turn_id': 'conversation:1', 'turn_hash': 'hash', 'source_hash': 'source'}
        self.state = {'conversation_id': 'conversation', 'turns': [self.turn],
                      'bindings': {'old': {**self.binding, 'status': 'retired'}}}
        path = self.root / 'migrations/native-to-unified-v1/conversation.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'format': 'shepherd-native-to-unified-v1',
            'conversation_id': 'conversation', 'state': {'active_binding_id': 'old',
            'bindings': {'old': self.binding}, 'turns': [self.turn]}}))
        native_home = self.root / 'native'; native_home.mkdir()
        self.rollout = native_home / 'rollout-native-id.jsonl'
        rows = [record('session_meta', {'id': 'native-id'}),
                record('response_item', text('large old transcript')),
                record('turn_context', {'model': 'model-a'}),
                record('compacted', {'replacement_history': [text('native summary')]}),
                record('response_item', text('retained tail'))]
        self.rollout.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        self.native = SimpleNamespace(home=str(native_home), native_session_id='native-id',
                                     _safe_home=mock.Mock(), _revision=mock.Mock(return_value='revision'))
        self.service = SimpleNamespace(connector=mock.Mock(return_value=self.native))

    def read(self, **options):
        return migrated_context(self.connector, self.service, self.state, {'model': 'model-a', **options})

    def test_frozen_snapshot_covers_only_migration_prefix_without_native_execution(self):
        raw = self.rollout.read_bytes()
        self.state['turns'].append({'turn_id': 'conversation:2', 'turn_hash': 'new', 'source_hash': 'new-source'})
        checkpoint = self.read()
        self.assertEqual(checkpoint['through_seq'], 1)
        self.assertEqual(checkpoint['payload']['source_hash'], 'source')
        self.assertEqual(checkpoint['payload']['native']['items'], [text('native summary'), text('retained tail')])
        self.assertEqual(self.rollout.read_bytes(), raw)

    def test_unverified_history_and_unretired_writer_are_rejected(self):
        self.state['bindings']['old']['status'] = 'current'
        with self.assertRaises(SourceVerificationError): self.read()
        self.state['bindings']['old']['status'] = 'retired'
        self.native._revision.return_value = 'changed'
        with self.assertRaises(SourceVerificationError): self.read()

    def test_changed_prefix_is_rejected(self):
        self.state['turns'] = [{**self.turn, 'turn_hash': 'other'}]
        with self.assertRaises(SourceVerificationError): self.read()

    def test_model_change_does_not_silently_expand_old_history(self):
        checkpoint=self.read(model='model-b')
        self.assertEqual(checkpoint['payload']['native']['model'],'model-a')
        self.assertEqual(checkpoint['payload']['native']['items'],[text('native summary'),text('retained tail')])

    def engine(self, engine):
        self.connector.engine=engine
        self.binding['engine']=engine
        self.state['bindings']['old']['engine']=engine
        path=self.root/'migrations/native-to-unified-v1/conversation.json'
        value=json.loads(path.read_text())
        value['state']['bindings']['old']=dict(self.binding)
        path.write_text(json.dumps(value))

    def test_pi_migration_retains_native_compaction_and_tail(self):
        import hashlib
        entries=[{'type':'session','id':'native-id'},
                 {'type':'message','id':'discarded','message':{'role':'user','content':'old'}},
                 {'type':'message','id':'kept','message':{'role':'user','content':'tail'}},
                 {'type':'compaction','id':'compact','firstKeptEntryId':'kept','summary':'native summary'},
                 {'type':'message','id':'last','message':{'role':'assistant','content':'done'}}]
        raw=''.join(json.dumps(e)+'\n' for e in entries).encode()
        self.rollout.write_bytes(raw)
        self.binding['native_revision']=hashlib.sha256(raw).hexdigest()
        self.engine('pi')
        self.native.path=str(self.rollout)
        self.native._safe_path=mock.Mock()
        checkpoint=self.read()
        self.assertEqual(checkpoint['payload']['native'],[entries[0],*entries[2:]])

    def test_claude_migration_rebuilds_today_prompt_but_preserves_native_summary(self):
        self.engine('claude')
        old={'type':'assistant','message':{'model':'old-model','content':'old'}}
        boundary={'type':'system','subtype':'compact_boundary','uuid':'boundary'}
        summary={'type':'user','isCompactSummary':True,'message':{'content':'native summary'}}
        prompt={'type':'attachment','attachment':{'type':'prompt_snapshot','systemPrompt':['old instructions']}}
        self.native.native=SimpleNamespace(revision=lambda:'revision',load=lambda:[old,boundary,summary,prompt])
        checkpoint=self.read(model='model-a')
        self.assertEqual(checkpoint['payload']['native'],[boundary,summary])


if __name__ == '__main__':
    unittest.main()
