"""设置面板 UI 外观配置回归。

跑:python3 -m unittest desktron/tests/test_settings_ui_theme.py
"""
import importlib
import os
import shutil
import sys
import tempfile
import types
import unittest


class SettingsUiTheme(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="settings-ui-theme-")
        self.repo = os.path.join(self.base, "repo")
        self.frontend = os.path.join(self.base, "frontend")
        self.data = os.path.join(self.base, "data")
        for path in (self.repo, self.frontend, self.data):
            os.makedirs(path, exist_ok=True)

        os.environ["DESKTRON_DATA_HOME"] = self.data
        sys.argv = ["test", self.frontend, "0", "0", self.repo]
        lib = os.path.join(os.path.dirname(__file__), "..", "lib")
        if lib not in sys.path:
            sys.path.insert(0, lib)

        for name in ("settings", "workbench", "deskcfg", "credstore", "engines"):
            sys.modules.pop(name, None)

        engines = types.ModuleType("engines")
        engines.ENGINES = {}
        sys.modules["engines"] = engines
        self.settings = importlib.import_module("settings")

    def tearDown(self):
        for name in ("settings", "workbench", "deskcfg", "credstore", "engines"):
            sys.modules.pop(name, None)
        shutil.rmtree(self.base, ignore_errors=True)

    def test_ui_theme_defaults_and_persists_selected_preset(self):
        ui = self.settings._settings_get()["ui"]
        self.assertEqual(ui["theme"], "cyan")
        self.assertFalse(ui["log_overlay"])

        # 现在只有默认深色与翻面的浅色两档(2026-08-16 清掉五套无入口的换色)
        self.settings._settings_apply({"ui": {"theme": "light"}})
        self.assertEqual(self.settings._settings_get()["ui"]["theme"], "light")

    def test_名单外的主题写不进去(self):
        """白名单挡住的值要静默丢弃并保持原样,不能把 state 写成一个前端认不出的主题。"""
        self.settings._settings_apply({"ui": {"theme": "light"}})
        self.settings._settings_apply({"ui": {"theme": "violet"}})   # 已退场的那五套之一
        self.assertEqual(self.settings._settings_get()["ui"]["theme"], "light")


if __name__ == "__main__":
    unittest.main()
