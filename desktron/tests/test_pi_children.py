"""Desk only projects upstream evidence; lifecycle and queue boundary regressions."""
import json
import threading
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from pi_children import PiChildren,_save

@pytest.fixture
def bridge(tmp_path,monkeypatch):
    adapter=SimpleNamespace(dir=str(tmp_path),index={'parent':{'domain':'test'}})
    adapter.session_options=lambda sid,fallback:{}
    adapter.cancel_unsent_delivery=lambda sid,rid:{'status':'cancelled'}
    monkeypatch.setitem(sys.modules,'workbench',SimpleNamespace(_wb_state_read=lambda:{'engine_opts':{}}))
    result=PiChildren(adapter)
    yield result
    result.close()


def launch(bridge,rid='pi_call',mode='async',state='running'):
    p=bridge._path('parent',rid).parent;p.mkdir(parents=True)
    meta={'id':rid,'sessionID':'parent','mode':mode,'status':'running','upstreamId':'native-'+rid,
          'tasks':[{'agent':'desk-scout','task':'fixture'}]}
    _save(p/'launch.json',meta)
    native=bridge.upstream_root/'async-subagent-runs'/meta['upstreamId'];native.mkdir(parents=True)
    _save(native/'status.json',{'runId':meta['upstreamId'],'state':state,'steps':[
        {'agent':'desk-scout','status':'complete' if state=='complete' else state,'finalOutput':'fixture result'}]})
    return meta,native


def test_async_start_is_not_completion_and_terminal_delivered_once(bridge):
    meta,native=launch(bridge)
    calls=[]
    bridge.owner.prompt_async=lambda *args,**kwargs:calls.append(kwargs) or {'status':'queued'}
    bridge.owner.request_status=lambda *args:{'status':'finished'}
    bridge.refresh();assert not calls
    value=json.loads((native/'status.json').read_text());value['state']='complete';_save(native/'status.json',value)
    bridge.refresh();bridge.refresh();bridge.refresh()
    assert len(calls)==1 and calls[0]['client_request_id']=='pi-child-pi_call'
    assert bridge._read('parent','pi_call')['delivery']=='delivered'


def test_restart_reloads_upstream_run_without_spawn_or_replay(bridge):
    launch(bridge,state='complete')
    restored=PiChildren(bridge.adapter)
    calls=[];restored.owner.prompt_async=lambda *a,**k:calls.append(k) or {'status':'finished'}
    restored.owner.request_status=lambda *a:{'status':'finished'}
    restored.refresh();restored.refresh()
    assert len(calls)==1
    assert 'fixture result' in restored._read('parent','pi_call')['result']
    restored.close()


def test_sync_result_is_inline(bridge):
    meta,_=launch(bridge,mode='sync');meta.update(status='done',outcome='completed',details={'results':[{'finalOutput':'fixture result'}]})
    _save(bridge._path('parent','pi_call').parent/'launch.json',meta)
    bridge.owner.prompt_async=lambda *a,**k:pytest.fail('sync result must not notify')
    bridge.refresh();assert bridge._read('parent','pi_call')['delivery']=='inline'


def test_switch_away_retains_result_and_rejection_never_replays(bridge):
    launch(bridge,state='complete');bridge.owner.selected_engine=lambda sid:'claude'
    bridge.refresh();row=bridge._read('parent','pi_call')
    assert row['delivery']=='pending' and '不是 Pi' in row['delivery_error']
    bridge.owner.selected_engine=lambda sid:'pi'
    bridge.owner.prompt_async=lambda *a,**k:{'status':'rejected','reason':'selection changed'}
    bridge.refresh();assert bridge._read('parent','pi_call')['delivery']=='blocked'
    bridge.owner.prompt_async=lambda *a,**k:pytest.fail('blocked result cannot replay')
    bridge.refresh()


def test_queue_error_retries_same_request_identity(bridge):
    launch(bridge,state='complete')
    bridge.owner.prompt_async=lambda *a,**k:(_ for _ in ()).throw(RuntimeError('queue closed'))
    bridge.refresh();assert bridge._read('parent','pi_call')['delivery']=='pending'
    calls=[];bridge.owner.prompt_async=lambda *a,**k:calls.append(k) or {'status':'queued'}
    bridge.refresh();assert calls[0]['client_request_id']=='pi-child-pi_call'
    assert 'fixture result' in bridge._read('parent','pi_call')['result']


