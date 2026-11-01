"""手机网络重连后，最终正文必须从权威记录补回来。

EventSource 自动重连但不重放断线窗口里的事件。过程区通常已经画出，若最终正文与 idle
恰好在手机切后台或换网时丢失，后端记录完整、当前 DOM 却永久缺正文。重连后应同步控制态，
再用现有的回合末对账修复消息差异；尚未加载或已冷却的页面仍交给 hsLoad，避免重复重建。
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


class SSEReconnectRecovery(unittest.TestCase):
    def test_重连后同步状态并对账正文(self):
        seg = fn("hsEnsureSSE")
        self.assertIn("_hsSSE.onopen", seg)
        self.assertIn("hsRecoverLiveViews()", seg)

        recover = fn("hsRecoverLiveViews")
        self.assertIn("hsStateSync(v)", recover)
        self.assertIn("hsReconcile(v)", recover)

    def test_只恢复已经加载且当前可见的会话(self):
        seg = fn("hsRecoverLiveViews")
        self.assertIn("!v.historyReady || hsHidden(v)", seg)

    def test_手机恢复前台与网络恢复也触发对账(self):
        seg = fn("hsEnsureSSE")
        # 2026-08-26 起这三个入口改指 hsResume:它先判「是不是从后台回来且藏够久」,
        # 是就重建僵尸 SSE(新连接的 onopen 再调 hsRecoverLiveViews),否则直通核对。
        # 意图不变:恢复前台/网络恢复都要触发对账,只是多了一层换连接的判断。
        self.assertIn('document.addEventListener("visibilitychange", hsResume)', seg)
        self.assertIn('window.addEventListener("pageshow", hsResume)', seg)
        self.assertIn('window.addEventListener("online", hsResume)', seg)
        resume = fn("hsResume")
        self.assertIn("hsRecoverLiveViews()", resume, "直通那一支要落回对账")
        self.assertIn("_hsRecoveryWired", seg)

    def test_历史成功灌入后才允许重连恢复(self):
        seg = fn("hsLoad")
        self.assertIn("v.historyReady = false", seg)
        self.assertIn("v.historyReady = true", seg)
        # 按 `hsStateSync(` 找,不按整串带参数的调用找:2026-08-25 那一趟改成了提前发、
        # 原位收(hsStateSync(v, statePre)),断言若绑在旧写法上就会「找不到」而不是变红,
        # 那比红更糟——它守的其实是「状态同步收完才置 historyReady」这个先后。
        self.assertLess(seg.index("await hsStateSync("), seg.index("v.historyReady = true"))

    def test_冷页清空时撤销已加载标记(self):
        self.assertIn("v.needLoad = true;\n    v.historyReady = false;", fn("hsEvictCold"))


class 服务实例握手(unittest.TestCase):
    def test_SSE首帧进入运行时版本判断(self):
        dispatch = fn("hsDispatchGlobal")
        self.assertIn('type === "server.connected"', dispatch)
        self.assertIn("hsRuntimeConnected(p.runtime", dispatch)

    def test_前端版本变化才整页重载(self):
        runtime = fn("hsRuntimeConnected")
        self.assertIn("prev.frontend_rev !== runtime.frontend_rev", runtime)
        self.assertIn("location.reload()", runtime)
        self.assertIn("prev.boot_id !== runtime.boot_id", runtime)
        boot_branch = runtime[runtime.rindex("if (prev.boot_id"):]
        self.assertIn("hsRecoverLiveViews()", boot_branch)
        self.assertNotIn("location.reload()", boot_branch)

    def test_心跳持续核对前端版本(self):
        """sync-home 可以只换静态文件、不重启当前后端；不能只靠连接首帧发现换版。"""
        ensure = fn("hsEnsureSSE")
        self.assertIn('addEventListener("ping", (e)', ensure)
        self.assertIn("hsRuntimeConnected(runtime)", ensure)
        self.assertIn('api("/workbench/runtime")', ensure, "旧后端空心跳也要能自愈")


if __name__ == "__main__":
    unittest.main()
