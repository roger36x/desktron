"""Product routing for Shepherd Conversations without exposing a fifth engine."""
from pathlib import Path
import json
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'desktron/lib'))

from engine_adapters.bus import EventBus
from engine_adapters.shepherd import ShepherdAdapter
from shepherd import Conflict
from shepherd.history_retrieval import HistoryRetrieval


class FakeService:
    def __init__(self, root): self.root=root; self.rows={}
    def create(self, sid, engine, cwd):
        row={'conversation_id':sid,'engine':engine,'binding_id':'binding_1',
             'lease':{'lease_id':'lease_1','binding_id':'binding_1','generation':1}}
        self.rows[sid]=row; return row
    def active(self, sid):
        row=self.rows[sid]
        return {'state':{'committed_offset':0},
                'binding':{'binding_id':row.get('binding_id','binding_1'),'engine':row['engine'],
                  'native_session_id':'native_1','native_cwd':'/tmp','applied_offset':0,
                  'connector_version':'v1','ownership_nonce':'nonce','owner':'shepherd'},
                'lease':row['lease']}
    def switch(self, sid, engine, _cwd, cancelled=None, selection_epoch=None):
        row=self.rows[sid];row['engine']=engine;row['binding_id']='binding_'+engine
        row['lease']={'lease_id':'lease_'+engine,'binding_id':row['binding_id'],'generation':2}
        return self.active(sid)


