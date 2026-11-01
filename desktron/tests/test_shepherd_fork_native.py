"""Real native engines continue independent same-engine Shepherd branches."""
import tempfile
import unittest

import pytest
from shepherd_product_fixture import ProductFixture
from shepherd.projector import ReadProjector
from shepherd.source_resolver import SourceResolver


@pytest.mark.native
class ForkNative(unittest.TestCase):
    def test_same_engine_fork_continues_after_source_deletion_and_cold_reopen(self):
        for engine in ('claude', 'pi', 'codex'):
            with self.subTest(engine=engine):
                self.exercise_branch(engine)

    def test_compacted_fork_continues_after_source_deletion_and_cold_reopen(self):
        # Pi 1.2.0's source /compact fixture currently returns no checkpoint
        # before fork is called (also reproduced independently). Keep ordinary
        # Pi branching above covered; this does not certify Pi compaction.
        for engine in ('claude', 'codex'):
            with self.subTest(engine=engine):
                self.exercise_branch(engine, compact=True)

    def exercise_branch(self, engine, *, compact=False):
        with tempfile.TemporaryDirectory() as root:
            product = ProductFixture(root)
            try:
                prompt = 'Remember the branch fixture context.'
                if compact and engine == 'claude':
                    prompt += ' archived detail' * 16000
                error, ctx = product.turn(engine, prompt)
                self.assertIsNone(error)
                if compact:
                    result, _ = product.compact(engine)
                    self.assertTrue(result['ok'], result)
                    error, ctx = product.turn(engine, 'Continue after the compression boundary.')
                    self.assertIsNone(error)
                original = product.sid
                ad = product.adapter
                source_count = len(ad.service.coordinator.state(original)['turns'])
                ad._rewrite_msgs(original, msgs=[{
                    'info': {'id': ctx['msg_id'], 'role': 'assistant',
                             'sessionID': original, 'seq': ctx['seq']},
                    'parts': [{'type': 'text', 'text': 'branch fixture reply'}]}])
                before = len(product.model.requests)
                child = ad.fork_session(original, ctx['msg_id'])
                self.assertEqual(before, len(product.model.requests), 'fork must not call a model')
                self.assertTrue(child['shepherd_mode'])
                self.assertEqual(engine, ad.selected_engine(child['id']))
                product.sid = child['id']
                ad.delete_session(original)
                product.reopen()
                error, ctx = product.turn(engine, 'Continue the branch context.')
                self.assertIsNone(error)
                self.assertNotIn('_projection_failures', ctx)
                service = product.adapter.service
                state = service.coordinator.state(product.sid)
                self.assertEqual(['completed'] * (source_count + 1), [t['outcome'] for t in state['turns']])
                rows = ReadProjector(service.store, SourceResolver([
                    service.connector(b) for b in state['bindings'].values()])).project(product.sid)
                self.assertEqual(source_count + 1, len(rows))
            finally:
                product.close()