def test_cancel_uses_scoped_upstream_inbox_and_never_signals_pid(bridge):
    _,native=launch(bridge)
    bridge.cancel('parent')
    requests=list((native/'control'/'stop-requests').glob('*.json'))
    assert len(requests)==1 and json.loads(requests[0].read_text(encoding='utf-8'))['type']=='stop'
    value=json.loads((native/'status.json').read_text());value['state']='stopped';_save(native/'status.json',value)
    bridge.owner.prompt_async=lambda *a,**k:pytest.fail('user stop must not resume parent')
    bridge.refresh();assert bridge._read('parent','pi_call')['delivery']=='suppressed'


def test_cancel_after_restart_does_not_require_a_new_parent(bridge):
    from pi_children import cancel_session
    _,native=launch(bridge)
    cancel_session(bridge.owner.dir,'parent')
    assert len(list((native/'control'/'stop-requests').glob('*.json')))==1


def test_user_interrupt_suppresses_completed_backlog_instead_of_cascading(bridge):
    launch(bridge,state='complete')
    bridge.owner.prompt_async=lambda *a,**k:(_ for _ in ()).throw(RuntimeError('parent busy'))
    bridge.refresh()
    assert bridge._read('parent','pi_call')['delivery']=='pending'
    bridge.cancel('parent',suppress_pending=True)
    assert bridge._read('parent','pi_call')['delivery']=='suppressed'
    bridge.owner.prompt_async=lambda *a,**k:pytest.fail('late result must stay suppressed')
    bridge.refresh();bridge.refresh()


def test_user_interrupt_fence_wins_when_native_completion_races_stop(bridge):
    _,native=launch(bridge)
    bridge.cancel('parent',suppress_pending=True)
    value=json.loads((native/'status.json').read_text());value['state']='complete';_save(native/'status.json',value)
    bridge.owner.prompt_async=lambda *a,**k:pytest.fail('completion after stop must not resume parent')
    bridge.refresh()
    row=bridge._read('parent','pi_call')
    assert row['status']=='done' and row['outcome']=='completed'
    assert row['delivery']=='suppressed' and row['suppression_reason']=='user_interrupt'


def test_interrupt_before_upstream_identity_is_replayed_as_stop(bridge):
    meta,native=launch(bridge)
    path=bridge._path('parent','pi_call').parent/'launch.json'
    meta.pop('upstreamId');_save(path,meta)
    bridge.cancel('parent',suppress_pending=True)
    assert not list((native/'control'/'stop-requests').glob('*.json'))
    meta['upstreamId']='native-pi_call';_save(path,meta)
    bridge.refresh()
    assert len(list((native/'control'/'stop-requests').glob('*.json')))==1
    bridge.refresh()
    assert len(list((native/'control'/'stop-requests').glob('*.json')))==1


def test_delivery_marks_notification_turn_and_removes_recursive_instruction(bridge):
    launch(bridge,state='complete')
    calls=[]
    bridge.owner.prompt_async=lambda sid,text,opts,**kwargs:calls.append((text,opts,kwargs)) or {'status':'queued'}
    bridge.refresh()
    text,opts,kwargs=calls[0]
    assert opts['_subagent_delivery'] is True
    assert '不要仅因本通知再次启动异步子代理' in text
    assert '请继续原任务' not in text
    assert kwargs['client_request_id']=='pi-child-pi_call'


def test_completed_backlog_is_delivered_in_one_bounded_batch(bridge):
    launch(bridge,'pi_one',state='complete');launch(bridge,'pi_two',state='complete')
    calls=[]
    bridge.owner.prompt_async=lambda sid,text,opts,**kwargs:calls.append((text,kwargs)) or {'status':'queued'}
    bridge.refresh()
    assert len(calls)==1
    text,kwargs=calls[0]
    assert 'Pi 子代理批量结果（2 批）' in text
    assert 'pi_one' in text and 'pi_two' in text
    assert kwargs['client_request_id'].startswith('pi-child-group-')
    rows=[bridge._read('parent',rid) for rid in ('pi_one','pi_two')]
    assert {r['delivery'] for r in rows}=={'queued'}
    assert len({r['delivery_request_id'] for r in rows})==1


