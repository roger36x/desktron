"""工具输出在列表里截断,展开时才取全文(2026-08-24)。

用户说 codex 切会话慢。接口本身很快(10~24ms),差别在返回体积:codex 一段会话 1.2~3.2MB,
claude 只有 0.5~0.8MB。拆开看是工具输出——606 条、平均 4.3KB、合计 2.0MB,占整个返回体的
99%;其中超过四分之一顶满 8000 的存储上限(p75 就是 8000)。而界面上这些输出默认是折叠的,
整份送过去等于让浏览器为看不见的内容建 2MB 的对象。

截到 2000 字符省 62%(实测整段会话 3.11MB → 1.55MB)。不截更狠是因为多数输出本来就不长
(p50 只有 1925),截太短反而让常见情形多跑一趟请求。
"""
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
    import engine_adapters as ea
finally:
    sys.argv = _argv

CHAT = _srcmap.chat_src()
CAP = ea.CliAdapter.LIST_OUTPUT_CAP


class Bus:
    def publish(self, e):
        pass


def tool(pid, out):
    return {"id": pid, "type": "tool", "tool": "bash",
            "state": {"status": "done", "input": {}, "output": out}}


class Trim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, Bus())
        self.sid = self.ad.create_session()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _msg(self, parts):
        return [{"info": {"id": "m1", "role": "assistant"}, "parts": parts}]

    def test_长输出截断并标出全长(self):
        got = self.ad._trim_outputs(self._msg([tool("p1", "x" * 9000)]))
        st = got[0]["parts"][0]["state"]
        self.assertEqual(len(st["output"]), CAP)
        self.assertEqual(st["outputMore"], 9000)

    def test_短输出原样不动(self):
        rows = self._msg([tool("p1", "很短")])
        got = self.ad._trim_outputs(rows)
        self.assertIs(got[0], rows[0], "没长输出的整条不该重建")
        self.assertNotIn("outputMore", got[0]["parts"][0]["state"])

    def test_不改原对象(self):
        """messages() 的返回别处还拿着(撤回会 pop、pi 会就地修图片标记),改脏了会牵连。"""
        rows = self._msg([tool("p1", "y" * 9000)])
        self.ad._trim_outputs(rows)
        self.assertEqual(len(rows[0]["parts"][0]["state"]["output"]), 9000)

    def test_非工具的块不动(self):
        rows = self._msg([{"id": "t1", "type": "text", "text": "z" * 9000}])
        got = self.ad._trim_outputs(rows)
        self.assertEqual(len(got[0]["parts"][0]["text"]), 9000)

    def test_同一条消息里长短混着也对(self):
        got = self.ad._trim_outputs(self._msg([tool("p1", "短"), tool("p2", "w" * 9000)]))
        self.assertEqual(got[0]["parts"][0]["state"]["output"], "短")
        self.assertEqual(len(got[0]["parts"][1]["state"]["output"]), CAP)

    def test_列表那条路真的截了(self):
        for p in self._msg([tool("p1", "q" * 9000)]):
            self.ad._append_msg(self.sid, p)
        body = json.loads(b"[" + self.ad.messages_window_json(self.sid, 25) + b"]")
        st = body[0]["parts"][0]["state"]
        self.assertEqual(len(st["output"]), CAP)
        self.assertEqual(st["outputMore"], 9000)

    def test_存储里仍是全文(self):
        """截的是送出去的那一份,不是记录本身——不然展开就再也取不回来了。"""
        for p in self._msg([tool("p1", "r" * 9000)]):
            self.ad._append_msg(self.sid, p)
        self.ad.messages_window_json(self.sid, 25)
        full = self.ad.messages(self.sid)[0]["parts"][0]["state"]["output"]
        self.assertEqual(len(full), 9000)

    def test_按块取全文(self):
        for p in self._msg([tool("p1", "s" * 9000)]):
            self.ad._append_msg(self.sid, p)
        got = self.ad.part(self.sid, "p1")
        self.assertEqual(len(got["state"]["output"]), 9000)

    def test_运行中的块也能按需取全文(self):
        live_tool = tool("live-p1", "进度" * 4000)
        self.ad._live[self.sid] = {
            "sid": self.sid, "msg_id": "live-m1", "parts": {"live-p1": live_tool},
            "order": ["live-p1"], "t0": 1,
        }
        got = self.ad.part(self.sid, "live-p1")
        self.assertEqual(got["state"]["output"], "进度" * 4000)

    def test_取不存在的块给空(self):
        self.assertIsNone(self.ad.part(self.sid, "没这块"))

    def test_上限取值有据(self):
        """p50 是 1925:上限低于它的话,多数输出都要多跑一趟请求。"""
        self.assertGreaterEqual(CAP, 1925)


