import copy
import tempfile
import unittest
from pathlib import Path

from shepherd.coordinator import digest
from shepherd.portable_checkpoint import CODEC, codex_items, derive
from shepherd.store import SourceVerificationError
from shepherd.unified_archive import UnifiedArchive
from shepherd.unified_connector import UnifiedConnector


def claude_checkpoint():
    common={'sessionId':'claude-session','cwd':'/work','version':'2.1.268',
            'timestamp':'2026-09-15T00:00:00.000Z'}
    boundary={**common,'type':'system','subtype':'compact_boundary','content':'Conversation compacted',
        'level':'info','uuid':'boundary','parentUuid':None,'logicalParentUuid':'prior',
        'compactMetadata':{'trigger':'manual','preTokens':100,'postTokens':20,
                           'cumulativeDroppedTokens':80,'durationMs':1}}
    summary={**common,'type':'user','uuid':'summary','parentUuid':'boundary',
        'isCompactSummary':True,'isVisibleInTranscriptOnly':True,
        'message':{'role':'user','content':'portable summary'}}
    assistant={**common,'type':'assistant','uuid':'answer','parentUuid':'summary',
        'message':{'role':'assistant','model':'fixture','content':[{'type':'text','text':'retained answer'}],
                   'stop_reason':'end_turn','stop_sequence':None,
                   'usage':{'input_tokens':0,'output_tokens':0}}}
    payload={'native':[boundary,summary,assistant],'source_hash':'a'*64}
    return {'context_key':'claude-context','turn_id':'conversation:6','through_seq':6,
            'covers_turn_hash':'c'*64,'payload':payload}


def pi_checkpoint():
    kept={'type':'message','id':'kept','parentId':None,
          'message':{'role':'user','content':[{'type':'text','text':'retained question'}]}}
    answer={'type':'message','id':'answer','parentId':'kept',
            'message':{'role':'assistant','content':[{'type':'text','text':'retained answer'}]}}
    compact={'type':'compaction','id':'compact','parentId':'answer','firstKeptEntryId':'kept',
             'summary':'portable summary','tokensBefore':100}
    payload={'native':[{'type':'session','id':'pi-session'},kept,answer,compact],
             'source_hash':'b'*64}
    return {'context_key':'pi-context','turn_id':'conversation:7','through_seq':7,
            'covers_turn_hash':'d'*64,'payload':payload}


