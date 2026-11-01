"""Versioned Pi context projection; SDK-only checks never call a model/tool."""
import copy
import json
from pathlib import Path
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from shepherd.harness import ExecutionHarness
from shepherd.projector import ReadProjector
from shepherd.recovery import CrashRecovery
from shepherd.turn_inputs import TurnInputs

from shepherd.coordinator import digest
from shepherd.pi_connector import PiConnector
from shepherd.source_codec import pi_public
from shepherd.store import Conflict, InvalidState
from test_shepherd_images import PNG, roundtrip
from test_shepherd_pi import PiFixture
import test_shepherd_pi_failed_generation as unified_fixture


def contexts():
    return [
        {'type': 'branch_summary', 'fromId': 'left_branch', 'summary': 'Investigated A; use B.'},
        {'type': 'custom_message', 'customType': 'extension', 'display': False,
         'content': [{'type': 'text', 'text': 'historical context'},
                     {'type': 'image', 'mimeType': 'image/png', 'data': PNG}],
         'details': {'private': 'not model context'}},
    ]


class ContextProjection(unittest.TestCase):
    def test_system_transcripts_remain_raw_without_changing_public_history(self):
        old = [{'type':'message','message':{'role':'user','content':'hello'}}]
        for content in ('', 'private instructions', [{'type':'text','text':'private instructions'}]):
            entry = {'type':'message','message':{'role':'system','content':content,
                     'sections':{'instructions':'private section','old':None},
                     'toolsAdded':[{'name':'tool','parameters':{}}], 'toolsRemoved':[]}}
            entries = [entry, *old]
            before = copy.deepcopy(entries)
            for version in (1, 2, 3, 4):
                self.assertEqual(pi_public(entries, version=version), pi_public(old, version=version))
                self.assertEqual(PiConnector.source_compatibility({'entries':entries}, version=version),
                    [{'engine':'pi','action':'ignored_runtime_instructions','kind':'system','count':1}])
            self.assertEqual(entries, before)

    def test_invalid_system_transcripts_and_unknown_roles_still_reject(self):
        for change in ({'content':42}, {'content':[{'type':'toolCall','id':'bad'}]},
                       {'sections':{'bad':42}}, {'toolsAdded':'bad'}, {'toolsRemoved':[42]},
                       {'role':'unknown'}):
            entry = {'type':'message','message':{'role':'system','content':'', **change}}
            with self.subTest(change=change), self.assertRaises(InvalidState):
                pi_public([entry], version=4)
        with self.assertRaises(InvalidState):
            pi_public([{'type':'custom','customType':'shepherd-reasoning-v2'},
                       {'type':'message','message':{'role':'system','content':''}}], version=4)

    def test_context_survives_with_provenance_images_and_no_execution(self):
        entries = contexts()
        before = copy.deepcopy(entries)
        public = pi_public(entries, version=4)
        self.assertEqual(entries, before)
        self.assertEqual(len(public), 2)
        self.assertTrue(all(item['type'] == 'message' for item in public))
        self.assertIn('branch_summary', public[0]['content'][0]['text'])
        self.assertIn('不是新用户请求或授权', public[1]['content'][0]['text'])
        self.assertTrue(public[0]['content'][0]['text'].endswith(entries[0]['summary']))
        self.assertEqual(public[1]['content'][1]['text'], 'historical context')
        self.assertTrue(public[1]['content'][2]['image_url'].endswith(PNG))
        self.assertNotIn('private', json.dumps(public))
        for engine in ('pi', 'claude', 'codex'):
            # Claude coalesces consecutive user messages; certify each context
            # record independently rather than imposing a different turn layout.
            for item in public:
                self.assertEqual(roundtrip(engine, {'round_id': 'conversation:1', 'offset': 1,
                    'entry_hash': 'a'*64, 'public': [item]}), [item])

    def test_hidden_and_visible_messages_both_enter_context(self):
        for display in (True, False):
            entry = {'type': 'custom_message', 'customType': 'extension',
                     'display': display, 'content': 'keep me', 'extra': {'future': True}}
            self.assertTrue(pi_public([entry], version=4)[0]['content'][0]['text'].endswith('keep me'))

    def test_embedded_native_message_forms(self):
        entries = [
            {'type': 'message', 'message': {'role': 'branchSummary', 'fromId': 'left_branch',
                                          'summary': 'Investigated A; use B.'}},
            {'type': 'message', 'message': {'role': 'custom', **{k: v for k, v in contexts()[1].items() if k != 'type'}}},
        ]
        self.assertEqual(pi_public(entries, version=4), pi_public(contexts(), version=4))

    def test_old_versions_keep_original_projection_and_rejection(self):
        for version in (1, 2, 3):
            self.assertEqual(pi_public(contexts(), version=version), [])
            with self.assertRaises(Conflict):
                PiConnector.source_compatibility({'entries': contexts()}, version=version)
        # Independent old successful sample, not just comparison to new code.
        old = [{'type': 'message', 'message': {'role': 'user', 'content': 'hello'}}]
        expected = [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'hello'}]}]
        for version in (1, 2, 3, 4):
            self.assertEqual(pi_public(old, version=version), expected)

    def test_invalid_semantic_payloads_never_silently_disappear(self):
        variants = [
            {'type': 'branch_summary', 'summary': 'no origin'},
            {'type': 'branch_summary', 'fromId': 'origin', 'summary': {}},
            {'type': 'custom_message', 'customType': '', 'display': False, 'content': 'x'},
            {'type': 'custom_message', 'customType': 'ext', 'display': 'false', 'content': 'x'},
        ]
        for content in (None, {}, [{'type': 'toolCall', 'id': 'never'}],
                        [{'type': 'text', 'text': 42}], [{'type': 'image', 'data': 'bad', 'mimeType': 'image/png'}]):
            variants.append({'type': 'custom_message', 'customType': 'ext', 'display': True, 'content': content})
        for entry in variants:
            with self.subTest(entry=entry), self.assertRaises(InvalidState):
                pi_public([entry], version=4)
        with self.assertRaises(Conflict):
            PiConnector.source_compatibility({'entries': [{'type': 'future_semantics'}]}, version=4)

    def test_control_marker_names_on_custom_messages_remain_content(self):
        for name in ('shepherd-reasoning-v2', 'shepherd-custom-tool-v1', 'shepherd-tool-call-ids-v1'):
            entry = {'type':'custom_message','customType':name,'display':False,'content':'context'}
            self.assertIn('context', pi_public([entry], version=4)[0]['content'][0]['text'])
        for version in (0, 5, True, '4'):
            with self.assertRaises(InvalidState):
                pi_public(contexts(), version=version)

    def test_reasoning_marker_cannot_be_consumed_by_extension_context(self):
        with self.assertRaises(InvalidState):
            pi_public([{'type': 'custom', 'customType': 'shepherd-reasoning-v2'}, *contexts()], version=4)


