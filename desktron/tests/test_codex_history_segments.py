"""Native revert segments retain the prefix but never revive the withdrawn tail."""
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import test_codex_recall as fixture
from codex_history import history_view, indexed_path


ESID = '019ee670-1111-7222-8333-444444444444'


class Segments(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.root = self.home / 'sessions'
        self.root.mkdir()
        self.cache = self.home / 'views'
        self.ad = fixture.ea.CodexAdapter(self.tmp.name, self.tmp.name, fixture.Bus())
        self.ad.ROLLOUT_DIRS = (str(self.root),)
        with mock.patch.object(self.ad, '_term_register'):
            self.sid = self.ad.create_session()['id']
        self.ad.index[self.sid]['engine_sid'] = ESID

    def tearDown(self):
        self.tmp.cleanup()

    def segment(self, birth, words, *, start=0, base=None, suffix=''):
        meta = {'id': ESID, 'timestamp': birth, 'history_mode': 'paginated'}
        if base is not None:
            meta['history_base'] = base
        rows = [{'type': 'session_meta', 'payload': meta}]
        for pos, word in enumerate(words):
            rows.append(fixture.CodexRecall.row(f'2026-09-11T10:00:{start+pos:02d}.000Z', 'user', word))
        for pos, row in enumerate(rows):
            row['ordinal'] = start + pos
        path = self.root / ('rollout-' + birth.replace(':', '-') + '-' + ESID + suffix + '.jsonl')
        path.write_bytes(b''.join(json.dumps(row).encode() + b'\n' for row in rows))
        return path

    @staticmethod
    def boundary(path, ordinal):
        offset = 0
        with open(path, 'rb') as stream:
            for line in stream:
                if json.loads(line)['ordinal'] == ordinal:
                    break
                offset += len(line)
        return {'thread_id': ESID, 'end_ordinal_exclusive': ordinal, 'end_byte_offset': offset}

    def chain(self):
        old = self.segment('2026-09-11T01:00:00Z', ['keep', 'withdrawn'])
        new = self.segment('2026-09-11T02:00:00Z', ['replacement', 'later'], start=2,
                           base=self.boundary(old, 2), suffix='_branch')
        return old, new

    def index(self, path):
        with closing(sqlite3.connect(self.home / 'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, rollout_path TEXT)')
            db.execute('INSERT OR REPLACE INTO threads VALUES (?, ?)', (ESID, str(path)))

    def test_resolve_current_index_even_after_old_path_was_cached(self):
        old, new = self.chain()
        self.index(old)
        self.assertEqual(self.ad._rollout_path(ESID), str(old))
        self.index(new)
        self.assertEqual(self.ad._rollout_path(ESID), str(new))

    def test_managed_home_index_precedes_symlink_target_index(self):
        old, new = self.chain()
        self.index(old)
        managed = self.home / 'managed'
        managed.mkdir()
        (managed / 'sessions').symlink_to(self.root, target_is_directory=True)
        with closing(sqlite3.connect(managed / 'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT)')
            db.execute('INSERT INTO threads VALUES (?, ?)', (ESID, str(new)))
        self.assertEqual(indexed_path((str(managed / 'sessions'),), ESID), str(new))

    def test_readonly_uri_is_explicit_for_standalone_python_sqlite(self):
        old, _new = self.chain()
        self.index(old)
        connect = sqlite3.connect
        with mock.patch('codex_history.sqlite3.connect', wraps=connect) as opened:
            self.assertEqual(indexed_path((str(self.root),), ESID), str(old))
        self.assertTrue(opened.call_args.kwargs['uri'])
        self.assertTrue(opened.call_args.args[0].endswith('?mode=ro'))

    def test_missing_active_index_path_does_not_fall_back_to_old(self):
        old, _new = self.chain()
        self.index(old.with_name('missing.jsonl'))
        self.assertEqual(self.ad._rollout_path(ESID), '')

    def test_discover_new_segment_without_index_invalidates_directory_cache(self):
        old = self.segment('2026-09-11T01:00:00Z', ['keep', 'withdrawn'])
        self.assertEqual(self.ad._rollout_path(ESID), str(old))
        new = self.segment('2026-09-11T02:00:00Z', ['replacement'], start=2,
                           base=self.boundary(old, 2), suffix='_branch')
        self.assertEqual(self.ad._rollout_path(ESID), str(new))

    def test_native_files_untouched_and_withdrawn_tail_not_reintroduced(self):
        old, new = self.chain()
        before = (old.read_bytes(), new.read_bytes())
        view = history_view(str(new), (str(self.root),), str(self.cache))
        payload = Path(view).read_text()
        self.assertIn('keep', payload)
        self.assertIn('replacement', payload)
        self.assertNotIn('withdrawn', payload)
        self.assertEqual(before, (old.read_bytes(), new.read_bytes()))
        stamp = os.stat(view).st_mtime_ns
        self.assertEqual(history_view(str(new), (str(self.root),), str(self.cache)), view)
        self.assertEqual(os.stat(view).st_mtime_ns, stamp)

    def test_nested_revert_clips_virtual_prefix_at_exact_boundary(self):
        _old, new = self.chain()
        view = history_view(str(new), (str(self.root),), str(self.cache))
        next_segment = self.segment('2026-09-11T03:00:00Z', ['newest'], start=4,
                                    base=self.boundary(view, 4), suffix='_second')
        payload = Path(history_view(str(next_segment), (str(self.root),), str(self.cache))).read_text()
        self.assertIn('replacement', payload)
        self.assertIn('newest', payload)
        self.assertNotIn('later', payload)
        self.assertNotIn('withdrawn', payload)

    def test_invalid_boundary_or_missing_parent_keeps_existing_projection(self):
        old, new = self.chain()
        data = new.read_bytes().splitlines(keepends=True)
        first = json.loads(data[0])
        first['payload']['history_base']['end_byte_offset'] -= 1
        new.write_bytes(json.dumps(first).encode() + b'\n' + b''.join(data[1:]))
        self.assertIsNone(self.ad._rollout_tail(str(new), self.sid, 50))
        old.unlink()
        self.assertIsNone(self.ad._rollout_tail(str(new), self.sid, 50))

    def test_tail_and_full_grouping_both_include_retained_history(self):
        _old, new = self.chain()
        for msgs in (self.ad._rollout_tail(str(new), self.sid, 50),
                     self.ad._group_rollout(str(new), self.sid)):
            text = ' '.join(p.get('text', '') for m in msgs for p in m['parts'])
            self.assertIn('keep', text)
            self.assertIn('replacement', text)
            self.assertNotIn('withdrawn', text)

    def test_empty_prefix_does_not_require_parent(self):
        new = self.segment('2026-09-11T02:00:00Z', ['fresh'], start=0, suffix='_empty',
                           base={'thread_id': ESID, 'end_byte_offset': 0, 'end_ordinal_exclusive': 0})
        msgs = self.ad._rollout_tail(str(new), self.sid, 50)
        self.assertEqual(len(msgs), 1)

    def test_more_old_turns_cannot_overwrite_newer_user_message(self):
        old, _new = self.chain()
        local = {'info': {'id': 'msg_recent', 'role': 'user', 'at': 1900000000000},
                 'parts': [{'type': 'text', 'text': 'new local input'}]}
        self.ad._append_msg(self.sid, local)
        with mock.patch.object(self.ad, '_rollout_path', return_value=str(old)):
            self.ad.import_session(ESID, self.sid)
        self.assertEqual(self.ad.messages(self.sid)[-1]['parts'][0]['text'], 'new local input')


if __name__ == '__main__':
    unittest.main()
