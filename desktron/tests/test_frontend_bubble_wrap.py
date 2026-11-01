"""用户气泡里的无空格长串会冲出气泡右缘,后半截看不见。

粘进来的东西常常一个空格都没有——数据库 DSN、token、长 URL 都是。气泡只设了
white-space:pre-wrap,它在空白处断,断不了就一路排下去。而气泡的宽度由 .hs-u 的
max-width:84% 定死,跟窗口多宽无关,于是桌面上照样溢出(2026-08-26 用户实见,
CDP 在 900px 容器里量出溢出 242px:内容宽 964、可视宽 722)。

窄屏那条同款规则(媒体查询里的 .hs-page .hs-turn *)早就有了,当时的判断是
「桌面宽,断不断都读得完」——这个前提对气泡不成立,气泡不吃窗口宽度。

补 overflow-wrap:anywhere 之后溢出归零,短消息仍随内容收窄(实测「好的」的气泡 55px)
——anywhere 只改变最小内容宽度的算法,不影响 max-content,而气泡的宽度取的正是
min(max-content, 84%)。
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


class 气泡里的长串要能断(unittest.TestCase):
    def _bubble(self):
        m = re.search(r"\n\.hs-page \.hs-bubble \{([^}]*)\}", CSS)
        self.assertTrue(m, "没找到 .hs-page .hs-bubble 规则")
        return m.group(1)

    def test_气泡有overflow_wrap(self):
        self.assertIn("overflow-wrap: anywhere", self._bubble())

    def test_pre_wrap仍在(self):
        """换行规则是加在 pre-wrap 之上的,不是替换它——粘进来的多行文本要保住原有换行。"""
        self.assertIn("white-space: pre-wrap", self._bubble())

    def test_不是只挂在窄屏(self):
        """回归防护:把它挪进媒体查询,桌面上那 242px 溢出原样复现。

        判据是「这条规则没有缩进」——这个样式表里,媒体查询内的规则一律缩进两格
        (例如窄屏那条 `  .hs-page .hs-turn, .hs-page .hs-turn * { overflow-wrap: anywhere; }`)。
        上面 _bubble() 的正则要求 \n 之后紧跟 . ,已经隐含了这一点;这里写明,免得
        以后有人把正则放宽、这道防护跟着失效。"""
        self.assertIsNotNone(re.search(r"\n\.hs-page \.hs-bubble \{", CSS), "气泡规则不在顶层了")
        self.assertIsNone(re.search(r"\n[ \t]+\.hs-page \.hs-bubble \{", CSS),
                          "气泡规则被挪进了媒体查询")


if __name__ == "__main__":
    unittest.main()
