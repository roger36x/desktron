"""Batch readers preserve source hashes, ownership and cancellation boundaries."""
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from shepherd import Coordinator, Store, ReadProjector, SourceResolver, Conflict
from shepherd.coordinator import digest
from shepherd.pi_connector import PiConnector
from shepherd.pi_codec import CODEC_VERSION as PI_CODEC
from shepherd.claude_connector import ClaudeConnector
from shepherd.claude_codec import CODEC_VERSION as CLAUDE_CODEC, encode_rounds
from shepherd.codex_connector import CodexFamilyConnector
from shepherd.codex_codec import CODEC_VERSIONS, encode_round
from shepherd.source_codec import codex_public


def public(text='answer'):
    return [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'question'}]},
            {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}]


def ref(engine='pi', sid='11111111-2222-4333-8444-555555555555', seq=1):
    entry = f'conversation:{seq}'
    return dict(kind='native_round', engine=engine, session_id=sid, entry_id=entry,
                source_key=f'{engine}:{sid}:{entry}')


def rounds(n=3):
    return [dict(round_id=f'conversation:{i}', offset=i, entry_hash=digest(i),
                 committed_at=10, public=public(str(i))) for i in range(1, n + 1)]


def connector(tmp_path, engine):
    store = Store(tmp_path / 'state'); coord = Coordinator(store); coord.create('conversation')
    codec = PI_CODEC if engine == 'pi' else CLAUDE_CODEC
    bid = coord.bind('conversation', engine, ref()['session_id'], native_cwd=str(tmp_path),
                     connector_version=codec, binding_id='binding', ownership_nonce='nonce')
    binding = coord.state('conversation')['bindings'][bid]
    cls = PiConnector if engine == 'pi' else ClaudeConnector
    return cls(binding, 'nonce', store_root=store.root, ownership=coord.ownership)


class BatchSource:
    engine = 'pi'
    native_session_id = ref()['session_id']

    def __init__(self):
        self.reads = []
        self.changed = False
        self.on_read = lambda: None

    def resolve_source(self, source):
        i = int(source['entry_id'].split(':')[-1])
        return {'source': {'n': i, 'changed': self.changed}, 'public': public(str(i))}

    def resolve_sources(self, sources):
        self.reads.append(len(sources)); self.on_read()
        return [self.resolve_source(source) for source in sources]


def projection(tmp_path, n=35, tokens=1):
    source = BatchSource(); store = Store(tmp_path / 'state'); coord = Coordinator(store)
    coord.create('conversation')
    bid = coord.bind('conversation', 'pi', source.native_session_id, native_cwd=str(tmp_path),
                     connector_version=PI_CODEC, binding_id='binding', ownership_nonce='nonce')
    lease = coord.acquire('conversation', bid, ownership_nonce='nonce')
    for i in range(1, n + 1):
        value = source.resolve_source(ref(seq=i))
        coord.commit('conversation', bid, lease['generation'], source=ref(seq=i),
                     source_hash=digest(value['source']), public_hash=digest(value['public']),
                     estimated_tokens=tokens)
    return ReadProjector(store, SourceResolver([source])), source


def test_batches_preserve_order_and_detect_changed_sources_between_calls(tmp_path):
    projector, source = projection(tmp_path)
    got = projector.project('conversation')
    assert [row['seq'] for row in got] == list(range(1, 36))
    assert source.reads == [32, 3]
    got[0]['public'][0]['content'][0]['text'] = 'consumer edit'
    assert projector.project('conversation', 1, 1)[0]['public'] == public('1')
    source.changed = True
    with pytest.raises(Conflict, match='Hash'):
        projector.project('conversation')


def test_token_budget_splits_batches_and_cancel_stops_after_first_read(tmp_path):
    projector, source = projection(tmp_path, n=5, tokens=200000)
    projector.project('conversation')
    assert source.reads == [1] * 5
    source.reads.clear(); cancelled = [False]
    source.on_read = lambda: cancelled.__setitem__(0, True)
    with pytest.raises(Conflict, match='取消'):
        projector.project('conversation', cancelled=lambda: cancelled[0])
    assert source.reads == [1]


