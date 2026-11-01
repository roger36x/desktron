"""整片重建之后,节点要接回文档,不能塞进一个看不见的旧盒子。

现象(2026-08-16 用户实见):一个回合还没跑完、已经出了一些正文,这时刷新——正文不见了,
过程区也点不开,刷几次才偶尔正常,而正文要等回合跑完才回来。

病灶在「节点还能不能用」的判据:`t.procEl.parentNode` 判的是「有没有父节点」。整片重建
(replaceChildren / replaceWith)把旧的那一片换走之后,旧盒子仍挂在那片已脱离文档的节点上,
parentNode 不为空——于是它被当成可用的,新画的步骤与正文全塞了进去,页面上什么也看不见。
诊断日志里的形状是「parts=30 步骤=3」:数据都在,DOM 落在了别处。

判据换成 `holder.contains(el)`——问的是「这个节点是不是我此刻要画进去的那个容器里的」,
并给三类块补一条「不在当前容器里就接回来」:接回而不是新建——已经画好的内容与后续增量都挂在
原节点上,新建等于把那一段丢掉。

中间试过 isConnected,一天后翻车(2026-08-17):往更早翻页那一路(hsPrepend)先在一个游离的
scratch 容器里铺好再整体搬进去,那期间整棵都不在文档里,照 isConnected 判就是每画一个 part
建一个新盒子——一屏十几条分割线,刷新之后才恢复。contains 两种情形都对。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()


def fn(name):
    m = re.search(r"\n(?:async )?function %s\(.*?\n}" % re.escape(name), CHAT, re.S)
    assert m, "%s 不见了" % name
    return m.group(0)


class ReattachOnReplay(unittest.TestCase):
    def test_过程区按在不在当前容器里判(self):
        seg = fn("hsProcBox")
        self.assertIn("t.holder.contains(t.procEl)", seg)
        self.assertNotIn("t.procEl && t.procEl.parentNode", seg)
        self.assertNotIn("t.procEl.isConnected", seg)      # 离屏铺页时它恒为假

    def test_正文块不在当前容器就接回(self):
        seg = fn("hsPaint")
        self.assertIn("!part.turn.holder.contains(part.el)", seg)
        self.assertIn("hsTextAttach(part.turn, part.el);", seg)

    def test_思考与工具也接回(self):
        self.assertEqual(CHAT.count("// 同正文那一支:不在当前容器里就接回来"), 2)
        think = fn("hsPaint")
        tool = fn("hsPaintTool")
        self.assertIn("!hsProcHost(part.turn).contains(part.el)", think)
        self.assertIn("const target = host || hsProcHost(part.turn)", tool)
        self.assertIn("!target.contains(part.el)", tool)

    def test_三处判据一致_都不用isConnected(self):
        """三类块只要有一处判错,那一类就会重复建节点(过程区重复 = 一屏分割线)。

        只看代码行,不看注释——注释里正记着「为什么不用 isConnected」,连注释一起断会自伤。
        """
        for name in ("hsProcBox", "hsPaint", "hsPaintTool"):
            code = "\n".join(l for l in fn(name).splitlines()
                              if not l.strip().startswith("//"))
            self.assertNotIn("isConnected", code, name + " 里还留着 isConnected")

    def test_接回不是新建(self):
        # 新建会丢掉已经画好的内容与挂在原节点上的后续增量
        seg = fn("hsPaint")
        i = seg.index("!part.turn.holder.contains(part.el)")
        self.assertNotIn("document.createElement", seg[i:i + 260])


if __name__ == "__main__":
    unittest.main()
