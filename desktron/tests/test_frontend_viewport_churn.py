"""贴底态下的残留上跳:状态行每秒刷新、回合收尾定稿、状态副行每秒同值重写(2026-09-07)。

现场取证(docs/chat-viewport.md「取证」):审批轮询那一路修掉之后,埋点又抓到几种较小的上跳。
共同点是节点被摘掉再放回,位置按「暂时变矮」的内容被夹住,放回后不会自己长回来。状态行改成
就地改字;收尾定稿放进布局事务,事务收尾立刻把贴底位置写回;副行与其他每秒重写的文字统一走
hsSetText,同值不碰 DOM。
"""
import json
import os
import re
import shutil
import subprocess
import unittest

import _srcmap
from frontend_i18n_js import NODE_PRELUDE

CHAT = _srcmap.chat_src()


def fn(name):
    m = re.search(r"\nfunction %s\(.*?\n}" % re.escape(name), CHAT, re.S)
    assert m, name
    return m.group(0)


# 只模拟这条函数会碰到的接口:childNodes、nodeType、nodeValue、textContent、remove、appendChild。
# 每次改动记一笔,断言看的是「动了几次 DOM」。
DOM = r"""
let ops = [];
class Text { constructor(t){this.nodeType=3;this._v=t;} get nodeValue(){return this._v;}
  set nodeValue(v){ops.push("text="+v);this._v=v;} get textContent(){return this._v;} }
class El { constructor(cls){this.nodeType=1;this.className=cls||"";this.childNodes=[];}
  get firstChild(){return this.childNodes[0]||null;}
  get textContent(){return this.childNodes.map(n=>n.textContent).join("");}
  set textContent(v){ops.push("clear."+(this.className||"new"));this.childNodes=v?[new Text(v)]:[];}
  appendChild(n){ops.push("append."+(n.className||"#text"));this.childNodes.push(n);n.parentNode=this;return n;}
  remove(){ops.push("remove."+this.className);const p=this.parentNode;if(p)p.childNodes=p.childNodes.filter(x=>x!==this);} }
const document={createElement:(t)=>new El(""),createTextNode:(t)=>new Text(t)};
"""


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class 状态行就地改字(unittest.TestCase):
    def run_js(self, body):
        r = subprocess.run(["node", "-e", NODE_PRELUDE + DOM + fn("hsStTxt") + body],
                           capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip())

    def test_每秒刷耗时只改附注文字(self):
        out = self.run_js("""
const el = new El("hs-st-t");
hsStTxt(el, "正在输出", " 12s"); const first = ops.slice(); ops = [];
hsStTxt(el, "正在输出", " 13s");
console.log(JSON.stringify({first, second: ops, text: el.textContent, n: el.childNodes.length}));""")
        self.assertEqual(out["second"], ["text= 13s"])
        self.assertEqual(out["text"], "正在输出 13s")
        self.assertEqual(out["n"], 2)

    def test_内容相同一次都不碰(self):
        out = self.run_js("""
const el = new El("hs-st-t");
hsStTxt(el, "已完成", " 3.2s"); ops = [];
hsStTxt(el, "已完成", " 3.2s");
console.log(JSON.stringify(ops));""")
        self.assertEqual(out, [])

    def test_换阶段与去掉附注都不整块重建(self):
        out = self.run_js("""
const el = new El("hs-st-t");
hsStTxt(el, "正在输出", " 12s"); ops = [];
hsStTxt(el, "等待中…", " 5s"); const a = ops.slice(); ops = [];
hsStTxt(el, "已完成", "");      const b = ops.slice(); ops = [];
hsStTxt(el, "已完成", " 40s");  const c = ops.slice();
console.log(JSON.stringify({a, b, c, text: el.textContent}));""")
        self.assertEqual(out["a"], ["text=等待中…", "text= 5s"])
        self.assertEqual(out["b"], ["text=已完成", "remove.hs-st-d"])
        self.assertNotIn("clear.hs-st-t", out["c"], "只有文字时加附注也不该清空整行")
        self.assertEqual(out["text"], "已完成 40s")

    def test_结构不对时退回整块重写(self):
        """别处塞进来的节点、或空元素:老路照走,结果一样。"""
        out = self.run_js("""
const el = new El("hs-st-t");
el.appendChild(new El("x")); el.appendChild(new Text("y")); ops = [];
hsStTxt(el, "正在输出", " 1s");
console.log(JSON.stringify({ops, text: el.textContent}));""")
        self.assertEqual(out["ops"][0], "clear.hs-st-t")
        self.assertEqual(out["text"], "正在输出 1s")


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class 副行同值刷新不摘挂节点(unittest.TestCase):
    """录屏取证:每秒 17px 上跳,mut 行 add/rm 各一个 #text,来自 hsStatusPaint 无条件
    nx.textContent=。textContent 赋值等于摘掉旧文本节点再挂新节点,摘掉那一瞬副行没了。"""

    def run_js(self, body):
        r = subprocess.run(["node", "-e", NODE_PRELUDE + DOM + fn("hsSetText") + body],
                           capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip())

    def test_同一句话每秒重写零次DOM操作(self):
        out = self.run_js("""
const el = new El("hs-st-next");
hsSetText(el, "接下来 跑测试"); const first = ops.slice(); ops = [];
for (let i = 0; i < 5; i++) hsSetText(el, "接下来 跑测试");
console.log(JSON.stringify({first, again: ops, n: el.childNodes.length, text: el.textContent}));""")
        self.assertEqual(out["first"], ["append.#text"])
        self.assertEqual(out["again"], [])
        self.assertEqual(out["n"], 1)
        self.assertEqual(out["text"], "接下来 跑测试")

    def test_换句只改文本节点的值(self):
        out = self.run_js("""
const el = new El("hs-st-next");
hsSetText(el, "思考中…"); ops = [];
hsSetText(el, "思考 · 3.2s"); const a = ops.slice(); ops = [];
hsSetText(el, "");            const b = ops.slice(); ops = [];
hsSetText(el, "又来了");        const c = ops.slice();
console.log(JSON.stringify({a, b, c, text: el.textContent, n: el.childNodes.length}));""")
        self.assertEqual(out["a"], ["text=思考 · 3.2s"])
        self.assertEqual(out["b"], ["text="], "清空也只改值,不摘节点:下一秒还要用")
        self.assertEqual(out["c"], ["text=又来了"])
        self.assertEqual(out["n"], 1)

    def test_多子节点时退回整块重写(self):
        out = self.run_js("""
const el = new El("x");
el.appendChild(new El("i")); el.appendChild(new Text("y")); ops = [];
hsSetText(el, "z");
console.log(JSON.stringify({ops, text: el.textContent}));""")
        self.assertEqual(out["ops"], ["clear.x"])
        self.assertEqual(out["text"], "z")

    def test_空元素与空串互不惊动(self):
        out = self.run_js("""
const el = new El("x");
hsSetText(el, ""); hsSetText(null, "a"); hsSetText(el, null);
console.log(JSON.stringify(ops));""")
        self.assertEqual(out, [])