def test_legacy_shared_values_remain_isolated_and_wrong_batches_fail():
    shared = {'source': {}, 'public': public()}
    legacy = SimpleNamespace(engine='pi', native_session_id=ref()['session_id'],
                             resolve_source=lambda source: shared)
    resolver = SourceResolver([legacy])
    result = resolver.resolve_batch([(ref(), None)])[0]
    result['public'][0]['content'][0]['text'] = 'edit'
    assert shared['public'] == public()
    with pytest.raises(Conflict):
        resolver.resolve_batch([(ref(sid='unowned'), None)])
    legacy.resolve_sources = lambda refs: []
    with pytest.raises(ValueError, match='长度'):
        resolver.resolve_batch([(ref(), None)])


def test_post_fence_read_still_rejects_tampering(tmp_path):
    from shepherd import SwitchRunner
    projector, source = projection(tmp_path, n=2)
    coord = Coordinator(projector.store)
    target = coord.bind('conversation', 'claude', 'target', native_cwd=str(tmp_path),
                        connector_version='test', binding_id='target', ownership_nonce='target_nonce')
    binding = coord.state('conversation')['bindings'][target]
    writer = SimpleNamespace(**{key: binding[key] for key in
        ('binding_id', 'engine', 'native_session_id', 'native_cwd', 'connector_version', 'ownership_nonce')},
        preflight=lambda *a, **k: None, append=mock.Mock())
    before = coord.state('conversation')['lease']
    prepare = coord.prepare_switch
    def tamper(*args, **kwargs):
        pending = prepare(*args, **kwargs); source.changed = True; return pending
    with mock.patch.object(coord, 'prepare_switch', side_effect=tamper), pytest.raises(Conflict, match='Hash'):
        SwitchRunner(coord, projector, writer).switch('conversation', target,
            ownership_nonce='target_nonce', lease_id=before['lease_id'], generation=before['generation'])
    writer.append.assert_not_called()
    assert coord.state('conversation')['pending_switch']


def test_pi_batch_uses_one_offline_read_and_matches_markers_and_receipts(tmp_path):
    p = connector(tmp_path, 'pi')
    if not PiConnector.availability()['available']:
        pytest.skip('pinned Pi SDK unavailable')
    p.append(rounds(), '')
    refs = [ref(seq=i) for i in range(1, 4)]
    expected = [p.resolve_source(source) for source in refs]
    entries = expected[0]['source']['entries']
    # Pi import has start + 2 messages + commit; production receipt selects the
    # same range to exercise the locator branch alongside marker-only sources.
    p.receipts.write(p.binding, refs[0]['entry_id'],
                     {'start_seq': 0, 'end_seq': len(entries), 'projection_version': 1,
                      'terminal_state': 'completed', 'native_revision': p.revision()})
    from shepherd import pi_history
    with mock.patch.object(pi_history, 'read_entries', wraps=pi_history.read_entries) as read, \
         mock.patch.object(p, '_run', side_effect=AssertionError('history must not start SDK')), \
         mock.patch.object(p, 'availability', side_effect=AssertionError('history must not probe')):
        actual = p.resolve_sources(refs)
    assert actual == expected
    assert read.call_count == 1
    with pytest.raises(Conflict):
        p.resolve_sources([ref(seq=999)])
    # Every new batch reads current bytes; altered markers cannot hit a cache.
    path = Path(p.path)
    text = path.read_text().replace(rounds()[1]['entry_hash'], '0' * 64, 1)
    path.write_text(text)
    with pytest.raises(Conflict):
        p.resolve_sources([refs[1]])


