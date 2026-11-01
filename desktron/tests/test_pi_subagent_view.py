"""Public ownership, resident invocation liveness and child transcript projection."""
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktron" / "lib"))
_argv = sys.argv
sys.argv = ["ui_server.py", str(ROOT / "frontend"), "7999", "7998", str(ROOT)]
try:
    from engine_adapters.pi_subagent_view import PiSubagentViewMixin
    from engine_adapters.bus import EventBus
    from engine_adapters.shepherd import ShepherdAdapter
finally:
    sys.argv = _argv


def view_at(root):
    view = PiSubagentViewMixin()
    view.dir = str(root)
    view.index = {'parent': {}, 'other': {}}
    view._live = {'parent': {'proc': SimpleNamespace(pid=123, poll=lambda: None), '_pi_tool': {'call': 'part'}}}
    return view


def write_run(root, sid='parent', aid='child', **fields):
    directory = Path(root) / 'subagents' / sid / aid
    directory.mkdir(parents=True, exist_ok=True)
    row = dict(id=aid, sessionID=sid, pid=123, toolCallId='call', status='running', started=1, updated=2,
               childSessionId='private-native-id', kind='desk-scout', task='Inspect')
    row.update(fields)
    (directory / 'state.json').write_text(json.dumps(row))
    return directory


def test_running_requires_current_process_and_current_invocation(tmp_path):
    view = view_at(tmp_path)
    directory = write_run(tmp_path, workerPid=456)
    assert view.running_agents('parent') == 1
    assert 'childSessionId' not in view.subagents('parent')[0]
    assert 'pid' not in view.subagents('parent')[0]
    assert 'workerPid' not in view.subagents('parent')[0]
    view._live['parent']['_pi_tool'] = {'next-call': 'part2'}
    assert view.subagents('parent')[0]['outcome'] == 'gone'
    assert view.running_agents('parent') == 0
    assert json.loads((directory/'state.json').read_text())['status'] == 'running'
    view._live['parent']['_pi_tool'] = {'call': 'part'}
    view._live['parent']['proc'].pid = 456
    assert view.subagents('parent')[0]['outcome'] == 'gone'


def test_sidecar_can_arrive_just_before_tool_lifecycle_mapping(tmp_path):
    view = view_at(tmp_path)
    view._live['parent']['_pi_tool'] = {}
    directory = write_run(tmp_path, started=int(time.time() * 1000))
    assert view.subagents('parent')[0]['status'] == 'running'
    row = json.loads((directory/'state.json').read_text())
    row['started'] = 1
    (directory/'state.json').write_text(json.dumps(row))
    assert view.subagents('parent')[0]['outcome'] == 'gone'


def test_upstream_sync_fanout_tracks_parent_call_not_child_pid(tmp_path):
    view = view_at(tmp_path)
    write_run(tmp_path, runtime='pi-subagents-v2', mode='sync', pid=999, workerPid=1000)
    assert view.subagents('parent')[0]['status'] == 'running'
    view._live['parent']['_pi_tool'] = {}
    assert view.subagents('parent')[0]['outcome'] == 'gone'


def test_upstream_async_outlives_parent_but_keeps_terminal_outcome(tmp_path):
    view = view_at(tmp_path)
    write_run(tmp_path, runtime='pi-subagents-v2', mode='async', pid=999, workerPid=1000)
    view._live = {}
    assert view.subagents('parent')[0]['status'] == 'running'
    write_run(tmp_path, runtime='pi-subagents-v2', mode='async', status='done', outcome='failed',
              error='账号额度不足', error_code='QUOTA', error_kind='quota', upstream_code=402)
    row = view.subagents('parent')[0]
    assert row['outcome'] == 'failed'
    assert row['error_code'] == 'QUOTA'
    assert row['error_kind'] == 'quota'
    assert row['upstream_code'] == 402


def test_legacy_node_pid_is_live_when_it_is_supervisor_child(tmp_path, monkeypatch):
    view = view_at(tmp_path)
    write_run(tmp_path, pid=456)
    seen = []
    monkeypatch.setattr(view, '_child_pid', lambda parent, child: seen.append((parent, child)) or True)
    assert view.subagents('parent')[0]['status'] == 'running'
    assert seen == [(123, 456)]
    monkeypatch.setattr(view, '_child_pid', lambda _parent, _child: False)
    assert view.subagents('parent')[0]['outcome'] == 'gone'


def test_terminal_and_new_sidecars_do_not_spawn_legacy_pid_probe(tmp_path, monkeypatch):
    view = view_at(tmp_path)
    write_run(tmp_path, aid='done', status='done', outcome='completed', pid=456)
    write_run(tmp_path, aid='new', workerPid=456)
    monkeypatch.setattr(view, '_child_pid', lambda *_: (_ for _ in ()).throw(AssertionError('unexpected ps')))
    assert [row['status'] for row in view.subagents('parent')] == ['done', 'running']


def test_foreign_removed_and_invalid_parent_have_no_records(tmp_path):
    view = view_at(tmp_path)
    directory = write_run(tmp_path)
    (directory/'events.jsonl').write_text('{"type":"message_end","message":{"content":[{"type":"text","text":"private"}]}}\n')
    assert view.subagent_log('parent', 'child')
    assert view.subagent_log('other', 'child') == []
    assert view.subagent_log('parent', '../child') == []
    (directory.parent/'alias').symlink_to(directory, target_is_directory=True)
    assert view.subagent_log('parent', 'alias') == []
    del view.index['parent']
    assert view.subagents('parent') == []
    assert view.subagent_log('parent', 'child') == []


