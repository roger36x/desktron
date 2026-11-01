"""手机上那两件事:切会话要点两下,以及长按没有菜单(2026-08-24 用户实见)。

后续:那次修复顺手让触屏常显 ×(理由是"触屏没有别的路子关会话"),但同一次补的
bindLongPressMenu 已经把长按桥到了 oncontextmenu,菜单里就有关闭与删除——理由不成立,
而一列常驻的 × 占地方又容易误触。触屏因此回到"不显示",两条断言随之调头。

两件都不是「移动端没做适配」,而是两处只对鼠标成立的假设:

  一、`.term-item:hover .ti-x { display: block }`。iOS 的第一次轻触先落成 :hover,
      × 从无到有把整行重排,触点下的元素跟着变,Safari 于是取消这一次 click。
      第二下因为 hover 已经在了、不再重排,才真的点进去。

  二、桌面十几处菜单都挂在 oncontextmenu 上,而手机上这个事件根本不发。
      于是改名、移动工作区、关闭会话在手机上一个入口都没有。
"""
import json
import os
import re
import shutil
import subprocess
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
EVENTS = open(os.path.join(ROOT, "frontend", "app-events.js"), encoding="utf-8").read()


def fn(src, name):
    m = re.search(r"\nfunction %s\(.*?\n}\n" % re.escape(name), src, re.S)
    assert m, name + " 不见了"
    return m.group(0)