def test_batch_identity_is_persisted_before_queue_claim_and_excludes_late_member(bridge):
    launch(bridge,'pi_one',state='complete');launch(bridge,'pi_two',state='complete')
    first=[]
    def uncertain_accept(_sid,text,_opts,**kwargs):
        first.append((text,kwargs['client_request_id']))
        raise RuntimeError('accepted but reply lost')
    bridge.owner.prompt_async=uncertain_accept
    bridge.refresh()
    rows=[bridge._read('parent',rid) for rid in ('pi_one','pi_two')]
    request_id=rows[0]['delivery_request_id']
    assert request_id and {r['delivery_request_id'] for r in rows}=={request_id}
    # Simulate crashes in both sequential projection phases: A reached queued,
    # while B lost even its per-run request ID. The atomic manifest must repair B.
    rows[0]['delivery']='queued';_save(bridge._path('parent','pi_one'),rows[0])
    rows[1].pop('delivery_request_id');_save(bridge._path('parent','pi_two'),rows[1])
    launch(bridge,'pi_three',state='complete')
    retried=[]
    bridge.owner.prompt_async=lambda _sid,text,_opts,**kwargs: retried.append(
        (text,kwargs['client_request_id'])) or {'status':'accepted'}
    restored=PiChildren(bridge.adapter)
    try:
        restored.refresh()
        assert len(retried)==1 and retried[0][1]==request_id
        assert 'pi_one' in retried[0][0] and 'pi_two' in retried[0][0]
        assert 'pi_three' not in retried[0][0]
    finally:
        restored.close()


def test_interrupt_cancels_one_queued_group_receipt_and_suppresses_all_members(bridge):
    launch(bridge,'pi_one',state='complete');launch(bridge,'pi_two',state='complete')
    bridge.owner.prompt_async=lambda *_a,**_k:{'status':'accepted'}
    bridge.refresh()
    cancelled=[]
    bridge.owner.cancel_unsent_delivery=lambda sid,rid:cancelled.append((sid,rid)) or {'status':'cancelled'}
    bridge.cancel('parent',suppress_pending=True)
    rows=[bridge._read('parent',rid) for rid in ('pi_one','pi_two')]
    assert cancelled==[('parent',rows[0]['delivery_request_id'])]
    assert {r['delivery'] for r in rows}=={'suppressed'}


def test_old_queued_delivery_without_request_id_uses_legacy_cancel_identity(bridge):
    meta,_=launch(bridge,state='complete')
    target=bridge._path('parent','pi_call');target.parent.mkdir(parents=True,exist_ok=True)
    _save(target,{'id':'pi_call','sessionID':'parent','mode':'async','started':0,
                  'delivery':'queued','status':'done','outcome':'completed','result':'legacy'})
    cancelled=[]
    bridge.owner.cancel_unsent_delivery=lambda sid,rid:cancelled.append((sid,rid)) or {'status':'cancelled'}
    bridge.cancel('parent',suppress_pending=True)
    assert cancelled==[('parent','pi-child-pi_call')]
    assert bridge._read('parent','pi_call')['delivery']=='suppressed'


def test_cancel_failure_stays_visible_and_retries_until_receipt_is_cancelled(bridge):
    launch(bridge,state='complete')
    bridge.owner.prompt_async=lambda *_a,**_k:{'status':'accepted'}
    bridge.refresh()
    bridge.owner.cancel_unsent_delivery=lambda *_a:(_ for _ in ()).throw(OSError('disk busy'))
    bridge.owner.request_status=lambda *_a:{'status':'accepted'}
    bridge.cancel('parent',suppress_pending=True)
    assert bridge._read('parent','pi_call')['delivery']=='cancelling'
    bridge.owner.cancel_unsent_delivery=lambda *_a:{'status':'cancelled'}
    bridge.retry_delivery()
    assert bridge._read('parent','pi_call')['delivery']=='suppressed'


