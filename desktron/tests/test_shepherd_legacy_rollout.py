"""Legacy source readback must not depend on an installed/runnable native CLI."""
import copy
import json
import os
from _platform import symlink
from pathlib import Path
from unittest import mock

import pytest

from shepherd import Conflict
from shepherd.codex_codec import CODEC_VERSIONS, encode_round
from shepherd.codex_connector import CodexFamilyConnector
from shepherd.coordinator import digest
from shepherd.service import Service

SID = '11111111-2222-4333-8444-555555555555'
PUBLIC = [{'type': 'message', 'role': 'user',
           'content': [{'type': 'input_text', 'text': 'historical question'}]},
          {'type': 'message', 'role': 'assistant',
           'content': [{'type': 'output_text', 'text': 'historical answer'}]}]


def fixture(tmp_path, engine='codex'):
    root = tmp_path / 'state'
    service = Service(root, storage='unified')
    coord = service.coordinator
    coord.create('conversation')
    bid = coord.bind('conversation', engine, SID, native_cwd=str(tmp_path),
                     connector_version=CODEC_VERSIONS[engine], binding_id='legacy',
                     ownership_nonce='fixture')
    binding = coord.state('conversation')['bindings'][bid]
    connector = CodexFamilyConnector(binding, 'fixture', store_root=root,
                                    ownership=coord.ownership, binary='/missing/cli')
    home = Path(connector.home)
    base = home
    path = base / 'sessions/2026/09/18' / f'rollout-fixture-{SID}.jsonl'
    path.parent.mkdir(parents=True)
    records = [{'type': 'session_meta', 'payload': {
        'id': SID, 'cwd': str(tmp_path), 'cli_version': '0.154.0' if engine == 'codex' else '0.201.6'}}]
    records += [{'type': 'response_item', 'payload': item} for item in encode_round({
        'round_id': 'conversation:1', 'offset': 1, 'entry_hash': digest('seed'), 'public': PUBLIC})]
    save(path, records)
    ref = dict(kind='native_round', engine=engine, session_id=SID, entry_id='conversation:1',
               source_key=f'{engine}:{SID}:conversation:1')
    return service, connector, path, records, ref


def save(path, records):
    path.write_text(''.join(json.dumps(row) + '\n' for row in records))


