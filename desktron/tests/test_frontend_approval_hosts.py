"""待审批全局提醒住在两个宿主之一，同时在发起它的回合正文保留上下文卡片。

人会在会话与引擎之间切换,卡片画在发起它的那一页里就常常没人看见,飞书通知只是补一条。
宽屏且右侧信息卡真实可见时进信息卡首区(多项折叠计数、只展开一项);其余情况进右上角
常驻通知。两个提醒宿主不会同时出现；正文是第二入口，
但三处共用账本，决定只有 apprDecide 一条路。
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
JS = open(os.path.join(ROOT, "frontend", "app-approval.js"), encoding="utf-8").read()
SIT = open(os.path.join(ROOT, "frontend", "app-chat-situation.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


def fn(name):
    i = JS.index("function " + name + "(")
    return JS[i:JS.index("\n}\n", i) + 3]


class 宿主选择(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
    def test_用途名称优先取侧栏会话清单(self):
        script = """
const pages = [{url: "chat:adp_760f", title: "对话"}];
function _termListedTitle(sid) { return sid === "adp_760f" ? "Desk Feature" : ""; }
""" + fn("_apprSessionName") + """
if (_apprSessionName("adp_760f") !== "Desk Feature") process.exit(1);
"""
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_业务意图作为主标题并补充范围用途(self):
        title = fn("_apprTitle")
        context = fn("_apprContext")
        self.assertIn('op.intent', title)
        self.assertIn('intent || _apprDesc(op)', title)
        self.assertIn('op.page_title', context)
        self.assertIn('_apprSessionName(op && op.session_id)', context)
        self.assertIn('"范围：" + scope', context)
        self.assertIn('"用途：" + purpose', context)
        self.assertNotIn('page_url', context)
        self.assertNotIn('_apprOrigin', context)
        self.assertIn('_apprTitle(op)', fn("_apprNodePaint"))
        self.assertIn('_apprContext(op)', fn("_apprNodePaint"))
        self.assertIn('_apprTitle(op)', fn("_apprPaint"))

    def test_按信息卡是否真实可见而不是猜窗口宽度(self):
        seg = fn("_apprCardHost")
        self.assertIn("sit.offsetParent === null", seg)
        self.assertIn("getBoundingClientRect().width < 280", seg)
        self.assertNotIn("innerWidth", seg)
        self.assertNotIn("matchMedia", seg)

    @unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
    def test_可见且够宽才用信息卡(self):
        js = """
const cases = [];
function sit(visible, width, host) {
  return { offsetParent: visible ? {} : null,
           getBoundingClientRect: () => ({ width }),
           querySelector: (s) => s === ".hs-sit-approval-host" ? host : null };
}
function view(s) { return { el: { isConnected: true, querySelector: (q) => q === ".hs-sit" ? s : null } }; }
let _hsViews;
""" + fn("_apprCardHost") + """
_hsViews = new Map([["a", view(sit(true, 300, "HOST-A"))]]);
cases.push(_apprCardHost());
_hsViews = new Map([["a", view(sit(false, 300, "HOST-A"))]]);
cases.push(_apprCardHost());
_hsViews = new Map([["a", view(sit(true, 279, "HOST-A"))]]);
cases.push(_apprCardHost());
_hsViews = new Map([["a", view(null)], ["b", view(sit(true, 320, "HOST-B"))]]);
cases.push(_apprCardHost());
_hsViews = new Map();
cases.push(_apprCardHost());
console.log(JSON.stringify(cases));
"""
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), ["HOST-A", None, None, "HOST-B", None])

    def test_小版面关闭信息栏时审批仍走通知(self):
        """小版面保留信息栏节点但默认 display:none；关闭时宿主选择仍自然落到通知。"""
        seg = SIT[SIT.index("function hsSituationMarkup"):]
        self.assertNotIn("if (!hsSituationEnabled()) return '';", seg[:500])
        self.assertIn('class="hs-sit-scrim"', seg[:500])
        self.assertIn(".hs-page.hs-sit-open:not(.rv-on) .hs-sit", CSS)


class 迁移与互斥(unittest.TestCase):
    def test_同一节点在两个宿主间迁移不复制(self):
        seg = fn("_apprRehostNow")
        self.assertIn("if (el.parentNode !== host) host.appendChild(el)", seg)
        self.assertNotIn("cloneNode", JS)
        # 决定事件只在建节点时绑一次
        node = fn("_apprNode")
        self.assertIn("if (!el) {", node)
        self.assertIn('querySelector(".appr-allow").onclick', node)

    def test_信息卡承载时通知不显示(self):
        seg = fn("_apprRehostNow")
        self.assertIn("const host = card;", seg)
        self.assertIn("if (card || !pendingIds.length) {", seg)

    def test_信息卡保留未清空历史_通知只承载待办(self):
        sync = fn("apprSync")
        self.assertNotIn("const recent =", sync)
        self.assertIn("!op.approval_cleared_at", sync)
        self.assertIn("if (!op.approval_cleared_at) _apprNode(op)", sync)
        host = fn("_apprRehostNow")
        self.assertIn("const hostedIds = card ? ids : []", host)
        self.assertIn('pendingIds.length === ids.length ? "sit.approvals.pending" : "sit.approvals.recent"', host)
        self.assertIn("count.textContent = String(ids.length)", host)
        self.assertIn("count.title = `待处理 ${pendingIds.length}，已结束 ${ids.length - pendingIds.length}`", host)
        self.assertIn("host.insertBefore(el, host.children[index] || null)", host)

    @unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
    def test_多条历史和待办共存_清空不影响正文(self):
        script = fn("_apprPending") + fn("apprSync") + fn("_apprSortedIds") + """
