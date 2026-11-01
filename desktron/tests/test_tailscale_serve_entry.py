"""Tailscale Serve 专用入口：环回来源不放行、HTTPS Cookie、本机判定与状态解析。"""
import ast
import http.client
import http.server
import json
import socketserver
import threading
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

import remote_auth
import tailscale_serve
from ui_remote_gate import UiRemoteGateMixin
from ui_static import UiStaticMixin

TOKEN = "serve-test-fixture"
# Synthetic private addresses: pure-function inputs only, never contacted.
SYNTHETIC_LAN_PEER = "192.168.0.20"
SYNTHETIC_FOREIGN_UPSTREAM = "http://10.0.0.2:7766"


@pytest.fixture(autouse=True)
def config(monkeypatch, tmp_path):
    import lan_settings
    import tailscale_settings
    monkeypatch.setattr(lan_settings, 'config_path', lambda: tmp_path / 'lan.json')
    monkeypatch.setattr(lan_settings, '_RUNTIME', None)
    monkeypatch.setattr(lan_settings, 'candidates', lambda: ([], ''))
    monkeypatch.setattr(tailscale_settings, 'config_path', lambda: tmp_path / 'tailscale.json')
    monkeypatch.delenv(remote_auth.BIND_ENV, raising=False)
    monkeypatch.setenv(remote_auth.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(remote_auth.SERVE_PORT_ENV, "7766")


def _no_secrets(monkeypatch):
    import credstore
    monkeypatch.setattr(credstore, "_secrets_read", lambda: {})


def test_serve_port_requires_token(monkeypatch):
    _no_secrets(monkeypatch)
    monkeypatch.setenv(remote_auth.TOKEN_ENV, "")
    with pytest.raises(ValueError, match="令牌"):
        remote_auth.serve_port(7765)


@pytest.mark.parametrize("raw", ["abc", "80", "70000", "7765"])
def test_serve_port_rejects_invalid(monkeypatch, raw):
    monkeypatch.setenv(remote_auth.SERVE_PORT_ENV, raw)
    with pytest.raises(ValueError):
        remote_auth.serve_port(7765)


def test_serve_port_unset_is_disabled(monkeypatch):
    _no_secrets(monkeypatch)
    monkeypatch.setenv(remote_auth.SERVE_PORT_ENV, "")
    assert remote_auth.serve_port(7765) is None
    assert remote_auth.serve_port(7765) is None


def test_proxied_loopback_is_not_trusted():
    assert remote_auth.check("127.0.0.1", {}, "/") == (True, False)
    assert remote_auth.check("127.0.0.1", {}, "/", proxied=True) == (False, False)
    bearer = {"Authorization": "Bearer " + TOKEN}
    assert remote_auth.check("127.0.0.1", bearer, "/", proxied=True) == (True, False)
    assert remote_auth.check("127.0.0.1", {}, "/?token=" + TOKEN, proxied=True) == (True, True)
    assert remote_auth.check(SYNTHETIC_LAN_PEER, bearer, "/", proxied=True) == (False, False)


def test_proxied_without_token_fails_closed(monkeypatch):
    _no_secrets(monkeypatch)
    monkeypatch.setenv(remote_auth.TOKEN_ENV, "")
    assert remote_auth.check("127.0.0.1", {}, "/", proxied=True) == (False, False)


def test_local_orchestrator_rejects_serve_proxy():
    from system.api import _local_orchestrator
    headers = {"X-Desktron-Orchestrator": "deploy"}
    local = SimpleNamespace(client_address=("127.0.0.1", 1), headers=headers,
                            server=SimpleNamespace())
    proxied = SimpleNamespace(client_address=("127.0.0.1", 1), headers=headers,
                              server=SimpleNamespace(serve_proxy=True))
    assert _local_orchestrator(local)
    assert not _local_orchestrator(proxied)


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(tailscale_serve, "status",
                        lambda port: {"state": "ready", "address": "https://mac.example.ts.net/"})
    source = Path(__file__).parents[1] / "lib/ui_server.py"
    tree = ast.parse(source.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
    selected = [n for n in cls.body if isinstance(n, ast.FunctionDef)
                and n.name in ("do_GET", "do_HEAD", "do_POST", "do_PUT", "do_DELETE")]
    routes = ast.ClassDef(name="Routes", bases=[], keywords=[], body=selected, decorator_list=[])
    ns = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[routes], type_ignores=[])),
                 str(source), "exec"), ns)

    class H(UiStaticMixin, UiRemoteGateMixin, ns["Routes"], http.server.SimpleHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _is_workbench(self):
            return False

        def _is_contract(self):
            return False

        def _serve_index(self):
            return self._probe()

        def _probe(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def _local_mutation(self, method):
            return self._probe()

        def log_message(self, *args):
            pass

    frontend = Path(__file__).parents[2] / "frontend"
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), partial(H, directory=str(frontend))) as srv:
        srv.serve_proxy = True
        srv.lan_address = ""
        srv.serve_port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            yield srv
        finally:
            srv.shutdown()
            thread.join(timeout=2)