class Wiring(unittest.TestCase):
    UI = _srcmap.ui_server_src()

    def test_有取块的路由(self):
        self.assertIn('seg[3] == "part" and method == "GET"', self.UI)
        self.assertIn("ad.part(seg[2]", self.UI)

    def test_展开时才去取(self):
        self.assertIn("function hsStepFull(v, part)", CHAT)
        m = re.search(r"function hsStepDetailMount\(part, v\)[\s\S]*?\n}", CHAT)
        self.assertTrue(m)
        self.assertIn("hsStepFull(v, part)", m.group(0))

    def test_没被截过的不去取(self):
        """守的是「两个截断标记都没有就不请求」。2026-08-25 入参也开始截了,
        判据从只看 outputMore 变成两者任一,断言跟着按意图改——原来那句写死的字面
        变成「找不到」而不是变红,那比红更糟。"""
        seg = re.search(r"function hsStepFull\(v, part\)[\s\S]+?\n}", CHAT).group(0)
        m = re.search(r"if \(\(?!?st\.outputMore[\s\S]{0,60}?part\._fullPending\) return;", seg)
        self.assertIsNotNone(m, "没有「没被截就不请求」这道判据")
        self.assertIn("_fullPending", m.group(0), "没有防重入")

    def test_取过一次就不再取(self):
        """取回来要把截断标记清掉,否则下次展开又请求一遍。
        取整个函数而不是固定的前 900 字符——函数一长,断言就悄悄落在函数外面。"""
        seg = re.search(r"function hsStepFull\(v, part\)[\s\S]+?\n}", CHAT).group(0)
        self.assertIn("delete part.state.outputMore;", seg)
        self.assertIn("part._fullPending = false", seg)

    def test_每个调用点都带上了视图(self):
        """少带一处,那一处展开就永远只有截断的那一截。"""
        self.assertEqual(CHAT.count("hsStepDetailMount(part)"), 0)
        self.assertGreaterEqual(CHAT.count("hsStepDetailMount(part, v)"), 3)

    def test_收起后释放详情且纯进度不进入绘制链(self):
        paint = re.search(r"function hsPaintTool\(v, part, host\)[\s\S]+?\n}", CHAT).group(0)
        quiet_fn = re.search(r"function hsToolProgressQuiet\(.*?\n}", CHAT, re.S).group(0)
        apply = re.search(r"function hsApply\(v, type, p\)[\s\S]+?\n}", CHAT).group(0)
        self.assertIn('querySelector(".hs-step-d")?.remove()', paint)
        self.assertIn("q.progressOnly", quiet_fn)
        self.assertIn('!part.el.classList.contains("open")', quiet_fn)
        self.assertIn("hsToolProgressQuiet(q, part, priorToolStatus)", apply)
        quiet = apply.index("hsToolProgressQuiet(q, part, priorToolStatus)")
        self.assertLess(quiet, apply.index("hsTodoSync(v, q)", quiet))
        self.assertLess(quiet, apply.index("hsPaintLive(v, part)", quiet))
        agents = re.search(r"async function hsAgentsSync\(v\)[\s\S]+?\n}", CHAT).group(0)
        self.assertLess(agents.index("hsAgentsWatch(v, true)"), agents.index("await api("))


if __name__ == "__main__":
    unittest.main()


def tool_in(pid, inp, out=""):
    return {"id": pid, "type": "tool", "tool": "bash",
            "state": {"status": "done", "input": inp, "output": out}}


