"""Claude auto-update must not strand persisted Shepherd rounds."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron/lib'))

from shepherd import Coordinator, ExecutionHarness, Store
from shepherd.claude_codec import CODEC_VERSION, SDK_VERSION, encode_rounds
from shepherd.claude_connector import ClaudeConnector, managed_claude_binary


class ClaudeVersionDrift(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cwd = self.root / 'work'; self.cwd.mkdir()
        self.sdk = self.root / 'sdk'; self.sdk.mkdir()
        (self.sdk / 'package.json').write_text(json.dumps({'version': SDK_VERSION}))
        (self.sdk / 'sdk.mjs').write_text('export {};')
        self.latest = self.root / 'claude-latest'
        self.latest.write_text('#!/bin/sh\necho "2.1.269 (Claude Code)"\n'); self.latest.chmod(0o755)
        self.store = Store(self.root / 'state')
        self.coordinator = Coordinator(self.store)
        self.cid = 'conversation_version_drift'; self.coordinator.create(self.cid)
        self.native_sid = '11111111-2222-4333-8444-555555555555'
        self.binding_id = self.coordinator.bind(
            self.cid, 'claude', self.native_sid, native_cwd=str(self.cwd),
            connector_version=CODEC_VERSION, binding_id='binding_claude',
            ownership_nonce='nonce_claude')
        self.binding = self.coordinator.state(self.cid)['bindings'][self.binding_id]

    def connector(self, binary=None):
        return ClaudeConnector(self.binding, 'nonce_claude', store_root=self.root / 'state',
            sdk_root=self.sdk, claude_binary=str(binary or self.latest),
            node_binary=shutil.which('node'), ownership=self.coordinator.ownership)

    def test_managed_binary_honors_custom_data_home(self):
        pinned = self.root / 'custom-data' / 'claude' / 'versions' / '2.1.268'
        pinned.parent.mkdir(parents=True)
        pinned.write_text('#!/bin/sh\nexit 0\n'); pinned.chmod(0o755)
        with mock.patch.dict(os.environ, {'DESKTRON_DATA_HOME':str(self.root / 'custom-data')}):
            self.assertEqual(managed_claude_binary(), str(pinned))

    def test_default_connector_uses_current_cli_without_silently_downgrading(self):
        pinned = self.root / 'claude-pinned'
        pinned.write_text('#!/bin/sh\necho "2.1.268 (Claude Code)"\n'); pinned.chmod(0o755)
        with mock.patch('shepherd.claude_connector.managed_claude_binary', return_value=str(pinned)), \
             mock.patch('shepherd.claude_connector.cli_path.which', return_value=str(self.latest)):
            connector = ClaudeConnector(self.binding, 'nonce_claude',
                store_root=self.root / 'state', sdk_root=self.sdk,
                node_binary=shutil.which('node'), ownership=self.coordinator.ownership)
        self.assertEqual(connector.claude_binary, str(self.latest))
        self.assertFalse(connector.availability(self.sdk, connector.claude_binary,
                                                shutil.which('node'))['available'])  # Empty SDK has no capabilities.

    def test_persisted_source_allows_known_sessionless_internal_record(self):
        connector = self.connector()
        public = [
            {'type':'message','role':'user','content':[{'type':'input_text','text':'question'}]},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'answer'}]},
        ]
        round_id = self.cid + ':1'
        rows = encode_rounds(self.binding, [{
            'round_id':round_id, 'offset':1, 'entry_hash':'1' * 64,
            'committed_at':10, 'public':public}])
        rows.insert(1, {'type':'file-history-snapshot', 'messageId':rows[0]['uuid'],
                        'snapshot':{}, 'isSnapshotUpdate':False})
        connector.native.append(rows, '')
        connector.receipts.write(self.binding, round_id, {
            'start_seq':0, 'end_seq':len(rows), 'terminal_uuid':rows[-1]['uuid'],
            'terminal_state':'completed', 'native_revision':connector.native.revision()})
        ref = {'kind':'native_round','engine':'claude','session_id':self.native_sid,
               'entry_id':round_id,
               'source_key':f'claude:{self.native_sid}:{round_id}'}
        self.assertEqual(connector.resolve_source(ref)['public'], public)

    def test_persisted_source_rejects_receipt_revision_mismatch(self):
        connector = self.connector()
        round_id = self.cid + ':1'
        rows = encode_rounds(self.binding, [{
            'round_id':round_id, 'offset':1, 'entry_hash':'1' * 64,
            'committed_at':10, 'public':[
                {'type':'message','role':'user','content':[{'type':'input_text','text':'question'}]},
                {'type':'message','role':'assistant','content':[{'type':'output_text','text':'answer'}]},
            ]}])
        connector.native.append(rows, '')
        connector.receipts.write(self.binding, round_id, {
            'start_seq':0, 'end_seq':len(rows), 'terminal_uuid':rows[-1]['uuid'],
            'terminal_state':'completed', 'native_revision':'0' * 64})
        ref = {'kind':'native_round','engine':'claude','session_id':self.native_sid,
               'entry_id':round_id,
               'source_key':f'claude:{self.native_sid}:{round_id}'}
        with self.assertRaisesRegex(Exception, 'Revision 不匹配'):
            connector.resolve_source(ref)

    def test_persisted_source_rejects_unknown_internal_record(self):
        connector = self.connector()
        round_id = self.cid + ':1'
        rows = encode_rounds(self.binding, [{
            'round_id':round_id, 'offset':1, 'entry_hash':'1' * 64,
            'committed_at':10, 'public':[
                {'type':'message','role':'user','content':[{'type':'input_text','text':'question'}]},
                {'type':'message','role':'assistant','content':[{'type':'output_text','text':'answer'}]},
            ]}])
        rows.insert(1, {'type':'future-semantic-record', 'payload':'must not be omitted'})
        connector.native.append(rows, '')
        connector.receipts.write(self.binding, round_id, {
            'start_seq':0, 'end_seq':len(rows), 'terminal_uuid':rows[-1]['uuid'],
            'terminal_state':'completed', 'native_revision':connector.native.revision()})
        ref = {'kind':'native_round','engine':'claude','session_id':self.native_sid,
               'entry_id':round_id,
               'source_key':f'claude:{self.native_sid}:{round_id}'}
        with self.assertRaisesRegex(Exception, '未知内部条目'):
            connector.resolve_source(ref)

    def test_resident_stop_hook_summary_uses_record_kind_not_writer_version(self):
        connector=self.connector()
        row={'type':'system','subtype':'stop_hook_summary','version':'2.1.268',
             'sessionId':self.native_sid,'uuid':'stop-summary','parentUuid':'answer',
             'cwd':str(self.cwd),'hookCount':1,'hookErrors':[]}
        connector._validate_source_entries([row],production=True)
        connector._validate_source_entries([{**row,'version':'9.9.9'}],production=True)
        with self.assertRaisesRegex(Exception,"未知内部条目: type='system' subtype='future-semantic'"):
            connector._validate_source_entries([{**row,'subtype':'future-semantic'}],production=True)

    def test_resident_api_error_is_reviewed_internal_record(self):
        connector=self.connector()
        row={'type':'system','subtype':'api_error','version':'2.1.268',
             'sessionId':self.native_sid,'uuid':'api-error','parentUuid':'answer',
             'cwd':str(self.cwd),'level':'error','source':'api','retryAttempt':1,
             'maxRetries':5,'retryInMs':1000,'error':{'type':'overloaded_error'}}
        connector._validate_source_entries([row],production=True)

    def test_persisted_source_read_ignores_newer_global_cli(self):
        connector = self.connector()
        public = [
            {'type':'message','role':'user','content':[{'type':'input_text','text':'question'}]},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'answer'}]},
        ]
        round_id = self.cid + ':1'
        rows = encode_rounds(self.binding, [{
            'round_id':round_id, 'offset':1, 'entry_hash':'1' * 64,
            'committed_at':10, 'public':public}])
        connector.native.append(rows, '')
        connector.receipts.write(self.binding, round_id, {
            'start_seq':0, 'end_seq':len(rows), 'terminal_uuid':rows[-1]['uuid'],
            'terminal_state':'completed', 'native_revision':connector.native.revision()})
        ref = {'kind':'native_round','engine':'claude','session_id':self.native_sid,
               'entry_id':round_id,
               'source_key':f'claude:{self.native_sid}:{round_id}'}
        self.assertEqual(ClaudeConnector.availability(
            self.sdk, str(self.latest), shutil.which('node'))['reason'], 'claude_protocol_incompatible')
        self.assertEqual(connector.resolve_source(ref)['public'], public)

    def test_source_persisted_recovery_commits_without_runner_rerun(self):
        connector = self.connector()
        public = [
            {'type':'message','role':'user','content':[{'type':'input_text','text':'question'}]},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'answer'}]},
        ]
        lease = self.coordinator.acquire(self.cid, self.binding_id,
                                          ownership_nonce='nonce_claude')
        inflight = self.coordinator.begin_round(
            self.cid, lease['lease_id'], lease['generation'], attempt_id='attempt_drift')
        rows = encode_rounds(self.binding, [{
            'round_id':inflight['round_id'], 'offset':inflight['offset'],
            'entry_hash':'1' * 64, 'committed_at':10, 'public':public}])
        connector.native.append(rows, '')
        connector.receipts.write(self.binding, inflight['round_id'], {
            'start_seq':0, 'end_seq':len(rows), 'terminal_uuid':rows[-1]['uuid'],
            'terminal_state':'completed', 'native_revision':connector.native.revision()})
        ref = {'kind':'native_round','engine':'claude','session_id':self.native_sid,
               'entry_id':inflight['round_id'],
               'source_key':f'claude:{self.native_sid}:{inflight["round_id"]}'}
        self.coordinator.source_persisted(
            self.cid, lease['lease_id'], lease['generation'], attempt_id='attempt_drift',
            source=ref, native_revision=connector.native.revision(), terminal_state='completed')
        runner = mock.Mock()
        row = ExecutionHarness(self.coordinator, connector, runner).recover(
            self.cid, lease_id=lease['lease_id'], generation=lease['generation'])
        runner.run.assert_not_called()
        self.assertEqual(row['offset'], 1)
        self.assertIsNone(self.coordinator.state(self.cid)['inflight'])


if __name__ == '__main__': unittest.main()