def test_interrupt_cancels_receipt_when_claim_reply_was_lost(bridge):
    launch(bridge,state='complete')
    bridge.owner.prompt_async=lambda *_a,**_k:(_ for _ in ()).throw(RuntimeError('reply lost'))
    bridge.refresh()
    row=bridge._read('parent','pi_call')
    assert row['delivery']=='pending' and row['delivery_request_id']=='pi-child-pi_call'
    cancelled=[]
    bridge.owner.cancel_unsent_delivery=lambda sid,rid:cancelled.append((sid,rid)) or {'status':'cancelled'}
    bridge.cancel('parent',suppress_pending=True)
    assert cancelled==[('parent','pi-child-pi_call')]
    assert bridge._read('parent','pi_call')['delivery']=='suppressed'


def test_engine_switch_after_queue_admission_rejects_notification_at_turn_start(bridge):
    launch(bridge,state='complete')
    engine=['pi']; queued=[]
    bridge.owner.session_engine=lambda _sid:engine[0]
    bridge.owner.prompt_async=lambda _sid,_text,_opts,**kwargs: queued.append(
        (kwargs['refresh_opts'],kwargs['client_request_id'])) or {'status':'accepted'}
    bridge.refresh();engine[0]='claude'
    with pytest.raises(RuntimeError,match='当前不是 Pi'):
        queued[0][0]()
    bridge.owner.request_status=lambda *_args:{'status':'rejected','reason':'原会话当前不是 Pi，子代理结果未派发'}
    bridge.refresh()
    row=bridge._read('parent','pi_call')
    assert row['delivery']=='pending' and row['delivery_attempt']==1
    engine[0]='pi';queued.clear();bridge.refresh()
    assert queued[0][1]=='pi-child-pi_call-r1'


def test_partially_suppressed_batch_delivers_only_post_fence_member(bridge):
    bridge._session_path('parent').mkdir(parents=True,exist_ok=True)
    _save(bridge._cancel_path('parent'),{'through_ms':100,'reason':'user_interrupt'})
    old={'id':'pi_old','sessionID':'parent','mode':'async','started':99,'delivery':'pending',
         'status':'done','outcome':'completed','result':'old result'}
    new={'id':'pi_new','sessionID':'parent','mode':'async','started':101,'delivery':'pending',
         'status':'done','outcome':'completed','result':'new result'}
    for row in (old,new):
        bridge._path('parent',row['id']).parent.mkdir(parents=True,exist_ok=True)
    calls=[]
    bridge.owner.prompt_async=lambda _sid,text,_opts,**kwargs:calls.append((text,kwargs)) or {'status':'accepted'}
    bridge.deliver_batch([old,new])
    assert bridge._read('parent','pi_old')['delivery']=='suppressed'
    assert bridge._read('parent','pi_new')['delivery']=='queued'
    assert 'pi_new' in calls[0][0] and 'pi_old' not in calls[0][0]


def test_old_pending_delivery_without_new_fields_is_resumed(bridge):
    launch(bridge,state='complete')
    target=bridge._path('parent','pi_call');target.parent.mkdir(parents=True,exist_ok=True)
    _save(target,{'id':'pi_call','sessionID':'parent','mode':'async','delivery':'pending',
                  'status':'done','outcome':'completed','result':'legacy'})
    calls=[];bridge.owner.prompt_async=lambda *_a,**kwargs:calls.append(kwargs) or {'status':'accepted'}
    bridge.refresh()
    assert calls[0]['client_request_id']=='pi-child-pi_call'
    assert bridge._read('parent','pi_call')['started']==0


def test_close_discovers_unpublished_upstream_identity_by_owned_session_dir(bridge):
    meta,native=launch(bridge)
    launch_path=bridge._path('parent','pi_call').parent/'launch.json'
    meta.pop('upstreamId');_save(launch_path,meta)
    state=json.loads((native/'status.json').read_text())
    state['sessionDir']=str(launch_path.parent/'native'/'0'/'fixture'/'async-native-pi_call')
    _save(native/'status.json',state)
    bridge.close()
    assert len(list((native/'control'/'stop-requests').glob('*.json')))==1


def test_shutdown_waits_for_identity_published_during_close_and_stops_it(bridge):
    meta,native=launch(bridge)
    launch_path=bridge._path('parent','pi_call').parent/'launch.json'
    meta.pop('upstreamId');_save(launch_path,meta)
    state=json.loads((native/'status.json').read_text())
    (native/'status.json').unlink()
    state['sessionDir']=str(launch_path.parent/'native'/'0'/'fixture'/'async-native-pi_call')
    timer=threading.Timer(.05,lambda:_save(native/'status.json',state));timer.start()
    try:
        bridge.close()
    finally:
        timer.join(1)
    assert len(list((native/'control'/'stop-requests').glob('*.json')))==1


