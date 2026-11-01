"""Manual compaction requires native completion plus a restorable snapshot."""
import time
import unittest
from unittest import mock

from codex_compaction import compact_projected
from codex_appserver import AppServerError


def frame(method, **params):
    return {'method':method,'params':{'threadId':'thread-1',**params}}


class Connection:
    def __init__(self, frames):
        self.frames=list(frames)
        self.calls=[]
        self.closed=False
    def request(self, method, params, receive=None, **kwargs):
        self.calls.append((method,params))
        if method=='thread/read':return {'thread':{'id':'thread-1','path':'/tmp/fixture'}}
        return {}
    def read(self, _timeout):
        if not self.frames:raise AppServerError('fixture EOF without a terminal')
        return self.frames.pop(0)
    def close(self):self.closed=True


class ManualCompaction(unittest.TestCase):
    def test_opaque_compaction_can_finish_after_normal_silence_limit(self):
        conn = Connection([frame('thread/compacted')])
        original = conn.read
        def read(timeout):
            time.sleep(.08)
            return original(timeout)
        conn.read = read
        finished, snapshots = self.run_compact(conn, timeout=.02, deadline=time.monotonic()+1)
        self.assertEqual(finished[0]['status'], 'completed')
        self.assertTrue(snapshots)

    def test_silent_compaction_still_reaches_its_absolute_limit(self):
        from codex_appserver import AppServerTimeout
        conn = Connection([])
        def read(timeout):
            time.sleep(min(timeout, .01))
            raise AppServerTimeout('no frame')
        conn.read = read
        done = mock.Mock(); snapshot = mock.Mock()
        with self.assertRaisesRegex(AppServerError, '压缩超时'):
            self.run_compact(conn, timeout=.02, deadline=time.monotonic()+.08,
                             on_completed=done, on_working_context=snapshot)
        done.assert_not_called(); snapshot.assert_not_called()

    def run_compact(self, conn, **kwargs):
        finished=[];snapshots=[]
        def snapshot(thread,path):
            self.assertTrue(conn.closed)
            snapshots.append((thread,path))
        args={'on_notification':lambda *_:None,'on_submitting':lambda _:True,
              'on_started':lambda *_:None,'on_completed':finished.append,
              'on_working_context':snapshot,'cancelled':lambda:False,
              'timeout':1,'deadline':time.monotonic()+5}
        args.update(kwargs)
        compact_projected(conn,'thread-1',**args)
        return finished,snapshots

    def test_rpc_acceptance_does_not_imply_compaction_or_start_model_turn(self):
        conn=Connection([])
        done=mock.Mock();snapshot=mock.Mock()
        with self.assertRaises(AppServerError):
            self.run_compact(conn,on_completed=done,on_working_context=snapshot)
        done.assert_not_called();snapshot.assert_not_called()
        self.assertEqual([m for m,_ in conn.calls],['thread/compact/start'])

    def test_modern_compaction_waits_for_matching_turn_terminal(self):
        conn=Connection([
            frame('turn/started',turn={'id':'turn-1'}),
            frame('item/completed',turnId='turn-1',item={'type':'contextCompaction'}),
            frame('turn/completed',turn={'id':'old-turn','status':'completed'}),
            frame('turn/completed',turn={'id':'turn-1','status':'completed'}),
        ])
        finished,snapshots=self.run_compact(conn)
        self.assertFalse(conn.frames)
        self.assertEqual(finished[0]['status'],'completed')
        self.assertEqual(snapshots,[('thread-1','/tmp/fixture')])

    def test_legacy_compacted_notification_is_scoped_to_private_thread(self):
        conn=Connection([frame('thread/compacted',threadId='foreign'),frame('thread/compacted')])
        finished,_=self.run_compact(conn)
        self.assertEqual(finished[0]['signal'],'thread/compacted')
        self.assertFalse(conn.frames)

    def test_failed_or_interrupted_native_turn_never_exports_success_checkpoint(self):
        for status in ('failed','interrupted'):
            conn=Connection([frame('turn/started',turn={'id':'turn-1'}),
                frame('item/completed',turnId='turn-1',item={'type':'contextCompaction'}),
                frame('turn/completed',turn={'id':'turn-1','status':status})])
            finished,snapshots=self.run_compact(conn)
            self.assertEqual(finished[0]['status'],status)
            self.assertFalse(snapshots)

    def test_successful_turn_without_compaction_signal_is_not_compacted(self):
        conn=Connection([frame('turn/started',turn={'id':'turn-1'}),
            frame('turn/completed',turn={'id':'turn-1','status':'completed'})])
        with self.assertRaises(AppServerError):self.run_compact(conn)

    def test_request_error_waits_for_the_native_failed_terminal(self):
        conn=Connection([frame('turn/started',turn={'id':'turn-1'}),
            frame('error',turnId='turn-1',error={'message':'fixture failure'},willRetry=False),
            frame('turn/completed',turn={'id':'turn-1','status':'failed'})])
        finished,snapshots=self.run_compact(conn)
        self.assertEqual(finished[0]['status'],'failed')
        self.assertFalse(snapshots)

    def test_checkpoint_failure_cannot_publish_completion(self):
        conn=Connection([frame('thread/compacted')])
        done=mock.Mock()
        with self.assertRaises(OSError):
            self.run_compact(conn,on_completed=done,on_working_context=mock.Mock(side_effect=OSError('disk full')))
        done.assert_not_called()

    def test_cancel_before_dispatch_does_not_call_rpc_or_fence(self):
        conn=Connection([]);submit=mock.Mock()
        with self.assertRaises(AppServerError):self.run_compact(conn,cancelled=lambda:True,on_submitting=submit)
        submit.assert_not_called();self.assertFalse(conn.calls)

    def test_unmatched_rollout_identity_is_rejected(self):
        conn=Connection([frame('thread/compacted')]);original=conn.request
        def request(method,*args,**kwargs):
            if method=='thread/read':return {'thread':{'id':'foreign','path':'/tmp/other'}}
            return original(method,*args,**kwargs)
        conn.request=request
        with self.assertRaises(AppServerError):self.run_compact(conn)
