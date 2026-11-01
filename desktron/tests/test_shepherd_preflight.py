"""Encoding failures must be discovered before giving up the current producer."""
import unittest
from unittest import mock

from test_shepherd import ProjectionAndSwitch
from shepherd import Conflict
from shepherd.codex_connector import CodexFamilyConnector
from shepherd.pi_connector import PiConnector
from shepherd.claude_connector import ClaudeConnector


class Preflight(ProjectionAndSwitch):

    def test_encoding_failure_keeps_original_lease_and_does_not_append(self):
        runner, connector = self.runner()
        before = self.coordinator.state(self.cid)
        with mock.patch.object(connector, 'preflight', side_effect=ValueError('工具参数不是 JSON')):
            with self.assertRaisesRegex(Conflict, '历史格式不兼容'):
                runner.switch(self.cid, self.target, ownership_nonce='other_nonce',
                    lease_id=self.old_lease['lease_id'], generation=self.old_lease['generation'])
        self.assertEqual(self.coordinator.state(self.cid), before)
        self.assertEqual(connector.calls, [])
        self.assertEqual(connector.checkpoint_calls, [])

    def test_missing_preflight_contract_fails_before_fence(self):
        runner, connector = self.runner()
        connector.preflight = None
        with self.assertRaisesRegex(Conflict, '缺少历史兼容预检'):
            runner.switch(self.cid, self.target, ownership_nonce='other_nonce',
                lease_id=self.old_lease['lease_id'], generation=self.old_lease['generation'])
        self.assertEqual(self.coordinator.state(self.cid)['lease'], self.old_lease)

    def test_all_native_connectors_preflight_without_opening_engine(self):
        bad = {'offset': 1, 'round_id': 'round_1', 'entry_hash': 'a' * 64,
               'public': [{'type': 'function_call', 'call_id': 'c', 'name': 'read',
                           'arguments': 'not JSON'}]}
        for cls in (PiConnector, ClaudeConnector, CodexFamilyConnector):
            connector = object.__new__(cls)
            connector.model_image_limit = False
            connector.binding = {'native_session_id': 'native', 'native_cwd': '/unused', 'binding_id': 'binding'}
            with self.subTest(cls=cls.__name__), self.assertRaises(ValueError):
                connector.preflight([bad])

    def test_new_selection_during_encoding_cancels_before_fence(self):
        runner, connector = self.runner()
        cancelled = [False]
        connector.preflight = lambda *a, **kw: cancelled.__setitem__(0, True)
        with self.assertRaisesRegex(Conflict, '更新的选择取消'):
            runner.switch(self.cid, self.target, ownership_nonce='other_nonce',
                lease_id=self.old_lease['lease_id'], generation=self.old_lease['generation'],
                cancelled=lambda: cancelled[0])
        self.assertEqual(self.coordinator.state(self.cid)['lease'], self.old_lease)
        self.assertEqual(connector.calls, [])

    def test_wrong_tool_result_type_rejected_by_every_connector(self):
        row = {'offset': 1, 'round_id': 'round_1', 'entry_hash': 'a' * 64, 'public': [
            {'type':'function_call','call_id':'c','name':'read','arguments':'{}'},
            {'type':'custom_tool_call_output','call_id':'c','output':'result'}]}
        for cls in (PiConnector, ClaudeConnector, CodexFamilyConnector):
            connector = object.__new__(cls)
            connector.model_image_limit = False
            connector.binding = {'native_session_id':'native','native_cwd':'/unused','binding_id':'binding'}
            with self.subTest(cls=cls.__name__), self.assertRaises(ValueError):
                connector.preflight([row])
