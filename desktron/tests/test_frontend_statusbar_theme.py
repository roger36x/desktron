"""手机独立窗口的状态栏底色跟着主题走,且不会被一次失败的请求钉死。

状态栏那一条由 index.html 顶上两个 meta 决定:theme-color 与
apple-mobile-web-app-status-bar-style。页面解析时先按当前端偏好或 localStorage 里的 cd_theme
同步生成两枚 meta，applyTheme 之后再校准普通浏览器的动态 chrome。iOS 主屏模式改主题时会重载,
applyTheme 之后再校准。无头浏览器实测这条路是通的:

    applyTheme('light')  -> theme-color #ffffff, apple 样式 default
    applyTheme('cyan')   -> theme-color #191919, apple 样式 black

问题出在启动那一步。/workbench/state 取不到时,wbState 还是默认值,
applyTheme(undefined) 会落回深色并把 cd_theme 一起覆盖成深色;而 iOS 独立窗口只在
启动那一刻读 apple-mobile-web-app-status-bar-style,之后改不动——于是浅色下顶上一直是
黑条(2026-08-24 用户实报)。手机经隧道访问时这条请求本来就容易失败,一次就钉住。
所以请求失败时不再动主题,保持启动时按缓存落好的那一档。
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
INIT = open(os.path.join(ROOT, "frontend", "app-init.js"), encoding="utf-8").read()
CORE = open(os.path.join(ROOT, "frontend", "app-core.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "frontend", "index.html"), encoding="utf-8").read()


class 启动不覆盖缓存的主题(unittest.TestCase):
    def test_取到状态才应用主题(self):
        m = re.search(r"let stateOk = false;[\s\S]{0,900}?if \(stateOk\) applyTheme\(", INIT)
        self.assertTrue(m, "applyTheme 没有挂在「请求成功」这个条件上")

    def test_失败时不落回深色(self):
        """applyTheme(undefined) 会把 cd_theme 写成 cyan,状态栏下次启动就跟着变黑。"""
        self.assertNotIn("applyTheme(wbState.ui && wbState.ui.theme);\n", INIT.replace(
            "if (stateOk) applyTheme(wbState.ui && wbState.ui.theme);", ""))


class 主题带动状态栏(unittest.TestCase):
    def test_applyTheme里同步状态栏(self):
        seg = re.search(r"function applyTheme\(theme\)[\s\S]+?\n}", CORE).group(0)
        self.assertIn("cdStatusBar(id === \"light\")", seg)
        self.assertIn('localStorage.setItem("cd_theme", id)', seg)

    def test_两个meta都跟着切(self):
        """theme-color 管新版 iOS 与 Android;apple-* 那个管更老的 iOS,
        且它在启动解析时就被读掉,少一个就有一路不跟。"""
        seg = re.search(r"window\.cdStatusBar = function \(light\)[\s\S]+?\n    \};", HTML).group(0)
        self.assertIn('meta[name="theme-color"]', seg)
        self.assertIn('meta[name="apple-mobile-web-app-status-bar-style"]', seg)
        self.assertIn('light ? "#ffffff" : "#191919"', seg)
        self.assertIn('light ? "default" : "black"', seg)

    def test_解析时先按缓存落一次(self):
        """等 JS 跑完再改就晚了——iOS 启动那一刻已经把 apple-* 读走。"""
        self.assertIn('localStorage.getItem("cd_theme_pref")', HTML)
        self.assertIn('localStorage.setItem("cd_theme_pref", light ? "light" : "dark")', HTML)
        self.assertIn('document.documentElement.dataset.theme = light ? "light" : "cyan"', HTML)
        self.assertIn('document.write(\'<meta name="apple-mobile-web-app-status-bar-style"', HTML)
        self.assertIn('document.write(\'<meta name="theme-color"', HTML)
        self.assertLess(HTML.index('localStorage.getItem("cd_theme_pref")'),
                        HTML.index('apple-mobile-web-app-status-bar-style" content="'))


class 当前端固定主题(unittest.TestCase):
    def test_偏好只写浏览器本地(self):
        seg = re.search(r"function setThemePreference\(preference\)[\s\S]+?\n}", CORE).group(0)
        self.assertIn('localStorage.setItem("cd_theme_pref", preference)', seg)

    def test_不再监听系统外观变化(self):
        self.assertNotIn("SYSTEM_THEME_MEDIA", CORE)
        self.assertNotIn("onSystemThemeChange", CORE)
        self.assertNotIn('themePreference() !== "system"', CORE)

    def test_旧system偏好会固化为当前主题(self):
        seg = re.search(r"function themePreference\(\)[\s\S]+?\n}", CORE).group(0)
        self.assertIn('if (id === "system")', seg)
        self.assertIn('localStorage.setItem("cd_theme_pref", fixed)', seg)


if __name__ == "__main__":
    unittest.main()
