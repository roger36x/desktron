"""消息渲染与滚动入口契约；控制器跨帧行为见 test_frontend_viewport_controller。"""
import json
import os
import re
import subprocess
import unittest

import _srcmap


CHAT = _srcmap.chat_src()
CSS = open(os.path.join(_srcmap.ROOT, "frontend", "style.css"), encoding="utf-8").read()


def fn(name):
    m = re.search(r"\n(?:async )?function %s\(.*?\n}" % re.escape(name), CHAT, re.S)
    assert m, name
    return m.group(0)


class ChatScrollStability(unittest.TestCase):
    def test_贴底只写精确底部且带阈值(self):
        seg = fn("hsSetScrollBottom")
        self.assertIn("scrollHeight - el.clientHeight", seg)
        self.assertIn("Math.abs(el.scrollTop - bottom) > 1", seg)
        self.assertIn("el.scrollTop = bottom", seg)

    def test_布局观察器覆盖正文输入区和视口(self):
        seg = fn("hsObserveLayout")
        self.assertIn("new ResizeObserver", seg)
        self.assertIn("[v.stream, composer, v.body, v.el]", seg)
        callback = seg[seg.index("new ResizeObserver"):seg.index("});", seg.index("new ResizeObserver"))]
        self.assertIn("hsScrollCommit(v)", callback)
        self.assertNotIn("requestAnimationFrame", callback)
        self.assertIn("hsObserveLayout(v)", fn("hsBuild"))

    def test_原生锚定常关滚动位单控制链(self):
        # WebKit 的原生锚定两头都靠不住(锚进流式节点被摘→重置到 0;排除流式块→
        # 尾部读者失去保护)。body 常关 overflow-anchor,贴底由 RO 提交、脱钩由
        # hsHoldAdjust 补偿、重建由语义锚点恢复,滚动位只有一条控制链。
        body_rule = CSS[CSS.index(".hs-page .hs-body {"):]
        body_rule = body_rule[:body_rule.index("}")]
        self.assertIn("overflow-anchor: none", body_rule)
        seg = fn("hsSetStick")
        self.assertIn('classList.toggle("hs-follow", next)', seg)

    def test_scroll事件只排队刻度同步(self):
        seg = fn("hsBuild")
        start = seg.index('v.body.addEventListener("scroll"')
        body = seg[start:seg.index("}, { passive: true });", start)]
        self.assertIn("hsTocSchedule(v)", body)
        self.assertNotIn("hsTocSync(v)", body)

    def test_scroll事件只回贴不脱钩(self):
        # WKWebView 在内容首次溢出滚动容器时会发一个位置没动过的 scroll 事件
        # (scrollTop=0、离底恰好越过 80px 阈值;Chromium 没有这种事件)。按位置反推
        # stick 会在这一刻把贴底关掉,整段输出从此不跟随,下一次 pin 才猛拽回底——
        # 用户看到的就是「不像 tail -f,一顿一跳」(2026-09-01 WebKit 探针实证)。
        # 所以 scroll 里位置判据只允许置 true(人滚回底部要回贴),置 false 只归
        # 明确手势:滚轮向上、拖动向上、触屏上滑、键盘上翻,四条各有监听器。
        seg = fn("hsBuild")
        start = seg.index('v.body.addEventListener("scroll"')
        body = seg[start:seg.index("}, { passive: true });", start)]
        self.assertIn("hsSetStick(v, true)", body)
        self.assertNotIn("hsSetStick(v, v.body", body)
        self.assertNotIn("hsSetStick(v, false)", body)

    def test_回贴无手势只认真正到底(self):
        # 脱钩后这一页归人:上滚下滚都不该被抓走。无手势的回贴只认真正到底(4px);
        # 80px 追赶区必须绑着明确的向下手势(拖动/滚轮/触屏各自判)。
        seg = fn("hsBuild")
        start = seg.index('v.body.addEventListener("scroll"')
        body = seg[start:seg.index("}, { passive: true });", start)]
        # 「向下到达」才回贴:向上离开的头几个 scroll 事件只挪一两像素,纯位置判
        # 会把刚脱的钩当场贴回去(探针实证七次失手一次)。
        self.assertIn("v.bottomIntent && scrollDown && gap < 80", body)
        self.assertIn("gap < 4 && !scrollUp", body)
        # 脱钩后的静默窗:跟随环自家钉底的 scroll 事件会迟到一两帧,方向判据挡不住
        # (钉底是向下移动),300ms 内不做位置回贴。
        self.assertIn("v.unstickAt", body)
        self.assertIn("v.unstickAt = performance.now()", fn("hsUnstick"))

    def test_iOS滚动停稳后按位置复核触底(self):
        # 惯性滚动可能在 300ms 静默窗内完成；到底后的弹性回弹还会让最后一帧
        # 方向变成向上。每帧重置计时，停稳后只按真实 gap 判断，不能再依赖方向。
        build = fn("hsBuild")
        scroll = build[build.index('v.body.addEventListener("scroll"'):]
        scroll = scroll[:scroll.index("}, { passive: true });")]
        self.assertIn("if (!v.stick)", scroll)
        self.assertIn("hsRestickAfterScroll(v, v.bottomIntent && scrollDown)", scroll)
        retry = fn("hsRestickAfterScroll")
        self.assertIn("clearTimeout(v.restickTimer)", retry)
        self.assertIn("setTimeout", retry)
        self.assertIn("near ? 80 : 4", retry)
        self.assertIn("gap >= limit", retry)
        self.assertIn("hsSetStick(v, true)", retry)
        self.assertIn("hsScrollCommit(v)", retry)
        cancel = fn("hsViewportCancel")
        self.assertIn("clearTimeout(v.restickTimer)", cancel)
        self.assertIn("v.restickNear = false", cancel)

    def test_向下手势进追赶区才回贴(self):
        # 流式期间底部是移动靶,只认 4px 手动追不上;滚轮/触屏向下滚进 80px 区
        # 是明确的「追上最新」,回贴并立即钉一次。被动位置漂移(锚定补偿、内容增长)
        # 不带手势,永远不会触发。
        seg = fn("hsBuild")
        wheel = seg[seg.index('v.body.addEventListener("wheel"'):]
        wheel = wheel[:wheel.index("}, { passive: true });")]
        self.assertIn("e.deltaY > 0", wheel)
        self.assertIn("v.bottomIntent = true", wheel)
        self.assertIn("< 80", wheel)
        self.assertIn("hsScrollCommit(v)", wheel)
        touch = seg[seg.index('v.body.addEventListener("touchmove"'):]
        touch = touch[:touch.index("}, { passive: true });")]
        self.assertIn("top > v.touchTop", touch)
        self.assertIn("v.bottomIntent = true", touch)
        self.assertIn("< 80", touch)

