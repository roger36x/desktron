"""终端浮层(设置页 _authTuiModal)断线不等于结束(frontend/app-settings.js)。

2026-08-26 用户实测:手机上点「已有,填入」弹出向导终端,切去别的 app 再回来,
浮层连终端进程一起没了;PC 休眠、网络抖动同理。根因是 ws 一 close 就
finish()——收浮层之外还 DELETE 了终端,把「断线」和「进程退出」混成一件事,
而手机切后台时浏览器掐 WebSocket 是常态。

钉住修复的三个要点:close 时先问后端终端活着吗;活着重连,不重开浮层;
查不到状态按活着处理(误留一个浮层可以再点 ×,误删一个跑了一半的向导找不回来)。
"""
import os
import unittest
import _srcmap

_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
JS = _srcmap.settings_src()

BLK = JS[JS.index("async function _authTuiModal"):]
BLK = BLK[:BLK.index("\nasync function ")]
CSS = open(os.path.join(_ROOT, "frontend", "style.css"), encoding="utf-8").read()


class DropIsNotExit(unittest.TestCase):
    def test_close_不再直接_finish(self):
        self.assertNotIn('ws.addEventListener("close", finish)', BLK)
        self.assertIn("onDrop", BLK)

    def test_断线先问终端还活着吗(self):
        self.assertIn('api("/workbench/terminals"', BLK)
        self.assertIn('t.alive !== false', BLK)

    def test_活着就重连_死了才收(self):
        self.assertIn("if (alive) attach();\n    else finish();", BLK)

    def test_查不到状态按活着处理(self):
        """刚恢复网络时列表接口可能还不通。误留浮层可以再点 ×,误删向导找不回来。"""
        i = BLK.index("const stillAlive")
        self.assertIn("return true", BLK[i:BLK.index("const onDrop")])

    def test_主动关闭仍然会删终端(self):
        """点 × 与点遮罩是明确的「不要了」,照旧收浮层删终端——修断线不该把这条也改掉。"""
        self.assertIn('method: "DELETE"', BLK)
        self.assertIn("x.onclick = finish", BLK)

    def test_主动断开的连接不触发重连(self):
        """dispose 主动断的 close 不应触发重新查询。"""
        self.assertIn("_intentional", BLK)

    def test_切回页面时主动查一次(self):
        """有的浏览器挂起期间不派 close,回来 socket 半死,界面看着还在敲什么都没反应。"""
        self.assertIn('"visibilitychange"', BLK)
        self.assertIn("readyState > 1", BLK)

    def test_浮层关掉后不再监听可见性(self):
        self.assertIn('removeEventListener("visibilitychange"', BLK)


class ProductizedTerminalShell(unittest.TestCase):
    def test_弹窗有对话框语义与清晰退出动作(self):
        self.assertIn('panel.setAttribute("role", "dialog")', BLK)
        self.assertIn('panel.setAttribute("aria-modal", "true")', BLK)
        self.assertIn('x.setAttribute("aria-label", DeskI18n.tx("关闭终端"))', BLK)
        self.assertIn('done.textContent = DeskI18n.tx("完成并返回")', BLK)
        self.assertIn("done.onclick = finish", BLK)

    def test_移除连接状态底部提示与操作详情(self):
        self.assertNotIn('auth-tui-connection', BLK)
        self.assertNotIn('document.createElement("details")', BLK)
        self.assertNotIn('document.createElement("p")', BLK)
        self.assertNotIn('title.appendChild(detail)', BLK)
        self.assertIn('foot.append(done)', BLK)

    def test_终端与说明层级分开且移动端铺满(self):
        for selector in (".auth-tui-copy", ".auth-tui-stage", ".auth-tui-foot", ".auth-tui-done"):
            self.assertIn(selector, CSS)
        self.assertIn(".auth-tui-panel > .auth-tui-host", CSS)
        self.assertIn("flex: 1 1 auto; height: 100%", CSS)
        mobile = CSS[CSS.index("@media (max-width: 600px)"):]
        self.assertIn(".auth-tui-panel { width: 100%; height: 100dvh", mobile)


if __name__ == "__main__":
    unittest.main()
