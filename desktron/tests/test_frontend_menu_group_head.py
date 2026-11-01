"""主引擎菜单独立保留标题层级，不依赖已退役的多侧栏样式。"""
from pathlib import Path
import re
import unittest

FRONT = Path(__file__).resolve().parents[2] / "frontend"


class MenuGroupHeadWeight(unittest.TestCase):
    def test_engine_group_heading_keeps_weight(self):
        css = (FRONT / "style.css").read_text(encoding='utf-8')
        rules = re.findall(r"^#engine-menu \.fm-head\s*\{([^}]+)\}", css, re.M)
        self.assertIn("font-weight: 650", rules[-1])
        self.assertIn("font-size: 14px", rules[-1])
        windows = re.search(r":where\(html.os-windows\) #engine-menu \.fm-head\s*\{([^}]+)\}", css)
        self.assertIsNotNone(windows)
        self.assertIn("font-weight: 700", windows.group(1))

    def test_base_heading_has_no_extra_weight(self):
        css = (FRONT / "style.css").read_text(encoding='utf-8')
        base = re.search(r"^\.fm-head \{[^}]*\}", css, re.M)
        self.assertIsNotNone(base)
        self.assertNotIn("font-weight", base.group())
