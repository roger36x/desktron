"""Source receipts contain locators, not transcript bodies."""
from pathlib import Path
import json
import os
from _platform import assert_private_file
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron/lib'))

from shepherd import Conflict, InvalidState, SourceReceipts


class SourceReceiptContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.receipts = SourceReceipts(self.tmp.name)
        self.binding = {'binding_id':'binding_1','conversation_id':'conversation_1',
            'engine':'claude','native_session_id':'11111111-2222-4333-8444-555555555555',
            'connector_version':'claude-v1','ownership_nonce':'nonce_1'}

    def test_round_trip_is_private_and_idempotent(self):
        locator = {'start_seq': 3, 'end_seq': 8, 'terminal_uuid': 'u_8'}
        first = self.receipts.write(self.binding, 'turn_1', locator)
        self.assertEqual(self.receipts.write(self.binding, 'turn_1', locator), first)
        self.assertEqual(self.receipts.read(self.binding, 'turn_1')['locator'], locator)
        assert_private_file(self.receipts.path('binding_1', 'turn_1'))

    def test_body_fields_and_conflicting_rewrite_are_rejected(self):
        with self.assertRaises(InvalidState):
            self.receipts.write(self.binding, 'turn_1', {'content':'secret'})
        with self.assertRaises(InvalidState):
            self.receipts.write(self.binding, 'turn_1', {'turn_id':{'content':'secret'}})
        with self.assertRaises(InvalidState):
            self.receipts.write(self.binding, 'turn_1', {'arbitrary':'transcript body'})
        self.receipts.write(self.binding, 'turn_1', {'start_seq':1,'end_seq':2})
        with self.assertRaises(Conflict):
            self.receipts.write(self.binding, 'turn_1', {'start_seq':1,'end_seq':3})

    def test_missing_receipt_can_be_sealed_without_nested_lock(self):
        sealed = self.receipts.seal(self.binding, 'turn_new', {
            'start_seq':1,'end_seq':2,'terminal_state':'unknown',
            'unfinished_tool_ids':['call_1'],'native_revision':'a'*64})
        self.assertEqual(sealed, self.receipts.read(self.binding, 'turn_new'))

    def test_boundary_receipt_can_be_monotonically_sealed_unknown(self):
        self.receipts.write(self.binding, 'turn_1', {'start_seq':1,'end_seq':2})
        sealed = self.receipts.seal(self.binding, 'turn_1', {
            'start_seq':1,'end_seq':2,'terminal_state':'unknown',
            'unfinished_tool_ids':['call_1'],'native_revision':'a'*64})
        self.assertEqual(sealed['locator']['terminal_state'], 'unknown')
        self.assertEqual(self.receipts.seal(self.binding, 'turn_1', sealed['locator']), sealed)
        with self.assertRaises(Conflict):
            self.receipts.seal(self.binding, 'turn_1', {
                'start_seq':1,'end_seq':3,'terminal_state':'unknown'})

    def test_existing_terminal_receipt_cannot_be_extended(self):
        self.receipts.write(self.binding, 'turn_1', {
            'start_seq':1,'end_seq':2,'terminal_state':'completed'})
        with self.assertRaises(Conflict):
            self.receipts.seal(self.binding, 'turn_1', {
                'start_seq':1,'end_seq':2,'terminal_state':'completed',
                'native_revision':'a'*64})

    def test_tamper_or_wrong_binding_is_rejected(self):
        self.receipts.write(self.binding, 'turn_1', {'start_seq':1,'end_seq':2})
        path = Path(self.receipts.path('binding_1', 'turn_1'))
        value = json.loads(path.read_text()); value['native_session_id'] = 'other'
        path.write_text(json.dumps(value)); path.chmod(0o600)
        with self.assertRaises(Conflict): self.receipts.read(self.binding, 'turn_1')


if __name__ == '__main__': unittest.main()