class 每秒重写的文字都走就地改字(unittest.TestCase):
    """源码守卫:正文里按计时器刷新的几处文字,不许再用 textContent 赋值换节点。"""

    def test_状态副行(self):
        body = fn("hsStatusPaint")
        self.assertIn('hsSetText(nx, working ? txt : "")', body)
        self.assertNotIn("nx.textContent =", body)

    def test_其他计时刷新点(self):
        rd = lambda f: open(os.path.join(_srcmap.FRONTEND, f), encoding="utf-8").read()
        srcs = {"model": CHAT, "detail": rd("app-chat-detail.js"), "tasksbar": rd("app-chat-tasksbar.js")}
        for name, needle in (("model", 'hsSetText(st, span > 0 ? " " + hsClock(span) : "")'),
                             ("model", 'hsSetText(v.compactEl.querySelector(".hs-cbar-n"), pct + "%")'),
                             ("detail", 'hsSetText(l, settled ? (dur ? DeskI18n.t("think.doneFor", { dur }) : DeskI18n.t("think.done")) : DeskI18n.t("think.running"))'),
                             ("detail", 'hsSetText(nEl, ret ? hsReturnVerb(ret)'),
                             ("tasksbar", 'hsSetText(el, st.dot === "run" ?')):
            self.assertIn(needle, srcs[name], name + " 里 " + needle)
        head = re.search(r"function hsAgentHead\(.*?\n}", srcs["tasksbar"], re.S).group(0)
        self.assertNotIn("el.textContent =", head)


class 工具行重建走布局事务(unittest.TestCase):
    """工具状态切换时整行 innerHTML 重建,先矮后高,贴底位置被夹(现场约 31px)。
    只有签名真变了才开事务:同签名的更新不重建,也就不必量气泡。"""

    def test_只在签名变化时开事务(self):
        body = fn("hsPaintLive")
        self.assertIn('part.sig === hsToolSig(part)) { hsPaint(v, part); return; }', body)
        self.assertIn('hsViewportChange(v, () => hsPaint(v, part), false, "tool")', body)
        self.assertLess(body.index("hsToolSig"), body.index('false, "tool")'))

    def test_重建判断与签名同源(self):
        self.assertIn("const sig = hsToolSig(part);", fn("hsPaintTool"))
        self.assertNotIn("${st.status}|", fn("hsPaintTool"))


class 收尾定稿走布局事务(unittest.TestCase):
    def test_定稿循环包在事务里并带来源标签(self):
        body = fn("hsFinalize")
        self.assertIn('hsViewportChange(v, () => {', body)
        self.assertIn('"finalize"', body)
        self.assertLess(body.index("hsViewportChange"), body.index("hsPaint(v, part)"))
        self.assertLess(body.index("hsFoot(v, t)"), body.index('"finalize"'),
                        "脚注也在事务内:它同样改正文高度")


if __name__ == "__main__":
    unittest.main()
