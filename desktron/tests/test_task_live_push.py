"""任务变化的实时通道:服务端推、客户端接、接了要重画。

看护本身在 test_task_watch;这里钉的是它两头的接线——推出来没人接,或者接了不清缓存,
都等于没修。2026-08-25 用户实报的正是「一个会话里改了任务,别的会话感知不到」。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = _srcmap.ui_server_src()


def read(name):
    return _srcmap.frontend_src(name)


CHAT = read("app-chat.js")
PANEL = read("app-panel.js")


class ServerSideStarted(unittest.TestCase):
    def test_起了看护线程(self):
        self.assertIn("task_watch.start(", SRC)

    def test_同时看清单根与任务账本目录(self):
        """TASKS.md 在 REPO_ROOT，轻量进展在 DATA_HOME；漏任一侧都会显示旧状态。"""
        m = re.search(r"task_watch\.start\((.*?)\)", SRC)
        self.assertIsNotNone(m)
        self.assertIn("REPO_ROOT", m.group(1))
        self.assertIn("DATA_HOME", m.group(1))

    def test_起不来不挡服务(self):
        """看护是锦上添花,页面打不开严重得多。"""
        at = SRC.index("task_watch.start(")
        seg = SRC[max(0, at - 200):at]
        self.assertIn("try:", seg, "没有包在 try 里")

    def test_服务先就绪再启动看护(self):
        self.assertLess(SRC.index("_SERVER_THREAD.start()"), SRC.index("task_watch.start("))


class ClientSideReceives(unittest.TestCase):
    """SSE 那一头原来只按 sessionID 找视图,找不到就整条丢掉。"""

    def _dispatch(self):
        m = re.search(r"function hsDispatchGlobal\(type, p\) \{.*?\n\}", CHAT, re.S)
        self.assertIsNotNone(m, "hsDispatchGlobal 不见了")
        return m.group(0)

    def test_不挂在会话上的事件先分派(self):
        m = re.search(r"_hsSSE\.onmessage = \(e\) => \{.*?\n  \};", CHAT, re.S)
        self.assertIsNotNone(m, "onmessage 不见了")
        seg = m.group(0)
        self.assertIn("hsDispatchGlobal(", seg)
        self.assertLess(seg.index("hsDispatchGlobal("), seg.index("_hsViews.get(sid)"),
                        "先按 sessionID 找视图了,找不到就丢——全局事件永远到不了")

    def test_认得那条任务事件(self):
        self.assertIn('type === "task.updated"', self._dispatch())

    def test_收到要使缓存与在途请求一起失效(self):
        """只删结果不够：旧 GET 若仍在途，会在事件之后把旧快照重新写回来。"""
        fn = self._dispatch()
        self.assertIn("invalidateTasks(did)", fn)

    def test_失效后才重画(self):
        fn = self._dispatch()
        self.assertLess(fn.index("invalidateTasks(did)"), fn.index("renderTaskDock("),
                        "先画后失效,仍可能画出旧数据")

    def test_别的事件照旧走会话那条路(self):
        """认下来才返回 true;一律拦掉的话会话事件就到不了视图。"""
        fn = self._dispatch()
        self.assertIn("return false;", fn)


class RepaintOpenViews(unittest.TestCase):
    """收到之后,开着的那几处要跟着换。"""

    def _fn(self):
        m = re.search(r"function onTasksChanged\(did\) \{.*?\n\}", PANEL, re.S)
        self.assertIsNotNone(m, "onTasksChanged 不见了")
        return m.group(0)

    def test_任务页跟着重画(self):
        self.assertIn("openTasksPage(true)", self._fn())

    def test_退役工作流页不再重画(self):
        self.assertNotIn("_wfPagePaintTasks(", self._fn())

    def test_已打开详情原位刷新(self):
        self.assertIn("wfRefreshTaskSheet(did)", self._fn())
        refresh = re.search(r"async function wfRefreshTaskSheet\(.*?\n}", PANEL, re.S)
        self.assertIsNotNone(refresh)
        self.assertIn("_wfTaskMarkdownRefresh", refresh.group(0))
        self.assertIn("Object.assign(task, current)", refresh.group(0))
        self.assertNotIn("_wfTaskMarkdownMount", refresh.group(0), "普通进展刷新不应冲掉正在编辑的补充说明")
        self.assertNotIn("flowChanged", refresh.group(0), "流程变化不应关闭纯 Markdown 阅读面")

    def test_看不见就不画(self):
        """后台标签页重画是白费;回前台时 visibilitychange 那条会补。"""
        self.assertIn("document.hidden", self._fn())

    def test_重画不推导航栈(self):
        """openTasksPage 原来开头就 navPush,直接复用会把返回历史堆满。"""
        m = re.search(r"async function openTasksPage\(repaint\) \{.*?const rows = await tpTaskRows\(\);",
                      PANEL, re.S)
        self.assertIsNotNone(m, "openTasksPage 的签名变了")
        seg = m.group(0)
        self.assertIn("if (!repaint) {", seg)
        self.assertLess(seg.index("if (!repaint) {"), seg.index("navPush("),
                        "navPush 没被挡在重画那一支外面")

    def test_重画不闪加载中也不动滚动位置(self):
        """整页换掉再填的话,别处一改任务,正在读的人就被弹回顶上。"""
        m = re.search(r"async function openTasksPage\(repaint\) \{.*?const rows = await tpTaskRows\(\);",
                      PANEL, re.S)
        seg = m.group(0)
        head = seg[:seg.index("const rows")]
        for gone in ("加载中", "scrollTop = 0"):
            at = head.find(gone)
            self.assertNotEqual(at, -1, gone + " 整个没了,首次打开那一支也需要它")
            self.assertLess(head.index("if (!repaint) {"), at, gone + " 没被挡在重画那一支外面")

    def test_不在那一页就不画(self):
        """事件是广播的,每一页都会收到。"""
        m = re.search(r"async function openTasksPage\(repaint\) \{.*?\n", PANEL, re.S)
        seg = PANEL[PANEL.index("async function openTasksPage(repaint) {"):][:600]
        self.assertIn('repaint && !(body0 && body0.classList.contains("tasks-page"))', seg)


class PollBecomesFallback(unittest.TestCase):
    """客户端那层轮询退成兜底。

    事件通道到得比它快得多,但服务端不重放断线窗口里的事件,所以不能整个撤掉。
    远端放长(那一侧每问一趟是 0.23~1.9 秒的跨区往返),漏掉的那一拍由 SSE 重连时补。
    """

    TASKS = read("app-tasks.js")

    def test_远端放长本机不变(self):
        m = re.search(r"const schedule = \(\) => \{.*?\n  \};", self.TASKS, re.S)
        self.assertIsNotNone(m, "schedule 不见了")
        seg = m.group(0)
        self.assertIn("isLocalView() ? 5000 : 30000", seg)

    def test_重连时补一次(self):
        m = re.search(r"function hsRecoverLiveViews\((?:opts)?\) \{.*?\n\}", CHAT, re.S)
        self.assertIsNotNone(m, "hsRecoverLiveViews 不见了")
        seg = m.group(0)
        self.assertIn("renderTaskDock(", seg, "重连没有补刷任务")
        self.assertIn("onTasksChanged(", seg, "重连没有重画开着的任务页")

    def test_补之前先使全部缓存和在途请求失效(self):
        """不失效的话重画可能继续复用断线前已经发出的旧请求。"""
        m = re.search(r"function hsRecoverLiveViews\((?:opts)?\) \{.*?\n\}", CHAT, re.S)
        seg = m.group(0)
        self.assertIn("invalidateAllTasks()", seg)
        self.assertLess(seg.index("invalidateAllTasks()"), seg.index("renderTaskDock("),
                        "先重画后失效,画的还是旧的")

    def test_轮询没被整个撤掉(self):
        """撤了之后事件通道一坏就再没人发现——清单会一直停在旧数据上。"""
        self.assertIn("_dock.timer = setTimeout(", self.TASKS)


class RegistryPushReplacesPoll(unittest.TestCase):
    """注册表那条轮询退成兜底(2026-08-25 实测:它是当时最密的一条)。

    经隧道 30 秒里 12 次 /workbench/apps-rev,每次换回 0 字节,而且没有 document.hidden
    判断、切到后台照打。改完之后同口径实测:29 个非静态请求降到 17,新建连接 5 降到 3。
    """

    INIT = read("app-init.js")

    def _poll(self):
        m = re.search(r"\(async function appRevPoll\(\) \{.*?\n\}\)\(\);", self.INIT, re.S)
        self.assertIsNotNone(m, "appRevPoll 不见了")
        return m.group(0)

    def test_客户端认那条事件(self):
        m = re.search(r"function hsDispatchGlobal\(type, p\) \{.*?\n\}", CHAT, re.S)
        self.assertIn('type === "apps.updated"', m.group(0))
        self.assertIn("onAppsChanged(", m.group(0))

    def test_两条路共用一个处理(self):
        """事件与兜底轮询都到 onAppsChanged,免得重拉那段逻辑写两份。"""
        self.assertIn("async function onAppsChanged(rev)", self.INIT)
        self.assertIn("await onAppsChanged(rev)", self._poll())

    def test_同一版本只处理一次(self):
        """两条路都会到。不去重的话,一次变更要重拉两遍域与注册表。"""
        m = re.search(r"async function onAppsChanged\(rev\) \{.*?\n\}", self.INIT, re.S)
        self.assertIn("rev === _appRev", m.group(0))

    def test_后台不问(self):
        self.assertIn("document.hidden", self._poll())

    def test_远端放长本机不变(self):
        self.assertIn("isLocalView() ? 2500 : 30000", self._poll())

    def test_轮询没被整个撤掉(self):
        """服务端不重放断线窗口里的事件,撤了之后通道一坏就再没人发现。"""
        self.assertIn("setInterval(", self._poll())


class WarmupDeferredWhenRemote(unittest.TestCase):
    """设置预热在远端错峰；已退役的资产预热不应阻断初始化。

    settings 一趟三百毫秒,开机头几秒正是真正要发的请求在抢连接的时候。
    """

    INIT = read("app-init.js")

    def test_按客户端所在分档(self):
        self.assertIn("const _warm = isLocalView() ? 1 : 20000", self.INIT)

    def test_设置走二十秒档位(self):
        self.assertRegex(self.INIT, r"setTimeout\(prefetchSettings, \d+ \+ _warm\)")

    def test_不调用已退役的资产预热函数(self):
        self.assertNotIn("setTimeout(prefetchAssets", self.INIT)


if __name__ == "__main__":
    unittest.main()
