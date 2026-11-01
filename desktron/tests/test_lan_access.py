"""真实 HTTP 回归：局域网来源、令牌换 Cookie 和本机连接页的边界。"""
import ast
import json
import http.client
import http.server
from pathlib import Path
import socketserver
import threading
from functools import partial
from html.parser import HTMLParser
import struct

import pytest

import remote_auth
from ui_remote_gate import UiRemoteGateMixin
from ui_static import UiStaticMixin


# Synthetic RFC1918 topology. Servers bind loopback; these are mocked peer identities.
SYNTHETIC_LAN_HOST = "192.168.0.10"
SYNTHETIC_LAN_PEER = "192.168.0.20"


@pytest.fixture(autouse=True)
def config(monkeypatch, tmp_path):
    import lan_settings
    import tailscale_settings
    monkeypatch.setattr(lan_settings, 'config_path', lambda: tmp_path / 'lan.json')
    monkeypatch.setattr(lan_settings, '_RUNTIME', None)
    monkeypatch.setattr(lan_settings, 'candidates', lambda: ([], ''))
    monkeypatch.setattr(tailscale_settings, 'config_path', lambda: tmp_path / 'tailscale.json')
    monkeypatch.setenv(remote_auth.BIND_ENV, "lan:" + SYNTHETIC_LAN_HOST + "/24")
    monkeypatch.setenv(remote_auth.TOKEN_ENV, "lan-test-fixture")
    # 本机凭证文件里的旧 Serve 端口不能改变状态接口的测试前提。
    import credstore
    monkeypatch.setattr(credstore, 'SECRETS_PATH', str(tmp_path / 'env'))
    monkeypatch.delenv(remote_auth.SERVE_PORT_ENV, raising=False)


@pytest.mark.parametrize("setting", ["lan:8.8.8.8/24", "lan:" + SYNTHETIC_LAN_HOST + "/0",
                                         "lan:169.254.1.2/24", "lan:invalid"])
def test_reject_non_private_or_invalid_scope(monkeypatch, setting):
    monkeypatch.setenv(remote_auth.BIND_ENV, setting)
    with pytest.raises(ValueError):
        remote_auth.bind_host()


def test_no_token_cannot_open_lan(monkeypatch):
    import credstore
    monkeypatch.setenv(remote_auth.TOKEN_ENV, "")
    monkeypatch.setattr(credstore, "_secrets_read", lambda: {})
    with pytest.raises(ValueError, match="令牌"):
        remote_auth.bind_host()


def test_lan_keeps_native_loopback():
    assert remote_auth.bind_host() == "127.0.0.1"
    assert str(remote_auth.lan_interface().ip) == SYNTHETIC_LAN_HOST


@pytest.mark.parametrize("peer", ["192.168.1.20", "10.1.2.3", "8.8.8.8", "::ffff:" + SYNTHETIC_LAN_PEER])
def test_other_network_rejected_even_with_token_and_probe(peer):
    headers = {"Authorization": "Bearer lan-test-fixture", "X-Forwarded-For": SYNTHETIC_LAN_PEER}
    for path in ("/", "/desk-probe"):
        assert remote_auth.check(peer, headers, path) == (False, False)


