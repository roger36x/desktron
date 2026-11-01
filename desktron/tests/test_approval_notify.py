"""审批挂起时给本人发一条飞书(2026-09-02 用户提出)。

界面那张卡只有人正看着 Desk 才见得到。人在别处、或手机锁着屏时,动作一直挂到超时
(APPROVAL_TTL_S),而本人毫不知情。

只发链接、不做交互卡片:卡片回调要开事件订阅、要常驻进程消费、要处理回写 token;
而人点开链接进 Desk 之后,那张卡本来就在会话里等着——决定仍然只在一处发生,
不必考虑「飞书点了但 Desk 不知道」的并发。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
sys.argv = ["test", os.path.join(ROOT, "frontend"), "0", "0", ROOT]

JS = open(os.path.join(ROOT, "frontend", "app-approval.js"), encoding="utf-8").read()
INIT = open(os.path.join(ROOT, "frontend", "app-init.js"), encoding="utf-8").read()


def _stub(case, mod, **attrs):
    """把桩装上,并在这条用例结束时还原。模块级属性是全测试进程共享的,不还原就外溢。"""
    for k, v in attrs.items():
        old = getattr(mod, k)
        setattr(mod, k, v)
        case.addCleanup(setattr, mod, k, old)


# Synthetic RFC1918 endpoint used only by notification stubs; never contacted.
SYNTHETIC_OUTER = "http://192.168.0.10:7765/"


class 推送(unittest.TestCase):
    def _rt(self, sent, outer=SYNTHETIC_OUTER):
        import remote_tunnel
        # 用完还回去。原来是直接赋值不复原,整轮跑下来 remote_tunnel 一直挂着这两个桩,
        # 后面任何验它自身行为的用例都在跟假的说话(2026-09-04 实见)。
        _stub(self, remote_tunnel, _lark_notify=lambda t: (sent.append(t), True)[1],
              status=lambda: {"outer": outer, "running": True})
        from app_control.runtime import AppControlRuntime
        rt = AppControlRuntime(os.path.join(tempfile.mkdtemp(), "t.sqlite3"))
        rt.NOTIFY_DELAY_S = 0   # 这几条验的是消息内容,不是延后策略(那条另有用例)
        rt.offer("browser", "tab-1", "w1", 300, session_id="s1")
        return rt, rt.claim("s1", "browser", "tab-1")

    def test_不在锁里推(self):
        """lark-cli 是子进程,慢的时候几秒。占着锁会把整个 runtime 卡住。"""
        src = open(os.path.join(ROOT, "desktron", "lib", "app_control", "runtime.py"),
                   encoding="utf-8").read()
        # 看的是 if 那一行的缩进:它要与 with 同级(8 空格)才说明已经出锁。
        # 推送本身是 if 的函数体,缩进自然更深,拿它判会误报。
        i = src.index("if needs_approval:\n            self._notify_later(out)")
        line = src[src.rindex("\n", 0, i) + 1:i]
        self.assertEqual(len(line) - len(line.lstrip()), 8, "还在锁里推")
        # 落库那一句仍在锁内(12 空格),两者一比才看得出确实跨出来了
        j = src.index("out = self._operation_dict")
        l2 = src[src.rindex("\n", 0, j) + 1:j]
        self.assertEqual(len(l2) - len(l2.lstrip()), 12)


class 说清要确认什么(unittest.TestCase):
    """推出去的是给人读的,不是内部叫法。原来直接拼 kind · name,飞书里显示
    「inspect · screenshot」——人看不出自己要同意什么(2026-09-03 用户实见)。"""

    def _label(self, op):
        from app_control.runtime import AppControlRuntime as R
        return R.__new__(R)._ask_label(op)

    def test_常见动作有人话(self):
        self.assertEqual(self._label({"kind": "inspect", "name": "screenshot"}), "对当前页面截图")
        self.assertEqual(self._label({"kind": "act", "name": "submit"}), "提交表单")

    def test_显式业务意图优先于通用动作(self):
        self.assertEqual(self._label({"kind": "act", "name": "submit",
                                      "intent": "提交本周值班安排"}),
                         "提交本周值班安排")

    def test_带上目标网址(self):
        self.assertIn("https://x.test/",
                      self._label({"kind": "act", "name": "open", "input": {"url": "https://x.test/"}}))

    def test_没登记的动作仍说得出话(self):
        """表里没有的不能显示成空白,退回内部叫法也好过什么都不说。"""
        self.assertIn("weird", self._label({"kind": "act", "name": "weird"}))


class 深链(unittest.TestCase):
    def _rt_for_link(self, sent):
        import remote_tunnel, tempfile as _t
        _stub(self, remote_tunnel, _lark_notify=lambda t: (sent.append(t), True)[1],
              status=lambda: {"outer": SYNTHETIC_OUTER, "running": True})
        from app_control.runtime import AppControlRuntime
        rt = AppControlRuntime(os.path.join(_t.mkdtemp(), "t.sqlite3"))
        rt.NOTIFY_DELAY_S = 0   # 这几条验的是消息内容,不是延后策略(那条另有用例)
        rt.offer("browser", "tab-1", "w1", 300, session_id="s1")
        return rt, rt.claim("s1", "browser", "tab-1")

    def test_认_approve_参数(self):
        self.assertIn('get("approve")', JS)
        # 要看真的被调用,不是「文件里出现过这个词」——注释里也会提到它,
        # 撤掉调用照样能通过(2026-09-03 反向验证时发现这条没咬住)。
        import re as _re
        self.assertRegex(INIT, r"\bapprFromLink\s*(\)|,|\()",
                         "apprFromLink 没有被真正调用")

    def test_排在页面恢复之后(self):
        """它要切到指定的那段会话,而恢复流程正按快照把「上次看的那一页」翻到前台。
        原来挂在 setTimeout(1.2 秒)上,等于和恢复赛跑——切好的页随即被覆盖,
        人看到的是「闪一下又回到别处,卡片不见了」(2026-09-03 用户实见)。"""
        self.assertNotIn("setTimeout(apprFromLink", INIT, "又改回赛跑式的定时触发了")
        self.assertIn("await apprFromLink()", INIT)
        self.assertLess(INIT.index("restorePagesAndBrowser()"), INIT.index("apprFromLink()"))
        self.assertLess(INIT.index("rememberEngineView(curEngine)"), INIT.index("apprFromLink()"))

    def test_立刻查一次不等轮询(self):
        """人是特意点进来处理它的,再等五秒就成了「点了没反应」。"""
        # 取到函数结束,不用固定字符数——函数会变长(2026-09-03 加了切页与失效提示),
        # 窗口写死就会截在断言目标之前,红得莫名其妙。
        i = JS.index("function apprFromLink")
        seg = JS[i:JS.index("\n}", i)]
        self.assertIn("/workbench/app-control/approvals/ticket", seg)
        self.assertNotIn("apprPoll()", seg)

    def test_进来先切到那段会话(self):
        i = JS.index("function apprFromLink")
        seg = JS[i:i + 1400]
        self.assertIn('q.get("session")', seg)
        self.assertIn("openHtmlSession", seg)
        self.assertIn("showOverlay", seg)
        # 切页要排在查待批之前,否则卡片画进旧的那一页
        self.assertLess(seg.index("showOverlay"),
                        seg.index("/workbench/app-control/approvals/ticket"))

    def test_失效链接有交代(self):
        """超时或已在别处决定之后点进来,什么都不说人会以为页面坏了。"""
        i = JS.index("function apprFromLink")
        seg = JS[i:i + 1800]
        self.assertIn("已经结束", seg)

    def test_用完即清(self):
        """留着的话刷新一次又跳一遍,而那条多半已经决定过了。"""
        i = JS.index("function apprFromLink")
        seg = JS[i:i + 700]
        self.assertIn('u.hash = ""', seg)
        self.assertIn("replaceState", seg)

if __name__ == "__main__":
    unittest.main(argv=["test_approval_notify"])

class NotifyOnlyWhenAbsent(unittest.TestCase):
    """在场的人不该被飞书吵(2026-09-03 实见:一晚推了九条)。

    界面那张卡只有「人不在」时才需要飞书补一条。原来一挂起就推,而人当时正盯着屏幕,
    codex 连续试开页,九次待确认全推了过去。判据不是猜人在不在,是看结果:等一小会儿
    再回查这条动作还在不在等人;已经批了、拒了、超时了就什么都不发。
    """

    def test_挂起不立刻推而是延后(self):
        src = open(os.path.join(ROOT, "desktron", "lib", "app_control", "runtime.py"), encoding="utf-8").read()
        seg = src[src.index("if needs_approval:"):]
        seg = seg[:seg.index("\n\n")]
        self.assertIn("_notify_later(out)", seg)
        self.assertNotIn("_notify_pending(out)", seg)   # 不再同步直推
        later = src[src.index("def _notify_later"):src.index("def _pending_others")]
        self.assertIn("NOTIFY_DELAY_S", later)
        self.assertIn("threading.Timer", later)
        # 到点必须回查状态,否则延后只是把噪音推迟
        self.assertIn('row["status"] != "waiting_user"', later)

class FocusFromLink(unittest.TestCase):
    """从飞书点进来的那一张要顶到眼前,且期限跟着放宽(2026-09-03)。

    2026-09-04 起不再开居中遮罩:待批常驻在信息卡首区或右上通知里,点进来只需在它所在的
    宿主里展开并短暂高亮。遮罩只是多一层要关的东西。
    """

    def test_深链就位后展开并高亮(self):
        self.assertIn("apprFocus(id)", JS)
        seg = JS[JS.index("function apprFocus"):]
        seg = seg[:seg.index("\n}\n", seg.index("return true"))]
        # 已结束的不弹,那只是打扰
        self.assertIn("!_apprPending(op)", seg)
        # 展开的是它；通知态会直接将同一项画进审批卡
        self.assertIn("_apprOpenId = id", seg)
        self.assertIn("noticeCard.dataset.op === id", seg)
        self.assertIn('"appr-hi"', seg)
        # 不再有遮罩
        self.assertNotIn("appr-focus", JS)

    def test_遮罩样式已撤(self):
        css = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
        self.assertNotIn(".appr-focus", css)

    def test_已通知的走宽档期限(self):
        src = open(os.path.join(ROOT, "desktron", "lib", "app_control", "runtime.py"),
                   encoding="utf-8").read()
        self.assertIn("NOTIFIED_TTL_S = 900", src)
        # 两档写在同一条查询里,免得两次扫描之间被通知的落进错的那一档
        seg = src[src.index("stale = self.db.execute"):]
        seg = seg[:seg.index(").fetchall()")]
        self.assertIn("COALESCE(notified_at,0)>0", seg)
        self.assertIn("NOTIFIED_TTL_S, APPROVAL_TTL_S", seg)

    def test_发失败不记已通知(self):
        src = open(os.path.join(ROOT, "desktron", "lib", "app_control", "runtime.py"),
                   encoding="utf-8").read()
        seg = src[src.index("def _notify_later"):src.index("def _pending_others")]
        # 人根本没被叫到时再给十五分钟,只是让 Agent 空等更久
        self.assertIn("if not self._notify_pending(op):", seg)
        self.assertIn("notified_at=?", seg)

    def test_旧库补列(self):
        src = open(os.path.join(ROOT, "desktron", "lib", "app_control", "runtime.py"),
                   encoding="utf-8").read()
        self.assertIn("ALTER TABLE operations ADD COLUMN notified_at REAL", src)
