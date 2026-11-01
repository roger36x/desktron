"""Peer working context is admitted without changing the source archive."""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from engine_adapters.recap import _RecapRun
from shepherd import recap
from shepherd.context_budget import estimate_context_tokens
from shepherd.context_fallback import text_receipt
from shepherd.pi_connector import pi_sdk_root
from shepherd.projector import ReadProjector
from shepherd.store import Conflict
from test_shepherd_context_admission import setup


def native_entries(c):
    return [
        {'type': 'session', 'version': 3, 'id': c.native_session_id,
         'cwd': c.native_cwd, 'timestamp': '2026-01-01T00:00:00.000Z'},
        {'type': 'message', 'id': 'aa001122', 'parentId': None,
         'timestamp': '2026-01-01T00:00:00.000Z',
         'message': {'role': 'user', 'content': 'verified working summary', 'timestamp': 0}},
    ]


def save_checkpoint(service, c, options=None):
    key, _, _ = c.context(options or {'model': 'fixture'})
    row = service.coordinator.state('admit')['rounds'][-1]
    checkpoint = {'native': native_entries(c), 'source_hash': row['source_hash']}
    c.archive.checkpoint(key, turn_id=row['round_id'], through_seq=1, payload=checkpoint)
    return key


def archive_dump(c):
    with c.archive.db() as db:
        return list(db.iterdump())


def test_peer_plan_uses_checkpoint_without_projecting_covered_history(tmp_path):
    service, c, _ = setup(tmp_path, 'pi')
    save_checkpoint(service, c)
    before = archive_dump(c)
    original = ReadProjector.project
    offsets = []
    def project(self, sid, start_offset=1, **kwargs):
        offsets.append(start_offset)
        return original(self, sid, start_offset, **kwargs)
    with mock.patch.object(ReadProjector, 'project', project):
        _, checkpoint, rounds = c.context({'model': 'fixture', '_context_window': 32000,
            '_context_admission': True}, read_only=True)
    assert checkpoint['through_seq'] == 1 and rounds == []
    assert offsets == []  # The checkpoint covers everything; even an empty projection is unnecessary.
    assert archive_dump(c) == before


def test_read_only_migrated_checkpoint_is_not_cached(tmp_path):
    service, c, _ = setup(tmp_path, 'pi')
    row = service.coordinator.state('admit')['rounds'][-1]
    migrated = {'through_seq': 1, 'turn_id': row['round_id'],
                'payload': {'native': native_entries(c), 'source_hash': row['source_hash']}}
    before = archive_dump(c)
    with mock.patch('shepherd.migrated_context.migrated_context', return_value=migrated), \
         mock.patch.object(c.archive, 'checkpoint', wraps=c.archive.checkpoint) as checkpoint:
        _, result, rounds = c.context({'model': 'fixture'}, read_only=True)
    assert result == migrated and not rounds
    assert all(not call.kwargs for call in checkpoint.call_args_list)
    assert archive_dump(c) == before


def test_peer_plan_bounds_context_and_rejects_oversized_question(tmp_path):
    service, c, _ = setup(tmp_path, 'pi')
    before = archive_dump(c)
    _, checkpoint, rounds = c.context({'model': 'fixture', '_context_window': 12000,
        '_context_admission': True, '_current_tokens': 1000}, read_only=True)
    assert text_receipt(checkpoint) and not rounds
    assert c.admission['history_tokens'] <= 12000 - 8192 - 1000
    assert archive_dump(c) == before  # Includes any portable-context derivation.
    with pytest.raises(Conflict, match='当前输入'):
        c.context({'model': 'fixture', '_context_window': 12000,
                   '_current_tokens': 12000, '_context_admission': True}, read_only=True)


def test_read_only_portable_derivation_does_not_cache(tmp_path):
    from test_shepherd_portable_checkpoint import pi_checkpoint
    service, c, _ = setup(tmp_path, 'pi')
    key, _, _ = c.context({'model': 'fixture'})
    native = pi_checkpoint()['payload']['native']
    native[0]['id'] = c.native_session_id
    row = service.coordinator.state('admit')['rounds'][-1]
    c.archive.checkpoint(key, turn_id=row['round_id'], through_seq=1,
        payload={'native': native, 'source_hash': row['source_hash']})
    before = archive_dump(c)
    _, checkpoint, _ = c.context({'model': 'other', '_context_admission': True}, read_only=True)
    assert text_receipt(checkpoint)
    assert 'portable summary' in json.dumps(checkpoint)
    assert not c.archive.portable_candidates()
    assert archive_dump(c) == before


def test_checkpoint_source_tampering_fails_closed(tmp_path):
    service, c, _ = setup(tmp_path, 'pi')
    key = save_checkpoint(service, c)
    checkpoint = c.archive.checkpoint(key)
    checkpoint['payload']['source_hash'] = 'wrong'
    with mock.patch.object(c.archive, 'checkpoint', return_value=checkpoint), \
         pytest.raises(Conflict, match='source hash'):
        c.context({'model': 'fixture'}, read_only=True)


