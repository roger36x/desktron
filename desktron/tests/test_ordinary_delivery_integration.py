import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from engine_adapters.bus import EventBus
from engine_adapters.pi import PiAdapter
from engine_adapters.delivery import DeliveryConflict, DeliveryReceipts


def hold_writer_lock(test, adapter, sid):
    """模拟旧原生写入进程仍存活：另开一个文件描述并持有写入锁。"""
    path = adapter.native_writer_lock(sid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    def release():
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return release


class OrdinaryDeliveryIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.workspace=root/'workspace';self.workspace.mkdir()
        (self.workspace/'prompts').mkdir();(self.workspace/'prompts/style.md').write_text('')
        self.bus=EventBus()
        self.ad=PiAdapter(str(root/'data'),str(self.workspace),self.bus)
        self.ad.delivery.boot_id='boot_old'
        self.sid=self.ad.create_session()['id']

    def test_restart_after_store_removes_ghost_user_and_rejects_receipt(self):
        rid='prompt_store';delivery=self.ad.delivery
        delivery.claim(self.sid,rid,{'text':'hello'})
        message={'info':{'id':'msg_u_1','role':'user','sessionID':self.sid,'at':1,
                         'delivery_request_id':rid},
                 'parts':[{'id':'p','type':'text','text':'hello'}]}
        delivery.stored(self.sid,rid,message_id='msg_u_1');self.ad._append_msg(self.sid,message)
        restored=PiAdapter(str(Path(self.tmp.name)/'data'),str(self.workspace),EventBus())
        self.assertEqual(restored.delivery.get(self.sid,rid)['reason'],'restart_before_dispatch')
        self.assertEqual(restored.messages(self.sid),[])

    def test_message_append_failure_does_not_leave_released_receipt(self):
        rid='prompt_append_fail'
        self.ad.delivery.claim(self.sid,rid,{'text':'hello','attachments':[]})
        self.ad._delivery_local.rid=rid
        self.ad.build_cmd=lambda *_:[]
        self.ad.grounding=lambda *_args,**_kwargs:{'constant':'','scene':''}
        self.ad._stash=lambda *_:[]
        with mock.patch.object(self.ad,'_append_msg',side_effect=OSError('disk failed')):
            with self.assertRaisesRegex(OSError,'disk failed'):
                self.ad._run_serial(self.sid,'hello',client_request_id=rid)
        self.assertEqual(self.ad.delivery.get(self.sid,rid)['status'],'rejected')
        self.assertEqual(self.ad.turn_state(self.sid)['state'],'ready')

    def test_restart_cleanup_failure_keeps_session_blocked(self):
        rid='prompt_cleanup';delivery=self.ad.delivery
        delivery.claim(self.sid,rid,{'text':'hello'})
        delivery.stored(self.sid,rid,message_id='msg_u_cleanup')
        with mock.patch.object(PiAdapter,'_rewrite_msgs',side_effect=OSError('disk failed')):
            restored=PiAdapter(str(Path(self.tmp.name)/'data'),str(self.workspace),EventBus())
        state=restored.turn_state(self.sid)
        self.assertEqual(state['state'],'blocked')
        self.assertEqual(state['delivery']['status'],'cleanup_required')
        with self.assertRaises(DeliveryConflict):
            restored.prompt_async(self.sid,'next',client_request_id='prompt_next')
        # A later restart retries the same cleanup instead of leaving a permanent dead end.
        again=PiAdapter(str(Path(self.tmp.name)/'data'),str(self.workspace),EventBus())
        self.assertEqual(again.delivery.get(self.sid,rid)['status'],'rejected')
        self.assertEqual(again.turn_state(self.sid)['state'],'ready')

    def test_restart_after_dispatch_blocks_while_old_writer_alive(self):
        rid='prompt_sent';delivery=self.ad.delivery
        delivery.claim(self.sid,rid,{'text':'hello'})
        delivery.stored(self.sid,rid,message_id='msg_u_1')
        delivery.dispatched(self.sid,rid,engine_session_id='native_1')
        release=hold_writer_lock(self,self.ad,self.sid)
        try:
            with mock.patch('engine_adapters.delivery_recovery.RETRY_DELAYS', ()):
                restored=PiAdapter(str(Path(self.tmp.name)/'data'),str(self.workspace),EventBus())
                restored._autoresolve_at={}
                state=restored.turn_state(self.sid)
                self.assertEqual(state['state'],'blocked')
                self.assertEqual(state['delivery']['status'],'uncertain')
                with self.assertRaises(DeliveryConflict):
                    restored.prompt_async(self.sid,'next',client_request_id='prompt_next')
                # 用户确认仍是可用的兜底出口。
                restored.resolve_delivery(self.sid,rid)
                self.assertEqual(restored.turn_state(self.sid)['state'],'ready')
        finally:
            release()

    def test_restart_after_dispatch_autoresolves_once_old_writer_exited(self):
        rid='prompt_sent';delivery=self.ad.delivery
        delivery.claim(self.sid,rid,{'text':'hello'})
        delivery.stored(self.sid,rid,message_id='msg_u_1')
        delivery.dispatched(self.sid,rid,engine_session_id='native_1')
        restored=PiAdapter(str(Path(self.tmp.name)/'data'),str(self.workspace),EventBus())
        self.assertEqual(restored.turn_state(self.sid)['state'],'ready')
        receipt=restored.delivery.get(self.sid,rid)
        self.assertEqual(receipt['status'],'finished')
        self.assertTrue(receipt['reason'].startswith('auto_writer_exited'))

    def test_cancel_unsent_removes_stored_user_projection(self):
        rid='prompt_cancel';delivery=self.ad.delivery
        delivery.claim(self.sid,rid,{'text':'hello'})
        message={'info':{'id':'msg_u_cancel','role':'user','sessionID':self.sid,'at':1,
                         'delivery_request_id':rid},
                 'parts':[{'id':'p','type':'text','text':'hello'}]}
        delivery.stored(self.sid,rid,message_id='msg_u_cancel');self.ad._append_msg(self.sid,message)
        row=self.ad.cancel_unsent_delivery(self.sid,rid)
        self.assertEqual(row['status'],'rejected')
        self.assertEqual(self.ad.messages(self.sid),[])

    def test_fast_abort_cancels_accepted_before_live_context_exists(self):
        rid='prompt_fast';self.ad.delivery.claim(self.sid,rid,{'text':'hello'})
        got=self.ad.abort(self.sid,recall=True,client_request_id=rid)
        self.assertEqual(got['request']['status'],'cancelled')
        self.assertEqual(self.ad.delivery.get(self.sid,rid)['status'],'cancelled')

    def test_cancel_during_storing_prevents_append_and_native_dispatch(self):
        rid='prompt_race';entered=threading.Event();release=threading.Event();real=self.ad._append_msg
        self.ad.delivery.claim(self.sid,rid,{'text':'hello','attachments':[]})
        self.ad._delivery_local.rid=rid
        self.ad.build_cmd=lambda *_:[];self.ad.grounding=lambda *_a,**_k:{'constant':'','scene':''};self.ad._stash=lambda *_:[]
        def blocked(*args,**kwargs):entered.set();release.wait(2);return real(*args,**kwargs)
        self.ad._append_msg=blocked;self.ad._turn_dispatch=mock.Mock()
        failure=[]
        def run():
            self.ad._delivery_local.rid=rid
            try:self.ad._run_serial(self.sid,'hello',client_request_id=rid)
            except Exception as exc:failure.append(exc)
        thread=threading.Thread(target=run);thread.start();self.assertTrue(entered.wait(1))
        row=self.ad.cancel_unsent_delivery(self.sid,rid);self.assertEqual(row['status'],'cleanup_required')
        release.set();thread.join(2)
        self.assertTrue(failure);self.ad._turn_dispatch.assert_not_called()
        self.assertFalse(any((m.get('info') or {}).get('delivery_request_id')==rid for m in self.ad.messages(self.sid)))
        self.assertEqual(self.ad.delivery.get(self.sid,rid)['status'],'rejected')

    def test_queued_thread_rechecks_rejected_receipt_before_message_write(self):
        rid='prompt_queued'
        self.ad.delivery.claim(self.sid,rid,{'text':'hello','attachments':[]})
        self.ad.delivery.reject(self.sid,rid,reason='restart_before_store')
        self.ad._delivery_local.rid=rid
        self.ad._run=mock.Mock()
        with self.assertRaisesRegex(DeliveryConflict,'派发前取消'):
            self.ad._run_serial(self.sid,'hello',client_request_id=rid)
        self.ad._run.assert_not_called()
        self.assertFalse(any((row.get('info') or {}).get('role')=='user'
                             for row in self.ad.messages(self.sid)))

    def test_queued_next_turn_cannot_cross_previous_uncertain_boundary(self):
        old='prompt_old';new='prompt_new'
        self.ad.delivery.claim(self.sid,old,{'text':'old'})
        self.ad.delivery.stored(self.sid,old,message_id='msg_old')
        self.ad.delivery.dispatched(self.sid,old)
        self.ad.delivery.claim(self.sid,new,{'text':'new'})
        self.ad.delivery.uncertain(self.sid,old,reason='NATIVE_TERMINAL_MISSING')
        self.ad._delivery_local.rid=new;self.ad._run=mock.Mock()
        with self.assertRaisesRegex(DeliveryConflict,'上一条请求可能已经执行'):
            self.ad._run_serial(self.sid,'new',client_request_id=new)
        self.ad._run.assert_not_called()
        self.assertEqual(self.ad.delivery.get(self.sid,new)['reason'],'failed_before_store')

    def test_normal_turn_finishes_receipt_before_ready(self):
        rid='prompt_ok'
        # Keep this test at the Adapter lifecycle boundary; no real model process.
        def dispatch(ctx,*_args):
            self.ad._delivery_dispatched(ctx)
            ctx['native_terminal']={'source':'agent_settled','status':'completed'}
            return None
        self.ad._turn_dispatch=dispatch
        self.ad.build_cmd=lambda *_:[]
        self.ad.grounding=lambda *_args,**_kwargs:{'constant':'','scene':''}
        self.ad._stash=lambda *_:[]
        self.ad.delivery.claim(self.sid,rid,{'text':'hello','attachments':[]})
        self.ad._delivery_local.rid=rid
        self.ad._run_serial(self.sid,'hello',client_request_id=rid)
        receipt=self.ad.delivery.get(self.sid,rid)
        self.assertEqual((receipt['status'],receipt['reason']),('finished','agent_settled'))
        self.assertEqual(self.ad.turn_state(self.sid)['state'],'ready')

    def test_missing_native_terminal_stays_blocked_after_finish_path(self):
        rid='prompt_unknown'
        def dispatch(ctx,*_args):
            self.ad._delivery_dispatched(ctx)
            ctx['engine_failure']={'source':'transport','code':'NATIVE_TERMINAL_MISSING',
                                   'message':'terminal missing'}
            return 'terminal missing'
        self.ad._turn_dispatch=dispatch
        self.ad.build_cmd=lambda *_:[]
        self.ad.grounding=lambda *_args,**_kwargs:{'constant':'','scene':''}
        self.ad._stash=lambda *_:[]
        self.ad.delivery.claim(self.sid,rid,{'text':'hello','attachments':[]})
        self.ad._delivery_local.rid=rid
        events=self.bus.subscribe()
        release=hold_writer_lock(self,self.ad,self.sid)
        self.ad._delivery_autoresolve_later=lambda sid: None
        try:
            self.ad._run_serial(self.sid,'hello',client_request_id=rid)
            self.assertEqual(self.ad.delivery.get(self.sid,rid)['status'],'uncertain')
            self.assertEqual(self.ad.turn_state(self.sid)['state'],'blocked')
            rows=[]
            while not events.empty(): rows.append(events.get_nowait())
            state_events=[e['properties'] for e in rows if e['type']=='session.state']
            self.assertEqual(state_events[-1]['state'],'blocked')
        finally:
            release()
        # 写入进程退出后，下一次读取状态即自动收口并推送 ready，无需用户确认。
        self.ad._autoresolve_at={}
        self.assertEqual(self.ad.turn_state(self.sid)['state'],'ready')
        receipt=self.ad.delivery.get(self.sid,rid)
        self.assertEqual((receipt['status'],receipt['reason'].split(':')[0]),
                         ('finished','auto_writer_exited'))
        rows=[]
        while not events.empty(): rows.append(events.get_nowait())
        self.assertEqual([e['properties']['state'] for e in rows if e['type']=='session.state'][-1],'ready')

    def test_predispatch_failure_rejects_without_fake_assistant_text(self):
        rid='prompt_fail'
        self.ad.build_cmd=lambda *_:[]
        self.ad.grounding=lambda *_args,**_kwargs:{'constant':'','scene':''}
        self.ad._stash=lambda *_:[]
        self.ad._turn_dispatch=mock.Mock(side_effect=OSError('prepare failed'))
        self.ad.delivery.claim(self.sid,rid,{'text':'hello','attachments':[]})
        self.ad._delivery_local.rid=rid
        events=self.bus.subscribe()
        self.ad._run_serial(self.sid,'hello',client_request_id=rid)
        receipt=self.ad.delivery.get(self.sid,rid)
        self.assertEqual(receipt['reason'],'failed_before_dispatch')
        messages=self.ad.messages(self.sid)
        roles=[(row.get('info') or {}).get('role') for row in messages]
        self.assertEqual(roles,['user','assistant'])
        visible=''.join(part.get('text','') for part in messages[-1]['parts'] if part.get('type')=='text')
        self.assertEqual(visible,'')
        rows=[]
        while not events.empty(): rows.append(events.get_nowait())
        errors=[row['properties']['error'] for row in rows if row['type']=='session.error']
        self.assertTrue(any(error.get('message')=='prepare failed' for error in errors))

    def test_adapter_failure_preserves_model_text_without_appending_diagnostic(self):
        rid='prompt_partial'
        self.ad.build_cmd=lambda *_:[]
        self.ad.grounding=lambda *_args,**_kwargs:{'constant':'','scene':''}
        self.ad._stash=lambda *_:[]
        def dispatch(ctx,*_args):
            self.ad._delivery_dispatched(ctx)
            self.ad._emit_text(ctx,'已完成的模型正文')
            raise RuntimeError('native trailer rejected')
        self.ad._turn_dispatch=dispatch
        self.ad.delivery.claim(self.sid,rid,{'text':'hello','attachments':[]})
        self.ad._delivery_local.rid=rid
        self.ad._run_serial(self.sid,'hello',client_request_id=rid)
        messages=self.ad.messages(self.sid)
        self.assertEqual([(row.get('info') or {}).get('role') for row in messages],['user','assistant'])
        text=''.join(part.get('text','') for part in messages[-1]['parts'] if part.get('type')=='text')
        self.assertEqual(text,'已完成的模型正文')
        self.assertNotIn('适配器错误',text)


if __name__=='__main__':unittest.main()