@pytest.mark.parametrize('engine', ['codex'])
def test_read_without_cli_is_read_only_and_batch_identical(tmp_path, engine):
    _, c, path, _, ref = fixture(tmp_path, engine)
    before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
    with mock.patch.object(c, '_available', side_effect=AssertionError('no CLI check')), \
         mock.patch.object(c, '_open', side_effect=AssertionError('no native resume')), \
         mock.patch('subprocess.Popen', side_effect=AssertionError('no spawn')):
        single = c.resolve_source(ref)
        with mock.patch.object(c, '_source_snapshot', wraps=c._source_snapshot) as read:
            assert c.resolve_sources([ref, ref]) == [single, single]
            assert read.call_count == 1
    assert single['public'] == PUBLIC
    assert before == (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
    with pytest.raises(Conflict, match='Connector 不可用'):
        c.verify()  # Execution admission remains version gated.


@pytest.mark.parametrize('case', ['id', 'cwd', 'version', 'duplicate_meta', 'malformed',
                                  'truncated', 'unknown_record', 'nonobject', 'mutation', 'type_object'])
def test_invalid_sources_fail_closed(tmp_path, case):
    _, c, path, records, ref = fixture(tmp_path)
    if case == 'id': records[0]['payload']['id'] = 'other'
    if case == 'cwd': records[0]['payload']['cwd'] = '/other'
    if case == 'version': records[0]['payload']['cli_version'] = None
    if case == 'duplicate_meta': records.append(copy.deepcopy(records[0]))
    if case == 'unknown_record': records.append({'type': 'future_rollback', 'payload': {}})
    if case == 'nonobject': records.insert(0, [])
    if case == 'type_object': records[0]['type'] = {}
    if case == 'mutation': records.append({'type': 'history_mutation', 'payload': {'items': 'bad'}})
    save(path, records)
    if case == 'malformed': path.write_bytes(path.read_bytes() + b'{bad}\n')
    if case == 'truncated': path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(Conflict): c.resolve_source(ref)


@pytest.mark.parametrize('cwd', [None, '', '.', 0, False, []])
def test_missing_or_relative_cwd_never_matches_process_cwd(tmp_path, monkeypatch, cwd):
    _, c, path, records, ref = fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    records[0]['payload']['cwd'] = cwd
    save(path, records)
    with pytest.raises(Conflict, match='身份'):
        c.resolve_source(ref)
    records[0]['payload'].pop('cwd')
    save(path, records)
    with pytest.raises(Conflict, match='身份'):
        c.resolve_source(ref)


@pytest.mark.parametrize('case', ['duplicate', 'missing', 'file_link', 'directory_link', 'home_link', 'fifo'])
def test_source_discovery_fails_closed(tmp_path, case):
    _, c, path, _, ref = fixture(tmp_path)
    if case == 'duplicate':
        other = Path(c.home) / 'archived_sessions' / path.name
        other.parent.mkdir(); other.write_bytes(path.read_bytes())
    elif case == 'missing': path.unlink()
    elif case == 'file_link':
        other = tmp_path / 'outside.jsonl'; path.rename(other); symlink(other, path)
    elif case == 'directory_link':
        parent = path.parent; other = tmp_path / 'outside'; parent.rename(other); parent.symlink_to(other)
    elif case == 'home_link':
        home = Path(c.home); other = tmp_path / 'other-home'; home.rename(other); home.symlink_to(other)
    elif case == 'fifo':
        if not hasattr(os, 'mkfifo'):
            pytest.skip('FIFO 是 POSIX 专用文件类型')
        path.unlink(); os.mkfifo(path)
    with pytest.raises(Conflict): c.resolve_source(ref)


@pytest.mark.parametrize('version', ['0.154.0', '0.155.0', '9.999.0', 'unknown-release'])
def test_source_format_is_independent_of_runtime_recommendation(tmp_path, version):
    _, c, path, records, ref = fixture(tmp_path)
    expected = c.resolve_source(ref)
    records[0]['payload']['cli_version'] = version
    save(path, records)
    with mock.patch.object(c, 'availability', side_effect=AssertionError('no runtime probe')), \
         mock.patch('subprocess.Popen', side_effect=AssertionError('no process')):
        assert c.resolve_source(ref) == expected
    assert expected['source']['codec'] == 'codex-0.154.0-v1'


def test_wrong_reference_and_receipt_are_not_bypassed(tmp_path):
    _, c, _, _, ref = fixture(tmp_path)
    for invalid in (None, {**ref, 'session_id': 'wrong'}, {**ref, 'source_key': 'wrong'}):
        with pytest.raises(Conflict): c.resolve_source(invalid)
    c.receipts.write(c.binding, ref['entry_id'], {
        'turn_id': 'missing', 'terminal_state': 'completed', 'projection_version': 2})
    with pytest.raises(Conflict): c.resolve_source(ref)


def test_empty_unknown_receipt_uses_same_snapshot(tmp_path):
    _, c, path, records, ref = fixture(tmp_path)
    save(path, records[:1])
    c.receipts.write(c.binding, ref['entry_id'], {
        'turn_id': '', 'terminal_state': 'unknown', 'projection_version': 2,
        'start_seq': 1, 'end_seq': 1, 'native_revision': c._items_revision([]),
        'unfinished_tool_ids': []})
    with mock.patch.object(c, '_raw_lines', side_effect=AssertionError('must not reopen')):
        assert c.resolve_source(ref)['public'] == []
    save(path, records)
    with pytest.raises(Conflict): c.resolve_source(ref)


def test_pi_standard_checkpoint_and_max_full_history_need_no_codex_process(tmp_path):
    service, c, path, records, ref = fixture(tmp_path)
    resolved = c.resolve_source(ref)
    coord = service.coordinator
    lease = coord.acquire('conversation', 'legacy', ownership_nonce='fixture')
    committed = coord.commit('conversation', lease['lease_id'], lease['generation'], source=ref,
                             source_hash=digest(resolved['source']), public_hash=digest(resolved['public']),
                             estimated_tokens=20)
    # A model/provider switch on Pi can revisit every old source, not just Pi's own.
    with mock.patch.object(CodexFamilyConnector, '_open', side_effect=AssertionError('no resume')), \
         mock.patch('subprocess.Popen', side_effect=AssertionError('no subprocess')):
        service.switch('conversation', 'pi', tmp_path)
        pi = service.connector(service.active('conversation')['binding'])
        standard = 'example-gw/fixture__dev'; maximum = 'example-gw/fixture__max'
        key, _, rows = pi.context({'model': standard})
        assert len(rows) == 1
        pi.archive.checkpoint(key, turn_id='conversation:1', through_seq=1, payload={
            'native': [], 'source_hash': committed['source_hash']})
        assert pi.context({'model': standard})[2] == []
        assert pi.context({'model': maximum})[2] == rows
        # The existing committed source/public hash check is still authoritative.
        records[-1]['payload']['content'][0]['text'] = 'corrupted answer'
        save(path, records)
        with pytest.raises(Conflict, match='Hash 不匹配'): pi.context({'model': maximum})
