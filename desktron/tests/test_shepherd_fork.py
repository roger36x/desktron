"""Shepherd fork preserves history, engine and independent ownership."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from engine_adapters.bus import EventBus
from engine_adapters.shepherd import ShepherdAdapter
from shepherd.fork import copy_history
from shepherd.harness import ExecutionHarness
from shepherd.projector import ReadProjector
from shepherd.service import Service
from shepherd.source_resolver import SourceResolver
from shepherd.store import Conflict
from shepherd.unified_archive import UnifiedArchive, _ArchiveConnections
from test_shepherd_unified_storage import JournalRunner, ENGINES


class ForkHistory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(_ArchiveConnections.close_all)
        self.root = Path(self.temp.name)
        self.service = Service(self.root / 'state', storage='unified')

    def turn(self, sid, engine, *, seq=1):
        active = self.service.active(sid)
        connector = self.service.connector(active['binding'])
        lease = active['lease']
        return ExecutionHarness(self.service.coordinator, connector, JournalRunner(connector)).run(
            sid, lease_id=lease['lease_id'], generation=lease['generation'],
            prompt='question', attempt_id=f'attempt_{seq}', input_public=[{
                'type': 'message', 'role': 'user', 'content': [
                    {'type': 'input_text', 'text': f'question {seq}'}]}])

    def project(self, sid):
        state = self.service.coordinator.state(sid)
        return ReadProjector(self.service.store, SourceResolver([
            self.service.connector(b) for b in state['bindings'].values()])).project(sid)

    def test_each_engine_branches_independently_and_continues_after_source_delete(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                original, target = 'source_' + engine, 'fork_' + engine
                self.service.create(original, engine, self.root)
                self.turn(original, engine)
                before = self.service.store.load_raw(original)
                public = self.project(original)[0]['public']
                self.service.create(target, engine, self.root)
                copy_history(self.service, original, target, 1)
                self.assertEqual(before, self.service.store.load_raw(original))
                old = self.service.active(original)['binding']
                new = self.service.active(target)['binding']
                self.assertEqual(engine, new['engine'])
                self.assertNotEqual(old['native_session_id'], new['native_session_id'])
                self.assertNotEqual(old['ownership_nonce'], new['ownership_nonce'])
                from shepherd.physical_delete import mark_deleted, purge
                state = self.service.coordinator.tombstone_for_delete(original)
                state = self.service.store.load_raw(original, allow_deleted=True)
                mark_deleted(self.service.root, original)
                purge(self.service.root, original, state)
                self.assertEqual(public, self.project(target)[0]['public'])
                self.turn(target, engine, seq=2)
                self.assertEqual(2, len(self.project(target)))

    def test_history_cut_excludes_later_turns_and_can_be_forked_again(self):
        self.service.create('source', 'claude', self.root)
        self.turn('source', 'claude')
        self.turn('source', 'claude', seq=2)
        self.service.create('target', 'claude', self.root)
        copy_history(self.service, 'source', 'target', 1)
        self.assertEqual(1, len(self.project('target')))
        self.assertNotIn('question 2', json.dumps(self.project('target')))
        self.service.create('nested', 'claude', self.root)
        copy_history(self.service, 'target', 'nested', 1)
        self.assertEqual(self.project('target')[0]['public'], self.project('nested')[0]['public'])

    def test_tampered_snapshot_and_wrong_owner_are_rejected(self):
        self.service.create('source', 'pi', self.root)
        self.turn('source', 'pi')
        self.service.create('target', 'pi', self.root)
        copy_history(self.service, 'source', 'target', 1)
        state = self.service.store.load_raw('target')
        binding = next(b for b in state['bindings'].values() if b['status'] == 'retired')
        wrong = {**binding, 'conversation_id': 'source'}
        with self.assertRaises(Conflict):
            self.service.connector(wrong)
        archive = UnifiedArchive(self.service.root, 'target')
        with archive.db() as db:
            db.execute("UPDATE fork_snapshots SET payload='{}'")
        with self.assertRaises(Conflict):
            self.project('target')

    def test_corrupt_source_never_becomes_trusted_snapshot(self):
        self.service.create('source', 'codex', self.root)
        self.turn('source', 'codex')
        archive = UnifiedArchive(self.service.root, 'source')
        with archive.db() as db:
            db.execute("UPDATE events SET payload='{}'")
        self.service.create('target', 'codex', self.root)
        with self.assertRaises(Conflict):
            copy_history(self.service, 'source', 'target', 1)
        self.assertEqual([], self.service.store.load_raw('target')['turns'])

    def test_compression_checkpoint_is_independent_and_keeps_coverage(self):
        self.service.create('source', 'claude', self.root)
        self.turn('source', 'claude')
        old = self.service.store.load_raw('source')['turns'][0]
        archive = UnifiedArchive(self.service.root, 'source')
        payload = {'source_hash': old['source_hash'], 'through_seq': 1,
                   'native': [{'type': 'fixture', 'text': 'compressed memory'}]}
        archive.checkpoint('context', turn_id=old['turn_id'], through_seq=1, payload=payload)
        self.service.create('target', 'claude', self.root)
        copy_history(self.service, 'source', 'target', 1)
        saved = UnifiedArchive(self.service.root, 'target').checkpoint('context')
        new = self.service.store.load_raw('target')['turns'][0]
        self.assertEqual('target:1', saved['turn_id'])
        self.assertEqual(payload['native'], saved['payload']['native'])
        self.assertEqual(new['source_hash'], saved['payload']['source_hash'])


class ForkFacade(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(_ArchiveConnections.close_all)
        self.root = Path(self.temp.name)
        workspace = self.root / 'work'; workspace.mkdir()
        (workspace / 'prompts').mkdir(); (workspace / 'prompts/style.md').write_text('')
        self.adapter = ShepherdAdapter(str(self.root / 'data'), str(workspace), EventBus())
        for name in ('_schedule_storage_promotion', '_schedule_engine_prepare'):
            patch = mock.patch.object(self.adapter, name); patch.start(); self.addCleanup(patch.stop)

    def seed(self, engine='claude'):
        ad = self.adapter
        sid = ad.create_session(opts={'active_engine': engine, 'model': 'fixture'})['id']
        active = ad.service.active(sid); lease = active['lease']
        connector = ad.service.connector(active['binding'])
        ExecutionHarness(ad.service.coordinator, connector, JournalRunner(connector)).run(
            sid, lease_id=lease['lease_id'], generation=lease['generation'],
            prompt='hello', attempt_id='attempt_100', input_public=[{
                'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'hello'}]}])
        ad._rewrite_msgs(sid, msgs=[
            {'info': {'id': 'user', 'role': 'user', 'sessionID': sid, 'seq': 100},
             'parts': [{'type': 'text', 'text': 'hello'}]},
            {'info': {'id': 'assistant', 'role': 'assistant', 'sessionID': sid, 'seq': 100},
             'parts': [{'type': 'text', 'text': 'done'}]}])
        return sid

    def test_fork_retains_shepherd_engine_model_and_anchors_at_reply(self):
        ad = self.adapter
        for engine in ENGINES:
            with self.subTest(engine=engine):
                sid = self.seed(engine)
                row = ad.fork_session(sid, 'assistant')
                self.assertTrue(row['shepherd_mode'])
                self.assertEqual(engine, ad.selected_engine(row['id']))
                self.assertEqual(ad.session_options(sid), ad.session_options(row['id']))
                self.assertEqual(sid, row['forked_from'])
                self.assertEqual(['hello', 'done'], [m['parts'][0]['text'] for m in ad.messages(row['id'])])
                self.assertTrue(all(m['info']['sessionID'] == row['id'] for m in ad.messages(row['id'])))

    def test_busy_and_missing_anchor_do_not_create_empty_sessions(self):
        ad = self.adapter; sid = self.seed()
        before = set(ad.index)
        lock = ad._turn_locks.setdefault(sid, threading.Lock())
        with lock, self.assertRaises(Conflict):
            ad.fork_session(sid, 'assistant')
        with self.assertRaises(Conflict):
            ad.fork_session(sid, 'missing')
        self.assertEqual(before, set(ad.index))

    def test_copied_attachment_survives_source_deletion(self):
        import base64
        ad = self.adapter; sid = self.seed()
        url = ad.asset_save(sid, base64.b64encode(b'fixture image').decode())
        messages = ad.messages(sid)
        messages[0]['parts'].append({'type': 'file', 'url': url})
        ad._rewrite_msgs(sid, msgs=messages)
        child = ad.fork_session(sid, 'assistant')['id']
        copied = ad.messages(child)[0]['parts'][-1]['url']
        self.assertTrue(copied.startswith(f'/session/{child}/asset/'))
        ad.delete_session(sid)
        path = ad.asset_path(child, copied.rsplit('/', 1)[1])
        self.assertEqual(b'fixture image', Path(path).read_bytes())

    def test_failure_removes_unpublished_child_and_its_snapshot(self):
        ad = self.adapter; sid = self.seed()
        before = set(ad.index)
        with mock.patch.object(ad, '_copy_fork_messages', side_effect=RuntimeError('fixture failure')):
            with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
                ad.fork_session(sid, 'assistant')
        self.assertEqual(before, set(ad.index))
        self.assertEqual([sid], ad.service.store.conversation_ids())
