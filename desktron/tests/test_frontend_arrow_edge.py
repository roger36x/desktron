"""上下键翻历史,只在光标真的到了第一行 / 最后一行时才接管。

判据原来是「光标前面还有没有文字」(Range.toString().length === 0)。而 Range.toString()
不把 <br> 与空的块算成换行:输入框上方全是空行时它读出 0,人想按 ↑ 回到上面那个空行,
却翻出了上一条指令,正在写的这条被整段覆盖(2026-08-20 用户实见)。

换成按几何位置判:光标矩形与容器顶/底的距离小于半行才算到边。软折行顺带也管住了——
一句话折成两行,在第二个视觉行按 ↑ 该回到第一行,而那时光标前面明明有文字,老判据同样答不对。

无头浏览器逐个位置实测(↑翻历史 / ↓翻历史):
    空输入框            True  / True
    单行·行首/行中        True  / True
    上面是空行·第二行行首   False / True     ← 本次的缺陷
    两行·在第一行         True  / False
    两行·在第二行         False / True
    软折行·第一视觉行      True  / False
    软折行·末视觉行       False / True
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()
EDGE = re.search(r"function hsAtEdge\(el, back\)[\s\S]+?\n}", CHAT).group(0)


class EdgeIsGeometric(unittest.TestCase):
    def test_按光标矩形判(self):
        self.assertIn("sel.getRangeAt(0).getBoundingClientRect()", EDGE)
        self.assertIn("el.getBoundingClientRect()", EDGE)

    def test_不再数光标前面的文字(self):
        """空行不产生文字,数出来永远是 0——缺陷就在这儿。"""
        self.assertNotIn("probe.toString()", EDGE)
        self.assertNotIn("selectNodeContents", EDGE)

    def test_留半行余量(self):
        """行内还有 padding 与基线偏移,严格相等靠不住。"""
        self.assertIn("const half = line * 0.5;", EDGE)
        self.assertIn("parseFloat(cs.lineHeight)", EDGE)

    def test_空输入框仍算边界(self):
        """没有任何行盒时量不到光标矩形,那本来就是边界——不然空框里按 ↑ 也翻不出历史。"""
        self.assertIn("if (!cr || (!cr.height && !cr.top)) return true;", EDGE)

    def test_两个方向各自判(self):
        seg = EDGE[EDGE.index("if (back)"):]
        self.assertIn("cr.top - (box.top", seg)
        self.assertIn("box.bottom", seg)

    def test_接管的仍是那两个键(self):
        self.assertIn('if (ev.key === "ArrowUp" && hsAtEdge(v.inp, true)) return hsQPop(v) || hsHistory(v, -1);', CHAT)
        self.assertIn('if (ev.key === "ArrowDown" && hsAtEdge(v.inp, false)) return hsHistory(v, 1);', CHAT)


if __name__ == "__main__":
    unittest.main()
