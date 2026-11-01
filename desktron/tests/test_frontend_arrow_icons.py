"""可见 UI 箭头使用图标而不是文字箭头。"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
PANEL_TERMS = open(os.path.join(ROOT, "frontend", "app-panel-terms.js"), encoding="utf-8").read()
USAGE = open(os.path.join(ROOT, "frontend", "app-settings-usage.js"), encoding="utf-8").read()
FILE_BROWSER = open(os.path.join(ROOT, "frontend", "pages", "file-browser.html"), encoding="utf-8").read()
ASSETS_CREATE = open(os.path.join(ROOT, "frontend", "app-assets-create.js"), encoding="utf-8").read()


class VisibleArrowIcons(unittest.TestCase):
    def test_通用箭头复用已登记图标资产(self):
        self.assertIn(".ui-arrow-icon", CSS)
        self.assertIn('url("/icons/right-arrow.png")', CSS)
        self.assertIn("width: .68em; height: .68em", CSS)
        self.assertIn(".ui-arrow-icon.is-left", CSS)

    def test_右键二级菜单不再输出文字尖括号(self):
        self.assertIn('const RP_CHEV = \'<span class="rp-chev" aria-hidden="true"></span>\';', PANEL_TERMS)
        self.assertNotIn('class="rp-chev">›</span>', PANEL_TERMS)

    def test_子代理返回入口是图标加主线文字(self):
        open_seg = re.search(r"async function hsAgentOpen\(v, a\)[\s\S]+?hs-agv-b\"></div>';", CHAT).group(0)
        self.assertIn('class="ui-arrow-icon is-left"', open_seg)
        self.assertIn('hsEsc(DeskI18n.t("ctx.main"))', open_seg)
        self.assertNotIn('←', open_seg)

    def test_用量页和文件面包屑不再输出文字箭头(self):
        self.assertIn('class="ui-arrow-icon is-left"', USAGE)
        self.assertIn('class="ui-arrow-icon"', USAGE)
        self.assertNotIn('工作流 ‹', USAGE)
        self.assertNotIn('"›"', USAGE)
        self.assertIn('fb-crumb-arrow', FILE_BROWSER)
        self.assertNotIn('<span>›</span>', FILE_BROWSER)

    def test_接续和设置流程不用文字箭头(self):
        self.assertIn('class="ui-arrow-icon"', ASSETS_CREATE)
        self.assertNotIn("' → '", ASSETS_CREATE)
        self.assertNotIn('content: "→"', CSS)
        self.assertNotIn('content: " →"', CSS)


if __name__ == "__main__":
    unittest.main()