def test_observer_can_resume_without_preparing_dependencies(bridge,monkeypatch):
    import pi_children
    launch(bridge,state='complete')
    monkeypatch.setattr(PiChildren,'start',lambda self:None)
    calls=[]
    bridge.owner.prompt_async=lambda *a,**k:calls.append(k) or {'status':'queued'}
    restored=pi_children.observe(bridge.adapter)
    try:
        restored.refresh()
        assert len(calls)==1
    finally:
        restored.close()


@pytest.mark.parametrize('sid,rid',[('../escape','run'),('other','run'),('parent','../run')])
def test_reject_foreign_identity(bridge,sid,rid):
    with pytest.raises(ValueError):bridge._path(sid,rid)


def test_corrupt_and_foreign_upstream_state_never_complete(bridge):
    _,native=launch(bridge)
    _save(native/'status.json',{'runId':'other','state':'complete'})
    bridge.refresh();assert not bridge._path('parent','pi_call').exists()
    (native/'status.json').write_text('[]')
    bridge.refresh();assert not bridge._path('parent','pi_call').exists()


def test_output_cannot_read_files_outside_exact_launch(bridge,tmp_path):
    secret=tmp_path/'unrelated.jsonl';secret.write_text(json.dumps({'message':{'role':'assistant','content':[{'type':'text','text':'private'}]}}))
    scope=tmp_path/'launch';scope.mkdir()
    assert bridge._output({'sessionFile':str(secret)},scope)==''


@pytest.mark.parametrize('agent', ['desk-scout--desk-x', 'desk-scout--desktron-x', 'desk-scout'])
def test_terminal_projection_preserves_successful_sibling(bridge, agent):
    meta,_=launch(bridge)
    for i in range(2):
        path=bridge.root/'parent'/str(i)/'state.json';path.parent.mkdir()
        _save(path,{'id':str(i),'sessionID':'parent','runtime':'pi-subagents-v2','callId':meta['id'],
                    'nativeSessionFile':str(i),'status':'running','task':'original task'})
    bridge._project(meta,[{'sessionFile':'0','status':'complete','agent':agent},
                          {'sessionFile':'1','status':'failed','error':'bad'}],True,'failed')
    rows=[json.loads(p.read_text()) for p in sorted((bridge.root/'parent').glob('*/state.json'))]
    assert [r['outcome'] for r in rows]==['completed','failed']
    assert rows[0]['task']=='original task'
    assert rows[0]['kind']=='desk-scout'


def test_partial_admission_reports_rejection_and_keeps_success(bridge):
    meta,_=launch(bridge,state='complete')
    meta['children']=[{'upstreamId':meta['upstreamId'],'status':'running'},
                      {'status':'done','outcome':'failed','error':'capacity rejected'}]
    _save(bridge._path('parent','pi_call').parent/'launch.json',meta)
    bridge.owner.prompt_async=lambda *a,**k:{'status':'queued'}
    bridge.refresh()
    row=bridge._read('parent','pi_call')
    assert row['outcome']=='failed'
    assert 'fixture result' in row['result'] and 'capacity rejected' in row['result']


def observed(bridge,meta,progress_at,phase='model',index=0):
    rid=meta['id'];d=bridge.root/'parent'/('child-'+rid+str(index));d.mkdir(exist_ok=True)
    _save(d/'state.json',{'id':d.name,'sessionID':'parent','runtime':'pi-subagents-v2','callId':rid,
        'status':'running','phase':phase,'started':progress_at,'progress_at':progress_at,
        'updated':int(__import__('time').time()*1000),
        'nativeSessionFile':str(bridge.root/'parent'/'_calls'/rid/'native'/str(index)/'s.jsonl')})


def test_async_child_that_keeps_progressing_is_not_stopped(bridge):
    import time
    meta,native=launch(bridge);meta['started']=int(time.time()*1000)-3600_000
    _save(bridge._path('parent','pi_call').parent/'launch.json',meta)
    observed(bridge,meta,int(time.time()*1000))
    observed(bridge,meta,0,phase='tool',index=0)  # a long tool call is alive
    bridge.refresh()
    assert not (native/'control').exists()


