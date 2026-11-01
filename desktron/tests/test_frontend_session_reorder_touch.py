"""手机端一直没法拖会话次序——桌面端的 makeReorderable(app-rail.js)从来没接过触屏事件,
不是被什么挡住了,是压根没写。移动端长按又已经被 bindLongPressMenu 占用来弹右键菜单,
长按只有一次,不能两边都要(2026-08-25 用户实见,给的方案是「长摁后推动就变成拖拽排序,
菜单如果已弹出,就让它消失」)。

链路:bindLongPressMenu 弹出菜单后,若同一次触摸还在推(超过 LONG_PRESS_SLOP),判定为
"其实想拖不想点菜单",收起菜单、派发 window 事件 cd-longpress-drag(带宿主元素与坐标);
makeReorderable 监听这个事件,认出归不归自己管,命中就直接进拖拽态(不再等鼠标路径那
600ms 候选——bindLongPressMenu 的 500ms 长按已经判过一次意图)。两个模块不知道彼此在做
"排序"这件事,只通过一个坐标+元素的信使事件接头。

第一版写完在真机上没跑起来。两个根因,都是"只有一层、一步到位"的那种测试测不出来的:

其一(可复现,下面有对应用例):侧栏是两层可排序区套着的——项目行(details.proj-fold,
把手是 summary)里面躺着会话行(.term-item),两层各绑一个 makeReorderable。鼠标那条路
靠 opts.rowSel 限定"只能从把手行起拖",触屏这条路当初漏了这道限制,于是从会话行往上
closest("details.proj-fold") 照样摸得到它所属的项目:长按一条会话再推,会话和整个工作流
被同时拖起来,两个实例抢同一个 ghost 与 body.reorder-active,落下来的次序两边都不对。
拿掉那道校验能立刻复现——按真实结构搭两层再拖一条会话,ghost 与 placeholder 各变成 2 个,
且工作流次序被无端提交了一次。

其二(推断,headless 证不了):preventDefault 与阈值判定的先后。手指是连续移动的,一帧
一帧地 2px、5px、8px 地来,而浏览器自己的滚动起判阈值(Android 的 touch slop 8dp、iOS
相近)比这里的 LONG_PRESS_SLOP=10 更小——它先在 8px 那一帧把触摸判给滚动、收走指针,等
我们到 10px 再 preventDefault 已经不被理会,紧跟一个 pointercancel 把拖拽掐掉。所以改成
"菜单一弹出,此后每一帧都先 preventDefault,再判够不够转拖拽"。这一条无法在本地证伪:
CDP 的 Input.dispatchTouchEvent 不走合成器那套 touch-slop 仲裁,新旧两种顺序在 headless
里都通过;依据是浏览器的手势仲裁行为与 SortableJS 的同类处理,不是本地实测。

两条一起改完,2026-08-25 用户在真机上确认可以拖动了。注意这是合并验证:真机只证明了
"两条一起改之后能用",没有单独验证过其二——若日后要动这个顺序,得自己重新在真机上验,
本地测试拦不住。

长按满 500ms 后原地松手时，Safari 还会向原触点补发一次 mousedown；若不拦截，
openCtxMenu 会误判为“点在菜单外”并立即关闭。该兼容事件现由 bindLongPressMenu 只吞一次，
回归覆盖在 test_frontend_touch_menu.py。
"""
import json
import os
import re
import subprocess
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
EVENTS = open(os.path.join(ROOT, "frontend", "app-events.js"), encoding="utf-8").read()
RAIL = _srcmap.rail_src()

LPM_FN = re.search(r"function bindLongPressMenu\(\)[\s\S]+?\n}\n", EVENTS).group(0)
MR_FN = re.search(r"function makeReorderable\([\s\S]+?\n}\n", RAIL).group(0)
MERGE_FN = re.search(r"function mergeOrderedSubset\([\s\S]+?\n}\n", RAIL).group(0)
# 转拖拽那一段:从"菜单已弹出"的早退判断起,到 touchmove 监听器结束
HANDOFF = LPM_FN[LPM_FN.index("if (!fired || !firedEl) return;"):LPM_FN.index("{ passive: false, capture: true }")]
# makeReorderable 里接手触屏的那个处理器
TOUCH_ENTRY = re.search(r'window\.addEventListener\("cd-longpress-drag".+?\n  \}\);', MR_FN, re.S).group(0)
TERM_POLL = re.search(r"function startTermPoll\(\)[\s\S]+?\n}\n", RAIL).group(0)
UPDATE_ROW = re.search(r"function updateTermRow\([\s\S]+?\n}\n", RAIL).group(0)


