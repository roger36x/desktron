"""Desk 的回合控制面只暴露 running / cancelling / ready 三态。"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
sys.path.insert(0, LIB)
import peer_mailbox

_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv


class Recorder(ea.EventBus):
    def __init__(self):
        super().__init__()
        self.events = []

    def publish(self, event):
        self.events.append(event)

    def of(self, event_type):
        return [x["properties"] for x in self.events if x["type"] == event_type]


class SerialProbe(ea.CliAdapter):
    name = "turn-contract-probe"

    def __init__(self, data_dir, workspace, bus):
        super().__init__(data_dir, workspace, bus)
        self.entered = []
        self.active = 0
        self.max_active = 0
        self.probe_lock = threading.Lock()
        self.first_entered = threading.Event()
        self.release_first = threading.Event()
        self.second_done = threading.Event()

    def _run(self, sid, text, opts=None, atts=None):
        with self.probe_lock:
            self.entered.append(text)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if text == "first":
                self.first_entered.set()
                self.release_first.wait(2)
        finally:
            with self.probe_lock:
                self.active -= 1
            if text == "second":
                self.second_done.set()


class TurnStateContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Recorder()
        self.ad = ea.CliAdapter(self.tmp.name, self.tmp.name, self.bus)

    def tearDown(self):
        self.tmp.cleanup()

    def test_task_review_runs_without_recap_and_skips_failed_turns(self):
        import task_ledger
        data = os.path.join(self.tmp.name, 'task-data')
        for outcome in ('completed', 'failed', 'interrupted', 'uncertain'):
            with self.subTest(outcome=outcome):
                sid = self.ad.create_session('alpha')['id']
                item, row, error = task_ledger.apply(data, self.tmp.name, 'alpha',
                    {'intent': 'create', 'title': outcome, 'done': '报告交付',
                     'contract': {'criteria': [{'id':'report','kind':'artifact','summary':'报告完成'}]},
                     'evidence': [{'criterion':'report','result':'passed','summary':'已验证','ref':'artifacts/report.md'}]},
                    {'id': sid})
                self.assertFalse(error)
                ctx = {'sid': sid, 'seq': 77, 'msg_id': 'msg_test', 'parts': {}, 'order': [],
                       'last_tool': None, 't0': row['activity'][sid], 'source': self.ad.SRC_USER}
                if outcome == 'interrupted':
                    ctx['aborted'] = 'interrupt'
                if outcome == 'uncertain':
                    ctx['delivery_blocked'] = True
                    ctx['client_request_id'] = 'request_uncertain'
                self.ad._live[sid] = ctx
                with mock.patch('deskcfg.DATA_HOME', data), \
                     mock.patch.object(self.ad.delivery, 'get', return_value={'status':'cleanup_required'}), \
                     mock.patch('engine_adapters.recap.recap_on', return_value=False):
                    self.ad._finish_turn(sid, ctx, 'failed' if outcome == 'failed' else None)
                state = task_ledger.get(data, 'alpha', item['t'])
                self.assertEqual(state.get('close_reason') == 'completed', outcome == 'completed')
                self.assertEqual(self.ad.turn_state(sid)['state'], 'ready')

    def test_task_review_failure_does_not_block_ready(self):
        sid = self.ad.create_session()['id']
        ctx = {'sid':sid, 'seq':9, 'msg_id':'m9', 'parts':{}, 'order':[],
               'last_tool':None, 't0':1, 'source':self.ad.SRC_USER}
        self.ad._live[sid] = ctx
        with mock.patch('task_ledger.review_turn', side_effect=OSError('fixture')):
            self.ad._finish_turn(sid, ctx, None)
        self.assertEqual(self.ad.turn_state(sid)['state'], 'ready')

    def test_状态按真实回合边界迁移(self):
        sid = self.ad.create_session()["id"]
        self.assertEqual(self.ad.turn_state(sid)["state"], "ready")

        ctx = {"sid": sid, "seq": 7, "msg_id": "msg_a_7", "parts": {}, "order": [],
               "last_tool": None, "t0": 1, "source": self.ad.SRC_USER}
        self.ad._live[sid] = ctx
        self.ad._turn_state_publish(sid, ctx, self.ad.TURN_RUNNING)
        self.assertEqual(self.ad.turn_state(sid)["state"], "running")

        stopped = self.ad.abort(sid)
        self.assertEqual(stopped["state"], "cancelling")
        self.assertEqual(stopped["generation"], 7)
        self.ad._finish_turn(sid, ctx, None)

        self.assertEqual(self.ad.turn_state(sid)["state"], "ready")
        self.assertEqual([x["state"] for x in self.bus.of("session.state")],
                         ["running", "cancelling", "ready"])
        idle = self.bus.of("session.idle")[-1]
        self.assertEqual((idle["generation"], idle["contract"]), (7, 1))

    def test_失败收尾不会被ready伪装成完成(self):
        sid = self.ad.create_session()["id"]
        failure = {"source": "provider", "provider": "fixture", "code": "TIMEOUT",
                   "message": "provider failed"}
        ctx = {"sid": sid, "seq": 8, "msg_id": "msg_failed", "parts": {}, "order": [],
               "last_tool": None, "t0": 1, "source": self.ad.SRC_USER,
               "engine_error": "provider failed", "engine_failure": failure}
        self.ad._live[sid] = ctx
        self.ad._finish_turn(sid, ctx, None)
        idle = self.bus.of("session.idle")[-1]
        ready = self.bus.of("session.state")[-1]
        self.assertTrue(idle["failed"])
        self.assertTrue(ready["failed"])
        messages = self.ad.messages(sid)
        self.assertTrue(messages[-1]["info"]["failed"])
        self.assertEqual(messages[-1]["info"]["error"], failure)
        self.assertEqual(self.bus.of("session.error")[-1]["error"], failure)

    def test_适配器已发布错误时收尾不重复但边界仍带原因(self):
        sid = self.ad.create_session()["id"]
        failure = {"source":"provider", "provider":"claude", "code":"AUTH",
                   "message":"token expired"}
        ctx = {"sid":sid, "seq":9, "msg_id":"msg_failed_once", "parts":{}, "order":[],
               "t0":1, "source":self.ad.SRC_USER, "engine_error":"token expired",
               "engine_failure":failure, "_engine_error_published":True}
        self.ad._live[sid] = ctx
        self.ad._finish_turn(sid, ctx, None)
        self.assertEqual(self.bus.of("session.error"), [])
        self.assertEqual(self.bus.of("session.idle")[-1]["error"], failure)
        self.assertEqual(self.bus.of("session.state")[-1]["error"], failure)

    def test_同一会话的异步入口严格串行(self):
        ad = SerialProbe(self.tmp.name, self.tmp.name, self.bus)
        sid = ad.create_session()["id"]
        ad.prompt_async(sid, "first")
        self.assertTrue(ad.first_entered.wait(1))
        ad.prompt_async(sid, "second")
        time.sleep(0.05)
        self.assertEqual(ad.entered, ["first"])

        ad.release_first.set()
        self.assertTrue(ad.second_done.wait(1))
        self.assertEqual(ad.entered, ["first", "second"])
        self.assertEqual(ad.max_active, 1)
        deadline=time.time()+1
        while ad.delivery.unresolved(sid) and time.time()<deadline:
            time.sleep(.01)
        self.assertEqual(ad.delivery.unresolved(sid),[])

    def test_普通回合入口拒绝跨会话问询(self):
        ad = SerialProbe(self.tmp.name, self.tmp.name, self.bus)
        with self.assertRaisesRegex(RuntimeError, "临时隔离支线"):
            ad._run_serial("same", "question", {}, [], None, "pm_" + "a" * 32)
        self.assertEqual(ad.entered, [])

    def test_状态查询路由使用适配器控制面(self):
        src = _srcmap.ui_server_src()
        self.assertIn('seg[3] == "state" and method == "GET"', src)
        self.assertIn("ad.turn_state(seg[2])", src)

    def test_问询请求号经路由传给隔离支线(self):
        src = _srcmap.ui_server_src()
        self.assertIn('body.get("peer_request_id")', src)
        self.assertIn("peer_mailbox.accept(peer_request_id, seg[2])", src)
        self.assertIn("ad.peer_prompt_async(", src)

    def test_手动压缩独立于普通回合仍可查(self):
        sid = self.ad.create_session()["id"]
        self.ad._compact_state[sid] = {
            "running": True, "started": 123456, "trigger": "manual"}
        live = self.ad.turn_state(sid)
        self.assertEqual(live["state"], "ready")
        self.assertEqual(live["compacting"], 123456)
        self.assertTrue(live["compact"]["running"])

        self.ad._compact_state[sid] = {
            "running": False, "started": 123456, "result": "success",
            "error": "", "trigger": "manual"}
        done = self.ad.turn_state(sid)
        self.assertNotIn("compacting", done)
        self.assertEqual(done["compact"]["result"], "success")


if __name__ == "__main__":
    unittest.main()
