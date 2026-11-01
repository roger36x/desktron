"""Claude connector unit and optional real-SDK protocol tests."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
from _platform import database, assert_private_file, symlink, version_cli
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron' / 'lib'))

import cli_path
from shepherd import (ClaudeRunner, Conflict, Coordinator, ExecutionHarness,
                      ReadProjector, Store, SwitchRunner)
from shepherd.claude_codec import (CLAUDE_CODE_VERSION, CODEC_VERSION, SDK_VERSION,
                                   PROMPT_SNAPSHOT_POLICY, CodecError,
                                   encode_checkpoint, encode_rounds, static_system_prompt)
from shepherd.claude_connector import ClaudeConnector
from shepherd.coordinator import digest
from shepherd.claude_store import ClaudeNativeStore, canonical
from shepherd import claude_sdk
from shepherd.capabilities import matrix
from shepherd.pi_connector import pi_sdk_root
from shepherd.source_codec import claude_public
from shepherd.store import SourceShapeUnsupported, SourceVerificationError


class ClaudeFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cwd = self.root / 'work'; self.cwd.mkdir()
        self.state = Store(self.root / 'state')
        self.coordinator = Coordinator(self.state)
        self.cid = 'conversation_claude'
        self.coordinator.create(self.cid)
        self.sid = '11111111-2222-4333-8444-555555555555'
        self.binding_id = self.coordinator.bind(
            self.cid, 'claude', self.sid, native_cwd=str(self.cwd),
            connector_version=CODEC_VERSION, binding_id='binding_claude',
            ownership_nonce='nonce_claude')
        self.binding = self.coordinator.state(self.cid)['bindings'][self.binding_id]

    def round(self, public, offset=1):
        return {'round_id': f'{self.cid}:{offset}', 'offset': offset,
                'entry_hash': str(offset) * 64, 'committed_at': 10,
                'public': public}


class ClaudeCodec(ClaudeFixture):
    def test_plain_and_tool_rounds_are_deterministic_and_paired(self):
        public = [
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'question'}]},
            {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'checking'}]},
            {'type': 'function_call', 'call_id': 'call_1', 'name': 'read.file',
             'arguments': json.dumps({'path': '/tmp/x'})},
            {'type': 'function_call_output', 'call_id': 'call_1', 'output': 'result'},
            {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'done'}]},
        ]
        first = encode_rounds(self.binding, [self.round(public)])
        second = encode_rounds(self.binding, [self.round(public)])
        self.assertEqual(first, second)
        self.assertEqual([row['type'] for row in first], ['user', 'assistant', 'user', 'assistant'])
        call = first[1]['message']['content'][-1]
        result = first[2]['message']['content'][0]
        self.assertEqual(call['type'], 'tool_use')
        self.assertEqual(result['tool_use_id'], call['id'])
        self.assertTrue(all(row['sessionId'] == self.sid for row in first))
        self.assertTrue(all(row['cwd'] == os.path.realpath(self.cwd) for row in first))

    def test_provider_specific_call_id_is_aliased_and_recoverable(self):
        original=('call_3D7bUZ7eLFrQFWgz0UEJ4k4r|'
                  'fc_0af2bfaeb884fe00016aaa93b4c9b087d286275f76ef625fe1')
        rows=encode_rounds(self.binding,[self.round([
            {'type':'function_call','call_id':original,'name':'read','arguments':'{}'},
            {'type':'function_call_output','call_id':original,'output':'ok'},
        ])])
        call=rows[0]['message']['content'][0]
        result=rows[1]['message']['content'][0]
        self.assertEqual(call['id'],result['tool_use_id'])
        self.assertRegex(call['id'],r'^toolu_[0-9a-f]{24}$')
        self.assertEqual(rows[0]['shepherd']['call_ids'][call['id']],original)

    def test_call_id_alias_collision_fails_closed(self):
        public=[
            {'type':'function_call','call_id':'first','name':'read','arguments':'{}'},
            {'type':'function_call_output','call_id':'first','output':'one'},
            {'type':'function_call','call_id':'second','name':'read','arguments':'{}'},
            {'type':'function_call_output','call_id':'second','output':'two'},
        ]
        with mock.patch('shepherd.claude_codec._tool_id',return_value='toolu_same'), \
             self.assertRaisesRegex(CodecError,'归一化冲突'):
            encode_rounds(self.binding,[self.round(public)])

    def test_incomplete_tool_or_system_message_fails_closed(self):
        with self.assertRaises(CodecError):
            encode_rounds(self.binding, [self.round([{
                'type': 'function_call', 'call_id': 'call_1', 'name': 'bash', 'arguments': '{}'}])])
        with self.assertRaises(CodecError):
            encode_rounds(self.binding, [self.round([{
                'type': 'message', 'role': 'system', 'content': [{'type': 'text', 'text': 'x'}]}])])

    def test_checkpoint_uses_native_compaction_shape_not_plain_user_turn(self):
        checkpoint = {'checkpoint_id': 'checkpoint_1', 'covers_through_offset': 8,
                      'estimated_tokens': 100, 'created_at': 10}
        rows = encode_checkpoint(self.binding, checkpoint, {'summary': 'portable summary'})
        self.assertEqual(rows[0]['subtype'], 'compact_boundary')
        self.assertTrue(rows[1]['isCompactSummary'])
        self.assertTrue(rows[1]['isVisibleInTranscriptOnly'])
        self.assertIn('portable summary', rows[1]['message']['content'])
        self.assertEqual(rows, encode_checkpoint(self.binding, checkpoint, {'summary': 'portable summary'}))


class ClaudeStore(ClaudeFixture):
    def native(self):
        return ClaudeNativeStore(self.root / 'state' / 'native' / 'claude', self.binding,
                                 'nonce_claude', ownership=self.coordinator.ownership)

    def test_append_is_revisioned_idempotent_and_private(self):
        native = self.native(); entries = encode_rounds(self.binding, [self.round([
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'one'}]},
            {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'two'}]},
        ])])
        revision = native.append(entries, '')
        self.assertEqual(native.append(entries, ''), revision)  # retry after write, before offset commit
        self.assertEqual(native.load(), entries)
        assert_private_file(native.path)
        changed = json.loads(json.dumps(entries)); changed[0]['message']['content'][0]['text'] = 'changed'
        with self.assertRaises(Conflict): native.append(changed, revision)

    def test_prefix_revisions_hash_exact_stored_payload_bytes(self):
        native=self.native();native.initialize()
        payload='{"type":"custom","cost":1.0,"2":"two","10":"ten"}'
        with database(native.path) as db:
            db.execute('INSERT INTO entries(uuid,payload) VALUES(NULL,?)',(payload,))
            expected=hashlib.sha256(('\0'+payload).encode()).hexdigest()
            db.execute("UPDATE meta SET value=? WHERE key='revision'",(expected,))
        self.assertEqual(native.prefix_revisions({0,1}),{0:'',1:expected})
        self.assertNotEqual(expected,hashlib.sha256(
            ('\0'+canonical(json.loads(payload))).encode()).hexdigest())

    def test_missing_ownership_and_symlink_store_fail_closed(self):
        native = self.native(); Path(self.coordinator.ownership.path(self.binding_id)).unlink()
        with self.assertRaises(Conflict): native.initialize()
        self.coordinator.ownership.issue(self.binding)
        target = self.root / 'outside'; target.write_text('not sqlite', encoding='utf-8')
        native.path = str(self.root / 'link.sqlite3'); symlink(target, native.path)
        with self.assertRaises(Exception): native.initialize()

    def test_store_root_cannot_escape_shepherd_private_layout(self):
        with self.assertRaises(Conflict):
            ClaudeNativeStore(self.root / 'arbitrary', self.binding, 'nonce_claude',
                              ownership=self.coordinator.ownership)

    def test_concurrent_same_revision_accepts_one_distinct_batch(self):
        native = self.native(); results, errors = [], []
        def append(index):
            entries = encode_rounds(self.binding, [self.round([
                {'type': 'message', 'role': 'user', 'content': [
                    {'type': 'input_text', 'text': str(index)}]}], offset=index)])
            try: results.append(native.append(entries, ''))
            except Exception as exc: errors.append(exc)
        workers = [threading.Thread(target=append, args=(index,)) for index in (1, 2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], Conflict)
        self.assertEqual(len(native.load()), 1)


class ClaudeSdkDependency(unittest.TestCase):
    def test_capability_matrix_keeps_unverified_connectors_disabled(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); sdk = root / 'sdk'; sdk.mkdir()
            (sdk / 'package.json').write_text(json.dumps({'version': SDK_VERSION}), encoding='utf-8')
            (sdk / 'sdk.mjs').write_text('export {};', encoding='utf-8')
            claude = version_cli(root / 'claude', '2.1.268')
            got = matrix(claude_paths={'sdk_root': sdk, 'claude_binary': str(claude),
                                       'node_binary': shutil.which('node')})
        self.assertFalse(got['claude']['enabled'])  # A known label cannot supply missing SDK methods.
        self.assertEqual(got['pi']['enabled'], bool(pi_sdk_root()))
        self.assertEqual(got['codex']['enabled'], bool(shutil.which('codex')))
        self.assertTrue(got['codex']['portable_checkpoint'])

    def test_cli_status_does_not_install_sdk(self):
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(claude_sdk, 'home', return_value=folder), \
             mock.patch.object(claude_sdk.subprocess, 'run') as run:
            from shepherd import cli
            with mock.patch.object(cli, 'matrix', return_value={'claude': {'enabled': False}}), \
                 mock.patch('builtins.print') as output:
                self.assertEqual(cli.main(['status']), 0)
            run.assert_not_called(); output.assert_called_once()

    def test_lockfile_pins_exact_sdk_and_only_optional_platform_binaries(self):
        lock = json.loads(claude_sdk.lockfile().read_text(encoding='utf-8'))
        package = lock['packages']['node_modules/@anthropic-ai/claude-agent-sdk']
        self.assertEqual(package['version'], SDK_VERSION)
        required = [name for name, value in lock['packages'].items()
                    if name and not value.get('optional')]
        self.assertEqual(required, ['node_modules/@anthropic-ai/claude-agent-sdk'])

    def test_pinned_minimal_install_and_idempotent_reuse(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                package = root / 'node_modules/@anthropic-ai/claude-agent-sdk'
                package.mkdir(parents=True)
                (package / 'package.json').write_text(json.dumps({'version': SDK_VERSION}), encoding='utf-8')
                (package / 'sdk.mjs').write_text('export {};', encoding='utf-8')
                return mock.Mock(returncode=0, stdout='', stderr='')
            with mock.patch.object(claude_sdk.subprocess, 'run', side_effect=run):
                self.assertTrue(claude_sdk.ensure(root, npm='/tmp/npm')[0])
                self.assertTrue(claude_sdk.ensure(root, npm='/tmp/npm')[0])
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1], 'ci')
            self.assertIn('--omit=optional', calls[0])
            self.assertIn('--legacy-peer-deps', calls[0])
            self.assertEqual(json.loads((root / 'package.json').read_text(encoding='utf-8'))['dependencies'],
                             {'@anthropic-ai/claude-agent-sdk': SDK_VERSION})
            self.assertEqual(json.loads((root / 'package-lock.json').read_text(encoding='utf-8'))['lockfileVersion'], 3)

    def test_shepherd_cli_is_read_only_until_explicit_prepare(self):
        from shepherd import cli
        with mock.patch.object(cli, 'matrix', return_value={'claude': {'enabled': False}}), \
             mock.patch.object(claude_sdk, 'ensure') as ensure, \
             mock.patch('builtins.print'):
            self.assertEqual(cli.main(['status']), 0)
            ensure.assert_not_called()
            ensure.return_value = (True, '/managed/sdk')
            self.assertEqual(cli.main(['prepare-claude']), 0)
            ensure.assert_called_once_with()


class ClaudeConnectorContract(ClaudeFixture):
    def policy_binding(self, prompt):
        with self.state.change(self.cid) as state:
            binding = state['bindings'][self.binding_id]
            binding['prompt_snapshot_policy'] = PROMPT_SNAPSHOT_POLICY
            binding['prompt_snapshot_sha256'] = hashlib.sha256(prompt.encode()).hexdigest()
        self.binding = self.coordinator.state(self.cid)['bindings'][self.binding_id]
        Path(self.coordinator.ownership.path(self.binding_id)).unlink()
        self.coordinator.ownership.issue(self.binding)

    def test_import_links_last_uuid_past_native_metadata(self):
        sdk, binary = self.fake_runtime()
        connector = ClaudeConnector(self.binding, 'nonce_claude',
            store_root=self.root/'state', sdk_root=str(sdk), claude_binary=str(binary),
            ownership=self.coordinator.ownership)
        public = [{'type':'message','role':'user','content':[{'type':'input_text','text':'fixture'}]}]
        old = encode_rounds(self.binding, [self.round(public)])
        revision = connector.native.append(old + [{'type':'last-prompt','lastPrompt':'fixture'},
                                                   {'type':'mode','mode':'default'}], '')
        row = self.round(public, offset=2)
        result = connector.append([row], revision)
        imported = [e for e in connector.native.load()
                    if (e.get('shepherd') or {}).get('round_id') == row['round_id']]
        self.assertEqual(imported[0]['parentUuid'], old[-1]['uuid'])
        self.assertEqual(connector.append([row], result['native_revision']), result)

    def test_custom_reasoning_append_reopens_with_real_sdk_without_execution(self):
        sdk = os.environ.get('DESK_SHEPHERD_CLAUDE_SDK_ROOT') or claude_sdk.sdk_root()
        if not (Path(sdk)/'sdk.mjs').is_file(): self.skipTest('pinned SDK not installed')
        cli = os.environ.get('DESK_SHEPHERD_CLAUDE_BINARY') or shutil.which('claude')
        if not cli: self.skipTest('Claude CLI unavailable')
        connector = ClaudeConnector(self.binding, 'nonce_claude',
            store_root=self.root/'state', sdk_root=sdk,
            claude_binary=cli, node_binary=shutil.which('node'),
            ownership=self.coordinator.ownership)
        public=[{'type':'reasoning','summary':[{'type':'summary_text','text':'inspect first'}]},
                {'type':'custom_tool_call','call_id':'c','name':'functions.exec','input':'text(1);'},
                {'type':'custom_tool_call_output','call_id':'c','output':'[工具执行失败] denied'}]
        row=self.round(public)
        result=connector.append([row], '')
        self.assertEqual(connector.append([row], result['native_revision'])['native_revision'], result['native_revision'])
        ref={'kind':'native_round','engine':'claude','session_id':self.sid,'entry_id':row['round_id'],
             'source_key':f'claude:{self.sid}:{row["round_id"]}'}
        self.assertEqual(connector.resolve_source(ref)['public'], public)

    def fake_runtime(self, sdk_version=SDK_VERSION, claude_version=CLAUDE_CODE_VERSION):
        sdk = self.root / 'sdk'; sdk.mkdir(exist_ok=True)
        (sdk / 'package.json').write_text(json.dumps({'version': sdk_version}), encoding='utf-8')
        (sdk / 'sdk.mjs').write_text('''
export const getSessionMessages=async(id, options)=>options.sessionStore.load({sessionId:id});
export const query=()=>({initializationResult:async()=>({commands:[],models:[]}),
  interrupt:async()=>{},close:()=>{}});
''', encoding='utf-8')
        claude = version_cli(self.root / 'claude', claude_version)
        return sdk, claude

    def bridge_fixture(self):
        sdk = self.root / 'bridge-sdk'; sdk.mkdir(exist_ok=True)
        consumed = self.root / 'user-frame-consumed'
        (sdk / 'sdk.mjs').write_text('''
export function query({prompt,options}) {
  const sources=JSON.stringify(options.settingSources);
  if(typeof prompt==='string' && sources!=='[]')throw new Error('bootstrap settings must stay isolated');
  if(typeof prompt!=='string') {
    if(sources!=='["project"]')throw new Error('main turn must load project settings');
    if(options.strictMcpConfig!==true)throw new Error('project MCP configuration must stay disabled');
    if(options.sandbox!==undefined || options.toolAliases!==undefined)throw new Error('bash sandbox still enabled');
    if(options.mcpServers?.desktron_bash)throw new Error('bash MCP still enabled');
    if(options.disallowedTools?.includes('Bash'))throw new Error('native Bash still disabled');
  }
  return {
    initializationResult: async()=>({commands:[],models:[]}),
    request: async()=>({subtype:'success',response:{version:'2.1.283'}}),
    interrupt: async()=>{}, return: async()=>{},
    async *[Symbol.asyncIterator]() {
      for await (const _frame of prompt) {
        await import('node:fs/promises').then(fs=>fs.writeFile(%s,'consumed'));
      }
    },
  };
}
''' % json.dumps(str(consumed)), encoding='utf-8')
        binary = version_cli(self.root / 'bridge-claude', '2.1.283')
        snapshot = {'type':'attachment','uuid':'11111111-2222-4333-8444-555555555555',
                    'attachment':{'type':'prompt_snapshot','systemPrompt':['fixture system']}}
        payload = {'unified':True,'entries':[snapshot],'sessionId':self.sid,
                   'sdkRoot':str(sdk),'claudeBinary':str(binary),
                   'dbPath':str(self.root/'bridge-memory'),'cwd':str(self.cwd),
                   'systemPrompt':'fixture system','prompt':'must remain fenced',
                   'bashCwdPolicy':{'codec':'claude-2.1.268-v1','mode':'reset_each_call'},
                   'protectedPaths':[str(self.root/'protected')]}
        return payload, consumed

    def test_resident_rejects_start_before_prepared(self):
        payload, consumed = self.bridge_fixture()
        payload['bridgeProtocolVersion'] = 4
        path = self.root / 'resident-bootstrap.json'; path.write_text(json.dumps(payload), encoding='utf-8')
        bridge = ROOT / 'desktron/lib/shepherd/claude_resident_runner.mjs'
        result = subprocess.run(['node',str(bridge),str(path)],input='{"type":"start"}\n',
                                capture_output=True,text=True,timeout=5, encoding='utf-8')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('start received before prepared',result.stderr)
        self.assertFalse(consumed.exists())

    def test_oneshot_stdin_disconnect_rejects_dispatch_fence_without_user_frame(self):
        payload, consumed = self.bridge_fixture()
        payload['bridgeProtocolVersion'] = 3
        path = self.root / 'oneshot-bootstrap.json'; path.write_text(json.dumps(payload), encoding='utf-8')
        bridge = ROOT / 'desktron/lib/shepherd/claude_runner.mjs'
        process = subprocess.Popen(['node',str(bridge),str(path)],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True, encoding='utf-8')
        try:
            prepared = json.loads(process.stdout.readline())
            self.assertEqual(prepared['type'],'prepared')
            process.stdin.close()
            process.wait(timeout=5)
            stderr = process.stderr.read()
        finally:
            if process.poll() is None: process.kill(); process.wait(timeout=5)
        self.assertNotEqual(process.returncode,0)
        self.assertIn('host disconnected before dispatch',stderr)
        self.assertFalse(consumed.exists())

    def test_availability_uses_capabilities_not_release_labels(self):
        sdk, claude = self.fake_runtime()
        self.assertTrue(ClaudeConnector.availability(sdk, str(claude), shutil.which('node'))['available'])
        (sdk / 'package.json').write_text(json.dumps({'version': '0.3.269'}), encoding='utf-8')
        self.assertTrue(ClaudeConnector.availability(sdk, str(claude), shutil.which('node'))['available'])
        (sdk / 'sdk.mjs').write_text('export {};', encoding='utf-8')
        self.assertEqual(ClaudeConnector.availability(sdk, str(claude), shutil.which('node'))['reason'],
                         'claude_protocol_incompatible')
        self.assertEqual(ClaudeConnector.availability(sdk, '/missing/claude', shutil.which('node'))['reason'],
                         'claude_missing')
        self.assertEqual(ClaudeConnector.availability(sdk, str(claude), '/missing/node')['reason'],
                         'claude_node_missing')

    def test_resolve_source_round_trips_private_native_entries(self):
        sdk, claude = self.fake_runtime()
        connector = ClaudeConnector(self.binding, 'nonce_claude',
            store_root=self.root / 'state', sdk_root=sdk,
            claude_binary=str(claude), node_binary=shutil.which('node'),
            ownership=self.coordinator.ownership)
        public = [
            {'type':'message','role':'user','content':[{'type':'input_text','text':'question'}]},
            {'type':'function_call','call_id':'call_1','name':'read','arguments':'{"path":"/tmp/x"}'},
            {'type':'function_call_output','call_id':'call_1','output':'result'},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'answer'}]},
        ]
        row = self.round(public)
        connector.native.append(encode_rounds(self.binding, [row]), '')
        ref = {'kind':'native_round','engine':'claude','session_id':self.sid,
               'entry_id':row['round_id'],
               'source_key':f'claude:{self.sid}:{row["round_id"]}'}
        resolved = connector.resolve_source(ref)
        self.assertEqual(resolved['public'], public)
        self.assertEqual(len(resolved['source']['entries']), 4)
        connector.sdk_root = '/missing/sdk'; connector.node_binary = '/missing/node'
        connector.claude_binary = '/missing/claude'
        with mock.patch.object(connector, 'availability', side_effect=AssertionError('no probe')), \
             mock.patch('subprocess.Popen', side_effect=AssertionError('no native reader')):
            self.assertEqual(connector.resolve_source(ref), resolved)
        with self.assertRaises(Conflict):
            connector.resolve_source({**ref, 'session_id':'someone_else'})

    def test_claude_21268_informational_system_source_is_internal_only(self):
        sdk, claude = self.fake_runtime()
        connector = ClaudeConnector(self.binding, 'nonce_claude',
            store_root=self.root / 'state', sdk_root=sdk,
            claude_binary=str(claude), node_binary=shutil.which('node'),
            ownership=self.coordinator.ownership)
        entries = encode_rounds(self.binding, [self.round([
            {'type':'message','role':'user','content':[{'type':'input_text','text':'question'}]},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'answer'}]},
        ])])
        notice = {
            'type':'system', 'subtype':'informational', 'sessionId':self.sid,
            'cwd':os.path.realpath(self.cwd),
            'message':'Session model shepherd-import could not be restored',
        }
        entries.insert(1, notice)
        connector._validate_source_entries(entries, production=True)
        self.assertEqual([item['role'] for item in claude_public(entries)
                          if item['type'] == 'message'], ['user', 'assistant'])
        # Informational metadata historically appears both with and without a
        # writer version. Preserve that existing allowance across managed
        # runtime upgrades; unknown system subtypes still fail closed below.
        with mock.patch('shepherd.claude_connector.CLAUDE_CODE_VERSION', '2.1.269'):
            connector._validate_source_entries([
                notice, {**notice, 'version':'2.1.205'}], production=True)

    def test_other_system_source_subtypes_still_fail_closed(self):
        sdk, claude = self.fake_runtime()
        connector = ClaudeConnector(self.binding, 'nonce_claude',
            store_root=self.root / 'state', sdk_root=sdk,
            claude_binary=str(claude), node_binary=shutil.which('node'),
            ownership=self.coordinator.ownership)
        base = {'type':'system', 'sessionId':self.sid, 'cwd':os.path.realpath(self.cwd)}
        for subtype in ('warning', None):
            with self.subTest(subtype=subtype):
                with self.assertRaisesRegex(Conflict, '未知内部条目'):
                    connector._validate_source_entries([{**base, 'subtype':subtype}], production=True)

    def test_empty_unknown_suffix_allows_receipt_bound_grounding_without_replay(self):
        sdk, claude = self.fake_runtime()
        connector = ClaudeConnector(self.binding, 'nonce_claude',
            store_root=self.root/'state', sdk_root=sdk, claude_binary=str(claude),
            node_binary=shutil.which('node'), ownership=self.coordinator.ownership)
        gid = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        recovered = connector.recover_suffix(
            {'start_seq':0,'native_revision':'','projection_version':2},
            {'start_seq':0,'native_revision':'','grounding_uuid':gid,
             'grounding_sha256':'a'*64})
        self.assertEqual(recovered['public'], [])
        self.assertEqual(recovered['source']['entries'], [])
        self.assertEqual(recovered['locator']['terminal_state'], 'unknown')
        self.assertEqual(recovered['locator']['grounding_uuid'], gid)
        self.assertEqual(recovered['locator']['grounding_sha256'], 'a'*64)

    def test_receipt_bound_grounding_block_is_removed_without_hiding_user_text(self):
        sdk, claude = self.fake_runtime()
        connector = ClaudeConnector(self.binding, 'nonce_claude',
            store_root=self.root/'state', sdk_root=sdk, claude_binary=str(claude),
            node_binary=shutil.which('node'), ownership=self.coordinator.ownership)
        gid = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        marker = f'<system-reminder data-desktron-grounding="{gid}">\nDYNAMIC\n</system-reminder>'
        entries = [
            {'type':'user','uuid':gid,'sessionId':self.sid,'cwd':str(self.cwd),
             'origin':{'kind':'human'},'message':{'role':'user','content':[
                 {'type':'text','text':marker},{'type':'text','text':'REAL USER'}]}},
            {'type':'assistant','uuid':'assistant-terminal','sessionId':self.sid,
             'cwd':str(self.cwd),'message':{'role':'assistant','content':[
                 {'type':'text','text':'answer'}]}},
        ]
        connector.native.append(entries, '')
        round_id = f'{self.cid}:grounding'
        locator = {'start_seq':0,'end_seq':2,'projection_version':2,
                   'grounding_uuid':gid,
                   'grounding_sha256':hashlib.sha256(marker.encode()).hexdigest(),
                   'terminal_uuid':'assistant-terminal',
                   'terminal_state':'completed','native_revision':connector.native.revision()}
        connector.receipts.write(self.binding, round_id, locator)
        ref = {'kind':'native_round','engine':'claude','session_id':self.sid,
               'entry_id':round_id,'source_key':f'claude:{self.sid}:{round_id}'}
        resolved = connector.resolve_source(ref)
        self.assertIn('DYNAMIC', json.dumps(resolved['source']))
        self.assertNotIn('DYNAMIC', json.dumps(resolved['public']))
        self.assertEqual(resolved['public'], [
            {'type':'message','role':'user','content':[{'type':'input_text','text':'REAL USER'}]},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'answer'}]},
        ])
        bad = dict(locator, grounding_uuid='11111111-2222-4333-8444-555555555555')
        with mock.patch.object(connector.receipts, 'read', return_value={'locator':bad}), \
             self.assertRaisesRegex(Conflict, 'Grounding 标记'):
            connector.resolve_source(ref)
        # A user can type the same visible wrapper. Without the host-bound UUID
        # and digest in the receipt it remains ordinary public user content.
        unmarked = claude_public(entries)
        self.assertIn('DYNAMIC', json.dumps(unmarked))

    def test_native_local_command_output_is_shape_gated_and_not_replayed(self):
        sdk, claude = self.fake_runtime()
        connector = ClaudeConnector(self.binding, 'nonce_claude',
            store_root=self.root/'state', sdk_root=sdk, claude_binary=str(claude),
            node_binary=shutil.which('node'), ownership=self.coordinator.ownership)
        entry={'type':'system','subtype':'local_command','version':'2.1.268',
               'sessionId':self.sid,'cwd':os.path.realpath(self.cwd),'level':'info',
               'content':'<local-command-stdout>Compaction canceled.</local-command-stdout>'}
        connector._validate_source_entries([entry],production=True)
        self.assertEqual(claude_public([entry]), [])
        connector._validate_source_entries([{**entry, 'version':'unknown'}], production=True)
        for changes in ({'content':{}}, {'level':'error'},
                        {'subtype':'unknown'}, {'sessionId':'foreign'}, {'content':'raw text'}):
            with self.subTest(changes=changes), self.assertRaises(Conflict):
                connector._validate_source_entries([{**entry,**changes}],production=True)

    def test_native_compaction_metadata_is_shape_gated_and_not_public_history(self):
        sdk, claude = self.fake_runtime()
        connector = ClaudeConnector(self.binding, 'nonce_claude',
            store_root=self.root/'state', sdk_root=sdk, claude_binary=str(claude),
            node_binary=shutil.which('node'), ownership=self.coordinator.ownership)
        cwd = os.path.realpath(self.cwd)
        entries = [
            {'type':'user','uuid':'user','parentUuid':None,'sessionId':self.sid,'cwd':cwd,
             'message':{'role':'user','content':'question'}},
            {'type':'assistant','uuid':'answer','parentUuid':'user','sessionId':self.sid,'cwd':cwd,
             'message':{'role':'assistant','content':[{'type':'text','text':'answer'}]}},
            {'type':'system','subtype':'compact_boundary','uuid':'boundary','parentUuid':None,
             'logicalParentUuid':'answer','sessionId':self.sid,'cwd':cwd,'version':CLAUDE_CODE_VERSION,
             'content':'Conversation compacted','level':'info',
             'compactMetadata':{'trigger':'auto','preTokens':100,'postTokens':20,
                                'cumulativeDroppedTokens':80,'durationMs':1}},
            {'type':'user','uuid':'summary','parentUuid':'boundary','sessionId':self.sid,'cwd':cwd,
             'version':CLAUDE_CODE_VERSION,'isCompactSummary':True,
             'isVisibleInTranscriptOnly':True,
             'message':{'role':'user','content':'historical compact summary'}},
        ]
        connector._validate_source_entries(entries, production=True)
        self.assertEqual(claude_public(entries), [
            {'type':'message','role':'user','content':[{'type':'input_text','text':'question'}]},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'answer'}]},
        ])
        kinds = connector.source_compatibility({'codec':CODEC_VERSION,'entries':entries})
        self.assertIn({'engine':'claude','action':'ignored_internal_entry',
                       'kind':'system/compact_boundary','count':1}, kinds)
        self.assertIn({'engine':'claude','action':'ignored_internal_entry',
                       'kind':'user/compact_summary','count':1}, kinds)
        with self.assertRaisesRegex(Conflict, '压缩摘要'):
            connector._validate_source_entries([{**entries[-1], 'parentUuid':'wrong'}], production=True)
        # Source compatibility is tied to the version written on each native
        # record, not to whichever managed Claude version is current today.
        with mock.patch('shepherd.claude_connector.CLAUDE_CODE_VERSION', '2.1.269'):
            connector._validate_source_entries(entries[-2:], production=True)
        unknown = [{**entries[-2], 'version':'9.9.9'},
                   {**entries[-1], 'version':'9.9.9'}]
        connector._validate_source_entries(unknown, production=True)
        with self.assertRaisesRegex(Conflict, '压缩边界'):
            connector._validate_source_entries([{**unknown[0], 'compactMetadata':{}}], production=True)

    def test_native_compaction_accepts_reviewed_attachment_parent_chain(self):
        sdk, claude = self.fake_runtime()
        connector = ClaudeConnector(self.binding, 'nonce_claude',
            store_root=self.root/'state', sdk_root=sdk, claude_binary=str(claude),
            node_binary=shutil.which('node'), ownership=self.coordinator.ownership)
        common = {'sessionId':self.sid, 'cwd':os.path.realpath(self.cwd),
                  'version':'2.1.283'}
        boundary = {**common, 'type':'system', 'subtype':'compact_boundary',
            'uuid':'boundary', 'parentUuid':None, 'logicalParentUuid':'prior',
            'content':'Conversation compacted', 'level':'info',
            'compactMetadata':{'trigger':'auto','preTokens':100,'postTokens':20,
                'cumulativeDroppedTokens':80,'durationMs':1,
                'preservedMessages':{'anchorUuid':'summary'}}}
        attachment_shapes = [
            {'type':'instructions','files':[]},
            {'type':'session_context','context':{}},
            {'type':'date','date':'2026-09-29'},
        ]
        attachments = []
        parent = 'boundary'
        for index, attachment in enumerate(attachment_shapes, 1):
            uuid = f'attachment-{index}'
            row = {**common, 'type':'attachment', 'uuid':uuid,
                   'parentUuid':parent, 'attachment':attachment}
            if index != 2: row['rendered'] = []  # 2.1.296 omits it for session_context.
            attachments.append(row)
            parent = uuid
        summary = {**common, 'type':'user', 'uuid':'summary', 'parentUuid':parent,
            'isCompactSummary':True, 'isVisibleInTranscriptOnly':True,
            'message':{'role':'user','content':'redacted compact summary'}}
        entries = [boundary, *attachments, summary]
        connector._validate_source_entries(entries, production=True)
        self.assertEqual(claude_public(entries), [])
        compatibility = connector.source_compatibility({'entries':entries})
        for kind in ('instructions','session_context','date'):
            self.assertIn({'engine':'claude','action':'ignored_internal_entry',
                           'kind':'attachment/' + kind,'count':1}, compatibility)
        self.assertIn({'engine':'claude','action':'ignored_internal_entry',
                       'kind':'user/compact_summary','count':1}, compatibility)

        unsupported = [boundary, {**attachments[0],
            'attachment':{'type':'future_internal'}}, *attachments[1:], summary]
        with self.assertRaises(SourceShapeUnsupported) as caught:
            connector._validate_source_entries(unsupported, production=True)
        self.assertEqual(caught.exception.code, 'source_shape_unsupported')
        self.assertIn('Claude Code 2.1.283', str(caught.exception))

        invalid = {
            'missing_summary': entries[:-1],
            'broken_parent': [boundary, {**attachments[0], 'parentUuid':'wrong'},
                              *attachments[1:], summary],
            'bad_rendered': [boundary, {**attachments[0], 'rendered':'wrong'},
                             *attachments[1:], summary],
            'wrong_anchor': [{**boundary, 'compactMetadata':{
                **boundary['compactMetadata'],
                'preservedMessages':{'anchorUuid':'other'}}}, *attachments, summary],
            'second_boundary': [boundary, {**boundary, 'uuid':'boundary-2'}, summary],
            'ordinary_assistant_before_summary': [boundary, {
                **common, 'type':'assistant', 'uuid':'answer', 'parentUuid':'boundary',
                'message':{'role':'assistant','content':[]}}, summary],
            'duplicate_summary': [boundary, *attachments, summary, summary],
        }
        for name, rows in invalid.items():
            with self.subTest(name=name), self.assertRaises(SourceVerificationError):
                connector._validate_source_entries(rows, production=True)

    def test_native_compaction_keeps_real_281_direct_shape(self):
        sdk, claude = self.fake_runtime()
        connector = ClaudeConnector(self.binding, 'nonce_claude',
            store_root=self.root/'state', sdk_root=sdk, claude_binary=str(claude),
            node_binary=shutil.which('node'), ownership=self.coordinator.ownership)
        common = {'sessionId':self.sid, 'cwd':os.path.realpath(self.cwd),
                  'version':'2.1.281'}
        boundary = {**common, 'type':'system', 'subtype':'compact_boundary',
            'uuid':'boundary-281', 'parentUuid':None, 'logicalParentUuid':'prior',
            'content':'Conversation compacted', 'level':'info',
            'compactMetadata':{'trigger':'auto','preTokens':100,'postTokens':20,
                'cumulativeDroppedTokens':80,'durationMs':1}}
        summary = {**common, 'type':'user', 'uuid':'summary-281',
            'parentUuid':'boundary-281', 'isCompactSummary':True,
            'isVisibleInTranscriptOnly':True,
            'message':{'role':'user','content':'redacted compact summary'}}
        connector._validate_source_entries([boundary, summary], production=True)

    def test_exact_session_metadata_is_internal_but_malformed_local_command_fails_closed(self):
        sdk, claude = self.fake_runtime()
        connector = ClaudeConnector(self.binding, 'nonce_claude',
            store_root=self.root/'state', sdk_root=sdk, claude_binary=str(claude),
            node_binary=shutil.which('node'), ownership=self.coordinator.ownership)
        entries = [
            {'type':'custom-title','customTitle':'renamed','sessionId':self.sid,'timestamp':'future'},
            {'type':'agent-name','agentName':'named','sessionId':self.sid,'version':'future'},
            {'type':'cost-state','sessionId':self.sid,'modelUsage':{},'totalCostUSD':0.0},

        ]
        connector._validate_source_entries(entries, production=True)
        self.assertEqual(claude_public(entries), [])
        kinds = connector.source_compatibility({'codec':CODEC_VERSION,'entries':entries})
        self.assertIn({'engine':'claude','action':'ignored_internal_entry',
                       'kind':'cost-state','count':1}, kinds)
        for changed in ({**entries[0], 'sessionId': 'foreign'},
                        {**entries[0], 'customTitle': None},
                        {**entries[1], 'agentName': 42},
                        {**entries[2], 'totalCostUSD': True},
                        {**entries[2], 'modelUsage': []}):
            with self.assertRaises(Conflict):
                connector._validate_source_entries([changed], production=True)
        local = {'type':'system','subtype':'local_command','uuid':'command',
                 'sessionId':self.sid,'cwd':os.path.realpath(self.cwd),
                 'version':CLAUDE_CODE_VERSION,'content':'ok','level':'info'}
        with self.assertRaisesRegex(Conflict, '本地命令结果形状无效'):
            connector._validate_source_entries([local], production=True)

    @unittest.skipUnless(ClaudeConnector.availability(
                         sdk_root=os.environ.get('DESK_SHEPHERD_CLAUDE_SDK_ROOT')).get('available'),
                         'managed Claude SDK and CLI required')
    def test_runner_executes_into_private_store_and_harness_commits(self):
        fixture = subprocess.Popen(['node', str(ROOT/'desktron/tests/shepherd/claude-fixture-server.mjs')],
                                   stdout=subprocess.PIPE, text=True, encoding='utf-8')
        self.addCleanup(lambda: fixture.poll() is None and fixture.terminate())
        port = json.loads(fixture.stdout.readline())['port']
        self.policy_binding(static_system_prompt('STATIC_STYLE_SENTINEL'))
        connector = ClaudeConnector(self.binding, 'nonce_claude',
            store_root=self.root/'state', ownership=self.coordinator.ownership,
            sdk_root=os.environ.get('DESK_SHEPHERD_CLAUDE_SDK_ROOT'))
        lease = self.coordinator.acquire(self.cid, self.binding_id,
                                          ownership_nonce='nonce_claude')
        events = []
        runner = ClaudeRunner(connector, on_event=events.append, timeout=60,
            system_text='STATIC_STYLE_SENTINEL', options={'_grounding':'DYNAMIC_GROUNDING_SENTINEL'}, env={
            'CLAUDE_CONFIG_DIR':str(self.root/'fixture-config'),
            'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{port}',
            'ANTHROPIC_API_KEY': 'synthetic-test-only',
            'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'})
        row = ExecutionHarness(self.coordinator, connector, runner).run(
            self.cid, lease_id=lease['lease_id'], generation=lease['generation'],
            prompt='runner-question', attempt_id='attempt_runner')
        resolved = connector.resolve_source(row['source'])
        self.assertEqual([item['role'] for item in resolved['public']
                          if item['type'] == 'message'], ['user','assistant'])
        public_json = json.dumps(resolved['public'])
        self.assertIn('runner-question', public_json)
        self.assertIn('runner-ok', public_json)
        self.assertNotIn('DYNAMIC_GROUNDING_SENTINEL', public_json)
        snapshot = next(entry for entry in connector.native.load()
                        if entry.get('type') == 'attachment'
                        and (entry.get('attachment') or {}).get('type') == 'prompt_snapshot')
        self.assertIn('STATIC_STYLE_SENTINEL', json.dumps(snapshot))
        receipt = connector.receipts.read(self.binding, row['round_id'])['locator']
        self.assertTrue(receipt.get('grounding_uuid'))
        self.assertTrue(any(event.get('type') == 'stream_event' for event in events))
        self.assertFalse(list((Path.home()/'.claude/projects').glob(f'*/{self.sid}.jsonl')))

    def test_binding_cwd_or_codec_mismatch_is_rejected(self):
        sdk, claude = self.fake_runtime()
        bad = {**self.binding, 'native_cwd': str(self.root / 'elsewhere')}
        with self.assertRaises(Conflict):
            ClaudeConnector(bad, 'nonce_claude', store_root=self.root / 'state',
                            sdk_root=sdk, claude_binary=str(claude), node_binary=shutil.which('node'),
                            ownership=self.coordinator.ownership)

    def test_bash_cwd_policy_is_connector_version_gated_before_spawn(self):
        connector = mock.Mock()
        connector.binding = self.binding
        connector.connector_version = 'claude-future-v1'
        connector.store_root = str(self.root/'state')
        connector.native = mock.Mock(root=str(self.root), path=str(self.root/'native.sqlite3'))
        connector.native.initialize = mock.Mock()
        connector._available = mock.Mock()
        runner = ClaudeRunner(connector)
        with self.assertRaisesRegex(Conflict, 'cwd 策略'):
            runner.run(prompt='q', round_id='round')
        connector.native.initialize.assert_not_called()

    def test_source_treats_entry_cwd_as_runtime_metadata_and_requires_assistant_terminal(self):
        sdk, claude = self.fake_runtime()
        connector = ClaudeConnector(self.binding, 'nonce_claude', store_root=self.root/'state',
            sdk_root=sdk, claude_binary=str(claude), node_binary=shutil.which('node'),
            ownership=self.coordinator.ownership)
        base = {'type':'assistant', 'sessionId':self.sid, 'cwd':str(self.cwd),
                'uuid':'assistant-terminal', 'message':{'role':'assistant','content':[]}}
        foreign = {**base, 'cwd':str(self.root/'foreign')}
        connector._validate_source_entries([foreign], production=True)
        self.assertEqual(connector.source_compatibility({
            'codec':CODEC_VERSION,'entries':[foreign]}), [{
                'engine':'claude','action':'ignored_environment_attribute',
                'kind':'runtime_cwd_changed','count':1}])
        user = {'type':'user', 'sessionId':self.sid, 'cwd':str(self.root/'foreign'),
                'uuid':'user-terminal','message':{'role':'user','content':'question'}}
        foreign_base = {**base, 'cwd':str(self.root/'foreign')}
        connector.native.append([user, foreign_base], '')
        round_id = f'{self.cid}:terminal-check'
        bad_round = f'{self.cid}:terminal-check-bad'
        connector.receipts.write(self.binding, bad_round, {
            'start_seq':0, 'end_seq':2, 'projection_version':2,
            'terminal_uuid':'user-terminal', 'terminal_state':'completed',
            'native_revision':connector.native.revision()})
        bad_ref = {'kind':'native_round','engine':'claude','session_id':self.sid,
                   'entry_id':bad_round,'source_key':f'claude:{self.sid}:{bad_round}'}
        with self.assertRaisesRegex(Conflict, '终态锚点不是父查询 Assistant'):
            connector.resolve_source(bad_ref)
        round_id = f'{self.cid}:terminal-check-good'
        connector.receipts.write(self.binding, round_id, {
            'start_seq':0, 'end_seq':2, 'projection_version':2,
            'terminal_uuid':'assistant-terminal', 'terminal_state':'completed',
            'native_revision':connector.native.revision()})
        ref = {'kind':'native_round','engine':'claude','session_id':self.sid,
               'entry_id':round_id,'source_key':f'claude:{self.sid}:{round_id}'}
        resolved = connector.resolve_source(ref)
        self.assertEqual([item['role'] for item in resolved['public']], ['user'])
        self.assertEqual(connector.source_compatibility(resolved['source']), [{
            'engine':'claude','action':'ignored_environment_attribute',
            'kind':'runtime_cwd_changed','count':2}])

    def test_bridge_bootstrap_snapshots_static_prompt_and_inlines_dynamic_grounding(self):
        sdk = self.root/'grounding-sdk'; sdk.mkdir()
        (sdk/'package.json').write_text(json.dumps({'version':SDK_VERSION}), encoding='utf-8')
        (sdk/'sdk.mjs').write_text(r'''
export const getSessionMessages=async(id, options)=>options.sessionStore.load({sessionId:id});
export function query({prompt, options}) {
  const iterator = (async function* () {
    yield {type:'system',subtype:'init',claude_code_version:'2.1.268'};
    if (typeof prompt === 'string') {
      await options.sessionStore.append({projectKey:'shepherd',sessionId:options.sessionId}, [{
        type:'attachment', uuid:'prompt-snapshot', sessionId:options.sessionId, cwd:options.cwd,
        attachment:{type:'prompt_snapshot', systemPrompt:[options.systemPrompt.prompt], tools:options.tools}},
        {type:'assistant',uuid:'bootstrap-assistant',sessionId:options.sessionId,cwd:options.cwd,
         message:{id:'msg_shepherd_bootstrap',role:'assistant',stop_reason:'end_turn',
                  content:[{type:'text',text:'initialized'}]}}]);
      yield {type:'assistant',uuid:'bootstrap-assistant',session_id:options.sessionId,
        parent_tool_use_id:null,message:{role:'assistant',content:[{type:'text',text:'initialized'}]}};
      yield {type:'result',subtype:'success',is_error:false,session_id:options.sessionId,
        result:'initialized',permission_denials:[]}; return;
    }
    const frames=[]; for await (const frame of prompt) frames.push(frame);
    const frame=frames[0];
    await options.sessionStore.append({projectKey:'shepherd',sessionId:options.resume}, [
      {...frame,sessionId:options.resume,cwd:options.cwd},
      {type:'assistant',uuid:'grounding-assistant',sessionId:options.resume,cwd:options.cwd,
       message:{role:'assistant',content:[{type:'text',text:'answer'}]}}]);
    yield {type:'system',subtype:'informational',uuid:'grounding-probe',session_id:options.resume,
      content:JSON.stringify({systemPrompt:options.systemPrompt,toolsOwn:Object.hasOwn(options,'tools'),frame})};
    yield {type:'assistant',uuid:'grounding-assistant',session_id:options.resume,
      parent_tool_use_id:null,message:{role:'assistant',content:[{type:'text',text:'answer'}]}};
    yield {type:'result',subtype:'success',is_error:false,session_id:options.resume,
      result:'answer',permission_denials:[]};
  })(); iterator.interrupt=async()=>{};
  iterator.initializationResult=async()=>({commands:[],models:[]});
  iterator.request=async request=>request.subtype==='get_binary_version'
    ? {subtype:'success',response:{version:'2.1.268'}} : {subtype:'error'};
  iterator.close=()=>{};
  return iterator;
}
''', encoding='utf-8')
        fake_cli = version_cli(self.root/'grounding-claude', '2.1.268')
        self.policy_binding(static_system_prompt('STATIC_STYLE'))
        connector = ClaudeConnector(self.binding, 'nonce_claude', store_root=self.root/'state',
            sdk_root=sdk, claude_binary=str(fake_cli), node_binary=shutil.which('node'),
            ownership=self.coordinator.ownership)
        lease = self.coordinator.acquire(self.cid,self.binding_id,ownership_nonce='nonce_claude')
        events=[]
        runner = ClaudeRunner(connector,on_event=events.append,system_text='STATIC_STYLE',
            options={'_grounding':'DYNAMIC_GROUNDING'},
            user_frame={'type':'user','message':{'role':'user','content':[
                {'type':'text','text':'REAL_USER'}]},'parent_tool_use_id':None,
                'session_id':self.sid})
        row=ExecutionHarness(self.coordinator,connector,runner).run(
            self.cid,lease_id=lease['lease_id'],generation=lease['generation'],
            prompt='REAL_USER',attempt_id='grounding-frame')
        entries=connector.native.load()
        snapshot=next(item['attachment'] for item in entries
                      if item.get('attachment',{}).get('type')=='prompt_snapshot')
        self.assertEqual(len(snapshot['systemPrompt']), 1)
        self.assertIn('STATIC_STYLE', snapshot['systemPrompt'][0])
        self.assertNotIn('DYNAMIC_GROUNDING', snapshot['systemPrompt'][0])
        probe=json.loads(next(event['content'] for event in events
                        if event.get('uuid')=='grounding-probe'))
        self.assertFalse(probe['toolsOwn'])
        self.assertIn('STATIC_STYLE',probe['systemPrompt']['prompt'])
        frame=probe['frame']; self.assertEqual(frame['origin'],{'kind':'human'})
        self.assertEqual(frame['message']['content'][1]['text'],'REAL_USER')
        self.assertIn('DYNAMIC_GROUNDING',frame['message']['content'][0]['text'])
        receipt=connector.receipts.read(self.binding,row['round_id'])['locator']
        self.assertEqual(receipt['grounding_uuid'],frame['uuid'])
        resolved=connector.resolve_source(row['source'])
        self.assertNotIn('DYNAMIC_GROUNDING',json.dumps(resolved['public']))
        self.assertIn('REAL_USER',json.dumps(resolved['public']))

    def test_policy_binding_checkpoint_appends_after_bootstrap_prefix_idempotently(self):
        prompt = static_system_prompt('STATIC_STYLE')
        self.policy_binding(prompt)
        sdk, claude = self.fake_runtime()
        connector = ClaudeConnector(self.binding, 'nonce_claude', store_root=self.root/'state',
            sdk_root=sdk, claude_binary=str(claude), node_binary=shutil.which('node'),
            ownership=self.coordinator.ownership)
        bootstrap = [
            {'type':'attachment','uuid':'static-snapshot','sessionId':self.sid,
             'cwd':str(self.cwd),'attachment':{'type':'prompt_snapshot','systemPrompt':[prompt]}},
            {'type':'assistant','uuid':'bootstrap-assistant','sessionId':self.sid,
             'cwd':str(self.cwd),'message':{'id':'msg_shepherd_bootstrap','role':'assistant',
             'stop_reason':'end_turn','content':[{'type':'text','text':'initialized'}]}},
        ]
        bootstrap_revision = connector.native.append(bootstrap, '')
        checkpoint = {'checkpoint_id':'checkpoint_1','covers_through_offset':8,
                      'estimated_tokens':100,'created_at':10}
        first = connector.load_checkpoint({'summary':'portable'}, checkpoint=checkpoint,
                                          expected_revision=bootstrap_revision)
        second = connector.load_checkpoint({'summary':'portable'}, checkpoint=checkpoint,
                                           expected_revision=bootstrap_revision)
        self.assertEqual(first, second)
        self.assertEqual(connector.native.load()[:2], bootstrap)

    def test_legacy_binding_preserves_empty_snapshot_without_rewrite(self):
        sdk, claude = self.fake_runtime()
        connector = ClaudeConnector(self.binding, 'nonce_claude', store_root=self.root/'state',
            sdk_root=sdk, claude_binary=str(claude), node_binary=shutil.which('node'),
            ownership=self.coordinator.ownership)
        entry = {'type':'attachment','uuid':'legacy-empty','sessionId':self.sid,
                 'cwd':str(self.cwd),'attachment':{'type':'prompt_snapshot','systemPrompt':['']}}
        revision = connector.native.append([entry], '')
        self.assertEqual(connector.ensure_prompt_snapshot(static_system_prompt('NEW_STYLE')),
                         {'legacy_preserved':True,'native_revision':revision})
        self.assertEqual(connector.native.load(), [entry])
        self.assertEqual(connector.native.revision(), revision)

    def test_policy_binding_rejects_empty_snapshot_and_history_before_bootstrap(self):
        prompt = static_system_prompt('STATIC_STYLE')
        self.policy_binding(prompt)
        sdk, claude = self.fake_runtime()
        connector = ClaudeConnector(self.binding, 'nonce_claude', store_root=self.root/'state',
            sdk_root=sdk, claude_binary=str(claude), node_binary=shutil.which('node'),
            ownership=self.coordinator.ownership)
        connector.native.append([{
            'type':'attachment','uuid':'empty-snapshot','sessionId':self.sid,
            'cwd':str(self.cwd),'attachment':{'type':'prompt_snapshot','systemPrompt':['']} }], '')
        with self.assertRaisesRegex(Conflict, '快照缺失、未完成或不匹配'):
            connector.ensure_prompt_snapshot(prompt)

        # A fresh policy Binding may not import portable history first and then
        # hide a bootstrap user/assistant pair at the native tail.
        other = self.root/'other'; other.mkdir()
        state = Store(other/'state'); coordinator = Coordinator(state)
        coordinator.create('other_conversation')
        other_id = coordinator.bind('other_conversation','claude',self.sid,
            native_cwd=str(self.cwd),connector_version=CODEC_VERSION,
            binding_id='other_binding',ownership_nonce='other_nonce',
            prompt_snapshot_policy=PROMPT_SNAPSHOT_POLICY,
            prompt_snapshot_sha256=hashlib.sha256(prompt.encode()).hexdigest())
        binding = coordinator.state('other_conversation')['bindings'][other_id]
        other_connector = ClaudeConnector(binding,'other_nonce',store_root=other/'state',
            sdk_root=sdk,claude_binary=str(claude),node_binary=shutil.which('node'),
            ownership=coordinator.ownership)
        other_connector.native.append([{
            'type':'user','uuid':'imported-first','sessionId':self.sid,'cwd':str(self.cwd),
            'message':{'role':'user','content':'history'}}], '')
        with self.assertRaisesRegex(Conflict, '快照缺失、未完成或不匹配'):
            other_connector.ensure_prompt_snapshot(prompt)

    @unittest.skipIf(os.name == 'nt', 'POSIX /bin/bash、BASH_ENV 和权限位隔离契约')
    def test_bridge_isolates_bypass_bash_cwd_env_and_anchors_parent_assistant(self):
        sdk = self.root/'fake-sdk'; sdk.mkdir()
        (sdk/'sdk.mjs').write_text(r'''
import {spawnSync} from 'node:child_process';
export function query({options}) {
  let interrupted = false;
  const iterator = (async function* () {
    yield {type:'system',subtype:'init',claude_code_version:'2.1.283'};
    const outputs = [];
    for (const command of [
      'cd /tmp && printf "first=%s config=%s session=%s\\n" "$PWD" "${CLAUDE_CONFIG_DIR-unset}" "${CLAUDE_CODE_SESSION_ID-unset}"',
      'printf "second=%s config=%s session=%s\\n" "$PWD" "${CLAUDE_CONFIG_DIR-unset}" "${CLAUDE_CODE_SESSION_ID-unset}"']) {
      const mode = (await import('node:fs')).statSync(options.env.BASH_ENV).mode & 0o777;
      const ran = spawnSync('/bin/bash', ['-c', command], {
        cwd:options.cwd, env:options.env, encoding:'utf8'});
      if (ran.status !== 0) throw new Error(ran.stderr || `bash status ${ran.status}`);
      outputs.push(`mode=${mode.toString(8)} ${ran.stdout.trim()}`);
    }
    yield {type:'system', subtype:'informational', uuid:'probe-event',
      session_id:options.resume, content:outputs.join('\n')};
    const terminal = {type:'assistant', uuid:'parent-assistant', sessionId:options.resume,
      session_id:options.resume, cwd:options.cwd,
      message:{role:'assistant', content:[{type:'text',text:'parent answer'}], stop_reason:'end_turn'}};
    await options.sessionStore.append({projectKey:'shepherd',sessionId:options.resume}, [
      {type:'user',uuid:'parent-user',sessionId:options.resume,cwd:options.cwd,
       message:{role:'user',content:'question'}}, terminal,
      {type:'attachment',uuid:'storage-tail',sessionId:options.resume,cwd:options.cwd}]);
    yield {type:'assistant', uuid:terminal.uuid, session_id:options.resume,
      parent_tool_use_id:null, message:terminal.message};
    yield {type:'result', subtype:'success', is_error:false, uuid:'result-frame',
      session_id:options.resume, result:'parent answer', permission_denials:[]};
  })();
  iterator.interrupt = async () => { interrupted = true; };
  iterator.initializationResult = async () => ({commands:[],models:[]});
  iterator.request = async request => request.subtype==='get_binary_version'
    ? {subtype:'success',response:{version:'2.1.283'}} : {subtype:'error'};
  return iterator;
}
''', encoding='utf-8')
        db = self.root/'native.sqlite3'
        with database(db) as conn:
            conn.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            conn.execute('CREATE TABLE entries (seq INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT UNIQUE, payload TEXT NOT NULL)')
            conn.execute("INSERT INTO meta(key,value) VALUES('schema','1')")
            conn.execute("INSERT INTO meta(key,value) VALUES('revision','')")
            seed = {'type':'attachment','uuid':'seed','sessionId':self.sid,
                    'cwd':str(self.cwd),'attachment':{'type':'prompt_snapshot'}}
            conn.execute('INSERT INTO entries(uuid,payload) VALUES(?,?)',
                         ('seed', json.dumps(seed, separators=(',',':'), sort_keys=True)))
        db.chmod(0o600)
        claude=version_cli(self.root/'claude-current', '2.1.283')
        payload = self.root/'payload.json'
        payload.write_text(json.dumps({
            'sdkRoot':str(sdk), 'dbPath':str(db), 'sessionId':self.sid,
            'cwd':str(self.cwd), 'claudeBinary':str(claude), 'prompt':'question',
            'bridgeProtocolVersion':3,
            'protectedPaths':[str(self.root/'protected')],
            # Shepherd always has an approval callback, but Claude deliberately
            # skips it in bypass mode. PreToolUse must still enforce isolation.
            'permissionMode':'bypassPermissions', 'interactivePermissions':True,
            'retryBudgetMs':60000, 'env':{
                'CLAUDE_CONFIG_DIR':'/private/parent-store',
                'CLAUDE_CODE_SESSION_ID':'parent-session'}}), encoding='utf-8')
        bridge = ROOT/'desktron/lib/shepherd/claude_runner.mjs'
        proc=subprocess.Popen([shutil.which('node'),str(bridge),str(payload)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True, encoding='utf-8')
        prepared_line=proc.stdout.readline()
        proc.stdin.write('{"type":"start"}\n');proc.stdin.flush()
        stdout,stderr=proc.communicate(timeout=30)
        self.assertEqual(proc.returncode,0,prepared_line+stdout+stderr)
        rows = [json.loads(line) for line in (prepared_line+stdout).splitlines()]
        prepared=next(row for row in rows if row.get('type') == 'prepared')
        self.assertEqual(prepared['bridgeProtocolVersion'],3)
        self.assertEqual(prepared['parentUuid'],'seed')
        self.assertEqual(prepared['storeSize'],1)
        self.assertEqual(prepared['nativeRevision'],'')
        self.assertEqual(prepared['claudeVersion'],'2.1.283')
        result = next(row for row in rows if row.get('type') == 'result')
        probe = next(row['event']['content'] for row in rows
                     if row.get('type') == 'event'
                     and row.get('event', {}).get('uuid') == 'probe-event')
        self.assertEqual(result['terminalUuid'], 'parent-assistant')
        self.assertNotEqual(result['terminalUuid'], 'storage-tail')
        self.assertIn('mode=600 first=/tmp config=unset session=unset', probe)
        self.assertIn(f'mode=600 second={os.path.realpath(self.cwd)} config=unset session=unset', probe)
        self.assertFalse(list(self.root.glob('native.sqlite3.bash-env-*')))

    def test_bridge_rejects_process_version_mismatch_before_prepared_or_user_input(self):
        sdk = self.root/'version-sdk'; sdk.mkdir()
        (sdk/'sdk.mjs').write_text(r'''
import {writeFileSync} from 'node:fs';
export function query({prompt,options}) {
  const iterator=(async function* () {
    for await (const frame of prompt) {
      writeFileSync(options.cwd+'/user-consumed',JSON.stringify(frame));
      yield {type:'system',subtype:'init',claude_code_version:'2.1.283'};
    }
  })();
  iterator.interrupt=async()=>{};
  iterator.initializationResult=async()=>({commands:[],models:[]});
  iterator.request=async request=>request.subtype==='get_binary_version'
    ? {subtype:'success',response:{version:'2.1.284'}} : {subtype:'error'};
  return iterator;
}
''', encoding='utf-8')
        claude=version_cli(self.root/'version-claude', '2.1.283')
        db=self.root/'version.sqlite3'
        with database(db) as conn:
            conn.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            conn.execute('CREATE TABLE entries (seq INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT UNIQUE, payload TEXT NOT NULL)')
            conn.execute("INSERT INTO meta(key,value) VALUES('schema','1')")
            conn.execute("INSERT INTO meta(key,value) VALUES('revision','')")
            conn.execute('INSERT INTO entries(uuid,payload) VALUES(?,?)',('seed',json.dumps({
                'type':'attachment','uuid':'seed','sessionId':self.sid,'cwd':str(self.cwd)})))
        db.chmod(0o600)
        payload=self.root/'version-payload.json'
        payload.write_text(json.dumps({'sdkRoot':str(sdk),'dbPath':str(db),
            'sessionId':self.sid,'cwd':str(self.cwd),'claudeBinary':str(claude),
            'prompt':'secret','bridgeProtocolVersion':3,
            'bashCwdPolicy':{'codec':CODEC_VERSION,'mode':'reset_each_call'},
            'protectedPaths':[str(self.root/'protected')]}), encoding='utf-8')
        completed=subprocess.run([shutil.which('node'),
            str(ROOT/'desktron/lib/shepherd/claude_runner.mjs'),str(payload)],
            capture_output=True,text=True,timeout=15, encoding='utf-8')
        self.assertNotEqual(completed.returncode,0)
        self.assertNotIn('"type":"prepared"',completed.stdout)
        self.assertIn('executable/process version mismatch: 2.1.283 != 2.1.284',completed.stderr)
        self.assertFalse((self.cwd/'user-consumed').exists())

    def test_bridge_rejects_only_an_explicit_conflicting_host_policy(self):
        payload = self.root/'bad-policy.json'
        payload.write_text(json.dumps({'bashCwdPolicy':{
            'codec':'future-codec','mode':'unsafe'}}), encoding='utf-8')
        completed = subprocess.run([shutil.which('node'),
            str(ROOT/'desktron/lib/shepherd/claude_runner.mjs'), str(payload)],
            capture_output=True, text=True, timeout=10, encoding='utf-8')
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn('Bash cwd policy/version mismatch', completed.stderr)

    def test_bridge_removes_bash_env_when_sdk_query_throws(self):
        sdk = self.root/'throwing-sdk'; sdk.mkdir()
        (sdk/'sdk.mjs').write_text("export function query() { throw new Error('query failed'); }\n", encoding='utf-8')
        db = self.root/'throwing.sqlite3'
        with database(db) as conn:
            conn.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            conn.execute('CREATE TABLE entries (seq INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT UNIQUE, payload TEXT NOT NULL)')
            conn.execute("INSERT INTO meta(key,value) VALUES('schema','1')")
            conn.execute("INSERT INTO meta(key,value) VALUES('revision','')")
            seed = {'type':'attachment','uuid':'seed','sessionId':self.sid,'cwd':str(self.cwd)}
            conn.execute('INSERT INTO entries(uuid,payload) VALUES(?,?)',
                         ('seed', json.dumps(seed, separators=(',',':'), sort_keys=True)))
        db.chmod(0o600)
        payload = self.root/'throwing-payload.json'
        payload.write_text(json.dumps({
            'sdkRoot':str(sdk), 'dbPath':str(db), 'sessionId':self.sid,
            'cwd':str(self.cwd), 'claudeBinary':'/unused', 'prompt':'question',
            'bashCwdPolicy':{'codec':CODEC_VERSION,'mode':'reset_each_call'},
            'protectedPaths':[str(self.root/'protected')],
            'permissionMode':'dontAsk', 'interactivePermissions':False,
            'retryBudgetMs':60000}), encoding='utf-8')
        completed = subprocess.run([
            shutil.which('node'), str(ROOT/'desktron/lib/shepherd/claude_runner.mjs'),
            str(payload)], input='{"type":"start"}\n', capture_output=True,
            text=True, timeout=30, encoding='utf-8')
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn('query failed', completed.stderr)
        self.assertFalse(list(self.root.glob('throwing.sqlite3.bash-env-*')))

    @unittest.skipIf(os.name == 'nt', 'POSIX /bin/bash 环境隔离；审批协议通用用例保留')
    def test_bridge_approval_shows_original_bash_and_isolates_edited_command(self):
        sdk = self.root/'approval-sdk'; sdk.mkdir()
        (sdk/'sdk.mjs').write_text(r'''
import {spawnSync} from 'node:child_process';
export function query({options}) {
  const iterator = (async function* () {
    yield {type:'system',subtype:'init',claude_code_version:'unused'};
    const original = {command:'printf "original"'};
    const decision = await options.canUseTool('Bash', original, {toolUseID:'approval'});
    if (decision.behavior !== 'allow') throw new Error('approval denied');
    const ran = spawnSync('/bin/bash', ['-c', decision.updatedInput.command], {
      cwd:options.cwd, env:options.env, encoding:'utf8'});
    if (ran.status !== 0) throw new Error(ran.stderr || `bash status ${ran.status}`);
    yield {type:'system', subtype:'informational', uuid:'approval-probe',
      session_id:options.resume, content:ran.stdout.trim()};
    const terminal = {type:'assistant', uuid:'approval-assistant', sessionId:options.resume,
      cwd:options.cwd, message:{role:'assistant', content:[{type:'text',text:'done'}]}};
    await options.sessionStore.append({projectKey:'shepherd',sessionId:options.resume}, [terminal]);
    yield {type:'assistant', uuid:terminal.uuid, session_id:options.resume,
      parent_tool_use_id:null, message:terminal.message};
    yield {type:'result', subtype:'success', is_error:false, session_id:options.resume,
      result:'done', permission_denials:[]};
  })();
  iterator.interrupt = async () => {};
  iterator.initializationResult = async () => ({commands:[],models:[]});
  return iterator;
}
''', encoding='utf-8')
        db = self.root/'approval.sqlite3'
        with database(db) as conn:
            conn.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            conn.execute('CREATE TABLE entries (seq INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT UNIQUE, payload TEXT NOT NULL)')
            conn.execute("INSERT INTO meta(key,value) VALUES('schema','1')")
            conn.execute("INSERT INTO meta(key,value) VALUES('revision','')")
            seed = {'type':'attachment','uuid':'seed','sessionId':self.sid,'cwd':str(self.cwd),
                    'attachment':{'type':'prompt_snapshot'}}
            conn.execute('INSERT INTO entries(uuid,payload) VALUES(?,?)',
                         ('seed', json.dumps(seed, separators=(',',':'), sort_keys=True)))
        db.chmod(0o600)
        payload = self.root/'approval-payload.json'
        payload.write_text(json.dumps({
            'sdkRoot':str(sdk), 'dbPath':str(db), 'sessionId':self.sid,
            'cwd':str(self.cwd), 'claudeBinary':'/unused', 'prompt':'question',
            'bashCwdPolicy':{'codec':CODEC_VERSION,'mode':'reset_each_call'},
            'protectedPaths':[str(self.root/'protected')],
            'permissionMode':'default', 'interactivePermissions':True,
            'retryBudgetMs':60000, 'env':{
                'CLAUDE_CONFIG_DIR':'/private/parent-store',
                'CLAUDE_CODE_SESSION_ID':'parent-session'}}), encoding='utf-8')
        bridge = ROOT/'desktron/lib/shepherd/claude_runner.mjs'
        proc = subprocess.Popen([shutil.which('node'), str(bridge), str(payload)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding='utf-8')
        prepared = json.loads(proc.stdout.readline())
        self.assertEqual(prepared['type'], 'prepared')
        proc.stdin.write('{"type":"start"}\n'); proc.stdin.flush()
        initialized = json.loads(proc.stdout.readline())
        self.assertEqual((initialized['type'], initialized['event']['subtype']), ('event', 'init'))
        permission = json.loads(proc.stdout.readline())
        self.assertEqual(permission['type'], 'permission')
        self.assertEqual(permission['input']['command'], 'printf "original"')
        edited = ('cd /tmp && printf "cwd=%s config=%s session=%s" "$PWD" '
                  '"${CLAUDE_CONFIG_DIR-unset}" "${CLAUDE_CODE_SESSION_ID-unset}"')
        proc.stdin.write(json.dumps({'type':'permission_result', 'id':permission['id'],
                                     'allow':True, 'updatedInput':{'command':edited}}) + '\n')
        proc.stdin.flush()
        stdout, stderr = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, stdout + stderr)
        rows = [json.loads(line) for line in stdout.splitlines()]
        probe = next(row['event']['content'] for row in rows
                     if row.get('type') == 'event'
                     and row.get('event', {}).get('uuid') == 'approval-probe')
        self.assertEqual(probe, f'cwd=/tmp config=unset session=unset')
        self.assertEqual(next(row for row in rows if row.get('type') == 'result')['terminalUuid'],
                         'approval-assistant')
        self.assertFalse(list(self.root.glob('approval.sqlite3.bash-env-*')))

    def test_real_sdk_bash_preserves_cwd_and_private_env_isolation(self):
        sdk = os.environ.get('DESK_SHEPHERD_CLAUDE_SDK_ROOT') or ClaudeConnector.default_sdk_root()
        claude = os.environ.get('DESK_SHEPHERD_CLAUDE_BINARY') or None
        if not ClaudeConnector.availability(sdk_root=sdk, claude_binary=claude).get('available'):
            self.skipTest('managed Claude SDK and CLI required')
        class ProbeHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body=b'desk-probe-ok';self.send_response(200);self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body)
            def log_message(self, *_args): pass
        probe = ThreadingHTTPServer(('127.0.0.1',0),ProbeHandler)
        probe_thread=threading.Thread(target=probe.serve_forever,daemon=True);probe_thread.start()
        self.addCleanup(lambda:(probe.shutdown(),probe.server_close(),probe_thread.join(timeout=2)))
        control_path=self.root/'app-control.sock';control=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        control.bind(str(control_path));control.listen(1);control_seen=threading.Event()
        def accept_control():
            connection,_=control.accept()
            with connection:
                if connection.recv(16)==b'probe':control_seen.set()
        control_thread=threading.Thread(target=accept_control,daemon=True);control_thread.start()
        self.addCleanup(lambda:(control.close(),control_thread.join(timeout=2)))
        runtime_marker=self.root/'desk-data-write'
        python=("import pathlib,socket,urllib.request;"
                f"assert urllib.request.urlopen('http://127.0.0.1:{probe.server_port}/desk-probe').read()==b'desk-probe-ok';"
                f"s=socket.socket(socket.AF_UNIX);s.connect({str(control_path)!r});s.sendall(b'probe');s.close();"
                f"pathlib.Path({str(runtime_marker)!r}).write_text('ok')")
        third_command='python3 -c '+shlex.quote(python)
        recap_marker=self.root/'.memory/fixture/recap.request'
        journal=ROOT/'.claude/skills/journal/scripts/journal_append.py'
        fourth_command=(f'DESKTRON_RECAP_REQUEST_PATH={shlex.quote(str(recap_marker))} '
                        f'python3 {shlex.quote(str(journal))} --request')
        server = self.root/'bash-fixture.mjs'
        source=r'''
import http from 'node:http';
const server = http.createServer((request, response) => {
  let raw = '';
  request.on('data', chunk => raw += chunk);
  request.on('end', () => {
    if (request.url.includes('count_tokens')) {
      const body = '{"input_tokens":10}';
      response.writeHead(200, {'content-type':'application/json','content-length':body.length});
      response.end(body); return;
    }
    const payload = JSON.parse(raw || '{}'), messages = payload.messages || [];
    const textOf = message => Array.isArray(message.content)
      ? message.content.map(x => x.text || '').join('') : String(message.content || '');
    const requestAt = messages.findLastIndex(message => textOf(message).includes('bash-isolation-real'));
    const results = requestAt < 0 ? [] : messages.slice(requestAt + 1).flatMap(message =>
      Array.isArray(message.content) ? message.content.filter(block => block.type === 'tool_result') : []);
    const bootstrap = requestAt < 0;
    const id = 'msg_' + Math.random().toString(16).slice(2), events = [
      {type:'message_start',message:{id,type:'message',role:'assistant',content:[],model:payload.model,
       stop_reason:null,stop_sequence:null,usage:{input_tokens:10,output_tokens:0}}},
      {type:'content_block_start',index:0,content_block:{type:'text',text:''}},
      {type:'content_block_delta',index:0,delta:{type:'text_delta',text:bootstrap ? 'initialized' : 'done'}},
      {type:'content_block_stop',index:0},
      {type:'message_delta',delta:{stop_reason:'end_turn',stop_sequence:null},usage:{output_tokens:2}},
      {type:'message_stop'},
    ];
    const commands = [
      'cd /tmp && printf "first=%s config=%s session=%s" "$PWD" "${CLAUDE_CONFIG_DIR-unset}" "${CLAUDE_CODE_SESSION_ID-unset}"',
      'printf "second=%s config=%s session=%s" "$PWD" "${CLAUDE_CONFIG_DIR-unset}" "${CLAUDE_CODE_SESSION_ID-unset}"',
      __THIRD_COMMAND__,
      __FOURTH_COMMAND__,
    ];
    if (!bootstrap && requestAt >= 0 && results.length < commands.length) {
      const command = commands[results.length];
      events.splice(1, 4,
        {type:'content_block_start',index:0,content_block:{type:'tool_use',id:`toolu_bash_${results.length}`,name:'Bash',input:{}}},
        {type:'content_block_delta',index:0,delta:{type:'input_json_delta',partial_json:JSON.stringify({command})}},
        {type:'content_block_stop',index:0},
        {type:'message_delta',delta:{stop_reason:'tool_use',stop_sequence:null},usage:{output_tokens:10}});
    }
    const body = events.map(event => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join('');
    response.writeHead(200, {'content-type':'text/event-stream','content-length':Buffer.byteLength(body)});
    response.end(body);
  });
});
server.listen(0, '127.0.0.1', () => console.log(JSON.stringify({port:server.address().port})));
process.on('SIGTERM', () => server.close(() => process.exit(0)));
'''
        server.write_text(source.replace('__THIRD_COMMAND__',json.dumps(third_command))
                          .replace('__FOURTH_COMMAND__',json.dumps(fourth_command)),encoding='utf-8')
        fixture = subprocess.Popen(['node', str(server)], stdout=subprocess.PIPE, text=True, encoding='utf-8')
        def close_fixture():
            fixture.terminate(); fixture.wait(timeout=5); fixture.stdout.close()
        self.addCleanup(close_fixture)
        port = json.loads(fixture.stdout.readline())['port']
        # Product-created Bindings now bootstrap a static snapshot before resume.
        self.policy_binding(static_system_prompt(''))
        connector = ClaudeConnector(self.binding, 'nonce_claude', store_root=self.root/'state',
            ownership=self.coordinator.ownership, sdk_root=sdk, claude_binary=claude)
        lease = self.coordinator.acquire(self.cid, self.binding_id, ownership_nonce='nonce_claude')
        asks = []
        def permission(request):
            asks.append(request)
            expected = [
                'cd /tmp && printf "first=%s config=%s session=%s" "$PWD" '
                '"${CLAUDE_CONFIG_DIR-unset}" "${CLAUDE_CODE_SESSION_ID-unset}"',
                'printf "second=%s config=%s session=%s" "$PWD" '
                '"${CLAUDE_CONFIG_DIR-unset}" "${CLAUDE_CODE_SESSION_ID-unset}"',
                third_command,
                fourth_command,
            ][len(asks)-1]
            self.assertEqual(request['input']['command'], expected)
            return {'allow':True}
        runner = ClaudeRunner(connector, on_permission=permission, timeout=30,
            options={'mode':'default'}, env={
                'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{port}',
                'CLAUDE_CONFIG_DIR':str(self.root/'fixture-config'),
                'ANTHROPIC_API_KEY':'synthetic-test-only',
                'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1'})
        try:
            row = ExecutionHarness(self.coordinator, connector, runner).run(self.cid,
                lease_id=lease['lease_id'], generation=lease['generation'],
                prompt='bash-isolation-real', attempt_id='attempt_bash_isolation')
        except Exception as exc:
            raise AssertionError(str(exc) + '\n' + ''.join(runner.process.errors)[-3000:]) from exc
        # Native Bash preserves ordinary approval semantics and exposes each
        # untouched command before execution.
        self.assertEqual(len(asks), 4)
        resolved = connector.resolve_source(row['source'])
        public = json.dumps(resolved['public'], ensure_ascii=False)
        self.assertIn('cd /tmp && printf', public)
        self.assertNotIn('.bash-env-', public)
        self.assertNotIn('Desktron: keep Claude parent identity', public)
        self.assertIn('first=/tmp config=unset session=unset', public)
        self.assertIn(f'second={os.path.realpath(self.cwd)} config=unset session=unset', public)
        self.assertTrue(control_seen.wait(2))
        self.assertEqual(runtime_marker.read_text(encoding='utf-8'),'ok')
        self.assertTrue(recap_marker.is_file())
        self.assertFalse(list(Path(connector.native.path).parent.glob(
            Path(connector.native.path).name + '.bash-env-*')))

    def test_real_runner_protected_read_fails_locally_then_continues(self):
        fixture = subprocess.Popen(['node', str(ROOT/'desktron/tests/shepherd/claude-fixture-server.mjs')],
                                   stdout=subprocess.PIPE, text=True, encoding='utf-8')
        def close_fixture():
            fixture.terminate(); fixture.wait(timeout=5); fixture.stdout.close()
        self.addCleanup(close_fixture)
        port = json.loads(fixture.stdout.readline())['port']
        self.policy_binding(static_system_prompt(''))
        sdk = os.environ.get('DESK_SHEPHERD_CLAUDE_SDK_ROOT') or ClaudeConnector.default_sdk_root()
        claude = os.environ.get('DESK_SHEPHERD_CLAUDE_BINARY') or None
        if not ClaudeConnector.availability(sdk_root=sdk, claude_binary=claude).get('available'):
            self.skipTest('managed Claude SDK and CLI required')
        connector = ClaudeConnector(self.binding, 'nonce_claude', store_root=self.root/'state',
            ownership=self.coordinator.ownership, sdk_root=sdk, claude_binary=claude)
        lease = self.coordinator.acquire(self.cid, self.binding_id, ownership_nonce='nonce_claude')
        tcc_probe = os.environ.get('DESK_TEST_CLAUDE_TCC_PROBE_ROOT')
        denied = Path(tcc_probe) if tcc_probe else self.root/'protected'
        secret = denied/'.desktron-nonexistent-probe' if tcc_probe else denied/'secret.txt'
        if not tcc_probe:
            denied.mkdir(); secret.write_text('must-not-be-read', encoding='utf-8')
        def unexpected_permission(request):
            self.fail('protected path must fail before user approval: ' + str(request.get('tool')))
        with mock.patch('shepherd.runners.MACOS_AGENT_PROTECTED_DIRS', (str(denied),)):
            runner = ClaudeRunner(connector, on_permission=unexpected_permission, timeout=30,
                options={'mode':'bypassPermissions'}, env={
                'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{port}',
                'CLAUDE_CONFIG_DIR':str(self.root/'fixture-config'),
                'ANTHROPIC_API_KEY':'synthetic-test-only',
                'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1'})
            try:
                row = ExecutionHarness(self.coordinator, connector, runner).run(self.cid,
                    lease_id=lease['lease_id'], generation=lease['generation'],
                    prompt=f'protected-read-fixture:{secret}',
                    attempt_id='attempt_protected_read')
            except Exception as exc:
                self.fail(str(exc) + ' stderr=' + ''.join(runner.process.errors)[-4000:])
            self.assertEqual(row['terminal_state'], 'completed')
            public = json.dumps(connector.resolve_source(row['source'])['public'],
                                ensure_ascii=False)
            self.assertIn('continued-after-denial', public)
            self.assertIn('function_call_output', public)
            self.assertIn('工具执行失败', public)
        if not tcc_probe:
            self.assertEqual(secret.read_text(encoding='utf-8'), 'must-not-be-read')

    def test_real_runner_loads_project_pretooluse_hook(self):
        sdk = os.environ.get('DESK_SHEPHERD_CLAUDE_SDK_ROOT') or ClaudeConnector.default_sdk_root()
        claude = os.environ.get('DESK_SHEPHERD_CLAUDE_BINARY') or None
        if not ClaudeConnector.availability(sdk_root=sdk, claude_binary=claude).get('available'):
            self.skipTest('managed Claude SDK and CLI required')
        fixture = subprocess.Popen(['node', str(ROOT/'desktron/tests/shepherd/claude-fixture-server.mjs')],
                                   stdout=subprocess.PIPE, text=True, encoding='utf-8')
        def close_fixture():
            fixture.terminate(); fixture.wait(timeout=5); fixture.stdout.close()
        self.addCleanup(close_fixture)
        port = json.loads(fixture.stdout.readline())['port']
        project = self.cwd/'.claude'; project.mkdir()
        hook = project/'fixture_hook.py'; audit = self.root/'project-hook-ran'
        hook.write_text('''import json,sys\njson.load(sys.stdin)\nopen(%s,"w").write("ran")\nprint(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"project-hook-denied"}}))\n''' % repr(str(audit)), encoding='utf-8')
        (project/'settings.json').write_text(json.dumps({'hooks':{'PreToolUse':[{
            'matcher':'Write|Bash','hooks':[{'type':'command','command':f'python3 "{hook}"'}]}]}}),
            encoding='utf-8')
        skill = project/'skills/fixture-skill'; skill.mkdir(parents=True)
        (skill/'SKILL.md').write_text('---\nname: fixture-skill\ndescription: fixture project skill\n---\nfixture\n',
                                      encoding='utf-8')
        self.policy_binding(static_system_prompt(''))
        connector = ClaudeConnector(self.binding, 'nonce_claude', store_root=self.root/'state',
            ownership=self.coordinator.ownership, sdk_root=sdk, claude_binary=claude)
        lease = self.coordinator.acquire(self.cid, self.binding_id, ownership_nonce='nonce_claude')
        target = self.root/'blocked-write'
        def unexpected_permission(request):
            self.fail('project PreToolUse hook must run before Desk approval: ' + str(request.get('tool')))
        events = []
        runner = ClaudeRunner(connector, on_permission=unexpected_permission, on_event=events.append,
            timeout=30, options={'mode':'bypassPermissions'}, env={
                'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{port}',
                'CLAUDE_CONFIG_DIR':str(self.root/'fixture-config'),
                'ANTHROPIC_API_KEY':'synthetic-test-only',
                'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1'})
        row = ExecutionHarness(self.coordinator, connector, runner).run(self.cid,
            lease_id=lease['lease_id'], generation=lease['generation'],
            prompt='permission-fixture:' + str(target), attempt_id='attempt_project_hook')
        self.assertEqual(row['terminal_state'], 'completed')
        self.assertEqual(audit.read_text(encoding='utf-8'), 'ran')
        self.assertFalse(target.exists())
        public = json.dumps(connector.resolve_source(row['source'])['public'], ensure_ascii=False)
        self.assertIn('project-hook-denied', public)
        self.assertIn('fixture-skill', json.dumps(events, ensure_ascii=False))

        runner = ClaudeRunner(connector, on_permission=unexpected_permission, timeout=30,
            options={'mode':'bypassPermissions'}, env={
                'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{port}',
                'CLAUDE_CONFIG_DIR':str(self.root/'fixture-config'),
                'ANTHROPIC_API_KEY':'synthetic-test-only',
                'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1'})
        row = ExecutionHarness(self.coordinator, connector, runner).run(self.cid,
            lease_id=lease['lease_id'], generation=lease['generation'],
            prompt='protected-bash-fixture:' + str(target), attempt_id='attempt_project_bash_hook')
        self.assertEqual(row['terminal_state'], 'completed')
        public = json.dumps(connector.resolve_source(row['source'])['public'], ensure_ascii=False)
        self.assertIn('project-hook-denied', public)
        self.assertIn('continued-after-denial', public)

    @unittest.skipUnless(ClaudeConnector.availability(
                         sdk_root=os.environ.get('DESK_SHEPHERD_CLAUDE_SDK_ROOT')).get('available'),
                         'managed Claude SDK and CLI required')
    def test_real_runner_permission_approve_updated_input_then_deny(self):
        fixture = subprocess.Popen(['node', str(ROOT/'desktron/tests/shepherd/claude-fixture-server.mjs')],
                                   stdout=subprocess.PIPE, text=True, encoding='utf-8')
        def close_fixture():
            fixture.terminate(); fixture.wait(timeout=5); fixture.stdout.close()
        self.addCleanup(close_fixture)
        port = json.loads(fixture.stdout.readline())['port']
        # Match product-created Bindings: snapshot/bootstrap precedes resume.
        # A bare test binding has no native conversation for any CLI to resume.
        self.policy_binding(static_system_prompt(''))
        connector = ClaudeConnector(self.binding, 'nonce_claude', store_root=self.root/'state',
            ownership=self.coordinator.ownership,
            sdk_root=os.environ.get('DESK_SHEPHERD_CLAUDE_SDK_ROOT'))
        lease = self.coordinator.acquire(self.cid, self.binding_id, ownership_nonce='nonce_claude')
        for mode in ('approve', 'deny', 'cancel'):
            allowed = mode == 'approve'
            marker = self.root/mode
            asks = []
            events = []
            def permission(request):
                asks.append(request)
                if mode == 'cancel': runner.cancel()
                return {'allow':allowed, 'updatedInput':{**request['input'], 'content':'reviewed'}}
            runner = ClaudeRunner(connector, on_permission=permission, on_event=events.append, timeout=15,
                options={'mode':'default'}, env={'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{port}',
                'CLAUDE_CONFIG_DIR':str(self.root/'fixture-config'),
                'ANTHROPIC_API_KEY':'synthetic-test-only', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1'})
            try:
                row = ExecutionHarness(self.coordinator, connector, runner).run(self.cid,
                    lease_id=lease['lease_id'], generation=lease['generation'],
                    prompt='permission-fixture:' + str(marker))
            except Exception as exc:
                self.fail(str(exc) + ' asks=' + str(len(asks)) + ' events=' +
                          json.dumps(events)[-2000:] + ' stderr=' + ''.join(runner.process.errors)[-3000:])
            self.assertEqual(row['terminal_state'],
                             'interrupted_confirmed' if mode == 'cancel' else 'completed')
            self.assertTrue(asks, 'native permission bridge must actually be exercised: ' +
                            json.dumps(events, ensure_ascii=False)[-10000:])
            self.assertEqual(asks[0]['tool'], 'Write')
            if allowed: self.assertEqual(marker.read_text(encoding='utf-8'), 'reviewed')
            else: self.assertFalse(marker.exists())
        self.assertFalse(list((Path.home()/'.claude/projects').glob(f'*/{self.sid}.jsonl')))

    def test_real_cli_compaction_and_tool_image_shapes_are_supported(self):
        sdk = os.environ.get('DESK_SHEPHERD_CLAUDE_SDK_ROOT') or ClaudeConnector.default_sdk_root()
        claude = os.environ.get('DESK_SHEPHERD_CLAUDE_BINARY') or cli_path.which('claude')
        if not ClaudeConnector.availability(sdk_root=sdk, claude_binary=claude).get('available'):
            self.skipTest('managed Claude SDK and CLI required')
        probe = ROOT/'desktron/tests/shepherd/claude-native-compat.mjs'
        work = self.root/'native-compat'
        completed = subprocess.run([shutil.which('node'), str(probe), sdk, claude, str(work)],
                                   capture_output=True, text=True, timeout=120, encoding='utf-8')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report['promptSnapshotHasStatic'])
        self.assertTrue(report['groundingRequestHasDynamic'])
        self.assertTrue(report['groundingRequestHasUser'])
        self.assertGreater(report['groundingRequestToolCount'], 0)
        entries = [report['boundary'], report['summary'],
                   report['imageCallRow'], report['imageResultRow']]
        connector = ClaudeConnector(self.binding, 'nonce_claude',
            store_root=self.root/'state', sdk_root=sdk, claude_binary=claude,
            node_binary=shutil.which('node'), ownership=self.coordinator.ownership)
        # Probe owns a separate session id/cwd. Rebind only identity fields to
        # exercise the exact pinned record shapes against this fixture Binding.
        for entry in entries:
            entry['sessionId'] = self.sid
            if entry.get('cwd'): entry['cwd'] = str(self.cwd)
        connector._validate_source_entries(entries[:2], production=True)
        projected = claude_public(entries[2:])
        output = next(item['output'] for item in projected
                      if item.get('type') == 'function_call_output')
        self.assertTrue(any(block.get('type') == 'input_image' for block in output))

    @unittest.skipUnless(os.environ.get('DESK_SHEPHERD_CLAUDE_PROTOCOL_TESTS') == '1',
                         'opt-in real Claude Agent SDK protocol')
    def test_real_sdk_reads_connector_materialized_session(self):
        sdk = os.environ['DESK_SHEPHERD_CLAUDE_SDK_ROOT']
        claude = os.environ.get('DESK_SHEPHERD_CLAUDE_BINARY') or shutil.which('claude')
        connector = ClaudeConnector(self.binding, 'nonce_claude',
            store_root=self.root / 'state', sdk_root=sdk,
            claude_binary=claude, node_binary=shutil.which('node'),
            ownership=self.coordinator.ownership)
        row = self.round([
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'seed-user'}]},
            {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'seed-assistant'}]},
        ])
        result = connector.append([row], expected_revision='')
        self.assertEqual(result['native_revision'], connector.native.revision())
        probe = ROOT / 'desktron/tests/shepherd/claude-resume.mjs'
        completed = subprocess.run(['node', str(probe), sdk, connector.native.path, self.sid,
                                    str(self.cwd), claude], capture_output=True, text=True, timeout=120, encoding='utf-8')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report['ok'])
        self.assertTrue(report['hasSeedUser'])
        self.assertTrue(report['hasSeedAssistant'])
        self.assertEqual(report['toolCalls'], 0)

    @unittest.skipUnless(os.environ.get('DESK_SHEPHERD_CLAUDE_PROTOCOL_TESTS') == '1',
                         'opt-in real Claude Agent SDK protocol')
    def test_real_sdk_preserves_tool_pair_without_reexecution(self):
        sdk = os.environ['DESK_SHEPHERD_CLAUDE_SDK_ROOT']
        claude = os.environ.get('DESK_SHEPHERD_CLAUDE_BINARY') or shutil.which('claude')
        connector = ClaudeConnector(self.binding, 'nonce_claude',
            store_root=self.root / 'state', sdk_root=sdk,
            claude_binary=claude, node_binary=shutil.which('node'), ownership=self.coordinator.ownership)
        row = self.round([
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'seed-user'}]},
            {'type': 'function_call', 'call_id': 'call_1', 'name': 'Bash',
             'arguments': json.dumps({'command': 'never-run-history'})},
            {'type': 'function_call_output', 'call_id': 'call_1', 'output': 'historical-tool-result'},
            {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'seed-assistant'}]},
        ])
        connector.append([row], expected_revision='')
        probe = ROOT / 'desktron/tests/shepherd/claude-resume.mjs'
        completed = subprocess.run(['node', str(probe), sdk, connector.native.path, self.sid,
                                    str(self.cwd), claude], capture_output=True, text=True, timeout=120, encoding='utf-8')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report['hasToolResult'])
        # The fake upstream saw the historical tool_use, but the SDK did not execute it.
        self.assertEqual(report['toolCalls'], 1)

    @unittest.skipUnless(os.environ.get('DESK_SHEPHERD_CLAUDE_PROTOCOL_TESTS') == '1',
                         'opt-in real Claude Agent SDK protocol')
    def test_switch_runner_materializes_and_activates_claude_binding(self):
        sdk = os.environ['DESK_SHEPHERD_CLAUDE_SDK_ROOT']
        claude = os.environ.get('DESK_SHEPHERD_CLAUDE_BINARY') or shutil.which('claude')
        # A different engine starts active and commits one source-addressed public round.
        pi = self.coordinator.bind(self.cid, 'pi', 'native_pi', native_cwd=str(self.cwd),
            connector_version='pi-v1', binding_id='binding_pi', ownership_nonce='nonce_pi')
        lease = self.coordinator.acquire(self.cid, pi, ownership_nonce='nonce_pi')
        source_ref = {'kind': 'native_round', 'engine': 'pi', 'session_id': 'native_pi',
                      'entry_id': 'turn_1', 'source_key': 'pi:native_pi:turn_1'}
        native = {'native': 'seed'}
        public = [
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'seed-user'}]},
            {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'seed-assistant'}]},
        ]
        self.coordinator.commit(self.cid, lease['lease_id'], lease['generation'],
            source=source_ref, source_hash=digest(native), public_hash=digest(public),
            estimated_tokens=20)
        class Resolver:
            def resolve(self, source):
                self.last = source
                return {'source': native, 'public': public}
        connector = ClaudeConnector(self.binding, 'nonce_claude', store_root=self.root / 'state',
            sdk_root=sdk, claude_binary=claude, node_binary=shutil.which('node'),
            ownership=self.coordinator.ownership)
        next_lease = SwitchRunner(self.coordinator, ReadProjector(self.state, Resolver()), connector).switch(
            self.cid, self.binding_id, ownership_nonce='nonce_claude',
            lease_id=lease['lease_id'], generation=lease['generation'])
        state = self.coordinator.state(self.cid)
        self.assertEqual(next_lease['binding_id'], self.binding_id)
        self.assertEqual(state['bindings'][self.binding_id]['applied_offset'], 1)
        self.assertEqual(state['bindings'][self.binding_id]['native_revision'], connector.native.revision())
        probe = ROOT / 'desktron/tests/shepherd/claude-resume.mjs'
        completed = subprocess.run(['node', str(probe), sdk, connector.native.path, self.sid,
                                    str(self.cwd), claude], capture_output=True, text=True, timeout=120, encoding='utf-8')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report['hasSeedUser']); self.assertTrue(report['hasSeedAssistant'])

    @unittest.skipUnless(os.environ.get('DESK_SHEPHERD_CLAUDE_PROTOCOL_TESTS') == '1',
                         'opt-in real Claude Agent SDK protocol')
    def test_real_sdk_resumes_from_portable_checkpoint(self):
        sdk = os.environ['DESK_SHEPHERD_CLAUDE_SDK_ROOT']
        claude = os.environ.get('DESK_SHEPHERD_CLAUDE_BINARY') or shutil.which('claude')
        connector = ClaudeConnector(self.binding, 'nonce_claude',
            store_root=self.root / 'state', sdk_root=sdk,
            claude_binary=claude, node_binary=shutil.which('node'), ownership=self.coordinator.ownership)
        checkpoint = {'checkpoint_id': 'checkpoint_1', 'covers_through_offset': 8,
                      'estimated_tokens': 100, 'created_at': 10}
        connector.load_checkpoint({'summary': 'portable-checkpoint-marker'}, checkpoint=checkpoint,
                                  expected_revision='')
        probe = ROOT / 'desktron/tests/shepherd/claude-resume.mjs'
        completed = subprocess.run(['node', str(probe), sdk, connector.native.path, self.sid,
                                    str(self.cwd), claude], capture_output=True, text=True, timeout=120, encoding='utf-8')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertTrue(json.loads(completed.stdout)['hasCheckpoint'])


if __name__ == '__main__':
    unittest.main()
