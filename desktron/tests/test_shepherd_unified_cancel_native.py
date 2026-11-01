"""Real Codex interrupt protocol against a delayed loopback model only."""
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest import mock

import pytest
import codex_appserver as app
from shepherd.codex_connector import CodexFamilyConnector
from shepherd.harness import ExecutionHarness
from shepherd.service import Service
from shepherd.unified_runners import UnifiedRunner
from test_shepherd_image_execution import ImageModel


@pytest.mark.native
class UnifiedCancelNative(unittest.TestCase):
    def test_interrupt_then_continue_on_real_codex(self):
        for engine in ('codex',):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as folder:
                self.assertTrue(shutil.which(engine), engine + ' is required')
                root = Path(folder)
                work = root / 'work'
                work.mkdir()
                requested, release = threading.Event(), threading.Event()
                def delayed_tokens(_body):
                    requested.set()
                    release.wait(30)
                    return 100
                model = ImageModel(input_tokens=delayed_tokens)
                def argv(binary, opts=None):
                    return [binary, '-c', 'model_provider="fixture"', '-c', 'model="gpt-5.6"',
                        '-c', 'model_providers.fixture.name="Fixture"',
                        '-c', f'model_providers.fixture.base_url="{model.url}/v1"',
                        '-c', 'model_providers.fixture.wire_api="responses"',
                        '-c', 'model_providers.fixture.requires_openai_auth=false',
                        *(['app-server', '--stdio'] if os.path.basename(binary) == 'codex'
                          else ['app-server', '--listen', 'stdio://'])]
                thread = None
                binding_id = None
                try:
                    with mock.patch.object(app, '_appserver_argv', side_effect=argv), \
                         mock.patch.object(CodexFamilyConnector, '_seed_private_config'):
                        service = Service(root / 'state', storage='unified')
                        service.create('cancel', engine, work)
                        active = service.active('cancel')
                        binding_id = active['binding']['binding_id']
                        connector = service.connector(active['binding'])
                        lease = active['lease']
                        def execute(runner, prompt):
                            return ExecutionHarness(service.coordinator, connector, runner).run(
                                'cancel', lease_id=lease['lease_id'], generation=lease['generation'],
                                prompt=prompt, input_public=[{'type': 'message', 'role': 'user',
                                    'content': [{'type': 'input_text', 'text': prompt}]}])
                        options = {'model': 'gpt-5.6', 'sandbox': 'read-only',
                                   '_context_window': 1000000}
                        runner = UnifiedRunner(connector, options=options, timeout=20)
                        result, errors = [], []
                        def run():
                            try:
                                result.append(execute(runner, 'wait for an answer'))
                            except Exception as exc:
                                errors.append(exc)
                        thread = threading.Thread(target=run, daemon=True)
                        thread.start()
                        self.assertTrue(requested.wait(15), 'native model request did not start')
                        runner.cancel()
                        thread.join(10)
                        self.assertFalse(thread.is_alive(), 'Esc did not settle the native turn')
                        self.assertEqual(errors, [])
                        self.assertEqual(result[0]['terminal_state'], 'interrupted_confirmed')
                        self.assertIsNone(runner.controller.connection)
                        self.assertIsNone(runner.controller._cancel_timer)
                        release.set()
                        following = UnifiedRunner(connector, options=options, timeout=20)
                        result = execute(following, 'continue after Esc')
                        self.assertEqual(result['terminal_state'], 'completed')
                        self.assertIsNone(service.coordinator.state('cancel').get('active_turn'))
                finally:
                    release.set()
                    if thread is not None:
                        thread.join(25)
                    if binding_id:
                        from shepherd.resident_process import drop
                        app.drop_session('shepherd:' + binding_id)
                        drop(binding_id)
                    model.close()
