"""设置页的浏览器操控应呈现 Desk App Control，而不是旧 Playwright 安装器。"""
import os
import unittest
import _srcmap


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
JS = _srcmap.settings_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


class BrowserControlSettings(unittest.TestCase):
    def test_默认页只呈现内置浏览器授权模型(self):
        self.assertIn('smSection("浏览器操控"', JS)
        for text in ("隔离网页", "登录态网页", "当前标签页", "已授权站点", "测试连接"):
            self.assertIn(text, JS)
        for retired in ("浏览器操控（Playwright）", "Chrome 扩展", "扩展 token（可选）"):
            self.assertNotIn(retired, JS)

    def test_读取真实控制状态并可撤销授权(self):
        self.assertIn('/workbench/app-control/probe?app_id=browser', JS)
        self.assertIn('_apprNative("grantsList"', JS)
        self.assertIn('_apprNative("grantRevoke"', JS)
        self.assertNotIn('api("/workbench/app-control/grants', JS)
        self.assertIn("授权记录仅可在 Desk 桌面端查看和撤销", JS)
        self.assertNotIn('api("/workbench/browser"', JS)

    def test_移动端使用独立响应式布局(self):
        self.assertIn(".brw-status-row", CSS)
        self.assertIn(".brw-path-row", CSS)
        self.assertIn("@media (max-width: 520px)", CSS)


if __name__ == "__main__":
    unittest.main()
