"""Required macOS boundary tests: real Foundation.Process, real OS identities and HTTP.

No installed app, user data, port 7765, network services or AppKit are used.
This is deliberately NOT marked native (optional engine tests): launch safety belongs in CI.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron/lib'))
import self_update_apply as apply


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'requires macOS Swift')
class NativeLaunchContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='desk-native-contract-')
        cls.root = Path(cls.tmp.name)
        main = cls.root / 'main.swift'
        main.write_text(r'''
import Foundation
import Darwin
if runUpdateLaunchContractCheckIfRequested() { exit(0) }
let child = Process()
let env = ProcessInfo.processInfo.environment
child.executableURL = URL(fileURLWithPath: env["DESK_TEST_PYTHON"]!)
child.arguments = [env["DESK_TEST_BACKEND"]!]
var childEnv = env
let resources = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent()
    .deletingLastPathComponent().appendingPathComponent("Resources")
childEnv["DESK_TEST_REV"] = try String(contentsOf: resources.appendingPathComponent("release-rev"), encoding: .utf8)
childEnv["DESK_TEST_BUILD"] = try String(contentsOf: resources.appendingPathComponent("release-build"), encoding: .utf8)
childEnv["DESK_TEST_HOST"] = String(getpid())
childEnv["DESK_TEST_INSTANCE"] = UUID().uuidString
child.environment = childEnv
try runNativeBackendProcess(child)
child.waitUntilExit()
''')
        cls.binary = cls.root / 'host'
        result = subprocess.run(['swiftc', str(ROOT / 'desktop/native/backend_supervisor.swift'),
                                 str(main), '-o', str(cls.binary)], capture_output=True, text=True, timeout=90)
        if result.returncode:
            cls.tmp.cleanup()
            raise AssertionError(result.stderr)
        cls.backend = cls.root / 'backend.py'
        cls.backend.write_text('''import http.server, json, os, uuid
boot = uuid.uuid4().hex
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if self.path == '/desk-probe':
            value = dict(desk=True, pid=os.getpid(), host_pid=int(os.environ['DESK_TEST_HOST']),
                         boot_id=boot, host_instance_id=os.environ['DESK_TEST_INSTANCE'],
                         launch_generation=boot, rev=os.environ['DESK_TEST_REV'].strip())
        elif self.path == '/workbench/update-check':
            value = dict(host_rev=os.environ['DESK_TEST_REV'].strip(),
                         host_build=int(os.environ['DESK_TEST_BUILD']))
        else: value = dict(local=True, enabled=False)
        data = json.dumps(value).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(data)
http.server.HTTPServer(('127.0.0.1', int(os.environ['DESK_TEST_PORT'])), Handler).serve_forever()
''')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.case = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.case.cleanup)
        self.base = Path(self.case.name)
        self.log = str(self.base / 'host.log')
        self.processes = []
        self.addCleanup(self.cleanup_processes)
        self.launch = apply.launch_app_exact
        def launch(*args, **kwargs):
            process = self.launch(*args, **kwargs)
            self.processes.append(process)
            return process
        patch = mock.patch.object(apply, 'launch_app_exact', side_effect=launch)
        patch.start(); self.addCleanup(patch.stop)

    def app(self, name, rev='test', build=1):
        app = self.base / (name + '.app')
        (app / 'Contents/MacOS').mkdir(parents=True)
        resources = app / 'Contents/Resources'; resources.mkdir()
        shutil.copy2(self.binary, app / 'Contents/MacOS/desktron')
        for key, value in {'release-rev':rev, 'release-build':str(build),
                           'update-launch-contract':'1'}.items():
            (resources / key).write_text(value)
        return str(app)

    def cleanup_processes(self):
        # Every signalled PID has a birth token observed under this test's own wrapper.
        for process in self.processes:
            if process.poll() is None:
                apply.stop_exact(process, timeout=.3)
            else:
                receipts = getattr(process, '_desk_descendant_receipts', {})
                apply._signal_detached_receipts(receipts, -1, signal.SIGTERM)
                end = time.monotonic() + 2
                while any(apply.process_matches_token(p, t) for p, t in receipts.items()) and time.monotonic() < end:
                    time.sleep(.02)
                apply._signal_detached_receipts(receipts, -1, signal.SIGKILL)
            process.wait(timeout=3)

    def assert_clean(self):
        for process in self.processes:
            self.assertIsNotNone(process.poll())
            self.assertFalse(any(apply.process_matches_token(pid, token)
                                 for pid, token in process._desk_descendant_receipts.items()))

    def test_real_staged_binary_preflight_and_cleanup(self):
        apply.verify_staged_launch_contract(self.app('candidate'), self.log)
        self.assert_clean()

    def test_original_equal_pgid_assumption_is_rejected_before_switch(self):
        original = apply.native_process_checks
        def broken(host, backend, **kwargs):
            checks = original(host, backend, **kwargs)
            checks['old_equal_pgid_rule'] = os.getpgid(host) == os.getpgid(backend)
            return checks
        with mock.patch.object(apply, 'native_process_checks', side_effect=broken):
            with self.assertRaisesRegex(RuntimeError, '契约自检失败'):
                apply.verify_staged_launch_contract(self.app('candidate'), self.log)
        self.assert_clean()

    def test_unsupported_candidate_never_launches(self):
        app = self.app('unsupported')
        (Path(app) / 'Contents/Resources/update-launch-contract').unlink()
        with self.assertRaisesRegex(RuntimeError, '缺少受支持'):
            apply.verify_staged_launch_contract(app, self.log)
        self.assertEqual(self.processes, [])

    def test_candidate_rejected_then_real_old_host_accepted_with_same_safety_core(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        env = {'PATH':os.environ['PATH'], 'HOME':str(self.base),
               'DESK_TEST_PYTHON':sys.executable, 'DESK_TEST_BACKEND':str(self.backend),
               'DESK_TEST_PORT':str(port)}
        previous = dict(boot_id='previous-boot', host_instance_id='previous-host', launch_generation='previous-gen')
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(apply, 'PORT', port):
            candidate = apply.launch_app_exact(self.app('candidate', 'new', 2), self.log)
            ok, detail = apply.probe('new', timeout=8, expect_host_rev='new', expect_host_build=2,
                                    expected_process=candidate, previous_identity=previous, stable_for=.1)
            self.assertTrue(ok, detail)
            backend = candidate._desk_backend_pid
            self.assertNotEqual(os.getpgid(backend), os.getpgid(candidate._desk_host_pid))
            self.assertEqual(os.getsid(backend), candidate._desk_pgid)
            # Reject a candidate-only version requirement; full cleanup precedes old launch.
            ok, _ = apply.probe('wrong', timeout=.1, expected_process=candidate)
            self.assertFalse(ok)
            apply.stop_exact(candidate, timeout=.5)
            self.assertTrue(apply.wait_backend_down(2, stable_for=.1))
            accepted = []
            ok, detail = apply.recover_native_host(self.app('old', 'old', 1), self.log,
                expect_rev='old', expect_host_rev='old', expect_host_build=1,
                previous_identity=previous, expect_remote_wss=False, attempts=1,
                before_release=lambda p: accepted.append(apply.accepted_launch_identity(p)))
            self.assertTrue(ok, detail)
            self.assertEqual(len(accepted), 1)
            self.assertEqual(apply.port_owner(), accepted[0]['pid'])
            self.assertNotEqual(accepted[0]['pid'], backend)


if __name__ == '__main__':
    unittest.main()
