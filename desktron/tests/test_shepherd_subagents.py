"""Shepherd /agents uses the same native parsers as ordinary engines, scoped by Binding."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron/lib'))
from engine_adapters import CodexAdapter, ShepherdAdapter
from frontend_i18n_js import NODE_PRELUDE

PARENT = '11111111-1111-4111-8111-111111111111'
CHILD = '22222222-2222-4222-8222-222222222222'
SID = 'adp_fixture'


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')


def rollout(home, *, completed=False):
    sessions = home / 'sessions'
    parent = sessions / ('rollout-test-' + PARENT + '.jsonl')
    child = sessions / ('rollout-test-' + CHILD + '.jsonl')
    write(parent, [{'type': 'session_meta', 'payload': {'id': PARENT, 'source': 'cli',
                  'timestamp': '2026-09-17T00:00:00Z'}}])
    rows = [{'type': 'session_meta', 'payload': {'id': CHILD, 'timestamp': '2026-09-17T00:01:00Z',
             'source': {'subagent': {'thread_spawn': {'parent_thread_id': PARENT, 'agent_role': 'reviewer'}}}}},
            {'type': 'response_item', 'timestamp': '2026-09-17T00:01:00Z',
             'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Review fixture code'}]}},
            {'type': 'response_item', 'timestamp': '2026-09-17T00:01:01Z',
             'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'child evidence'}]}}]
    if completed:
        rows.append({'type': 'event_msg', 'timestamp': '2026-09-17T00:01:02Z', 'payload': {'type': 'task_complete'}})
    write(child, rows)
    return parent, child


@pytest.fixture
def facade(tmp_path):
    ad = object.__new__(ShepherdAdapter)
    ad.shepherd_root = str(tmp_path / 'shepherd')
    ad.dir = str(tmp_path / 'adapter/shepherd')
    ad.index = {SID: {'selected_engine': 'codex'}}
    ad._live = {SID: {'producer_engine': 'codex'}}
    state = {'conversation_id': SID, 'bindings': {}, 'active_binding_id': ''}
    ad.service = SimpleNamespace(store=SimpleNamespace(load_raw=lambda sid: state if sid == SID else None))
    bus = SimpleNamespace(publish=lambda _: None)
    workers = {engine: cls(str(tmp_path / 'adapters'), str(tmp_path), bus)
               for engine, cls in [('codex', CodexAdapter)]}
    for worker in workers.values():
        worker.index = ad.index
        worker._live = ad._live
    pi = {'id': 'pi-child', 'task': 'Pi evidence', 'started': 1, 'status': 'running'}
    workers['pi'] = SimpleNamespace(subagents=lambda sid: [dict(pi)],
        subagent_log=lambda sid, aid, **kw: [{'pi': True}] if aid == 'pi-child' else [])
    ad._worker = lambda engine: workers[engine]
    return ad, state, workers


def binding(ad, state, engine='codex', unified=True, suffix='1'):
    bid = 'binding_' + engine + suffix
    row = {'binding_id': bid, 'conversation_id': SID, 'engine': engine,
           'native_session_id': 'synthetic-native-id' if unified else PARENT,
           'native_cwd': '/fixture', 'connector_version': 'unified-v1:test' if unified else 'legacy'}
    state['bindings'][bid] = row
    state['active_binding_id'] = bid
    ad._live[SID]['producer_engine'] = engine
    home = (Path(ad.shepherd_root) / 'resident' / 'codex-family' / engine / bid / 'home' if unified
            else Path(ad.shepherd_root) / 'native' / engine / bid / 'home')
    return bid, home


@pytest.mark.parametrize('engine', ['codex'])
@pytest.mark.parametrize('unified', [True, False])
def test_native_list_and_detail_parity_without_public_native_id(facade, engine, unified):
    ad, state, workers = facade
    bid, home = binding(ad, state, engine, unified)
    rollout(home)
    original = workers[engine]
    before = (original.ROLLOUT_DIRS, copy.deepcopy(original.index), original._live)
    ordinary = copy.copy(original)
    ordinary.ROLLOUT_DIRS = (str(home / 'sessions'), str(home / 'archived_sessions'))
    ordinary.index = {SID: {'engine_sid': PARENT}}
    expected = ordinary.subagents(SID)
    rows = [row for row in ad.subagents(SID) if row.get('engine') == engine]
    assert len(rows) == 1
    row = dict(rows[0]); public_id = row.pop('id'); row.pop('engine')
    assert row == {key: value for key, value in expected[0].items() if key != 'id'}
    assert public_id == f'shepherd:{engine}:{bid}:{CHILD}'
    assert ad.subagent_log(SID, public_id) == ordinary.subagent_log(SID, CHILD)
    limited = ad.subagent_log(SID, public_id, limit=1, limit_bytes=1024)
    assert len(limited) == 1 and 'child evidence' in json.dumps(limited)
    assert 'child evidence' in json.dumps(ad.subagent_log(SID, public_id))
    assert ad.running_agents(SID) == 2  # native + Pi
    assert original.ROLLOUT_DIRS == before[0] and original.index == before[1]
    assert original._live is before[2] and 'engine_sid' not in ad.index[SID]


def test_switch_restart_and_colliding_native_ids_keep_producer_routes(facade):
    ad, state, _ = facade
    for engine in ('codex',):
        _, home = binding(ad, state, engine)
        rollout(home, completed=True)
    ad._live = {}  # cold view; no connector/CLI is constructed to list children
    rows = ad.subagents(SID)
    native = [row for row in rows if row.get('engine')]
    assert len(native) == 1
    assert all(row['status'] == 'done' and row['outcome'] == 'completed' for row in native)
    assert all(ad.subagent_log(SID, row['id']) for row in native)
    assert ad.subagent_log(SID, 'pi-child') == [{'pi': True}]
    assert ad.running_agents(SID) == 1


@pytest.mark.parametrize('engine', ['codex'])
def test_native_home_disposal_keeps_child_only_details(facade, engine):
    import shutil
    ad, state, _ = facade
    bid, home = binding(ad, state, engine)
    parent, _ = rollout(home, completed=True)
    with parent.open('a', encoding='utf-8') as f:
        f.write(json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                     'content': [{'type': 'input_text', 'text': 'private parent context'}]}})+'\n')
    before = [row for row in ad.subagents(SID) if row.get('engine') == engine]
    messages = ad.subagent_log(SID, before[0]['id'])
    ad._archive_native_children(SID, bid)
    assert len(ad.subagents(SID)) == 2  # live and persisted projection deduplicated
    shutil.rmtree(home.parent)  # real resident cleanup, not merely dropping _live
    ad._live = {}
    after = [row for row in ad.subagents(SID) if row.get('engine') == engine]
    assert after == before
    assert ad.subagent_log(SID, before[0]['id']) == messages
    archive = ad._child_archive_path(SID, bid).read_text(encoding='utf-8')
    assert 'private parent context' not in archive and 'messages' not in archive
    log = (ad._child_archive_path(SID, bid).with_suffix('') / (CHILD + '.json')).read_text(encoding='utf-8')
    assert 'private parent context' not in log and 'child evidence' in log


def test_disposal_bypasses_empty_poll_cache_for_fast_children(facade):
    import shutil
    ad, state, _ = facade
    bid, home = binding(ad, state)
    _, child = rollout(home, completed=True)
    body = child.read_text(encoding='utf-8')
    child.unlink()
    assert len(ad.subagents(SID)) == 1  # cache contains no Codex child
    child.write_text(body, encoding='utf-8')
    ad._archive_native_children(SID, bid)
    shutil.rmtree(home.parent)
    assert len(ad.subagents(SID)) == 2


def test_disposal_marks_nonterminal_child_gone_and_deleted_sid_is_not_recreated(facade):
    import shutil
    ad, state, _ = facade
    bid, home = binding(ad, state)
    rollout(home)
    ad._archive_native_children(SID, bid)
    shutil.rmtree(home.parent)
    native = [row for row in ad.subagents(SID) if row.get('engine')]
    assert native[0]['status'] == 'done' and native[0]['outcome'] == 'gone'
    archive = Path(ad.shepherd_root) / 'archive' / SID
    shutil.rmtree(archive)
    ad.index.clear()
    ad._archive_native_children(SID, bid)
    assert not archive.exists()


def test_eviction_archive_and_physical_deletion_share_a_fence(facade):
    import shutil
    import threading
    from shepherd.physical_delete import mark_deleted
    ad, state, _ = facade
    bid, home = binding(ad, state)
    rollout(home, completed=True)
    entered, release, deleting, deleted = [threading.Event() for _ in range(4)]
    errors = []
    original = ad._archive_native_children_locked
    def blocked(sid, binding_id):
        entered.set()
        assert release.wait(5)
        original(sid, binding_id)
    ad._archive_native_children_locked = blocked
    def archive():
        try:
            ad._archive_native_children(SID, bid)
        except Exception as exc:
            errors.append(exc)
    def delete():
        try:
            deleting.set()
            mark_deleted(ad.shepherd_root, SID)
            shutil.rmtree(Path(ad.shepherd_root) / 'archive' / SID)
            deleted.set()
        except Exception as exc:
            errors.append(exc)
    first = threading.Thread(target=archive); first.start()
    assert entered.wait(5)
    second = threading.Thread(target=delete); second.start()
    try:
        assert deleting.wait(5)
        assert not deleted.wait(.1), 'delete must not race the archive writer'
    finally:
        release.set()
        first.join(5); second.join(5)
    assert not errors and deleted.is_set()
    # A stale in-memory public index cannot reopen the durable deletion fence.
    ad._archive_native_children(SID, bid)
    assert not (Path(ad.shepherd_root) / 'archive' / SID).exists()


def test_other_conversation_binding_and_ordinary_home_never_leak(facade, tmp_path):
    ad, state, workers = facade
    _, home = binding(ad, state)
    rollout(home)
    other_bid, other_home = binding(ad, state, suffix='foreign')
    rollout(other_home)
    state['bindings'][other_bid]['conversation_id'] = 'adp_other'
    ordinary_home = tmp_path / 'ordinary'
    rollout(ordinary_home)
    workers['codex'].ROLLOUT_DIRS = (str(ordinary_home),)
    rows = ad.subagents(SID)
    assert len(rows) == 2
    assert not ad.subagents('adp_other')
    assert not ad.subagent_log('adp_other', rows[-1]['id'])
    assert not ad.subagent_log(SID, f'shepherd:codex:{other_bid}:{CHILD}')
    assert not ad.subagent_log(SID, f'shepherd:claude:binding_codex1:{CHILD}')
    assert not ad.subagent_log(SID, 'shepherd:codex:../../escape:../secret')


def test_symlink_home_and_cached_child_are_not_followed(facade, tmp_path):
    ad, state, _ = facade
    bid, home = binding(ad, state)
    _, child = rollout(home)
    public_id = f'shepherd:codex:{bid}:{CHILD}'
    assert ad.subagent_log(SID, public_id)
    outside = tmp_path / 'outside.jsonl'
    outside.write_text(child.read_text(encoding='utf-8'), encoding='utf-8')
    from _platform import symlink
    child.unlink(); symlink(outside, child)
    assert len(ad.subagents(SID)) == 1
    assert not ad.subagent_log(SID, public_id)
    import shutil
    shutil.rmtree(home)
    home.symlink_to(tmp_path, target_is_directory=True)
    assert len(ad.subagents(SID)) == 1


def test_claude_is_explicitly_disabled_and_pi_remains_public_scoped(facade):
    ad, state, _ = facade
    state['bindings']['claude'] = {'binding_id': 'claude', 'conversation_id': SID, 'engine': 'claude'}
    assert ad.subagents(SID) == [{'id': 'pi-child', 'task': 'Pi evidence', 'started': 1, 'status': 'running'}]
    for name in ('claude_runner.mjs', 'claude_resident_runner.mjs'):
        source = (ROOT / 'desktron/lib/shepherd' / name).read_text(encoding='utf-8')
        assert re_disallowed(source)


def test_public_rows_make_bottom_entry_visible_for_each_supported_engine(facade):
    import re
    import subprocess
    ad, state, _ = facade
    samples = []
    for engine in ('codex',):
        _, home = binding(ad, state, engine)
        rollout(home)
        samples.append([row for row in ad.subagents(SID) if row.get('engine') == engine])
    samples.append([row for row in ad.subagents(SID) if row['id'] == 'pi-child'])
    source = (ROOT / 'frontend/app-chat-tasksbar.js').read_text(encoding='utf-8')
    funcs = '\n'.join(re.search(r'(?:async )?function ' + name + r'\([^\n]*\) \{[\s\S]+?\n}', source).group(0)
                      for name in ('hsAgentsSync', 'hsAgentArm', 'hsAgentLive'))
    js = '''
const assert=require('node:assert/strict');
const HS_AG_ARM=60000, HS_AG_FRESH=90000;
const hsAgentsWatch=()=>{}, hsAgentPick=()=>{};
let rows=[];
const api=async()=>rows;
'''+funcs+'\nconst samples='+json.dumps(samples)+''';
(async()=>{
 for(const sample of samples){
  const label={},btn={hidden:true,classList:{toggle(){}},querySelector:()=>label};
  const v={sid:'adp_fixture',el:{querySelector:()=>btn},busy:true,statusT0:Date.now(),
    turns:[{parts:new Map([['spawn',{type:'tool',tool:'task',state:{status:'running'}}]])}]};
  rows=[];await hsAgentsSync(v);assert.ok(v.agentWatchUntil>Date.now());
  rows=sample;await hsAgentsSync(v);
  assert.equal(btn.hidden,false);assert.equal(label.textContent,'1 个代理运行中');
  assert.equal(v.agents[0].id,sample[0].id);
  v.busy=false;v.statusT0=0;await hsAgentsSync(v);assert.equal(btn.hidden,false);
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run(['node', '-e', NODE_PRELUDE + js], capture_output=True, text=True, timeout=15, encoding='utf-8')
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('engine', ['codex'])
def test_real_runner_cleanup_callback_keeps_discovered_children(facade, tmp_path, monkeypatch, engine):
    from unittest.mock import Mock
    from shepherd.service import Service
    from shepherd.harness import ExecutionHarness
    from shepherd.unified_runners import UnifiedRunner
    from shepherd.codex_connector import CodexFamilyConnector
    import codex_appserver
    ad, _state, _ = facade
    monkeypatch.setattr(CodexFamilyConnector, 'availability', lambda *a, **kw: {'available': True})
    monkeypatch.setattr(CodexFamilyConnector, '_seed_private_config', lambda *a: None)
    ad.service = Service(ad.shepherd_root, storage='unified')
    ad.service.create(SID, engine, tmp_path)
    active = ad.service.active(SID)
    bid = active['binding']['binding_id']
    runner = UnifiedRunner(ad.service.connector(active['binding']), options={'_context_window': 400000})
    runner.on_native_context_drop = lambda: ad._archive_native_children(SID, bid)
    ad._live[SID]['producer_engine'] = engine
    def native(*args, **kw):
        import os
        # This protocol fake runs in-process: register and release that exact
        # identity so the real scratch collector does not infer an unknown spawn.
        process=os.getpid()
        kw['on_process'](process)
        runner.execution.release_process(process)
        kw['on_session_create']()
        home = Path(args[1]['CODEX_HOME'])
        rollout(home, completed=True)
        assert any(row.get('engine') == engine for row in ad.subagents(SID))
        kw['on_connection'](Mock(proc=None))
        kw['on_turn_submitting'](PARENT)
        kw['on_turn_started'](PARENT, 'native-turn')
        kw['on_notification']('rawResponseItem/completed', {'turnId':'native-turn',
            'item':{'type':'message','role':'assistant',
                    'content':[{'type':'output_text','text':'fixture answer'}]}})
        kw['on_notification']('turn/completed', {'threadId': PARENT,
                               'turn': {'id': 'native-turn', 'status': 'completed'}})
        kw['on_session_drop']()
        assert not home.exists()
        return PARENT
    monkeypatch.setattr(codex_appserver, 'run_turn', native)
    lease = active['lease']
    result = ExecutionHarness(ad.service.coordinator, runner.connector, runner).run(
        SID, lease_id=lease['lease_id'], generation=lease['generation'], prompt='inspect fixture',
        input_public=[{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'inspect fixture'}]}])
    assert result['terminal_state'] == 'completed'
    rows = [row for row in ad.subagents(SID) if row.get('engine') == engine]
    assert len(rows) == 1 and 'child evidence' in json.dumps(ad.subagent_log(SID, rows[0]['id']))
    # Physical deletion removes these derived child artifacts with the public archive.
    from shepherd.physical_delete import mark_deleted, purge
    state = ad.service.store.load_raw(SID)
    mark_deleted(ad.shepherd_root, SID)
    purge(ad.shepherd_root, SID, state)
    assert not (Path(ad.shepherd_root) / 'archive' / SID).exists()


def native_cleanup(owner, home):
    """Run the complete production callback, including its post-archive work."""
    import ast
    from shepherd.unified_runners import _clear_private_tree
    source = (ROOT / 'desktron/lib/shepherd/unified_runners.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    clean = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'clean_home')
    namespace = {'self': owner, 'home': home, '_clear_private_tree': _clear_private_tree,
                 '__package__': 'shepherd'}
    exec(compile(ast.Module(body=[clean], type_ignores=[]), '<native-cleanup>', 'exec'), namespace)
    return namespace['clean_home']


@pytest.mark.parametrize('unsealed,preserve_existing', [(False, False), (True, False), (False, True)])
def test_cleanup_callback_archives_before_deleting_and_retains_home_on_error(tmp_path, unsealed, preserve_existing):
    home = tmp_path / 'resident' / 'binding' / 'home'
    home.mkdir(parents=True)
    events = []
    binding = {'binding_id': 'fixture', 'conversation_id': SID}
    def retain_native_home(actual_binding, actual_home):
        assert actual_binding is binding and actual_home == home
        assert events == [True] and home.exists()
        events.append('retained')
    archive = SimpleNamespace(has_unsealed_turn=lambda actual_binding: unsealed,
                              retain_native_home=retain_native_home)
    owner = SimpleNamespace(connector=SimpleNamespace(store_root=str(tmp_path), archive=archive),
                            binding=binding,
                            on_native_context_drop=lambda: events.append(home.exists()))
    clean = native_cleanup(owner, home)
    clean(preserve_existing=preserve_existing)
    assert events == ([True, 'retained'] if unsealed or preserve_existing else [True])
    assert not home.exists()
    home.mkdir(parents=True)
    def failed_archive():
        raise OSError('disk full')
    owner.on_native_context_drop = failed_archive
    with pytest.raises(OSError):
        clean(preserve_existing=preserve_existing)
    assert home.exists()


def test_sidebar_and_quiet_gate_resolve_shepherd_facade(monkeypatch):
    import term
    formal = SimpleNamespace(running_agents=lambda sid: 2)
    trial = SimpleNamespace(running_agents=lambda sid: 3)
    ordinary = SimpleNamespace(running_agents=lambda sid: 1)
    monkeypatch.setattr(term, '_shepherd', lambda: formal)
    monkeypatch.setattr(term, '_shepherd_trial', lambda: trial)
    monkeypatch.setattr(term, '_adapter', lambda engine: ordinary)
    rows = [{'engine': 'codex', 'id': SID, 'shepherd_mode': True, 'busy': True},
            {'engine': 'pi', 'id': 'adp_trial_fixture', 'shepherd_mode': True, 'shepherd_trial': True, 'resident': True},
            {'engine': 'codex', 'id': CHILD, 'resident': True}]
    targets = term._agents_targets(rows)
    assert targets == [('shepherd', SID), ('shepherd_trial', 'adp_trial_fixture'), ('codex', CHILD)]
    monkeypatch.setattr(term, '_AGENTS_RUN', {})
    term._agents_worker(targets)
    assert [term._AGENTS_RUN[sid][0] for _, sid in targets] == [2, 3, 1]
    # The update-idle gate must consume the same facade routing, not native adapters.
    assert 'term._agents_adapter(eng).running_agents(sid)' in (ROOT / 'desktron/lib/self_update.py').read_text(encoding='utf-8')


@pytest.mark.parametrize('trial', [False, True])
def test_real_idle_shepherd_row_blocks_quiet_update(facade, monkeypatch, trial):
    import term
    import self_update
    import engine
    ad, state, workers = facade
    _, home = binding(ad, state)
    rollout(home)
    ad._live.clear()  # Parent finished; its native child is still fresh/running.
    workers['pi'].subagents = lambda sid: []
    ad.list_sessions = lambda: [{'id': SID, 'engine': 'codex', 'title': 'fixture'}]
    ad.turn_state = lambda sid: {'state': 'ready', 'generation': 1, 'completed_generation': 1}
    monkeypatch.setattr(term, '_shepherd_trial' if trial else '_shepherd', lambda: ad)
    rows = term._shepherd_space_sessions([], {}, trial=trial)
    assert len(rows) == 1 and not rows[0]['busy'] and not rows[0]['alive']
    assert 'resident' not in rows[0]
    assert term._agents_targets(rows) == [('shepherd_trial' if trial else 'shepherd', SID)]
    monkeypatch.setattr(term, '_AGENTS_RUN', {})
    term._agents_worker(term._agents_targets(rows))
    assert term._AGENTS_RUN[SID][0] == 1
    # Exercise the real quiet gate, without prepopulating row['agents'].
    monkeypatch.setattr(self_update, '_ACTIVE_WORK', 0)
    monkeypatch.setattr(term, '_term_list', lambda: [])
    monkeypatch.setattr(term, '_term_persisted', lambda: rows)
    monkeypatch.setattr(engine, '_adapters', {})
    monkeypatch.setitem(sys.modules, 'app_control.runtime', SimpleNamespace(
        runtime=lambda: SimpleNamespace(pending_all=lambda _: [])))
    monkeypatch.setitem(sys.modules, 'arrangement.scheduler', SimpleNamespace(has_running=lambda: False))
    assert not self_update._work_is_quiet()
    rollout(home, completed=True)
    assert self_update._work_is_quiet()


@pytest.mark.parametrize('engine', ['codex'])
@pytest.mark.parametrize('failure', ['head_read', 'body_read', 'second_read', 'bad_head', 'bad_body', 'parse_none'])
def test_real_read_failures_preserve_home_during_cleanup(facade, monkeypatch, engine, failure):
    import builtins
    from unittest.mock import Mock
    ad, state, workers = facade
    bid, home = binding(ad, state, engine)
    parent, child = rollout(home, completed=True)
    # Warm the forgiving UI caches first: cleanup must not trust them.
    assert len(ad.subagents(SID)) == 2
    if failure == 'bad_head':
        child.write_bytes(b'{bad metadata}\n' + child.read_bytes().split(b'\n', 1)[1])
    elif failure == 'bad_body':
        with child.open('ab') as stream:
            stream.write(b'{truncated child output')
    before = {path: path.read_bytes() for path in (parent, child)}
    if failure in ('head_read', 'body_read', 'second_read'):
        original_open = builtins.open
        child_opens = 0
        class BrokenReader:
            def __init__(self, stream):
                self.stream = stream
                self.calls = 0
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def readline(self):
                self.calls += 1
                if self.calls > 1:
                    raise OSError('injected body read failure')
                return self.stream.readline()
            def tell(self): return self.stream.tell()
        def broken_open(path, *args, **kwargs):
            nonlocal child_opens
            if str(path) == str(child):
                child_opens += 1
                if failure == 'head_read' or (failure == 'second_read' and child_opens > 1):
                    raise PermissionError('injected read denial')
                if failure == 'body_read':
                    return BrokenReader(original_open(path, *args, **kwargs))
            return original_open(path, *args, **kwargs)
        monkeypatch.setattr(builtins, 'open', broken_open)
    elif failure == 'parse_none':
        monkeypatch.setattr(workers[engine], '_group_rollout', lambda *args, **kw: None)
    archive = Mock()
    owner = SimpleNamespace(connector=SimpleNamespace(store_root=ad.shepherd_root, archive=archive),
                            binding=state['bindings'][bid],
                            on_native_context_drop=lambda: ad._archive_native_children(SID, bid))
    with pytest.raises((OSError, ValueError)):
        native_cleanup(owner, home)()
    assert home.exists() and {path: path.read_bytes() for path in before} == before
    archive.has_unsealed_turn.assert_not_called()
    archive.retain_native_home.assert_not_called()


@pytest.mark.parametrize('engine', ['codex'])
def test_archive_metadata_never_uses_forgiving_second_reads(facade, monkeypatch, engine):
    ad, state, workers = facade
    bid, home = binding(ad, state, engine)
    rollout(home, completed=True)
    def forbidden(*args, **kwargs):
        pytest.fail('destructive archive must not reuse forgiving native reads')
    for name in ('_subagent_tail', '_subagent_task', '_spawn_index', '_rollout_head'):
        monkeypatch.setattr(workers[engine], name, forbidden)
    ad._archive_native_children(SID, bid)
    rows = list(ad._archived_children(SID))
    assert len(rows) == 1
    assert rows[0]['row']['outcome'] == 'completed'
    assert rows[0]['row']['task'] == 'Review fixture code'


@pytest.mark.parametrize('pause_at', ['after_children', 'before_db', 'before_retain'])
def test_complete_cleanup_callback_cannot_resurrect_purged_archive(facade, monkeypatch, pause_at):
    import threading
    from shepherd.physical_delete import mark_deleted, purge
    from shepherd.unified_archive import UnifiedArchive
    ad, state, _ = facade
    bid, home = binding(ad, state)
    state['bindings'][bid]['ownership_nonce'] = 'fixture'
    rollout(home, completed=True)
    archive = UnifiedArchive(ad.shepherd_root, SID)
    entered, resume, deleting, deleted = [threading.Event() for _ in range(4)]
    errors = []
    def pause():
        entered.set()
        assert resume.wait(5)
    def children():
        ad._archive_native_children(SID, bid)
        if pause_at == 'after_children':
            pause()
    if pause_at != 'after_children':
        name = 'has_unsealed_turn' if pause_at == 'before_db' else 'retain_native_home'
        original = getattr(archive, name)
        def blocked(*args):
            pause()
            return original(*args)
        monkeypatch.setattr(archive, name, blocked)
    owner = SimpleNamespace(connector=SimpleNamespace(store_root=ad.shepherd_root, archive=archive),
                            binding=state['bindings'][bid], on_native_context_drop=children)
    clean = native_cleanup(owner, home)
    def cleanup():
        try:
            clean(preserve_existing=pause_at == 'before_retain')
        except Exception as exc:
            errors.append(exc)
    def delete():
        try:
            deleting.set()
            mark_deleted(ad.shepherd_root, SID)
            purge(ad.shepherd_root, SID, state)
            deleted.set()
        except Exception as exc:
            errors.append(exc)
    first = threading.Thread(target=cleanup); first.start()
    assert entered.wait(5)
    second = threading.Thread(target=delete); second.start()
    try:
        assert deleting.wait(5)
        if pause_at == 'after_children':
            assert deleted.wait(5), 'delete may win between the two guarded phases'
        else:
            assert not deleted.wait(.1), 'all archive DB/retention work must hold the fence'
    finally:
        resume.set()
        first.join(5); second.join(5)
    assert not first.is_alive() and not second.is_alive() and not errors
    assert deleted.is_set() and not archive.directory.exists() and not home.exists()
    # Repeat the full late callback while its in-memory index/binding are stale.
    clean(preserve_existing=True)
    assert not archive.directory.exists() and not home.exists()


def re_disallowed(source):
    import re
    policy = (ROOT / 'desktron/lib/shepherd/claude_sdk_options.mjs').read_text(encoding='utf-8')
    return (re.search(r'disallowedTools:[^\n]*claudeDisallowedTools', source)
            and re.search(r"claudeDisallowedTools = \['Agent', 'Task', 'Monitor'\]", policy))
