"""Replay reproduces the committed input layout instead of re-deciding it.

The rule that decides whether the durable TurnInputs record is prepended has
changed across releases. Turns committed under an older rule must still replay
under the committed public_hash; any other drift must still be rejected.
"""
import copy
import tempfile
import unittest
from types import SimpleNamespace

from shepherd.coordinator import digest
from shepherd.projector import ReadProjector
from shepherd.source_codec import UNKNOWN_TOOL_OUTPUT
from shepherd.store import Conflict
from shepherd.turn_inputs import TurnInputs

QUESTION = [{'type':'message','role':'user','content':[{'type':'input_text','text':'q'}]}]
# A pre-2026-09-21 Claude user entry: no origin/shepherd marker, so the current
# rule treats it as "no native user input" and would prepend TurnInputs.
SOURCE = {'codec':'unified-v1:claude-2','events':[
    {'type':'claude_entry','value':{'type':'user','uuid':'u',
     'message':{'role':'user','content':'q'}}}]}
NATIVE = [{'type':'message','role':'user','content':[{'type':'input_text','text':'q'}]},
          {'type':'function_call','call_id':'c1','name':'read','arguments':'{}'},
          {'type':'message','role':'assistant','content':[{'type':'output_text','text':'a'}]}]


class Resolver:
    def resolve(self, _ref):
        return {'source':copy.deepcopy(SOURCE), 'public':copy.deepcopy(NATIVE)}


class InputLayout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ref = TurnInputs(self.tmp.name).write('c', 'c:1', QUESTION)

    def project(self, public_hash, unfinished=()):
        row = {'seq':1,'turn_id':'c:1','turn_hash':'h','kind':'user_turn',
               'outcome':'completed','history_state':'verified',
               'source_ref':{'engine':'claude','session_id':'s'},
               'source_hash':digest(SOURCE),'public_hash':public_hash,
               'input_ref':self.ref,'unfinished_tool_ids':list(unfinished)}
        store = SimpleNamespace(root=self.tmp.name, load_raw=lambda _cid:{'turns':[row]})
        return ReadProjector(store, Resolver()).project('c')[0]['public']

    def test_legacy_native_layout_replays(self):
        self.assertEqual(self.project(digest(NATIVE)), NATIVE)

    def test_current_prefixed_layout_replays(self):
        self.assertEqual(self.project(digest(QUESTION + NATIVE)), QUESTION + NATIVE)

    def test_layout_selection_keeps_unknown_tool_closure(self):
        closed = NATIVE + [{'type':'function_call_output','call_id':'c1',
                            'output':UNKNOWN_TOOL_OUTPUT}]
        self.assertEqual(self.project(digest(closed), unfinished=['c1']), closed)
        self.assertEqual(self.project(digest(QUESTION + closed), unfinished=['c1']),
                         QUESTION + closed)

    def test_any_other_drift_is_still_rejected(self):
        for public in (NATIVE[1:], QUESTION + QUESTION + NATIVE, NATIVE + NATIVE[-1:]):
            with self.assertRaisesRegex(Conflict, '公共投影 Hash 不匹配'):
                self.project(digest(public))


if __name__ == '__main__':
    unittest.main()
