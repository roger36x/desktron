"""文档里挂多少个回合有上限(2026-08-22)。

页面开着不关,回合只增不减:后端给的 25 个窗口只管首屏,之后每一轮都往 DOM 里加,
此前没有任何一处裁剪。用户实见 Desk 涨到 2~3GB;同一台机器上还有一个停在 Desk 页面上
没退的无头浏览器涨到 18.5GB——那是同一件事的极端版本,进程不退、页面一直收、DOM 一直长。

裁的是 DOM 不是数据:回合对象连同 parts 都留在 v.turns 里,刻度那一列照旧按 v.turns 画,
所以摘掉的回合在尺子上仍然看得见、点得到;人一往上走就挂回来。
"""
import json
import os
import re
import shutil
import subprocess
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()


def fn(name):
    m = re.search(r"\nfunction %s\(.*?\n}\n" % re.escape(name), CHAT, re.S)
    assert m, name + " 不见了"
    return m.group(0)


HARNESS = r"""
class Node {
  constructor(tag){this.tag=tag;this.children=[];this.parentNode=null;this.className="";}
  get isConnected(){return !!this.view && this.parentNode===this.view.stream}
  getBoundingClientRect(){const top=this.parentNode.children.indexOf(this)*100-this.view.body.scrollTop;
    return {top,bottom:top+100,height:100};}
  get childNodes(){ return this.children; }   // 真 DocumentFragment 有这个,hsRemount 用它判空
  appendChild(n){ if(n.tag==="#frag"){ for(const c of n.children.slice()) this.appendChild(c); n.children=[]; return n; }
                  if(n.parentNode) n.parentNode.children.splice(n.parentNode.children.indexOf(n),1);
                  this.children.push(n); n.parentNode=this; return n; }
  insertBefore(n,ref){ const at = ref ? this.children.indexOf(ref) : this.children.length;
    const put=(x)=>{ if(x.parentNode) x.parentNode.children.splice(x.parentNode.children.indexOf(x),1); x.parentNode=this; };
    if(n.tag==="#frag"){ const kids=n.children.slice(); n.children=[]; kids.forEach(put);
                         this.children.splice(at<0?this.children.length:at,0,...kids); return n; }
    put(n); this.children.splice(at<0?this.children.length:at,0,n); return n; }
  remove(){ if(this.parentNode){ this.parentNode.children.splice(this.parentNode.children.indexOf(this),1); this.parentNode=null; } }
}
const document = { createElement:(t)=>new Node(t), createDocumentFragment:()=>new Node("#frag") };
function hsMeasureBubbles(){}
function hsTocSchedule(){}
function cancelAnimationFrame(){}
// 每个回合当作等高 100:scrollHeight 就是挂着的回合数 × 100,补偿算得出来。
function mkView(hidden){
  const stream = new Node("div");
  const body = { scrollTop: 0, clientHeight:600,isConnected:true,
    classList:{remove(){},add(){}},getBoundingClientRect:()=>({top:0}),
    get scrollHeight(){ return stream.children.length * 100; } };
  // hsHidden 判的是这两样:节点还在文档里、且量得出版面位置(display:none 时 offsetParent 为 null)
  const el = { isConnected: true, offsetParent: hidden ? null : {} };
  return { el, stream, body, turns: [], stick: true, pouring: false, domTrimmed: false };
}
function addTurn(v){
  const el = new Node("div");
  el.view=v;
  const t = { el, parts: new Map() };
  v.stream.appendChild(el);
  v.turns.push(t);
  hsTrimDom(v);
  return t;
}
function mounted(v){ return v.turns.filter((t)=>t.el && t.el.parentNode).length; }
function order(v){ return v.stream.children.map((el)=>v.turns.findIndex((t)=>t.el===el)); }
SRC
BODY
"""


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class TrimAndRemount(unittest.TestCase):
    def _run(self, body, phone=False):
        src = (fn("hsTrimDom") + fn("hsRemount") + fn("hsHidden")
               + "\nconst HS_DOM_TURNS = %s;\n" % self._limit())
        if phone:
            src += "\nfunction hsPhoneInput(){return true;}\n"
            for name in ("HS_PHONE_DOM_TURNS", "HS_PHONE_HIDDEN_DOM_TURNS"):
                src += re.search(r"const " + name + r" = \d+;", CHAT).group() + "\n"
        src += "\n".join(fn(n) for n in ["hsViewport", "hsViewportChange", "hsViewportAnchor",
            "hsTurnAnchor", "hsTurnAtAnchor", "hsBlockPath", "hsBlockAt", "hsScrollCommit",
            "hsViewportWrite", "hsViewportCommit", "hsRestoreViewportAnchor", "hsViewportCancel",
            "hsViewportApplyAnchor", "hsHoldCapture", "hsHoldAdjust"])
        js = HARNESS.replace("SRC", src).replace("BODY", body)
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def _limit(self):
        m = re.search(r"const HS_DOM_TURNS = (\d+);", CHAT)
        self.assertTrue(m, "HS_DOM_TURNS 不见了")
        return m.group(1)

    def test_phone_repeated_scroll_keeps_turns_and_restores_locally(self):
        got = self._run(r"""
const v=mkView();
for(let i=0;i<55;i++) addTurn(v);
const original=v.turns.slice(), nodes=original.map(t=>t.el);
v.turns[0].parts.set('text',{text:'older reply'});
const cycles=[];
for(let i=0;i<4;i++) {
  v.stick=false; v.body.scrollTop=300;
  hsRemount(v); // hsMore is deliberately undefined: scrolling must not refetch history.
  const restored=[mounted(v),v.body.scrollTop,order(v)];
  hsTrimDom(v); // Reading history does not trim it.
  const reading=mounted(v);
  v.stick=true; hsTrimDom(v);
  cycles.push({restored,reading,attached:mounted(v),retained:v.turns.length,
    same:v.turns.every((t,n)=>t===original[n] && t.el===nodes[n]),
    mobileTrimmed:!!v.mobileTrimmed});
}
console.log(JSON.stringify({cycles,text:v.turns[0].parts.get('text').text}));
""", phone=True)
        self.assertEqual(got["text"], "older reply")
        for i, cycle in enumerate(got["cycles"]):
            self.assertEqual(cycle["restored"], [55, 3300 if i == 0 else 300, list(range(55))])
            self.assertEqual(cycle["reading"], 55)
            self.assertEqual(cycle["attached"], 55)
            self.assertEqual(cycle["retained"], 55)
            self.assertTrue(cycle["same"])
            self.assertFalse(cycle["mobileTrimmed"])

    def test_没超过上限时一个都不摘(self):
        n = self._run("const v=mkView(); for(let i=0;i<%s;i++) addTurn(v);"
                      "console.log(JSON.stringify([mounted(v), v.turns.length, v.domTrimmed]));"
                      % self._limit())
        self.assertEqual(n, [int(self._limit()), int(self._limit()), False])

    def test_超过就摘掉最老的(self):
        got = self._run("const v=mkView(); for(let i=0;i<%d;i++) addTurn(v);"
                        "console.log(JSON.stringify([mounted(v), v.turns.length, order(v)[0]]));"
                        % (int(self._limit()) + 25))
        self.assertEqual(got[0], int(self._limit()), "文档里挂着的超了上限")
        self.assertEqual(got[1], int(self._limit()) + 25, "数据被一起裁掉了")
        self.assertEqual(got[2], 25, "摘掉的不是最老的那几轮")

    def test_露着的那页人往上走就不再摘(self):
        """他正在读的就是那些内容。前提是这一页露着——盖住的那页没有阅读位置要护。"""
        got = self._run("const v=mkView(); v.stick=false; for(let i=0;i<%d;i++) addTurn(v);"
                        "console.log(JSON.stringify([mounted(v), v.domTrimmed]));"
                        % (int(self._limit()) + 25))
        self.assertEqual(got, [int(self._limit()) + 25, False])

    def test_盖住的那页没贴底也照样摘(self):
        """并行时人只停在一段上,其余几段各自往上翻过(stick 已是 false)。原来这道闸对它们
        全部落空,DOM 只增不减——2026-08-24 实测界面进程涨到 5.79GB。"""
        got = self._run("const v=mkView(true); v.stick=false; for(let i=0;i<%d;i++) addTurn(v);"
                        "console.log(JSON.stringify([mounted(v), v.turns.length, v.domTrimmed]));"
                        % (int(self._limit()) + 25))
        self.assertEqual(got[0], int(self._limit()), "盖住的那页没裁")
        self.assertEqual(got[1], int(self._limit()) + 25, "数据被一起裁掉了")
        self.assertTrue(got[2])

    def test_盖住时裁掉的仍在数据里且挂得回来(self):
        """裁的是 DOM 不是数据:露面时 hsShown 会挂回来,刻度点得到。"""
        got = self._run("const v=mkView(true); v.stick=false; for(let i=0;i<%d;i++) addTurn(v);"
                        "v.el.offsetParent={}; hsRemount(v);"
                        "console.log(JSON.stringify([mounted(v), order(v), v.domTrimmed]));"
                        % (int(self._limit()) + 25))
        n = int(self._limit()) + 25
        self.assertEqual(got[0], n, "没有全部挂回来")
        self.assertEqual(got[1], list(range(n)), "挂回来的顺序乱了")
        self.assertFalse(got[2])

    def test_铺历史那一批不摘(self):
        """整批回填期间 stick 还是真的,但那时正在铺,摘了下一句就要挂回来。"""
        got = self._run("const v=mkView(); v.pouring=true; for(let i=0;i<%d;i++) addTurn(v);"
                        "console.log(JSON.stringify(mounted(v)));" % (int(self._limit()) + 25))
        self.assertEqual(got, int(self._limit()) + 25)

    def test_挂回来按原顺序(self):
        got = self._run("const v=mkView(); for(let i=0;i<%d;i++) addTurn(v);"
                        "hsRemount(v);"
                        "console.log(JSON.stringify([mounted(v), order(v), v.domTrimmed]));"
                        % (int(self._limit()) + 25))
        n = int(self._limit()) + 25
        self.assertEqual(got[0], n, "没有全部挂回来")
        self.assertEqual(got[1], list(range(n)), "挂回来的顺序乱了")
        self.assertFalse(got[2])

    def test_挂回来补偿滚动位置(self):
        """内容加在视口上方,scrollTop 不跟着涨的话视线会往下跳。"""
        got = self._run("const v=mkView(); for(let i=0;i<%d;i++) addTurn(v);"
                        "v.stick=false;v.body.scrollTop = 300;"
                        "hsRemount(v);"
                        "console.log(JSON.stringify(v.body.scrollTop));"
                        % (int(self._limit()) + 25))
        self.assertEqual(got, 300 + 25 * 100, "摘掉 25 轮就该补回 25 轮的高度")

    def test_没摘过时挂回是空转(self):
        got = self._run("const v=mkView(); for(let i=0;i<3;i++) addTurn(v);"
                        "const before=v.body.scrollTop=77; hsRemount(v);"
                        "console.log(JSON.stringify([mounted(v), v.body.scrollTop===before]));")
        self.assertEqual(got, [3, True])


