"""跨侧管理:在本机看见并操作 server 上的 bot(lib/larkbot/peers.py)。

两侧的 workspace 仍然各自独立——声明与凭证不跨侧,那是对的,凭证该跟着机器走。
变的只是看得见、管得着:人在本机想知道 server 上那个 bot 在不在线、想把它停掉,
不该为此切一次后端。

钉住三条:
  聚合在服务端做(令牌在 backends.json 里,前端拿不到也不该拿到);
  只转 larkbot 那几个端点(通用代理意味着拿到本机的票就能借道打另一台);
  两台互相指着对方时不会无限转下去。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from larkbot import peers  # noqa: E402

# deskcfg 在导入那一刻就从 sys.argv 取 FRONTEND/UI_PORT/REPO_ROOT(模块级单例),
# 拿 unittest 自己的 argv 去 int() 会报 "invalid literal for int()"。同 test_larkbot_wizard.py:17。
_argv = sys.argv
sys.argv = ["ui_server.py", "/tmp/peers-frontend", "0", "0", tempfile.mkdtemp(prefix="peers-")]
try:
    from larkbot import api as lb_api  # noqa: E402
finally:
    sys.argv = _argv


class Others(unittest.TestCase):
    def setUp(self):
        self.f = tempfile.mktemp(suffix=".json")
        self.p = mock.patch.object(peers, "CONF", self.f)
        self.p.start()
        self.addCleanup(self.p.stop)

    def _write(self, arr):
        with open(self.f, "w", encoding="utf-8") as fh:
            json.dump(arr, fh)

    def test_按地址排除自己(self):
        self._write([{"name": "desktop", "url": "http://127.0.0.1:7765"},
                     {"name": "server", "url": "http://10.0.0.9:7765", "token": "t"}])
        got = peers.others("http://127.0.0.1:7765", "desktop")
        self.assertEqual([p["name"] for p in got], ["server"])

    def test_按名字排除自己(self):
        """只按地址会漏:server 自己那条写的是它的外部地址(10.x.x.x),而从它内部看
        自己是 127.0.0.1,字面对不上,结果它把自己当成对端转发一圈(2026-08-25 实测)。"""
        self._write([{"name": "server", "url": "http://10.0.0.9:7765", "token": "t"}])
        self.assertEqual(peers.others("http://127.0.0.1:7765", "server"), [])
        # 不传名字时确实漏——这条钉住上面那个修复的必要性
        self.assertEqual(len(peers.others("http://127.0.0.1:7765")), 1)

    def test_不会把自己当对端去转一圈(self):
        """server 上实测过:peers 里出现 server 自己,等于对自己发了一次请求。"""
        self._write([{"name": "server", "url": "http://10.0.0.9:7765", "token": "t"},
                     {"name": "desktop", "url": "http://192.168.1.5:7765"}])
        got = [p["name"] for p in peers.others("http://127.0.0.1:7765", "server")]
        self.assertNotIn("server", got)
        self.assertEqual(got, ["desktop"])

    def test_没配地址的跳过(self):
        self._write([{"name": "x"}, {"name": "server", "url": "http://10.0.0.9:7765"}])
        self.assertEqual([p["name"] for p in peers.others()], ["server"])

    def test_配置文件不存在时返回空不抛(self):
        self.assertEqual(peers.others(), [])

    def test_配置文件坏了时返回空不抛(self):
        with open(self.f, "w", encoding="utf-8") as fh:
            fh.write("{不是 json")
        self.assertEqual(peers.others(), [])

    def test_按名字找得到(self):
        self._write([{"name": "server", "url": "http://10.0.0.9:7765", "token": "t"}])
        p, err = peers.find("server", "", "desktop")
        self.assertEqual(err, "")
        self.assertEqual(p["token"], "t")

    def test_找不到时说清楚去哪配(self):
        self._write([])
        _p, err = peers.find("server")
        self.assertIn("backends.json", err)


class Call(unittest.TestCase):
    PEER = {"name": "server", "url": "http://10.0.0.9:7765", "token": "tok"}

    def test_只转_larkbot_那几个端点(self):
        """通用代理意味着拿到本机的票就能借道打另一台,而那张票本来只保证
        「你能用这一台」。"""
        for bad in ("/workbench/terminals", "/workbench/shell", "/", "/desk-probe",
                    "/workbench/lark-sync"):
            d, err = peers.call(self.PEER, bad)
            self.assertIsNone(d, bad)
            self.assertIn("不允许", err, bad)

    def test_带票并打上转发标记(self):
        seen = {}

        class _R:
            status = 200

            def read(self):
                return b'{"ok":true}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _open(req, timeout=None):
            seen["url"] = req.full_url
            seen["auth"] = req.get_header("Authorization")
            # 大小写不敏感地找:urllib 的 add_header 会把名字规范成 .capitalize()
            # 形式(X-desk-peer-hop),按原样查会落空。不依赖它的内部规范。
            seen["hop"] = next((v for k, v in req.header_items()
                                if k.lower() == peers.HOP_HEADER.lower()), None)
            return _R()

        with mock.patch("urllib.request.urlopen", _open):
            d, err = peers.call(self.PEER, "/workbench/lark-bot/agents")
        self.assertEqual(err, "")
        self.assertEqual(d, {"ok": True})
        self.assertEqual(seen["url"], "http://10.0.0.9:7765/workbench/lark-bot/agents")
        self.assertEqual(seen["auth"], "Bearer tok")
        self.assertTrue(seen["hop"], "转发标记没带上,两台互指时会无限转")

    def test_连不上时给一句人话不抛异常(self):
        """那一侧关机、不在内网都很正常,页面该显示「连不上」而不是塌掉。"""
        with mock.patch("urllib.request.urlopen", side_effect=OSError("timed out")):
            d, err = peers.call(self.PEER, "/workbench/lark-bot/agents")
        self.assertIsNone(d)
        self.assertIn("连不上", err)

    def test_票不对时说的是票不对(self):
        """与「连不上」分开:一个去重新部署,一个去看网络。"""
        import urllib.error
        e = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=e):
            _d, err = peers.call(self.PEER, "/workbench/lark-bot/agents")
        self.assertIn("票不对", err)


class Hop(unittest.TestCase):
    def test_认得出转发进来的请求(self):
        class H(dict):
            def get(self, k, d=None):
                for kk, vv in self.items():
                    if kk.lower() == k.lower():
                        return vv
                return d

        self.assertTrue(peers.is_hop(H({peers.HOP_HEADER: "1"})))
        self.assertFalse(peers.is_hop(H({})))

    def test_头名不为空(self):
        """空串会让 is_hop 恒为假,两台互指时无限转下去。"""
        self.assertTrue(peers.HOP_HEADER.strip())


class ApiWiring(unittest.TestCase):
    """接口那一侧的接线:聚合与转发都要挂对。"""

    def _src(self):
        p = os.path.join(os.path.dirname(__file__), "..", "lib", "larkbot", "api.py")
        with open(p, encoding="utf-8") as f:
            return f.read()

    def test_转发前先看有没有转发标记(self):
        """否则 A 转给 B、B 又转回 A。"""
        s = self._src()
        self.assertIn("not peers.is_hop(h.headers)", s)

    def test_聚合时也不再往外转(self):
        s = self._src()
        blk = s[s.index('if rest == "agents" and method == "GET"'):s.index('if rest == "agents" and method == "POST"')]
        self.assertIn("peers.is_hop", blk)

    def test_每条都标出自己是哪一侧的(self):
        """不标的话,前端不知道该把「停止」发回哪一台。"""
        s = self._src()
        self.assertIn('backend=me', s)
        self.assertIn('backend=pr["name"]', s)

    def test_那一侧读不到时如实报不静默(self):
        """少显示几个 bot 与「那边连不上」是两回事,后者要人去处理。"""
        s = self._src()
        self.assertIn('"ok": False, "error"', s)

    def test_held_有只回条数的模式(self):
        """跨侧看拦截清单时用它。见下面 ForwardQuery 里的理由。"""
        s = self._src()
        blk = s[s.index('if rest == "held"'):]
        self.assertIn('_query(q, "count")', blk)
        self.assertIn('"count": len(', blk)


class ForwardQuery(unittest.TestCase):
    """转发时查询参数跟着走,只摘掉 backend。"""

    def test_摘掉_backend_留下其余(self):
        self.assertEqual(lb_api._q_forward("backend=server&count=1"), "count=1")
        self.assertEqual(lb_api._q_forward("count=1&backend=server"), "count=1")

    def test_只有_backend_时不带查询串(self):
        self.assertEqual(lb_api._q_forward("backend=server"), "")
        self.assertEqual(lb_api._q_forward(""), "")

    def test_转发时确实把查询串接上(self):
        """丢掉参数不会报错,只会让对面按默认行为答——/held?count=1 会退化成
        「把被拦下的原文整份发过来」,而那正是红线要挡的那类文本。"""
        p = os.path.join(os.path.dirname(__file__), "..", "lib", "larkbot", "api.py")
        with open(p, encoding="utf-8") as f:
            s = f.read()
        self.assertIn('fwd = _q_forward(q)', s)
        self.assertIn('p + ("?" + fwd if fwd else "")', s)

    def test_backend_不跟着转过去(self):
        """留着它对面会看见一个不是自己的名字,又往外转一次。"""
        self.assertNotIn("backend", lb_api._q_forward("backend=server&count=1"))

    def test_对面是旧版本时把原文就地丢掉(self):
        """实测过:server 那一侧还没更新时不认 count,把被拦下的原文整份发了回来
        (2026-08-25)。丢掉只留条数——原文该留在它自己那台上。"""
        p = os.path.join(os.path.dirname(__file__), "..", "lib", "larkbot", "api.py")
        with open(p, encoding="utf-8") as f:
            s = f.read()
        blk = s[s.index("if err2:"):s.index("# ---- sidecar 的入口 ----")]
        self.assertIn('rest == "held"', blk)
        self.assertIn('"items" in d', blk)
        self.assertIn('"count": len(d.get("items")', blk)


if __name__ == "__main__":
    unittest.main()
