"""Native source resolution stays ownership-bound and fail-closed."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron/lib'))

from shepherd import Conflict, InvalidState, SourceResolver


class FakeConnector:
    def __init__(self, engine='pi', session='session_1', result=None):
        self.engine = engine
        self.native_session_id = session
        self.result = result or {'source': {'native': 1}, 'public': [{'type': 'message'}]}
        self.seen = []

    def resolve_source(self, source_ref):
        self.seen.append(source_ref)
        return self.result


class SourceResolverContract(unittest.TestCase):
    def source(self, engine='pi', session='session_1'):
        return {'kind': 'native_round', 'engine': engine, 'session_id': session,
                'entry_id': 'conversation:1',
                'source_key': f'{engine}:{session}:conversation:1'}

    def test_routes_only_to_registered_native_session(self):
        connector = FakeConnector()
        resolver = SourceResolver([connector])
        got = resolver.resolve(self.source())
        self.assertEqual(got, connector.result)
        self.assertEqual(connector.seen, [self.source()])
        with self.assertRaises(Conflict):
            resolver.resolve(self.source(session='someone_else'))

    def test_trusted_turn_input_is_forwarded_only_to_codex(self):
        class CodexConnector(FakeConnector):
            def resolve_source(self, source_ref, trusted_public=None):
                self.seen.append((source_ref, trusted_public))
                return self.result
        trusted = [{'type':'message','role':'user','content':[]}]
        codex = CodexConnector(engine='codex')
        resolver = SourceResolver([codex])
        source = self.source(engine='codex')
        resolver.resolve_with_input(source, trusted)
        self.assertEqual(codex.seen, [(source, trusted)])
        pi = FakeConnector()
        SourceResolver([pi]).resolve_with_input(self.source(), trusted)
        self.assertEqual(pi.seen, [self.source()])

    def test_duplicate_or_non_resolving_connector_is_rejected(self):
        with self.assertRaises(Conflict):
            SourceResolver([FakeConnector(), FakeConnector()])
        broken = FakeConnector(); broken.resolve_source = None
        with self.assertRaises(InvalidState):
            SourceResolver([broken])

    def test_connector_result_must_contain_only_native_and_public(self):
        connector = FakeConnector(result={'source': {}, 'public': [], 'extra': True})
        with self.assertRaises(InvalidState):
            SourceResolver([connector]).resolve(self.source())


if __name__ == '__main__':
    unittest.main()
