"""Pi failed streaming drafts are not dispatched tools; old receipts stay stable."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from shepherd.coordinator import digest
from shepherd.harness import ExecutionHarness
from shepherd.pi_terminal import assistant_terminal
from shepherd.service import Service
from shepherd.source_codec import pi_public, seal_incomplete_tools
from shepherd.store import InvalidState, SourceVerificationError
from test_shepherd_images import roundtrip


def call(call_id, stop='toolUse'):
    return {'type': 'message', 'message': {'role': 'assistant', 'stopReason': stop,
        'content': [{'type': 'toolCall', 'id': call_id, 'name': 'bash',
                     'arguments': {'command': 'never replay'}}]}}


def result(call_id):
    return {'type': 'message', 'message': {'role': 'toolResult', 'toolCallId': call_id,
        'toolName': 'bash', 'content': [{'type': 'text', 'text': 'actual result'}]}}


def recovered_entries(stop='error'):
    return [call('draft', stop), call('executed'), result('executed'),
            {'type': 'message', 'message': {'role': 'assistant', 'stopReason': 'stop',
             'content': [{'type': 'text', 'text': 'done'}]}}]


class FailedGeneration(unittest.TestCase):
    def test_pi_length_stop_is_not_completed(self):
        self.assertEqual(assistant_terminal({
            'role': 'assistant', 'stopReason': 'length',
            'content': [{'type': 'text', 'text': 'partial'}]}), 'failed_recoverable')
        self.assertEqual(assistant_terminal({
            'role': 'assistant', 'stopReason': 'max_tokens'}), 'failed_recoverable')
        self.assertEqual(assistant_terminal({
            'role': 'assistant', 'stopReason': 'stop'}), '')

    def test_failed_and_aborted_drafts_are_inert_and_lossless(self):
        for stop in ('error', 'aborted'):
            entries = recovered_entries(stop)
            before = copy.deepcopy(entries)
            public = pi_public(entries, version=3)
            self.assertEqual(entries, before)
            self.assertEqual(seal_incomplete_tools(public)['unfinished_tool_ids'], [])
            self.assertEqual([x['call_id'] for x in public if x['type'] == 'function_call'], ['executed'])
            marker = public[0]['content'][0]['text']
            self.assertIn('未派发', marker)
            self.assertEqual(json.loads(marker.split('] ', 1)[1])['toolCall'], entries[0]['message']['content'][0])
            for engine in ('pi', 'claude', 'codex'):
                self.assertEqual(roundtrip(engine, {'round_id': 'conversation:1', 'offset': 1,
                    'entry_hash': 'a'*64, 'public': public}), public)

    def test_v2_projection_is_unchanged(self):
        public = pi_public(recovered_entries(), version=2)
        self.assertEqual(public, [
            {'type': 'function_call', 'call_id': 'draft', 'name': 'bash',
             'arguments': '{"command":"never replay"}'},
            {'type': 'function_call', 'call_id': 'executed', 'name': 'bash',
             'arguments': '{"command":"never replay"}'},
            {'type': 'function_call_output', 'call_id': 'executed', 'output': 'actual result'},
            {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'done'}]}])
        self.assertEqual(seal_incomplete_tools(public)['unfinished_tool_ids'], ['draft'])

    def test_real_missing_results_and_non_failure_stops_remain_unfinished(self):
        for stop in ('toolUse', 'stop', 'length', 'pending', None):
            public = pi_public([call('real', stop)], version=3)
            self.assertEqual(seal_incomplete_tools(public)['unfinished_tool_ids'], ['real'])
        entry = call('real', 'toolUse')
        entry['message']['errorMessage'] = 'WebSocket error'
        self.assertEqual(seal_incomplete_tools(pi_public([entry], version=3))['unfinished_tool_ids'], ['real'])

    def test_conflicting_results_duplicates_and_invalid_ids_still_reject(self):
        for entries in ([call('draft', 'error'), result('draft')],
                        [result('draft'), call('draft', 'error')],
                        [call('draft', 'error'), call('draft')],
                        [call('', 'error')]):
            with self.subTest(entries=entries), self.assertRaises(InvalidState):
                pi_public(entries, version=3)

    def test_custom_and_mapped_failed_drafts_remain_inert(self):
        entry = call('draft', 'error')
        entry['message']['content'][0].update(name='custom', arguments={'input': 'partial'})
        entries = [
            {'type': 'custom', 'customType': 'shepherd-tool-call-ids-v1',
             'data': {'ids': [{'native_id': 'draft', 'public_id': 'original'}]}},
            {'type': 'custom', 'customType': 'shepherd-custom-tool-v1',
             'data': {'call_id': 'draft', 'name': 'custom'}}, entry]
        public = pi_public(entries, version=3)
        self.assertEqual([x['type'] for x in public], ['message'])
        self.assertEqual(seal_incomplete_tools(public)['unfinished_tool_ids'], [])


class UnifiedFailedGeneration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = Service(self.root, storage='unified')
        self.service.create('conversation', 'pi', self.root)
        self.c = self.service.connector(self.service.active('conversation')['binding'])
        self.tid = 'conversation:1'

    def archive(self, entries, terminal='completed'):
        c = self.c
        c.archive.begin(c.binding, self.tid)
        for entry in entries:
            c.archive.append(c.binding, self.tid, {'type': 'pi_entry', 'value': entry})
        revision = c.archive.finish(c.binding, self.tid, terminal)
        return revision

    def test_new_receipt_commits_and_reloads_without_replaying_tools(self):
        c = self.c
        entries = recovered_entries()
        def run(*, round_id, on_dispatched, **kwargs):
            on_dispatched()
            revision = self.archive(entries)
            c.receipts.write(c.binding, round_id, c.terminal_locator(round_id, 'completed', revision))
            return {'source': c._ref(round_id), 'terminal_state': 'completed', 'native_revision': revision}
        runner = SimpleNamespace(run=mock.Mock(side_effect=run))
        active = self.service.active('conversation')
        row = ExecutionHarness(self.service.coordinator, c, runner).run('conversation',
            lease_id=active['lease']['lease_id'], generation=active['lease']['generation'], prompt='question')
        self.assertEqual(runner.run.call_count, 1)
        self.assertEqual(c.receipts.read(c.binding, self.tid)['locator']['projection_version'], 4)
        before = c.resolve_source(row['source'])
        restarted = Service(self.root, storage='unified')
        rebuilt = restarted.connector(c.binding)
        with mock.patch.object(runner, 'run', side_effect=AssertionError('must not replay')):
            self.assertEqual(rebuilt.resolve_source(row['source']), before)
            rebuilt.verify()
        self.assertEqual([e['value'] for e in before['source']['events']], entries)
        self.assertEqual(seal_incomplete_tools(before['public'])['unfinished_tool_ids'], [])

    def test_completed_archive_can_recover_receipt_idempotently_without_source_changes(self):
        self.archive(recovered_entries())
        before = self.c.archive.read(self.c.binding, self.tid)
        first = self.c.recover_terminal_receipt(self.tid)
        self.assertEqual(self.c.recover_terminal_receipt(self.tid), first)
        self.assertEqual(first['locator']['projection_version'], 4)
        self.assertEqual(first['locator']['unfinished_tool_ids'], [])
        self.assertEqual(self.c.archive.read(self.c.binding, self.tid), before)

    def test_legacy_receipts_and_public_hashes_do_not_change(self):
        for version in (None, 2):
            with self.subTest(version=version):
                self.tid = 'conversation:' + ('2' if version else '1')
                revision = self.archive(recovered_entries(), 'failed_recoverable')
                locator = {'turn_id': self.tid, 'terminal_state': 'failed_recoverable',
                           'native_revision': revision, 'unfinished_tool_ids': ['draft']}
                if version is not None:
                    locator['projection_version'] = version
                before = self.c.receipts.write(self.c.binding, self.tid, locator)
                expected = seal_incomplete_tools(pi_public(recovered_entries(), version=2))['public']
                actual = self.c.resolve_source(self.c._ref(self.tid))
                self.assertEqual(actual['public'], expected)
                self.assertEqual(digest(actual['public']), digest(expected))
                self.assertEqual(self.c.receipts.read(self.c.binding, self.tid), before)
                self.assertEqual(self.c.terminal_locator(self.tid, 'failed_recoverable', revision)['projection_version'], 2)

    def test_recovery_suffix_uses_same_version_as_later_receipt_reads(self):
        boundary = self.c.recovery_boundary()
        self.archive(recovered_entries())
        recovered = self.c.recover_suffix(boundary, {'turn_id': self.tid})
        self.assertEqual(recovered['locator']['projection_version'], 4)
        self.c.receipts.write(self.c.binding, self.tid, recovered['locator'])
        self.assertEqual(self.c.resolve_source(self.c._ref(self.tid))['public'], recovered['public'])

    def test_real_unfinished_tools_still_block_completed_turn(self):
        revision = self.archive([call('real')])
        with self.assertRaisesRegex(SourceVerificationError, 'unfinished tools'):
            self.c.terminal_locator(self.tid, 'completed', revision)
        self.assertIsNone(self.c.receipts.read(self.c.binding, self.tid, required=False))

    def test_unknown_projection_versions_are_not_silently_reinterpreted(self):
        source = {'events': []}
        for version in (0, 5, '4', True):
            with self.assertRaises(SourceVerificationError):
                self.c._public(source, version=version)


if __name__ == '__main__':
    unittest.main()
