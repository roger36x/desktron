"""PC 端长按拖拽会话「很难触发」的根因:window 上的捕获态 blur 监听把元素失焦当成了窗口失焦。

2026-09-04 用户报:修过多次仍很难触发。此前几轮修的是时序与轮询重绘的竞争,都在干净页面里
验证通过——而干净页面正是唯一能拖的情形。

WebKit(Desk 壳的内核)探针实证,同一行、同一点、同一套动作:

    页面里什么都没聚焦        → 600ms 后进入拖拽,期间无 blur 事件
    聊天输入框 #hs-inp 聚焦   → 按下 3ms 收到 blur(target=DIV#hs-inp),候选被清,拖不起来
    修复后,输入框聚焦        → 仍收到那条 blur,但被忽略,600ms 后进入拖拽

机制:`window.addEventListener("blur", fn, true)`。blur 不冒泡,但捕获阶段从 window 往下走,
页面里任何元素失焦都会先经过 window 的捕获监听。点会话行那一下让正聚焦的输入框失焦,
这条监听就把它当成「窗口切走了」,调 cleanupPre / endDrag 收尾。真实使用中输入框几乎总是
聚焦的,于是长按几乎总是失败;偶尔能拖,是刚好点过别处、焦点落在 body 上。

三处同样写法(候选期 preUp、拖拽期 dragUp、手机长按菜单的 clear)一并只认 e.target === window。
不改捕获标志:removeEventListener 按 (type, fn, capture) 三元组匹配,改标志就得连着改三处
remove,漏一处监听器永远摘不掉。

顺带:候选期(body.reorder-pending)禁掉选区。探针里按住+1px 抖动时 116ms 就起了 selectstart,
一直按着会在行里划选文字。
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
NAV = open(os.path.join(ROOT, "frontend", "app-rail-nav.js"), encoding="utf-8").read()
EVENTS = open(os.path.join(ROOT, "frontend", "app-events.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()

MR = re.search(r"\nfunction makeReorderable\([\s\S]+?\n}\n", NAV).group(0)
LPM = re.search(r"\nfunction bindLongPressMenu\(\)[\s\S]+?\n}\n", EVENTS).group(0)


class 只认窗口自己的失焦(unittest.TestCase):
    def test_候选期的收尾只在窗口失焦时触发(self):
        """preUp 也挂在 mouseup/pointerup 等事件上,那些事件的 target 是元素,
        所以判据必须限定在 e.type === "blur" 之内,不能一概按 target 过滤。"""
        self.assertIn("const winBlur = (e) => e.target === window;", MR)
        self.assertIn('const preUp = (e) => { if (e && e.type === "blur" && !winBlur(e)) return; cleanupPre(); };', MR)

    def test_拖拽期的收尾同样只认窗口失焦(self):
        """拖到一半焦点从输入框移开是常事(拖拽本身就会让它失焦),元素 blur 不能结束拖拽。"""
        self.assertIn('const dragUp = (e) => { if (e && e.type === "blur" && !winBlur(e)) return; endDrag(moved); };', MR)

    def test_手机长按菜单那份也改了(self):
        """同一写法的第三处。手机上点会话行时输入框失焦,长按候选同样会被误清。"""
        self.assertIn('window.addEventListener("blur", (e) => { if (e.target === window) clear(); }, true);', LPM)
        self.assertNotIn('window.addEventListener("blur", clear, true);', LPM)

    def test_捕获标志没有改动(self):
        """add 与 remove 的 capture 标志必须成对。这里选择保留 true、在处理器里过滤,
        而不是把 add 改成非捕获——那样三处 removeEventListener 都要跟着改,漏一处就摘不掉。"""
        self.assertIn('window.addEventListener("blur", preUp, true);', MR)
        self.assertIn('window.removeEventListener("blur", preUp, true);', MR)
        self.assertIn('window.addEventListener("blur", dragUp, true);', MR)
        self.assertIn('window.removeEventListener("blur", dragUp, true);', MR)


class 候选期禁选区(unittest.TestCase):
    def test_reorder_pending期间整页不可选(self):
        m = re.search(r"body\.reorder-pending, body\.reorder-pending \* \{([^}]*)\}", CSS)
        self.assertTrue(m, "缺 reorder-pending 的禁选区规则")
        self.assertIn("user-select: none !important", m.group(1))
        self.assertIn("-webkit-user-select: none !important", m.group(1))


if __name__ == "__main__":
    unittest.main()
