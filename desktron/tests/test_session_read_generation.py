"""会话已读按回合确认：完成、轮询和多客户端回执可以任意先后到达。"""
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron/lib'))
_argv = sys.argv
sys.argv = ['ui_server.py', str(ROOT / 'frontend'), '7999', '7998', str(ROOT)]
try:
    import term
    import ui_contract
    from engine_adapters import CliAdapter, CodexAdapter
finally:
    sys.argv = _argv


class Handler(ui_contract.UiContractMixin):
    def __init__(self, sid, body):
        raw = json.dumps(body).encode()
        self.path = '/session/' + sid + '/seen'
        self.headers = {'Content-Length': str(len(raw))}
        self.rfile = BytesIO(raw)

    def _send_json(self, obj, code=200):
        self.out, self.code = obj, code


class ReadGeneration(unittest.TestCase):
    def setUp(self):
        self.state = {'state': 'running', 'generation': 100}
        self.ad = SimpleNamespace(name='codex', index={'adp_test': {'engine_sid': 'native'}},
                                  turn_state=lambda sid: {**self.state, 'completed_generation': self.state.get(
                                      'completed_generation', self.state['generation'] if self.state['state'] == 'ready' else 0)})
        self.patch = mock.patch.object(term, '_TERM_STATE', {})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def status(self):
        return term._gui_read_status(self.ad, 'adp_test')

    def seen(self, generation, sid='adp_test'):
        h = Handler(sid, {} if generation is None else {'generation': generation})
        with mock.patch.object(ui_contract, '_adapter', return_value=self.ad), \
             mock.patch.object(ui_contract, '_engine_pref', return_value='claude'), \
             mock.patch.object(Handler, '_adapter_owning', return_value=self.ad):
            h._adapter_route('POST')
        self.assertEqual(h.code, 200)
        return h.out

    def test_visible_completion_ack_precedes_idle_poll(self):
        self.assertTrue(self.status()['busy'])
        self.state['state'] = 'ready'
        self.seen(100)
        for _ in range(3):
            self.assertFalse(self.status()['unread'])

    def test_background_completion_and_next_turn(self):
        self.status()
        self.state['state'] = 'ready'
        self.assertTrue(self.status()['unread'])
        self.seen(100)
        self.state.update(state='running', generation=200)
        self.assertTrue(self.status()['busy'])
        self.state['state'] = 'ready'
        self.assertTrue(self.status()['unread'])

    def test_opening_busy_turn_does_not_ack_future_completion(self):
        self.assertEqual(self.seen(100)['seen_generation'], 0)
        self.state['state'] = 'ready'
        self.assertTrue(self.status()['unread'])

    def test_old_client_also_consumes_completed_transition(self):
        self.status()
        self.state['state'] = 'ready'
        self.seen(None)
        self.assertFalse(self.status()['unread'])

    def test_late_ack_from_other_client_cannot_clear_next_turn(self):
        self.state.update(state='ready', generation=200)
        self.seen(100)
        self.assertTrue(self.status()['unread'])
        self.seen(200)
        self.seen(100)
        self.assertFalse(self.status()['unread'])

    def test_fast_turn_finished_between_polls_still_unread(self):
        self.state['state'] = 'ready'
        self.assertTrue(self.status()['unread'])

    def test_late_running_snapshot_cannot_reopen_completed_turn(self):
        self.state['state'] = 'ready'
        self.seen(100)
        self.state['state'] = 'running'
        self.assertFalse(self.status()['busy'])
        self.assertFalse(self.status()['unread'])

    def test_preparation_ready_is_not_completed_or_acknowledged(self):
        self.state.update(state='ready', completed_generation=0)
        self.assertFalse(self.status()['unread'])
        self.assertEqual(self.seen(100)['seen_generation'], 0)
        self.state['state'] = 'running'
        self.assertTrue(self.status()['busy'])
        self.state.update(state='ready', completed_generation=100)
        self.assertTrue(self.status()['unread'])

    def test_real_adapter_records_only_published_completion(self):
        with tempfile.TemporaryDirectory() as root:
            ad = CliAdapter(root, root, mock.Mock())
            ad._turn_seq['test'] = 100
            self.assertEqual(ad.turn_state('test')['completed_generation'], 0)
            self.assertFalse(term._gui_read_status(ad, 'test')['unread'])
            ad._live['test'] = {'seq': 100}
            ad._turn_state_publish('test', {'seq': 100}, ad.TURN_RUNNING)
            self.assertTrue(term._gui_read_status(ad, 'test')['busy'])
            del ad._live['test']
            ad._turn_state_publish('test', {'seq': 100}, ad.TURN_READY)
            self.assertEqual(ad.turn_state('test')['completed_generation'], 100)
            self.assertTrue(term._gui_read_status(ad, 'test')['unread'])
            ad._turn_state_publish('test', {'seq': 50}, ad.TURN_READY)
            self.assertEqual(ad.turn_state('test')['completed_generation'], 100)

    def test_all_engines_and_both_ids_share_same_ack(self):
        for engine in ('claude', 'codex', 'pi'):
            with self.subTest(engine=engine):
                self.ad.name = engine
                self.state['state'] = 'ready'
                self.assertTrue(self.status()['unread'])
                self.seen(100, 'native')
                self.assertFalse(self.status()['unread'])

    def test_codex_completion_is_published_and_consumed_once(self):
        with tempfile.TemporaryDirectory() as root:
            bus = mock.Mock()
            self.ad = CodexAdapter(root, root, bus)
            self.ad.index['adp_test'] = {'engine_sid': 'native'}
            ctx = {'seq': 100}
            self.ad._turn_seq['adp_test'] = 100
            self.ad._live['adp_test'] = ctx
            for state in (self.ad.TURN_RUNNING, self.ad.TURN_CANCELLING):
                self.ad._turn_state_publish('adp_test', ctx, state)
                self.assertEqual(self.ad.turn_state('adp_test')['completed_generation'], 0)
            self.assertEqual(self.seen(100)['seen_generation'], 0)
            del self.ad._live['adp_test']
            self.ad._turn_state_publish('adp_test', ctx, self.ad.TURN_READY)
            props = bus.publish.call_args.args[0]['properties']
            self.assertEqual(props['completed_generation'], 100)
            self.assertFalse(self.status()['busy'])
            self.assertTrue(self.status()['unread'])
            self.seen(100, 'native')
            self.assertFalse(self.status()['unread'])
            # 重复或迟到的收尾不能重新点亮已读回合。
            for generation in (100, 50):
                self.ad._turn_state_publish('adp_test', {'seq': generation}, self.ad.TURN_READY)
                self.assertEqual(self.ad.turn_state('adp_test')['completed_generation'], 100)
                self.assertFalse(self.status()['unread'])
            # 下一轮即使在两次列表轮询之间完成，仍必须显示未读。
            self.ad._turn_seq['adp_test'] = 200
            self.ad._turn_state_publish('adp_test', {'seq': 200}, self.ad.TURN_READY)
            self.seen(100)
            self.assertTrue(self.status()['unread'])
            self.seen(200)
            self.assertFalse(self.status()['unread'])

    def test_ack_before_native_id_is_assigned_survives_assignment(self):
        self.ad.index['adp_test'] = {}
        self.state['state'] = 'ready'
        self.seen(100)
        self.ad.index['adp_test']['engine_sid'] = 'native'
        self.assertFalse(self.status()['unread'])


if __name__ == '__main__':
    unittest.main()
