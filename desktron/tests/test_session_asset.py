"""会话附件走 URL,不再内联进正文(2026-08-20)。

起因是切会话慢。实测一段 3.1MB 的会话:49 条消息 322 个 part,其中 5 张图占 2.4MB(77%)。
图以 base64 内联在正文里,于是每打开一次会话,同一张图就要重读一次盘、重解析一次 JSON、
重序列化一次、重传一次、浏览器再重解一次 base64——而它本来就以文件形态躺在 uploads/ 下。

改法是正文里只留 `/session/<id>/asset/<name>`。三条产图的路都要换:用户发的附件、
claude 的 transcript 导入、codex 的 rollout 图片标记。旧记录里的 data_b64 不回填,
前端保留那一支(hsImgSrc),否则改动之前的截图会全部画不出来。
"""
import base64
import json
import os
import re
import sys
import tempfile
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv

CHAT = _srcmap.chat_src()
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n-fake").decode("ascii")


class Bus:
    def __init__(self):
        self.events = []

    def publish(self, e):
        self.events.append(e)


class AssetPath(unittest.TestCase):
    """`/session/<id>/asset/<name>` 里的 name 来自 URL,当不可信输入对待。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, Bus())
        self.d = os.path.join(self.ad.dir, "uploads", "adp_1")
        os.makedirs(self.d)
        self.f = os.path.join(self.d, "shot.png")
        with open(self.f, "wb") as fh:
            fh.write(b"png-bytes")

    def tearDown(self):
        self.tmp.cleanup()

    def test_取得到本会话的文件(self):
        self.assertEqual(self.ad.asset_path("adp_1", "shot.png"), os.path.realpath(self.f))

    def test_穿不出本会话目录(self):
        outside = os.path.join(self.ad.dir, "sessions.json")
        with open(outside, "w") as fh:
            fh.write("{}")
        for probe in ("../sessions.json", "..%2Fsessions.json", "/etc/passwd",
                      "....//sessions.json", "a/../../sessions.json"):
            with self.subTest(name=probe):
                self.assertEqual(self.ad.asset_path("adp_1", probe), "")

    def test_别的会话也取不到(self):
        other = os.path.join(self.ad.dir, "uploads", "adp_2")
        os.makedirs(other)
        with open(os.path.join(other, "his.png"), "wb") as fh:
            fh.write(b"x")
        self.assertEqual(self.ad.asset_path("adp_1", "../adp_2/his.png"), "")

    def test_不存在的返回空(self):
        self.assertEqual(self.ad.asset_path("adp_1", "nope.png"), "")

    def test_会话_id_本身也不能穿(self):
        self.assertEqual(self.ad.asset_path("../..", "shot.png"), "")

    def test_引用形状(self):
        self.assertEqual(self.ad.asset_url("adp_1", self.f), "/session/adp_1/asset/shot.png")

    def test_文件名要转义(self):
        """名字里可以有空格和中文(_save_atts 只滤掉了危险字符,没滤掉这些)。"""
        u = self.ad.asset_url("adp_1", "/x/屏幕 快照.png")
        self.assertNotIn(" ", u)
        self.assertTrue(u.startswith("/session/adp_1/asset/"))


class AssetSave(unittest.TestCase):
    """记录里只有 base64、盘上没有原件的那条路(claude 的 transcript)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, Bus())

    def tearDown(self):
        self.tmp.cleanup()

    def test_落盘后取得回来(self):
        u = self.ad.asset_save("adp_1", PNG, "image/png")
        name = u.rsplit("/", 1)[-1]
        self.assertTrue(self.ad.asset_path("adp_1", name))
        with open(self.ad.asset_path("adp_1", name), "rb") as f:
            self.assertEqual(f.read(), base64.b64decode(PNG))

    def test_按内容哈希命名_对重建幂等(self):
        """对账会把同一份 transcript 重建很多次。按时间戳命名的话,每次都多出一份副本。"""
        a = self.ad.asset_save("adp_1", PNG, "image/png")
        b = self.ad.asset_save("adp_1", PNG, "image/png")
        self.assertEqual(a, b)
        self.assertEqual(len(os.listdir(os.path.join(self.ad.dir, "uploads", "adp_1"))), 1)

    def test_内容不同则文件不同(self):
        other = base64.b64encode(b"another").decode("ascii")
        self.assertNotEqual(self.ad.asset_save("adp_1", PNG),
                            self.ad.asset_save("adp_1", other))

    def test_扩展名跟着媒体类型(self):
        self.assertTrue(self.ad.asset_save("adp_1", PNG, "image/jpeg").endswith(".jpg"))

    def test_空数据不落盘(self):
        self.assertEqual(self.ad.asset_save("adp_1", ""), "")
        self.assertEqual(self.ad.asset_save("adp_1", "!!!不是 base64!!!"), "")