class 长按菜单弹出后推动转拖拽(unittest.TestCase):
    def test_touchmove监听器是非passive(self):
        """passive:true 下 preventDefault 会被直接忽略,拦不住浏览器起滚动。"""
        m = re.search(r'document\.addEventListener\("touchmove", \(ev\) => \{[\s\S]+?\}, (\{[^}]*\})\);', LPM_FN)
        self.assertTrue(m, "没找到 touchmove 监听器")
        self.assertIn("passive: false", m.group(1))

    def test_拦默认行为排在阈值判定之前(self):
        """顺序不能反,见文件头「其二」:反过来写的话,浏览器在更小的阈值上就把这次触摸
        判给滚动了,等我们够 10px 再拦已经晚了。"""
        pd = HANDOFF.index("ev.preventDefault();")
        gate = HANDOFF.index("if (handedOff) return;")
        slop = HANDOFF.index("> LONG_PRESS_SLOP", gate)
        self.assertLess(pd, gate, "preventDefault 要在 handedOff 早退之前")
        self.assertLess(pd, slop, "preventDefault 要在阈值判定之前")

    def test_转发的是自定义事件cd_longpress_drag(self):
        self.assertIn('new CustomEvent("cd-longpress-drag"', LPM_FN)
        self.assertIn("window.dispatchEvent(", LPM_FN)

    def test_转发前先收起菜单(self):
        self.assertIn("openCtxMenu.close?.()", LPM_FN)

    def test_handedOff挡同一次触摸重复转发(self):
        """一次触摸只转发一次,但 preventDefault 要继续——所以 handedOff 是早退点,
        不是包住 preventDefault 的条件。"""
        self.assertIn("handedOff = true", HANDOFF)
        self.assertIn("if (handedOff) return;", HANDOFF)

    def test_按下即锁住会话列表重绘(self):
        self.assertIn('classList.add("reorder-pending")', LPM_FN)
        self.assertIn('classList.remove("reorder-pending")', LPM_FN)

    def test_fired在转发后仍保持true(self):
        """抬手时补发的 click 仍要吞掉(见 bindLongPressMenu 尾部那条 click 拦截),
        所以转发这一段不能顺手把 fired 清成 false;挡重复转发是 handedOff 的活。"""
        self.assertNotIn("fired = false", HANDOFF)


class openCtxMenu暴露关闭方法(unittest.TestCase):
    def test_close指向drop(self):
        """bindLongPressMenu 没有 m/drop 的引用,只能靠这个口子主动收掉当前菜单。"""
        self.assertIn("openCtxMenu.close = drop;", RAIL)


