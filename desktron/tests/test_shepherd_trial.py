"""Trial UI routes, data ownership and execution remain independently switchable."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import contextlib
from unittest import mock

from engine_adapters.bus import EventBus
from engine_adapters.shepherd import ShepherdAdapter, ShepherdTrialAdapter
from shepherd.trial import set_policy, policy
from shepherd.compact_operations import CompactOperations
from shepherd.store import Conflict
from shepherd.harness import ExecutionHarness
from test_shepherd_unified_storage import JournalRunner
import test_shepherd_mode as _mode


def snapshot(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(root).rglob('*') if p.is_file()}


class TrialIsolation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.home=self.root/'data';self.home.mkdir()
        self.workspace=self.root/'workspace';self.workspace.mkdir()
        set_policy(self.home,create=True,execute=True)
        self.regular=ShepherdAdapter(self.home,self.workspace,EventBus())
        self.trial=ShepherdTrialAdapter(self.home,self.workspace,EventBus())
        self.addCleanup(self.regular.shutdown);self.addCleanup(self.trial.shutdown)

    def new_trial(self):
        return self.trial.create_session(opts={'active_engine':'pi'})['id']

    def test_new_sessions_and_private_workers_are_separate(self):
        sid=self.new_trial()
        self.assertTrue(sid.startswith('adp_trial_'))
        self.assertTrue(self.trial.index[sid]['shepherd_trial'])
        self.assertEqual(self.regular.service.storage,'unified')
        self.assertEqual(self.trial.service.storage,'unified')
        self.assertNotEqual(self.regular.dir,self.trial.dir)
        self.assertNotIn(sid,self.regular.index)
        self.assertIsNot(self.regular._worker('pi')._live,self.trial._worker('pi')._live)
        before=snapshot(self.home/'adapter')
        with self.assertRaises(Conflict):self.regular.prompt_async(sid,'wrong space')
        with self.assertRaises(Conflict):self.regular.abort(sid)
        with self.assertRaises(Conflict):self.regular.delete_session(sid)
        with self.assertRaises(Conflict):self.regular.recover_pending(sid)
        with self.assertRaises(Conflict):self.trial.prompt_async('adp_regular','wrong space')
        with self.assertRaises(Conflict):self.trial._apply_requested_engine('adp_regular')
        self.assertEqual(snapshot(self.home/'adapter'),before)
        self.assertEqual(self.regular._shepherd_approvals,{})
        self.assertFalse(self.regular.respond_permission('trial-only',True))

    def test_pause_reopen_and_delete_preserve_other_namespace_and_erase_archive(self):
        sid=self.new_trial();active=self.trial.service.active(sid)
        connector=self.trial.service.connector(active['binding']);lease=active['lease']
        row=ExecutionHarness(self.trial.service.coordinator,connector,JournalRunner(connector)).run(
            sid,lease_id=lease['lease_id'],generation=lease['generation'],prompt='fixture',
            input_public=[{'type':'message','role':'user','content':[{'type':'input_text','text':'fixture'}]}])
        expected=connector.resolve_source(row['source'])
        self.trial._append_msg(sid,{'info':{'id':'fixture','role':'user','seq':1},
                                    'parts':[{'type':'text','text':'fixture'}]})
        archive_before=snapshot(connector.archive.directory)
        ordinary_before=snapshot(self.home/'adapter')
        set_policy(self.home,create=False,execute=False)
        with self.assertRaises(Conflict):self.new_trial()
        with self.assertRaises(Conflict):self.trial.prompt_async(sid,'paused')
        with self.assertRaises(Conflict):self.trial._turn_dispatch({'sid':sid},None,{},None)
        with self.assertRaises(Conflict):self.trial._apply_requested_engine(sid)
        reopened=ShepherdTrialAdapter(self.home,self.workspace,EventBus());self.addCleanup(reopened.shutdown)
        self.assertTrue(reopened.list_sessions())
        self.assertEqual(reopened.messages(sid)[0]['parts'][0]['text'],'fixture')
        rebuilt=reopened.service.connector(reopened.service.active(sid)['binding'])
        self.assertEqual(rebuilt.resolve_source(row['source']),expected)
        self.assertEqual(snapshot(connector.archive.directory),archive_before)
        self.assertEqual(snapshot(self.home/'adapter'),ordinary_before)
        set_policy(self.home,create=False,execute=True)
        reopened._require_execution(sid)
        with self.assertRaises(Conflict):reopened.create_session(opts={'active_engine':'pi'})
        reopened.delete_session(sid)
        self.assertNotIn(sid,reopened.index)
        self.assertEqual(snapshot(self.home/'adapter'),ordinary_before)
        self.assertEqual(snapshot(connector.archive.directory),{})
        marker=Path(reopened.shepherd_root)/'deleted'/(sid+'.json')
        self.assertTrue(marker.is_file())
        self.assertEqual(set(json.loads(marker.read_text(encoding='utf-8'))),
                         {'version','conversation_id','state'})

    def test_malformed_policy_pauses_and_compatibility_defaults_are_closed(self):
        (self.home/'shepherd-trial-policy.json').write_text('{bad', encoding='utf-8')
        self.assertFalse(policy(self.home)['execute'])
        with self.assertRaises(Conflict):self.new_trial()
        (self.home/'shepherd-trial-policy.json').unlink()
        with mock.patch('shepherd.trial.TRIAL_DEFAULT_ENABLED',False):
            self.assertEqual(policy(self.home),{'create':False,'execute':False})

    def test_manual_compact_is_a_product_command_without_grounding(self):
        sid=self.new_trial();captured={}
        def dispatch(ctx,*_args):
            captured.update(text=ctx['text'], user=ctx['_user_text'])
        with mock.patch.object(self.trial,'_turn_dispatch',side_effect=dispatch):
            self.trial._run_serial(sid,'/compact',self.trial.session_options(sid,{}),[])
        self.assertEqual(captured,{'text':'/compact','user':'/compact'})

    def test_manual_compact_async_acknowledges_before_terminal(self):
        sid=self.new_trial();entered=threading.Event();release=threading.Event();result=[]
        def slow(*_args,**_kwargs):
            entered.set();release.wait(3);return {'ok':True,'result':'success'}
        with mock.patch.object(self.trial,'compact_session',side_effect=slow):
            started=time.monotonic()
            got=self.trial.compact_session_async(sid,'compact_mobile')
            self.assertLess(time.monotonic()-started,.5)
            self.assertTrue(got['accepted']);self.assertEqual(got['operation_id'],'compact_mobile')
            self.assertTrue(entered.wait(1))
            state=self.trial.turn_state(sid)
            self.assertEqual(state['compact']['operation_id'],'compact_mobile')
            self.assertTrue(state['compact']['running'])
            same=self.trial.compact_session_async(sid,'compact_mobile')
            self.assertTrue(same['accepted'])
            other=self.trial.compact_session_async(sid,'compact_other')
            self.assertFalse(other['ok'])
            release.set()

    def test_manual_compact_restart_never_replays_and_settles_existing_evidence(self):
        sid=self.new_trial();state=self.trial.service.coordinator.state(sid)
        before=self.trial._history_seq(state)
        CompactOperations(self.trial.shepherd_root,'old-boot').claim(
            sid,'compact_restart',before_seq=before)
        restarted=ShepherdTrialAdapter(self.home,self.workspace,EventBus())
        self.addCleanup(restarted.shutdown)
        with mock.patch.object(restarted,'compact_session') as replay:
            got=restarted.compact_status(sid,'compact_restart')
        replay.assert_not_called()
        self.assertFalse(got['running']);self.assertEqual(got['result'],'failed')
        self.assertIn('重启前',got['error'])

    def test_manual_compact_restart_settles_committed_turn_without_replay(self):
        sid=self.new_trial();state=self.trial.service.coordinator.state(sid)
        before=self.trial._history_seq(state)
        old=CompactOperations(self.trial.shepherd_root,'old-boot')
        old.claim(sid,'compact_committed',before_seq=before)
        active=self.trial.service.active(sid);connector=self.trial.service.connector(active['binding'])
        ExecutionHarness(self.trial.service.coordinator,connector,JournalRunner(connector)).run(
            sid,lease_id=active['lease']['lease_id'],generation=active['lease']['generation'],
            prompt='/compact',input_public=[{'type':'message','role':'user','content':[
                {'type':'input_text','text':'/compact'}]}])
        restarted=ShepherdTrialAdapter(self.home,self.workspace,EventBus())
        self.addCleanup(restarted.shutdown)
        with mock.patch.object(restarted,'compact_session') as replay:
            got=restarted.compact_status(sid,'compact_committed')
        replay.assert_not_called()
        self.assertFalse(got['running']);self.assertEqual(got['result'],'success')

    def test_restart_migrates_legacy_compaction_permission_from_durable_success(self):
        sid=self.new_trial();state=self.trial.service.coordinator.state(sid)
        before=self.trial._history_seq(state)
        old=CompactOperations(self.trial.shepherd_root,'old-boot')
        old.claim(sid,'compact_legacy_notice',before_seq=before)
        active=self.trial.service.active(sid);connector=self.trial.service.connector(active['binding'])
        ExecutionHarness(self.trial.service.coordinator,connector,JournalRunner(connector)).run(
            sid,lease_id=active['lease']['lease_id'],generation=active['lease']['generation'],
            prompt='/compact',input_public=[{'type':'message','role':'user','content':[
                {'type':'input_text','text':'/compact'}]}])
        self.trial._env_publish(sid,{'stage':'permission','state':'error',
            'label':'Pi 权限闸异常','engine':'pi','provider':'openai',
            'startedAt':1,'observedAt':1,
            'error':{'source':'engine','code':'PERMISSION_EXTENSION_ERROR',
                     'message':'Compaction summary failed: WebSocket error'}})
        self.assertIn('permission',self.trial.index[sid]['environment_issues'])
        restarted=ShepherdTrialAdapter(self.home,self.workspace,EventBus())
        self.addCleanup(restarted.shutdown)
        self.assertNotIn('permission',restarted.index[sid]['environment_issues'])
        self.assertEqual(restarted.compact_status(
            sid,'compact_legacy_notice')['result'],'success')

    def test_manual_compact_native_state_cannot_overwrite_operation_receipt(self):
        sid=self.new_trial();self.trial.compact_operations.claim(sid,'compact_owned',before_seq=0)
        self.trial._compact_threads['compact_owned']=None
        self.trial._compact_state[sid]={'running':False,'result':'cancelled','started':9999999999999}
        state=self.trial.turn_state(sid)
        self.assertEqual(state['compact']['operation_id'],'compact_owned')
        self.assertTrue(state['compact']['running'])
        with self.assertRaises(Conflict):
            self.trial._before_turn_start(sid)

    def test_manual_compact_reports_only_a_committed_completed_checkpoint(self):
        adapter=object.__new__(ShepherdTrialAdapter)
        adapter._live={};adapter._compact_state={};adapter.bus=mock.Mock()
        adapter._require_execution=mock.Mock();adapter.compact_available=mock.Mock(return_value=True)
        adapter.session_options=mock.Mock(return_value={'model':'fixture'})
        adapter._persist_manual_compact_marker=mock.Mock()
        adapter.service=mock.Mock()
        adapter.service.coordinator.state.side_effect=[
            {'turns':[]}, {'turns':[{'outcome':'completed'}]}]
        adapter._run_serial=mock.Mock()
        with mock.patch('self_update.tracked_work',return_value=contextlib.nullcontext(True)):
            result=adapter.compact_session('adp_trial_fixture')
        self.assertTrue(result['ok'])
        adapter._run_serial.assert_called_once_with(
            'adp_trial_fixture','/compact',{'model':'fixture'},[])
        adapter._persist_manual_compact_marker.assert_called_once()
        events=[call.args[0] for call in adapter.bus.publish.call_args_list]
        self.assertTrue(events[0]['properties']['running'])
        self.assertEqual(events[-1]['properties']['result'],'success')

    def test_manual_compact_marker_persists_after_command_and_is_idempotent(self):
        sid=self.new_trial()
        seq=123
        self.trial._append_msg(sid,{'info':{'id':'command','role':'user','seq':seq,'at':seq},
            'parts':[{'type':'text','text':'/compact'}]})
        self.trial._persist_manual_compact_marker(sid,seq)
        self.trial._persist_manual_compact_marker(sid,seq)
        messages=self.trial.messages(sid)
        self.assertEqual(len(messages),2)
        self.assertEqual(messages[0]['parts'][0]['text'],'/compact')
        self.assertEqual(messages[1]['info']['compact'],{'trigger':'manual'})

    def test_manual_compact_cancel_uses_new_ledger_terminal_not_cancel_request(self):
        for outcome in ('interrupted','failed','unknown'):
            with self.subTest(outcome=outcome):
                adapter=object.__new__(ShepherdTrialAdapter)
                adapter._live={};adapter._compact_state={};adapter.bus=mock.Mock()
                adapter._require_execution=mock.Mock()
                adapter.compact_available=mock.Mock(return_value=True)
                adapter.session_options=mock.Mock(return_value={})
                adapter._persist_manual_compact_marker=mock.Mock()
                adapter.service=mock.Mock()
                adapter.service.coordinator.state.side_effect=[
                    {'turns':[]}, {'turns':[{'outcome':outcome}]}]
                adapter._run_serial=mock.Mock()
                with mock.patch('self_update.tracked_work',return_value=contextlib.nullcontext(True)):
                    result=adapter.compact_session('adp_trial_fixture')
                self.assertEqual(result['result'], 'cancelled' if outcome=='interrupted' else 'failed')
                self.assertEqual(result['ok'],outcome=='interrupted')
                adapter._persist_manual_compact_marker.assert_not_called()

    def test_marker_write_failure_stays_verifying_and_query_retries_success(self):
        adapter=object.__new__(ShepherdTrialAdapter)
        adapter._live={};adapter._compact_state={};adapter._compact_threads={}
        adapter._lock=threading.RLock();adapter.bus=mock.Mock()
        adapter._compact_local=threading.local()
        adapter._require_execution=mock.Mock();adapter.compact_available=mock.Mock(return_value=True)
        adapter.session_options=mock.Mock(return_value={})
        adapter.service=mock.Mock()
        committed={'turn_id':'adp_trial_fixture:1','outcome':'completed'}
        adapter.service.coordinator.state.side_effect=[{'turns':[]},{'turns':[committed]}]
        adapter._run_serial=mock.Mock()
        adapter.compact_operations=CompactOperations(self.trial.shepherd_root,'marker-boot')
        adapter.compact_operations.claim('adp_trial_fixture','compact_marker',before_seq=0)
        adapter._persist_manual_compact_marker=mock.Mock(side_effect=[OSError('disk'),None])
        with mock.patch('self_update.tracked_work',return_value=contextlib.nullcontext(True)):
            pending=adapter.compact_session('adp_trial_fixture',started=1,admitted=True,
                                            operation_id='compact_marker')
        self.assertTrue(pending['running'])
        self.assertEqual(adapter.compact_operations.get(
            'adp_trial_fixture','compact_marker')['status'],'verifying')
        adapter.service.coordinator.state.side_effect=None
        adapter.service.coordinator.state.return_value={'turns':[committed]}
        done=adapter.compact_status('adp_trial_fixture','compact_marker')
        self.assertFalse(done['running']);self.assertEqual(done['result'],'success')
        self.assertEqual(adapter._persist_manual_compact_marker.call_count,2)

    def test_previous_interruption_cannot_confirm_new_compaction_cancel(self):
        adapter=object.__new__(ShepherdTrialAdapter)
        adapter._live={};adapter._compact_state={};adapter.bus=mock.Mock()
        adapter._require_execution=mock.Mock()
        adapter.compact_available=mock.Mock(return_value=True)
        adapter.session_options=mock.Mock(return_value={})
        adapter._persist_manual_compact_marker=mock.Mock()
        adapter.service=mock.Mock()
        adapter.service.coordinator.state.return_value={'turns':[{'outcome':'interrupted'}]}
        adapter._run_serial=mock.Mock()
        with mock.patch('self_update.tracked_work',return_value=contextlib.nullcontext(True)):
            result=adapter.compact_session('adp_trial_fixture')
        self.assertFalse(result['ok'])
        adapter._persist_manual_compact_marker.assert_not_called()

    def test_manual_compact_rejects_non_checkpoint_engine_before_running(self):
        adapter=object.__new__(ShepherdTrialAdapter)
        adapter._require_execution=mock.Mock();adapter.compact_available=mock.Mock(return_value=False)
        adapter._run_serial=mock.Mock()
        result=adapter.compact_session('adp_trial_fixture')
        self.assertFalse(result['ok']);adapter._run_serial.assert_not_called()


class TrialRoutes(unittest.TestCase):
    setUpClass = classmethod(_mode.BackendContract.setUpClass.__func__)
    handler = classmethod(_mode.BackendContract.handler.__func__)
    def test_trial_creation_and_sid_routing_are_explicit(self):
        import ui_contract
        ordinary=mock.Mock();ordinary.index={'adp_normal':{}}
        trial=mock.Mock();trial.index={'adp_trial_a':{}}
        trial.create_session.return_value={'id':'adp_trial_new','shepherd_mode':True,'shepherd_trial':True}
        with mock.patch.object(ui_contract,'_shepherd',return_value=ordinary),mock.patch.object(ui_contract,'_shepherd_trial',return_value=trial):
            self.assertIs(ui_contract.UiContractMixin._adapter_owning('adp_normal'),ordinary)
            self.assertIs(ui_contract.UiContractMixin._adapter_owning('adp_trial_a'),trial)
            h=self.handler({'engine':'pi','shepherd_mode':True,'shepherd_trial':True},contract=True)
            with mock.patch.object(ui_contract,'_engine_pref',return_value='pi'):
                result=h._adapter_route('POST')
            self.assertEqual(result[0]['id'],'adp_trial_new')
            ordinary.create_session.assert_not_called()

    def test_unknown_trial_never_falls_back_to_preferred_engine(self):
        import ui_contract
        trial=mock.Mock();trial.index={}
        with mock.patch.object(ui_contract,'_shepherd_trial',return_value=trial),mock.patch.object(ui_contract,'_adapter') as native:
            self.assertIsNone(ui_contract.UiContractMixin._adapter_owning('adp_trial_missing'))
            native.assert_not_called()
            h=self.handler(contract=True,path='/session/adp_trial_missing/abort')
            result=h._adapter_route('POST')
            self.assertEqual(result[1],404)
            native.return_value.abort.assert_not_called()

    def test_trial_listing_failure_keeps_ordinary_rows(self):
        import ui_contract
        ordinary=mock.Mock();ordinary.list_sessions.return_value=[{'id':'adp_normal','engine':'pi'}]
        with mock.patch.object(ui_contract,'_shepherd',return_value=ordinary),mock.patch.object(ui_contract,'_shepherd_trial',side_effect=ValueError('bad index')),mock.patch.object(ui_contract,'GUI_ENGINES',()),mock.patch.object(ui_contract.UiContractMixin,'_session_belonging',side_effect=dict):
            rows=ui_contract.UiContractMixin._sessions_listing()
            self.assertEqual([r['id'] for r in rows],['adp_normal'])
