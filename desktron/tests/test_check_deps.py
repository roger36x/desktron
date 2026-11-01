"""check_deps regression tests.

Run: python3 -m unittest desktron/tests/test_check_deps.py
"""
import io
from itertools import product
import os
from contextlib import redirect_stdout
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import check_deps  # noqa: E402


class CodexInstallAdvice(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'POSIX ~/.local npm prefix 建议不适用于 Windows 安装')
    def test_warns_when_desk_codex_path_differs_from_npm_prefix(self):
        home = "/Users/tester"
        with mock.patch.dict(os.environ, {"HOME": home}, clear=False), \
             mock.patch("shutil.which", return_value=f"{home}/.local/bin/codex"), \
             mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=f"{home}/.nvm/versions/node/v22.22.1\n", stderr="")

            detail = check_deps.engine_detail("codex", "0.142.5", "已装")

        self.assertIn("Desk 优先使用 ~/.local/bin/codex", detail)
        self.assertIn("npm i -g --prefix \"$HOME/.local\" @openai/codex", detail)

    def test_does_not_warn_when_npm_prefix_matches_desk_prefix(self):
        home = "/Users/tester"
        with mock.patch.dict(os.environ, {"HOME": home}, clear=False), \
             mock.patch("shutil.which", return_value=f"{home}/.local/bin/codex"), \
             mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=f"{home}/.local\n", stderr="")

            detail = check_deps.engine_detail("codex", "0.142.5", "0.142.5")

        self.assertEqual(detail, "0.142.5")


class CodexCompatibility(unittest.TestCase):
    def test_engine_ready_requires_both_binary_and_protocol(self):
        with mock.patch.object(check_deps, 'probe', return_value=(True, '0.87.1')), \
             mock.patch.object(check_deps.engine_protocol, 'availability', return_value={'available': False}):
            self.assertEqual(check_deps.engine_ready('pi'), 1)
        with mock.patch.object(check_deps, 'probe', return_value=(True, '0.87.1')), \
             mock.patch.object(check_deps.engine_protocol, 'availability', return_value={'available': True}):
            self.assertEqual(check_deps.engine_ready('pi'), 0)

    def test_dependency_report_uses_protocol_not_release_number(self):
        for engine, (version, expected) in product(('codex','claude','pi'),
                [('0.154.0', 'warn'), ('9.999.0', 'ok'),
                 ('0.156.0', 'warn'), ('0.155.0-beta.1', 'ok'), ('', 'ok')]):
            with self.subTest(engine=engine, version=version), \
                 mock.patch.object(sys, 'argv', ['check_deps.py', '/fixture']), \
                 mock.patch.object(check_deps, 'parse_reqs', return_value=[
                     {'kind': 'engine', 'name': engine}]), \
                 mock.patch.object(check_deps, 'probe', return_value=(True, version)), \
                 mock.patch.object(check_deps.engine_protocol, 'availability', return_value={
                     'available': expected == 'ok', 'message': '缺少 thread/inject_items；消息未派发。'}), \
                 mock.patch.object(check_deps, 'engine_detail', side_effect=lambda _n, _v, text: text), \
                 redirect_stdout(io.StringIO()) as output:
                self.assertEqual(check_deps.main(), 0)
            self.assertIn(f'[{expected:>4}]', output.getvalue())
            if expected == 'warn':
                self.assertIn('缺少 thread/inject_items', output.getvalue())

    def test_codex_probe_preserves_prerelease_suffix(self):
        with mock.patch('shutil.which', return_value='/fixture/codex'), \
             mock.patch('subprocess.run', return_value=mock.Mock(returncode=0,
                       stdout='codex-cli 0.155.0-beta.1', stderr='')):
            self.assertEqual(check_deps.probe('codex'), (True, '0.155.0-beta.1'))


if __name__ == "__main__":
    unittest.main()
