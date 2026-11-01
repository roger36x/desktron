"""App Control Agent transport 的内核进程身份闭环。"""
import http.server
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
if "deskcfg" not in sys.modules:
    sys.argv = ["test", os.path.join(ROOT, "frontend"), "0", "0", tempfile.mkdtemp()]

from app_control.capability import (issue, peer_pid, register_process, revoke, verify)

CLI = os.path.join(ROOT, "desktron", "bin", "app-control")
MCP = os.path.join(ROOT, "desktron", "bin", "app-control-mcp")


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.server.registered.wait(2)
        allowed = verify(self.server.session_id,
                         self.headers.get("X-Desk-App-Control-Session", ""),
                         peer_pid(self.request))
        payload = json.dumps({"ok": allowed}).encode()
        self.send_response(200 if allowed else 403)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


class _Server(getattr(socketserver, 'ThreadingUnixStreamServer', socketserver.ThreadingTCPServer)):
    daemon_threads = True


@unittest.skipIf(os.name == 'nt', 'Unix socket 内核身份契约；Windows 使用回环 TCP')
class UnixTransport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmp.name, "app-control.sock")
        self.sid = "session-unix-test"
        self.token = issue(self.sid)
        self.server = _Server(self.socket_path, _Handler)
        self.server.session_id = self.sid
        self.server.registered = threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        revoke(self.sid)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def _env(self):
        env = dict(os.environ)
        env.update({
            "DESKTRON_SESSION_ID": self.sid,
            "DESKTRON_APP_CONTROL_SESSION_CAPABILITY": self.token,
            "DESKTRON_APP_CONTROL_SOCKET": self.socket_path,
            # 若实现错误地回退 TCP，这个端口会立即失败。
            "DESKTRON_UI_URL": "http://127.0.0.1:1",
        })
        return env

    def test_cli_uses_unix_socket_and_kernel_peer_pid(self):
        proc = subprocess.Popen(
            [sys.executable, CLI, "--session", self.sid, "tabs"],
            env=self._env(), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        register_process(self.sid, proc)
        self.server.registered.set()
        stdout, stderr = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout), {"ok": True})

    def test_stolen_token_from_sibling_process_is_rejected(self):
        victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        def stop_victim():
            if victim.poll() is None:
                victim.kill()
            victim.wait(timeout=2)
        self.addCleanup(stop_victim)
        register_process(self.sid, victim)
        self.server.registered.set()
        attacker = subprocess.run(
            [sys.executable, CLI, "--session", self.sid, "tabs"],
            env=self._env(), text=True, capture_output=True, timeout=5)
        self.assertNotEqual(attacker.returncode, 0)
        self.assertIn("HTTP 403", attacker.stderr)

    def test_mcp_uses_same_unix_process_boundary(self):
        frame = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "browser_status", "arguments": {}},
        }) + "\n"
        proc = subprocess.Popen(
            [sys.executable, MCP], env=self._env(), text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        register_process(self.sid, proc)
        self.server.registered.set()
        stdout, stderr = proc.communicate(frame, timeout=5)
        self.assertEqual(proc.returncode, 0, stderr)
        row = json.loads(stdout)
        self.assertFalse(row["result"]["isError"])
        self.assertEqual(row["result"]["structuredContent"], {"ok": True})

class UnixServerSource(unittest.TestCase):
    def test_server_uses_dedicated_app_control_only_handler(self):
        with open(os.path.join(LIB, "ui_server.py"), encoding="utf-8") as source:
            text = source.read()
        start = text.index("class AppControlUnixHandler")
        end = text.index("class AppControlUnixServer", start)
        handler = text[start:end]
        self.assertIn("/workbench/app-control", handler)
        self.assertIn('{"error": "unknown"}', handler)
        self.assertNotIn("_peer(", handler)
        self.assertIn(
            "AppControlUnixServer(_APP_CONTROL_SOCKET, AppControlUnixHandler)", text)


if __name__ == "__main__":
    unittest.main()
