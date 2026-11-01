"""分批补历史时，附属投影必须计入同一次滚动补偿。"""
import os
import re
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PAGE = open(os.path.join(ROOT, "frontend", "app-chat-page.js"), encoding="utf-8").read()


def fn(name):
    match = re.search(r"\nfunction %s\(.*?\n}" % re.escape(name), PAGE, re.S)
    assert match, name
    return match.group(0)


class 历史投影与滚动补偿同批提交(unittest.TestCase):
    def test_审批回执先投影_再按完整高度补偿(self):
        segment = fn("hsPrepend")
        inserted = segment.index("real.insertBefore(frag, real.firstChild)")
        projected = segment.index("apprRenderView(v)")
        compensated = segment.index("return hsViewportChange(v, prepend)")
        self.assertIn("const prepend = () => {", segment)
        self.assertLess(inserted, projected)
        self.assertLess(projected, compensated)

    def test_仍以插入前位置和最终高度差补偿(self):
        segment = fn("hsPrepend")
        self.assertIn("hsViewportChange(v,", segment)
        self.assertEqual(segment.count("v.body.scrollTop ="), 0)
        self.assertNotIn("scrollIntoView", segment)


if __name__ == "__main__":
    unittest.main()
