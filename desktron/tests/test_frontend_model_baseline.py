"""模型名(.hs-m1 14px)与档位(.hs-m2 13.5px)按字形基线对齐,不是按行盒居中。

原来 .hs-model 用 align-items:center,两段不同字号的文字按各自行盒(含 line-height
留白)的中心对齐,不是按字形基线——这次实测两者的行盒高度恰好都是 21px,基线才
巧合地几乎重合(差 0.33px)。这件事不该靠巧合成立:字体度量跟着系统字体走、或者
哪天字号再调一次,这 0.33px 就可能变成肉眼可见的错位(2026-08-24 用户实报,看着
没对齐)。改成 baseline 后两者的差从 0.33px 精确收到 0。

模型入口只保留两段文字，不再显示下拉箭头。
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


class 模型格的对齐(unittest.TestCase):
    def test_按基线对齐不是居中(self):
        m = re.search(r"\n\.hs-page \.hs-model \{[^}]*\}", CSS)
        self.assertTrue(m, "找不到 .hs-model 的规则")
        self.assertIn("align-items: baseline", m.group(0))
        self.assertNotIn("align-items: center", m.group(0))

    def test_模型入口不再保留箭头样式(self):
        self.assertNotIn(".hs-page .hs-model svg", CSS)


if __name__ == "__main__":
    unittest.main()