const assert = require('node:assert/strict');
const _apprItems = new Map(), _apprNodes = new Map(), _apprSeen = new Set();
let _apprOpenId = null, _apprNoticeMin = false;
const document = {querySelectorAll: () => []};
const transcript = new Set();
function apprOpen(op) { transcript.add(op.id); }
function apprRehost() {}
function _apprNode(op) { if (!_apprNodes.has(op.id)) _apprNodes.set(op.id, {remove() {}}); }
function add(id, status, approval_state, at, cleared = null) {
  _apprItems.set(id, {id, status, approval_state, created_at: at,
    updated_at: at, approval_cleared_at: cleared});
}
for (let i = 1; i <= 6; i++) add('history-' + i, 'succeeded', 'approved:ui_user', i);
add('cleared', 'rejected', 'rejected:ui_user', 7, 8);
apprSync();
assert.equal(_apprNodes.size, 6);
assert.equal(transcript.size, 7);
add('pending-new', 'waiting_user', 'pending', 10);
add('pending-old', 'waiting_user', 'pending', 9);
apprSync();
assert.deepEqual(_apprSortedIds(), ['pending-old', 'pending-new',
  'history-6', 'history-5', 'history-4', 'history-3', 'history-2', 'history-1']);
const done = _apprItems.get('pending-old');
Object.assign(done, {status: 'queued', approval_state: 'approved:ui_user', updated_at: 11});
apprSync();
assert.deepEqual(_apprSortedIds().slice(0, 3), ['pending-new', 'pending-old', 'history-6']);
for (const op of _apprItems.values()) if (!_apprPending(op)) op.approval_cleared_at = 12;
apprSync();
assert.deepEqual(_apprSortedIds(), ['pending-new']);
assert.equal(transcript.size, 9);
apprSync(); // 重复轮询不恢复已清空历史
assert.equal(_apprNodes.size, 1);
"""
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_信息卡只展开一项_通知逐项显示(self):
        seg = fn("_apprRehostNow")
        self.assertIn("const open = card ? id === _apprOpenId : true;", seg)
        self.assertIn('el.classList.toggle("cur", id === _apprOpenId)', seg)
        self.assertIn("const selectableIds = card ? hostedIds : pendingIds", seg)
        self.assertIn("_apprPaint(noticeCard, op)", seg)

    def test_条目可以收起且巡检不会强制重开(self):
        node = fn("_apprNode")
        self.assertIn('_apprOpenId === op.id ? "" : op.id', node)
        host = fn("_apprRehostNow")
        self.assertIn("_apprOpenId === null", host)
        self.assertNotIn("if (!_apprOpenId ||", host)

    def test_下一帧迁移并跟随布局变化(self):
        self.assertIn("requestAnimationFrame(() => { _apprRaf = 0; _apprRehostNow(); })", JS)
        watch = fn("_apprWatch")
        self.assertIn("ResizeObserver", watch)
        self.assertIn('addEventListener("resize"', watch)
        # 没有待批时停表,不白跑
        self.assertIn("clearInterval(_apprTick)", watch)

    def test_通知显隐会同步原生浏览器避让且相同矩形不重发(self):
        sync = fn("_apprSyncNativeSurface")
        self.assertIn("_apprNativeSig", sync)
        self.assertIn('typeof browserSyncNative === "function"', sync)
        self.assertIn("requestAnimationFrame", sync)
        self.assertIn("_apprSyncNativeSurface(notice)", fn("_apprRehostNow"))


class 通知行为(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
    def test_历史默认折叠_轮询保留手动选择_待办结束再折叠(self):
        script = fn("_apprSectionState") + """