class 跟随是逐帧逼近而不是一次到位(unittest.TestCase):
    """保留缓动参数与消息渲染契约；调度与取消见 viewport_controller 行为测试。"""

    def test_跟随步进是按比例逼近(self):
        seg = fn("hsFollowTarget")
        self.assertIn("HS_FOLLOW_K", seg)
        self.assertIn("HS_FOLLOW_MIN", seg)
        self.assertIn("Math.max(HS_FOLLOW_MIN, d * k)", seg)

    def test_比例随欠量渐变而不是到顶截断(self):
        """不能拿「顺滑」换掉「看得见最新的字」:欠量越大追得越快,涨到蓄水池满时
        比例正好是 1(当帧追平)。用渐变而不是硬截断——硬截断在出字快的时候等于
        每帧原样跟住增量,又变回一行一跳,边界两侧的观感是断的。"""
        seg = fn("hsFollowTarget")
        self.assertIn("Math.min(1, d / hsFollowCap(el))", seg)
        self.assertIn("HS_FOLLOW_K + (1 - HS_FOLLOW_K) * lag * lag", seg)
        self.assertIn("Math.min(bottom,", seg)

    def test_蓄水池按视口取(self):
        """「落后多少算多」跟看得见多少有关:同样 48px,在 180px 高的窗口里是四分之一屏,
        在 900px 的窗口里几乎看不出来。定值 48px 也实测过,常规档位差别很小
        (静止帧 7% 对 5%),换成相对量是为了极端窗口高度下不失准。"""
        seg = fn("hsFollowCap")
        self.assertIn("Math.max(64, el.clientHeight * 0.5)", seg)

    def test_精确贴底仍是非运行期的写法(self):
        """回贴、恢复、收尾这些必须一步到位的地方不受影响,仍写精确底部。"""
        seg = fn("hsSetScrollBottom")
        self.assertIn("el.scrollTop = bottom", seg)
        self.assertNotIn("HS_FOLLOW_K", seg)

    def test_节流窗内尾巴逐帧直写(self):
        # 长回复的整段重绘按 80~400ms 节流,但新到的字符以 O(增量) 直写尾部文本节点,
        # 观感不冻住;直写作废尾巴缓存,到点的全量重绘按真实文本重建结构。
        seg = fn("hsPaintLive")
        self.assertIn("hsTailNudge(part)", seg)
        nudge = fn("hsTailNudge")
        self.assertIn("part.paintedLen", nudge)
        self.assertIn("part.streamTailHtml = null", nudge)
        # 全量重绘对齐基线,快照替换不会重复追加
        self.assertIn("part.paintedLen = (part.text || \"\").length", CHAT)

    def test_正文与过程双向迁移时清掉旧DOM渲染缓存(self):
        seg = fn("hsPart")
        branch = seg[seg.index('["text", "reasoning"].includes(part.type)'):]
        self.assertIn('["text", "reasoning"].includes(type)', branch)
        self.assertIn("part.el.remove()", branch)
        self.assertIn("part.streamRoot = part.streamMark = null", branch)
        self.assertIn("part.streamHeadHtml = part.streamTailHtml = null", branch)
        self.assertIn("part.paintedLen = 0", branch)
        self.assertIn("part.paintAt = 0", branch)

    def test_过程迁到正文会删除旧节点并保留同一块(self):
        """真实执行 hsPart，而不是只检查源码字符串。"""
        script = r'''
const hsTurnFor = () => { throw new Error("不应新建回合"); };
const hsAsideOwn = () => {};
const t = { parts: new Map() };
let removed = 0;
const original = { id: "p", type: "reasoning", text: "answer", mid: "m", turn: t,
  el: { remove() { removed++; } }, paintT: 0, timer: 0,
  streamRoot: {}, streamMark: {}, streamHeadHtml: "h", streamTailHtml: "t",
  paintedLen: 6, paintAt: 9 };
t.parts.set("p", original);
const v = { turns: [t] };
SRC
const got = hsPart(v, "m", "p", "text");
console.log(JSON.stringify({ same: got === original, type: got.type, removed,
  root: got.streamRoot, paintedLen: got.paintedLen, paintAt: got.paintAt }));
'''.replace("SRC", fn("hsPart"))
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got, {"same": True, "type": "text", "removed": 1,
                               "root": None, "paintedLen": 0, "paintAt": 0})

    def test_替换式快照作废尾巴基线(self):
        # 尾巴直写的基线按前缀增长记。整块替换(思考摘要、改写)之后基线指向另一份
        # 文本,再直写会把新文本的尾段接在旧内容后面。替换分支清零 paintAt,
        # 下一次 hsPaintLive 立刻全量重绘,基线重新对齐。
        seg = fn("hsMergePart")
        tail = seg[seg.index("part.buf = \"\";"):]
        self.assertIn("part.paintAt = 0", tail)

    def test_权威替换帧不会被防倒退逻辑挡住(self):
        seg = fn("hsMergePart")
        self.assertIn("if (q.replace)", seg)
        self.assertLess(seg.index("if (q.replace)"),
                        seg.index("if (visible && (!next || visible.startsWith(next)))"))
        self.assertIn('part.buf = ""', seg)
        self.assertIn("delete part.replace", seg)

    def test_权威替换帧实际覆盖旧正文并清空缓冲(self):
        seg = fn("hsResetNarrativeCommit") + fn("hsMergePart")
        js = seg + """
const part = {type: "reasoning", text: "原始思考", buf: "未排空",
              timer: 0, paintAt: 99, mdCommittedLen: 4, mdCommittedHtml: "old"};
hsMergePart(part, {type: "reasoning", text: "最终摘要", replace: true});
console.log(JSON.stringify(part));
"""
        got = subprocess.run(["node", "-e", js], check=True, text=True,
                             capture_output=True, encoding='utf-8')
        part = json.loads(got.stdout)
        self.assertEqual(part["text"], "最终摘要")
        self.assertEqual(part["buf"], "")
        self.assertEqual(part["paintAt"], 0)
        self.assertEqual(part["mdCommittedLen"], 0)
        self.assertIsNone(part["mdCommittedHtml"])
        self.assertNotIn("replace", part)

    def test_历史分批未完成时不做整页对账(self):
        seg = fn("hsReconcileNow")
        self.assertIn("if (!v.historyReady || v.pendRest) { v.recAfterLoad = true; return false; }", seg)

    def test_自持锚点的建立时机(self):
        # 脱钩那一刻、用户每次挪动视口,都要重建锚点;贴底/重建期间不留锚。
        self.assertIn("hsHoldCapture(v)", fn("hsUnstick"))
        cap = fn("hsHoldCapture")
        self.assertIn("v.stick || v.restoringScroll", cap)
        self.assertIn("v.holdAnchor = null", cap)
        # 巨大回合按层下钻:回合级的锚看不见回合内部块定型的推挤
        self.assertIn("el.children", cap)

    def test_刻度同步被rAF合并(self):
        seg = fn("hsTocSchedule")
        self.assertIn("v.tocRaf", seg)
        self.assertIn("requestAnimationFrame", seg)
        self.assertIn("hsTocSync(v)", seg)

    def test_贴底刻度列也不写越界scrollTop(self):
        seg = fn("hsTocSync")
        fast = seg[seg.index("if (v.stick)"):seg.index("const r = v.body")]
        self.assertIn("hsSetScrollBottom(v.toc)", fast)
        self.assertNotIn("v.toc.scrollTop = v.toc.scrollHeight", fast)

    def test_锚点恢复期间滚动事件不改贴底状态(self):
        seg = fn("hsBuild")
        start = seg.index('v.body.addEventListener("scroll"')
        body = seg[start:seg.index("}, { passive: true });", start)]
        self.assertIn("if (v.restoringScroll)", body)
        self.assertLess(body.index("if (v.restoringScroll)"), body.index("hsSetStick(v,"))

    def test_子代理视图复用精确底部(self):
        self.assertIn("if (follow) hsSetScrollBottom(body)", fn("hsAgentOpen"))


