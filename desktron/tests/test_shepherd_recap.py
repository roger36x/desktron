"""Public recap identity, source fences, cancellation and old-session naming."""
import copy
import json
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'lib'))
from engine_adapters.shepherd import ShepherdAdapter
from engine_adapters.bus import EventBus
from engine_adapters import recap
import session_autotitle as titles


class Recap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ad = ShepherdAdapter(str(root/'data'), str(root), EventBus())
        self.sid = 'adp_test'
        self.ad.index[self.sid] = {'id':self.sid, 'title':'first user message', 'domain':'desk'}
        self.ad._turn_seq[self.sid] = 9
        self.binding = {'binding_id':'bind_pi','engine':'pi','native_session_id':'secret_native'}
        self.active = {'binding':self.binding, 'lease':{'generation':1},
                      'state':{'committed_offset':9,'inflight':None,'pending_switch':None}}
        self.ad.service = mock.Mock()
        self.ad.service.active.side_effect = lambda sid: copy.deepcopy(self.active)
        self.ad.messages = mock.Mock(return_value=[{}]*9)
        self.ad._turn_start = lambda msg: True
        self.ad.session_cwd = lambda sid: str(root)
        self.ad.grounding_domain = lambda sid: 'desk'
        self.journal = mock.Mock(); self.ad._recap_journal = self.journal
        self.ad._recap_project_event = mock.Mock()
        for attr, val in [('WB_AUTO_TITLES',str(root/'auto.json')),
                          ('WB_CUSTOM_TITLES',str(root/'custom.json')),
                          ('_CACHE',{'t':0.0,'v':{}})]:
            p=mock.patch.object(titles,attr,val);p.start();self.addCleanup(p.stop)
        p=mock.patch.dict('os.environ',{'DESKTRON_RECAP':'1'});p.start();self.addCleanup(p.stop)
        self.addCleanup(self.ad._recap_cancel, self.sid)

    def snap(self):
        snap=self.ad._recap_snapshot(self.sid,{'seq':9,'esid':'secret_native','opts':{'model':'chosen'}})
        self.ad._recap_snapshots[self.sid]=snap
        return snap

    def fire(self, callback=None):
        snap=self.snap()
        self.ad.recap_generate=callback or (lambda *args:'标题: 图片接入\nLOG:已验证图片接入。\nEVENT:{}')
        return self.ad._recap_fire(self.sid,9,snap,force=True)

    def test_old_conversation_gets_public_title_once(self):
        self.assertTrue(self.fire())
        self.assertEqual(titles.auto_title_for(self.sid),'图片接入')
        data=json.loads(Path(titles.WB_AUTO_TITLES).read_text())
        self.assertEqual(list(data),[self.sid])
        self.assertFalse(self.snap()['want_title'])
        self.journal.assert_called_once_with(self.sid,9,'已验证图片接入。')

    def test_manual_name_and_ordinary_turn_window_are_unchanged(self):
        Path(titles.WB_CUSTOM_TITLES).write_text(json.dumps({self.sid:'我的名称'}))
        self.assertFalse(self.snap()['want_title'])
        self.assertFalse(titles.want_autotitle('ordinary',9))

    def test_snapshot_freezes_binding_and_does_not_alias_native_sid(self):
        snap=self.snap()
        self.assertEqual(snap['engine_sid'],'')
        self.active['binding']['engine']='claude'
        self.assertEqual(snap['_shepherd_source']['binding']['engine'],'pi')
        self.assertFalse(self.ad._recap_eligible(self.sid,snap,force=True))

    def test_peer_snapshot_survives_later_user_turn_but_recap_does_not(self):
        snap = self.snap()
        self.active['state']['committed_offset'] = 10
        self.assertFalse(self.ad._recap_context_ready(self.sid, snap))
        snap['source'] = self.ad.SRC_PEER
        self.assertTrue(self.ad._recap_context_ready(self.sid, snap))

    def test_failed_or_unconfirmed_source_never_runs(self):
        for state in ('running','needs_reconciliation','source_persisted'):
            with self.subTest(state=state):
                self.active['state']['inflight']={'state':state}
                generate=mock.Mock()
                self.assertFalse(self.fire(generate));generate.assert_not_called()

    def test_generation_error_does_not_write_title_or_journal(self):
        self.assertFalse(self.fire(mock.Mock(side_effect=RuntimeError('offline'))))
        self.journal.assert_not_called()
        self.assertIsNone(titles.auto_title_for(self.sid))
        state=json.loads(Path(self.ad._recap_state_path()).read_text())
        self.assertEqual(state['fails'],1)

    def test_source_change_during_generation_discards_title_and_log(self):
        def generate(*_):
            self.active['state']['committed_offset']=10
            return '标题: 不应落盘\nLOG:已完成。\nEVENT:{}'
        self.assertFalse(self.fire(generate))
        self.journal.assert_not_called();self.assertIsNone(titles.auto_title_for(self.sid))

    def test_new_turn_cancels_old_result(self):
        def generate(*_):
            self.ad._recap_cancel(self.sid)
            return '标题: 不应落盘\nLOG:已完成。\nEVENT:{}'
        self.assertFalse(self.fire(generate));self.journal.assert_not_called()
        self.assertIsNone(titles.auto_title_for(self.sid))

    def test_title_only_result_still_checks_source_boundary(self):
        def generate(*_):
            self.active['lease']['generation']=2
            return '标题: 不应落盘'
        self.assertFalse(self.fire(generate))
        self.assertIsNone(titles.auto_title_for(self.sid))

    def test_switch_cancels_scheduled_recap_before_touching_binding(self):
        self.snap()
        self.ad.index[self.sid]['next_engine']='claude'
        def switch(*_,**__):
            self.assertNotIn(self.sid,self.ad._recap_snapshots)
            raise RuntimeError('test stops before native switch')
        self.ad.service.switch.side_effect=switch
        with self.assertRaisesRegex(RuntimeError,'test stops'):
            self.ad._apply_requested_engine(self.sid)

    def test_request_marker_consumed_only_on_success(self):
        path=Path(self.ad._recap_request_path(self.sid));path.parent.mkdir(parents=True)
        path.write_text('{}')
        self.assertFalse(self.fire(lambda *_:''));self.assertTrue(path.exists())
        self.assertTrue(self.fire());self.assertFalse(path.exists())

    def test_empty_progress_can_still_name_conversation(self):
        self.assertTrue(self.fire(lambda *_:'标题: 图片接入\nLOG:无实质进展\nEVENT:{}'))
        self.journal.assert_not_called();self.assertEqual(titles.auto_title_for(self.sid),'图片接入')

    def test_arm_reuses_delayed_scheduler(self):
        with mock.patch.object(self.ad,'_recap_schedule') as schedule:
            self.ad._recap_arm(self.sid,{'seq':9})
        self.assertEqual(schedule.call_args.args[0],self.sid)
        self.assertEqual(schedule.call_args.args[2],self.ad._recap_idle_delay())

    def test_peer_snapshot_ignores_omitted_rounds_without_source(self):
        import shepherd.recap as native_recap
        run = mock.Mock()
        run.cancelled = threading.Event()
        active = {
            'binding': {'binding_id':'bind_claude','engine':'claude','native_session_id':'native_active'},
            'lease': {'generation':1},
            'state': {
                'committed_offset': 2,
                'inflight': None,
                'pending_switch': None,
                'rounds': [
                    {'round_id':'adp_test:1', 'source': {
                        'engine':'claude', 'session_id':'native_old',
                        'kind':'native_round', 'entry_id':'adp_test:1'}},
                    {'round_id':'adp_test:2', 'outcome':'unknown',
                     'history_state':'omitted', 'recovery_reason':'source_unavailable'},
                ],
                'bindings': {
                    'old': {'binding_id':'old','engine':'claude',
                            'native_session_id':'native_old','ownership_nonce':'n'},
                    'active': {'binding_id':'bind_claude','engine':'claude',
                               'native_session_id':'native_active','ownership_nonce':'n'},
                },
            },
        }
        connector = mock.Mock()
        connector.unified = False
        connector.binding = active['binding']
        connector.engine = 'claude'
        connector.native_session_id = 'native_old'
        connector.resolve_source.return_value = {'source': [], 'public': []}
        @contextmanager
        def fake_recap_connector(*_args, **_kwargs):
            yield connector

        with mock.patch.object(self.ad.service, 'active', return_value=active), \
             mock.patch.object(self.ad, '_connector', return_value=connector), \
             mock.patch.object(native_recap.ReadProjector, 'project', return_value=[]), \
             mock.patch.object(native_recap, '_recap_connector', side_effect=fake_recap_connector), \
             mock.patch.object(native_recap, '_generate_native', return_value='OK'):
            self.assertEqual(native_recap.generate(self.ad, self.sid, run, {
                'source': self.ad.SRC_PEER,
                '_shepherd_source': copy.deepcopy(active),
                'opts': {},
            }), 'OK')

    def test_peer_cancel_during_native_copy_never_starts_pi_model(self):
        import shepherd.recap as native_recap
        started, release = threading.Event(), threading.Event()
        run = mock.Mock()
        run.cancelled = threading.Event()
        source = Path(self.tmp.name) / "source.jsonl"
        source.write_text("{}\n", encoding="utf-8")
        connector = mock.Mock()
        connector.binding = {"engine": "pi", "native_session_id": "native"}
        connector.path = str(source)
        connector._safe_path.return_value = str(source)
        called = []

        class Worker:
            @classmethod
            def recap_generate(cls, *_args):
                called.append(True)
                return "must not run"

        real_copy = native_recap.shutil.copyfile

        def blocked_copy(src, dst):
            started.set()
            release.wait(2)
            return real_copy(src, dst)

        result = []
        with mock.patch.object(native_recap.shutil, "copyfile", side_effect=blocked_copy):
            thread = threading.Thread(target=lambda: result.append(
                native_recap._generate_native(connector, Worker(), connector.binding,
                                                self.sid, run, {"source": "peer"}, {}, {})))
            thread.start()
            self.assertTrue(started.wait(1))
            run.cancelled.set()
            release.set()
            thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [""])
        self.assertEqual(called, [])

    def test_codex_isolation_survives_compatibility_fallback(self):
        import codex_appserver as ca
        from test_recap import CodexFamilyFork
        class Old(CodexFamilyFork.Conn):
            instances=[]
            frames=[('item/completed',{'item':{'type':'agentMessage','text':'LOG:无实质进展'}}),
                    ('turn/completed',{'turn':{'status':'completed'}})]
            def request(self,method,params=None,*args,**kwargs):
                if method=='thread/fork' and params.get('ephemeral'):
                    self.calls.append((method,params))
                    raise ca.AppServerError('unknown field ephemeral')
                return super().request(method,params,*args,**kwargs)
        with mock.patch.object(ca,'_Connection',Old):
            self.assertEqual(ca.recap_fork('private','recap',read_only=True),'LOG:无实质进展')
        conn=Old.instances[-1]
        for method,params in conn.calls:
            if method=='thread/fork':
                self.assertEqual(params['approvalPolicy'],'untrusted')
                self.assertEqual(params['sandbox'],'read-only')
            if method=='turn/start':
                self.assertEqual(params['sandboxPolicy'],{'type':'readOnly'})
        self.assertIn('thread/delete',[m for m,_ in conn.calls])


