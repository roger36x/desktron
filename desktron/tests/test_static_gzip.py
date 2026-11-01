"""静态资源按需 gzip + 连接复用。

回归的是这条:手机首屏要拉 22 个资源、2.25 MB 原始字节,而 SimpleHTTPRequestHandler
不压缩。这些全是 JS/CSS,gzip 通常能压掉七成——经隧道那一路尤其值得。

另一半是连接复用:默认的 HTTP/1.0 每个资源都要重建一次 TCP,而经隧道那一路建一次
连接实测 241ms,首屏 22 个资源光握手就 5.3 秒。改 1.1 的代价是每个响应都得声明长度,
所以要有一道兜底——没声明的就关连接,用 EOF 作结束标志(那正是 1.0 时代的行为)。

钉六件:该压的压、不声明支持就不压、结果按 mtime 缓存、太大的不进内存、
连接可复用、没有 Content-Length 的响应不会把客户端挂住。
"""
import datetime
import email.utils
import gzip
import http.client
import http.server
import io
import os
import socketserver
import stat
import sys
import threading
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
UI = os.path.join(ROOT, "desktron", "lib", "ui_static.py")   # 2026-08-27 分拆:gzip 一段在 ui_static
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
from frontend_bundle import bundle_data  # noqa: E402


def _handler_cls():
    """把 ui_server 里那段 send_head 抠出来,挂在一个干净的 handler 上跑。

    整份 ui_server import 不进来(它一 import 就起引擎、装信号、连一堆东西),
    而要验的只是这一段静态资源的收发。
    """
    src = open(UI, encoding="utf-8").read()
    i = src.index("    # ---- 静态资源按需 gzip ----")
    body = src[i:src.index("    def handle_one_request(self):", i)]
    ns = {"os": os, "gzip": gzip, "io": io, "stat": stat, "http": http,
          "datetime": datetime, "email": email, "bundle_data": bundle_data,
          "FRONTEND": os.path.join(ROOT, "frontend")}
    exec("class H(http.server.SimpleHTTPRequestHandler):\n" + body
         + "\n    def log_message(self, *a): pass\n", ns)
    return ns["H"]


class Gzip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = os.path.join(ROOT, "frontend")
        H = _handler_cls()
        cls.srv = socketserver.ThreadingTCPServer(
            ("127.0.0.1", 0), lambda *a, **k: H(*a, directory=cls.dir, **k))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _get(self, path, gz=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request("GET", path, headers={"Accept-Encoding": "gzip"} if gz else {})
        r = c.getresponse()
        raw = r.read()
        enc = r.getheader("Content-Encoding")
        ln = r.getheader("Content-Length")
        c.close()
        return raw, enc, ln

    def test_文本资源会压(self):
        raw, enc, ln = self._get("/app-core.js")
        self.assertEqual(enc, "gzip")
        self.assertEqual(int(ln), len(raw), "Content-Length 要按压完的字节算")
        self.assertIn(b"function", gzip.decompress(raw), "解开要是原文")

    def test_省下的量对得上(self):
        gz, _, _ = self._get("/app-core.js")
        plain, enc, _ = self._get("/app-core.js", gz=False)
        self.assertIsNone(enc, "没声明支持 gzip 就不该压")
        self.assertLess(len(gz), len(plain) * 0.7, "至少省三成才值得做这件事")
        self.assertEqual(gzip.decompress(gz), plain, "压的和不压的必须是同一份内容")

    def test_带指纹的地址也认(self):
        """index.html 注入过 ?v=<令牌>,判后缀时要先把查询串切掉。"""
        raw, enc, _ = self._get("/app-core.js?v=123456")
        self.assertEqual(enc, "gzip")

    def test_虚拟合包也压且内容按序(self):
        raw, enc, _ = self._get("/app-chat.js?v=test")
        self.assertEqual(enc, "gzip")
        self.assertEqual(gzip.decompress(raw), bundle_data(self.dir, "/app-chat.js")[1])

    def test_按_mtime_缓存(self):
        H = _handler_cls()
        self.assertIn("st.st_mtime_ns", open(UI, encoding="utf-8").read(),
                      "缓存键要含 mtime,否则改了文件还发旧的")
        self.assertTrue(hasattr(H, "_gz_cache"))

    def test_连接可复用(self):
        """同一条连接连取多个资源,服务端不该每次都关。"""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            kept = 0
            for f in ("/app-core.js", "/app-rail-nav.js", "/index.html"):   # 2026-08-27 分拆后的实文件
                c.request("GET", f, headers={"Accept-Encoding": "gzip"})
                r = c.getresponse()
                r.read()
                if (r.getheader("Connection") or "").lower() != "close":
                    kept += 1
            self.assertEqual(kept, 3, "静态资源该保持连接")
        finally:
            c.close()

    def test_没声明长度的响应不会挂住(self):
        """HTTP/1.1 下客户端靠 Content-Length 判断何时收完。没有的(SSE 流、少数手写响应)
        必须关连接,用 EOF 作结束标志,否则页面就卡在那儿等。"""
        src = open(UI, encoding="utf-8").read()
        i = src.index("    def end_headers(self):")
        seg = src[i:src.index("\n    def ", i + 10)]
        self.assertIn("content-length:", seg)
        self.assertIn("upgrade:", seg, "协议升级(WebSocket)那一路不能被误伤")
        self.assertIn("close_connection = True", seg)
        # protocol_version 留在 ui_server.py 的 Handler 本体(2026-08-27 分拆),从拼接整体找
        self.assertIn('protocol_version = "HTTP/1.1"', _srcmap.ui_server_src())

    def test_没改过就给_304_不重下(self):
        # 实文件走条件请求；/app-core.js 已是虚拟合包（含界面语言模块），靠 ?v= 指纹长缓存。
        """vendor/ 那几份挂的是 no-cache(允许存但每次都要问),这条路必须真的省下
        重下的字节——否则跟 no-store 没区别,只是多问一句(2026-08-24 修)。"""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            c.request("GET", "/app-logdock.js", headers={"Accept-Encoding": "gzip"})
            r = c.getresponse()
            r.read()
            lm = r.getheader("Last-Modified")
            self.assertTrue(lm, "没有 Last-Modified 就没法做条件请求")
            c.request("GET", "/app-logdock.js",
                      headers={"Accept-Encoding": "gzip", "If-Modified-Since": lm})
            r2 = c.getresponse()
            body = r2.read()
            self.assertEqual(r2.status, 304)
            self.assertEqual(body, b"")
            self.assertNotEqual((r2.getheader("Connection") or "").lower(), "close",
                                "304 没有正文,不该被没声明长度那条兜底规则误伤成断连接")
        finally:
            c.close()

    def test_太大的不压(self):
        src = open(UI, encoding="utf-8").read()
        self.assertIn("_GZ_MAX", src, "大文件不该压完塞进内存")
        self.assertIn("len(self._gz_cache) > 256", src, "缓存要有上限")


if __name__ == "__main__":
    unittest.main()