def test_refresh_preserves_terminal_status_and_projects_tools(tmp_path):
    directory = write_run(tmp_path, status='done', outcome='failed')
    events = [
        {'type':'message_start'},
        {'type':'message_update','assistantMessageEvent':{'type':'thinking_delta','contentIndex':0,'delta':'reason'}},
        {'type':'message_update','assistantMessageEvent':{'type':'text_delta','contentIndex':1,'delta':'partial'}},
        {'type':'message_end','message':{'role':'assistant','content':[
            {'type':'thinking','thinking':'reason'}, {'type':'text','text':'complete'},
            {'type':'toolCall','id':'read1','name':'read','arguments':{'path':'fixture'}}]}},
        {'type':'tool_execution_start','toolCallId':'read1','toolName':'read','args':{'path':'fixture'}},
        {'type':'tool_execution_end','toolCallId':'read1','isError':True,
         'result':{'content':[{'type':'text','text':'missing fixture'}]}},
    ]
    (directory/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events)+'{"type":')
    view = view_at(tmp_path); view._live = {}
    assert view.subagents('parent')[0]['outcome'] == 'failed'
    parts = [p for m in view.subagent_log('parent','child') for p in m['parts']]
    assert [(p['type'],p.get('text')) for p in parts[:2]] == [('reasoning','reason'),('text','complete')]
    assert len(parts) == 3
    assert parts[2]['state']['status'] == 'error'
    assert parts[2]['state']['output'] == 'missing fixture'


def test_partial_stream_is_visible_before_message_end(tmp_path):
    directory = write_run(tmp_path)
    (directory/'events.jsonl').write_text(json.dumps({'type':'message_update',
        'assistantMessageEvent':{'type':'text_delta','contentIndex':0,'delta':'still running'}})+'\n')
    assert view_at(tmp_path).subagent_log('parent','child')[0]['parts'][0]['text'] == 'still running'


def test_bounded_log_tail_and_corrupt_metadata(tmp_path, monkeypatch):
    from engine_adapters import pi_subagent_view
    monkeypatch.setattr(pi_subagent_view, 'MAX_LOG_BYTES', 512)
    directory = write_run(tmp_path)
    events = [{'type':'message_end','message':{'role':'assistant','content':[
        {'type':'text','text':str(i)*20}]}} for i in range(20)]
    (directory/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
    view = view_at(tmp_path)
    rows = view.subagent_log('parent','child')
    assert rows[0]['parts'][0]['text'].startswith('记录较长')
    assert rows[-1]['parts'][0]['text'] == '19'*20
    corrupt = write_run(tmp_path, aid='broken')
    (corrupt/'state.json').write_text('[]')
    assert [row['id'] for row in view.subagents('parent')] == ['child']


def test_subagent_log_accepts_smaller_tail_window(tmp_path, monkeypatch):
    from engine_adapters import pi_subagent_view
    monkeypatch.setattr(pi_subagent_view, 'MAX_LOG_BYTES', 1024)
    directory = write_run(tmp_path)
    events = [{'type':'message_end','message':{'role':'assistant','content':[
        {'type':'text','text':str(i)}]}} for i in range(20)]
    (directory/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
    rows = view_at(tmp_path).subagent_log('parent', 'child', limit_bytes=128)
    assert '128 B' in rows[0]['parts'][0]['text']
    assert rows[-1]['parts'][0]['text'] == '19'
    assert all(p.get('text') != '0' for m in rows for p in m['parts'])


def test_subagent_log_accepts_message_limit(tmp_path):
    directory = write_run(tmp_path)
    events = [{'type':'message_end','message':{'role':'assistant','content':[
        {'type':'text','text':str(i)}]}} for i in range(8)]
    (directory/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
    rows = view_at(tmp_path).subagent_log('parent', 'child', limit=3)
    assert [row['parts'][0]['text'] for row in rows] == ['5', '6', '7']


def test_shepherd_delegates_public_child_records_across_reopen(tmp_path):
    home = tmp_path/'data'; home.mkdir()
    work = tmp_path/'work'; work.mkdir()
    adapter = ShepherdAdapter(home, work, EventBus())
    try:
        sid = adapter.create_session(opts={'active_engine':'pi'})['id']
        directory = write_run(adapter.dir, sid, status='done', outcome='completed')
        (directory/'events.jsonl').write_text(json.dumps({'type':'message_end','message':{
            'role':'assistant','content':[{'type':'text','text':'retained report'}]}})+'\n')
        assert adapter.subagents(sid)[0]['outcome'] == 'completed'
    finally:
        adapter.shutdown()
    reopened = ShepherdAdapter(home, work, EventBus())
    try:
        assert reopened.subagent_log(sid,'child')[0]['parts'][0]['text'] == 'retained report'
        assert reopened.running_agents(sid) == 0
        reopened.delete_session(sid)
        assert reopened.subagent_log(sid,'child') == []
    finally:
        reopened.shutdown()
