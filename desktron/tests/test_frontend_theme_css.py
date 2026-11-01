"""前端主题 token 回归:主题不能只换强调色,也要覆盖主体底色。

主题现在只有两个——默认深色(cyan,写在 :root)与翻面的浅色(light)。紫、绿、橙、玫红、灰
那五套换色 2026-08-16 随配色选择器一起清掉:选择器早已不在界面上,常量与 CSS 却都留着,
谁也选不中。这里跟着收敛成「名单三处同源 + 非默认那一档要有自己的 CSS 块」。
"""
import os
import re
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
STYLE = os.path.join(ROOT, "frontend", "style.css")
CORE_JS = os.path.join(ROOT, "frontend", "app-core.js")
SETTINGS_PY = os.path.join(ROOT, "desktron", "lib", "settings.py")


class FrontendThemeCss(unittest.TestCase):
    def setUp(self):
        with open(STYLE, encoding="utf-8") as f:
            self.css = f.read()

    def test_native_controls_follow_theme_without_vibrancy_fallback(self):
        root = re.search(r':root\s*\{(?P<body>.*?)\n\}', self.css, re.S)
        self.assertIsNotNone(root)
        self.assertIn('color-scheme: dark;', root.group('body'))
        light = re.search(r'html\[data-theme="light"\]\s*\{(?P<body>.*?)\n\}', self.css, re.S)
        self.assertIsNotNone(light)
        self.assertIn('color-scheme: light;', light.group('body'))

    def test_shell_surfaces_use_theme_tokens(self):
        self.assertIn("background: var(--chrome-base);", self.css)
        self.assertIn("background: var(--asset-base);", self.css)

    def test_浅色那一档覆盖整套接入点(self):
        """light 不是换个强调色,是翻面:中性色阶、浮层基色、文字与边框都要重写。

        只覆盖 --background-base 一类的话,浅底上会留着深色主题的文字色,正文直接看不清。
        """
        m = re.search(r'html\[data-theme="light"\]\s*\{(?P<body>.*?)\n\}', self.css, re.S)
        self.assertIsNotNone(m)
        body = m.group("body")
        for token in ("--background-base:", "--chrome-base:", "--asset-base:"):
            self.assertIn(token, body, token)


class ThemeRegistrySync(unittest.TestCase):
    """三处主题名单必须同源:CSS 的块、前端的 THEME_PRESET_IDS、后端的 UI_THEME_PRESETS。

    后端那份是白名单:不在表里的值写入被静默丢弃、读取回落到默认。只加前端时开关点得动、
    界面也当场变了,却写不进 state——一刷新或一打开设置页就掉回默认主题(2026-08-15 实见,
    light 就是这么漏的)。三处任缺其一都是同一种「看着生效、其实没存」。
    """

    def setUp(self):
        for name, path in (("css", STYLE), ("js", CORE_JS), ("py", SETTINGS_PY)):
            with open(path, encoding="utf-8") as f:
                setattr(self, name, f.read())

    def _front(self):
        m = re.search(r"const THEME_PRESET_IDS = new Set\(\[(.*?)\]\)", self.js, re.S)
        self.assertIsNotNone(m, "没找到 THEME_PRESET_IDS")
        return set(re.findall(r'"([a-z]+)"', m.group(1)))

    def _back(self):
        m = re.search(r"UI_THEME_PRESETS = \{(.*?)\}", self.py, re.S)
        self.assertIsNotNone(m, "没找到 UI_THEME_PRESETS")
        return set(re.findall(r'"([a-z]+)"', m.group(1)))

    def test_前后端名单一致(self):
        self.assertEqual(self._front(), self._back())

    def test_每个主题都有对应的_css_块(self):
        # 默认那一档写在 :root,不另开块
        default = re.search(r'const id = THEME_PRESET_IDS\.has\(theme\) \? theme : "([a-z]+)"', self.js)
        self.assertIsNotNone(default, "没找到默认主题名")
        rest = self._front() - {default.group(1)}
        self.assertTrue(rest, "除默认外至少还有一档,否则这条断言测不到东西")
        for t in rest:
            self.assertRegex(self.css, rf'html\[data-theme="{t}"\]\s*\{{', t)

    def test_清掉的配色不留残迹(self):
        """选择器不在了,常量与 CSS 也别留着——留着就是下一个人以为能选的东西。"""
        for dead in ("violet", "emerald", "amber", "rose", "slate"):
            self.assertNotRegex(self.css, rf'html\[data-theme="{dead}"\]', dead)
            self.assertNotIn(f'"{dead}"', self._back_raw(), dead)

    def _back_raw(self):
        m = re.search(r"UI_THEME_PRESETS = \{(.*?)\}", self.py, re.S)
        return m.group(1) if m else ""


if __name__ == "__main__":
    unittest.main()
