"""Windows 原生行为测试：只在 Windows 上运行(CI 的 windows runner 或真机)。

覆盖兼容层的真实系统调用：文件锁、进程存活、Job Object 整组结束、os.kill(pid, 0) 不发送
Ctrl+C、端口占用者、回环对端 PID、os.open 二进制模式、os.replace 重试，以及 ConPTY 终端。
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "desktron" / "lib"
sys.path.insert(0, str(LIB))
if len(sys.argv) < 5 or not sys.argv[2].isdigit():
    sys.argv = ["test", str(ROOT / "frontend"), "0", "0", tempfile.mkdtemp()]

WINDOWS_ONLY = unittest.skipUnless(sys.platform == "win32", "Windows only")

import oscompat  # noqa: E402


def _sleeper(seconds=30, **kwargs):
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(%d)" % seconds], **kwargs)


@WINDOWS_ONLY
class Polyfill(unittest.TestCase):
    def test_fcntl_shim_installed(self):
        import fcntl
        self.assertIs(fcntl, oscompat._FcntlShim)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lock")
            a = os.open(path, os.O_CREAT | os.O_RDWR)
            b = os.open(path, os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(a, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(b, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # 锁在文件内容之外，持锁时仍可读写正文。
                os.write(b, b"data")
                fcntl.flock(a, fcntl.LOCK_UN)
            finally:
                os.close(a)
                os.close(b)

    def test_json_index_commit_and_backup(self):
        from durable_json import read, read_value, write
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "adapter" / "index.json"
            snapshot = read(path)
            snapshot["session"] = {"title": "initial"}
            write(path, snapshot)
            self.assertEqual(snapshot.base, read_value(path))
            snapshot["session"]["title"] = "updated"
            write(path, snapshot)
            self.assertEqual(read_value(path)["session"]["title"], "updated")
            self.assertEqual(read_value(str(path) + ".previous")["session"]["title"], "initial")
            self.assertEqual(snapshot.base, read_value(path))

    def test_atomic_snapshot_writers(self):
        import json
        from shepherd.store import _replace_json
        from self_update import _atomic_json
        from self_update_apply import atomic_json
        from shepherd.execution_scratch import ExecutionScratch
        with tempfile.TemporaryDirectory() as tmp:
            for writer in (_replace_json, _atomic_json, atomic_json):
                path = Path(tmp) / (writer.__module__.replace(".", "-") + ".json")
                for revision in (1, 2):
                    writer(str(path), {"revision": revision})
                    self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"revision": revision})
            scratch = ExecutionScratch.__new__(ExecutionScratch)
            scratch.path = Path(tmp)
            scratch.data = {"revision": 1}
            scratch._save()
            self.assertEqual(json.loads((Path(tmp) / "owner.json").read_text()), scratch.data)

    def test_git_bash_entry_forwards_arguments_and_exit_code(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("Git Bash not installed")
        with tempfile.TemporaryDirectory() as tmp:
            python = Path(tmp) / "fake-python"
            python.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\nexit 17\n", newline="\n")
            # Enter via a script: Windows CRT and MSYS parse quoted argv
            # differently. This tests the shell-to-launcher boundary, not CRT.
            driver = Path(tmp) / "driver.sh"
            driver.write_text('exec bash "$(cygpath -u "$DESK_TEST_ENTRY")" test-command '
                              '"argument with spaces" \'quoted="value"\'\n', newline="\n")
            env = dict(os.environ, DESKTRON_PYTHON=str(python), HOME=tmp, USERPROFILE=tmp,
                       DESK_TEST_ENTRY=str(ROOT / "desktron/bin/desktron"))
            result = subprocess.run([bash, str(driver)], env=env, capture_output=True,
                                    text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 17, result.stderr)
            lines = result.stdout.splitlines()
            self.assertTrue(lines[0].endswith("/lib/desk_launcher.py"))
            self.assertEqual(lines[1:], ["test-command", "argument with spaces", 'quoted="value"'])

    def test_shepherd_turn_ids_use_safe_distinct_filenames(self):
        from shepherd.turn_inputs import TurnInputs
        with tempfile.TemporaryDirectory() as tmp:
            inputs = TurnInputs(tmp)
            public = inputs.public_input("fixture 中文 😀")
            first = inputs.write("fixture", "fixture:1", public)
            second = inputs.write("fixture", "fixture_1", public)
            self.assertEqual(first["turn_id"], "fixture:1")
            self.assertNotEqual(inputs.reference_path(first), inputs.reference_path(second))
            self.assertNotIn(":", Path(inputs.reference_path(first)).name)
            self.assertEqual(inputs.read(first), public)
            self.assertEqual(inputs.read(second), public)
            self.assertEqual(inputs.write("fixture", "fixture:1", public), first)
            self.assertTrue(inputs.discard(first))
            self.assertEqual(inputs.read(second), public)

    def test_old_filename_safe_shepherd_input_is_still_readable(self):
        import json
        from shepherd.turn_inputs import TurnInputs
        with tempfile.TemporaryDirectory() as tmp:
            inputs = TurnInputs(tmp)
            public = inputs.public_input("legacy fixture")
            reference = {"conversation_id": "fixture", "turn_id": "safe-turn",
                         "input_hash": inputs._hash(public)}
            old = Path(tmp) / "inputs" / "fixture" / ("safe-turn." + reference["input_hash"] + ".json")
            old.parent.mkdir(parents=True)
            old.write_text(json.dumps({"version": 1, **reference, "public": public}), encoding="utf-8")
            self.assertEqual(Path(inputs.reference_path(reference)), old)
            self.assertEqual(inputs.read(reference), public)

    def test_shepherd_source_receipt_uses_safe_filename(self):
        from shepherd.source_receipts import SourceReceipts
        with tempfile.TemporaryDirectory() as tmp:
            receipts = SourceReceipts(tmp)
            binding = {"binding_id": "fixture-binding", "conversation_id": "fixture",
                       "engine": "pi", "native_session_id": "fixture-native",
                       "connector_version": "fixture-v1", "ownership_nonce": "fixture-nonce"}
            row = receipts.write(binding, "fixture:1", {"turn_id": "fixture:1"})
            self.assertNotIn(":", Path(receipts.path(binding["binding_id"], "fixture:1")).name)
            self.assertEqual(receipts.read(binding, "fixture:1"), row)
            self.assertEqual(receipts.write(binding, "fixture:1", {"turn_id": "fixture:1"}), row)
            sealed = receipts.seal(binding, "fixture:1", {"turn_id": "fixture:1", "terminal_state": "completed"})
            self.assertEqual(receipts.read(binding, "fixture:1"), sealed)

    def test_shepherd_completed_scratch_is_collected_without_open_lease(self):
        from shepherd.execution_scratch import ExecutionScratch, sweep
        with tempfile.TemporaryDirectory() as tmp:
            with ExecutionScratch(tmp, {"binding_id": "fixture"}, "fixture:1") as scratch:
                path = scratch.path
                self.assertEqual(sweep(tmp), {"removed": 0, "retained": 1})
            self.assertFalse(path.exists())
            self.assertEqual(sweep(tmp), {"removed": 0, "retained": 0})

    def test_os_open_is_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x")
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.write(fd, b"a\nb")
            os.close(fd)
            self.assertEqual(Path(path).read_bytes(), b"a\nb")

    def test_kill_zero_is_liveness_only(self):
        proc = _sleeper(30, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            os.kill(proc.pid, 0)
            time.sleep(0.3)
            self.assertIsNone(proc.poll(), "os.kill(pid, 0) 不应结束或中断目标进程")
        finally:
            proc.kill()
            proc.wait()
        with self.assertRaises(ProcessLookupError):
            os.kill(proc.pid, 0)

    def test_new_session_tree_killpg(self):
        script = ("import subprocess, sys, time; "
                  "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)")
        proc = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
        time.sleep(1.0)
        children = oscompat.descendants(proc.pid)
        self.assertTrue(children, "子进程应已启动")
        os.killpg(proc.pid, oscompat.SIGKILL)
        proc.wait(timeout=10)
        deadline = time.time() + 5
        while time.time() < deadline and any(oscompat.process_alive(pid) for pid in children):
            time.sleep(0.1)
        self.assertFalse(any(oscompat.process_alive(pid) for pid in children))

    def test_getpgid_and_start_token(self):
        self.assertEqual(os.getpgid(os.getpid()), os.getpid())
        token = oscompat.process_start_token(os.getpid())
        self.assertTrue(token.startswith("win-ct:"))
        self.assertEqual(token, oscompat.process_start_token(os.getpid()))

    def test_replace_retries_while_target_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "target")
            Path(target).write_text("old")
            source = os.path.join(tmp, "source")
            Path(source).write_text("new")
            handle = open(target, "rb")
            threading.Timer(0.3, handle.close).start()
            os.replace(source, target)
            self.assertEqual(Path(target).read_text(), "new")


@WINDOWS_ONLY
class Network(unittest.TestCase):
    def test_listening_and_peer_pid(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            self.assertEqual(oscompat.listening_pid(port), os.getpid())
            client = socket.create_connection(("127.0.0.1", port))
            conn, _ = server.accept()
            try:
                peer_port = conn.getpeername()[1]
                self.assertEqual(oscompat.loopback_peer_pid(peer_port, port), os.getpid())
            finally:
                conn.close()
                client.close()
        finally:
            server.close()


@WINDOWS_ONLY
class ConPty(unittest.TestCase):
    def test_echo_roundtrip(self):
        import termpty
        closed = threading.Event()
        sid, pid, created = termpty.pty_open(["cmd.exe", "/c", "echo desk-pty-ok"], dict(os.environ),
                                             os.getcwd(), cols=80, rows=24)
        self.assertTrue(created)
        termpty.pty_set_on_close(sid, lambda _info: closed.set())
        self.assertTrue(closed.wait(15))
        sessions = [s for s in termpty._PTY.values() if s["id"] == sid]
        self.assertEqual(sessions, [])


@WINDOWS_ONLY
class LoopbackChannel(unittest.TestCase):
    def test_peer_pid_only_on_registered_port(self):
        from app_control import capability
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        capability.set_loopback_port(port)
        try:
            client = socket.create_connection(("127.0.0.1", port))
            conn, _ = server.accept()
            try:
                self.assertEqual(capability.peer_pid(conn), os.getpid())
                capability.set_loopback_port(port + 1)
                self.assertEqual(capability.peer_pid(conn), 0)
            finally:
                conn.close()
                client.close()
        finally:
            capability.set_loopback_port(0)
            server.close()


if __name__ == "__main__":
    unittest.main()