class HoverRevealCostsATap(unittest.TestCase):
    """悬停才现的关闭钮,在触屏上要吃掉第一次点击。"""

    def _block(self, sel):
        """取这条选择器所在的 @media 块名(没有则空串)。"""
        at = CSS.index(sel)
        head = CSS[:at]
        # 往前找最近的 @media,若它已经闭合则不算数
        i = head.rfind("@media")
        if i < 0:
            return ""
        depth = head[i:].count("{") - head[i:].count("}")
        return head[i:head.index("{", i)].strip() if depth > 0 else ""

    def test_会话行不再提供快捷关闭按钮(self):
        self.assertNotIn(".term-item:hover .ti-x", CSS)
        panel = _srcmap.panel_src()
        self.assertNotIn('<span class="ti-x"', panel)
        self.assertNotIn('r.querySelector(".ti-x").onclick', panel)

    def test_触屏上关闭钮不常显(self):
        """旧脚本残留的关闭按钮在所有设备上都隐藏。

        这条断言原来是反的——那次修复顺手让触屏常显 ×,理由写的是「那边没有别的路子
        关会话」。可同一次已经补上了 bindLongPressMenu,会话行长按就能开右键菜单,
        里面有「关闭进程」与「删除会话」,那个理由当时就不成立。一列常驻的 × 既占地方
        也容易误触(2026-08-24 用户实见),所以触屏回到不显示。"""
        m = re.search(r"^\.term-item \.ti-x \{([^}]*)\}", CSS, re.M)
        self.assertIsNotNone(m, "基线规则不见了")
        self.assertIn("display: none", m.group(1), "基线没藏,触屏上又会常显一列 ×")

    def test_触屏仍有关会话的入口(self):
        """藏起 × 的前提是长按能开菜单,且菜单里真有关闭那两项。少了任一条,
        触屏上就没法关会话了。"""
        self.assertIn("bindLongPressMenu();", EVENTS)
        panel = _srcmap.panel_src()
        seg = re.search(r"function openTermCtxMenu\([\s\S]+?\n}", panel).group(0)
        self.assertIn('label: DeskI18n.t("menu.closeProcess")', seg)
        self.assertIn('label: DeskI18n.t("menu.deleteConversation")', seg)
        self.assertIn("r.oncontextmenu =", panel)

    def test_另一处同样的写法也收了(self):
        """.pv-sub 那处是同一个手法,留着就是同一个坑。"""
        self.assertIn("hover: hover", self._block(".pv-sub:hover .ti-x"))

    def test_引擎菜单未选项不在触屏粘住悬停底色(self):
        """当前项由 .on 与圆点表示；未选项的 hover 只能属于真实鼠标。"""
        selector = "#engine-menu .fm-row:not(.on):hover"
        self.assertIn("hover: hover", self._block(selector))
        self.assertIn("pointer: fine", self._block(selector))
        self.assertIn("#engine-menu .fm-row:not(.on):active", CSS)


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class LongPressBridgesContextMenu(unittest.TestCase):
    """长按 = 右键。谁挂了 oncontextmenu 谁就自动有,不必逐处登记。"""

    HARNESS = r"""
const LISTENERS = {};
let _t = 0; const TIMERS = new Map();
globalThis.setTimeout = (fn, ms) => { const id = ++_t; TIMERS.set(id, {fn, ms}); return id; };
globalThis.clearTimeout = (id) => TIMERS.delete(id);
function runTimers(){ for (const [id, t] of [...TIMERS]) { TIMERS.delete(id); t.fn(); } }
class MouseEvent {
  constructor(type, o){ Object.assign(this, {type}, o||{}); this.defaultPrevented=false; this.stopped=false; }
  preventDefault(){ this.defaultPrevented = true; }
  stopPropagation(){ this.stopped = true; }
}
globalThis.MouseEvent = MouseEvent;
const document = {
  addEventListener(type, fn){ (LISTENERS[type] = LISTENERS[type] || []).push(fn); },
};
// window 与 document 共用一份监听表:被测代码往两处都挂(blur 挂在 window 上——
// 切走应用时长按要作废,而 document 收不到 blur)。桩里只造 document 的话,
// 被测代码一执行到那一行就 ReferenceError,十条用例一起红。
const window = {
  addEventListener(type, fn){ (LISTENERS[type] = LISTENERS[type] || []).push(fn); },
};
function fire(type, ev){
  ev = ev || {};
  ev.stopImmediatePropagation = ev.stopImmediatePropagation || function(){ this.immediateStopped = true; };
  for (const fn of (LISTENERS[type] || [])) { fn(ev); if (ev.immediateStopped) break; }
}
function mkNode(handler, parent){
  return { oncontextmenu: handler || null, parentNode: parent || document, isConnected: true };
}
SRC
BODY
"""

    def _run(self, body):
        src = (fn(EVENTS, "_ctxHostOf") + fn(EVENTS, "bindLongPressMenu")
               + re.search(r"const LONG_PRESS_MS = \d+;", EVENTS).group(0)
               + re.search(r"const LONG_PRESS_SLOP = \d+;", EVENTS).group(0))
        js = self.HARNESS.replace("SRC", src).replace("BODY", body)
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_按住不动就弹出那一处的右键菜单(self):
        got = self._run("""
bindLongPressMenu();
let got = null;
const row = mkNode((ev) => { got = {x: ev.clientX, y: ev.clientY, type: ev.type}; });
fire("touchstart", { touches: [{clientX: 30, clientY: 44}], target: row });
runTimers();
console.log(JSON.stringify(got));
""")
        self.assertEqual(got, {"x": 30, "y": 44, "type": "contextmenu"},
                         "没把坐标递给菜单")

    def test_菜单是从祖先上找的(self):
        """行内的 span 被按住时,菜单挂在行上。逐处补触摸处理就得每处都想到这一层。"""
        got = self._run("""
bindLongPressMenu();
let hit = 0;
const row = mkNode(() => { hit++; });
const label = mkNode(null, row);
fire("touchstart", { touches: [{clientX: 1, clientY: 2}], target: label });
runTimers();
console.log(JSON.stringify(hit));
""")
        self.assertEqual(got, 1)

    def test_在滚就不算长按(self):
        got = self._run("""
bindLongPressMenu();
let hit = 0;
const row = mkNode(() => { hit++; });
fire("touchstart", { touches: [{clientX: 10, clientY: 10}], target: row });
fire("touchmove", { touches: [{clientX: 10, clientY: 60}] });
runTimers();
console.log(JSON.stringify(hit));
""")
        self.assertEqual(got, 0, "手指划走了还弹菜单")

    def test_轻微抖动仍算长按(self):
        """按住时手不可能完全不动,容差之内不该取消。"""
        got = self._run("""
bindLongPressMenu();
let hit = 0;
const row = mkNode(() => { hit++; });
fire("touchstart", { touches: [{clientX: 10, clientY: 10}], target: row });
fire("touchmove", { touches: [{clientX: 13, clientY: 14}] });
runTimers();
console.log(JSON.stringify(hit));
""")
        self.assertEqual(got, 1)

    def test_抬手就取消(self):
        got = self._run("""
bindLongPressMenu();
let hit = 0;
const row = mkNode(() => { hit++; });
fire("touchstart", { touches: [{clientX: 10, clientY: 10}], target: row });
fire("touchend", {});
runTimers();
console.log(JSON.stringify(hit));
""")
        self.assertEqual(got, 0)

    def test_没挂右键菜单的地方不拦(self):
        """正文长按仍要能选字。"""
        got = self._run("""
bindLongPressMenu();
fire("touchstart", { touches: [{clientX: 1, clientY: 1}], target: mkNode(null) });
runTimers();
const ev = { defaultPrevented: false, preventDefault(){ this.defaultPrevented = true; },
             stopPropagation(){} };
fire("click", ev);
console.log(JSON.stringify(ev.defaultPrevented));
""")
        self.assertFalse(got, "没弹菜单却把点击吞了")

    def test_弹过菜单就吞掉紧随的那次点击(self):
        """不吞的话,菜单弹出的同时那一行也被点开——人看到的是「长按把会话切走了」。"""
        got = self._run("""
bindLongPressMenu();
const row = mkNode(() => {});
fire("touchstart", { touches: [{clientX: 1, clientY: 1}], target: row });
runTimers();
const ev = { target: row, defaultPrevented: false, preventDefault(){ this.defaultPrevented = true; },
             stopPropagation(){} };
fire("click", ev);
console.log(JSON.stringify(ev.defaultPrevented));
""")
        self.assertTrue(got, "长按之后那一次点击没被吞掉")

    def test_只吞一次(self):
        """吞掉的是长按那一次。下一次真的点击必须照常生效。"""
        got = self._run("""
bindLongPressMenu();
const row = mkNode(() => {});
fire("touchstart", { touches: [{clientX: 1, clientY: 1}], target: row });
runTimers();
const mk = () => ({ target: row, defaultPrevented: false, preventDefault(){ this.defaultPrevented = true; },
                    stopPropagation(){} });
const a = mk(); fire("click", a);
const b = mk(); fire("click", b);
console.log(JSON.stringify([a.defaultPrevented, b.defaultPrevented]));
""")
        self.assertEqual(got, [True, False], "把后面的点击也一起吞了")

    def test_长按抬手后的兼容mousedown不关闭菜单(self):
        """Safari 会在 touchend 后向原触点补 mousedown；必须在菜单的点外关闭监听前吞掉。"""
        got = self._run("""
bindLongPressMenu();
const row = mkNode(() => {});
fire("touchstart", { touches: [{clientX: 8, clientY: 9}], target: row });
runTimers();
fire("touchend", {});
const ev = { target: row, defaultPrevented: false, immediateStopped: false,
  preventDefault(){ this.defaultPrevented = true; } };
fire("mousedown", ev);
console.log(JSON.stringify([ev.defaultPrevented, ev.immediateStopped]));
""")
        self.assertEqual(got, [True, True], "兼容鼠标事件仍会把刚弹出的菜单关掉")

    def test_兼容mousedown只吞一次(self):
        got = self._run("""
bindLongPressMenu();
const row = mkNode(() => {});
fire("touchstart", { touches: [{clientX: 8, clientY: 9}], target: row });
runTimers();
const mk = () => ({ target: row, defaultPrevented: false, immediateStopped: false,
  preventDefault(){ this.defaultPrevented = true; } });
const a = mk(); fire("mousedown", a);
const b = mk(); fire("mousedown", b);
console.log(JSON.stringify([[a.defaultPrevented,a.immediateStopped],[b.defaultPrevented,b.immediateStopped]]));
""")
        self.assertEqual(got, [[True, True], [False, False]])

    def test_菜单项的真实点击不被兼容事件守卫吞掉(self):
        got = self._run("""
bindLongPressMenu();
const row = mkNode(() => {});
const menuItem = mkNode(null);
fire("touchstart", { touches: [{clientX: 8, clientY: 9}], target: row });
runTimers();
const down = { target: menuItem, defaultPrevented: false,
  preventDefault(){ this.defaultPrevented = true; } };
const click = { target: menuItem, defaultPrevented: false,
  preventDefault(){ this.defaultPrevented = true; }, stopPropagation(){} };
fire("mousedown", down); fire("click", click);
console.log(JSON.stringify([down.defaultPrevented, click.defaultPrevented]));
""")
        self.assertEqual(got, [False, False], "点菜单项被误当成长按的兼容鼠标事件")

    def test_多指不当长按(self):
        """两指是缩放手势。"""
        got = self._run("""
bindLongPressMenu();
let hit = 0;
const row = mkNode(() => { hit++; });
fire("touchstart", { touches: [{clientX: 1, clientY: 1}, {clientX: 9, clientY: 9}], target: row });
runTimers();
console.log(JSON.stringify(hit));
""")
        self.assertEqual(got, 0)


class SystemCalloutSuppressed(unittest.TestCase):
    def test_带菜单的行关掉系统长按浮层(self):
        """不关的话,我们的菜单与 iOS 的选择/预览浮层会同时冒出来。"""
        m = re.search(r"\.term-item,[^{]*\{\s*-webkit-touch-callout: none;", CSS)
        self.assertIsNotNone(m, "没有关掉 -webkit-touch-callout")
        self.assertIn(".side-item", m.group(0))
