"""步骤行末尾那两个增删计数(+N / -M),底边要与同一行别的字齐平。

基线本身早就是对的:.hs-step-h 用 align-items:baseline,而 .hs-step-a(文件名)与
.hs-step-c(计数)同字体同字号。4 倍图上逐块量墨迹,文件名里那个句点的底与 +70 的底
都落在同一行,就是基线。

看着没对齐的是墨迹底边。这一行里别的东西都探到基线以下——「已编辑」是汉字,重心低、
下沿到基线下 1.25px;文件名带 _ y p 这类降部,下沿到 2px。只有这两个数字没有降部,
底边正好停在基线上,于是整块看着比左边高一截(2026-08-24 用户实报)。

下移 1.25px 把它的底边对到汉字的下沿。这是光学补偿,不是改基线——同 .pv-state svg
那一处的做法(那里是汉字重心低于行盒中心,对勾下移 1.5px)。

实测(4 倍图,墨迹底边):
    改前  已编辑 191 / 句点(基线) 186 / 含降部 194 / +70 186
    改后  已编辑 191 / 句点(基线) 186 / 含降部 194 / +70 191
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


def rule(sel):
    hits = re.findall(r"\n%s \{[^}]*\}" % re.escape(sel), CSS)
    assert hits, "找不到 %s" % sel
    return hits[-1]


class 计数块的对齐(unittest.TestCase):
    def test_下移做光学补偿(self):
        self.assertIn("transform: translateY(1.25px)", rule(".hs-page .hs-step-c"))

    def test_没有改基线(self):
        """补偿只动 transform。改 vertical-align 或 position 会牵动行盒,
        把同一行别的东西一起带偏。"""
        r = rule(".hs-page .hs-step-c")
        self.assertNotIn("vertical-align", r)
        self.assertNotIn("position", r)

    def test_容器仍按基线排(self):
        """底边靠补偿凑齐,基线该对的还得对——两者是两回事,不能拿 align-items 顶替。"""
        self.assertIn("align-items: baseline", rule(".hs-page .hs-step-h"))

    def test_与文件名同字体同字号(self):
        """字号一旦分家,基线相同也会看出高低差,补偿量就得重算。"""
        c, a = rule(".hs-page .hs-step-c"), rule(".hs-page .hs-step-a")
        for k in ("font-family: var(--mono-action)", "font-size: 13px"):
            self.assertIn(k, c)
            self.assertIn(k, a)


if __name__ == "__main__":
    unittest.main()
