"""并行测试入口自身的轻量契约。"""

import ast
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "dev" / "run-tests.py"
SPEC = importlib.util.spec_from_file_location("desk_test_runner", RUNNER)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(runner)


class Discovery(unittest.TestCase):
    def test_递归包含嵌套测试但不收基准脚本(self):
        names = {p.relative_to(runner.TEST_ROOT).as_posix()
                 for p in runner.discover_tests()}
        self.assertIn("memory_engine/test_commands.py", names)
        self.assertNotIn("benchmark_codex_rollout_tail.py", names)

    def test_显式路径只接收测试文件(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            yes = root / "test_yes.py"
            no = root / "benchmark_no.py"
            yes.write_text("pass\n", encoding="utf-8")
            no.write_text("pass\n", encoding="utf-8")
            self.assertEqual(runner.discover_tests([str(root)]), [yes.resolve()])


class Selection(unittest.TestCase):
    def _suite(self, root):
        direct = root / "desktron/tests/test_direct.py"
        neighbour = root / "desktron/tests/test_neighbour.py"
        unrelated = root / "desktron/tests/test_unrelated.py"
        for path in (direct, neighbour, unrelated):
            path.parent.mkdir(parents=True, exist_ok=True)
        direct.write_text("SOURCE = 'frontend/app-chat-detail.js'\n", encoding="utf-8")
        neighbour.write_text("CHAT = _srcmap.chat_src()\n", encoding="utf-8")
        unrelated.write_text("def test_compile(): pass\n", encoding="utf-8")
        return direct, neighbour, unrelated

    def test_quick只选直接依赖_module才扩展分拆邻接(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(runner, "ROOT", Path(td)):
            direct, neighbour, unrelated = self._suite(Path(td))
            tests = [direct, neighbour, unrelated]
            quick = runner.select_changed_tests(["frontend/app-chat-detail.js"], tests)
            module = runner.select_changed_tests(
                ["frontend/app-chat-detail.js"], tests, module=True)
            self.assertEqual(quick, [direct])
            self.assertEqual(module, [direct, neighbour])

    def test_短模块名不按普通子串误选(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(runner, "ROOT", Path(td)):
            direct, _neighbour, unrelated = self._suite(Path(td))
            direct.write_text("import pi\npi.run()\n", encoding="utf-8")
            unrelated.write_text("def test_compile_and_skip(): pass\n", encoding="utf-8")
            selected = runner.select_changed_tests(
                ["desktron/lib/engine_adapters/pi.py"], [direct, unrelated])
            self.assertEqual(selected, [direct])

    def test_全局测试输入变更回退完整集合且去重(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(runner, "ROOT", Path(td)):
            direct, neighbour, unrelated = self._suite(Path(td))
            tests = [direct, neighbour, unrelated]
            self.assertEqual(runner.select_changed_tests(
                ["desktron/tests/conftest.py"], tests), tests)
            self.assertEqual(runner.select_changed_tests(
                ["desktron/tests/memory_engine/conftest.py"], tests), tests)
            self.assertEqual(len(runner.select_changed_tests(
                ["frontend/app-chat-detail.js", "frontend/app-chat-detail.js"], tests)), 1)

    def test_native文件单独发现(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            native = root / "test_native.py"
            normal = root / "test_normal.py"
            native.write_text("@pytest.mark.native\ndef test_real(): pass\n", encoding="utf-8")
            normal.write_text("def test_fast(): pass\n", encoding="utf-8")
            self.assertEqual(runner.native_tests([normal, native]), [native])


class DesktopBoundary(unittest.TestCase):
    def test_desktop_selection_supports_class_method_and_module_marks(self):
        with tempfile.TemporaryDirectory() as td:
            files = []
            for i, source in enumerate((
                '@pytest.mark.desktop\nclass TestGUI: pass\n',
                'class TestMixed:\n    @pytest.mark.desktop\n    def test_gui(self): pass\n',
                'pytestmark = pytest.mark.desktop\n',
                'pytestmark = [pytest.mark.desktop]\n',
                'def test_static(): pass\n',
            )):
                path = Path(td) / f'test_{i}.py'
                path.write_text(source, encoding='utf-8')
                files.append(path)
            self.assertEqual(runner.desktop_tests(files), files[:-1])

    def test_desktop_is_serial_even_with_parallel_environment_or_argument(self):
        with mock.patch.dict(os.environ, {'DESKTRON_TEST_JOBS':'8'}):
            self.assertEqual(runner.resolve_jobs(runner.parser().parse_args(['--desktop'])), 1)
            self.assertEqual(runner.resolve_jobs(runner.parser().parse_args(['--desktop', '-j', '8'])), 1)

    def test_daily_worker_cannot_inherit_desktop_opt_in_or_debug(self):
        flags = {'DESKTRON_DESKTOP_TESTS':'1', 'DESK_WEBKIT_TESTS':'1',
                 'DESK_SHEPHERD_IMAGE_UI':'1', 'PWDEBUG':'1'}
        with mock.patch.dict(os.environ, flags):
            self.assertFalse(any(name in runner.worker_env() for name in flags))
            desktop = runner.worker_env('desktop')
            self.assertEqual(desktop['DESKTRON_DESKTOP_TESTS'], '1')
            self.assertEqual(desktop['DESK_WEBKIT_TESTS'], '1')
            self.assertNotIn('PWDEBUG', desktop)

    def test_default_runs_static_test_but_never_enters_gui_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'test_mixed.py'
            sentinel = Path(td) / 'gui-started'
            path.write_text(
                'import pytest\nfrom pathlib import Path\n'
                '@pytest.fixture\ndef browser():\n'
                f'    Path({str(sentinel)!r}).touch()\n'
                '    return True\n'
                '@pytest.mark.desktop\ndef test_gui(browser): assert browser\n'
                'def test_static(): assert True\n', encoding='utf-8')
            ordinary = runner.run_file(path, 15)
            self.assertEqual(ordinary[1], 0, ordinary[-1])
            self.assertEqual(ordinary[2], 1)
            self.assertFalse(sentinel.exists())
            desktop = runner.run_file(path, 15, marker='desktop')
            self.assertEqual(desktop[1], 0, desktop[-1])
            self.assertEqual(desktop[2], 1)
            self.assertTrue(sentinel.exists())

    def test_direct_pytest_also_skips_gui_before_fixture_setup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'conftest.py').write_text((runner.TEST_ROOT / 'conftest.py').read_text(encoding='utf-8'), encoding='utf-8')
            sentinel = root / 'gui-started'
            path = root / 'test_direct.py'
            path.write_text(
                'import pytest\nfrom pathlib import Path\n'
                '@pytest.fixture\ndef browser():\n'
                f'    Path({str(sentinel)!r}).touch()\n'
                '    return True\n'
                '@pytest.mark.desktop\ndef test_gui(browser): assert browser\n', encoding='utf-8')
            result = subprocess.run([sys.executable, '-m', 'pytest', '-q', str(path)],
                cwd=root, env=runner.worker_env(), capture_output=True, text=True, timeout=15, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('1 skipped', result.stdout)
            self.assertFalse(sentinel.exists())

    def _assert_gui_markers(self, root):
        # A newly added launcher must not silently enter daily regression.
        for path in root.rglob('test_*.py'):
            if path == Path(__file__).resolve():
                continue
            source = path.read_text(encoding='utf-8')
            tree = ast.parse(source)
            def marked(node):
                return any(ast.unparse(d) == 'pytest.mark.desktop'
                           for d in getattr(node, 'decorator_list', ()))
            module_marked = any(isinstance(n, ast.Assign) and
                any(isinstance(t, ast.Name) and t.id == 'pytestmark' for t in n.targets) and
                'pytest.mark.desktop' in ast.unparse(n.value) for n in tree.body)
            for top in tree.body:
                if not isinstance(top, (ast.ClassDef, ast.FunctionDef)):
                    continue
                functions = [top] if isinstance(top, ast.FunctionDef) else [
                    f for f in top.body if isinstance(f, ast.FunctionDef)]
                for function in functions:
                    calls = [n for n in ast.walk(function) if isinstance(n, ast.Call)]
                    launches = 'playwright' in source and any(
                        isinstance(n.func, ast.Attribute) and n.func.attr in
                        ('launch', 'launch_persistent_context', 'sync_playwright')
                        or isinstance(n.func, ast.Name) and n.func.id == 'sync_playwright'
                        for n in calls)
                    text = ast.get_source_segment(source, function)
                    swift_gui = 'swiftc' in text and ('fixtures/' in text or 'WKWebView' in text)
                    if not launches and not swift_gui:
                        continue
                    if module_marked or marked(top) or marked(function):
                        continue
                    # A browser fixture is safe only when every consumer is opted in.
                    fixture = any(ast.unparse(d).startswith('pytest.fixture')
                                  for d in function.decorator_list)
                    consumers = [n for n in tree.body if isinstance(n, ast.FunctionDef) and
                                 n.name.startswith('test_') and
                                 function.name in [a.arg for a in n.args.args]]
                    self.assertTrue(fixture and consumers and all(marked(n) for n in consumers),
                                    f'{path.name}:{function.name} needs @pytest.mark.desktop')


    def test_browser_launchers_and_native_window_consumers_are_marked(self):
        self._assert_gui_markers(runner.TEST_ROOT)

    def test_nested_unmarked_gui_launcher_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / 'nested' / 'test_browser.py'
            path.parent.mkdir()
            source = ('from playwright.sync_api import sync_playwright\n'
                      'def test_gui():\n    with sync_playwright() as pw:\n'
                      '        pw.webkit.launch()\n')
            path.write_text(source, encoding='utf-8')
            with self.assertRaisesRegex(AssertionError, 'needs @pytest.mark.desktop'):
                self._assert_gui_markers(root)
            path.write_text(source.replace('def test_gui():', '@pytest.mark.desktop\ndef test_gui():'), encoding='utf-8')
            self._assert_gui_markers(root)


class Reporting(unittest.TestCase):
    def test_汇总通过与跳过数量(self):
        self.assertEqual(runner.parse_counts("17 passed, 2 skipped in 0.4s"), (17, 2))
        self.assertEqual(runner.parse_counts("1 skipped in 0.1s"), (0, 1))

    def test_native默认串行且显式并发仍优先(self):
        native = runner.parser().parse_args(["--native"])
        self.assertEqual(runner.resolve_jobs(native), 1)
        explicit = runner.parser().parse_args(["--native", "--jobs", "3"])
        self.assertEqual(runner.resolve_jobs(explicit), 3)
        ordinary = runner.parser().parse_args([])
        with mock.patch.dict(os.environ, {"DESKTRON_TEST_JOBS":"5"}):
            self.assertEqual(runner.resolve_jobs(ordinary), 5)


class Execution(unittest.TestCase):
    def test_worker_does_not_inherit_host_conversation_writer(self):
        with mock.patch.dict(os.environ, {'DESKTRON_NATIVE_WRITER_SESSION_ID': 'host-session'}):
            self.assertNotIn('DESKTRON_NATIVE_WRITER_SESSION_ID', runner.worker_env())
            self.assertEqual(os.environ['DESKTRON_NATIVE_WRITER_SESSION_ID'], 'host-session')

    def test_纯native文件不让默认门禁误报空测试(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test_native.py"
            path.write_text(
                "import pytest\n@pytest.mark.native\ndef test_real(): assert True\n",
                encoding="utf-8")
            result = runner.run_file(path, 15)
            self.assertEqual(result[1], 0, result[-1])
            native = runner.run_file(path, 15, marker="native")
            self.assertEqual(native[1], 0, native[-1])
            self.assertEqual(native[2], 1)

    def test_失败空文件和错路径均不能通过门禁(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test_fixture.py"
            for src, expected in (("def test_bad(): assert False\n", 1),
                                  ("# no tests\n", 1),
                                  ('import pytest\npytestmark = pytest.mark.desktop\n', 1),
                                  ('import pytest\n@pytest.mark.native\nclass TestEmpty: pass\n', 1)):
                with self.subTest(src=src):
                    path.write_text(src, encoding="utf-8")
                    cp = subprocess.run([sys.executable, str(RUNNER), str(path)],
                                        capture_output=True, text=True, timeout=15, encoding='utf-8')
                    self.assertEqual(cp.returncode, expected, cp.stdout + cp.stderr)
            cp = subprocess.run([sys.executable, str(RUNNER), str(path), str(path)+'missing'],
                                capture_output=True, text=True, timeout=15, encoding='utf-8')
            self.assertEqual(cp.returncode, 2, cp.stdout + cp.stderr)

    def _sleeping_test(self, root):
        child_pid = root / "child.pid"
        path = root / "test_sleep.py"
        path.write_text(
            "import subprocess, sys, time\nfrom pathlib import Path\n"
            "def test_wait():\n"
            "    child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])\n"
            f"    Path({str(child_pid)!r}).write_text(str(child.pid))\n"
            "    print('worker started', flush=True)\n"
            "    time.sleep(30)\n", encoding="utf-8")
        return path, child_pid

    def _assert_child_stopped(self, pid_file):
        self.assertTrue(pid_file.exists(), "夹具必须实际启动子进程")
        pid = int(pid_file.read_text(encoding='utf-8'))
        until = time.monotonic() + 3
        while time.monotonic() < until:
            cp = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='],
                                capture_output=True, text=True, encoding='utf-8')
            if not cp.stdout.strip() or cp.stdout.strip().startswith('Z'):
                return
            time.sleep(0.05)
        self.fail(f"测试子进程 {pid} 未停止")

    def test_超时终止测试及其子进程(self):
        with tempfile.TemporaryDirectory() as td:
            path, pid_file = self._sleeping_test(Path(td))
            result = runner.run_file(path, 3)
            self.assertEqual(result[1], 124, result[-1])
            self.assertIn("超时", result[-1])
            self._assert_child_stopped(pid_file)

    @unittest.skipIf(os.name == 'nt', 'POSIX SIGINT 中断；Windows 不向子进程发送此信号')
    def test_中断清理工作进程不等待完整超时(self):
        with tempfile.TemporaryDirectory() as td:
            path, pid_file = self._sleeping_test(Path(td))
            proc = subprocess.Popen([sys.executable, str(RUNNER), str(path)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
            try:
                until = time.monotonic() + 10
                while not pid_file.exists() and time.monotonic() < until:
                    time.sleep(0.05)
                self.assertTrue(pid_file.exists())
                proc.send_signal(signal.SIGTERM)
                out, err = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 130, out + err)
                self._assert_child_stopped(pid_file)
            finally:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                    proc.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
