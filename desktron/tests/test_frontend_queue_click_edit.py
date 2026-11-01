"""排队消息里最后一条(队尾)点击即回输入框重编辑,与 ↑ 键完全一致。

原来这条路只有键盘能走(hsQPop 挂在 ArrowUp/`p` 上),触屏没有物理键位,排错了的排队
消息在手机上只能整条撤销重打,改不了字(2026-08-24 用户实报)。

只给最后一条挂点击,不是全部:hsQPop 本身也只弹队尾那条,点其余几条会跟它的语义对不上
——点了却编辑的是另一条。× 按钮要阻止冒泡,否则点它会先触发行的 onclick 把队尾那条
(不一定是被点的这条)取回输入框,再执行删除,行为对不上用户点的到底是哪个按钮。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
PAINT = re.search(r"function hsQPaint\(v\)[\s\S]+?\n}", CHAT).group(0)


class 只有队尾可点(unittest.TestCase):
    def test_正常队列不再显示冗余标题(self):
        self.assertNotIn("待发送 ·", PAINT)
        self.assertNotIn("这条消息是否送达需要核对", PAINT)
        self.assertNotIn("hs-queue-head", PAINT)
        self.assertNotIn("hs-queue-action", CSS)

    def test_判据是最后一条(self):
        self.assertIn("if (i === list.length - 1) {", PAINT)

    def test_点击走的是hsQPop(self):
        """与 ↑ 键同一个函数,行为不能分叉成两套。"""
        m = re.search(r"row\.onclick = \(e\) => \{[^}]*\};", PAINT)
        self.assertTrue(m, "队尾行没有挂 onclick")
        self.assertIn("hsQPop(v)", m.group(0))

    def test_叉不触发编辑(self):
        m = re.search(r"x\.onclick = \(e\) => \{[^}]*\};", PAINT)
        self.assertTrue(m, "找不到叉的 onclick")
        self.assertIn("e.stopPropagation()", m.group(0))

    def test_点叉不误判成点了编辑区(self):
        m = re.search(r"row\.onclick = \(e\) => \{[^}]*\};", PAINT)
        self.assertIn('e.target.closest(".hs-q-x")', m.group(0))


class 与上键同语义(unittest.TestCase):
    def test_hsQPop没有变(self):
        """点击复用的是这一个函数;它对「手上有草稿」「弹队尾那条」的判断不用重写一遍。"""
        pop = re.search(r"function hsQPop\(v\)[\s\S]+?\n}", CHAT).group(0)
        self.assertIn("hsQEdit(v, (v.queue || []).length - 1)", pop)
        edit = re.search(r"function hsQEdit\(v, index\)[\s\S]+?\n}", CHAT).group(0)
        self.assertIn("hsInpText(v.inp).trim()", edit)


class 样式给出可点提示(unittest.TestCase):
    def test_有指针光标(self):
        self.assertIn(".hs-page .hs-q-editable { cursor: pointer; }", CSS)

    def test_悬停是叠加不是换绝对灰阶(self):
        """与 .hs-mode 悬停同源:两个主题都是「比行底深一档」,不写死某一档灰。"""
        m = re.search(r"\.hs-page \.hs-q-editable:hover \{[^}]*\}", CSS)
        self.assertTrue(m)
        self.assertIn("--lift-07", m.group(0))
        self.assertNotIn("var(--ink-", m.group(0))


if __name__ == "__main__":
    unittest.main()
