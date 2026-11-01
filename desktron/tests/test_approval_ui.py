"""审批要能在任何一端点(2026-09-02 用户指出)。

审批状态与回合锚点都在后端 operations 表。待批同时放进发起它的会话过程与全局提醒，
决定后的过程卡原位变成回执；刷新、切引擎与跨端处理都不能让它消失。
"""
import os
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
sys.argv = ["test", os.path.join(ROOT, "frontend"), "0", "0", ROOT]

API = open(os.path.join(ROOT, "desktron", "lib", "app_control", "api.py"), encoding="utf-8").read()
JS = open(os.path.join(ROOT, "frontend", "app-approval.js"), encoding="utf-8").read()
DETAIL = open(os.path.join(ROOT, "frontend", "app-chat-detail.js"), encoding="utf-8").read()
RECONCILE = open(os.path.join(ROOT, "frontend", "app-chat-reconcile.js"), encoding="utf-8").read()
INIT = open(os.path.join(ROOT, "frontend", "app-init.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "frontend", "index.html"), encoding="utf-8").read()


class 后端(unittest.TestCase):
    def _rt(self):
        from app_control.runtime import AppControlRuntime
        return AppControlRuntime(os.path.join(tempfile.mkdtemp(), "t.sqlite3"))

    def test_待批可查且批准后流转(self):
        rt = self._rt()
        rt.offer("browser", "tab-1", "w1", 300, session_id="s1")
        lease = rt.claim("s1", "browser", "tab-1")
        op = rt.act(lease["id"], "s1", "click", {"node_id": "n1"}, "", "k1")
        self.assertEqual(op["status"], "waiting_user")
        pend = rt.pending_all()
        self.assertEqual(len(pend), 1)
        done = rt.approve(pend[0]["id"], True, "ui_user")
        self.assertEqual(done["status"], "queued")
        self.assertEqual(rt.pending_all(), [])
        self.assertEqual(rt.approval_history()[0]["approval_state"], "approved:ui_user")

    def test_审批保存稳定回合坐标(self):
        rt = self._rt()
        rt.offer("browser", "tab-1", "w1", 300, session_id="s1")
        lease = rt.claim("s1", "browser", "tab-1")
        op = rt.act(lease["id"], "s1", "click", {"node_id": "n1"}, "", "k1",
                    {"turn_id": "msg-a", "turn_seq": 7,
                     "after_part_id": "part-2", "after_part_index": 2})
        self.assertEqual((op["turn_id"], op["turn_seq"]), ("msg-a", 7))
        self.assertEqual((op["after_part_id"], op["after_part_index"]), ("part-2", 2))

    def test_旧待批在决定时补写回合坐标(self):
        rt = self._rt()
        rt.offer("browser", "tab-1", "w1", 300, session_id="s1")
        lease = rt.claim("s1", "browser", "tab-1")
        op = rt.act(lease["id"], "s1", "click", {"node_id": "n1"}, "", "k1")
        done = rt.approve(op["id"], True, "ui_user",
                          {"turn_id": "msg-old", "turn_seq": 3, "after_part_index": 1})
        self.assertEqual((done["turn_id"], done["turn_seq"]), ("msg-old", 3))
        self.assertEqual(rt.approval_history()[0]["after_part_index"], 1)

    def test_拒绝也终结不留挂起(self):
        """拒绝要让动作落地成失败,不能还留在待批里——那会一直弹。"""
        rt = self._rt()
        rt.offer("browser", "tab-1", "w1", 300, session_id="s1")
        lease = rt.claim("s1", "browser", "tab-1")
        op = rt.act(lease["id"], "s1", "click", {"node_id": "n1"}, "", "k1")
        rt.approve(op["id"], False, "ui_user")
        self.assertEqual(rt.pending_all(), [])

    def test_清空只影响信息卡标记_完整审批账本仍可恢复正文(self):
        rt = self._rt()
        rt.offer("browser", "tab-1", "w1", 300, session_id="s1")
        lease = rt.claim("s1", "browser", "tab-1")
        op = rt.act(lease["id"], "s1", "click", {"node_id": "n1"}, "", "clear-1")
        rt.approve(op["id"], False, "ui_user")
        self.assertEqual(rt.clear_approval_history()["cleared"], 1)
        history = rt.approval_history()
        self.assertEqual(history[0]["id"], op["id"])
        self.assertIsNotNone(history[0]["approval_cleared_at"])

    def test_不限单个_app(self):
        """原生那条只问自己那一个 app 且只取一条(它就在那台机器上,弹一个框够了)。
        界面这条要一次看全,否则多个待批时人只看得到第一个。"""
        from app_control.runtime import AppControlRuntime
        import inspect
        src = inspect.getsource(AppControlRuntime.pending_all)
        self.assertNotIn("app_id=?", src)
        self.assertIn("LIMIT ?", src)

    def test_端点走统一验票而非宿主令牌(self):
        """宿主令牌只有原生壳有。审批端点要给界面用,不能挂在 /host/ 下——
        那样手机永远调不到。"""
        # 只截审批分区：再往后就是第一个宿主专用端点，窗口一宽会误判。
        i = API.index('PREFIX + "/approvals"')
        j = API.index('PREFIX + "/host/desktop-capability"')
        self.assertLess(i, j)
        self.assertNotIn("_host(h)", API[i:j])
        self.assertIn('PREFIX + "/approvals/decide"', API[i:j])

    def test_决定端点仍复用统一状态机(self):
        """保留 host 兼容端点，但产品呈现只走会话卡片。"""
        self.assertEqual(API.count("rt.approve("), 2)   # /host/approve 与 /approvals/decide

    def test_记得下谁点的(self):
        self.assertIn('"ui_user"', API)
        self.assertIn('"native_user"', API)


class 前端(unittest.TestCase):
    """审批是过程时间线里的独立交互卡，不混进模型正文。"""

    ASK = open(os.path.join(ROOT, "frontend", "app-chat-overlays.js"), encoding="utf-8").read()

    def test_原生壳不再弹第二套审批(self):
        swift = open(os.path.join(ROOT, "desktop", "native", "main.swift"), encoding="utf-8").read()
        self.assertNotIn("promptAppControlApproval", swift)
        self.assertNotIn("host/pending-approval", swift)

    def test_两端都轮询(self):
        """不能只在本机轮询——手机端才是原来看不见的那一端。"""
        self.assertIn("apprPoll", INIT)
        at = INIT.index("apprPoll")
        head = INIT[max(0, at - 300):at]
        self.assertNotIn("isLocalView()", head, "轮询不该只挂在本机")

    def test_脚本已进会话包(self):
        """并进 /app-chat.js:它用的全是会话页的符号。不单开 script——首屏脚本请求数
        有预算。漏登记的话文件在、页面上却没有,最难查的一类。"""
        import re as _re
        src = open(os.path.join(ROOT, "desktron", "lib", "frontend_bundle.py"),
                   encoding="utf-8").read()
        m = _re.search(r'"/app-chat\.js": \(([^)]*)\)', src, _re.S)
        self.assertIsNotNone(m)
        self.assertIn("app-approval.js", m.group(1))
        self.assertNotIn('src="/app-approval.js"', HTML, "不该再单开一个 script")

    def test_过程审批用同一张卡的类名(self):
        """过程待批与回执复用权限卡形制；待批动作也沿用同一套按钮。"""
        for k in ("hs-card hs-ask", "hs-ico", "hs-card-t", "hs-t1", "hs-t2"):
            self.assertIn(k, JS, "少了 " + k)
            self.assertIn(k, self.ASK, k + " 不在 hsAsk 里,那就不是复刻")
        for k in ("hs-acts", "hs-lnk", "hs-pill"):
            self.assertIn(k, JS)
            self.assertIn(k, self.ASK)

    def test_不再自画一套样式(self):
        css = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
        self.assertNotIn(".appr-card", css)
        self.assertNotIn(".appr-back", css)

    def test_待批与回执都按锚点落在过程区(self):
        """过程卡由审批账本重建，待批可操作，决定后在原位变成回执。"""
        self.assertIn("hsProcPlace(t, el, at)", JS)
        self.assertIn("hsProcHost(t)", JS)
        self.assertIn("hsProcReveal(t)", JS)
        self.assertNotIn("auth-tui-back", JS)
        seg = JS[JS.index("function apprOpen(op)"):]
        seg = seg[:seg.index("\n}")]
        self.assertNotIn("if (_apprPending(op)) return;", seg)
        sync = JS[JS.index("function apprSync()"):JS.index("function _apprNode(op)")]
        self.assertIn("_apprNode(op);\n      apprOpen(op);", sync)
        self.assertIn('onclick = () => apprDecide(op.id, true)', seg)

    def test_按会话落位(self):
        """审批属于发起它的那段会话。找不到那一页就先不画,下一轮再来——
        画到别的会话里比不画更糟。"""
        self.assertIn("_hsViews.get(op.session_id", JS)
        self.assertIn("v.el.isConnected", JS)

    def test_审批按锚点与过程步骤混排且重复同步不搬节点(self):
        """两张连续审批应留在前后工具之间；五秒轮询不能反复移动已正确落位的卡。"""
        place = re.search(r"function hsProcPlace\(t, el, at\) \{[\s\S]+?\n\}", DETAIL).group(0)
        appr_place = re.search(r"function _apprPlace\(t, el, op\) \{[\s\S]+?\n\}", JS).group(0)
        script = r'''
const assert = require("node:assert/strict");
let moves = 0;
class N {
  constructor(name) { this.name=name; this.children=[]; this.parentNode=null; this.classList={add(){}}; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const a=this.parentNode.children, i=a.indexOf(this);
    return i >= 0 ? (a[i+1] || null) : null;
  }
  insertBefore(el, ref) {
    if (el.parentNode) el.parentNode.children=el.parentNode.children.filter(x=>x!==el);
    const i=ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(el); else this.children.splice(i,0,el);
    el.parentNode=this; moves++;
  }
}
const body=new N("body"), box=new N("box");
function hsProcHost(){ return body; }
function hsProcBox(){ return box; }
''' + place + appr_place + r'''
const p1={id:"p1",el:new N("p1")}, p2={id:"p2",el:new N("p2")};
const t={parts:new Map([[p1.id,p1],[p2.id,p2]])};
body.insertBefore(p1.el,null); body.insertBefore(p2.el,null);
const a=new N("a"), b=new N("b");
_apprPlace(t,a,{after_part_id:"p1"});
_apprPlace(t,b,{after_part_id:"p1"});
assert.deepEqual(body.children.map(x=>x.name), ["p1","a","b","p2"]);
const before=moves;
_apprPlace(t,a,{after_part_id:"p1"});
_apprPlace(t,b,{after_part_id:"p1"});
assert.equal(moves,before);
'''
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_刷新后从完整账本恢复而非只查待批(self):
        self.assertIn('_apprNative("list")', JS)
        self.assertIn('/workbench/app-control/approvals?include_resolved=1', JS)
        self.assertIn('"X-Desk-Approval-UI": "1"', JS)
        self.assertIn('_apprHasNative() ? _apprNative("list") : _apprBrowser("list")', JS)
        self.assertIn("r.approvals || r.pending", JS)
        self.assertIn("approval_history", open(os.path.join(
            ROOT, "desktron", "lib", "app_control", "runtime.py"), encoding="utf-8").read())

    def test_答复后就地变字(self):
        """全局保留最近结果，过程回执也不受自动退出定时器影响。"""
        self.assertIn('return "已允许"', JS)
        self.assertIn(".appr-state", JS)
        self.assertNotIn(".appr-decision", JS)
        sync = JS[JS.index("function apprSync()"):JS.index("function _apprNode(op)")]
        self.assertIn("if (!op.approval_cleared_at) _apprNode(op)", sync)
        self.assertNotIn("setTimeout", sync)
        card = JS[JS.index("function apprOpen(op)"):]
        self.assertNotIn("setTimeout", card[:card.index("function apprRenderView")])
        # hsAsk 也是这么收的
        self.assertIn('"已允许"', self.ASK)

    def test_清空只过滤信息卡_过程区仍遍历完整账本(self):
        sync = JS[JS.index("function apprSync()"):JS.index("function _apprNode(op)")]
        self.assertIn("!op.approval_cleared_at", sync)
        self.assertIn("apprOpen(op);", sync)
        clear = JS[JS.index("async function apprClearHistory()"):JS.index("function _apprNodePaint")]
        self.assertIn("会话过程中的审批记录会保留", clear)

    def test_对账替换会话后重挂审批卡(self):
        i = RECONCILE.index("function hsRebuild(")
        j = RECONCILE.index("\n}\n", i)
        self.assertIn("apprRenderView(v)", RECONCILE[i:j])

    def test_允许后改用解锁图标(self):
        icons = open(os.path.join(ROOT, "frontend", "app-chat-tools.js"), encoding="utf-8").read()
        self.assertIn("unlock:", icons)
        self.assertIn("approved ? HS_ICONS.unlock : HS_ICONS.lock", JS)
        self.assertIn('if (allow) el.querySelector(".hs-ico").innerHTML = HS_ICONS.unlock', self.ASK)

    def test_提交失败要说出来(self):
        """静默留着「已允许」的话,人以为点过了,而动作还挂着。"""
        i = JS.index("catch (e)", JS.index("async function apprDecide"))
        tail = JS[i:i + 760]
        self.assertIn("Object.assign(op, before)", tail)
        self.assertIn("提交失败", tail)

    def test_页面重建后旧_dom_引用不会阻止重画(self):
        self.assertIn("!el.isConnected", JS)
        self.assertIn("_apprCards.delete(id)", JS)

    def test_按回合双锚点与块序号落位(self):
        for key in ("turn_id", "turn_seq", "after_part_id", "after_part_index"):
            self.assertIn(key, JS)
        self.assertIn("{ anchor }", JS)   # 决定时只在那一页开着才带锚点,补旧记录的缺

    @unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
    def test_Codex历史消息换id后按动作时间恢复所属回合(self):
        i = JS.index("function _apprTurn(")
        turn = JS[i:JS.index("\n}\n", i) + 3]
        script = turn + """
const turns = [
  {msgId:'new-a1', seq:0, uat:1000},
  {msgId:'new-a2', seq:0, uat:3000},
  {msgId:'new-a3', seq:0, uat:5000},
];
const byTime = _apprTurn({turns}, {turn_id:'old-live-id', turn_seq:2222, created_at:4});
const notLoaded = _apprTurn({turns:turns.slice(1)}, {turn_id:'old-live-id', created_at:2});
const exact = _apprTurn({turns}, {turn_id:'new-a1', created_at:9});
console.log(JSON.stringify([byTime && byTime.msgId, notLoaded, exact && exact.msgId]));
"""
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), ["new-a2", None, "new-a1"])

    @unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
    def test_并发轮询复用同一请求(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
let calls=0,release;
const ctx=vm.createContext({window:{},setTimeout(){},
  async api(){calls++;await new Promise(resolve=>{release=resolve});return {approvals:[]};}});
vm.runInContext(fs.readFileSync('frontend/app-approval.js','utf8'),ctx);
ctx.apprSync=()=>{};ctx.apprRehost=()=>{};
(async()=>{
  const p1=ctx.apprPoll(),p2=ctx.apprPoll();
  assert.equal(calls,1);
  release();
  await Promise.all([p1,p2]);
  assert.equal(calls,1);
})().catch(e=>{console.error(e);process.exit(1)});
'''
        out = subprocess.run(["node", "-e", script], cwd=ROOT,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_说清同意的是什么(self):
        self.assertIn("_APPR_WHAT", JS)
        self.assertIn("o.input && o.input.url", JS)


if __name__ == "__main__":
    unittest.main(argv=["test_approval_ui"])
