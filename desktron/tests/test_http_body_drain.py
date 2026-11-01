"""长连接上未读完的请求正文不能污染下一条请求(会话列表间歇 501 的根因)。"""
import ast
import http.client
import http.server
import socket
import socketserver
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "desktron/lib/ui_server.py").read_text(encoding="utf-8")


def _pieces():
    """只取正文收尾相关的定义；直接 import ui_server 依赖启动参数与进程环境。"""
    tree = ast.parse(SOURCE)
    ns = {"time": time}
    handler_methods = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in (
                    "BODY_DRAIN_MAX", "BODY_DRAIN_TIMEOUT", "BODY_READ_TIMEOUT")
                for t in node.targets):
            exec(compile(ast.Module([node], []), "ui_server", "exec"), ns)
        if isinstance(node, ast.ClassDef) and node.name == "_RequestBody":
            exec(compile(ast.Module([node], []), "ui_server", "exec"), ns)
        if isinstance(node, ast.ClassDef) and node.name == "Handler":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name in (
                        "parse_request", "_finish_request_body"):
                    handler_methods[item.name] = item
    # 原样放进 BaseHTTPRequestHandler 子类，零参 super() 才指向正确的父类。
    cls = ast.ClassDef(name="Base", bases=[ast.Name("BaseHTTPRequestHandler", ast.Load())],
                       keywords=[], body=list(handler_methods.values()), decorator_list=[])
    ns["BaseHTTPRequestHandler"] = http.server.BaseHTTPRequestHandler
    exec(compile(ast.fix_missing_locations(ast.Module([cls], [])), "ui_server", "exec"), ns)
    return ns


NS = _pieces()
NS["BODY_READ_TIMEOUT"] = 0.25


class _Handler(NS["Base"]):
    protocol_version = "HTTP/1.1"

    def handle_one_request(self):
        self._raw_rfile = None
        try:
            super().handle_one_request()
        finally:
            self._finish_request_body()

    def _reply(self, code):
        raw = b"{}"
        self.send_response(code)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._reply(200)

    def do_POST(self):
        if self.path == "/read":
            self.rfile.read(int(self.headers["Content-Length"]))
        self._reply(200 if self.path == "/read" else 404)   # 其余路径不读正文就返回

    def log_message(self, *a):
        pass


class BodyDrain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _pair(self, path, body):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            conn.request("POST", path, body=body, headers={"content-type": "application/json"})
            first = conn.getresponse(); first.read()
            conn.request("GET", "/workbench/terminals?integrity=1")
            second = conn.getresponse(); second.read()
            return first.status, second.status
        finally:
            conn.close()

    def test_unread_body_does_not_break_next_request(self):
        self.assertEqual(self._pair("/unknown", b'{"x":1}'), (404, 200))

    def test_handler_that_reads_body_still_works(self):
        self.assertEqual(self._pair("/read", b'{"x":1}'), (200, 200))

    def test_handler_read_of_partial_body_is_bounded(self):
        sock = socket.create_connection(self.server.server_address, timeout=2)
        sock.settimeout(2)
        began = time.monotonic()
        try:
            sock.sendall(b"POST /read HTTP/1.1\r\nHost: localhost\r\n"
                         b"Content-Length: 10\r\nConnection: keep-alive\r\n\r\nx")
            while sock.recv(4096):
                pass
            self.assertLess(time.monotonic() - began, 1.5)
        finally:
            sock.close()

    def test_partial_small_body_closes_within_drain_timeout(self):
        sock = socket.create_connection(self.server.server_address, timeout=2)
        sock.settimeout(2)
        began = time.monotonic()
        try:
            sock.sendall(b"POST /unknown HTTP/1.1\r\nHost: localhost\r\n"
                         b"Content-Length: 10\r\nConnection: keep-alive\r\n\r\nx")
            received = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                received += chunk
            self.assertIn(b" 404 ", received)
            self.assertLess(time.monotonic() - began, 1.5)
        finally:
            sock.close()

    def test_oversized_unread_body_closes_connection_instead_of_draining(self):
        # 超限时不代读：连接被关闭，同一连接上的下一条请求可以失败，但不能被错配成 501。
        body = b"x" * (NS["BODY_DRAIN_MAX"] + 1)
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        statuses = []
        try:
            for path in ("/unknown", "/after"):
                conn.request("POST" if path == "/unknown" else "GET", path,
                             body=body if path == "/unknown" else None)
                response = conn.getresponse(); response.read()
                statuses.append(response.status)
        except (ConnectionError, http.client.HTTPException, OSError):
            pass
        finally:
            conn.close()
        self.assertNotIn(501, statuses)

    def test_real_handler_finishes_body_after_each_request(self):
        segment = SOURCE[SOURCE.index("    def handle_one_request(self):"):]
        segment = segment[:segment.index("\n    def ", 10)]
        self.assertIn("self._finish_request_body()", segment)
        self.assertLess(segment.index("super().handle_one_request()"),
                        segment.index("self._finish_request_body()"))


if __name__ == "__main__":
    unittest.main()