if __name__ == '__main__': unittest.main()


class UnifiedPiRecapCacheAffinity(unittest.TestCase):
    """2026-10-03：Shepherd Pi recap 与主回合同一参数、系统提示和会话号，只拒绝工具。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        from engine_adapters.pi import PiAdapter
        self.worker = PiAdapter(self.tmp.name, self.tmp.name, EventBus())
        self.connector = mock.Mock()
        self.connector.native_cwd = self.tmp.name
        self.connector.store_root = self.tmp.name
        self.connector.binding = {'conversation_id': 'adp_test'}
        self.opts = {'model': 'openai/gpt-test', 'effort': 'high', '_grounding': '当前约定'}

    def _process(self, *events):
        process = mock.Mock()
        process.events.return_value = iter(events)
        process.proc = mock.Mock()
        return process

    def _run(self, process, payload=None, source='user'):
        import shepherd.recap as native_recap
        from engine_adapters import recap as rc
        run = rc._RecapRun('adp_test', 9)
        ad = mock.Mock(); ad.name = 'shepherd'; ad._recap_context_ready.return_value = True
        seen = {}

        def spawn(command, **kwargs):
            seen['command'], seen['env'] = command, kwargs['env']
            seen['payload'] = json.load(open(command[3], encoding='utf-8'))
            return process

        with mock.patch.object(native_recap, 'NativeProcess', side_effect=spawn), \
                mock.patch('shepherd.pi_connector.pi_sdk_root', return_value='/sdk'):
            text = native_recap._generate_unified_pi_affine(
                ad, self.connector, self.worker, 'adp_test', run, {'source': source},
                self.opts, payload or {'id': 'native-pi', 'messages': [], 'entries': None,
                                       'fallbackReceipt': None})
        return text, run, seen

    def test_args_match_main_turn_composition_and_tools_are_denied_not_removed(self):
        from pi_extensions import append_system_prompt_if_missing, enforce_prompt_isolation
        from shepherd.history_prompt import history_prompt
        process = self._process(
            {'type': 'protocol_ready', 'sessionId': 'native-pi'},
            {'type': 'message_end', 'message': {'role': 'assistant',
             'content': [{'type': 'text', 'text': 'LOG:完成。'}],
             'usage': {'input': 40, 'output': 9, 'cacheRead': 70000, 'cacheWrite': 0}}})
        text, run, seen = self._run(process)
        self.assertEqual(text, 'LOG:完成。')
        system = '\n\n'.join((self.worker.system_body(self.opts), '当前约定'))
        expected = enforce_prompt_isolation(list(self.worker.build_cmd('', None, self.opts)[1:]))
        expected = append_system_prompt_if_missing(expected, system)
        expected = append_system_prompt_if_missing(expected, history_prompt(self.connector))
        self.assertEqual(seen['command'][4:], expected)
        self.assertNotIn('--no-tools', seen['command'])
        self.assertNotIn('--fork', seen['command'])
        self.assertEqual(seen['payload']['id'], 'native-pi')
        self.assertIs(seen['payload']['compact'], False)
        self.assertEqual(seen['env']['DESKTRON_PI_RECAP'], '1')
        process.send.assert_called_once()
        self.assertEqual(process.send.call_args.args[0]['type'], 'prompt')
        process.close.assert_called_once()
        self.assertEqual(run.cache_usage['cacheRead'], 70000)

    def test_session_mismatch_never_sends_prompt(self):
        process = self._process({'type': 'protocol_ready', 'sessionId': 'other'})
        with self.assertRaises(RuntimeError):
            self._run(process)
        process.send.assert_not_called()
        process.close.assert_called_once()

    def test_nonfinal_frames_with_text_are_not_successful_recap(self):
        for reason, tools, expected in (
            ('toolUse', True, 'Pi recap 试图调用工具，已拒绝'),
            ('stop', True, 'Pi recap 试图调用工具，已拒绝'),
            ('aborted', False, 'Pi recap 未正常结束'),
            ('length', False, 'Pi recap 未正常结束'),
            ('max_tokens', False, 'Pi recap 未正常结束'),
            ('unknown', False, 'Pi recap 未正常结束'),
            ('error', False, 'Pi 服务端错误'),
        ):
            with self.subTest(reason=reason, tools=tools):
                content = [{'type': 'text', 'text': '不能归档的过程文字'}]
                if tools:
                    content.append({'type': 'toolCall', 'name': 'read', 'arguments': {}})
                process = self._process(
                    {'type': 'protocol_ready', 'sessionId': 'native-pi'},
                    {'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': reason,
                                                      'content': content}},
                    {'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'stop',
                                                      'content': [{'type': 'text', 'text': '不要再次请求'}]}})
                text, run, _ = self._run(process)
                self.assertEqual(text, '')
                self.assertEqual(run.reason, expected)
                process.send.assert_called_once()
                process.close.assert_called_once()

    def test_tool_attempt_stops_at_first_frame(self):
        process = self._process(
            {'type': 'protocol_ready', 'sessionId': 'native-pi'},
            {'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'toolUse',
             'content': [{'type': 'toolCall', 'name': 'read', 'arguments': {}}]}},
            {'type': 'message_end', 'message': {'role': 'assistant',
             'content': [{'type': 'text', 'text': '不应读到'}]}})
        text, run, _seen = self._run(process)
        self.assertEqual(text, '')
        self.assertEqual(run.reason, 'Pi recap 试图调用工具，已拒绝')
