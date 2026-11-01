"""远端共享令牌鉴权。

钉住的核心是这条:未配令牌 = 只绑环回 + 不设防,本机形态一字不变;配了令牌才对外开放。
反过来的组合——已开放但没鉴权——不允许存在,那正是把本机假设带到公网上的那种事故。
"""
import os
import sys
import unittest
import pytest
import _srcmap
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import credstore  # noqa: E402
import remote_auth  # noqa: E402

TOK = "t0ken-abc"
# Synthetic RFC1918 peer; these pure-function tests never connect to it.
SYNTHETIC_PEER = "192.168.0.20"


@pytest.fixture(autouse=True)
def isolated_bind(monkeypatch, tmp_path):
    import lan_settings
    import tailscale_settings
    monkeypatch.setattr(lan_settings, '_RUNTIME', None)
    monkeypatch.setattr(lan_settings, 'config_path', lambda: tmp_path / 'lan.json')
    monkeypatch.setattr(tailscale_settings, 'config_path', lambda: tmp_path / 'tailscale.json')
    # 用户的实际局域网配置不能改变本文件默认监听形态的测试前提。
    monkeypatch.delenv(remote_auth.BIND_ENV, raising=False)
    # 本机 ~/.config/desktron/env 里的 DESKTRON_SERVE_PORT 等旧配置同样不能参与。
    monkeypatch.setattr(credstore, 'SECRETS_PATH', str(tmp_path / 'env'))
    monkeypatch.delenv(remote_auth.SERVE_PORT_ENV, raising=False)


@pytest.mark.parametrize('setting', ['all', '0.0.0.0', 'any', ' ALL '])
def test_explicit_all_interfaces_require_token(monkeypatch, setting):
    monkeypatch.setenv(remote_auth.BIND_ENV, setting)
    monkeypatch.setenv(remote_auth.TOKEN_ENV, '')
    with mock.patch.object(credstore, '_secrets_read', return_value={}):
        with pytest.raises(ValueError, match='全网卡监听必须配置访问令牌'):
            remote_auth.bind_host()


@pytest.mark.parametrize('setting', ['all', '0.0.0.0', 'any'])
@pytest.mark.parametrize('token_source', ['env', 'secrets'])
def test_explicit_all_interfaces_with_token_still_authenticate(monkeypatch, setting, token_source):
    monkeypatch.setenv(remote_auth.BIND_ENV, setting)
    monkeypatch.setenv(remote_auth.TOKEN_ENV, TOK if token_source == 'env' else '')
    secrets = {remote_auth.TOKEN_ENV: TOK} if token_source == 'secrets' else {}
    with mock.patch.object(credstore, '_secrets_read', return_value=secrets):
        assert remote_auth.bind_host() == '0.0.0.0'
        assert remote_auth.check('10.0.0.9', {}, '/workbench/terminals') == (False, False)
        assert remote_auth.check('10.0.0.9', {'Authorization': 'Bearer ' + TOK}, '/') == (True, False)


@pytest.mark.parametrize('setting', ['loopback', '127.0.0.1', 'local'])
def test_loopback_override_does_not_require_token(monkeypatch, setting):
    monkeypatch.setenv(remote_auth.BIND_ENV, setting)
    with mock.patch.object(remote_auth, 'token', return_value=''):
        assert remote_auth.bind_host() == '127.0.0.1'


class Headers(dict):
    def get(self, k, d=None):        # http.client.HTTPMessage 的取值是大小写不敏感的
        for kk, vv in self.items():
            if kk.lower() == str(k).lower():
                return vv
        return d


class NoToken(unittest.TestCase):
    def setUp(self):
        os.environ[remote_auth.TOKEN_ENV] = ""
        # 环境变量空只是没配的一半:token() 还会回落读 SECRETS。跑这套用例的机器上很可能
        # 已经配过真令牌(反向暴露那条线要用),那时「未配」的前提不成立,下面两条会假失败。
        # 所以把 SECRETS 那一路也断掉,让这个类测的确实是「两处都没有」。
        self._secrets = mock.patch.object(credstore, "_secrets_read", return_value={})
        self._secrets.start()
        self.addCleanup(self._secrets.stop)

    def test_只绑环回(self):
        self.assertEqual(remote_auth.bind_host(), "127.0.0.1")

    def test_不设防时恒放行(self):
        ok, _ = remote_auth.check("10.0.0.9", Headers(), "/workbench/terminals")
        self.assertTrue(ok)