@pytest.fixture
def server():
    # Compile the production route methods without importing ui_server, whose
    # module body would start engines/background work against the test process.
    source = Path(__file__).parents[1] / "lib/ui_server.py"
    tree = ast.parse(source.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
    selected = [n for n in cls.body if isinstance(n, ast.FunctionDef)
                and n.name in ("do_GET", "do_HEAD", "do_POST", "do_PUT", "do_DELETE")]
    routes = ast.ClassDef(name="Routes", bases=[], keywords=[], body=selected, decorator_list=[])
    methods = ast.fix_missing_locations(ast.Module(body=[routes], type_ignores=[]))
    ns = {}
    exec(compile(methods, str(source), "exec"), ns)

    class H(UiStaticMixin, UiRemoteGateMixin, ns["Routes"], http.server.SimpleHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.client_address = (self.server.test_peer, self.client_address[1])

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
        srv.test_peer = SYNTHETIC_LAN_PEER
        srv.lan_address = SYNTHETIC_LAN_HOST
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
def test_no_auth_is_rejected(server, method):
    assert request(server, method=method)[0] == 401


@pytest.mark.parametrize("size", [180, 192, 512])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_home_icon_is_readable_without_browser_cookie(server, size, method):
    status, headers, body = request(server, f"/icon-{size}.png?v=home", method=method)
    assert status == 200
    assert {k.lower(): v for k, v in headers.items()}["content-type"] == "image/png"
    assert "Set-Cookie" not in headers
    if method == "GET":
        assert body.startswith(b"\x89PNG\r\n\x1a\n")
        assert struct.unpack(">II", body[16:24]) == (size, size)
    else:
        assert body == b""


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_home_icon_exception_does_not_allow_writes(server, method):
    assert request(server, "/icon-180.png", method=method)[0] == 401


@pytest.mark.parametrize("path", ["/manifest.json", "/index.html", "/app-core.js",
                                  "/brand-logo.png", "/workbench/domains",
                                  "/icon-180.png/other", "/icons/icon-180.png",
                                  "/%69con-180.png", "/x/../icon-180.png"])
def test_home_icon_exception_does_not_extend_to_other_paths(server, path):
    assert request(server, path)[0] == 401


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_home_icons_still_require_allowed_source_network(server, method):
    server.test_peer = "10.1.2.3"
    assert request(server, "/icon-180.png", method=method)[0] == 401


def test_home_screen_metadata_sends_manifest_credentials():
    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = {}

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == "link":
                self.links[values.get("rel")] = values

    frontend = Path(__file__).parents[2] / "frontend"
    links = Links()
    links.feed((frontend / "index.html").read_text(encoding='utf-8'))
    assert links.links["manifest"]["crossorigin"] == "use-credentials"
    assert links.links["apple-touch-icon"]["href"] == "/icon-180.png"
    assert links.links["apple-touch-icon"]["sizes"] == "180x180"
    manifest = json.loads((frontend / "manifest.json").read_text(encoding='utf-8'))
    assert {icon["src"] for icon in manifest["icons"]} == {"/icon-192.png", "/icon-512.png"}


def test_cookie_exchange_redirect_removes_token(server):
    status, headers, body = request(server, "/?token=lan-test-fixture&view=phone")
    assert status == 303 and body == b""
    assert headers["Location"] == "/?view=phone"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "no-store" in headers["Cache-Control"]
    assert "HttpOnly" in headers["Set-Cookie"]
    assert request(server, headers={"Cookie": headers["Set-Cookie"].split(";", 1)[0]})[0] == 200
    assert request(server, headers={"Authorization": "Bearer wrong"})[0] == 401


def test_token_exchange_cannot_redirect_to_backslash_host(server):
    status, headers, _ = request(server, "/\\external.example?token=lan-test-fixture")
    assert status == 303 and headers["Location"] == "/"


def test_probe_enforces_source_network(server):
    assert request(server, "/desk-probe")[0] == 200
    server.test_peer = "10.1.2.3"
    assert request(server, "/desk-probe")[0] == 401


def test_pairing_page_is_local_only_and_does_not_cache(server):
    headers = {"Authorization": "Bearer lan-test-fixture"}
    assert request(server, "/desk-lan-access", headers)[0] == 403
    server.test_peer = "127.0.0.1"
    status, response_headers, body = request(server, "/desk-lan-access")
    assert status == 200
    assert SYNTHETIC_LAN_HOST.encode() in body and b"lan-test-fixture" in body
    assert "no-store" in response_headers["Cache-Control"]
    assert response_headers["X-Frame-Options"] == "DENY"
    assert request(server, "/desk-lan-access", {"Host": "untrusted.example"})[0] == 403
    server.lan_address = ""
    assert b"lan-test-fixture" not in request(server, "/desk-lan-access")[2]


def test_status_shares_credentials_only_with_local_origin(server):
    path = "/desk-lan-access/status"
    assert request(server, path)[0] == 401
    status, headers, body = request(server, path, {"Authorization": "Bearer lan-test-fixture"})
    assert status == 200 and json.loads(body) == {"local": False, "enabled": True}
    assert b"lan-test-fixture" not in body
    assert "no-store" in headers["Cache-Control"]
    assert "Access-Control-Allow-Origin" not in headers
    server.test_peer = "127.0.0.1"
    state = json.loads(request(server, path)[2])
    assert state["local"] and state["enabled"]
    assert state["url"] == state["address"] + "?token=lan-test-fixture"
    for extra in ({"Host": "untrusted.example"}, {"Origin": "https://untrusted.example"}):
        body = request(server, path, extra)[2]
        assert b"lan-test-fixture" not in body and not json.loads(body)["local"]
    server.lan_address = ""
    state = json.loads(request(server, path)[2])
    assert state.pop('tailscale_config')['enabled'] is False
    assert not state.pop('lan_config')['managed']  # Legacy bind remains authoritative.
    assert state == {"local": True, "enabled": False}


@pytest.mark.parametrize('name', ['tailscale.json', 'lan.json'])
def test_status_reports_damaged_settings_without_hiding_active_entry(server, tmp_path, name):
    # 损坏的期望配置只影响对应设置卡片；已生效的局域网链接仍需返回。
    (tmp_path / name).write_text('{broken', encoding='utf-8')
    server.test_peer = "127.0.0.1"
    status, _, body = request(server, "/desk-lan-access/status")
    state = json.loads(body)
    assert status == 200 and state["url"] == state["address"] + "?token=lan-test-fixture"
    key = 'tailscale_config' if name == 'tailscale.json' else 'lan_config'
    other = 'lan_config' if key == 'tailscale_config' else 'tailscale_config'
    assert key not in state and '配置损坏' in state[key + '_error']
    assert other in state and other + '_error' not in state
    assert (tmp_path / name).read_text(encoding='utf-8') == '{broken'