class makeReorderable接入触屏(unittest.TestCase):
    def test_dragMoveTo被提炼为坐标无关的落点判定(self):
        """鼠标版 dragMove 与触屏版 touchMove 共用同一份插入排序逻辑,不能两处各写一遍。"""
        self.assertIn("const dragMoveTo = (x, y) => {", MR_FN)
        self.assertIn("dragMoveTo(e.clientX, e.clientY);", MR_FN)
        self.assertIn("dragMoveTo(t.clientX, t.clientY);", MR_FN)

    def test_touchMove按touchId锁定发起拖拽的那根手指(self):
        self.assertIn("x.identifier === touchId", MR_FN)

    def test_监听cd_longpress_drag发起触屏拖拽(self):
        self.assertIn("el.closest(itemSel)", TOUCH_ENTRY)
        self.assertIn("container.contains(item)", TOUCH_ENTRY)

    def test_触屏路径同样认把手行(self):
        """文件头「其一」那个真机 bug 的守卫:少了这句,项目行那个实例会跟着会话行一起
        被拖起来(会话行 closest("details.proj-fold") 摸得到它所属的项目)。"""
        self.assertIn("opts.rowSel && !rowOf(item).contains(el)", TOUCH_ENTRY)

    def test_触屏拖拽不绑pointercancel(self):
        """pointercancel 正是"浏览器把这次手势收走了"的信号。触屏拖拽是我们自己用 touch
        事件驱动的,收到它只该忽略——当成"手指松开"会把拖拽掐掉,收尾交给 touchend /
        touchcancel。所以 startDrag 要按来源分,不能无条件挂 PRE_OFF。"""
        self.assertIn("const startDrag = (item, viaTouch) => {", MR_FN)
        self.assertIn("if (!viaTouch) {", MR_FN)
        self.assertIn("startDrag(item, true);", TOUCH_ENTRY)

    def test_行被重渲换掉后按触点兜底(self):
        """侧栏 1.5~5s 轮询一次,集合变了整块重渲;长按 500ms 加人反应过来推动跨着一秒多,
        中间被换掉的话 el 已脱离文档。elementFromPoint 现取一次比认死 el 稳。"""
        self.assertIn("document.elementFromPoint(x, y)", TOUCH_ENTRY)

    def test_endDrag对称清理触屏监听器(self):
        """不管这次拖拽是鼠标发起还是触屏发起,endDrag 都无条件 remove 两套监听——
        移除没挂过的监听器是安全的空操作,不必分支记来源。"""
        self.assertIn('document.removeEventListener("touchmove", touchMove, true);', MR_FN)
        self.assertIn('document.removeEventListener("touchend", touchEnd, true);', MR_FN)
        self.assertIn('document.removeEventListener("touchcancel", touchEnd, true);', MR_FN)

    def test_收尾归零touchId(self):
        self.assertIn("dragging = null; touchId = null;", MR_FN)

    def test_触屏路径不再等600ms候选(self):
        """那 600ms 是鼠标路径防「点一下」误判成拖拽用的;触屏这边 bindLongPressMenu 的
        500ms 长按已经判过一次意图,cd-longpress-drag 一到就直接 startDrag,不再等一轮。"""
        self.assertNotIn("setTimeout", TOUCH_ENTRY)


class 拖拽只重排可见子集(unittest.TestCase):
    def _merge(self, existing, visible):
        js = (MERGE_FN + "\nconsole.log(JSON.stringify(mergeOrderedSubset(" +
              json.dumps(existing) + "," + json.dumps(visible) + ")));" )
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=10)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_折叠在后面的会话不丢失(self):
        self.assertEqual(
            self._merge(["a", "b", "c", "d", "e", "f"], ["b", "a", "c", "d", "e"]),
            ["b", "a", "c", "d", "e", "f"],
        )

    def test_其他引擎的会话保持原位(self):
        self.assertEqual(
            self._merge(["codex-a", "claude-a", "codex-b", "claude-b"],
                        ["claude-b", "claude-a"]),
            ["codex-a", "claude-b", "codex-b", "claude-a"],
        )


class 会话长按不被状态轮询打断(unittest.TestCase):
    def test_桌面鼠标候选期也锁住列表重绘(self):
        """PC 端会话行会被状态轮询替换；600ms 长按尚未进入 dragging 时也必须阻止重绘。
        触屏的候选锁仍由 bindLongPressMenu 独立管理，不能借修 PC 改掉手机手势。"""
        self.assertIn("mousePending = true", MR_FN)
        self.assertIn('classList.add("reorder-pending")', MR_FN)
        self.assertIn('classList.remove("reorder-pending")', MR_FN)
        self.assertIn("mousePending = false", MR_FN)

    def test_请求前后都检查长按候选态(self):
        self.assertGreaterEqual(TERM_POLL.count('classList.contains("reorder-pending")'), 2)

    def test_轮询比较使用稳定会话身份(self):
        self.assertIn("shown.map(termOrderId)", TERM_POLL)
        self.assertIn("r.dataset.orderId", TERM_POLL)

    def test_临时身份变化只就地更新(self):
        self.assertIn("bindTermRowIdentity(row, t)", UPDATE_ROW)


if __name__ == "__main__":
    unittest.main()