def request(server, path="/", headers=None, method="GET"):
    conn = http.client.HTTPConnection(*server.server_address, timeout=3)
    try:
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "DELETE"])
def test_serve_entry_rejects_loopback_without_token(server, method):
    assert request(server, method=method)[0] == 401


def test_serve_entry_cookie_is_secure(server):
    status, headers, _ = request(server, "/?token=" + TOKEN, {"Host": "mac.example.ts.net"})
    assert status == 303 and headers["Location"] == "/"
    cookie = headers["Set-Cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie
    assert request(server, headers={"Cookie": cookie.split(";", 1)[0]})[0] == 200


def test_serve_entry_never_reveals_credentials(server):
    port = server.server_address[1]
    host = {"Host": f"127.0.0.1:{port}", "Authorization": "Bearer " + TOKEN}
    body = request(server, "/desk-lan-access/status", host)[2]
    assert TOKEN.encode() not in body
    assert json.loads(body) == {"local": False, "enabled": False, "tailscale": {"enabled": True}}
    assert request(server, "/desk-lan-access", host)[0] == 403


def test_serve_target_matches_only_own_host_and_port():
    cfg = {"TCP": {"443": {"HTTPS": True}}, "Web": {
        "other.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:7766"}}},
        "mac.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:7766"}}},
    }}
    assert tailscale_serve.serve_target(cfg, "mac.example.ts.net", 7766) == "https://mac.example.ts.net/"
    assert tailscale_serve.serve_target(cfg, "mac.example.ts.net", 7765) == ""
    alt = {"TCP": {"8443": {"HTTPS": True}}, "Web": {"mac.example.ts.net:8443": {"Handlers": {"/": {"Proxy": "http://localhost:7766"}}}}}
    assert tailscale_serve.serve_target(alt, "mac.example.ts.net", 7766) == "https://mac.example.ts.net:8443/"
    bad = {"TCP": {"443": {"HTTPS": True}}, "Web": {"mac.example.ts.net:443": {"Handlers": {"/": {"Proxy": SYNTHETIC_FOREIGN_UPSTREAM}}}}}
    assert tailscale_serve.serve_target(bad, "mac.example.ts.net", 7766) == ""


@pytest.mark.parametrize("tcp", [None, {}, {"443": {}}, {"443": {"HTTP": True}},
    {"8443": {"HTTPS": True}}, {"443": {"HTTPS": "true"}},
    {"443": {"HTTPS": True, "HTTP": True}},
    {"443": {"HTTPS": True, "TCPForward": "localhost:7766"}}])
def test_serve_target_requires_exact_https_listener(tcp):
    cfg = {"TCP": tcp, "Web": {"mac.example.ts.net:443": {
        "Handlers": {"/": {"Proxy": "http://127.0.0.1:7766"}}}}}
    assert tailscale_serve.serve_target(cfg, "mac.example.ts.net", 7766) == ""


@pytest.mark.parametrize("port", ["", "bad", "0", "65536"])
def test_serve_target_rejects_invalid_listener_port(port):
    cfg = {"TCP": {port: {"HTTPS": True}}, "Web": {"mac.example.ts.net:" + port: {
        "Handlers": {"/": {"Proxy": "http://127.0.0.1:7766"}}}}}
    assert tailscale_serve.serve_target(cfg, "mac.example.ts.net", 7766) == ""


def test_status_reports_each_state(monkeypatch):
    monkeypatch.setattr(tailscale_serve, "_cli", lambda: "")
    assert tailscale_serve.status(7766)["state"] == "missing"
    monkeypatch.setattr(tailscale_serve, "_cli", lambda: "/bin/tailscale")
    replies = {}
    monkeypatch.setattr(tailscale_serve, "_run_json", lambda cli, *args: replies[args])
    replies[("status",)] = {"BackendState": "Stopped", "Self": {"DNSName": "mac.example.ts.net."}}
    assert tailscale_serve.status(7766)["state"] == "offline"
    replies[("status",)] = {"BackendState": "Running", "Self": {"DNSName": "mac.example.ts.net."}}
    replies[("serve", "status")] = {}
    got = tailscale_serve.status(7766)
    assert got["state"] == "unconfigured" and got["command"].endswith("http://127.0.0.1:7766")
    replies[("serve", "status")] = {"TCP": {"443": {"HTTPS": True}},
        "Web": {"mac.example.ts.net:443": {
        "Handlers": {"/": {"Proxy": "http://127.0.0.1:7766"}}}}}
    assert tailscale_serve.status(7766) == {"state": "ready", "address": "https://mac.example.ts.net/"}
