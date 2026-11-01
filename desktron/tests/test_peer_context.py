"""Fast ask: stable evidence, bounded work, no generation or index writes."""
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

import peer_context
from peer_context import read_context, fetch, refresh_after_settle
from shepherd.history_retrieval import HistoryRetrieval
import test_shepherd_history_retrieval as history_fixture


class PeerContextTest(unittest.TestCase):
    setUp = history_fixture.HistoryRetrievalTest.setUp
    turn = history_fixture.HistoryRetrievalTest.turn
    history = history_fixture.HistoryRetrievalTest.history

    def target(self):
        return {'sid': 'conversation', 'engine': 'shepherd',
                'data_root': str(self.root.parent/'adapter')}

    def test_verified_evidence_ignores_tool_output_and_does_not_build_index(self):
        self.history()
        with mock.patch.object(HistoryRetrieval, 'sync', side_effect=AssertionError('no sync')):
            packet = read_context(self.target(), '量子香蕉')
        self.assertTrue(packet['ok'])
        self.assertIn('index_missing', packet['warnings'])
        self.assertFalse((self.root/'retrieval').exists())
        text = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn('secret tool output', text)
        self.assertNotIn('read_secret', text)
        self.assertIn('蓝色方案', text)
        for row in packet['records']:
            self.assertTrue(HistoryRetrieval(self.root).verify(row['citation'])['ok'])

    def test_busy_writer_does_not_block_reader_and_active_turn_is_excluded(self):
        self.history()
        active = self.service.active('conversation')
        self.service.coordinator.begin_round(
            "conversation", active["lease"]["lease_id"], active["lease"]["generation"])
        # A locked turn/index must not be consulted by this read path.
        from durable_json import locked
        with locked(str(self.root/'retrieval'/'locks'/'conversation')):
            before = time.monotonic()
            packet = fetch(self.target(), '香蕉')
        self.assertTrue(packet['ok'], packet)
        self.assertLess(time.monotonic()-before, 5)
        self.assertEqual(packet['snapshot']['head_seq'], 2)
        self.assertTrue(packet['snapshot']['active_turn_excluded'])

    def test_archive_writer_does_not_block_evidence_read(self):
        self.history()
        connector = self.service.connector(self.service.active('conversation')['binding'])
        with connector.archive.db() as writer:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("UPDATE turns SET terminal=terminal")
            packet = fetch(self.target(), '香蕉', 2)
        self.assertTrue(packet['ok'], packet)
        self.assertTrue(packet['records'])
        self.assertFalse(any(w.startswith('source_unavailable') for w in packet['warnings']))

    def test_existing_index_lag_is_reported_and_recent_turn_is_read(self):
        self.history()
        HistoryRetrieval(self.root).sync('conversation')
        self.turn('codex', [{'type': 'message', 'role': 'assistant', 'content': [
            {'type': 'output_text', 'text': '最新改变选择绿色方案'}]}])
        with mock.patch.object(HistoryRetrieval, 'sync', side_effect=AssertionError('no sync')):
            packet = read_context(self.target(), '量子香蕉')
        self.assertIn('index_lag', packet['warnings'])
        self.assertIn(1, packet['coverage']['matched_seqs'])
        self.assertIn('绿色方案', json.dumps(packet, ensure_ascii=False))

    def test_damaged_index_still_returns_verified_recent_evidence_with_warning(self):
        self.history()
        r = HistoryRetrieval(self.root)
        r.path.parent.mkdir()
        r.path.write_bytes(b'broken sqlite')
        packet = read_context(self.target(), '香蕉')
        self.assertIn('index_unavailable', packet['warnings'])
        self.assertTrue(packet['records'])

    def test_unverifiable_source_is_not_returned(self):
        self.history()
        with mock.patch('shepherd.projector.ReadProjector.project', side_effect=ValueError('hash mismatch')):
            packet = read_context(self.target(), '香蕉')
        self.assertEqual(packet['records'], [])
        self.assertTrue(packet['partial'])
        self.assertIn('source_unavailable:1', packet['warnings'])

    def test_append_during_read_keeps_one_head(self):
        self.history()
        from shepherd.projector import ReadProjector
        original = ReadProjector.project
        appended = []
        def project(projector, *args, **kwargs):
            if not appended:
                appended.append(True)
                self.turn('codex', [{'type': 'message', 'role': 'assistant', 'content': [
                    {'type': 'output_text', 'text': 'concurrent new turn'}]}])
            return original(projector, *args, **kwargs)
        with mock.patch.object(ReadProjector, 'project', project):
            packet = read_context(self.target(), '香蕉')
        self.assertEqual(packet['snapshot']['head_seq'], 2)
        self.assertNotIn('concurrent new turn', json.dumps(packet))

    def test_changed_prefix_rejects_whole_packet(self):
        self.history()
        original = HistoryRetrieval._prefix_hash
        calls = []
        def changed(rows, through=None):
            calls.append(True)
            return original(rows, through) if len(calls) == 1 else 'changed'
        with mock.patch.object(HistoryRetrieval, '_prefix_hash', side_effect=changed):
            with self.assertRaisesRegex(ValueError, '历史发生变化'):
                read_context(self.target(), '香蕉')

    def test_reader_deadline_terminates_a_real_process(self):
        original = subprocess.run
        def slow_reader(command, **kwargs):
            return original([sys.executable, '-c', 'import time; time.sleep(10)'], **kwargs)
        before = time.monotonic()
        with mock.patch.object(peer_context.subprocess, 'run', slow_reader):
            packet = fetch(self.target(), 'question', .1)
        self.assertEqual(packet['code'], 'read_timeout')
        self.assertLess(time.monotonic()-before, 1)

    def test_native_excludes_unfinished_turn_and_non_text_parts(self):
        folder = self.root.parent/'adapter'/'pi'
        folder.mkdir(parents=True)
        def msg(role, text, done=False):
            info = {'id': text, 'role': role}
            if done:
                info.update(done_at=1, terminal={'status': 'completed'})
            return {'info': info, 'parts': [{'type': 'text', 'text': text},
                {'type': 'tool', 'text': 'secret tool output'}, {'type': 'reasoning', 'text': 'private reasoning'}]}
        rows = [msg('user', 'first'), msg('assistant', 'answer', True), msg('user', 'unfinished')]
        (folder/'native.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows), encoding='utf-8')
        packet = read_context({'sid': 'native', 'engine': 'pi', 'data_root': str(folder.parent)}, 'first')
        self.assertEqual([r['text'] for r in packet['records']], ['first', 'answer'])
        self.assertTrue(packet['snapshot']['active_turn_excluded'])
        self.assertEqual(packet['coverage']['kind'], 'recent_projection_only')

    def test_output_budget_and_redaction(self):
        self.turn('claude', [{'type': 'message', 'role': 'assistant', 'content': [
            {'type': 'output_text', 'text': 'token=secretvalue '+('长文本'*20000)}]}])
        packet = read_context(self.target(), '长文本')
        self.assertTrue(packet['truncated'])
        self.assertNotIn('secretvalue', json.dumps(packet))
        self.assertLess(len(json.dumps(packet, ensure_ascii=False).encode()), peer_context.MAX_BYTES)
        self.assertLess(peer_context.estimate_tokens(packet), peer_context.MAX_TOKENS)

    def test_old_hit_survives_long_recent_context_and_text_truncation(self):
        self.turn('claude', [{'type': 'message', 'role': 'assistant', 'content': [
            {'type': 'output_text', 'text': '早期背景'*1500 + 'needle-keyword 的最终结论'}]}])
        for _ in range(4):
            self.turn('claude', [{'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': '最新无关正文'*3000}]}])
        HistoryRetrieval(self.root).sync('conversation')
        packet = read_context(self.target(), 'needle-keyword')
        self.assertEqual(packet['records'][0]['seq'], 1)
        hit = next(row for row in packet['records'] if row['seq'] == 1 and row['role'] == 'assistant')
        self.assertIn('needle-keyword', hit['text'])
        self.assertGreater(hit['text_offset'], 0)

    def test_timeout_kills_only_reader_and_caps_old_300_second_flag(self):
        with mock.patch.object(peer_context.subprocess, 'run', side_effect=subprocess.TimeoutExpired('reader', 5)) as run:
            packet = fetch(self.target(), 'question', 300)
        self.assertEqual(run.call_args.kwargs['timeout'], 5)
        self.assertEqual(packet['code'], 'read_timeout')

    def test_background_index_refresh_is_detached_and_coalesced(self):
        self.history()
        with mock.patch.object(peer_context.threading, 'Thread') as thread:
            try:
                refresh_after_settle(self.root, 'conversation')
                refresh_after_settle(self.root, 'conversation')
                self.assertEqual(thread.call_count, 1)
                self.assertEqual(len(peer_context._INDEX_PENDING), 1)
                with mock.patch.object(HistoryRetrieval, 'sync') as sync:
                    peer_context._refresh_pending()
                    sync.assert_called_once_with('conversation')
            finally:
                peer_context._INDEX_PENDING.clear()
                peer_context._INDEX_RUNNING = False


if __name__ == '__main__':
    unittest.main()
