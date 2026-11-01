import fcntl
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from engine_adapters.bus import EventBus
from engine_adapters.claude import ClaudeAdapter
from engine_adapters.codex import CodexAdapter
from engine_adapters.pi import PiAdapter


class OrdinaryDeliveryMatrix(unittest.TestCase):
    def run_case(self, cls, native, abnormal, writer_alive=False):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch('engine_adapters.delivery_recovery.RETRY_DELAYS', ()):
            work=Path(root)/'work';work.mkdir();(work/'prompts').mkdir();(work/'prompts/style.md').write_text('')
            ad=cls(str(Path(root)/'data'),str(work),EventBus());sid=ad.create_session()['id'];rid='prompt_case'
            ad.delivery.claim(sid,rid,{'text':'hello','attachments':[]})
            ad._delivery_local.rid=rid
            ad.build_cmd=lambda *_:[];ad.grounding=lambda *_a,**_k:{'constant':'','scene':''};ad._stash=lambda *_:[]
            def dispatch(ctx,*_args):
                ad._delivery_dispatched(ctx)
                if native is not None:ctx['native_terminal']=dict(native)
                if abnormal:
                    ctx['engine_failure']={'source':'transport','code':'TRANSPORT','message':abnormal}
                return abnormal
            ad._turn_dispatch=dispatch
            fd=None
            if writer_alive:
                # 旧原生写入进程仍持有写入锁：没有终态也不能放行。
                fd=os.open(ad.native_writer_lock(sid),os.O_RDWR|os.O_CREAT,0o600)
                fcntl.flock(fd,fcntl.LOCK_EX)
            try:
                ad._run_serial(sid,'hello',client_request_id=rid)
                ad._autoresolve_at={}
                state=ad.turn_state(sid)
                return ad.delivery.get(sid,rid),state
            finally:
                if fd is not None:
                    os.close(fd)

    def test_all_engines_release_only_with_explicit_native_terminal(self):
        for cls in (ClaudeAdapter,CodexAdapter,PiAdapter):
            for status in ('completed','failed','interrupted'):
                with self.subTest(engine=cls.name,status=status):
                    row,state=self.run_case(cls,{'source':'fixture','status':status},
                                            'native failed' if status=='failed' else '')
                    self.assertEqual(row['status'],'finished')
                    self.assertEqual(state['state'],'ready')

    def test_all_engines_fail_closed_while_writer_may_be_alive(self):
        for cls in (ClaudeAdapter,CodexAdapter,PiAdapter):
            with self.subTest(engine=cls.name):
                row,state=self.run_case(cls,None,'transport disconnected',writer_alive=True)
                self.assertEqual(row['status'],'uncertain')
                self.assertEqual(state['state'],'blocked')

    def test_all_engines_autoresolve_once_writer_exited(self):
        for cls in (ClaudeAdapter,CodexAdapter,PiAdapter):
            with self.subTest(engine=cls.name):
                row,state=self.run_case(cls,None,'transport disconnected')
                self.assertEqual(state['state'],'ready')
                self.assertEqual((row['status'],row['reason'].split(':')[0]),
                                 ('finished','auto_writer_exited'))



if __name__=='__main__':unittest.main()