class Wiring(unittest.TestCase):
    def test_建完回合顺手裁一次(self):
        self.assertIn("if (!v.pouring) { hsIndex(v); hsTrimDom(v); }", CHAT)

    def test_一往上走就挂回来(self):
        """往上翻、点刻度、点回到某一轮走的都是 hsUnstick,一处接住全部上行入口。"""
        self.assertIn("hsRemount(v);   // 贴底时摘掉的那几轮挂回来", fn("hsUnstick"))

    def test_刻度按数据画_不按文档(self):
        """摘掉的回合在尺子上仍要看得见、点得到。"""
        self.assertIn("v.turns.forEach", fn("hsIndex"))

    def test_露面时只挂回仍在本地的桌面节点(self):
        """桌面裁剪只摘 DOM，露面时应本地挂回；手机已释放的旧回合不能因重开会话
        自动触发 hsMore 扩窗，等用户明确向上操作时再补。"""
        shown = fn("hsShown")
        self.assertIn("if (v.domTrimmed && !v.mobileTrimmed && !v.stick) hsRemount(v);", shown)
        self.assertNotIn("else if (v.mobileTrimmed", shown)

    def test_在跑的那页退一档裁而不是整段跳过(self):
        """密集并行时每一段都在跑,原来的 v.busy 一律跳过等于这道闸整体失效。"""
        src = fn("hsEvictCold")
        self.assertIn("if (v.busy) { hsTrimDom(v); continue; }", src)
        self.assertNotIn("v.busy || !hsHidden(v)", src)

    def test_在跑的那页不整个清(self):
        """v.turns 一空,正在收的那一轮就没有落点了。"""
        src = fn("hsEvictCold")
        busy_at = src.index("if (v.busy)")
        clear_at = src.index("v.turns = []")
        self.assertLess(busy_at, clear_at, "整页清的那几行跑在了 busy 判断前面")

    def test_翻页的_have_按数据算(self):
        """裁的是 DOM,v.turns 不变,所以 /history 的 have 不受影响。"""
        self.assertIn("const have = (v.historyNewer || 0) + v.turns.length;", CHAT)
        self.assertIn("have=${have}", CHAT)

    def test_重建时清掉标记(self):
        self.assertGreaterEqual(CHAT.count("v.turns = []; v.domTrimmed = false;"), 3)

    def test_hsPrepend_里那处不清(self):
        """那是把 turns 临时换成草稿,不是清空视图;清了会丢掉「真实流里还挂着摘掉的回合」。"""
        self.assertIn("v.stream = scratch; v.turns = []; v.stick = false;", CHAT)


if __name__ == "__main__":
    unittest.main()