class TrimInput(unittest.TestCase):
    """入参也截。

    2026-08-25 拆一段真实会话的返回体:工具那一类占 69%,其中入参 41% 比输出 22% 还大——
    长命令、heredoc、整段内联脚本都在入参里,而原来只截输出,大头漏了。

    实测收益比这个比例小得多(791KB → 751KB,只裁到 20 个块):那 41% 摊在 258 个工具块上、
    平均每个 1.2KB,多数低于上限。所以这条截的是极端情形,不是常态——常态那部分由压缩接手
    (整份 gzip 之后省 72%)。两件事各管一段,别指望其中一件把另一件的活也干了。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, Bus())

    def tearDown(self):
        self.tmp.cleanup()

    def _msg(self, parts):
        return [{"info": {"id": "m1", "role": "assistant"}, "parts": parts}]

    def test_长入参截断并标出全长(self):
        got = self.ad._trim_outputs(self._msg([tool_in("p1", {"command": "y" * 9000})]))
        st = got[0]["parts"][0]["state"]
        self.assertEqual(len(st["input"]["command"]), CAP)
        self.assertEqual(st["inputMore"], 9000)

    def test_按值截而不是按字段名(self):
        """各工具的入参形状不同(bash 是 command、Agent 是 prompt、编辑类是 content),
        列字段名迟早漏掉一个。"""
        for key in ("command", "prompt", "content", "new_string", "随便什么名字"):
            got = self.ad._trim_outputs(self._msg([tool_in("p1", {key: "z" * 9000})]))
            st = got[0]["parts"][0]["state"]
            self.assertEqual(len(st["input"][key]), CAP, key + " 没被截")

    def test_短入参原样不动(self):
        got = self.ad._trim_outputs(self._msg([tool_in("p1", {"command": "ls"})]))
        st = got[0]["parts"][0]["state"]
        self.assertEqual(st["input"], {"command": "ls"})
        self.assertNotIn("inputMore", st)

    def test_同一块里长短字段混着(self):
        got = self.ad._trim_outputs(self._msg([
            tool_in("p1", {"command": "y" * 9000, "description": "查一下"})]))
        st = got[0]["parts"][0]["state"]
        self.assertEqual(len(st["input"]["command"]), CAP)
        self.assertEqual(st["input"]["description"], "查一下", "短的那个也被截了")

    def test_只有入参超长时也要截(self):
        """写文件、派子代理这类:输出为空而入参很大。原来的判据只看输出,这一路整个漏掉。"""
        got = self.ad._trim_outputs(self._msg([tool_in("p1", {"content": "y" * 9000}, "")]))
        st = got[0]["parts"][0]["state"]
        self.assertEqual(st["inputMore"], 9000)

    def test_不改原对象(self):
        src = self._msg([tool_in("p1", {"command": "y" * 9000})])
        self.ad._trim_outputs(src)
        self.assertEqual(len(src[0]["parts"][0]["state"]["input"]["command"]), 9000)

    def test_入参不是字典时不炸(self):
        got = self.ad._trim_outputs(self._msg([tool_in("p1", "一串字符串")]))
        self.assertEqual(got[0]["parts"][0]["state"]["input"], "一串字符串")


class FrontendTakesBoth(unittest.TestCase):
    def test_入参被截时也去取全块(self):
        seg = re.search(r"function hsStepFull\(v, part\)[\s\S]+?\n}", CHAT).group(0)
        self.assertIn("st.inputMore", seg, "只认输出被截,入参那一路取不回全文")

    def test_取回来要把入参也补上(self):
        seg = re.search(r"function hsStepFull\(v, part\)[\s\S]+?\n}", CHAT).group(0)
        self.assertIn("delete part.state.inputMore", seg)
        self.assertIn("part.state.input = gs.input", seg)

    def test_输出为空的那一路不能提前返回(self):
        """写文件、派子代理这类输出为空:判据若是「output 是不是字符串」,
        这一路会在补全之前 return,入参永远补不回来。"""
        seg = re.search(r"function hsStepFull\(v, part\)[\s\S]+?\n}", CHAT).group(0)
        self.assertNotIn('if (typeof full !== "string" || !part.el) return;', seg,
                         "还按 output 是不是字符串提前返回")
        self.assertIn("if (!got || !part.el) return;", seg)
