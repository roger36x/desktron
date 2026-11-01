"""Claude recap reads the admitted context, never the whole archive in one batch."""
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'lib'))
import shepherd.recap as native_recap
from shepherd.service import Service
from shepherd.store import InvalidState


def _entry(binding, index, size):
    return {'type':'user', 'uuid':f'00000000-0000-4000-8000-{index:012d}',
            'sessionId':binding['native_session_id'],
            'message':{'role':'user', 'content':'x'*size}}


class RecapAdmission(unittest.TestCase):
    def test_long_view_is_written_in_bounded_batches(self):
        with tempfile.TemporaryDirectory() as root:
            service = Service(root)
            service.create('recap', 'claude', root)
            derived = service.connector(service.active('recap')['binding'])
            entries = [_entry(derived.binding, i, 1024*1024) for i in range(20)]
            # One 20 MB transaction is the failure seen on long conversations.
            with self.assertRaisesRegex(InvalidState, '批次过大'):
                derived.native.append(entries, derived.native.revision())
            native_recap._append_bounded(derived.native, entries)
            self.assertEqual([row['uuid'] for row in derived.native.load()],
                             [row['uuid'] for row in entries])

    def test_view_entries_drop_prompt_snapshots_and_relink_chain(self):
        derived = mock.Mock(native_session_id='derived', binding={})
        checkpoint = {'payload':{'native':[
            {'type':'attachment', 'uuid':'a', 'sessionId':'old',
             'attachment':{'type':'prompt_snapshot', 'systemPrompt':['x']}},
            {'type':'user', 'uuid':'u1', 'sessionId':'old', 'parentUuid':'gone'},
            {'type':'system', 'subtype':'compact_boundary', 'uuid':'b', 'parentUuid':None},
            {'type':'assistant', 'uuid':'u2', 'sessionId':'old'},
        ]}}
        rows = native_recap._admitted_view_entries(derived, checkpoint, [])
        self.assertEqual([row['uuid'] for row in rows], ['u1', 'b', 'u2'])
        self.assertEqual([row.get('sessionId') for row in rows], ['derived', None, 'derived'])
        self.assertEqual([row['parentUuid'] for row in rows], [None, None, 'b'])
        self.assertEqual(checkpoint['payload']['native'][1]['sessionId'], 'old')

    def test_unified_claude_uses_read_only_admission_not_full_projection(self):
        run = mock.Mock(); run.cancelled = threading.Event()
        binding = {'binding_id':'ubind_claude_x', 'engine':'claude', 'native_session_id':'n'}
        connector = mock.Mock(unified=True, binding=binding, engine='claude', native_cwd='/tmp')
        connector.context.return_value = ('key', {'payload':{'native':[]}}, ['admitted'])
        worker = mock.Mock(SRC_PEER='peer'); worker.system_body.return_value = 'system'
        worker.options_catalog.return_value = {}
        ad = mock.Mock(); ad._connector.return_value = connector
        ad._worker.return_value = worker; ad._recap_context_ready.return_value = True
        seen = {}

        @contextmanager
        def view(conn, checkpoint, rounds, opts):
            seen['rounds'] = rounds
            yield 'derived'

        snap = {'source':'public', 'opts':{'model':'m'},
                '_shepherd_source':{'state':{'rounds':[]}, 'binding':binding}}
        with mock.patch.object(native_recap.ReadProjector, 'project') as project, \
             mock.patch.object(native_recap, '_admitted_claude_view', side_effect=view), \
             mock.patch.object(native_recap, '_generate_native', return_value='OK') as native, \
             mock.patch('engine_adapters.recap._engine_env', return_value={}), \
             mock.patch('claude_token.apply', side_effect=lambda env, log=None: env):
            self.assertEqual(native_recap.generate(ad, 'sid', run, snap), 'OK')
        project.assert_not_called()
        self.assertTrue(connector.context.call_args.kwargs['read_only'])
        self.assertTrue(connector.context.call_args.args[0]['_context_admission'])
        self.assertEqual(seen['rounds'], ['admitted'])
        self.assertEqual(native.call_args.args[0], 'derived')


if __name__ == '__main__':
    unittest.main()