class PortableCheckpointTests(unittest.TestCase):
    def test_claude_keeps_summary_and_semantic_tail(self):
        got=derive(claude_checkpoint(),producer_engine='claude',producer_binding_id='source-binding',session_id='claude-session',
                   native_cwd='/work',native_codec='claude-2.1.268-v1')
        self.assertEqual(got['codec'],CODEC)
        self.assertIn('portable summary',got['items'][0]['content'][0]['text'])
        self.assertEqual(got['items'][1]['content'][0]['text'],'retained answer')
        self.assertEqual(got['source_hash'],'a'*64)
        self.assertEqual(got['public_hash'],digest(got['items']))

    def test_pi_keeps_summary_and_retained_tail(self):
        got=derive(pi_checkpoint(),producer_engine='pi',producer_binding_id='source-binding',session_id='pi-session',
                   native_cwd='/work',native_codec='pi-0.85.1-v1')
        texts=[block['text'] for item in got['items'] if item.get('type')=='message'
               for block in item.get('content') or []]
        self.assertTrue(any('portable summary' in text for text in texts))
        self.assertIn('retained question',texts)
        self.assertIn('retained answer',texts)

    def test_v3_retains_pi_extension_context_and_v2_frozen_payload_still_loads(self):
        from shepherd.portable_checkpoint import _identity_hash
        candidate = pi_checkpoint()
        candidate['payload']['native'].extend([
            {'type':'branch_summary','id':'branch','parentId':'compact',
             'fromId':'old_leaf','summary':'branch context'},
            {'type':'custom_message','id':'extension','parentId':'branch',
             'customType':'extension','content':'hidden context','display':False}])
        before = copy.deepcopy(candidate)
        result = derive(candidate, producer_engine='pi', producer_binding_id='source-binding',
                        session_id='pi-session', native_cwd='/work', native_codec='pi-0.85.1-v1')
        self.assertEqual(result['codec'], 'portable-working-context-v3')
        self.assertEqual(candidate, before)
        texts = [block['text'] for item in result['items'] for block in item.get('content', [])]
        self.assertTrue(any('branch context' in text for text in texts))
        self.assertTrue(any('hidden context' in text for text in texts))
        self.assertTrue(codex_items(result))
        old = derive(pi_checkpoint(), producer_engine='pi', producer_binding_id='source-binding',
                     session_id='pi-session', native_cwd='/work', native_codec='pi-0.85.1-v1')
        old['codec'] = 'portable-working-context-v2'
        identity = {key: value for key, value in old.items()
                    if key not in ('checkpoint_id','source_hash','estimated_tokens','items')}
        old['checkpoint_id'] = _identity_hash(identity)
        frozen = copy.deepcopy(old)
        self.assertTrue(codex_items(old))
        self.assertEqual(old, frozen)

    def test_pi_latest_compaction_can_retain_an_older_compaction(self):
        candidate = pi_checkpoint()
        entries = candidate['payload']['native']
        entries.append({'type': 'message', 'id': 'tail', 'parentId': 'compact',
                        'message': {'role': 'assistant', 'content': 'later retained answer'}})
        entries.append({'type': 'compaction', 'id': 'compact2', 'parentId': 'tail',
                        'firstKeptEntryId': 'kept', 'summary': 'latest summary', 'tokensBefore': 200})
        before = copy.deepcopy(candidate)
        result = derive(candidate, producer_engine='pi', producer_binding_id='source-binding',
                        session_id='pi-session', native_cwd='/work', native_codec='pi-0.85.1-v1')
        self.assertEqual(candidate, before)
        texts = [block['text'] for item in result['items'] for block in item.get('content', [])]
        self.assertIn('latest summary', texts[0])
        self.assertFalse(any('portable summary' in text for text in texts))
        self.assertIn('retained answer', texts)
        self.assertIn('later retained answer', texts)
        entries[-1]['parentId'] = 'missing'
        with self.assertRaises(SourceVerificationError):
            derive(candidate, producer_engine='pi', producer_binding_id='source-binding',
                   session_id='pi-session', native_cwd='/work', native_codec='pi-0.85.1-v1')

    def test_invalid_boundary_and_unfinished_tool_fail_closed(self):
        candidate=claude_checkpoint();candidate['payload']['native'][1]['parentUuid']='other'
        with self.assertRaises(SourceVerificationError):
            derive(candidate,producer_engine='claude',producer_binding_id='source-binding',session_id='claude-session',
                   native_cwd='/work',native_codec='claude-2.1.268-v1')
        candidate=pi_checkpoint();candidate['payload']['native'].insert(-1,
            {'type':'message','id':'call','message':{'role':'assistant','content':[
                {'type':'toolCall','id':'c','name':'write','arguments':{}}]}})
        with self.assertRaises(Exception):
            derive(candidate,producer_engine='pi',producer_binding_id='source-binding',session_id='pi-session',
                   native_cwd='/work',native_codec='pi-0.85.1-v1')

    def test_pi_rejects_header_self_broken_and_forked_retained_boundaries(self):
        for kept,broken,fork in (('pi-session',False,False),('compact',False,False),
                                  ('kept',True,False),('kept',False,True)):
            candidate=pi_checkpoint();entries=candidate['payload']['native']
            compact=next(row for row in entries if row.get('type')=='compaction')
            compact['firstKeptEntryId']=kept
            if broken:entries[2]['parentId']='missing'
            if fork:
                entries.insert(2,{'type':'message','id':'fork','parentId':'kept',
                    'message':{'role':'assistant','content':'branch'}})
                compact['parentId']='fork'
            with self.subTest(kept=kept,broken=broken,fork=fork),self.assertRaises(SourceVerificationError):
                derive(candidate,producer_engine='pi',producer_binding_id='source-binding',session_id='pi-session',
                       native_cwd='/work',native_codec='pi-0.85.1-v1')

    def test_codex_encoding_uses_only_supported_turn_id_metadata(self):
        portable=derive(claude_checkpoint(),producer_engine='claude',producer_binding_id='source-binding',session_id='claude-session',
                        native_cwd='/work',native_codec='claude-2.1.268-v1')
        rows=codex_items(portable)
        self.assertTrue(rows)
        for index,row in enumerate(rows):
            metadata=row['internal_chat_message_metadata_passthrough']
            self.assertEqual(set(metadata),({'turn_id','content_item_kinds'} if index==0 else {'turn_id'}))
            self.assertTrue(metadata['turn_id'].startswith('shepherd-checkpoint:v2:portable_'))
        self.assertEqual(rows[0]['internal_chat_message_metadata_passthrough']['content_item_kinds'],
                         ['compaction.summary'])
        changed=copy.deepcopy(portable);changed['producer_binding_id']='other'
        with self.assertRaises(SourceVerificationError):codex_items(changed)

    def test_portable_archive_is_immutable_and_hash_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            archive=UnifiedArchive(directory,'conversation')
            portable=derive(claude_checkpoint(),producer_engine='claude',producer_binding_id='source-binding',session_id='claude-session',
                            native_cwd='/work',native_codec='claude-2.1.268-v1')
            archive.portable_checkpoint(portable['checkpoint_id'],turn_id='conversation:6',
                                        through_seq=6,payload=portable)
            self.assertEqual(archive.portable_checkpoint(portable['checkpoint_id'])['payload'],portable)
            archive.portable_checkpoint(portable['checkpoint_id'],turn_id='conversation:6',
                                        through_seq=6,payload=portable)
            changed=copy.deepcopy(portable);changed['items'][0]['content'][0]['text']='changed'
            with self.assertRaises(Exception):
                archive.portable_checkpoint(portable['checkpoint_id'],turn_id='conversation:6',
                                            through_seq=6,payload=changed)

    def test_newer_portable_candidate_is_frozen_and_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive=UnifiedArchive(directory,'conversation')
            candidate=claude_checkpoint()
            archive.checkpoint(candidate['context_key'],turn_id=candidate['turn_id'],
                               through_seq=candidate['through_seq'],payload=candidate['payload'])
            connector=object.__new__(UnifiedConnector)
            connector.archive=archive
            state={'rounds':[{'round_id':f'conversation:{seq}',
                              'source_hash':'a'*64 if seq==6 else str(seq),
                              'turn_hash':'c'*64 if seq==6 else str(seq),
                              'producer_binding_id':'source-binding',
                              'outcome':'completed','history_state':'available'} for seq in range(1,7)],
                   'bindings':{'source-binding':{'binding_id':'source-binding','engine':'claude','connector_version':'unified-v1:claude-2.1.268-v1',
                       'native_session_id':'claude-session','native_cwd':'/work'}}}
            chosen=connector._portable_context(state)
            self.assertEqual(chosen['through_seq'],6)
            self.assertTrue(chosen['payload']['native']['portable_checkpoint_id'].startswith('portable_'))
            self.assertEqual(len(archive.portable_candidates()),1)
            # The frozen row remains usable even after the mutable native cache is gone.
            with archive.db() as db:db.execute('DELETE FROM checkpoints')
            again=connector._portable_context(state)
            self.assertEqual(again,chosen)

    def test_unknown_or_unavailable_turn_never_grants_coverage(self):
        for outcome,history in (('unknown','available'),('completed','omitted'),('failed','available')):
            with tempfile.TemporaryDirectory() as directory:
                archive=UnifiedArchive(directory,'conversation');candidate=claude_checkpoint()
                archive.checkpoint(candidate['context_key'],turn_id=candidate['turn_id'],
                                   through_seq=candidate['through_seq'],payload=candidate['payload'])
                connector=object.__new__(UnifiedConnector);connector.archive=archive
                state={'rounds':[{'round_id':f'conversation:{seq}',
                    'source_hash':'a'*64 if seq==6 else str(seq),
                    'turn_hash':'c'*64 if seq==6 else str(seq),
                    'producer_binding_id':'source-binding',
                    'outcome':outcome if seq==6 else 'completed',
                    'history_state':history if seq==6 else 'available'} for seq in range(1,7)],
                    'bindings':{'source-binding':{'binding_id':'source-binding','engine':'claude',
                    'connector_version':'unified-v1:claude-2.1.268-v1',
                    'native_session_id':'claude-session','native_cwd':'/work'}}}
                with self.subTest(outcome=outcome,history=history):
                    self.assertIsNone(connector._portable_context(state))

    def test_wrong_ledger_source_hash_never_grants_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            archive=UnifiedArchive(directory,'conversation');candidate=claude_checkpoint()
            archive.checkpoint(candidate['context_key'],turn_id=candidate['turn_id'],
                               through_seq=candidate['through_seq'],payload=candidate['payload'])
            connector=object.__new__(UnifiedConnector);connector.archive=archive
            state={'rounds':[{'round_id':f'conversation:{seq}','source_hash':'wrong',
                              'turn_hash':'c'*64,'producer_binding_id':'source-binding',
                              'outcome':'completed','history_state':'available'} for seq in range(1,7)],
                   'bindings':{'source-binding':{'binding_id':'source-binding','engine':'claude','connector_version':'unified-v1:claude-2.1.268-v1',
                       'native_session_id':'claude-session','native_cwd':'/work'}}}
            self.assertIsNone(connector._portable_context(state))
            state['rounds'][-1].update(source_hash='a'*64,producer_binding_id='other')
            self.assertIsNone(connector._portable_context(state))


if __name__=='__main__':unittest.main()
