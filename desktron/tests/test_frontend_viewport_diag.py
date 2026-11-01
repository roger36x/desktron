"""会话视口跳动取证(2026-09-07)。

用户实见:输出期间「处理中」折叠着、画面布局没有可见变化,正文仍先上跳、再快速回底。
此前只有模拟用例能复现一部分,现场没有证据。这里落的是只取证、不改滚动行为的埋点:
每页一个环形缓冲,贴底态下无人操作却上跳时把缓冲倒进 deskLog。

守三件事:钩子可缺省(十个测试按函数名抽取控制器真实源码在 node 里跑)、判定器行为、
日志里只有结构与数值。
"""
import json
import re
import subprocess
import unittest

import _srcmap

CHAT = _srcmap.chat_src()


def fn(name):
    m = re.search(r"\n(?:async )?function %s\(.*?\n}" % re.escape(name), CHAT, re.S)
    assert m, name
    return m.group(0)


VPD = ["hsViewport", "hsViewportWrite", "hsVpd", "hsVpdState", "hsVpdNote", "hsVpdWrite",
       "hsVpdRo", "hsVpdScroll", "hsVpdInput", "hsVpdCheck", "hsVpdBuild", "hsVpdWatchDom"]

HARNESS = r"""
let now=1000; const performance={now:()=>now};
const HS_VPD_KEEP=80,HS_VPD_DUMP=28,HS_VPD_JUMP=8,HS_VPD_QUIET=500,HS_VPD_GAP=2000,HS_VPD_MAX=16; let _hsVpdBuild="";
const logs=[]; function deskLog(s){logs.push(String(s))} function hsTocSchedule(){}
let top=1400, maxTop=1400;
const body={get scrollHeight(){return maxTop+600},clientHeight:600,isConnected:true,
  get scrollTop(){return top}, set scrollTop(t){top=Math.max(0,Math.min(maxTop,t))}};
const v={sid:"adp_test",body,stick:true,busy:true,turns:[],lastTop:1400};
"""


class 钩子可缺省(unittest.TestCase):
    def test_控制器与页面里的钩子都走可选调用(self):
        """钩子函数不在各测试的抽取集里,直呼其名会 ReferenceError。"""
        for name in ["hsViewportWrite", "hsViewportChange", "hsSetStick", "hsObserveLayout",
                     "hsBusy", "hsBuild", "hsPaint", "hsPaintTool", "hsStatus", "hsPrepend"]:
            body = fn(name)
            bare = re.findall(r"(?<![\w.])hsVpd\w*\(", body)
            self.assertEqual(bare, [], "%s 里有裸调用 %s" % (name, bare))
            self.assertIn("globalThis.hsVpd", body, "%s 没有接取证钩子" % name)

    def test_写入点带来源标签(self):
        body = fn("hsViewportWrite")
        self.assertIn("function hsViewportWrite(v, top, why)", body)
        for tag in ('"follow"', '"bottom"', '"reveal"', '"hold"', '"anchor-block"'):
            self.assertIn(tag, CHAT, "写入点调用方缺少来源标签 " + tag)

    def test_布局事务带来源标签(self):
        for tag in ('"rebuild"', '"trim"', '"remount"', '"fold-user"', '"fold-ro"', '"finalize"', '"tool"'):
            self.assertIn(tag, CHAT, "布局事务缺少来源标签 " + tag)


class 判定器(unittest.TestCase):
    def run_js(self, body):
        code = "\n".join(fn(n) for n in VPD) + HARNESS + body
        r = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=15, encoding='utf-8')
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_自家写入的回声不倒(self):
        out = self.run_js("""
hsViewportWrite(v,1400,"follow"); hsVpdScroll(v);
console.log(JSON.stringify({n:logs.length}));""")
        self.assertEqual(out["n"], 0)

    def test_浏览器夹小位置无人操作则倒出现场(self):
        out = self.run_js("""
hsViewportWrite(v,1400,"follow"); top=1350; hsVpdScroll(v);
console.log(JSON.stringify({n:logs.length, head:logs[0]||"", rows:logs.slice(1)}));""")
        self.assertGreater(out["n"], 1)
        self.assertIn("上跳 50px 来源=scroll", out["head"])
        self.assertTrue(any('"k":"scroll"' in r and '"echo":0' in r for r in out["rows"]),
                        "缓冲里要有一条 echo=0 的 scroll 行,说明不是自家写入的回声")

    def test_最近有用户输入则不倒(self):
        out = self.run_js("""
hsVpdInput(v,"wheel-up"); top=1300; hsVpdScroll(v);
console.log(JSON.stringify({n:logs.length}));""")
        self.assertEqual(out["n"], 0)

    def test_贴底态下写入点被缩高夹住也倒(self):
        out = self.run_js("""
maxTop=1360; hsViewportWrite(v, maxTop, "bottom");
console.log(JSON.stringify({n:logs.length, head:logs[0]||""}));""")
        self.assertGreater(out["n"], 0)
        self.assertIn("来源=write:bottom", out["head"])

    def test_限流与计数(self):
        out = self.run_js("""
top=1350; hsVpdScroll(v); const n=logs.length;
now+=100; v.lastTop=1400; top=1300; hsVpdScroll(v);
console.log(JSON.stringify({same:logs.length===n, held:v.vpd.held}));""")
        self.assertTrue(out["same"])
        self.assertEqual(out["held"], 1)

    def test_日志只有结构与数值(self):
        """行里不该出现正文、工具参数;这里的判据是键名白名单。"""
        out = self.run_js("""
hsVpdNote(v,"paint",{pt:"text",done:0,nw:0,len:120,pl:100});
hsVpdNote(v,"tool",{st:"running",open:0,sub:0});
hsVpdNote(v,"status",{fresh:0,moved:1,cls:0,slot:1,st:"working"});
hsVpdRo(v,[{target:v.body,contentRect:{height:600}}]);
top=1350; hsVpdScroll(v);
console.log(JSON.stringify({rows:logs.slice(1)}));""")
        allowed = {"t", "k", "why", "from", "want", "got", "sh", "ch", "top", "prev", "sinceW", "echo",
                   "st", "bz", "rs", "dg", "ep", "an", "dp", "ho", "pt", "done", "nw", "len", "pl",
                   "open", "sub", "fresh", "moved", "cls", "slot", "tg", "h", "src", "on", "was",
                   "ph", "same", "n", "cd", "add", "rm", "at"}
        for r in out["rows"]:
            row = json.loads(r[len("[vpd]  "):])
            self.assertTrue(set(row) <= allowed, "出现白名单之外的键: %s" % (set(row) - allowed))
            for val in row.values():
                if isinstance(val, str):
                    self.assertLess(len(val), 24, "字符串值只能是短标签: %r" % val)


if __name__ == "__main__":
    unittest.main()
