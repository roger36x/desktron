"""开机的往返次数(2026-08-24 用户实报手机首启与刷新都非常慢)。

这条链路的开销与字节数几乎无关:实测经隧道每个请求固定 1.2~1.7 秒,433KB 的 style.css
与 376 字节的 manifest.json 一样慢——贵的是往返次数,不是传了多少。冷启 20 个请求、
顺序取 31.7 秒,就是这么来的。

于是两处改法都冲着「少跑往返」:

  一、静态资源的缓存指纹改成按内容算,而且逐个文件各算各的。原来 19 个文件共用一个
      「全前端最新 mtime」,动一个就把其余 18 个的缓存键一起换掉,客户端 1.7MB 全量重下。
      我那天同步了六七次,每次都让缓存全废。

  二、开机要的状态合成一个端点。前端原来串行取,后来并行成一波,合并之后连那一波也省了。
      取数逻辑不重写:转调各域已有的处理器,把它们本要写回客户端的 JSON 截下来。
"""
import io
import json
import os
import re
import sys
import tempfile
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = _srcmap.ui_server_src()
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
from frontend_bundle import ASSET_BUNDLES, bundle_data  # noqa: E402


def _load_token_fns(frontend):
    """把指纹函数抠出来单独跑——整个 ui_server 一 import 就起服务。"""
    ns = {"os": os, "time": __import__("time"), "hashlib": __import__("hashlib"),
          "FRONTEND": frontend, "bundle_data": bundle_data,
          "_FRONTEND_REV": {"signature": None, "token": ""}}
    for name in ("_frontend_tree_signature", "_frontend_token", "_asset_token"):
        m = re.search(r"\ndef %s\(.*?\n(?=\n\n|\ndef |\nclass |\n#)" % name, SRC, re.S)
        assert m, name + " 不见了"
        exec(compile(m.group(0), "x", "exec"), ns)
    ns.setdefault("_ASSET_FP", {})
    return ns