def test_async_idle_child_is_stopped_and_delivered_as_failure(bridge):
    import time
    meta,native=launch(bridge);meta['started']=int(time.time()*1000)-3600_000
    _save(bridge._path('parent','pi_call').parent/'launch.json',meta)
    # Transport heartbeats refresh `updated`, but progress_at is old: stalled.
    observed(bridge,meta,int(time.time()*1000)-3600_000)
    calls=[]
    bridge.owner.prompt_async=lambda *a,**k:calls.append(k) or {'status':'queued'}
    bridge.owner.request_status=lambda *a:{'status':'finished'}
    bridge.refresh()
    requests=list((native/'control'/'stop-requests').glob('*.json'))
    assert len(requests)==1 and '无进展' in json.loads(requests[0].read_text(encoding='utf-8'))['reason']
    bridge.refresh()
    assert len(list((native/'control'/'stop-requests').glob('*.json')))==1,'stop is requested once'
    value=json.loads((native/'status.json').read_text());value['state']='stopped'
    value['steps'][0].update(status='stopped',finalOutput='partial findings')
    _save(native/'status.json',value)
    bridge.refresh()
    row=bridge._read('parent','pi_call')
    assert row['outcome']=='failed' and row['delivery']!='suppressed'
    assert 'partial findings' in row['result'] and '无进展' in row['result']
    child=json.loads((bridge._path('parent','pi_call').parent/'launch.json').read_text(encoding='utf-8'))
    assert child.get('watchdog',{}).get('reason')=='idle' and child['error_code']=='IDLE_TIMEOUT'
    assert calls,'idle failure must reach the parent'


def test_user_interrupt_is_not_reclassified_as_idle(bridge):
    import time
    meta,native=launch(bridge);meta['started']=int(time.time()*1000)-3600_000
    _save(bridge._path('parent','pi_call').parent/'launch.json',meta)
    observed(bridge,meta,int(time.time()*1000)-3600_000)
    bridge.cancel('parent')
    bridge.refresh()
    reasons=[json.loads(p.read_text(encoding='utf-8'))['reason'] for p in (native/'control'/'stop-requests').glob('*.json')]
    assert reasons and not any('无进展' in r for r in reasons)


def test_async_fork_child_is_attributed_by_upstream_session_file(bridge):
    import time
    meta,native=launch(bridge);meta['started']=int(time.time()*1000)-3600_000
    _save(bridge._path('parent','pi_call').parent/'launch.json',meta)
    fork=str(bridge.root/'parent'/'_calls'/'pi_call'/'parent'/'forks'/'f.jsonl')
    d=bridge.root/'parent'/'child-fork';d.mkdir()
    _save(d/'state.json',{'id':'child-fork','sessionID':'parent','runtime':'pi-subagents-v2','callId':'pi_call',
        'status':'running','phase':'model','progress_at':int(time.time()*1000),'nativeSessionFile':fork})
    value=json.loads((native/'status.json').read_text());value['steps'][0]['sessionFile']=fork
    _save(native/'status.json',value)
    bridge.refresh()
    assert not (native/'control').exists(),'progressing fork child must not look never-started'


def test_async_sibling_progress_cannot_keep_unattributed_slot_alive(bridge):
    import time
    meta,native=launch(bridge);meta['started']=int(time.time()*1000)-3600_000
    _save(bridge._path('parent','pi_call').parent/'launch.json',meta)
    sibling=str(bridge.root/'parent'/'_calls'/'pi_call'/'parent'/'forks'/'sibling.jsonl')
    d=bridge.root/'parent'/'child-sibling';d.mkdir()
    _save(d/'state.json',{'id':'child-sibling','sessionID':'parent','runtime':'pi-subagents-v2','callId':'pi_call',
        'status':'running','phase':'model','progress_at':int(time.time()*1000),'nativeSessionFile':sibling})
    # This run exposes no matching sessionFile. A different slot's live row is
    # not evidence that this slot started or progressed.
    bridge.refresh()
    requests=list((native/'control'/'stop-requests').glob('*.json'))
    assert len(requests)==1