const assert = require('node:assert/strict');
let collapsed = false, expanded = '';
const sec = {
  classList: {toggle: (name, value) => { collapsed = value; }},
  querySelector: () => ({setAttribute: (name, value) => { expanded = value; }})
};
_apprSectionState(sec, false);
assert.equal(collapsed, true);
assert.equal(expanded, 'false');
collapsed = false; expanded = 'true'; // 用户展开历史
_apprSectionState(sec, false);
assert.equal(collapsed, false);
assert.equal(expanded, 'true');
_apprSectionState(sec, true); // 新待审批到达
assert.equal(collapsed, false);
collapsed = true; expanded = 'false'; // 用户收起待办
_apprSectionState(sec, true);
assert.equal(collapsed, true);
_apprSectionState(sec, false); // 最后一张处理完
assert.equal(collapsed, true);
assert.equal(expanded, 'false');
_apprSectionState(sec, true);
assert.equal(collapsed, false);
assert.equal(expanded, 'true');
"""
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("_apprSectionState(sec, pendingIds.length > 0)", fn("_apprRehostNow"))

    def test_通知只保留审批卡片且长内容内部滚动(self):
        full = re.search(r"\.appr-notice \{([^}]*)\}", CSS).group(1)
        self.assertIn("background: transparent", full)
        self.assertNotIn("backdrop-filter", full)
        self.assertIn("max-height: calc(100dvh - var(--appr-top)", full)
        body = re.search(r"\.appr-notice-body \{([^}]*)\}", CSS).group(1)
        self.assertIn("overflow-y: auto", body)
        animation = CSS[CSS.index("@keyframes appr-notice-in"):].split("@media", 1)[0]
        self.assertNotIn("opacity", animation)
        self.assertNotIn(".appr-notice-min", CSS)

    @unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
    def test_通知位于可见标题栏下方并忽略隐藏栏(self):
        script = fn("_apprPositionNotice") + """
let heads = [];
const document = {querySelectorAll: () => heads};
const window = {innerHeight: 800};
const getComputedStyle = h => ({visibility: h.visibility || 'visible'});
const values = [];
const notice = {style: {setProperty: (key, value) => values.push([key, value])}};
function header(bottom, extra = {}) {
  return {getClientRects: () => [1], getBoundingClientRect: () =>
    ({top: 0, bottom, width: 390, height: bottom}), ...extra};
}
heads = [header(38), header(44)]; // 手机导航与标题栏
_apprPositionNotice(notice);
heads = [header(38), header(58), header(200, {visibility: 'hidden'}),
  header(222, {getClientRects: () => []})]; // 桌面较高顶栏、隐藏页面
