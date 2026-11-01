"""外观设置是端侧浅色/深色固定偏好，不写服务端全局状态。"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SETTINGS = open(os.path.join(ROOT, "frontend", "app-settings-core.js"), encoding="utf-8").read()
STYLE = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
CORE = open(os.path.join(ROOT, "frontend", "app-core.js"), encoding="utf-8").read()


class 端侧外观偏好(unittest.TestCase):
    def test_设置页只提供浅色与深色(self):
        seg = re.search(r'// ===== 外观:当前端的深浅偏好 =====[\s\S]+?gPref\.appendChild\(ap\);', SETTINGS).group(0)
        self.assertIn('["light", "浅色"', seg)
        self.assertIn('["dark", "深色"', seg)
        self.assertNotIn("跟随系统", seg)
        self.assertIn("不随系统外观变化", seg)

    def test_外观选择不写服务端或全局状态(self):
        seg = re.search(r'// ===== 外观:当前端的深浅偏好 =====[\s\S]+?gPref\.appendChild\(ap\);', SETTINGS).group(0)
        self.assertIn("setThemePreference(id)", seg)
        self.assertNotIn("put(", seg)
        self.assertNotIn("wbState.ui =", seg)

    def test_选择后刷新顶部那一条的底色(self):
        seg = re.search(r'// ===== 外观:当前端的深浅偏好 =====[\s\S]+?gPref\.appendChild\(ap\);', SETTINGS).group(0)
        self.assertIn("refreshStandaloneThemeChrome()", seg)

    def test_手机端整体重载而不只是主屏模式(self):
        """顶部那一条只在启动时取一次色:主屏网页读 apple-* 那枚 meta,普通 Safari 标签读
        theme-color,运行时改都不重画。原来只对 navigator.standalone 重载,普通 Safari 标签
        被漏掉(2026-09-03 用户实见浅色下顶上仍是黑的)。判据改成手机端整体,与 CSS 那条
        媒体查询同一口径;桌面壳与桌面浏览器改 meta 即时生效,不重载。"""
        fn = re.search(r"function refreshStandaloneThemeChrome\(\)[\s\S]+?\n}", CORE).group(0)
        self.assertIn("isMobileView()", fn)
        self.assertIn("navigator.standalone === true || mobile", fn)
        self.assertIn("location.reload()", fn)

    def test_重载前偏好已落盘(self):
        """index.html 启动那段读 localStorage 里的 cd_theme_pref 来产出 meta;
        setThemePreference 必须排在 refreshStandaloneThemeChrome 之前,否则重来一次还是旧色。"""
        seg = re.search(r'// ===== 外观:当前端的深浅偏好 =====[\s\S]+?gPref\.appendChild\(ap\);', SETTINGS).group(0)
        self.assertLess(seg.index("setThemePreference(id)"), seg.index("refreshStandaloneThemeChrome()"))

    def test_两个外观选项等分整行(self):
        rule = re.search(r'\.sm-theme-list\s*\{([^}]*)\}', STYLE).group(1)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", rule)
        self.assertNotIn("repeat(3", rule)


if __name__ == "__main__":
    unittest.main()