class ReconcileBehavior(unittest.TestCase):
    def run_js(self, names, body):
        src = "\n".join(fn(name) for name in names)
        result = subprocess.run(["node", "-e", src + "\n" + body],
                                capture_output=True, text=True, timeout=15, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_异步对账不覆盖变化后的页面(self):
        got = self.run_js(["hsProcessShape", "hsShapeSame", "hsMessageShape",
                           "hsViewShape", "hsReconcileNow"], r"""
let rebuilds=0, retries=0, waiting=[];
function hsHidden(v){return !!v.hidden}
function hsRebuild(){rebuilds++}
function hsReconcile(){retries++}
function api(){return new Promise(resolve=>waiting.push(resolve))}
function view(){return {sid:'test',el:{isConnected:true},historyReady:true,turns:[]}}
const messages=[{info:{role:'assistant'},parts:[{type:'text',text:'fresh reply'}]}];
(async()=>{
  const rows={};
  const changes={pending:v=>v.pendRest=[{}],loading:v=>v.historyReady=false,
    hidden:v=>v.hidden=true,closed:v=>v.el.isConnected=false,paged:v=>v.paged=true,
    reloaded:v=>v.turns=[],finishedNextTurn:v=>v.turnGeneration=2,
    event:v=>v.lastEventAt=42,anchor:v=>v.anchorToken=2,busy:v=>v.busy=true,
    loadingEmptyView:v=>v.recToken++};
  for(const [key,change] of Object.entries(changes)){
    const v=view(), before=rebuilds;
    const pending=hsReconcileNow(v,{adopt:false});change(v);waiting.shift()(messages);
    rows[key]={result:await pending,rebuilds:rebuilds-before,owed:!!v.recOwed};
  }
  const v=view();
  const first=hsReconcileNow(v,{adopt:false}), second=hsReconcileNow(v,{adopt:false});
  const [old,latest]=waiting.splice(0);latest(messages);await second;old(messages);await first;
  rows.parallel=rebuilds;
  const partial=view();partial.pendRest=[{}];rows.partial=await hsReconcileNow(partial,{adopt:false});
  console.log(JSON.stringify(rows));
})().catch(e=>{console.error(e);process.exit(1)});
""")
        self.assertEqual(got.pop("parallel"), 1)
        self.assertFalse(got.pop("partial"))
        for name, row in got.items():
            with self.subTest(name=name):
                self.assertFalse(row["result"])
                self.assertEqual(row["rebuilds"], 0)
        self.assertTrue(got["busy"]["owed"])

    def test_相同字数不允许丢失独立回复(self):
        got = self.run_js(["hsProcessShape", "hsShapeSame", "hsMessageShape",
                           "hsViewShape", "hsReconcileNow"], r"""
function hsHidden(){return false}
function hsRebuild(){}
function hsReconcile(){}
let records=[];
async function api(){return records}
function turn(id,text){return {msgId:id,parts:new Map([['p',{type:'text',text}]])}}
function message(text){return {info:{role:'assistant'},parts:[{type:'text',text}]}}
async function check(turns,db){records=db.map(message);return hsReconcileNow(
  {sid:'test',el:{isConnected:true},historyReady:true,turns},{adopt:false})}
(async()=>{
 const a='A'.repeat(80),b='B'.repeat(80);
 console.log(JSON.stringify({
   sameLength:await check([turn('a',a),turn('b',b)],[a]),
   sameTextDifferentMessage:await check([turn('a',a),turn('b',a)],[a]),
   provenDuplicate:await check([turn('a',a),turn('a',a)],[a]),
   duplicatePlusMissing:await check([turn('a',a),turn('a',a),turn('b',b)],[a]),
   repairMissing:await check([turn('a',a)],[a,b])
 }));
})().catch(e=>{console.error(e);process.exit(1)});
""")
        self.assertEqual(got, dict(sameLength=False, sameTextDifferentMessage=False,
                                  provenDuplicate=True, duplicatePlusMissing=False,
                                  repairMissing=True))

    def test_历史加载完成后补做被延迟的对账(self):
        got = self.run_js(["hsReconcileAfterLoad"], r"""
let calls=0;function hsReconcile(){calls++}
const v={recAfterLoad:true,historyReady:false,pendRest:[{}]};
hsReconcileAfterLoad(v);v.historyReady=true;hsReconcileAfterLoad(v);
const early=calls;v.pendRest=null;hsReconcileAfterLoad(v);hsReconcileAfterLoad(v);
console.log(JSON.stringify({early,calls,pending:v.recAfterLoad}));
""")
        self.assertEqual(got, dict(early=0, calls=1, pending=False))
        for name in ("hsLoad", "hsLoadAnchor", "hsPourRest"):
            self.assertIn("hsReconcileAfterLoad(v)", fn(name))

    def test_自绘滚动条先解除贴底再记录拖动起点(self):
        path = os.path.join(_srcmap.ROOT, "frontend", "app-init.js")
        with open(path, encoding="utf-8") as source:
            init = source.read()
        start = init.index('t.addEventListener("mousedown"')
        end = init.index('t.addEventListener("mouseenter"', start)
        drag = init[start:end]
        self.assertLess(drag.index('new CustomEvent("desk-scroll-start")'),
                        drag.index("startTop = el.scrollTop"))
        self.assertIn('addEventListener("desk-scroll-start", () => hsUnstick(v))', fn("hsBuild"))


if __name__ == "__main__":
    unittest.main()