def test_cancelled_planner_does_not_continue_projection(tmp_path):
    _, c, _ = setup(tmp_path, 'pi')
    with pytest.raises(Conflict, match='取消'):
        c.context({'model': 'fixture'}, read_only=True, cancelled=lambda: True)


class Worker:
    PI_SAFETY = 'safety-fixture'
    def options_catalog(self): return {}
    def system_body(self, opts): return 'system-fixture'
    def recap_generate(self, sid, run, snap): return 'answer'


@pytest.mark.parametrize('cancel_at', ['plan', 'prepare', 'timeout', 'timeout_cancel', 'none'])
def test_prepared_pi_branch_budget_and_final_cancellation_fence(tmp_path, cancel_at):
    _, c, _ = setup(tmp_path, 'pi')
    run = _RecapRun('admit', 1)
    worker = Worker()
    ad = SimpleNamespace(_worker=lambda engine: worker, _recap_context_ready=lambda *args: True)
    snap = {'opts': {'model': 'fixture', '_context_window': 32000, '_grounding': 'ground-fixture'},
            'source': 'peer', '_prompt': 'question-fixture'}
    def plan(opts, **kwargs):
        assert kwargs['read_only'] is True and callable(kwargs['cancelled'])
        assert opts['_current_tokens'] > estimate_context_tokens('question-fixture')
        assert '无法确认' in opts['_history_hint']
        if cancel_at == 'plan': run.cancel()
        return 'key', None, []
    process = mock.Mock(timed_out=cancel_at.startswith('timeout'))
    def events():
        if cancel_at.startswith('timeout'):
            prepare.call_args.kwargs['on_timeout']()
            if cancel_at == 'timeout_cancel': run.mark_cancelled('目标会话发送了新消息')
            raise Conflict('private native timeout details')
        if cancel_at == 'prepare': run.cancel()
        yield {'type': 'prepared', 'fallbackReceipt': None}
    process.events.side_effect = events
    with mock.patch.object(c, 'context', side_effect=plan), \
         mock.patch('engine_adapters.recap._engine_env', return_value={}), \
         mock.patch.object(recap, 'NativeProcess', return_value=process) as prepare, \
         mock.patch.object(Worker, 'recap_generate', return_value='answer') as model:
        if cancel_at.startswith('timeout'):
            with pytest.raises(RuntimeError, match='Pi 上下文准备超时 120s') as error:
                recap._generate_unified_pi(ad, c, c.binding, 'admit', run, snap)
            assert error.value.code == 'preparation_timeout'
            result = ''
        else:
            result = recap._generate_unified_pi(ad, c, c.binding, 'admit', run, snap)
    assert result == ('answer' if cancel_at == 'none' else '')
    assert model.call_count == (cancel_at == 'none')
    assert prepare.call_count == (cancel_at != 'plan')


def test_prepare_timeout_is_recorded_before_late_reader_and_user_cancel(tmp_path):
    import os
    import sys
    from shepherd.runner_process import NativeProcess
    run = _RecapRun('fixture', 0)
    process = NativeProcess([sys.executable, '-c', 'import time; time.sleep(5)'],
        cwd=str(tmp_path), env={'PATH': os.environ['PATH']}, timeout=.1, hard_timeout=.2,
        cancel_frame={'type': 'cancel'}, on_process=run.bind,
        on_timeout=lambda: run.mark_expired('Pi 上下文准备超时 120s', code='preparation_timeout'))
    try:
        assert run.cancelled.wait(3)
        # The reader has not consumed process.events() yet. This later user
        # operation must not win over the watchdog's already-recorded timeout.
        run.mark_cancelled('目标会话发送了新消息，隔离问询已取消')
        with pytest.raises(Conflict): list(process.events())
        assert run.failure_code == 'preparation_timeout' and not run.cancel_reason
        with pytest.raises(RuntimeError, match='Pi 上下文准备超时'):
            run.check_cancelled()
    finally:
        process.close()
        run.unbind(process.proc)