_apprPositionNotice(notice);
heads = [header(85)]; // 包含安全区的顶栏
_apprPositionNotice(notice);
console.log(JSON.stringify(values));
"""
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [["--appr-title-bottom", v] for v in ["44px", "58px", "85px"]])
        host = fn("_apprRehostNow")
        self.assertLess(host.index("_apprPositionNotice(notice)"), host.index("_apprSyncNativeSurface(notice)"))
        self.assertIn("calc(var(--appr-title-bottom, 0px) + 10px)", CSS)

    def test_待审批持续显示且新审批成为当前项(self):
        host = fn("_apprNoticeHost")
        self.assertNotIn("appr-notice-close", host)
        self.assertNotIn("appr-notice-min", host)
        sync = fn("apprSync")
        self.assertIn("_apprSeen.add(op.id);", sync)
        self.assertIn("_apprOpenId = op.id;", sync)
        self.assertIn('sec.classList.remove("collapsed")', sync)

    def test_通知按钮仍走唯一决定路径(self):
        host = fn("_apprNoticeHost")
        self.assertIn("apprDecide(card.dataset.op, false)", host)
        self.assertIn("apprDecide(card.dataset.op, true)", host)
        self.assertNotIn('addEventListener("keydown"', host)

    def test_全部处理后显示回执再退出(self):
        seg = fn("_apprRehostNow")
        self.assertIn("_apprHadPending && !pendingIds.length", seg)
        self.assertIn("}, 1600);", seg)

    def test_通知层级与程序坞让位(self):
        m = re.search(r"\.appr-notice-host \{([^}]*)\}", CSS)
        self.assertIsNotNone(m)
        self.assertIn("z-index: 46", m.group(1))
        self.assertIn("position: fixed", m.group(1))
        self.assertIn("width: min(340px", m.group(1))
        self.assertIn("body.dock-open .appr-notice-host { right: 92px; }", CSS)
        self.assertIn("body.dock-open .hs-sit { right: 92px; }", CSS)

    def test_窄屏贴安全区(self):
        i = CSS.index("@media (max-width: 820px), (hover: none) and (pointer: coarse) {\n  .appr-notice-host")
        seg = CSS[i:i + 400]
        self.assertIn("env(safe-area-inset-top)", seg)
        self.assertIn("right: 20px", seg)
        self.assertIn("width: calc(100vw - 40px)", seg)


class 决定只有一条路(unittest.TestCase):
    def test_决定接口只在apprDecide里调(self):
        self.assertEqual(JS.count("/workbench/app-control/approvals/decide"), 1)
        self.assertIn("/workbench/app-control/approvals/decide", fn("apprDecide"))

    def test_防重与失败回滚(self):
        seg = fn("apprDecide")
        self.assertIn("if (_apprReplying.has(id)) return;", seg)
        self.assertIn("Object.assign(op, before)", seg)
        self.assertIn("提交失败", seg)

    def test_那段会话没开着也能决定(self):
        """锚点后端入队时已记下;这里只在那一页开着时把当下坐标带上,补旧记录的缺。"""
        seg = fn("apprDecide")
        self.assertIn("anchor ? { anchor } : {}", seg)
        self.assertIn("v.el.isConnected ? _apprTurn(v, op) : null", seg)

    def test_条目说清要执行什么且详情不限定为页面(self):
        self.assertIn("op.page_title", JS)
        self.assertIn("_apprInputText(op)", fn("_apprNodePaint"))
        self.assertNotIn("appr-page", fn("_apprNode"))
        for k in ("coordinate_click", "type", "hover", "back", "close"):
            self.assertIn(k + ":", JS, "_APPR_WHAT 少了 " + k)


class 信息卡分区(unittest.TestCase):
    def test_条目标题与图标中线对齐_详情回到图标左沿(self):
        summary = re.search(r"\.appr-sum \{([^}]*)\}", CSS).group(1)
        self.assertIn("grid-template-columns: 38px minmax(0,1fr)", summary)
        self.assertIn("grid-template-rows: 19px auto", summary)
        self.assertIn('.appr-meta { display: contents; }', CSS)
        actions = re.search(r"\.appr-meta-line \{([^}]*)\}", CSS).group(1)
        self.assertIn('flex: 0 0 auto', actions)
        self.assertIn("align-items: center", summary)
        title = re.search(r"\.appr-copy b \{([^}]*)\}", CSS).group(1)
        self.assertIn("min-height: 19px", title)
        self.assertIn("display: block", title)
        self.assertIn("text-overflow: ellipsis", title)
        self.assertIn("min-width: 0", title)
        subtitle = re.search(r"\.appr-copy small \{([^}]*)\}", CSS).group(1)
        self.assertIn("display: block", subtitle)
        self.assertIn("text-overflow: ellipsis", subtitle)
        self.assertIn('class="appr-heading"', fn("_apprNode"))
        self.assertIn('class="appr-secondary"', fn("_apprNode"))
        self.assertIn('.appr-secondary { grid-row: 2; gap: 14px; }', CSS)
        glyph = re.search(r"\.appr-glyph \{([^}]*)\}", CSS).group(1)
        self.assertIn("width: 38px", glyph)
        self.assertIn("height: 38px", glyph)
        detail = re.search(r"\.appr-detail \{([^}]*)\}", CSS).group(1)
        self.assertIn("padding: 0 10px 10px 9px", detail)

    def test_箭头与时间在首行_展开内容只在悬停时统一高亮(self):
        toggle = re.search(r"\.appr-toggle \{([^}]*)\}", CSS).group(1)
        self.assertIn(".appr-heading { grid-row: 1; gap: 9px; }", CSS)
        self.assertIn("height: 19px", toggle)
        self.assertIn(".appr-item.open:hover { background: var(--surface-hover); }", CSS)
        self.assertIn(".appr-item.open .appr-sum:hover { background: transparent; }", CSS)
        detail = re.search(r"\.appr-what \{([^}]*)\}", CSS).group(1)
        self.assertIn("font-size: 11.5px", detail)

    def test_状态和操作都在摘要右侧_详情只铺操作内容(self):
        node = fn("_apprNode")
        self.assertIn('class="appr-meta"', node)
        self.assertIn('class="appr-state"', node)
        self.assertLess(node.index('class="appr-actions"'), node.index('class="appr-detail"'))
        self.assertNotIn("appr-page", node)
        self.assertNotIn("appr-decision", node)
        self.assertIn('<div class="appr-detail"><div class="appr-what"></div></div>', node)

    def test_批准结果使用解锁图标(self):
        paint = fn("_apprNodePaint")
        self.assertIn('startsWith("approved:")', paint)
        self.assertIn("approved ? glyphs.unlock : glyphs.lock", paint)

    def test_通知与正文共用同一套审批卡结构(self):
        host = fn("_apprNoticeHost")
        card = fn("apprOpen")
        self.assertNotIn("appr-notice-kicker", host)
        self.assertNotIn("需要你的确认</div>", host)
        self.assertNotIn("Desktron", host)
        self.assertNotIn("浏览器 App Control", host)
        self.assertNotIn("<header>", host)
        self.assertIn('class="appr-notice-card hs-card hs-ask hs-approval"', host)
        self.assertIn("_apprApprovalInner()", host)
        self.assertIn("_apprApprovalInner()", card)
        self.assertNotIn("appr-notice-close", host)
        inner = fn("_apprApprovalInner")
        for cls in ("hs-ico", "hs-card-t", "hs-t1", "hs-t2", "hs-acts", "hs-lnk", "hs-pill"):
            self.assertIn(cls, inner)

    def test_允许拒绝沿用正文卡按钮形制(self):
        allow = re.search(r"\.appr-actions \.appr-allow \{([^}]*)\}", CSS).group(1)
        self.assertIn("border: 1px solid var(--ink-35)", allow)
        self.assertIn("border-radius: 999px", allow)
        self.assertIn("background: var(--ink-23)", allow)
        reject = re.search(r"\.appr-actions button \{([^}]*)\}", CSS).group(1)
        self.assertIn("background: transparent", reject)

    def test_分区在首位且空态也保留(self):
        seg = SIT[SIT.index("<aside class=\"hs-sit\""):]
        self.assertLess(seg.index("hs-sit-approvals"), seg.index("hs-sit-projects"))
        self.assertIn('hs-sit-approvals collapsed">', seg)
        self.assertNotIn('hs-sit-approvals" hidden', seg)
        self.assertIn('hs-sit-approval-count" aria-live="polite"', seg)
        approval = seg[:seg.index("hs-sit-projects")]
        self.assertIn("appr-chev", approval)
        self.assertIn('aria-expanded="false"', approval)
        self.assertIn('hs-sit-approvals collapsed', approval)

    def test_审批与项目同级且标题不带图标(self):
        seg = SIT[SIT.index("<aside class=\"hs-sit\""):]
        approval = seg[:seg.index("hs-sit-projects")]
        self.assertNotIn("HS_ICONS.lock", approval)
        self.assertNotIn("hs-sit-context-label", seg)
        # 锁只属于具体审批条目和审批卡，不再给信息卡分区标题增加视觉权重。
        self.assertIn("HS_ICONS.lock", fn("_apprApprovalInner"))
        self.assertNotIn("appr-dot", SIT); self.assertNotIn("appr-dot", JS); self.assertNotIn("appr-dot", CSS)
        self.assertNotIn('querySelector(".hs-sit-context-label")', fn("_apprRehostNow"))
        self.assertNotIn(".hs-sit-context-label", CSS)

    def test_数量在胶囊内真实居中(self):
        # 这枚胶囊自 2026-09-10 起与制品那枚共用一条规则(同一张卡上同一种计数)
        rule = re.search(r"\.hs-sit-approval-count, \.hs-sit-artifact-count \{([^}]*)\}",
                         CSS).group(1)
        self.assertIn("display: inline-flex", rule)
        self.assertIn("align-items: center", rule)
        self.assertIn("justify-content: center", rule)
        self.assertIn("line-height: 1", rule)

    def test_清空操作与审批标题同字阶(self):
        heading = re.search(r"\.hs-sit h2, \.hs-sit-change-title \{([^}]*)\}", CSS).group(1)
        clear = re.search(r"\.hs-sit-approval-clear \{([^}]*)\}", CSS).group(1)
        self.assertIn("font-size: 14.5px", heading)
        self.assertIn("font-size: 14.5px", clear)
        self.assertIn("font-weight: 350", clear)

    def test_数量统计当前条目且标题可以整体折叠(self):
        seg = fn("_apprRehostNow")
        self.assertIn("const mine = !!card && sec.contains(card);", seg)
        self.assertNotIn("sec.contains(card) && ids.length > 0", seg)
        self.assertIn("count.textContent = String(ids.length)", seg)
        self.assertIn("count.hidden = !ids.length", seg)
        self.assertIn('sec.classList.toggle("collapsed")', seg)
        # 收起要挡住标题行以下的全部内容:列表容器,以及 app-approval.js 补挂的状态行
        # (「暂无审批记录」/「正在加载审批…」)。两者是兄弟节点,规则里必须都在。
        self.assertIn(".hs-sit-approvals.collapsed .hs-sit-approval-host,\n"
                      ".hs-sit-approvals.collapsed .appr-load-state { display: none; }", CSS)

    def test_大量审批限制在信息卡内部滚动_通知逐项轮换(self):
        host = re.search(r"\.hs-sit-approval-host:not\(:empty\) \{([^}]*)\}", CSS).group(1)
        self.assertIn("max-height: min(360px,45vh)", host)
        self.assertIn("overflow-y: auto", host)
        self.assertIn("overscroll-behavior: contain", host)
        self.assertIn("width: calc(100% + 18px)", host)
        self.assertIn("margin-right: -18px", host)
        # 原生条要关掉:系统设成「总是显示滚动条」时它占 15px 内容宽,卡片右缘会比
        # 标题行的箭头短一截。车道交给全局自绘滑块,见 test_frontend_approval_gutter。
        self.assertIn("scrollbar-width: none", host)
        self.assertNotIn("scrollbar-gutter", host)
        self.assertIn("padding: 9px 18px 0 0", host)
        self.assertIn("box-sizing: border-box", host)
        self.assertIn("const open = card ? id === _apprOpenId : true;", fn("_apprRehostNow"))
        self.assertIn("appr-notice-next", fn("_apprNoticeHost"))

    def test_最近审批支持仅清空信息卡(self):
        approval = SIT[SIT.index("hs-sit-approvals"):SIT.index("hs-sit-projects")]
        self.assertIn("hs-sit-approval-clear", approval)
        clear = fn("apprClearHistory")
        self.assertIn('_apprNative("clear"', clear)
        self.assertNotIn("/workbench/app-control/approvals/clear", clear)
        self.assertIn("会话过程中的审批记录会保留", clear)

    def test_详情不重复会话名(self):
        node = fn("_apprNode")
        self.assertNotIn("appr-sess", node)
        self.assertNotIn('>会话<', node)


if __name__ == "__main__":
    unittest.main()