class PerFileFingerprint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for n, body in (("style.css", "a{}"), ("app-core.js", "//core"),
                        ("app-init.js", "//init")):
            io.open(os.path.join(self.tmp.name, n), "w", encoding="utf-8").write(body)
        os.makedirs(os.path.join(self.tmp.name, "vendor"))
        io.open(os.path.join(self.tmp.name, "vendor", "x.js"), "w", encoding="utf-8").write("//v")
        for parts in ASSET_BUNDLES.values():
            for name in parts:
                path = os.path.join(self.tmp.name, name)
                if not os.path.exists(path):
                    io.open(path, "w", encoding="utf-8").write("//" + name)
        self.ns = _load_token_fns(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _tok(self, rel):
        return self.ns["_asset_token"](rel)

    def _write(self, name, body):
        io.open(os.path.join(self.tmp.name, name), "w", encoding="utf-8").write(body)

    def test_每个文件各有各的指纹(self):
        a, b = self._tok("/app-core.js"), self._tok("/app-init.js")
        self.assertNotEqual(a, b, "两个内容不同的文件指纹一样")

    def test_改一个不动其余(self):
        """这一条就是那天的痛处:动一个文件,客户端把 1.7MB 全量重下。"""
        before = {n: self._tok(n) for n in ("/style.css", "/app-core.js", "/app-init.js")}
        self._write("app-init.js", "//init changed")
        after = {n: self._tok(n) for n in ("/style.css", "/app-core.js", "/app-init.js")}
        self.assertNotEqual(before["/app-init.js"], after["/app-init.js"], "改了却没变")
        self.assertEqual(before["/style.css"], after["/style.css"], "没改的也跟着失效了")
        self.assertEqual(before["/app-core.js"], after["/app-core.js"], "没改的也跟着失效了")

    def test_只碰mtime不算改(self):
        """rsync 会重写一份内容相同的文件。按 mtime 算的话,每次同步都白白作废一次缓存。"""
        before = self._tok("/app-core.js")
        os.utime(os.path.join(self.tmp.name, "app-core.js"), None)
        self.assertEqual(self._tok("/app-core.js"), before)

    def test_改回去指纹也回去(self):
        """按内容算才有这个性质。改错了再改回来,客户端不必重下。"""
        before = self._tok("/app-core.js")
        self._write("app-core.js", "//tmp")
        self.assertNotEqual(self._tok("/app-core.js"), before)
        self._write("app-core.js", "//core")
        self.assertEqual(self._tok("/app-core.js"), before)

    def test_文件不在时退回全局令牌(self):
        """取不到指纹也得给个值,否则页面会引用到一个没有 ?v= 的地址。"""
        self.assertTrue(self._tok("/没有这个文件.js"))

    def test_虚拟合包任一分片变化会换指纹(self):
        before = self._tok("/app-chat.js")
        self._write(ASSET_BUNDLES["/app-chat.js"][-1], "//changed")
        self.assertNotEqual(self._tok("/app-chat.js"), before)

    def test_index_里逐个打指纹(self):
        """三处替换(css / app-*.js / vendor)都要按各自的文件算,不能再共用一个令牌。"""
        m = re.search(r"def _serve_index\(self\):.*?\n    def ", SRC, re.S)
        self.assertIsNotNone(m, "_serve_index 不见了")
        seg = m.group(0)
        self.assertEqual(seg.count("_asset_token("), 3,
                         "有替换没走按文件指纹:%d 处" % seg.count("_asset_token("))
        self.assertNotIn("?v={token}", seg, "还留着共用一个令牌的写法")


class BootEndpoint(unittest.TestCase):
    """开机状态一次给全。"""

    def _boot_src(self):
        """取数那一半。2026-08-25 从 _boot 里分出 _boot_data:首页要内联同一份数据,
        两处不能各取各的,否则迟早只改一边。

        同日又把「转调并截获」那一步分出 _capture_get:合并端点不止 /workbench/boot 一条了
        (还有 /workbench/tasks),那一步两处照抄的话,迟早只改一边。所以这里连它一起取。
        """
        out = []
        for name in ("_capture_get", "_boot_data"):
            m = re.search(r"\n    def %s\(self.*?\n    (?:def |# )" % name, SRC, re.S)
            assert m, name + " 不见了"
            out.append(m.group(0))
        return "\n".join(out)

    def test_开机状态都在清单里(self):
        m = re.search(r"BOOT_PARTS = \((.*?)\)", SRC, re.S)
        self.assertIsNotNone(m, "BOOT_PARTS 不见了")
        parts = re.findall(r'"([^"]+)"', m.group(1))
        for need in ("/workbench.json", "/workbench/domains", "/workbench/hidden",
                     "/workbench/apps", "/workbench/state", "/workbench/backends",
                     "/workbench/engine", "/workbench/runtime"):
            self.assertIn(need, parts, need + " 不在开机清单里")
        self.assertNotIn("/workbench/terminals", parts,
                         "会话冷扫进首页会把整份 index 的首字节拖住")

    def test_转调既有处理器而不是另写一份(self):
        """另写一份取数逻辑,两处迟早只改一边。"""
        seg = self._boot_src()
        self.assertIn("m.owns(sub", seg, "没有按路径去问各域谁认领")
        self.assertIn("owner.handle(cap", seg, "没有转调那一份,像是自己另写了取数")

    def test_一样取不到不拖垮整份(self):
        """其中一样坏掉，其他状态照给；前端按缺项自己回落。"""
        seg = self._boot_src()
        self.assertIn("except Exception", seg)
        self.assertIn("out[sub] = None", seg)

    def test_不归路由的那种当静态文件读(self):
        """workbench.json 是 frontend 下的静态文件,平时由静态处理器直接发。
        只问模块的话它永远是空的(2026-08-25 重启后实测:七项里就它是空的)。"""
        seg = self._boot_src()
        self.assertIn('any(m.owns(sub, "GET") for m in self._WB_API)', seg,
                      "没有先判断这一项归不归路由")
        self.assertIn("os.path.join(FRONTEND, sub.lstrip(\"/\"))", seg,
                      "没有从静态文件里读那一项")

    def test_非200的那一样记成空(self):
        seg = self._boot_src()
        self.assertIn("cap.code in (0, 200)", seg)

    def test_路由上认这条路(self):
        m = re.search(r"def _workbench\(self, method\):.*?\n    def ", SRC, re.S)
        self.assertIn('p == "/workbench/boot"', m.group(0))


class TasksBundleEndpoint(unittest.TestCase):
    """几个工作流的任务一次给全。

    前端原来一个工作流打一个请求,而侧栏折叠计数、任务页、设置页统计各遍历一遍,同一时刻
    就是十来个(2026-08-25 实测 11 个,去重后仍有 5 个)。服务端这头一个只要 1~3 毫秒、
    响应最大 4.8KB,贵的全在路上:经隧道握手中位 384ms,四分之一的新连接还要多付整整一秒。
    """

    def _src(self):
        # 2026-09-03 起取数那圈搬进 _tasks_map,由合并端点与开机内联共用;_tasks_bundle 只剩
        # 解析 ids 再转调。两段紧挨着,一起截下来看,断言的仍是同一批事实。
        m = re.search(r"\n    def _tasks_map\(self, ids\):.*?\n    def _tasks_bundle\(self, q\):"
                      r".*?\n(?=\n    def |\n    [A-Z_]+ = )", SRC, re.S)
        assert m, "_tasks_map / _tasks_bundle 不见了"
        return m.group(0)

    def test_路由上认这条路(self):
        m = re.search(r"def _workbench\(self, method\):.*?\n    def ", SRC, re.S)
        self.assertIn('p == "/workbench/tasks"', m.group(0))

    def test_转调既有处理器而不是另写一份(self):
        """任务怎么读是 task 那一域的事,这里再实现一遍迟早只改一边。"""
        seg = self._src()
        self.assertIn("_capture_get(", seg)
        self.assertIn("/workbench/domains/%s/tasks", seg)

    def test_id要转义(self):
        """工作流 id 出现在路径段里,不转义的话带斜杠的能拼出别的路由。"""
        seg = self._src()
        self.assertIn('urllib.parse.quote(did, safe="")', seg)

    def test_有条数上限(self):
        seg = self._src()
        self.assertIn("TASKS_BUNDLE_MAX", seg, "没有上限,一条 URL 能让它扇出无穷多次")
        m = re.search(r"TASKS_BUNDLE_MAX = (\d+)", SRC)
        self.assertIsNotNone(m)
        self.assertLessEqual(int(m.group(1)), 256)

    def test_空的ids不报错(self):
        """前端可能传空(一个工作流都没有),这条路要给个空对象而不是 500。"""
        seg = self._src()
        self.assertIn('get("ids", [""])[0]', seg, "没有给 ids 缺省值")
        self.assertIn('if x', seg, "没有滤掉空串,空 ids 会扇出一个空 id 的请求")


class InlinedIntoIndex(unittest.TestCase):
    """开机数据内联进首页,热缓存刷新就只剩一个请求。

    index.html 是 no-store、每次加载都要取,顺路捎上等于白拿。经隧道时一个往返一两秒
    (与响应大小几乎无关),而热缓存刷新原本只剩两个请求:index 与 /workbench/boot。
    """

    def _index_src(self):
        m = re.search(r"def _serve_index\(self\):.*?\n    def ", SRC, re.S)
        assert m, "_serve_index 不见了"
        return m.group(0)

    def test_首页里内联了开机数据(self):
        seg = self._index_src()
        self.assertIn("window.__BOOT__=", seg)
        self.assertIn("_boot_data()", seg, "没有复用那份取数,像是另写了一遍")

    def test_取数与发送拆开了(self):
        """内联与单独那条路要取同一份数据,不能各写各的。"""
        self.assertIn("def _boot_data(self):", SRC)
        m = re.search(r"\n    def _boot\(self\):.*?\n\n", SRC, re.S)
        self.assertIn("_boot_data()", m.group(0), "/workbench/boot 没有走同一份取数")

    def test_闭合标签要转义(self):
        """`</script>` 若原样出现在 JSON 里会提前闭合脚本标签,整页从那里断掉。"""
        seg = self._index_src()
        self.assertIn('replace("<"', seg, "没有转义 < ")
        self.assertIn("u003c", seg)

    def test_取不到不能拖垮首页(self):
        """白页比慢一秒严重得多。"""
        seg = self._index_src()
        at = seg.index("window.__BOOT__=")
        head = seg[:at]
        self.assertIn("try:", head[-400:], "内联那段没有包在 try 里")

    def test_内联在第一个脚本之前(self):
        """要赶在 app-core.js 之前,否则 init 跑到时它还不存在。"""
        seg = self._index_src()
        self.assertIn('<script>window.__BOOT__=%s;</script>', seg)
        self.assertIn('<script src="/app-core.js', seg)


class CaptureProxy(unittest.TestCase):
    """截获代理:只拦 _send_json,其余照转给真 handler。"""

    def _cls(self):
        m = re.search(r"\n    class _Capture:.*?\n    def _boot", SRC, re.S)
        assert m, "_Capture 不见了"
        src = "\n".join(l[4:] if l.startswith("    ") else l
                        for l in m.group(0).splitlines())
        src = src[:src.index("\ndef _boot")]
        ns = {}
        exec(compile(src, "x", "exec"), ns)
        return ns["_Capture"]

    def test_截下_json_不写回客户端(self):
        class Real:
            def __init__(self):
                self.written = []
            def _send_json(self, obj, code=200):
                self.written.append(obj)
        real = Real()
        cap = self._cls()(real)
        cap._send_json({"a": 1})
        self.assertEqual(cap.result, {"a": 1})
        self.assertEqual(real.written, [], "还是写回了客户端")

    def test_记下状态码(self):
        cap = self._cls()(object())
        cap._send_json({"e": 1}, 500)
        self.assertEqual(cap.code, 500)

    def test_其余属性照转(self):
        class Real:
            headers = {"X": "1"}
            path = "/p"
        cap = self._cls()(Real())
        self.assertEqual(cap.path, "/p")
        self.assertEqual(cap.headers, {"X": "1"})

    def test_写属性落到真身上(self):
        """处理器有时会往 handler 上挂东西,别让它落在代理上丢掉。"""
        class Real:
            pass
        real = Real()
        cap = self._cls()(real)
        cap.foo = 7
        self.assertEqual(getattr(real, "foo", None), 7)


class JsonCompressed(unittest.TestCase):
    """API 响应也压缩。

    原来只有静态文件与首页压,API 一律原样发——而最大的那一份恰恰是 API:一段会话的
    消息 741KB,gzip 之后 205KB(2026-08-25 实测,省 72%)。经隧道时这一份四秒多,
    是「刷新到会话可用」里最大的一块。JSON 压缩比很高(重复的键名、大量 ASCII)。
    """

    def _fn(self, name):
        m = re.search(r"\n    def %s\(self.*?\n    def " % re.escape(name), SRC, re.S)
        assert m, name + " 不见了"
        return m.group(0)

    def test_按_Accept_Encoding_压(self):
        seg = self._fn("_send_json_bytes")
        self.assertIn('"gzip" in (self.headers.get("Accept-Encoding")', seg,
                      "没看客户端要不要压")
        self.assertIn("gzip.compress(data", seg)

    def test_压了要标编码与_Vary(self):
        """不标 Content-Encoding 客户端会把压缩字节当 JSON 解;不标 Vary,
        中间那层缓存会把压过的发给不支持的客户端。"""
        seg = self._fn("_send_json_bytes")
        self.assertIn('send_header("Content-Encoding", enc)', seg)
        self.assertIn('send_header("Vary", "Accept-Encoding")', seg)

    def test_动态_JSON_禁缓存(self):
        """Safari/WKWebView 会启发式缓存无缓存头的同 URL JSON，刷新后仍可读到旧目录。"""
        seg = self._fn("_send_json_bytes")
        self.assertIn('send_header("Cache-Control", "no-store, must-revalidate")', seg)
        self.assertIn('send_header("Pragma", "no-cache")', seg)

    def test_小响应不压(self):
        """省下的字节还不够一次系统调用,而 CPU 是实打实花的。"""
        self.assertIn("JSON_GZIP_MIN", SRC)
        seg = self._fn("_send_json_bytes")
        self.assertIn("len(data) >= self.JSON_GZIP_MIN", seg)

    def test_压不动也要把响应发出去(self):
        seg = self._fn("_send_json_bytes")
        self.assertIn("except Exception", seg)
        self.assertIn("enc = None", seg)

    def test_长度按压缩后算(self):
        """Content-Length 若还是原文长度,客户端会一直等剩下的字节。"""
        seg = self._fn("_send_json_bytes")
        self.assertLess(seg.index("gzip.compress(data"),
                        seg.index('send_header("Content-Length"'),
                        "先写了长度才压,长度就是错的")

    def test_会话消息那条也走公用发送(self):
        """它自己拼字节直发,是全站最大的一个响应。两处各写一套的话,
        压缩这类事迟早只加在其中一边。"""
        m = re.search(r'seg\[3\] == "message" and method == "GET".*?return', SRC, re.S)
        self.assertIsNotNone(m, "消息那条路不见了")
        self.assertIn("_send_json_bytes(", m.group(0), "没走公用发送,压缩落不到它头上")
        self.assertNotIn("self.wfile.write(data)", m.group(0), "还留着自己发的那套")
