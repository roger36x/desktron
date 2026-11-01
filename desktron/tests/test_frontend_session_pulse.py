"""会话在跑这件事,从行首那枚点搬到会话名自己的呼吸上。

原来是「状态归点、标题只管读」:在跑的点带一圈呼吸的晕,没在跑的是素灰点。改成标题呼吸之后
那枚点没有别的话要说了,隐去。

点与它的盒子一并收掉。头一版只隐了点、留着 12px 的空盒,那段空白把会话名推得比工作流名
靠右 15px;而工作流名、会话名、「展开显示」这三者本该落在同一条左缘上(38)。收掉之后每一档
少了 12px 盒宽加 5px gap,各处行内距分别补 17px 把原位置还回去,只有侧栏工作流那一档补到
24px——它要对齐的是那条线,不是原来的位置。.ti-pt 那几条样式仍留着:它们记着这枚点当初
是怎么调的,真要退回来去掉一行即可。

选择器从 .ti-stat 的状态类横过来(~):状态挂在它身上(见 termState),.ti-label 是它后面的
兄弟,够得着,不必为这一处改 DOM。用 opacity 不用 color——侧栏标题色深浅两套不同
(浅色 --sess-title、深色 --ink-89),写成颜色关键帧要维护两份;这里只有一个元素在动,
没有状态行那种「点与文字要同频同相」的问题。

无头浏览器实测(造四种状态的行):
    点 display=none,盒子仍占位;
    working / agents 的 .ti-label 挂着 ti-title-breathe,idle / dormant 没有;
    同在 .ti-main 里的分支名一律不动;
    在跑那一行连采三帧 opacity:0.97 → 0.55 → 0.43。
"""
import os
import re
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
PANEL = _srcmap.panel_src()


class 点隐去(unittest.TestCase):
    def test_点与盒子一并收掉(self):
        self.assertIn(".term-item .ti-stat { display: none; }", CSS)

    def test_行内距把盒子那份补了回去(self):
        """收掉盒子每档少 17px(12 盒宽 + 5 gap)。侧栏工作流那一档不还原位置,
        改成对齐 .pf-name 的 38;其余各档原样补回,视觉不动。"""
        self.assertIn("#panel-list .proj-sessions .term-item { padding-left: 24px; }", CSS)
        self.assertIn("#panel-list .loose-sessions .term-item { padding-left: 27px; }", CSS)
        self.assertNotIn(".wf-sess .term-item", CSS)
        self.assertNotIn(".wf-tsess .term-item", CSS)

    def test_展开显示跟着会话行走(self):
        """三者左缘都落在 38:工作流名、会话名、「展开显示」。"""
        self.assertIn(".sess-more, #panel-list .proj-sessions .group-empty { padding-left: 24px; }", CSS)

    def test_原来那套样式留着(self):
        """记着这枚点当初怎么调的;退回来只要去掉一行。"""
        self.assertIn(".term-item .ti-stat .ti-pt::after", CSS)
        self.assertIn("@keyframes ti-halo", CSS)


class 在跑的标题呼吸(unittest.TestCase):
    def _rule(self):
        m = re.search(r"\.term-item \.ti-stat\.st-working ~ \.ti-main \.ti-label,\s*"
                      r"\.term-item \.ti-stat\.st-agents ~ \.ti-main \.ti-label \{[^}]*\}", CSS)
        self.assertTrue(m, "找不到标题呼吸那条规则")
        return m.group(0)

    def test_两种在跑都算(self):
        """st-agents 是主线答完、子代理还在后台跑,对人是同一件事。"""
        r = self._rule()
        self.assertIn("st-working", r)
        self.assertIn("st-agents", r)
        self.assertIn("ti-title-breathe", r)

    def test_只给标题不给分支名(self):
        """动的该是「哪段会话在跑」,不是它的元信息。"""
        self.assertNotIn(".ti-main .ti-branch { animation", CSS)
        self.assertNotIn("~ .ti-main { animation", CSS)

    def test_两端确实拉开(self):
        m = re.search(r"@keyframes ti-title-breathe \{([^}]*\}[^}]*)\}", CSS)
        self.assertTrue(m)
        vals = re.findall(r"opacity:\s*([\d.]+)", m.group(1))
        self.assertEqual(len(vals), 2)
        self.assertGreaterEqual(abs(float(vals[0]) - float(vals[1])), 0.4,
                                "幅度太小,一眼分不出它在动还是停着")


class 状态本身没动(unittest.TestCase):
    def test_五档判据照旧(self):
        seg = re.search(r"function termState\(s\)[\s\S]+?\n}", PANEL).group(0)
        for st in ("working", "agents", "dormant", "unread", "idle"):
            self.assertIn('"%s"' % st, seg)

    def test_悬停说明还在(self):
        """点看不见了,那 18px 仍是热区,说明照旧给。"""
        self.assertIn("function termStatTip", PANEL)
        self.assertIn('title="${escapeHtml(termStatTip(s))}"', PANEL)


if __name__ == "__main__":
    unittest.main()
