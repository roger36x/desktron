"""创建会话必须使用请求指定的引擎，不更改或回落全局偏好。"""
from contextlib import nullcontext
from io import BytesIO
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron/lib'))
_argv = sys.argv
sys.argv = ['ui_server.py', str(ROOT / 'frontend'), '7999', '7998', str(ROOT)]
try:
    import ui_contract
    import self_update
finally:
    sys.argv = _argv


class Handler(ui_contract.UiContractMixin):
    def __init__(self, body):
        raw = json.dumps(body).encode()
        self.path = '/session'
        self.headers = {'Content-Length': str(len(raw))}
        self.rfile = BytesIO(raw)

    def _send_json(self, obj, code=200):
        self.out, self.code = obj, code


class CreateEngine(unittest.TestCase):
    def call(self, body, unavailable=False, admitted=True):
        h = Handler(body)
        adapter = mock.Mock()
        adapter.create_session.return_value = {'id': 'new-session'}
        with mock.patch.object(ui_contract, '_engine_pref', return_value='claude'), \
             mock.patch.object(ui_contract, '_adapter', return_value=adapter) as select, \
             mock.patch.object(self_update, 'admit_work', return_value=nullcontext(admitted)):
            if unavailable:
                select.side_effect = RuntimeError('not available')
            h._adapter_route('POST')
        return h, adapter, select

    def test_explicit_engine_and_workflow_survive_creation(self):
        for engine in ('codex', 'pi', 'claude'):
            with self.subTest(engine=engine):
                h, adapter, select = self.call({'engine': engine, 'domain': 'workflow',
                                                'task': True, 'desk_role': ''})
                self.assertEqual(h.code, 200)
                select.assert_called_once_with(engine)
                adapter.create_session.assert_called_once_with(
                    'workflow', task=True, opts={'desk_role': ''})

    def test_old_client_uses_current_preference(self):
        h, _, select = self.call({'domain': 'workflow'})
        self.assertEqual(h.code, 200)
        select.assert_called_once_with('claude')

    def test_invalid_input_never_creates_in_default_engine(self):
        for body in ({'engine': 'missing'}, {'engine': []}, [], None):
            with self.subTest(body=body):
                h, adapter, select = self.call(body)
                self.assertEqual(h.code, 400)
                select.assert_not_called()
                adapter.create_session.assert_not_called()

    def test_unavailable_engine_does_not_fallback(self):
        h, adapter, select = self.call({'engine': 'pi'}, unavailable=True)
        self.assertEqual(h.code, 503)
        select.assert_called_once_with('pi')
        adapter.create_session.assert_not_called()

    def test_update_admission_still_blocks_creation(self):
        h, adapter, _ = self.call({'engine': 'codex'}, admitted=False)
        self.assertEqual(h.code, 409)
        adapter.create_session.assert_not_called()
