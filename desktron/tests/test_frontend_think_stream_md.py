"""思考正文边打边排,不等收尾。

过程区里的思考是模型写的 markdown。原来流式期间只摆纯文本,一段长思考在整个输出期间都是
「标题是井号、列表是星号」,收尾那一下再整段重排,跳得很明显——正文那一侧早就不是这样了
(hsStreamHtml:已定型的那截走完整 markdown,正在打的最后一段保持素排,切点取最后一个换行
并保证围栏成对)。这里把思考接到同一条路上。

复用而不是再写一份:两处的取舍完全一样(半截的 ** 会闪、每帧全解析会卡),各写一份迟早
只改一边。
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
CORE = open(os.path.join(ROOT, "frontend", "app-core.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


def js_function(name):
    match = re.search(r"\nfunction %s\(.*?\n}" % re.escape(name), CHAT, re.S)
    assert match, "%s 不见了" % name
    return match.group(0)


def think_branch():
    seg = re.search(r"function hsPaint\(v, part\)[\s\S]+?\n}", CHAT).group(0)
    i = seg.index('part.type === "reasoning"')
    return seg[i:seg.index("\n  if (!part.el)", i)]


class ThinkStreamsMarkdown(unittest.TestCase):
    def test_流式期间也走markdown(self):
        seg = think_branch()
        self.assertIn("hsNarrativePieces(part, settled, !!part.done)", seg)
        pieces = js_function("hsNarrativePieces")
        self.assertIn("hsStreamHtml(part, txt", pieces)
        self.assertNotIn("body.textContent = txt", seg)   # 纯文本那一支已经撤了

    def test_折叠时不解析并释放思考正文(self):
        seg = think_branch()
        gate = seg.index('if (!part.turn.procEl?.classList.contains("open"))')
        self.assertLess(gate, seg.index("hsNarrativePieces(part, settled, !!part.done)"))
        self.assertNotIn("md.render", seg[:gate])
        self.assertNotIn('part.el.classList.contains("open")', seg)
        self.assertIn('if (q.type !== "reasoning") continue', js_function("hsProcBox"))
        self.assertIn('hsPaint(t.view, q)', js_function("hsProcBox"))
        self.assertIn("hsThinkFull(v, part)", seg)
        self.assertIn("body.replaceChildren()", seg)
        self.assertIn("part.streamRoot = part.streamMark = null", seg)
        self.assertIn("part.mdCommittedLen = 0", seg)
        self.assertIn("part.mdHtml = part.mdCommittedHtml = part.mdCommittedStamp = null", seg)

    def test_md类从第一帧就挂上(self):
        """pre-wrap 与 markdown 输出不能共存(块级元素之间那个 \\n 会渲染成空行盒)。
        类要么一直在、要么一直不在,不能中途切。"""
        seg = think_branch()
        self.assertIn('body.classList.add("md");', seg)
        i = seg.index('body.classList.add("md")')
        self.assertNotIn("part.done", seg[max(0, i - 60):i])   # 不挂在 done 的分支里

    def test_收尾或分段定稿把活动尾部升级为完整markdown(self):
        # 完整 markdown 仍会生成，但由增量补丁并入，不能再覆盖整块 innerHTML。
        seg = think_branch()
        pieces = js_function("hsNarrativePieces")
        self.assertIn("const settled = part.done || part.settled", seg)
        self.assertIn("hsStreamPaint(part, body, hsNarrativePieces(part, settled, !!part.done))", seg)
        self.assertIn("md.render(suffix).trim()", pieces)
        self.assertIn("if (final)", pieces)
        self.assertIn("md.render(txt).trim()", pieces)
        self.assertIn('return { head, tail: "",', pieces)
        self.assertNotIn("body.innerHTML =", seg)

    def test_工具开始与新文字块都会定稿上一段(self):
        delta = re.search(r'if \(type === "message\.part\.delta"\).*?\n  }', CHAT, re.S).group(0)
        updated = re.search(r'if \(type === "message\.part\.updated"\).*?\n  }', CHAT, re.S).group(0)
        self.assertLess(delta.index("hsSettleNarrative"), delta.index("hsQueue"))
        self.assertIn('["text", "reasoning"].includes(q.type)', updated)
        self.assertIn('q.type === "tool" && (q.state || {}).status === "running"', updated)
        self.assertNotIn('q.type === "tool" && (q.state || {}).status === "done"', updated)

    @unittest.skipUnless(shutil.which("node"), "需要 node 验证分段生命周期")
    def test_分段定稿会排空缓冲且后续增量可重新打开(self):
        js = js_function("hsOpenNarrative") + js_function("hsSettleNarrative") + r'''
const painted = [], cancelled = [];
globalThis.cancelAnimationFrame = (id) => cancelled.push(id);
globalThis.hsPaintDone = (part) => { part.paintT = 0; part.paintAt = 0; };
globalThis.hsPaint = (_v, part) => painted.push(part.id);
globalThis.hsViewportChange = (_v, fn) => fn();
const prior = {id:"r1", type:"reasoning", text:"**标题**", buf:" 后缀", mid:"m1",
               done:false, settled:false, timer:7, streamHeadHtml:"old", streamTailHtml:"tail", mdAt:9};
const active = {id:"r2", type:"reasoning", text:"活动", buf:"", mid:"m1", done:false};
const foreign = {id:"r3", type:"reasoning", text:"别轮", buf:"", mid:"m2", done:false};
const turn = {parts:new Map([[prior.id,prior],[active.id,active],[foreign.id,foreign]])};
const v = {turns:[turn]};
hsSettleNarrative(v,turn,"m1","r2");
const settled = {flag:prior.settled, text:prior.text, timer:prior.timer,
                 active:!!active.settled, foreign:!!foreign.settled,
                 painted:[...painted], cancelled:[...cancelled]};
hsOpenNarrative(prior);
console.log(JSON.stringify({settled, reopened:{done:prior.done, settled:prior.settled,
  head:prior.streamHeadHtml, tail:prior.streamTailHtml, mdAt:prior.mdAt}}));
'''
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got["settled"], {
            "flag": True, "text": "**标题** 后缀", "timer": 0,
            "active": False, "foreign": False, "painted": ["r1"], "cancelled": [7],
        })
        self.assertEqual(got["reopened"], {
            "done": False, "settled": False, "head": "old", "tail": "tail", "mdAt": 0,
        })

    def test_与正文共用同一份实现(self):
        # reasoning 与正文都只经同一个分段器；分段器内部再复用唯一的流式 markdown 实现。
        self.assertEqual(len(re.findall(r"function hsStreamHtml\(", CHAT)), 1)
        self.assertEqual(len(re.findall(r"function hsNarrativePieces\(", CHAT)), 1)
        paint = js_function("hsPaint")
        self.assertEqual(paint.count("hsNarrativePieces(part,"), 3)

    def test_已定型节点与活动尾部由注释边界分开(self):
        mark = re.search(r"function hsStreamMark\(.*?\n}", CHAT, re.S).group(0)
        head = re.search(r"function hsStreamPatchHead\(.*?\n}", CHAT, re.S).group(0)
        tail = re.search(r"function hsStreamPatchTail\(.*?\n}", CHAT, re.S).group(0)
        self.assertIn('createComment("hs-live-tail")', mark)
        self.assertIn("hsStreamNodeSame", head)
        self.assertIn("root.insertBefore(n, mark)", head)
        self.assertIn("part.streamCommittedNodes", head)
        self.assertIn("html.slice(base.length)", head)
        self.assertIn("mark.nextSibling", tail)
        self.assertNotIn("root.innerHTML", head + tail)

    @unittest.skipUnless(shutil.which("node"), "需要 node 验证 markdown 工作量")
    def test_跨工具边界只解析新增后缀(self):
        source = (js_function("hsStreamHtml") + js_function("hsNarrativeStamp")
                  + js_function("hsNarrativePieces")) + r'''
const assert = require("node:assert/strict");
function stripFraming(x){return String(x || "");}
function hsSafeCut(x){return x.length;}
function hsLiteHtml(x){return x;}
let chars=0, calls=[];
const md={render(x){chars += x.length; calls.push(x.length); return `<p>${x.length}</p>`;}};
const part={text:"a".repeat(50000)};
let out=hsNarrativePieces(part,true);
assert.equal(out.commit,true);
for(let i=0;i<12;i++){
  part.done=false; part.settled=false;
  part.text += "\n\n" + String(i).padStart(2,"0") + "b".repeat(998);
  out=hsNarrativePieces(part,true);
  assert.equal(out.base.length>0,true);
}
assert.deepEqual(calls,[50000,...Array(12).fill(1000)]);
assert.equal(chars,50000+12*1000);
// 引擎终态只做一次全文规范化，随后重复绘制直接复用。
out=hsNarrativePieces(part,true,true);
assert.equal(calls.at(-1),part.text.trim().length);
const finalizedCalls=calls.length;
hsNarrativePieces(part,true,true);
assert.equal(calls.length,finalizedCalls);
// Provider 若改写旧前缀，必须关闭增量捷径并重建当前全文。
part.text="rewritten";
out=hsNarrativePieces(part,true);
assert.equal(calls.at(-1),9);
assert.equal(out.base,"");
console.log(JSON.stringify({chars,calls}));
'''
        out = subprocess.run(["node", "-e", source], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 node 验证 framing 跨边界")
    def test_framing闭合改写旧尾巴时撤销分段缓存(self):
        framing = re.search(r"const FRAMING_RE = .*?;\nfunction stripFraming\(.*?\n}", CORE, re.S).group(0)
        source = (framing + js_function("hsStreamHtml") + js_function("hsNarrativeStamp")
                  + js_function("hsNarrativePieces")) + r'''
const assert=require("node:assert/strict");
function hsSafeCut(x){return x.length;} function hsLiteHtml(x){return x;}
const calls=[],md={render(x){calls.push(x);return `<p>${x}</p>`;}};
const part={text:"正文\n\n<total_tokens>9"};
let first=hsNarrativePieces(part,true,false);
assert.ok(first.head.includes("total_tokens"));
part.text += "</total_tokens>\n\n" + "后续".repeat(100);
const next=hsNarrativePieces(part,true,false);
assert.equal(next.base,"");
assert.ok(!next.head.includes("total_tokens"));
assert.equal(part.mdCommittedLen,stripFraming(part.text).trim().length);
'''
        out = subprocess.run(["node", "-e", source], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_围栏未闭合时退到它之前(self):
        """半截的代码围栏会把后面所有内容吞进 <pre>,画面整块翻黑再翻回来。"""
        seg = re.search(r"function hsSafeCut\(t\)[\s\S]+?\n}", CHAT).group(0)
        self.assertIn("fence % 2", seg)
        self.assertIn('t.lastIndexOf("\\n```")', seg)

    def test_样式上撤掉pre_wrap(self):
        self.assertRegex(CSS, r"\.hs-think-body\.md \{[^}]*white-space: normal")


if __name__ == "__main__":
    unittest.main()