class NoInlineBase64(unittest.TestCase):
    """三条产图的路都不再往正文里塞 base64。"""

    SRC = _srcmap.engine_adapters_src()

    def test_产图处不再写_data_b64(self):
        """写进我们自己存储的那些 image part,一律给引用。

        判据用「有没有 id 字段」把两个方向分开:我们的 part 一定带 id;发给引擎的那一帧
        是 Anthropic 的 image block(`{"type":"image","source":{...}}`),没有 id,而且
        它必须带真的字节——引擎读不了我们的 URL。混在一起改会把发图整条打断。
        """
        writes = [w for w in re.findall(r'\{"id"[^\n]*?"type": "image"[\s\S]{0,260}?\}', self.SRC)]
        writes += [w for w in re.findall(r'"type": "image",[\s\S]{0,260}?"id"[\s\S]{0,120}?\}', self.SRC)]
        self.assertGreaterEqual(len(writes), 2, "产图的路少于两条,正则该更新了")
        for w in writes:
            self.assertNotIn("data_b64", w, "还有一条路在往正文里塞 base64:\n" + w)
            self.assertIn("url", w)

    def test_发给引擎的那一帧仍是真字节(self):
        """引擎读不了 /session/<id>/asset/... —— 这一帧改成引用等于图发不出去。"""
        i = self.SRC.index("def build_user_frame(")
        seg = self.SRC[i:i + 700]
        self.assertIn('"type": "base64"', seg)
        self.assertIn('"data": b64', seg)

    def test_上传仍然按base64收(self):
        """前端 FileReader 读出来就是 base64,收的这一侧不能跟着改。"""
        from engine_adapters.attachment_context import attachment_bytes
        self.assertIn('raw = attachment_bytes(a)', self.SRC)
        self.assertEqual(attachment_bytes({'data_b64':'aGVsbG8='}), b'hello')

    def test_体积上限已撤(self):
        """上限是为「别把正文撑大」设的。正文里不再有内容,这个理由就不成立了;
        留着只会让大图在界面上凭空消失。"""
        self.assertNotIn("1_500_000", self.SRC)
        self.assertNotIn("1_125_000", self.SRC)


class FrontendFallback(unittest.TestCase):
    def test_优先用引用(self):
        self.assertIn("if (a.url) return a.url;", CHAT)

    def test_旧记录仍画得出(self):
        """改动之前落盘的截图里只有 data_b64,不会被回填。少了这一支它们会全部变空白。"""
        self.assertIn("a.data_b64 ? `data:${a.media_type", CHAT)

    def test_重建时带上_url(self):
        self.assertIn('q.type === "image" && (q.url || q.data_b64)', CHAT)

    def test_图按需加载(self):
        self.assertIn('im.loading = "lazy"', CHAT)


class Route(unittest.TestCase):
    SRC = _srcmap.ui_server_src()

    def test_路由在位(self):
        self.assertIn('if len(seg) == 5 and seg[3] == "asset" and method == "GET":', self.SRC)

    def test_取不到回_404_而不是抛(self):
        i = self.SRC.index('seg[3] == "asset"')
        seg = self.SRC[i:i + 1200]
        self.assertIn('"not found"}, 404', seg)

    def test_长缓存(self):
        """文件名带时间戳或内容哈希,同名即同物。没有这条,切回同一段会话还是要重取。"""
        i = self.SRC.index('seg[3] == "asset"')
        self.assertIn("max-age=31536000", self.SRC[i:i + 1200])


if __name__ == "__main__":
    unittest.main()
