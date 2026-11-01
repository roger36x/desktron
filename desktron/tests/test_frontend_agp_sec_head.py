"""代理页下线后不再保留 agp 段头样式。"""
import os
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
PANEL = _srcmap.panel_src()


class 代理页段头已下线(unittest.TestCase):
    def test_旧_agp_段头样式不再存在(self):
        for marker in (".agp-sec-head", ".agp-sec-act", ".agp-resource-head"):
            self.assertNotIn(marker, CSS)

    def test_安排系统卡使用独立命名空间(self):
        for marker in (".arr-sys-head", ".arr-sys-resource-head", ".arr-sys-status-grid"):
            self.assertIn(marker, CSS)
        self.assertIn("_arrSysCards", PANEL)
        self.assertNotIn("_agpSysCards", PANEL)


if __name__ == "__main__":
    unittest.main()
