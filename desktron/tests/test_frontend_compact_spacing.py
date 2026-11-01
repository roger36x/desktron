"""压缩完成分隔线应与上下内容保持对称距离。"""
import re
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
STYLE = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")


class CompactSpacing(unittest.TestCase):
    def test_compact_divider_has_symmetric_vertical_margin(self):
        match = re.search(r"\.hs-page \.hs-cmp\s*\{([^}]*)\}", STYLE)
        self.assertIsNotNone(match)
        self.assertRegex(match.group(1), r"\bmargin:\s*18px\s+0\s*;")


if __name__ == "__main__":
    unittest.main()