class ShepherdFacade(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.workspace=root/'workspace';self.workspace.mkdir()
        (self.workspace/'prompts').mkdir();(self.workspace/'prompts/style.md').write_text('')
        self.adapter=ShepherdAdapter(str(root/'data'),str(self.workspace),EventBus())
        self.adapter.service=FakeService(str(root/'state'))
        # Restored adapters created inside a test share this temporary tree too;
        # patch the class so none can leave a promotion timer past fixture cleanup.
        promotion=mock.patch.object(ShepherdAdapter, '_schedule_storage_promotion')
        promotion.start();self.addCleanup(promotion.stop)

    def test_shepherd_pulse_uses_public_projection_without_changing_pi(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        self.assertEqual(self.adapter.transcript_stat(sid),{})
        self.adapter._append_msg(sid,{'info':{'id':'user','role':'user','sessionID':sid,
            'seq':1,'source':'user','at':1},'parts':[{'id':'text','type':'text','text':'hi'}]})
        stat=self.adapter.transcript_stat(sid)
        self.assertGreater(stat.get('size') or 0,0)
        self.assertGreater(stat.get('mtime') or 0,0)
        from engine_adapters.pi import PiAdapter
        self.assertNotIn('transcript_stat',PiAdapter.__dict__)

    def test_unknown_recovery_downgrades_local_failure_projection(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'claude'})['id']
        self.adapter._append_msg(sid,{'info':{'id':'msg_u','role':'user','sessionID':sid,
            'seq':1000,'source':'user','at':1000},'parts':[{'id':'u','type':'text','text':'go'}]})
        self.adapter._append_msg(sid,{'info':{'id':'msg_a','role':'assistant','sessionID':sid,
            'seq':1000,'done_at':9000,'turn_t0':1000,'local_only':True,'failed':True,
            'terminal':{'status':'failed','source':'transport'},
            'error':{'message':'Shepherd 常驻原生进程异常退出，未确认的回合保持封锁'}},
            'parts':[{'id':'a','type':'text','text':'partial answer'}]})
        self.adapter.service.coordinator = SimpleNamespace(state=lambda _sid:{'turns':[{
            'seq':1,'started_at':1,'outcome':'unknown','history_state':'available',
            'recovery_reason':'native_terminal_unconfirmed'}]})
        self.assertTrue(self.adapter._downgrade_unknown_projection(sid,1))
        msg=self.adapter.messages(sid)[-1]
        info=msg['info']
        self.assertNotIn('failed',info);self.assertNotIn('error',info);self.assertNotIn('local_only',info)
        self.assertEqual(info['terminal']['status'],'unknown')
        self.assertEqual(info['recovery']['reason'],'native_terminal_unconfirmed')

    def test_session_search_groups_verified_hits_and_maps_turn_offsets(self):
        class Retrieval:
            def catalog(self):
                return {'conversations':[{'conversation':'adp_hit','title':'命中会话',
                    'domain':'desk-iter','engine':'pi','total':9,'updated_at':123,
                    'index':{'state':'ready'}}], 'warnings':[]}
            def search_cached(self, query, limit, sort, exclude):
                self.args=(query,limit,sort,exclude)
                return {'hits':[{'conversation':'adp_hit','seq':7,'role':'assistant',
                    'settled_at':12.5,'snippet':'目标片段','title':'命中会话',
                    'domain':'desk-iter'}], 'warnings':[]}
        retrieval=Retrieval();self.adapter._history_retrieval=retrieval
        result=self.adapter.search_sessions('目标',sort='time')
        self.assertFalse(result['indexing']);self.assertEqual('',result['error'])
        self.assertEqual(('目标',20,'time',('',)),retrieval.args)
        group=result['results'][0];hit=group['hits'][0]
        self.assertEqual(('adp_hit','pi','命中会话'),
                         (group['session_id'],group['engine'],group['title']))
        self.assertEqual((2,12500,'目标片段'),
                         (hit['turn_from_end'],hit['at'],hit['snippet']))

    def test_session_search_refresh_failure_is_bounded_and_retryable(self):
        retrieval=mock.Mock();retrieval.sync.side_effect=RuntimeError('broken source')
        self.adapter._history_retrieval=retrieval
        before=__import__('time').time()
        self.adapter._history_search_building=True
        self.adapter._refresh_history_search(['adp_bad'])
        self.assertFalse(self.adapter._history_search_building)
        self.assertEqual(1,self.adapter._history_search_done)
        self.assertEqual(['broken source'],self.adapter._history_search_errors)
        self.assertGreaterEqual(self.adapter._history_search_retry_at,before+29)

    def test_auth_notice_does_not_follow_engine_switch(self):
        ad=self.adapter
        row=ad.create_session('workflow',opts={'active_engine':'claude','shepherd_mode':True})
        sid=row['id']
        worker=ad._worker('claude')
        ctx={'sid':sid,'seq':1,'t0':10,'opts':{'model':'opus'},'producer_engine':'claude'}
        worker._ctx_env_publish(ctx,{'stage':'auth','state':'error','label':'expired'})
        self.assertEqual(ad.session_environment(sid)['engine'],'claude')
        with mock.patch.object(ad,'_schedule_engine_prepare'):
            ad.switch_engine(sid,'pi')
        self.assertIsNone(ad.session_environment(sid))
        self.assertIn('auth:claude:claude',ad.index[sid]['environment_issues'])
        # Logging Claude in still clears its hidden issue while Pi is selected.
        ad.resolve_auth({'engine':'claude','provider':'claude'},20)
        with mock.patch.object(ad,'_schedule_engine_prepare'):
            ad.switch_engine(sid,'claude')
        self.assertIsNone(ad.session_environment(sid))

    def test_create_is_one_public_conversation_with_hidden_binding(self):
        row=self.adapter.create_session('workflow',opts={'active_engine':'pi','shepherd_mode':True})
        self.assertEqual(row['engine'],'pi');self.assertTrue(row['shepherd_mode'])
        self.assertEqual(self.adapter.list_sessions()[0]['id'],row['id'])
        self.assertEqual(self.adapter.list_sessions()[0]['engine'],'pi')
        self.assertEqual(self.adapter.execution_name(row['id']),'pi')
        self.assertEqual(row['opts']['active_engine'],'pi')

    def test_create_snapshots_initial_engine_defaults_for_first_paint_and_turn(self):
        defaults={'model':'example-gw/gpt-5.6-sol:gpt-5.6-sol__max',
                  'effort':'xhigh','mode':'bypassPermissions'}
        with mock.patch('workbench._wb_state_read',return_value={'engine_opts':{'pi':defaults}}):
            row=self.adapter.create_session('workflow',opts={'active_engine':'pi'})
        saved=row['engine_opts']['pi']
        self.assertEqual(saved['model'],defaults['model'])
        self.assertEqual(saved['effort'],'xhigh')
        self.assertEqual(self.adapter.session_options(row['id'],engine='pi')['model'],defaults['model'])

    def test_create_explicit_model_overrides_engine_default(self):
        with mock.patch('workbench._wb_state_read',return_value={'engine_opts':{
                'pi':{'model':'deepseek/deepseek-v4-pro','effort':'max'}}}):
            row=self.adapter.create_session('workflow',opts={
                'active_engine':'pi','model':'example-gw/gpt-5.6-sol:gpt-5.6-sol__max',
                'effort':'xhigh'})
        self.assertEqual(row['engine_opts']['pi']['model'],
                         'example-gw/gpt-5.6-sol:gpt-5.6-sol__max')
        self.assertEqual(row['engine_opts']['pi']['effort'],'xhigh')

    def test_create_does_not_inherit_global_role_or_persist_routing_flags_as_engine_opts(self):
        with mock.patch('workbench._wb_state_read',return_value={'engine_opts':{'pi':{
                'model':'pi-model','desk_role':'ops','shepherd_mode':False,
                'shepherd_trial':True,'active_engine':'claude'}}}):
            row=self.adapter.create_session('workflow',opts={
                'active_engine':'pi','desk_role':'release'})
        saved=row['engine_opts']['pi']
        self.assertEqual(saved['model'],'pi-model')
        self.assertEqual(saved['desk_role'],'release')
        self.assertNotIn('shepherd_mode',saved)
        self.assertNotIn('shepherd_trial',saved)
        self.assertNotIn('active_engine',saved)
        self.assertEqual(row['opts']['desk_role'],'release')
        self.assertEqual(row['opts']['active_engine'],'pi')
        self.assertEqual(self.adapter.session_options(row['id'],{
            'desk_role':'stale','active_engine':'claude'},engine='pi')['desk_role'],'release')
        changed=self.adapter.update_session_options(row['id'],{'effort':'high'},
            {'desk_role':'stale','active_engine':'claude'},engine='pi')
        self.assertEqual(changed['desk_role'],'release')
        self.assertNotIn('active_engine',changed)

    def test_create_without_explicit_role_ignores_stale_global_role_in_claude_prompt(self):
        agents=self.workspace/'.desk/agents';agents.mkdir(parents=True)
        (agents/'ops.md').write_text('STALE GLOBAL ROLE MUST NOT ENTER PROMPT')
        with mock.patch('workbench._wb_state_read',return_value={'engine_opts':{'claude':{
                'model':'opus','desk_role':'ops'}}}):
            row=self.adapter.create_session('workflow',opts={'active_engine':'claude'})
        self.assertNotIn('desk_role',row['engine_opts']['claude'])
        self.assertNotIn('STALE GLOBAL ROLE',self.adapter._claude_static_system(row['id']))

    def test_dirty_global_fallback_cannot_reenter_first_turn_or_settings(self):
        agents=self.workspace/'.desk/agents';agents.mkdir(parents=True,exist_ok=True)
        (agents/'ops.md').write_text('STALE GLOBAL ROLE MUST NOT ENTER TURN')
        with mock.patch('workbench._wb_state_read',return_value={'engine_opts':{'claude':{
                'model':'opus','desk_role':'ops','shepherd_mode':False,
                'shepherd_trial':True,'active_engine':'pi'}}}):
            row=self.adapter.create_session('workflow',opts={'active_engine':'claude'})
        sid=row['id']
        dirty={'model':'opus','desk_role':'ops','shepherd_mode':False,
               'shepherd_trial':True,'active_engine':'pi'}
        execution=self.adapter.execution_options(sid,dirty)
        self.assertEqual(execution['model'],'opus')
        for key in ('desk_role','shepherd_mode','shepherd_trial','active_engine'):
            self.assertNotIn(key,execution)
        self.assertNotIn('STALE GLOBAL ROLE',self.adapter._worker('claude').system_body(execution))
        updated=self.adapter.update_session_options(sid,{'effort':'max'},dirty,engine='claude')
        self.assertEqual(updated['effort'],'max')
        for key in ('desk_role','shepherd_mode','shepherd_trial','active_engine'):
            self.assertNotIn(key,updated)
            self.assertNotIn(key,self.adapter.index[sid]['engine_opts']['claude'])

    def test_old_codex_failure_does_not_mask_next_preflight(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'codex'})['id']
        for stage in ('engine','native_pre_dispatch'):
            self.adapter._env_publish(sid,{'state':'error','stage':stage,'label':'old failure'})
            self.adapter._clear_turn_error_environment(sid)
            self.assertIsNone(self.adapter.session_environment(sid))
        for stage in ('auth','permission','recovery'):
            self.adapter._env_publish(sid,{'state':'error','stage':stage,'label':'still relevant'})
            self.adapter._clear_turn_error_environment(sid)
            self.assertEqual(self.adapter.session_environment(sid)['stage'],stage)
            self.adapter._env_publish(sid,{'state':'ready','stage':stage})
        self.adapter._env_publish(sid,{'state':'error','stage':'engine','label':'explicit action',
                                       'action':{'kind':'login'}})
        self.adapter._clear_turn_error_environment(sid)
        self.assertEqual(self.adapter.session_environment(sid)['action'],{'kind':'login'})

    def test_config_label_uses_selected_engines_own_reported_model(self):
        row=self.adapter.create_session('workflow',opts={'active_engine':'pi'});sid=row['id']
        self.adapter.index[sid]['engine_models']={'pi':'openai/gpt-5.4','claude':'opus'}
        self.assertEqual(self.adapter.config_label(sid),'pi · openai/gpt-5.4')
        with mock.patch.object(self.adapter, '_schedule_engine_prepare'):
            self.adapter.switch_engine(sid,'claude')
        self.assertEqual(self.adapter.config_label(sid),'claude · opus')
        self.assertEqual(self.adapter.config_label(),'Shepherd')

    def test_create_failure_removes_public_index_row(self):
        self.adapter.service.create=mock.Mock(side_effect=RuntimeError('native failed'))
        with self.assertRaisesRegex(RuntimeError,'native failed'):
            self.adapter.create_session('workflow',opts={'active_engine':'claude'})
        self.assertEqual(self.adapter.index,{})

    def test_history_pages_public_mixed_engine_projection_without_native_reads(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        for seq, engine in enumerate(('pi','claude','codex','pi') * 10, 1):
            self.adapter._append_msg(sid, {'info':{'id':f'u{seq}','role':'user',
                'sessionID':sid,'seq':seq,'engine':engine,'at':seq},
                'parts':[{'id':f'up{seq}','type':'text','text':f'q{seq}'}]})
            self.adapter._append_msg(sid, {'info':{'id':f'a{seq}','role':'assistant',
                'sessionID':sid,'engine':engine,'at':seq},
                'parts':[{'id':f'ap{seq}','type':'text','text':f'a{seq}'}]})
        self.adapter._connector=mock.Mock(side_effect=AssertionError('native read during UI paging'))
        page=self.adapter.history(sid,have=25,limit=10)
        self.assertEqual([m['info']['seq'] for m in page
                          if m['info']['role']=='user'],list(range(6,16)))
        self.adapter._connector.assert_not_called()

    def test_history_hides_only_pi_openai_title_only_reasoning(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        title='**Defining governance and tool policies**'
        body=title+'\n\nThe policy needs an explicit approval boundary.'
        for index,(model,text) in enumerate((
                ('openai/gpt-5.6-sol',title),
                ('openai/gpt-5.6-sol',body),
                ('deepseek/deepseek-v4',title)),1):
            self.adapter._append_msg(sid,{'info':{
                'id':f'u{index}','role':'user','sessionID':sid,'seq':index,'at':index},
                'parts':[{'id':f'u{index}p','type':'text','text':'continue'}]})
            self.adapter._append_msg(sid,{'info':{
                'id':f'a{index}','role':'assistant','sessionID':sid,
                'engine':'pi','model':model,'at':index},'parts':[{
                    'id':f'p{index}','type':'reasoning','text':text}]})
        page=self.adapter.history(sid,have=0,limit=10)
        assistant=[message for message in page if message['info']['role']=='assistant']
        expected = [
            [],
            [{'id':'p2','type':'reasoning','text':body}],
            [{'id':'p3','type':'reasoning','text':title}],
        ]
        self.assertEqual([message['parts'] for message in assistant], expected)
        initial=json.loads(b'['+self.adapter.messages_window_json(sid,10)+b']')
        self.assertEqual([message['parts'] for message in initial
                          if message['info']['role']=='assistant'], expected)
        stored=self.adapter.messages(sid)
        self.assertEqual(stored[1]['parts'][0]['text'],title)
        self.assertEqual(self.adapter.part(sid,'p1')['text'],title)

    def test_recovered_live_message_keeps_turn_attribution_when_archived(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        title='**Checking the recovered turn**'
        snapshot={'info':{
            'id':'live-a','role':'assistant','sessionID':sid,'running':True,
            'started':100,'beat':200,'engine':'pi','model':'openai/gpt-5.6-sol'},
            'parts':[{'id':'live-p','messageID':'live-a','sessionID':sid,
                      'type':'reasoning','text':title}]}
        Path(self.adapter._live_checkpoint_path(sid)).write_text(
            json.dumps(snapshot),encoding='utf-8')
        self.assertEqual(self.adapter.live_message(sid)['parts'],[])
        self.assertEqual(self.adapter.part(sid,'live-p')['text'],title)
        self.assertTrue(self.adapter._archive_live_checkpoint(sid))
        initial=json.loads(b'['+self.adapter.messages_window_json(sid,10)+b']')
        self.assertEqual(initial[0]['parts'],[])
        self.assertEqual(self.adapter.history(sid,0,10)[0]['parts'],[])
        self.assertEqual(self.adapter.messages(sid)[0]['parts'][0]['text'],title)

    def test_live_checkpoint_records_immutable_producer_and_model(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        ctx={'sid':sid,'msg_id':'live-a','parts':{},'order':[],
             'producer_engine':'pi','actual_model':'openai/gpt-5.6-sol'}
        self.adapter._checkpoint_live(ctx,force=True)
        snapshot=json.loads(Path(self.adapter._live_checkpoint_path(sid)).read_text())
        self.assertEqual(snapshot['info']['engine'],'pi')
        self.assertEqual(snapshot['info']['model'],'openai/gpt-5.6-sol')

    def test_old_live_checkpoint_without_attribution_is_not_guessed(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        self.adapter.index[sid].update({
            'engine':'pi','model':'openai/gpt-5.6-sol',
            'engine_models':{'pi':'openai/gpt-5.6-sol'}})
        title='**Preserve an unattributed legacy block**'
        snapshot={'info':{
            'id':'legacy-a','role':'assistant','sessionID':sid,'running':True,
            'started':100,'beat':200},'parts':[{
                'id':'legacy-p','messageID':'legacy-a','sessionID':sid,
                'type':'reasoning','text':title}]}
        Path(self.adapter._live_checkpoint_path(sid)).write_text(
            json.dumps(snapshot),encoding='utf-8')
        self.assertEqual(self.adapter.live_message(sid)['parts'][0]['text'],title)

    def test_native_slash_command_is_rejected_before_public_persistence(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'claude'})['id']
        self.assertEqual(self.adapter.slash_commands_for_session(sid),[])
        for text in (' /rename unsafe', '/compact', '\n/foo', '/skill:x arg', '/goal'):
            with self.subTest(text=text), self.assertRaisesRegex(Conflict,'斜杠命令'):
                self.adapter._accept_prompt(sid,text,{},[], 'request_slash','',{})
        self.assertEqual(self.adapter.messages(sid),[])

    def test_text_that_only_begins_with_slash_passes_the_command_gate(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'claude'})['id']
        class PastGate(Exception):
            pass
        for text in ('/', '  /', '/ 这是正文', '/opt/a/b.txt 是什么', '//注释',
                     '/中文', '/tmp/x', '/a.b', '/foo/', 'a /new'):
            with self.subTest(text=text), \
                 mock.patch.object(self.adapter, '_model_patch_limited', side_effect=PastGate):
                with self.assertRaises(PastGate):
                    self.adapter._accept_prompt(sid,text,{},[], 'request_text','',{})

    def test_switch_selects_immediately_without_waiting_for_catchup(self):
        row=self.adapter.create_session('workflow',opts={'active_engine':'pi'});sid=row['id']
        self.adapter.index[sid]['model']='old-pi-model'
        with mock.patch.object(self.adapter, '_schedule_engine_prepare') as prepare:
            got=self.adapter.switch_engine(sid,'claude')
        self.assertEqual(got['engine'],'claude');self.assertEqual(got['producer_engine'],'pi')
        self.assertTrue(got['preparing']);prepare.assert_called_once_with(sid)
        self.assertEqual(self.adapter.selected_engine(sid),'claude')
        self.assertEqual(self.adapter.session_engine(sid),'pi')
        self.assertEqual(self.adapter.index[sid]['model'],'old-pi-model')

    def test_first_codex_selection_snapshots_target_defaults_for_response_and_events(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        defaults={'sandbox':'danger-full-access','model':'gpt-6-astra','effort':'high',
                  'desk_role':'ops','active_engine':'pi','shepherd_mode':True}
        with mock.patch('workbench._wb_state_read',return_value={'engine_opts':{'codex':defaults}}), \
                mock.patch.object(self.adapter,'_schedule_engine_prepare'), \
                mock.patch.object(self.adapter.bus,'publish') as publish:
            result=self.adapter.switch_engine(sid,'codex')
        self.assertEqual(result['opts']['sandbox'],'danger-full-access')
        settings=[call.args[0]['properties'] for call in publish.call_args_list
                  if call.args[0]['type']=='session.settings']
        self.assertEqual(settings[-1]['opts'],result['opts'])
        self.assertEqual(self.adapter.session_options(sid,engine='codex'),result['opts'])
        for key in ('desk_role','active_engine','shepherd_mode'):
            self.assertNotIn(key,result['opts'])
        # A later global change cannot replace this Conversation's choice.
        self.assertEqual(self.adapter.session_options(sid,{'sandbox':'read-only'},
                                                     engine='codex')['sandbox'],'danger-full-access')

    def test_switch_preserves_an_existing_explicit_codex_permission(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        self.adapter.update_session_options(sid,{'sandbox':'read-only'},engine='codex')
        with mock.patch('workbench._wb_state_read',return_value={
                'engine_opts':{'codex':{'sandbox':'danger-full-access'}}}), \
                mock.patch.object(self.adapter,'_schedule_engine_prepare'):
            result=self.adapter.switch_engine(sid,'codex')
        self.assertEqual(result['opts']['sandbox'],'read-only')

    def test_partial_codex_settings_do_not_replace_inherited_permission_with_default(self):
        for patch in ({'model':'gpt-6-astra'},{'effort':'high'}):
            with self.subTest(patch=patch):
                sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
                result=self.adapter.update_session_options(sid,patch,
                    {'sandbox':'danger-full-access'},engine='codex')
                self.assertEqual(result['sandbox'],'danger-full-access')
                changed=self.adapter.update_session_options(sid,{'sandbox':'workspace-write'},
                    {'sandbox':'danger-full-access'},engine='codex')
                self.assertEqual(changed['sandbox'],'workspace-write')
                self.assertEqual(self.adapter.update_session_options(sid,{'effort':'high'},
                    {'sandbox':'danger-full-access'},engine='codex')['sandbox'],'workspace-write')

    def test_running_switch_updates_controls_immediately(self):
        row=self.adapter.create_session('workflow',opts={'active_engine':'pi'});sid=row['id']
        self.adapter._live[sid]={'sid':sid}
        with mock.patch.object(self.adapter, '_schedule_engine_prepare'):
            got=self.adapter.switch_engine(sid,'claude')
        self.assertEqual(got['engine'],'claude');self.assertEqual(got['producer_engine'],'pi')
        self.assertEqual(self.adapter.execution_status(sid),{
            'selected_engine':'claude','producer_engine':'pi','preparing':True})

    def test_switch_during_pre_live_turn_window_is_immediate(self):
        row=self.adapter.create_session('workflow',opts={'active_engine':'pi'});sid=row['id']
        guard=self.adapter._turn_locks.setdefault(sid,threading.Lock());guard.acquire()
        try:
            with mock.patch.object(self.adapter, '_schedule_engine_prepare'):
                got=self.adapter.switch_engine(sid,'claude')
        finally: guard.release()
        self.assertEqual(got['engine'],'claude');self.assertEqual(self.adapter.session_engine(sid),'pi')

    def test_selecting_current_producer_cancels_pending_target(self):
        row=self.adapter.create_session('workflow',opts={'active_engine':'pi'});sid=row['id']
        timer=mock.Mock()
        with mock.patch.object(self.adapter, '_schedule_engine_prepare'):
            self.adapter.switch_engine(sid,'claude')
        with self.adapter._lock:
            self.adapter._prepare_timers[sid]=timer
            self.adapter._selection_epochs[sid]=3
        got=self.adapter.switch_engine(sid,'pi')
        timer.cancel.assert_called_once_with()
        self.assertEqual(self.adapter._selection_epochs[sid],4)
        self.assertFalse(got['preparing']);self.assertEqual(got['engine'],'pi')
        self.assertEqual(self.adapter.execution_status(sid),{
            'selected_engine':'pi','producer_engine':'pi','preparing':False})

    def test_queued_switch_is_consumed_before_next_turn(self):
        row=self.adapter.create_session('workflow',opts={'active_engine':'pi'});sid=row['id']
        with self.adapter._lock:
            self.adapter.index[sid]['next_engine']='claude';self.adapter._save_index()
        self.adapter._before_turn_start(sid)
        self.assertEqual(self.adapter.session_engine(sid),'claude')
        self.assertEqual(self.adapter.execution_status(sid),{
            'selected_engine':'claude','producer_engine':'claude','preparing':False})
        self.assertNotIn('next_engine',self.adapter.index[sid])

    def test_selection_epoch_survives_adapter_restart(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        with mock.patch.object(self.adapter, '_schedule_engine_prepare'):
            self.adapter.switch_engine(sid,'claude')
        persisted_epoch=self.adapter.index[sid]['selection_epoch']
        root=Path(self.tmp.name)
        restored=ShepherdAdapter(str(root/'data'),str(self.workspace),EventBus())
        restored.service=FakeService(str(root/'state'))
        restored.service.rows=self.adapter.service.rows
        with mock.patch.object(restored.service, 'switch', wraps=restored.service.switch) as switch:
            restored._apply_requested_engine(sid)
        self.assertEqual(switch.call_args.kwargs['selection_epoch'],persisted_epoch)
        self.assertEqual(restored.session_engine(sid),'claude')

    def test_late_switch_result_cannot_overwrite_newer_selection_epoch(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        with self.adapter._lock:
            self.adapter.index[sid]['selected_engine']='claude'
            self.adapter.index[sid]['next_engine']='claude'
            self.adapter._selection_epochs[sid]=1
        original=self.adapter.service.switch
        def switch(sid, engine, cwd, cancelled=None, selection_epoch=None):
            self.assertEqual(engine,'claude')
            with mock.patch.object(self.adapter, '_schedule_engine_prepare'):
                self.adapter.switch_engine(sid,'pi')
            return original(sid,engine,cwd,cancelled=cancelled)
        self.adapter.service.switch=switch
        self.adapter._apply_requested_engine(sid)
        self.assertEqual(self.adapter.selected_engine(sid),'pi')
        self.assertEqual(self.adapter.session_engine(sid),'claude')
        self.assertEqual(self.adapter.index[sid]['next_engine'],'pi')

    def test_pre_turn_catchup_consumes_latest_selection(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        self.adapter.index[sid]['next_engine']='claude'
        original=self.adapter.service.switch
        def switch(sid, engine, cwd, cancelled=None, selection_epoch=None):
            if engine == 'claude':
                with mock.patch.object(self.adapter, '_schedule_engine_prepare'):
                    self.adapter.switch_engine(sid,'codex')
            return original(sid,engine,cwd,cancelled=cancelled)
        self.adapter.service.switch=switch
        launched=[]
        self.adapter._run=lambda sid,*_args: launched.append(self.adapter.session_engine(sid))
        self.adapter._run_serial(sid,'next turn',{},[])
        self.assertEqual(launched,['codex'])
        self.assertEqual(self.adapter.execution_status(sid),{
            'selected_engine':'codex','producer_engine':'codex','preparing':False})

    def test_unsettled_pre_turn_selection_blocks_message(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        self.adapter.index[sid]['next_engine']='claude'
        original=self.adapter.service.switch
        def switch(sid, engine, cwd, cancelled=None, selection_epoch=None):
            target='codex' if engine == 'claude' else 'claude'
            with mock.patch.object(self.adapter, '_schedule_engine_prepare'):
                self.adapter.switch_engine(sid,target)
            return original(sid,engine,cwd,cancelled=cancelled)
        self.adapter.service.switch=mock.Mock(side_effect=switch)
        self.adapter._run=mock.Mock()
        with self.assertRaisesRegex(RuntimeError,'仍在切换'):
            self.adapter._run_serial(sid,'must not persist',{},[])
        self.adapter._run.assert_not_called()
        self.assertEqual(self.adapter.service.switch.call_count,8)
        self.assertEqual(self.adapter.execution_status(sid),{
            'selected_engine':'claude','producer_engine':'codex','preparing':True})
        with open(self.adapter.index_path,encoding='utf-8') as saved:
            self.assertEqual(json.load(saved)[sid]['next_engine'],'claude')

    def test_failed_queued_switch_remains_durable(self):
        row=self.adapter.create_session('workflow',opts={'active_engine':'pi'});sid=row['id']
        with self.adapter._lock:
            self.adapter.index[sid]['next_engine']='claude';self.adapter._save_index()
        self.adapter.service.switch=mock.Mock(side_effect=RuntimeError('catch-up failed'))
        with self.assertRaisesRegex(RuntimeError,'catch-up failed'):
            self.adapter._before_turn_start(sid)
        self.assertEqual(self.adapter.index[sid]['next_engine'],'claude')
        self.assertEqual(self.adapter.session_engine(sid),'pi')

    def test_predispatch_failure_restores_prompt_without_failed_assistant(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'codex'})['id']
        self.adapter._env_publish(sid,{'state':'error','stage':'engine','label':'old context error'})
        # _run clears old turn errors before persisting the user's new message.
        self.adapter._clear_turn_error_environment(sid)
        self.adapter._append_msg(sid, {'info':{'id':'user','role':'user','sessionID':sid,
            'seq':7,'source':'user','at':7},'parts':[{'id':'text','type':'text','text':'retry me'}]})
        ctx={'sid':sid,'msg_id':'assistant','parts':{},'order':[],
             'seq':7,'source':'user','t0':7,
             '_prompt_not_started':'bridge failed before prepared'}
        self.adapter._live[sid]=ctx
        self.adapter.service.coordinator=SimpleNamespace(state=lambda _sid:{'inflight':None})
        self.adapter._request_local.rid='request-7'
        events=[];self.adapter.bus.publish=events.append
        with mock.patch.object(self.adapter,'_schedule_engine_prepare'):
            self.adapter._finish_turn(sid,ctx,'bridge failed before prepared')
        self.assertEqual(self.adapter.messages(sid),[])
        self.assertIsNone(self.adapter._live.get(sid))
        errors=[e for e in events if e.get('type')=='session.error']
        self.assertEqual(errors[-1]['properties']['requestID'],'request-7')
        self.assertEqual(errors[-1]['properties']['error']['code'],'prompt_not_started')
        self.assertEqual(errors[-1]['properties']['error']['stage'],'native_pre_dispatch')
        saved=self.adapter.session_environment(sid)
        self.assertEqual(saved['stage'],'native_pre_dispatch')
        self.assertEqual(saved['details'],['bridge failed before prepared'])
        self.assertNotIn('old context error',saved['label'])

    def test_finish_refreshes_index_only_for_a_settled_head(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        states = ({'turns': []}, {'turns': [{'seq': 1}], 'active_turn': {'seq': 2}},
                  {'turns': [{'seq': 1}], 'active_turn': None})
        for state in states:
            self.adapter.service.coordinator=SimpleNamespace(state=lambda _sid:state)
            with mock.patch('engine_adapters.shepherd.CliAdapter._finish_turn',return_value='done'), \
                 mock.patch('peer_context.refresh_after_settle') as refresh:
                self.adapter._finish_turn(sid, {}, None)
                self.assertEqual(refresh.called, bool(state['turns'] and not state.get('active_turn')))

    def test_turn_finish_keeps_selection_for_next_turn(self):
        row=self.adapter.create_session('workflow',opts={'active_engine':'pi'});sid=row['id']
        with self.adapter._lock:
            self.adapter.index[sid]['next_engine']='claude';self.adapter._save_index()
        self.adapter.service.coordinator=SimpleNamespace(state=lambda _sid:{'inflight':None})
        order=[]
        original=self.adapter._apply_requested_engine
        def apply(request_sid): order.append('switch');return original(request_sid)
        self.adapter._apply_requested_engine=apply
        with mock.patch('engine_adapters.shepherd.CliAdapter._finish_turn',
                        side_effect=lambda *_args: order.append('finish') or 'done'), \
             mock.patch.object(self.adapter, '_schedule_engine_prepare'):
            got=self.adapter._finish_turn(sid,{},None)
        self.assertEqual(got,'done');self.assertEqual(order,['finish'])
        self.assertEqual(self.adapter.session_engine(sid),'pi')
        self.assertEqual(self.adapter.execution_status(sid),{
            'selected_engine':'claude','producer_engine':'pi','preparing':True})

    def test_per_engine_options_survive_a_b_a_selection(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        self.adapter._workers['pi']=mock.Mock(
            normalize_options=lambda value:dict(value), config_label=lambda _sid:'pi')
        claude=mock.Mock(normalize_options=lambda value:dict(value), config_label=lambda _sid:'claude')
        self.adapter._workers['claude']=claude
        self.adapter.update_session_options(sid, {'model':'pi-model','effort':'high','mode':'acceptEdits'},
                                            engine='pi')
        with mock.patch.object(self.adapter, '_schedule_engine_prepare'):
            self.adapter.switch_engine(sid,'claude')
        self.adapter.update_session_options(sid, {'model':'claude-model','effort':'max',
                                                    'mode':'bypassPermissions'}, engine='claude')
        with mock.patch.object(self.adapter, '_schedule_engine_prepare'):
            self.adapter.switch_engine(sid,'pi')
        self.assertEqual(self.adapter.session_options(sid, engine='pi')['model'],'pi-model')
        self.assertEqual(self.adapter.session_options(sid, engine='pi')['effort'],'high')
        self.assertEqual(self.adapter.session_options(sid, engine='pi')['mode'],'acceptEdits')
        self.assertEqual(self.adapter.session_options(sid, engine='claude')['model'],'claude-model')
        self.assertEqual(self.adapter.session_options(sid, engine='claude')['effort'],'max')

    def test_reconciliation_blocks_queued_switch_at_finish(self):
        row=self.adapter.create_session('workflow',opts={'active_engine':'pi'});sid=row['id']
        with self.adapter._lock:
            self.adapter.index[sid]['next_engine']='claude';self.adapter._save_index()
        self.adapter.service.coordinator=SimpleNamespace(
            state=lambda _sid:{'inflight':{'state':'needs_reconciliation'}})
        self.adapter._apply_requested_engine=mock.Mock()
        with mock.patch('engine_adapters.shepherd.CliAdapter._finish_turn',return_value='done'):
            self.adapter._finish_turn(sid,{},None)
        self.adapter._apply_requested_engine.assert_not_called()
        self.assertEqual(self.adapter.index[sid]['next_engine'],'claude')

    def test_failed_queued_switch_blocks_turn_and_publishes_error(self):
        row=self.adapter.create_session('workflow',opts={'active_engine':'pi'});sid=row['id']
        with self.adapter._lock:
            self.adapter.index[sid]['next_engine']='claude';self.adapter._save_index()
        self.adapter.service.switch=mock.Mock(side_effect=RuntimeError('catch-up failed'))
        self.adapter._run=mock.Mock()
        queue=self.adapter.bus.subscribe()
        with self.assertRaisesRegex(RuntimeError,'catch-up failed'):
            self.adapter._run_serial(sid,'must not persist',{},[],client_request_id='client-1')
        self.adapter._run.assert_not_called()
        events=[]
        while not queue.empty(): events.append(queue.get_nowait())
        errors=[event for event in events if event.get('type')=='session.error']
        self.assertEqual(errors[-1]['properties']['error']['message'],'catch-up failed')
        self.assertEqual(errors[-1]['properties']['error']['code'],'prompt_not_started')
        self.assertEqual(errors[-1]['properties']['requestID'],'client-1')

    def test_reconciliation_without_process_identity_keeps_fence(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        state={'inflight':{'state':'needs_reconciliation','attempt_id':'attempt_legacy',
                           'recovery':{}},'pending_switch':None}
        self.adapter.service.coordinator=SimpleNamespace(state=lambda _sid:state)
        active=self.adapter.service.active(sid)
        self.adapter.service.reconcile_end=mock.Mock(return_value=active)
        with mock.patch('shepherd.recovery.terminate_recorded_process') as terminate, \
             self.assertRaisesRegex(Conflict,'无法确认旧原生写者已退出'):
            self.adapter.reconcile_end(sid,'attempt_legacy')
        terminate.assert_not_called()
        self.adapter.service.reconcile_end.assert_not_called()

    def test_reconciliation_with_ambiguous_process_keeps_fence_without_signalling(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        state={'inflight':{'state':'needs_reconciliation','attempt_id':'attempt_current',
                           'recovery':{'process':{'pid':42,'pgid':42,'start_token':'old'}}},
               'pending_switch':None}
        self.adapter.service.coordinator=SimpleNamespace(state=lambda _sid:state)
        self.adapter.service.reconcile_end=mock.Mock(return_value=self.adapter.service.active(sid))
        with mock.patch('shepherd.recovery.process_state',return_value='unknown'), \
             mock.patch('shepherd.recovery.terminate_recorded_process') as terminate, \
             self.assertRaisesRegex(Conflict,'无法确认旧原生写者已退出'):
            self.adapter.reconcile_end(sid,'attempt_current')
        terminate.assert_not_called()
        self.adapter.service.reconcile_end.assert_not_called()

    def test_reconciliation_with_dead_process_allows_replacement(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        state={'inflight':{'state':'needs_reconciliation','attempt_id':'attempt_dead',
                           'recovery':{'process':{'pid':42,'pgid':42,'start_token':'old'}}},
               'pending_switch':None}
        self.adapter.service.coordinator=SimpleNamespace(state=lambda _sid:state)
        self.adapter.service.reconcile_end=mock.Mock(return_value=self.adapter.service.active(sid))
        with mock.patch('shepherd.recovery.process_state',return_value='dead'):
            got=self.adapter.reconcile_end(sid,'attempt_dead')
        self.assertTrue(got['ok'])
        self.adapter.service.reconcile_end.assert_called_once()

    def test_reconciliation_with_exact_live_process_requires_termination(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        state={'inflight':{'state':'needs_reconciliation','attempt_id':'attempt_current',
                           'recovery':{'process':{'pid':42,'pgid':42,'start_token':'old'}}},
               'pending_switch':None}
        self.adapter.service.coordinator=SimpleNamespace(state=lambda _sid:state)
        self.adapter.service.reconcile_end=mock.Mock()
        with mock.patch('shepherd.recovery.process_state',return_value='alive'), \
             mock.patch('shepherd.recovery.terminate_recorded_process',return_value=False):
            with self.assertRaisesRegex(Conflict,'仍存活'):
                self.adapter.reconcile_end(sid,'attempt_current')
        self.adapter.service.reconcile_end.assert_not_called()

    def test_reconciliation_rejects_occupied_switch_lock_without_waiting(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        guard=self.adapter._requested_switch_locks.setdefault(sid,threading.Lock())
        guard.acquire()
        try:
            with self.assertRaisesRegex(Conflict,'仍在切换'):
                self.adapter.reconcile_end(sid,'attempt_current')
        finally:
            guard.release()

    def test_delete_reconciliating_conversation_removes_public_projection_after_purge(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        with mock.patch.object(self.adapter,'_tombstone_and_purge',return_value={
                'state':'deleted','reason':'user_delete'} ) as purge:
            self.adapter.delete_session(sid)
        purge.assert_called_once_with(sid)
        self.assertNotIn(sid,self.adapter.index)

    def test_delete_writer_conflict_keeps_public_projection_and_recovery_timer(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        coordinator=mock.Mock()
        coordinator.tombstone_for_delete.side_effect=Conflict(
            'Shepherd 会话仍在执行或切换，不能删除')
        self.adapter.service.coordinator=coordinator
        timer=mock.Mock()
        with self.adapter._lock:
            self.adapter._prepare_timers[sid]=timer
            self.adapter._selection_epochs[sid]=3
        self.adapter._recovery.cancel_conversation=mock.Mock()
        with self.assertRaisesRegex(RuntimeError,'仍在执行或切换'):
            self.adapter.delete_session(sid)
        timer.cancel.assert_not_called()
        self.adapter._recovery.cancel_conversation.assert_not_called()
        self.assertIn(sid,self.adapter.index)

    def test_delete_reconciliation_does_not_override_active_turn_guard(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        self.adapter.service.coordinator=mock.Mock()
        guard=self.adapter._turn_locks.setdefault(sid,threading.Lock());guard.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError,'仍在执行或切换'):
                self.adapter.delete_session(sid)
        finally:
            guard.release()
        self.adapter.service.coordinator.tombstone_for_delete.assert_not_called()
        self.assertIn(sid,self.adapter.index)

    def test_delete_cancels_hidden_peer_and_waits_for_actual_worker_exit(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        from engine_adapters.recap import _RecapRun
        run = _RecapRun(sid, 0)
        with self.adapter._lock:
            self.adapter._peer_runs[sid]=run
        exited=threading.Event()
        def finish_peer():
            if run.cancelled.wait(1):
                with self.adapter._lock:
                    self.adapter._peer_runs.pop(sid,None)
                exited.set();run.finished.set()
        threading.Thread(target=finish_peer,daemon=True).start()
        with mock.patch.object(self.adapter,'_tombstone_and_purge',return_value={
                'state':'deleted','reason':'user_delete'}):
            self.adapter.delete_session(sid)
        self.assertTrue(exited.wait(1))
        self.assertNotIn(sid,self.adapter.index)

    def test_delete_waits_for_real_peer_async_wrapper_before_purge(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        entered=threading.Event();release=threading.Event();deleted=threading.Event();errors=[]
        def blocked(*_args,**_kwargs):
            entered.set();release.wait(2)
        request={'id':'req_real_lifecycle','target_sid':sid,'accepted_at':'now','status':'running'}
        with mock.patch('peer_mailbox.get',return_value=request), \
             mock.patch('self_update.begin_work',return_value=True), \
             mock.patch('self_update.finish_work'), \
             mock.patch.object(self.adapter,'_run_peer_serial',side_effect=blocked), \
             mock.patch.object(self.adapter,'_tombstone_and_purge',return_value={
                 'state':'deleted','reason':'user_delete'}) as purge:
            self.adapter.peer_prompt_async(sid,'peer',{},peer_request_id=request['id'])
            self.assertTrue(entered.wait(1))
            def remove():
                try:self.adapter.delete_session(sid)
                except Exception as exc:errors.append(exc)
                finally:deleted.set()
            threading.Thread(target=remove,daemon=True).start()
            self.assertFalse(deleted.wait(.1))
            purge.assert_not_called()
            with self.adapter._lock:
                self.assertGreater(self.adapter._peer_blocks.get(sid,0),0)
            with self.assertRaisesRegex(RuntimeError,'正在删除'):
                self.adapter.peer_prompt_async(sid,'late peer',{},peer_request_id=request['id'])
            release.set()
            self.assertTrue(deleted.wait(1))
        self.assertEqual(errors,[])
        purge.assert_called_once_with(sid)

    def test_delete_does_not_purge_when_peer_worker_stays_blocked_outside_turn_lock(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        finished=mock.Mock()
        finished.wait.return_value=False
        from engine_adapters.recap import _RecapRun
        run = _RecapRun(sid, 0)
        run.finished = finished
        with self.adapter._lock:
            self.adapter._peer_runs[sid]=run
        with mock.patch.object(self.adapter,'_tombstone_and_purge') as purge:
            with self.assertRaisesRegex(RuntimeError,'仍在执行或切换'):
                self.adapter.delete_session(sid)
        finished.wait.assert_called_once_with(3)
        purge.assert_not_called()
        self.assertIn(sid,self.adapter.index)

    def test_delete_is_idempotent_after_public_projection_is_gone(self):
        sid=self.adapter.create_session('workflow',opts={'active_engine':'pi'})['id']
        with mock.patch.object(self.adapter,'_tombstone_and_purge',return_value={
                'state':'deleted','reason':'user_delete'}) as purge:
            self.adapter.delete_session(sid)
            self.adapter.delete_session(sid)
        purge.assert_called_once_with(sid)

    def test_real_delete_immediately_removes_history_search_cache(self):
        root=Path(self.tmp.name)
        real=ShepherdAdapter(str(root/'indexed-delete'),str(self.workspace),EventBus())
        sid=real.create_session('workflow',opts={'active_engine':'pi'})['id']
        retrieval=HistoryRetrieval(real.shepherd_root)
        retrieval.sync(sid)
        with retrieval._db() as db:
            self.assertIsNotNone(db.execute('SELECT 1 FROM history_heads WHERE conversation=?',
                                             (sid,)).fetchone())
        real.delete_session(sid)
        with retrieval._db() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM history_heads WHERE conversation=?',
                                         (sid,)).fetchone())
            self.assertEqual(0,db.execute('SELECT count(*) FROM history_docs WHERE conversation=?',
                                          (sid,)).fetchone()[0])

    def test_real_delete_erases_private_content_and_keeps_only_body_free_marker(self):
        root=Path(self.tmp.name)/'physical-delete'
        real=ShepherdAdapter(str(root),str(self.workspace),EventBus())
        sid=real.create_session('workflow',opts={'active_engine':'pi'})['id']
        state=real.service.store.load_raw(sid)
        binding=next(iter(state['bindings'].values()));bid=binding['binding_id']
        secret=b'private-delete-probe-42791'
        shepherd=Path(real.shepherd_root)
        real.requests.claim(sid,'probe-request',{'text':'request without stored body'})
        files=[
            shepherd/'archive'/sid/'probe', shepherd/'inputs'/sid/'probe',
            shepherd/'sources'/bid/'probe.json',
            shepherd/'compact-operations'/(sid+'.json'),
            shepherd/'recovery-events'/(sid+'.json'),
            shepherd/'migrations'/'native-to-unified-v1'/(sid+'.json'),
            shepherd/'native'/'pi'/(bid+'.jsonl'),
            shepherd/'resident'/'writer-locks'/(bid+'.lock'),
        ]
        for path in files:
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(secret)
        for path in [shepherd/'native'/'pi'/bid,
                     shepherd/'resident'/'claude'/bid,
                     shepherd/'resident'/'codex-family'/'pi'/bid,
                     shepherd/'reconciliation-backups'/(sid+'-old')]:
            path.mkdir(parents=True,exist_ok=True);(path/'probe').write_bytes(secret)
        real.delete_session(sid)
        marker=shepherd/'deleted'/(sid+'.json')
        self.assertTrue(marker.is_file())
        self.assertEqual({'conversation_id':sid,'state':'deleted','version':1},
                         json.loads(marker.read_text()))
        self.assertFalse((shepherd/sid).exists())
        self.assertIsNone(real.service.store.load_raw(sid))
        self.assertEqual(real.requests.get(sid,'late')['reason'],'conversation_deleted')
        with self.assertRaisesRegex(Conflict,'已删除'):
            real.requests.claim(sid,'late',{'text':'do not revive'})
        with self.assertRaisesRegex(Conflict,'已删除'):
            real.service.create(sid,'pi',self.workspace)
        survivors=[]
        for path in shepherd.rglob('*'):
            if path.is_file() and path != marker:
                try:data=path.read_bytes()
                except OSError:continue
                if secret in data or bid.encode() in data:
                    survivors.append(str(path.relative_to(shepherd)))
        self.assertEqual([],survivors)

    def test_restart_physically_purges_public_row_already_tombstoned(self):
        root=Path(self.tmp.name)
        real=ShepherdAdapter(str(root/'restart-data'),str(self.workspace),EventBus())
        sid=real.create_session('workflow',opts={'active_engine':'pi'})['id']
        real.service.coordinator.tombstone_for_delete(sid)
        self.assertIn(sid, real.index)
        restored=ShepherdAdapter(str(root/'restart-data'),str(self.workspace),EventBus())
        self.assertNotIn(sid, restored.index)
        self.assertIsNone(restored.service.coordinator.state(sid))
        self.assertFalse((Path(restored.shepherd_root)/sid).exists())
        self.assertTrue((Path(restored.shepherd_root)/'deleted'/(sid+'.json')).is_file())
        self.assertEqual(restored._recovery_reports,[])

    def test_corrupt_cache_is_erased_without_blocking_physical_delete(self):
        root=Path(self.tmp.name)/'corrupt-delete'
        real=ShepherdAdapter(str(root),str(self.workspace),EventBus())
        sid=real.create_session('workflow',opts={'active_engine':'pi'})['id']
        retrieval=HistoryRetrieval(real.shepherd_root);retrieval.sync(sid)
        retrieval.path.write_bytes(b'broken sqlite')
        real.delete_session(sid)
        self.assertNotIn(sid,real.index)
        self.assertFalse(retrieval.path.exists())
        restored=ShepherdAdapter(str(root),str(self.workspace),EventBus())
        self.assertEqual([],restored._history_cleanup_warnings)
        self.assertNotIn(sid,restored.index)

    def test_startup_inflight_recovery_uses_controller_not_direct_scan(self):
        root=Path(self.tmp.name)
        real=ShepherdAdapter(str(root/'startup-recovery'),str(self.workspace),EventBus())
        sid=real.create_session('workflow',opts={'active_engine':'pi'})['id']
        coordinator=real.service.coordinator
        active=real.service.active(sid); lease=active['lease']
        turn=coordinator.begin_round(sid,lease['lease_id'],lease['generation'],
                                     attempt_id='attempt_startup')
        source={'kind':'native_round','engine':'pi',
                'session_id':active['binding']['native_session_id'],
                'entry_id':turn['round_id'],
                'source_key':f'pi:{active["binding"]["native_session_id"]}:{turn["round_id"]}'}
        coordinator.source_persisted(sid,lease['lease_id'],lease['generation'],
            attempt_id=turn['attempt_id'],source=source,native_revision='a'*64,
            terminal_state='completed')
        original_scan=__import__('shepherd.recovery',fromlist=['CrashRecovery']).CrashRecovery.scan
        with mock.patch('shepherd.recovery.CrashRecovery.scan',autospec=True,
                        side_effect=lambda instance, ids=None, **kwargs:
                            original_scan(instance,ids,**kwargs)) as scan, \
             mock.patch('shepherd.recovery_controller.RecoveryController.recover_guarded',
                        return_value={'outcome':'source_persisted'}) as recover:
            ShepherdAdapter(str(root/'startup-recovery'),str(self.workspace),EventBus())
        self.assertFalse(scan.call_args.kwargs.get('recover_inflight',True))
        recover.assert_called_once_with(sid,trigger='startup',retry_count=0)

    def test_restart_finishes_delete_after_public_index_save_failure(self):
        root=Path(self.tmp.name)
        real=ShepherdAdapter(str(root/'retry-data'),str(self.workspace),EventBus())
        sid=real.create_session('workflow',opts={'active_engine':'pi'})['id']
        real.requests.claim(sid,'request_waiting',{})
        persisted=json.loads(Path(real.index_path).read_text())
        self.assertIn(sid,persisted)
        with mock.patch.object(real,'_save_index',side_effect=OSError('disk full')), \
             self.assertRaisesRegex(OSError,'disk full'):
            real.delete_session(sid)
        self.assertIsNone(real.service.coordinator.state(sid))
        self.assertEqual(real.requests.get(sid,'request_waiting')['reason'],'conversation_deleted')
        self.assertIn(sid,json.loads(Path(real.index_path).read_text()))
        restored=ShepherdAdapter(str(root/'retry-data'),str(self.workspace),EventBus())
        self.assertNotIn(sid,restored.index)
        self.assertNotIn(sid,json.loads(Path(restored.index_path).read_text()))
        self.assertEqual(restored.requests.get(sid,'request_waiting')['reason'],'conversation_deleted')

    def test_worker_is_bound_to_public_store(self):
        worker=mock.Mock(); worker.name='pi'
        worker.index={};worker._lock=mock.Mock();worker._live={};worker._turn_seq={};worker._turn_prev={}
        worker._msg_cache={};worker._msg_cache_lock=mock.Mock();worker._compact_state={};worker._bg_tasks={}
        self.adapter._bind_worker(worker)
        self.assertIs(worker.owner,self.adapter)
        self.assertIs(worker.index,self.adapter.index)
        self.assertIs(worker._live,self.adapter._live)
        self.assertEqual(worker.dir,self.adapter.dir)
        worker._set_engine_sid('public', 'native-secret')
        worker._term_register('public', 'native-secret')
        self.assertNotIn('engine_sid', self.adapter.index.get('public', {}))


if __name__=='__main__':unittest.main()