class WithToken(unittest.TestCase):
    def setUp(self):
        os.environ[remote_auth.TOKEN_ENV] = TOK

    def tearDown(self):
        os.environ[remote_auth.TOKEN_ENV] = ""

    def test_配了令牌才对外绑(self):
        self.assertEqual(remote_auth.bind_host(), "0.0.0.0")

    def test_环回不受拦(self):
        for host in ("127.0.0.1", "::1"):
            ok, _ = remote_auth.check(host, Headers(), "/")
            self.assertTrue(ok, host)

    def test_远端无票被拦(self):
        ok, _ = remote_auth.check(SYNTHETIC_PEER, Headers(), "/")
        self.assertFalse(ok)

    def test_探活不验票(self):
        """客户端要在拿到令牌之前分清「那边没装」与「装了但令牌不对」:前者去装,后者去配票。
        探活若也验票,两种都回 401,这个区分就没了。"""
        for path in (remote_auth.PROBE_PATH, remote_auth.PROBE_PATH + "?x=1"):
            ok, cookie = remote_auth.check(SYNTHETIC_PEER, Headers(), path)
            self.assertTrue(ok, path)
            self.assertFalse(cookie, "探活不该种 Cookie")

    def test_探活豁免不外溢到别的路径(self):
        for path in ("/desk-probe-x", "/a/desk-probe", "/?p=/desk-probe"):
            ok, _ = remote_auth.check(SYNTHETIC_PEER, Headers(), path)
            self.assertFalse(ok, path)

    def test_bearer_放行(self):
        ok, cookie = remote_auth.check("10.0.0.9", Headers({"Authorization": f"Bearer {TOK}"}), "/")
        self.assertTrue(ok)
        self.assertFalse(cookie)

    def test_错票被拦(self):
        for h in (Headers({"Authorization": "Bearer wrong"}),
                  Headers({"Cookie": f"{remote_auth.COOKIE}=wrong"})):
            ok, _ = remote_auth.check("10.0.0.9", h, "/")
            self.assertFalse(ok)

    def test_cookie_放行(self):
        ok, _ = remote_auth.check("10.0.0.9", Headers({"Cookie": f"a=1; {remote_auth.COOKIE}={TOK}"}), "/")
        self.assertTrue(ok)

    def test_首次带查询参数放行并要求种_cookie(self):
        ok, cookie = remote_auth.check("10.0.0.9", Headers(), "/?token=" + TOK)
        self.assertTrue(ok)
        self.assertTrue(cookie)   # 之后靠 Cookie,令牌不再留在地址栏

    def test_cookie_是_httponly(self):
        v = remote_auth.cookie_header()
        self.assertIn("HttpOnly", v)
        self.assertNotIn("Secure", v)                      # 当前是 http,标 Secure 会让 Cookie 根本种不上
        self.assertIn("Secure", remote_auth.cookie_header(secure=True))


class DeskID(unittest.TestCase):
    """desk_id():跨重启稳定、server 与 desktop 都靠懒生成覆盖,不依赖安装脚本。"""

    def setUp(self):
        os.environ[remote_auth.DESK_ID_ENV] = ""

    def tearDown(self):
        os.environ[remote_auth.DESK_ID_ENV] = ""

    def test_环境变量设了就直接用_不碰_secrets(self):
        os.environ[remote_auth.DESK_ID_ENV] = "desk_from_env"
        with mock.patch.object(credstore, "_secrets_read") as m:
            self.assertEqual(remote_auth.desk_id(), "desk_from_env")
            m.assert_not_called()

    def test_secrets_里已有就直接返回_不重新生成(self):
        with mock.patch.object(credstore, "_secrets_read", return_value={remote_auth.DESK_ID_ENV: "desk_existing"}), \
             mock.patch.object(credstore, "_secrets_write") as w:
            self.assertEqual(remote_auth.desk_id(), "desk_existing")
            w.assert_not_called()

    def test_两边都没有时生成并写回(self):
        with mock.patch.object(credstore, "_secrets_read", return_value={}), \
             mock.patch.object(credstore, "_secrets_write") as w:
            v = remote_auth.desk_id()
            self.assertTrue(v.startswith("desk_"), v)
            w.assert_called_once_with({remote_auth.DESK_ID_ENV: v})

    def test_两次生成值不同_不是写死的常量(self):
        with mock.patch.object(credstore, "_secrets_read", return_value={}), \
             mock.patch.object(credstore, "_secrets_write"):
            self.assertNotEqual(remote_auth.desk_id(), remote_auth.desk_id())

    def test_读写异常时返回空串_不抛出(self):
        with mock.patch.object(credstore, "_secrets_read", side_effect=OSError("boom")):
            self.assertEqual(remote_auth.desk_id(), "")


class Wiring(unittest.TestCase):
    def _src(self, rel):
        base = os.path.basename(rel)
        if base in ("app-chat.js", "app-panel.js", "app-rail.js", "app-assets.js", "app-settings.js"):
            return _srcmap.frontend_src(base)   # 2026-08-27 分拆,拼接整体等价
        with open(os.path.join(os.path.dirname(__file__), "..", "..", rel), encoding="utf-8") as f:
            return f.read()

    def test_三个入口都过闸(self):
        s = self._src("desktron/lib/ui_server.py")
        for m in ("def do_GET", "def do_POST", "def do_DELETE"):
            body = s[s.index(m):s.index(m) + 260]
            self.assertIn("self._gate()", body, m)

    def test_绑定地址由令牌决定(self):
        s = self._src("desktron/lib/ui_server.py")
        self.assertIn("_BIND = remote_auth.bind_host()", s)
        self.assertIn("Server((_BIND, UI_PORT), Handler)", s)

    def test_前端_websocket_协议自适应(self):
        for f in ("frontend/app-assets.js", "frontend/term-embed.js"):
            s = self._src(f)
            self.assertIn('location.protocol === "https:" ? "wss" : "ws"', s, f)
            self.assertNotIn("`ws://${location.host}/term", s, f)


if __name__ == "__main__":
    unittest.main()
