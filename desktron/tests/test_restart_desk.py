"""restart-desk is a thin client of the native host supervisor."""
import io
import json
import os
import types
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, "desktron", "bin", "restart-desk")


def load():
    source = open(SCRIPT, encoding="utf-8").read().replace(
        'if __name__ == "__main__":\n    raise SystemExit(main())\n', '')
    module = types.ModuleType('restart_desk'); module.__file__ = SCRIPT
    exec(compile(source, SCRIPT, 'exec'), module.__dict__)
    return module


class RestartCLI(unittest.TestCase):
    def setUp(self): self.m = load()

    def test_preview_is_read_only(self):
        self.m.request = mock.Mock(return_value={
            'boot_id':'old','supervised':True,'host_instance_id':'host'})
        self.assertEqual(self.m.main([]), 0)
        self.m.request.assert_called_once_with('/workbench/runtime')

    def test_requires_supervised_native_host(self):
        self.m.request = mock.Mock(return_value={'boot_id':'old','supervised':False})
        self.assertEqual(self.m.main(['--yes']), 1)
        self.m.request.assert_called_once_with('/workbench/runtime')

    def test_detach_is_compatibility_flag_not_launchd(self):
        replies = iter([
            {'boot_id':'old','supervised':True},
            {'ok':True,'state':'accepted'},
        ])
        self.m.request = mock.Mock(side_effect=lambda *_a, **_k: next(replies))
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            self.assertEqual(self.m.main(['--detach','--yes']), 0)
        calls = self.m.request.call_args_list
        self.assertEqual(calls[1].args[0], '/workbench/restart')
        self.assertRegex(calls[1].args[1]['operation_id'], r'^[0-9a-f]{32}$')
        self.assertEqual(calls[1].args[1]['expected_boot_id'], 'old')
        self.assertEqual(json.loads(out.getvalue())['old_boot_id'], 'old')
        self.assertEqual(json.loads(out.getvalue())['state'], 'accepted')
        source = open(SCRIPT, encoding='utf-8').read()
        for forbidden in ('launchctl', 'os.kill(', 'SIGTERM', 'SIGKILL', 'open -a',
                          'restart-drain', 'restore_rc', 'drain_shepherd'):
            self.assertNotIn(forbidden, source)

    def test_blocker_is_reported_without_retry_loop(self):
        replies = iter([
            {'boot_id':'old','supervised':True},
            {'ok':False,'error':'仍有不可中断操作',
             'blockers':[{'kind':'app_control','count':1}]},
        ])
        self.m.request = mock.Mock(side_effect=lambda *_a, **_k: next(replies))
        self.assertEqual(self.m.main(['--yes']), 1)
        self.assertEqual(self.m.request.call_count, 2)

    def test_pinned_identity_rejects_changed_backend_before_mutation(self):
        for flags in (['--expected-boot-id', 'expected'],
                      ['--expected-host-instance-id', 'expected']):
            with self.subTest(flags=flags):
                self.m.request = mock.Mock(return_value={
                    'boot_id': 'foreign', 'host_instance_id': 'foreign', 'supervised': True})
                self.assertEqual(self.m.main(['--yes', *flags]), 1)
                self.m.request.assert_called_once_with('/workbench/runtime')

    def test_matching_pinned_identity_uses_normal_supervisor_protocol(self):
        self.m.request = mock.Mock(side_effect=[
            {'boot_id': 'expected', 'host_instance_id': 'host', 'supervised': True},
            {'ok': True, 'state': 'accepted'}])
        self.assertEqual(self.m.main(['--yes', '--expected-boot-id', 'expected',
                                     '--expected-host-instance-id', 'host']), 0)
        self.assertEqual(self.m.request.call_count, 2)
        self.assertEqual(self.m.request.call_args.args[1]['expected_boot_id'], 'expected')

    def test_bad_identity_arguments_rejected_without_runtime_request(self):
        for args in (['--expected-boot-id'], ['--expected-boot-id', '--yes'],
                     ['--expected-boot-id', 'one', '--expected-boot-id', 'two']):
            with self.subTest(args=args):
                self.m.request = mock.Mock()
                self.assertEqual(self.m.main(args), 2)
                self.m.request.assert_not_called()

    def test_unknown_arguments_are_not_echoed_into_logs(self):
        self.m.request = mock.Mock()
        error = io.StringIO()
        with mock.patch('sys.stderr', error):
            self.assertEqual(self.m.main(['fixture-do-not-log']), 2)
        self.assertNotIn('fixture-do-not-log', error.getvalue())
        self.m.request.assert_not_called()

    def test_model_convention_keeps_authorized_entry_but_not_launchd(self):
        agents = open(os.path.join(ROOT, 'AGENTS.md'), encoding='utf-8').read()
        self.assertIn('restart-desk" --detach --yes', agents)
        self.assertIn('盘点共享工作树的全部改动', agents)


if __name__ == '__main__': unittest.main()