def test_claude_batch_loads_once_and_keeps_receipt_prefix_validation(tmp_path):
    c = connector(tmp_path, 'claude')
    from shepherd.claude_codec import SDK_VERSION
    sdk = tmp_path / 'sdk'; sdk.mkdir()
    (sdk / 'package.json').write_text(json.dumps({'version': SDK_VERSION}))
    c.sdk_root = str(sdk)
    c.native.append(encode_rounds(c.binding, rounds()), '')
    refs = [ref(engine='claude', seq=i) for i in range(1, 4)]
    expected = [c.resolve_source(source) for source in refs]
    # Production receipts refer to distinct prefixes, in deliberately reversed order.
    entries = c.native.load()
    revision = ''
    import hashlib
    from shepherd.claude_store import canonical
    for i, entry in enumerate(entries, 1):
        revision = hashlib.sha256((revision + '\0' + canonical(entry)).encode()).hexdigest()
        if i % 2 == 0:
            c.receipts.write(c.binding, refs[i // 2 - 1]['entry_id'],
                             {'start_seq': i - 2, 'end_seq': i, 'projection_version': 1,
                              'terminal_state': 'completed', 'terminal_uuid': entry['uuid'],
                              'native_revision': revision})
    with mock.patch.object(c.native, 'load', wraps=c.native.load) as load:
        got = c.resolve_sources(list(reversed(refs)))
    assert got == list(reversed(expected))
    assert load.call_count == 1
    receipt = c.receipts.read(c.binding, refs[0]['entry_id'])
    # Exercise the shared projection verifier directly with an invalid prefix.
    receipt['locator']['native_revision'] = '0' * 64
    with mock.patch.object(c.receipts, 'read', return_value=receipt), pytest.raises(Conflict, match='Revision'):
        c.resolve_sources([refs[0]])


@pytest.mark.parametrize('engine', ['codex'])
def test_codex_family_batch_parses_once_and_matches_single_reads(tmp_path, engine):
    c = object.__new__(CodexFamilyConnector)
    c.engine = engine; c.native_session_id = ref()['session_id']; c.connector_version = CODEC_VERSIONS[engine]
    c.binding = {}; c.ownership_nonce = 'nonce'; c.ownership = SimpleNamespace(verify=lambda *a: None)
    c._available = lambda: None; c._safe_home = lambda: None
    connection = mock.Mock(); c._open = mock.Mock(return_value=connection)
    path = tmp_path / 'rollout.jsonl'
    c._source_snapshot = lambda: (str(path), path.read_bytes().splitlines(keepends=True))
    native = [item for row in rounds() for item in encode_round(row)]
    # A real Turn locator coexists with imported marker batches.
    turn_item = {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'native'}],
                 'internal_chat_message_metadata_passthrough': {'turn_id': 'real_turn'}}
    path.write_text(''.join(json.dumps({'type': 'response_item', 'payload': item}) + '\n'
                            for item in native + [turn_item]))
    c.receipts = SimpleNamespace(read=lambda binding, entry, required=False:
        {'locator': {'turn_id': 'real_turn', 'projection_version': 2, 'terminal_state': 'completed'}}
        if entry == 'conversation:4' else None)
    refs = [ref(engine=engine, seq=i) for i in range(1, 5)]
    expected = [c.resolve_source(source) for source in refs]
    c._open.reset_mock(); connection.close.reset_mock()
    with mock.patch.object(c, '_authoritative_items', wraps=c._authoritative_items) as parse:
        actual = c.resolve_sources(refs)
    assert actual == expected
    assert parse.call_count == 1
    c._open.assert_not_called()
    connection.close.assert_not_called()
    with pytest.raises(Conflict):
        c.resolve_sources([ref(engine=engine, seq=999)])


def test_codecs_do_not_share_mutable_input_or_mutate_native_metadata():
    native = [{'type': 'function_call_output', 'call_id': 'c', 'output': [{'type': 'text', 'text': 'value'}],
               'id': 'keep', 'internal_chat_message_metadata_passthrough': {'turn_id': 'keep'}}]
    snapshot = copy.deepcopy(native)
    projected = codex_public(native)
    projected[0]['output'][0]['text'] = 'edit'
    assert native == snapshot
    row = rounds(1)[0]
    row['public'] = [{'type': 'function_call', 'call_id': 'c', 'name': 'read', 'arguments': {'paths': ['a']}},
                     {'type': 'function_call_output', 'call_id': 'c', 'output': 'ok'}]
    snapshot = copy.deepcopy(row)
    encoded = encode_rounds({'native_session_id': 's', 'native_cwd': '/tmp', 'binding_id': 'b'}, [row])
    encoded[0]['message']['content'][0]['input']['paths'].append('b')
    assert row == snapshot


def test_normalization_only_replaces_metadata_without_json_roundtrip():
    native = {'type': 'message', 'content': [{'type': 'output_text', 'text': 'x' * 100000}],
              'internal_chat_message_metadata_passthrough': {'turn_id': 'turn', 'extra': 'native'}}
    with mock.patch('shepherd.codex_connector.json.dumps', side_effect=AssertionError('unexpected serialization')):
        normalized = CodexFamilyConnector._normalized_item(native)
    assert normalized['content'] is native['content']
    assert normalized['internal_chat_message_metadata_passthrough'] == {'turn_id': 'turn'}
    assert native['internal_chat_message_metadata_passthrough']['extra'] == 'native'
