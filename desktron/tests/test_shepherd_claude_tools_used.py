import os
import shutil
import unittest

import test_shepherd_claude
from shepherd import Conflict
from shepherd.store import SourceShapeUnsupported
from shepherd.claude_connector import ClaudeConnector
from shepherd.source_codec import claude_public
from shepherd.claude_codec import encode_rounds


class ToolsUsedSourceEntry(test_shepherd_claude.ClaudeFixture):
    fake_runtime = test_shepherd_claude.ClaudeConnectorContract.fake_runtime

    def connector(self):
        sdk, claude = self.fake_runtime()
        return ClaudeConnector(self.binding, 'nonce_claude',
            store_root=self.root / 'state', sdk_root=sdk,
            claude_binary=str(claude), node_binary=shutil.which('node'),
            ownership=self.coordinator.ownership)

    def test_tools_used_record_is_internal_only(self):
        connector = self.connector()
        entries = encode_rounds(self.binding, [self.round([
            {'type':'message','role':'user','content':[{'type':'input_text','text':'q'}]},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'a'}]},
        ])])
        record = {'type':'tools-used', 'tools':['Bash', 'Read'], 'sessionId':self.sid}
        entries.insert(1, record)
        connector._validate_source_entries(entries, production=True)
        self.assertEqual([item['role'] for item in claude_public(entries)
                          if item['type'] == 'message'], ['user', 'assistant'])
        self.assertIn({'engine':'claude', 'action':'ignored_internal_entry',
                       'kind':'tools-used', 'count':1},
                      connector.source_compatibility({'entries':entries}))

    def test_malformed_tools_used_record_fails_closed(self):
        connector = self.connector()
        base = {'type':'tools-used', 'sessionId':self.sid}
        for changes in ({'tools':'Bash'}, {'tools':[1]}, {'tools':['Bash'], 'message':'x'}):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(Conflict, '元数据形状无效'):
                    connector._validate_source_entries([{**base, **changes}], production=True)
        with self.assertRaisesRegex(Conflict, '身份污染'):
            connector._validate_source_entries(
                [{**base, 'tools':['Bash'], 'sessionId':'other'}], production=True)


class UnreviewedMetadataEntry(ToolsUsedSourceEntry):
    def test_unknown_metadata_record_is_tolerated_and_reported(self):
        connector = self.connector()
        entries = encode_rounds(self.binding, [self.round([
            {'type':'message','role':'user','content':[{'type':'input_text','text':'q'}]},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'a'}]},
        ])])
        entries.insert(1, {'type':'future-stats', 'sessionId':self.sid,
                           'names':['Bash'], 'count':2, 'enabled':True})
        connector._validate_source_entries(entries, production=True)
        self.assertEqual([item['role'] for item in claude_public(entries)
                          if item['type'] == 'message'], ['user', 'assistant'])
        self.assertIn({'engine':'claude', 'action':'tolerated_unreviewed_entry',
                       'kind':'unreviewed/future-stats', 'count':1},
                      connector.source_compatibility({'entries':entries}))

    def test_unknown_record_with_content_or_chain_stays_isolated(self):
        connector = self.connector()
        base = {'type':'future-stats', 'sessionId':self.sid}
        for changes in ({'message':'x'}, {'content':'x'}, {'uuid':'u'}, {'parentUuid':'p'},
                        {'detail':{'nested':'x'}}, {'note':'x' * 257},
                        {'names':[{'a':1}]}, {'sessionId':'other'}, {'type':'system'},
                        {'type':'Future Stats'}):
            with self.subTest(changes=changes):
                with self.assertRaises(SourceShapeUnsupported) as caught:
                    connector._validate_source_entries([{**base, **changes}], production=True)
                self.assertIn('未知内部条目', str(caught.exception))
        missing = {'type':'future-stats', 'names':['Bash']}
        with self.assertRaises(SourceShapeUnsupported):
            connector._validate_source_entries([missing], production=True)


if __name__ == '__main__':
    unittest.main()
