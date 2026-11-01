"""会话正文与输入区必须由同一条 CSS 内容轨同步排版。

旧实现读取 .hs-stream 的矩形，再把宽度和左偏移逐项写给目标、任务、环境与输入框。
侧栏拖动或窗口缩放时，正文先随 flex 重排，输入区下一帧才追上，因而出现分开变化。
现在各块共用 .hs-col 与同一组 padding / margin 变量，浏览器一次布局即可完成。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
CHAT = _srcmap.chat_src()

ROWS = ("hs-stream", "hs-goal", "hs-context", "hs-env", "hs-box", "hs-qp")


class 同一内容轨(unittest.TestCase):
    def test_正文列宽度只定义一处(self):
        self.assertEqual(len(re.findall(r"--hs-col:\s*\d+px", CSS)), 1)

    def test_正文与输入区各块都挂内容轨(self):
        for cls in ROWS:
            self.assertIn(f"{cls} hs-col", CHAT)

    def test_系统提示状态更新后仍留在内容轨(self):
        self.assertIn('el.className = "hs-env hs-col"', CHAT)
        self.assertIn('el.className = "hs-env hs-col " + state', CHAT)

    def test_系统提示自身也消费内容轨(self):
        block = re.search(r"\.hs-page \.hs-env \{[\s\S]*?\n\}", CSS)
        self.assertIsNotNone(block)
        for declaration in (
            "max-width: var(--hs-col)",
            "margin: 0 var(--hs-col-mr) 5px var(--hs-col-ml)",
        ):
            self.assertIn(declaration, block.group(0))

    def test_内容轨统一消费列宽和横向位置(self):
        block = re.search(r"\.hs-page \.hs-col \{[\s\S]*?\n\}", CSS)
        self.assertIsNotNone(block)
        for declaration in (
            "width: 100%",
            "max-width: var(--hs-col)",
            "margin-left: var(--hs-col-ml)",
            "margin-right: var(--hs-col-mr)",
        ):
            self.assertIn(declaration, block.group(0))

    def test_正文和输入根使用同一组沟槽变量(self):
        self.assertIn("padding: 0 var(--hs-pad-r) 0 var(--hs-pad-l)", CSS)
        self.assertIn("padding: 4px var(--hs-pad-r) 20px var(--hs-pad-l)", CSS)

    def test_不再逐项量取和写入行内像素(self):
        self.assertNotIn("function hsAlign", CHAT)
        self.assertNotIn("alignLater", CHAT)
        self.assertNotIn("target.style.marginLeft", CHAT)


class 回到最新也在内容轨(unittest.TestCase):
    def test_按钮通过轨道居中(self):
        self.assertIn('downTrack.className = "hs-down-track hs-col"', CHAT)
        self.assertIn(".hs-page .hs-down-track { display: flex; justify-content: center; }", CSS)
        self.assertIn("left: var(--hs-pad-l); right: var(--hs-pad-r)", CSS)

    def test_按钮不再写绝对横坐标(self):
        self.assertNotIn("down.style.left", CHAT)


if __name__ == "__main__":
    unittest.main()