class ContextReceipts(unittest.TestCase):
    def setUp(self):
        unified_fixture.UnifiedFailedGeneration.setUp(self)
        # Windows cannot remove fixture DBs while the process-local LRU holds
        # SQLite handles. Close them before the inherited TemporaryDirectory cleanup.
        from shepherd.unified_archive import _ArchiveConnections
        self.addCleanup(_ArchiveConnections.close_all)

    archive = unified_fixture.UnifiedFailedGeneration.archive

    def test_system_transcript_receipt_recovers_without_rewriting_evidence(self):
        entries = [
            {'type':'message','message':{'role':'system','content':'private instructions',
                                       'sections':{'runtime':'private runtime'}}},
            {'type':'message','message':{'role':'assistant','content':[{'type':'text','text':'READY'}]}},
        ]
        self.archive(entries)
        original = self.c.archive.read(self.c.binding, self.tid)
        receipt = self.c.recover_terminal_receipt(self.tid)
        public = self.c.resolve_source(self.c._ref(self.tid))['public']
        self.assertEqual(public, [{'type':'message','role':'assistant',
                                 'content':[{'type':'output_text','text':'READY'}]}])
        self.assertEqual(self.c.recover_terminal_receipt(self.tid), receipt)
        self.assertEqual(self.c.archive.read(self.c.binding, self.tid), original)

    def test_context_receipt_recovery_is_idempotent_and_hash_stable(self):
        entries = contexts()
        self.archive(entries)
        original = self.c.archive.read(self.c.binding, self.tid)
        receipt = self.c.recover_terminal_receipt(self.tid)
        self.assertEqual(receipt['locator']['projection_version'], 4)
        first = self.c.resolve_source(self.c._ref(self.tid))
        self.assertEqual(first['public'], pi_public(entries, version=4))
        self.assertEqual(self.c.recover_terminal_receipt(self.tid), receipt)
        self.assertEqual(self.c.resolve_source(self.c._ref(self.tid)), first)
        self.assertEqual(self.c.archive.read(self.c.binding, self.tid), original)

    def input_public(self):
        return [{'type':'message','role':'user','content':[
            {'type':'input_text','text':'actual user request'},
            {'type':'input_image','image_url':'data:image/png;base64,' + PNG}]}]

    def projected(self):
        return ReadProjector(self.service.store, SimpleNamespace(
            resolve=self.c.resolve_source)).project('conversation')[0]['public']

    def test_completed_commit_preserves_actual_input_and_image(self):
        c = self.c
        def run(*, round_id, on_dispatched, **kwargs):
            on_dispatched()
            revision = self.archive(contexts())
            c.receipts.write(c.binding, round_id, c.terminal_locator(round_id, 'completed', revision))
            return {'source':c._ref(round_id),'terminal_state':'completed','native_revision':revision}
        runner = SimpleNamespace(run=mock.Mock(side_effect=run))
        active = self.service.active('conversation')
        ExecutionHarness(self.service.coordinator, c, runner).run('conversation',
            lease_id=active['lease']['lease_id'], generation=active['lease']['generation'],
            prompt='actual user request', input_public=self.input_public())
        self.assertEqual(self.projected(), self.input_public() + pi_public(contexts(), version=4))
        self.assertEqual(runner.run.call_count, 1)

    def test_unknown_recovery_preserves_actual_input_without_executing(self):
        c = self.c
        active = self.service.active('conversation')
        lease = active['lease']
        boundary = c.recovery_boundary()
        ref = TurnInputs(self.root).write('conversation', self.tid, self.input_public())
        co = self.service.coordinator
        row = co.begin_round('conversation', lease['lease_id'], lease['generation'],
            attempt_id='interrupted', input_ref=ref,
            recovery={'phase':'reserved','boot_id':'old','boundary':boundary})
        co.mark_dispatched('conversation', lease['lease_id'], lease['generation'],
            attempt_id=row['attempt_id'], process={'pid':1,'pgid':1,'start_token':'old'})
        self.archive(contexts(), 'unknown')
        recovered = c.recover_suffix(boundary, {'turn_id':self.tid})
        c.receipts.write(c.binding, self.tid, recovered['locator'])
        inflight = co.state('conversation')['inflight']
        CrashRecovery(self.service)._settle_unknown('conversation', inflight,
            c.binding, lease, c, c._ref(self.tid), recovered['locator'])
        self.assertEqual(self.projected(), self.input_public() + pi_public(contexts(), version=4))

    def test_existing_v3_receipt_is_not_upgraded(self):
        entries = [{'type': 'message', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'done'}]}}]
        revision = self.archive(entries)
        receipt = self.c.receipts.write(self.c.binding, self.tid, {
            'turn_id': self.tid, 'terminal_state': 'completed', 'native_revision': revision,
            'projection_version': 3, 'unfinished_tool_ids': []})
        before = self.c.resolve_source(self.c._ref(self.tid))
        self.assertEqual(self.c.terminal_locator(self.tid, 'completed', revision)['projection_version'], 3)
        self.assertEqual(digest(self.c.resolve_source(self.c._ref(self.tid))['public']), digest(before['public']))
        self.assertEqual(self.c.receipts.read(self.c.binding, self.tid), receipt)