@pytest.mark.native
@pytest.mark.parametrize('kind', ['checkpoint', 'compaction', 'fallback'])
def test_pi_sdk_prepared_store_roundtrips_working_context(tmp_path, kind):
    fallback = kind == 'fallback'
    sdk = pi_sdk_root()
    if not sdk or not shutil.which('node'): pytest.skip('installed Pi SDK required')
    (tmp_path / 'source').mkdir()
    service, c, _ = setup(tmp_path / 'source', 'pi')
    if fallback:
        _, checkpoint, _ = c.context({'model': 'fixture', '_context_admission': True}, read_only=True)
        from shepherd.context_fallback import native_items
        payload = {'id': c.native_session_id, 'entries': None,
                   'messages': native_items(checkpoint), 'fallbackReceipt': text_receipt(checkpoint)}
    else:
        payload = {'id': c.native_session_id, 'entries': native_entries(c),
                   'messages': [{'role': 'user', 'content': 'current-tail', 'timestamp': 1}]}
    if kind == 'compaction':
        from test_shepherd_portable_checkpoint import pi_checkpoint
        native = pi_checkpoint()['payload']['native']
        native[0] = native_entries(c)[0]
        payload['entries'] = native
    request = tmp_path / 'request.json'
    request.write_text(json.dumps(payload))
    output = tmp_path / 'source.jsonl'
    result = subprocess.run(['node', str(Path(recap.__file__).with_name('pi_recap_context.mjs')),
                             sdk, str(request), str(output)], cwd=c.native_cwd,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['type'] == 'prepared'
    assert output.stat().st_mode & 0o777 == 0o600
    if not fallback:
        assert 'history history history' not in output.read_text()
        assert ('portable summary' if kind == 'compaction' else 'verified working summary') in output.read_text()
        assert 'current-tail' in output.read_text()


@pytest.mark.native
def test_real_pi_ask_replies_from_checkpoint_without_source_mutation(tmp_path, monkeypatch):
    import os
    import peer_mailbox
    from engine_adapters.bus import EventBus
    from engine_adapters.shepherd import ShepherdAdapter
    from shepherd.harness import ExecutionHarness
    from test_shepherd_unified_storage import JournalRunner
    from test_shepherd_image_execution import ImageModel
    if not pi_sdk_root() or not shutil.which('pi'): pytest.skip('installed Pi required')
    work = tmp_path / 'work'; work.mkdir()
    agent = tmp_path / 'agent'; agent.mkdir()
    home = tmp_path / 'home'; home.mkdir()
    monkeypatch.setenv('DESKTRON_DATA_HOME', str(tmp_path / 'data'))
    server = ImageModel()
    try:
        (agent / 'models.json').write_text(json.dumps({'providers': {'fixture': {
            'baseUrl': server.url, 'api': 'anthropic-messages', 'apiKey': 'fixture',
            'models': [{'id': 'fixture', 'name': 'Fixture', 'reasoning': False, 'input': ['text'],
                        'contextWindow': 32000, 'maxTokens': 1024}]}}}))
        env = {'PATH': os.environ['PATH'], 'HOME': str(home), 'PI_CODING_AGENT_DIR': str(agent),
               'PI_OFFLINE': '1', 'PI_TELEMETRY': '0'}
        ad = ShepherdAdapter(str(tmp_path / 'adapter'), str(work), EventBus())
        sid = ad.create_session(opts={'active_engine': 'pi'})['id']
        active = ad.service.active(sid); c = ad._connector(active['binding']); lease = active['lease']
        public = [{'type': 'message', 'role': 'assistant', 'content': [
            {'type': 'output_text', 'text': 'old-tool-evidence-' * 20000}]}]
        ExecutionHarness(ad.service.coordinator, c, JournalRunner(c, public)).run(sid,
            lease_id=lease['lease_id'], generation=lease['generation'], prompt='old input')
        opts = {'model': 'fixture', 'provider': 'fixture', 'effort': 'off', '_context_window': 32000}
        key, _, _ = c.context(opts)
        row = ad.service.coordinator.state(sid)['rounds'][-1]
        c.archive.checkpoint(key, turn_id=row['round_id'], through_seq=1,
            payload={'native': native_entries(c), 'source_hash': row['source_hash']})
        source = archive_dump(c); state = ad.service.active(sid)
        worker = ad._worker('pi')
        rid = peer_mailbox.create('fixture-source', sid)['id']
        with mock.patch('engine_adapters.recap._engine_env', return_value=env), \
             mock.patch('engine_adapters.pi.provider_extension_args', return_value=[]), \
             mock.patch.object(worker, 'options_catalog', return_value={}), \
             mock.patch.object(worker, 'system_body', return_value='Fixture safety: never call tools.'), \
             mock.patch.object(ad, 'grounding', return_value={}), \
             mock.patch.object(ad, 'running_agents', return_value=0):
            assert ad._run_peer_serial(sid, 'Summarize the known decision.', opts, rid)
        result = peer_mailbox.get(rid)
        assert result['status'] == 'replied' and result['reply'] == 'image-fixture-ok'
        assert len(server.requests) == 1
        sent = json.dumps(server.requests[0])
        assert 'verified working summary' in sent and 'old-tool-evidence' not in sent
        assert not server.requests[0].get('tools')
        assert len(sent) < 12000
        assert archive_dump(c) == source and ad.service.active(sid) == state
        assert not list(agent.rglob('sessions/**/*.jsonl'))
    finally:
        server.close()
