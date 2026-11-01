"""从后台回来的恢复:僵尸 SSE 要换一条,恢复链不许被顺路的核对绊死。

Chrome 里冻结/解冻复现不出「回来之后页面停在最小化时的状态」——恢复链本身是好的。
iOS 上多两件事:standalone 形态下恢复时 visibilitychange 有不触发的已知场景;
EventSource 挂起后变成僵尸,readyState 还是 OPEN 却再也收不到帧、也不报错。
恢复那一刻不重建它,这一次核对之后的所有更新照样到不了(2026-08-26 用户实见)。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()


def fn(name):
    m = re.search(r"\n(?:async )?function %s\(.*?\n}" % re.escape(name), CHAT, re.S)
    assert m, "%s 不见了" % name
    return m.group(0)


class ResumeRebuildsZombieSSE(unittest.TestCase):
    def test_iOS主屏不发恢复事件时由时钟跳变兜底(self):
        seg = fn("hsWakeProbe")
        self.assertIn("now - _hsWakeAt", seg)
        self.assertIn("gap <= 5000", seg)
        self.assertIn("hsResume()", seg)
        self.assertIn("document.hidden", seg, "后台不能靠探针继续发请求")
        wire = fn("hsEnsureSSE")
        self.assertIn("setInterval(hsWakeProbe, 2000)", wire)

    def test_可见页面也用可观测心跳识别僵尸连接(self):
        seg = fn("hsWakeProbe")
        self.assertIn("now - _hsSSEAt > 45000", seg)
        self.assertIn("_hsSSE.close()", seg)
        self.assertIn("hsEnsureSSE()", seg)
        wire = fn("hsEnsureSSE")
        self.assertIn('addEventListener("ping"', wire)
        self.assertIn("_hsSSEAt = Date.now()", wire)

    def test_藏够久才重建(self):
        seg = fn("hsResume")
        self.assertIn("away > 5000", seg, "桌面上焦点每天变几十次,不该次次重连")
        self.assertIn("_hsSSE.close()", seg)
        self.assertIn("hsEnsureSSE()", seg)

    def test_重连之后照样核对(self):
        """2026-09-03 反转:原来这条钉的是「重连后 return,别重复核对」——省一趟请求。
        但那把恢复整个押在了新连接的 onopen 上,而建连要一个往返(手机经隧道一两秒),
        蜂窝刚恢复、VPN 没挂上时还会失败;onopen 不触发,恢复就再也不发生,内容永久
        缺一截,只能退出重开(用户实见)。

        两边的代价不对等:重复一次是多一趟 HTTP,漏掉一次是内容丢失且无法自愈。
        所以改成两条路都核对,并把这条守卫反过来钉住。"""
        seg = fn("hsResume")
        i = seg.index("hsEnsureSSE()")
        self.assertNotIn("return;", seg[i:], "重连之后又 return 了,恢复重新押回 onopen")
        self.assertIn("hsRecoverLiveViews()", seg[i:])

    def test_两条路都走到对账(self):
        """走不走重连都要核对:只有一处 hsRecoverLiveViews,排在重连之后兜住两条路。"""
        seg = fn("hsResume")
        self.assertEqual(seg.count("hsRecoverLiveViews()"), 1)
        self.assertLess(seg.index("hsEnsureSSE()"), seg.index("hsRecoverLiveViews()"))

    def test_对账不依赖_SSE(self):
        """它走 /state 与转录,是独立的 HTTP。依赖 SSE 的话断线时恰恰用不上。"""
        self.assertIn("hsStateSync", fn("hsRecoverLiveViews"))

    def test_忙时挡下的对账要记欠账(self):
        """回合跑着时不重建(会和流式写入打架),但只挡不记的话,断线窗口里丢掉的内容
        就永久缺失,只能退出重开才看得到(2026-09-03 用户实见:显示已完成但正文只有
        几行,退出重开是完整的 4m01s)。两道 busy 闸都要记。"""
        rec = open(os.path.join(ROOT, "frontend", "app-chat-reconcile.js"), encoding="utf-8").read()
        self.assertEqual(rec.count("v.recOwed = true"), 2, "两道 busy 闸都要记欠账")
        # 入口那道
        i = rec.index("async function hsReconcileNow")
        self.assertIn("v.recOwed = true", rec[i:i + 400])

    def test_取完数又忙了也要记(self):
        """取数那几步是网络往返,手机经隧道要几秒;这期间下一个回合一开始就会被丢弃。
        连续对话时正好撞上。"""
        rec = open(os.path.join(ROOT, "frontend", "app-chat-reconcile.js"), encoding="utf-8").read()
        i = rec.index("/message`")
        self.assertIn("v.recOwed = true", rec[i:i + 600])

    def test_收尾时把欠账还上(self):
        seg = fn("hsTurnSettled")
        self.assertIn("v.recOwed", seg)
        self.assertIn("v.recOwed = false", seg)      # 还完要清,否则每轮都补一次
        self.assertIn("hsReconcileNow(v)", seg)      # 兜底那一次绕开防抖
        # 清账要在触发之前读,不然读到的是已清的值
        self.assertLess(seg.index("const owed = v.recOwed"), seg.index("v.recOwed = false"))

    def test_还账排在_busy_置否之后(self):
        """置否之前还,hsReconcileNow 一进门就被自己那道闸挡下,等于没还。"""
        seg = fn("hsTurnSettled")
        self.assertLess(seg.index("hsBusy(v, false)"), seg.index("const owed = v.recOwed"))

    def test_四个恢复入口都指向_hsResume(self):
        for ev in ("visibilitychange", "pageshow", "online", "focus"):
            self.assertRegex(CHAT, re.escape(ev) + r"\", hsResume\)",
                             ev + " 没接到 hsResume(focus 是 standalone 下的兜底)")

    def test_藏起来的时刻有人记(self):
        seg = fn("hsResume")
        self.assertIn("_hsHiddenAt = now", seg)


class RecoveryIsNotFragile(unittest.TestCase):
    def test_回前台用可查状态收掉压缩进度(self):
        """SSE 收尾帧不重放；/state 必须能区分仍在压与已结束。"""
        sync = fn("hsStateSync")
        self.assertIn("hsCompactStateSync(v, st)", sync)
        seg = fn("hsCompactStateSync")
        self.assertIn("st.compacting", seg)
        self.assertIn("hsCompactTerminal(v, compact)", seg)
        self.assertIn('st.state !== "ready"', seg)
        terminal = fn("hsCompactTerminal")
        self.assertIn("hsCompactOff(v)", terminal)
        self.assertIn('compact.result === "failed"', terminal)

    def test_顺路的核对各自兜错(self):
        """任务清单那一段抛了不该把会话恢复一起带走——带走就是「回来之后永远不动」。"""
        seg = fn("hsRecoverLiveViews")
        i_try = seg.index("try {")
        i_task = seg.index("renderTaskDock")
        i_loop = seg.index("for (const v of _hsViews.values())")
        self.assertLess(i_try, i_task)
        self.assertLess(i_task, i_loop, "会话恢复要在 try 之外,不能连坐")

    def test_状态核对失败要重试一次(self):
        """回前台头几秒蜂窝/VPN 往往还没挂上,第一趟十有八九失败;静默吞掉就悬在半空。"""
        seg = fn("hsRecoverLiveViews")
        self.assertIn("setTimeout", seg)
        self.assertIn("2500", seg)


if __name__ == "__main__":
    unittest.main()