class NativeContextMessages(PiFixture):
    def test_real_sdk_context_entries_recover_without_model_or_tool(self):
        c = self.connector()
        boundary = c.recovery_boundary()
        script = r'''
import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';
const [sdk, file, dir, cwd] = process.argv.slice(1);
const {SessionManager} = await import(pathToFileURL(resolve(sdk, 'dist/core/session-manager.js')));
const {convertToLlm} = await import(pathToFileURL(resolve(sdk, 'dist/core/messages.js')));
const sm = SessionManager.open(file, dir, cwd);
const anchor = sm.getLeafId();
sm.branchWithSummary(anchor, 'branch summary');
sm.appendCustomMessageEntry('test-extension', 'hidden extension context', false, {notContext: 'metadata'});
const entries = sm.getEntries().filter(e => ['branch_summary','custom_message'].includes(e.type));
const messages = sm.buildSessionContext().messages.filter(m => ['branchSummary','custom'].includes(m.role));
console.log(JSON.stringify({entries, llm: convertToLlm(messages)}));
'''
        run = subprocess.run([self.node, '--input-type=module', '-e', script,
            self.sdk, c.path, c.directory, str(self.cwd)], text=True, capture_output=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        native = json.loads(run.stdout)
        self.assertEqual([m['role'] for m in native['llm']], ['user', 'user'])
        before = Path(c.path).read_bytes()
        recovered = c.recover_suffix(boundary)
        self.assertEqual(recovered['public'], pi_public(native['entries'], version=4))
        self.assertEqual(recovered['unfinished_tool_ids'], [])
        tid = self.cid + ':1'
        c.receipts.write(c.binding, tid, recovered['locator'])
        ref = {'kind': 'native_round', 'engine': 'pi', 'session_id': self.sid,
               'entry_id': tid, 'source_key': f'pi:{self.sid}:{tid}'}
        self.assertEqual(c.resolve_source(ref)['public'], recovered['public'])
        self.assertEqual(c.recover_suffix(boundary)['public'], recovered['public'])
        self.assertEqual(Path(c.path).read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
