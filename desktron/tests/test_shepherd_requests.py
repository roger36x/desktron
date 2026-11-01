import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from shepherd.requests import Requests
from shepherd import Conflict
from test_shepherd_product import ShepherdFacade
from test_peer_message_route import Adapter, Handler, ui_contract


class ReceiptStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Requests(self.tmp.name, 'boot1')

    def test_concurrent_duplicate_is_admitted_once_and_no_body_is_stored(self):
        outcomes=[]
        def claim(): outcomes.append(self.store.claim('s','r',{'text':'private body'})[1])
        threads=[threading.Thread(target=claim) for _ in range(8)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(sum(outcomes),1)
        self.assertNotIn('private body',Path(self.store.path('s')).read_text())
        with self.assertRaises(Conflict):self.store.claim('s','r',{'text':'different'})

    def test_cancel_before_late_post_prevents_dispatch(self):
        self.assertEqual(self.store.cancel('s','r',only_before_start=True)['status'],'cancelled')
        self.assertFalse(self.store.claim('s','r',{'text':'late'})[1])
        with self.assertRaises(Conflict):self.store.update('s','r','started')

    def test_retry_proof_does_not_replay_user_cancellation(self):
        first = self.store.cancel('s', 'r', only_before_start=True)
        self.assertIs(first['retryable'], True)
        self.assertNotIn('retryable', self.store.get('s', 'r'))
        self.assertFalse(self.store.cancel('s', 'r', only_before_start=True).get('retryable'))
        self.store.claim('s2', 'user', {})
        self.store.cancel('s2', 'user')
        self.assertFalse(self.store.cancel('s2', 'user', only_before_start=True).get('retryable'))

    def test_recovered_retry_proof_is_consumed_once(self):
        self.store.claim('s','r',{})
        new = Requests(self.tmp.name, 'new_boot')
        new.recover('s', {})
        outcomes = []
        def claim(): outcomes.append(new.cancel('s','r',only_before_start=True).get('retryable'))
        threads = [threading.Thread(target=claim) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(outcomes.count(True), 1)

    def test_open_receipt_refusal_distinguishes_pending_from_uncertain(self):
        from shepherd.store import RequestBusy, RequestPending
        self.store.claim('s','r',{})
        for status in ('accepted','preparing','started'):
            self.store.update('s','r',status)
            with self.assertRaises(RequestPending):self.store.claim('s','next',{})
        self.store.update('s','r','uncertain')
        with self.assertRaises(RequestBusy) as caught:self.store.claim('s','next',{})
        self.assertNotIsInstance(caught.exception, RequestPending)
        self.assertEqual(caught.exception.code, 'session_busy')

    def test_cancel_unsent_cannot_interrupt_started_request(self):
        self.store.claim('s','r',{})
        self.store.update('s','r','started')
        self.assertEqual(self.store.cancel('s','r',only_before_start=True)['status'],'started')
        self.assertFalse(self.store.cancelled('s','r'))

    def test_restart_rejects_unstarted_but_never_replays_uncertain(self):
        self.store.claim('s','r',{})
        newer=Requests(self.tmp.name,'boot2');newer.recover('s',{})
        self.assertEqual(newer.get('s','r')['status'],'rejected')
        self.assertIs(newer.get('s','r')['retryable'], True)
        newer.cancel('s','r')
        self.assertFalse(newer.get('s','r').get('retryable'))
        newer.claim('s','r2',{});newer.update('s','r2','started',attempt_id='a')
        newest=Requests(self.tmp.name,'boot3');newest.recover('s',{})
        self.assertEqual(newest.get('s','r2')['status'],'uncertain')
        self.assertFalse(newest.claim('s','r2',{})[1])
        newest.recover('s',{'rounds':[{'attempt_id':'a','terminal_state':'completed'}]})
        self.assertEqual(newest.get('s','r2')['status'],'finished')

    def test_explicit_reconciliation_releases_only_matching_request(self):
        self.store.claim('s','r',{})
        self.store.update('s','r','uncertain',attempt_id='a')
        self.store.finish_reconciliation('s','other')
        with self.assertRaises(Conflict): self.store.claim('s','new',{})
        self.store.finish_reconciliation('s','a')
        self.assertTrue(self.store.claim('s','new',{})[1])

    def test_crash_between_request_link_and_reservation_does_not_permanently_block(self):
        from shepherd import Coordinator, Store
        coordinator=Coordinator(Store(Path(self.tmp.name)/'ledger'))
        coordinator.create('s')
        self.store.claim('s','r',{})
        self.store.update('s','r','started',attempt_id='a')
        newer=Requests(self.tmp.name,'boot2')
        newer.recover('s',coordinator.state('s'))
        self.assertEqual(newer.get('s','r'), {'request_id':'r','status':'finished','reason':'failed_before_dispatch'})
        self.assertTrue(newer.claim('s','new',{})[1])

    def test_reserved_cancellation_receipt_is_not_left_uncertain(self):
        self.store.claim('s','r',{})
        self.store.update('s','r','started',attempt_id='a')
        Requests(self.tmp.name,'boot2').recover('s',{'reconciliations':[
            {'action':'cancel_before_dispatch','inflight':{'attempt_id':'a'}}]})
        self.assertEqual(self.store.get('s','r')['reason'], 'cancel_before_dispatch')

    def test_tombstone_rejects_claimed_request_without_turn(self):
        self.store.claim('s','r',{})
        self.store.settle('s','r',{'conversation_id':'s','tombstone':{
            'state':'deleted','reason':'user_delete','deleted_at':1}})
        self.assertEqual(self.store.get('s','r'), {
            'request_id':'r','status':'rejected','reason':'conversation_deleted'})

    def test_deleted_sentinel_rejects_late_claim_across_restart(self):
        self.store.claim('s','r',{})
        self.store.reject_deleted('s')
        with self.assertRaisesRegex(Conflict,'已删除'):
            self.store.claim('s','late',{})
        restored=Requests(self.tmp.name,'boot2')
        with self.assertRaisesRegex(Conflict,'已删除'):
            restored.claim('s','after_restart',{})
        self.assertEqual(restored.get('s','r'), {
            'request_id':'r','status':'rejected','reason':'conversation_deleted'})


class Admission(unittest.TestCase):
    setUp=ShepherdFacade.setUp

    def test_busy_send_returns_409_without_new_receipt_or_dispatch(self):
        ad = self.adapter
        for status in ('accepted', 'preparing', 'started', 'uncertain'):
            with self.subTest(status=status):
                sid = ad.create_session('workflow', opts={'active_engine': 'pi'})['id']
                ad.requests.claim(sid, 'outstanding', {})
                ad.requests.update(sid, 'outstanding', status)
                before = Path(ad.requests.path(sid)).read_bytes()
                # Match desk-msg send: no peer_request_id/client_request_id.
                h = Handler(sid, {'parts': [{'type': 'text', 'text': 'notification',
                                            'peer_head': 'fixture source'}]})
                with mock.patch.object(Handler, '_adapter_owning', return_value=ad), \
                     mock.patch.object(ui_contract, '_engine_pref', return_value='pi'), \
                     mock.patch.object(ui_contract, '_wb_state_read', return_value={}), \
                     mock.patch.object(ad, 'execution_options', return_value={}), \
                     mock.patch.object(ad, 'recover_pending', return_value={'busy': True}), \
                     mock.patch.object(ad, '_accept_prompt') as accept:
                    h._adapter_route('POST')
                self.assertEqual(h.code, 409)
                # Only an uncertain receipt needs verification; the others clear
                # on their own, so clients may hold the message and resend.
                self.assertEqual(h.out['code'], 'session_busy' if status == 'uncertain'
                                 else 'request_pending')
                self.assertIn('消息未接收', h.out['error'])
                accept.assert_not_called()
                self.assertEqual(Path(ad.requests.path(sid)).read_bytes(), before)

    def test_legacy_peer_prompt_bypasses_ordinary_request_ledger(self):
        ad=self.adapter;sid=ad.create_session('workflow',opts={'active_engine':'pi'})['id']
        with mock.patch.object(ad.requests,'claim') as claim, \
             mock.patch('engine_adapters.cli_turn.TurnRunnerMixin.prompt_async') as sidecar:
            ad.prompt_async(sid,'peer question',peer_request_id='pm_'+'a'*32,
                            peer_head='source')
        claim.assert_not_called()
        sidecar.assert_called_once()
        self.assertEqual(sidecar.call_args.kwargs['peer_request_id'],'pm_'+'a'*32)

    def test_switch_engine_waits_for_peer_snapshot_before_selection_mutation(self):
        ad=self.adapter;sid=ad.create_session('workflow',opts={'active_engine':'pi'})['id']
        with mock.patch.object(ad,'_peer_visible_operation_admit',return_value=True) as admit, \
             mock.patch.object(ad,'_peer_user_release') as release, \
             mock.patch.object(ad,'_schedule_engine_prepare'), \
             mock.patch.object(ad,'refresh_environment'):
            ad.switch_engine(sid,'claude')
        admit.assert_called_once_with(
            sid, '目标会话切换引擎前等待隔离问询冻结上下文')
        release.assert_called_once_with(sid)
        self.assertEqual(ad.selected_engine(sid),'claude')

    def test_duplicate_does_not_switch_or_spawn_twice(self):
        ad=self.adapter;sid=ad.create_session('workflow',opts={'active_engine':'pi'})['id']
        with mock.patch('engine_adapters.cli_turn.TurnRunnerMixin.prompt_async') as spawn, \
             mock.patch.object(ad,'_switch_engine_fenced') as switch:
            ad.prompt_async(sid,'hello',client_request_id='r',requested_engine='claude')
            ad.prompt_async(sid,'hello',client_request_id='r',requested_engine='claude')
            self.assertEqual(spawn.call_count,1)
            callback=spawn.call_args.kwargs['_before_dispatch'];callback()
            self.assertEqual(switch.call_count,1)

    def test_requested_engine_reuses_prompt_fence_without_self_conflict(self):
        ad=self.adapter;sid=ad.create_session('workflow',opts={'active_engine':'pi'})['id']
        with mock.patch('engine_adapters.cli_turn.TurnRunnerMixin.prompt_async') as spawn, \
             mock.patch.object(ad,'_schedule_engine_prepare'), \
             mock.patch.object(ad,'refresh_environment'):
            ad.prompt_async(sid,'hello',client_request_id='route',requested_engine='claude')
            spawn.assert_called_once()
            spawn.call_args.kwargs['_before_dispatch']()
            self.assertEqual(ad.selected_engine(sid),'claude')
        with ad._lock:
            self.assertNotIn(sid,ad._peer_blocks)

    def test_requested_engine_waits_for_active_ask_snapshot(self):
        from engine_adapters.recap import _RecapRun
        ad=self.adapter;sid=ad.create_session('workflow',opts={'active_engine':'pi'})['id']
        peer=_RecapRun(sid,0,request_id='pm_'+'a'*32)
        with ad._lock:
            ad._peer_runs[sid]=peer
        started=threading.Event()
        try:
            with mock.patch.object(ad,'_schedule_engine_prepare'), \
                 mock.patch.object(ad,'_schedule_storage_promotion'), \
                 mock.patch.object(ad,'refresh_environment'), \
                 mock.patch.object(ad,'_run',side_effect=lambda *a,**k: started.set()), \
                 mock.patch('self_update.begin_work',return_value=True), \
                 mock.patch('self_update.finish_work'):
                receipt=ad.prompt_async(sid,'hello',client_request_id='wait-route',
                                        requested_engine='claude')
                self.assertEqual(receipt['status'],'accepted')
                self.assertEqual(ad.selected_engine(sid),'pi')
                peer.mark_prepared()
                # The dispatch thread missed a 1 s window on a loaded Windows runner (run 37669956043).
                self.assertTrue(started.wait(10))
                self.assertEqual(ad.selected_engine(sid),'claude')
                deadline=time.time()+10
                while (ad.requests.get(sid,'wait-route')['status'] in
                       ('accepted','preparing','started','uncertain') and time.time()<deadline):
                    time.sleep(.01)
        finally:
            with ad._lock:
                ad._peer_runs.pop(sid,None)
            peer.finished.set()

    def test_cancel_accepted_request_cannot_run_after_switch_preparation(self):
        ad=self.adapter;sid=ad.create_session('workflow',opts={'active_engine':'pi'})['id']
        ad.requests.claim(sid,'r',{})
        with mock.patch('pi_children.cancel_session') as children:
            ad.abort(sid,client_request_id='r')
        children.assert_called_once_with(ad.dir,sid,suppress_pending=True)
        with mock.patch('engine_adapters.cli_turn.TurnRunnerMixin._run') as run:
            with self.assertRaises(Conflict):ad._run_serial(sid,'hello',client_request_id='r')
        run.assert_not_called()

    def test_worker_resolution_keeps_native_and_other_worker_asks(self):
        ad=self.adapter;sid=ad.create_session('workflow',opts={'active_engine':'pi'})['id']
        captured=[]
        class Worker:
            def __init__(self, asks, handles=False): self.asks=asks;self.handles=handles
            def _pending_ask(self, target_sid):
                return [dict(item) for item in self.asks if item['sessionID']==target_sid]
            def respond_permission(self, request_id, allow, answers=None, response='',
                                   *, resolution='', asks_provider=None):
                if not self.handles:return False
                self.asks=[]
                captured.extend(asks_provider(sid))
                return True
        target=Worker([{'id':'target','sessionID':sid}],True)
        other=Worker([{'id':'other','sessionID':sid}])
        ad._workers={'claude':target,'pi':other}
        native_done=threading.Event()
        with ad._shepherd_approval_lock:
            ad._shepherd_approvals['native']={
                'done':native_done,'result':None,'sid':sid,
                'props':{'id':'native','sessionID':sid,'tool':'Bash','input':{}}}
        self.assertTrue(ad.respond_permission('target',True))
        self.assertEqual({item['id'] for item in captured},{'native','other'})

    def test_native_resolution_survives_worker_snapshot_failure(self):
        ad=self.adapter;sid=ad.create_session('workflow',opts={'active_engine':'pi'})['id']
        class BrokenWorker:
            def _pending_ask(self, _sid): raise RuntimeError('broken snapshot')
        ad._workers={'broken':BrokenWorker()}
        done=threading.Event()
        with ad._shepherd_approval_lock:
            ad._shepherd_approvals['native']={
                'done':done,'result':None,'sid':sid,
                'props':{'id':'native','sessionID':sid,'tool':'Bash','input':{}}}
        self.assertTrue(ad.respond_permission('native',True))
        self.assertTrue(done.is_set())

    def test_worker_internal_timeout_uses_shepherd_global_asks(self):
        ad=self.adapter;sid=ad.create_session('workflow',opts={'active_engine':'claude'})['id']
        events=ad.bus.subscribe();worker=ad._worker('claude')
        proc=mock.Mock();proc.poll.return_value=1
        with worker._plock:
            worker._pending['target']=proc
            worker._pending_input['target']={}
            worker._pending_ask_meta['target']={
                'id':'target','sessionID':sid,'tool':'AskUserQuestion','input':{}}
        native_done=threading.Event()
        with ad._shepherd_approval_lock:
            ad._shepherd_approvals['native']={
                'done':native_done,'result':None,'sid':sid,
                'props':{'id':'native','sessionID':sid,'tool':'Bash','input':{}}}
        self.assertFalse(worker.respond_permission('target',False,resolution='timeout'))
        event=events.get(timeout=1)
        self.assertEqual(event['type'],'permission.resolved')
        self.assertEqual({item['id'] for item in event['properties']['asks']},{'native'})

    def test_shutdown_rejects_new_messages_and_does_not_start_engines(self):
        ad=self.adapter;sid=ad.create_session('workflow',opts={'active_engine':'pi'})['id']
        ad.requests.claim(sid,'r',{})
        ad.shutdown(timeout=0)
        with self.assertRaises(Conflict): ad.prompt_async(sid,'new',client_request_id='new')
        with mock.patch('engine_adapters.cli_turn.TurnRunnerMixin._run') as run:
            with self.assertRaises(Conflict): ad._run_serial(sid,'accepted',client_request_id='r')
        run.assert_not_called()
        self.assertEqual(ad.requests.get(sid,'r')['status'],'rejected')

    def test_claimed_request_waiting_for_guard_is_rejected_after_delete(self):
        ad=self.adapter;sid=ad.create_session('workflow',opts={'active_engine':'pi'})['id']
        ad.requests.claim(sid,'r',{})
        tombstone={'state':'deleted','reason':'user_delete','deleted_at':1}
        ad.service.coordinator=mock.Mock()
        ad.service.coordinator.state.return_value={
            'version':2,'conversation_id':sid,'tombstone':tombstone}
        with mock.patch('engine_adapters.cli_turn.TurnRunnerMixin._run') as run, \
             self.assertRaisesRegex(Conflict,'已删除'):
            ad._run_serial(sid,'accepted',client_request_id='r')
        run.assert_not_called()
        self.assertEqual(ad.requests.get(sid,'r'), {
            'request_id':'r','status':'rejected','reason':'conversation_deleted'})


class ReadyBoundary(unittest.TestCase):
    setUp = ShepherdFacade.setUp

    def prepare(self, outcome='completed'):
        from types import SimpleNamespace
        ad = self.adapter
        sid = ad.create_session('workflow', opts={'active_engine':'pi'})['id']
        state = {'conversation_id':sid, 'turns':[], 'active_turn':None}
        ad.service.coordinator = SimpleNamespace(state=lambda _:state)
        ad.requests.claim(sid, 'first', {})
        def run(*_args):
            ad.requests.update(sid, 'first', 'started', attempt_id='a1')
            state['turns'] = [{'attempt_id':'a1', 'turn_id':'turn1', 'outcome':outcome}]
            now = int(time.time() * 1000)
            ctx = {'sid':sid, 'seq':now, 'msg_id':'assistant', 'parts':{}, 'order':[],
                   'source':'user', 't0':now, 'client_request_id':'first',
                   'native_terminal':{'status':outcome, 'source':'pi'}}
            ad._live[sid] = ctx
            ad._finish_turn(sid, ctx, 'fixture failure' if outcome == 'failed' else None)
        return ad, sid, state, run

    def run_fixture(self, ad, sid, run):
        with mock.patch.object(ad, '_run', side_effect=run), \
             mock.patch('engine_adapters.recap.recap_on', return_value=False), \
             mock.patch('task_ledger.review_turn', return_value=[]), \
             mock.patch('peer_context.refresh_after_settle'):
            ad._run_serial(sid, 'first', client_request_id='first')

    def test_ready_immediate_resend_is_admitted_once(self):
        ad, sid, _, run = self.prepare()
        events = []
        def publish(event):
            events.append(event)
            if event['type'] in ('session.idle', 'session.state'):
                self.assertEqual(ad.requests.get(sid, 'first')['status'], 'finished')
            if event['type'] == 'session.state' and event['properties']['state'] == 'ready':
                one = ad.prompt_async(sid, 'next', client_request_id='next')
                two = ad.prompt_async(sid, 'next', client_request_id='next')
                self.assertEqual(one['status'], 'accepted')
                self.assertEqual(two['status'], 'accepted')
        with mock.patch.object(ad.bus, 'publish', side_effect=publish), \
             mock.patch.object(ad, '_accept_prompt', return_value={'status':'accepted'}) as accept:
            self.run_fixture(ad, sid, run)
        accept.assert_called_once()
        self.assertEqual(ad.requests.get(sid, 'next')['status'], 'accepted')
        self.assertTrue(any(e['type'] == 'session.state' and
                            e['properties']['state'] == 'ready' for e in events))

    def test_ready_starts_real_async_runner_once_after_old_guard_releases(self):
        ad, sid, state, first_run = self.prepare()
        ready, release, dispatched, completed = (threading.Event() for _ in range(4))
        calls = []
        failures = []
        original_serial = ad._run_serial
        def serial(*args, **kwargs):
            try:
                return original_serial(*args, **kwargs)
            except Exception as exc:
                failures.append(exc)
            finally:
                completed.set()
        def run(target, text, *args):
            calls.append(text)
            if text == 'first':
                first_run(target, text, *args)
                return
            self.assertTrue(release.is_set())
            dispatched.set()
            ad.requests.update(sid, 'next', 'started', attempt_id='a2')
            state['turns'].append({'attempt_id':'a2', 'turn_id':'turn2', 'outcome':'completed'})
            ctx = {'sid':sid, 'seq':int(time.time()*1000), 'msg_id':'next-assistant',
                   'parts':{}, 'order':[], 'source':'user', 't0':1,
                   'client_request_id':'next', 'native_terminal':{'status':'completed', 'source':'pi'}}
            ad._live[sid] = ctx
            ad._finish_turn(sid, ctx, None)
        def publish(event):
            if event['type'] == 'session.state' and event['properties']['state'] == 'ready' and not ready.is_set():
                self.assertEqual(ad.requests.get(sid, 'first')['status'], 'finished')
                receipt = ad.prompt_async(sid, 'next', client_request_id='next')
                duplicate = ad.prompt_async(sid, 'next', client_request_id='next')
                self.assertEqual(receipt['status'], 'accepted')
                self.assertEqual(duplicate['status'], 'accepted')
                ready.set()
                self.assertTrue(release.wait(3))
        with mock.patch.object(ad, '_run', side_effect=run), \
             mock.patch.object(ad, '_run_serial', side_effect=serial), \
             mock.patch.object(ad.bus, 'publish', side_effect=publish), \
             mock.patch('engine_adapters.recap.recap_on', return_value=False), \
             mock.patch('task_ledger.review_turn', return_value=[]), \
             mock.patch('peer_context.refresh_after_settle'), \
             mock.patch('self_update.begin_work', return_value=True), \
             mock.patch('self_update.finish_work'):
            thread = threading.Thread(target=original_serial,
                args=(sid, 'first'), kwargs={'client_request_id':'first'})
            thread.start()
            try:
                self.assertTrue(ready.wait(3))
                self.assertFalse(dispatched.is_set())
            finally:
                release.set()
                thread.join(3)
            self.assertTrue(completed.wait(3))
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(calls, ['first', 'next'])
        self.assertEqual(ad.requests.get(sid, 'next')['status'], 'finished')

    def test_get_stays_running_until_receipt_write_completes(self):
        ad, sid, _, run = self.prepare()
        entered, release = threading.Event(), threading.Event()
        original = ad.requests.settle
        def settle(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(*args, **kwargs)
        with mock.patch.object(ad.requests, 'settle', side_effect=settle):
            thread = threading.Thread(target=self.run_fixture, args=(ad, sid, run))
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(ad.turn_state(sid)['state'], 'running')
                self.assertEqual(ad.requests.get(sid, 'first')['status'], 'started')
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(ad.turn_state(sid)['state'], 'ready')

    def test_native_failed_and_interrupted_settle_before_ready(self):
        for outcome in ('failed', 'interrupted'):
            with self.subTest(outcome=outcome):
                ad, sid, _, run = self.prepare(outcome)
                receipts = []
                def publish(event):
                    if event['type'] == 'session.state' and event['properties']['state'] == 'ready':
                        receipts.append(ad.requests.get(sid, 'first'))
                with mock.patch.object(ad.bus, 'publish', side_effect=publish):
                    self.run_fixture(ad, sid, run)
                self.assertEqual(len(receipts), 1)
                self.assertEqual(receipts[0]['status'], 'finished')
                self.assertEqual(receipts[0]['reason'], outcome)

    def test_unconfirmed_native_result_never_publishes_ready(self):
        ad, sid, state, run = self.prepare()
        finish = ad._finish_turn
        def incomplete(_sid, ctx, abnormal):
            state['turns'] = []
            state['active_turn'] = {'attempt_id':'a1', 'recovery_state':'blocked'}
            ctx.pop('native_terminal')
            return finish(_sid, ctx, abnormal)
        events = []
        with mock.patch.object(ad, '_finish_turn', side_effect=incomplete), \
             mock.patch.object(ad.bus, 'publish', side_effect=events.append):
            self.run_fixture(ad, sid, run)
        self.assertFalse(any(e['type'] == 'session.state' and
                             e['properties']['state'] == 'ready' for e in events))
        self.assertEqual(ad.turn_state(sid)['state'], 'blocked')
        self.assertEqual(ad.requests.get(sid, 'first')['status'], 'uncertain')
        with mock.patch.object(ad, '_accept_prompt') as accept, self.assertRaises(Conflict):
            ad.prompt_async(sid, 'next', client_request_id='next')
        accept.assert_not_called()

    def test_receipt_write_failure_blocks_and_later_reconciles_without_replay(self):
        ad, sid, _, run = self.prepare()
        events = []
        with mock.patch.object(ad.requests, 'settle', side_effect=OSError('fixture')), \
             mock.patch.object(ad.bus, 'publish', side_effect=events.append), \
             mock.patch.object(ad, '_receipt_settle_failed') as diagnostic:
            self.run_fixture(ad, sid, run)
            blocked = ad.turn_state(sid)
            self.assertEqual(blocked['state'], 'blocked')
            self.assertEqual(blocked['request']['request_id'], 'first')
            self.assertTrue(diagnostic.called)
            self.assertFalse(any(e['type'] == 'session.state' and
                                 e['properties']['state'] == 'ready' for e in events))
        with mock.patch.object(ad, '_run') as native:
            self.assertEqual(ad.turn_state(sid)['state'], 'ready')
            native.assert_not_called()
        self.assertEqual(ad.requests.get(sid, 'first')['reason'], 'completed')
        self.assertEqual(ad._request_local.rid, '')

    def test_receipt_and_diagnostic_writes_can_both_fail_without_stale_live(self):
        ad, sid, _, run = self.prepare()
        events = []
        with mock.patch.object(ad.requests, 'settle', side_effect=OSError('full disk')), \
             mock.patch.object(ad, '_env_publish', side_effect=OSError('full disk')), \
             mock.patch('deskcfg.log', side_effect=OSError('log unavailable')), \
             mock.patch.object(ad.bus, 'publish', side_effect=events.append):
            self.run_fixture(ad, sid, run)
            self.assertNotIn(sid, ad._live)
            self.assertEqual(ad.turn_state(sid)['state'], 'blocked')
            self.assertEqual(ad.request_status(sid, 'first')['status'], 'started')
            self.assertFalse(any(e['type'] == 'session.state' and
                                 e['properties']['state'] == 'ready' for e in events))
        with mock.patch.object(ad, '_run') as native:
            self.assertEqual(ad.turn_state(sid)['state'], 'ready')
            native.assert_not_called()
        self.assertEqual(ad.requests.get(sid, 'first')['reason'], 'completed')
        self.assertEqual(ad._request_local.rid, '')

    def test_request_status_does_not_settle_link_before_reservation(self):
        ad, sid, _, _ = self.prepare()
        ad.requests.update(sid, 'first', 'uncertain', attempt_id='a1')
        guard = ad._turn_locks.setdefault(sid, threading.Lock())
        with guard:
            self.assertEqual(ad.request_status(sid, 'first')['status'], 'uncertain')
            self.assertEqual(ad.turn_state(sid)['state'], 'blocked')
        self.assertEqual(ad.request_status(sid, 'first')['reason'], 'failed_before_dispatch')


class HTTPReceipts(unittest.TestCase):
    def test_get_and_cancel_are_routed_by_request_id(self):
        with tempfile.TemporaryDirectory() as root:
            ad = Adapter(); ad.name = 'shepherd'; ad.requests = Requests(root)
            ad.request_status = ad.requests.get
            with mock.patch.object(Handler, '_adapter_owning', return_value=ad):
                h = Handler('s',{}); h.path='/session/s/request/r'
                h._adapter_route('GET')
                self.assertEqual(h.out, {'request_id':'r','status':'not_found'})
                h.path += '/cancel-unsent'; h._adapter_route('POST')
                self.assertEqual(h.out['status'], 'cancelled')
                self.assertFalse(ad.requests.claim('s','r',{})[1])

    def test_prompt_returns_persisted_acceptance_instead_of_empty_ack(self):
        ad = Adapter(); ad.name='shepherd'
        ad.prompt_async=mock.Mock(return_value={'request_id':'r','status':'accepted'})
        ad.execution_name=mock.Mock(return_value='pi')
        ad.session_engine=mock.Mock(return_value='pi')
        ad.execution_options=mock.Mock(return_value={})
        h = Handler('s',{'client_request_id':'r','parts':[{'type':'text','text':'hello'}]})
        with mock.patch.object(Handler,'_adapter_owning',return_value=ad), \
             mock.patch.object(ui_contract,'_engine_pref',return_value='pi'), \
             mock.patch.object(ui_contract,'_wb_state_read',return_value={}):
            h._adapter_route('POST')
        self.assertEqual(h.code, 202)
        self.assertEqual(h.out, {'request_id':'r','status':'accepted'})
