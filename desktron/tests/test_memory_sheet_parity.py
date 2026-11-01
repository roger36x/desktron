"""两处记忆详情的正文样式要一致(2026-09-01 用户实见:工作流那份是贴边裸文)。

同一件东西有两条实现路径:
  - 会话页右上角那张卡(app-chat-situation):另建正文,复用任务备忘的内衬卡 .wf-note-ed,
    于是有底色、描边、内边距。
  - 工作流的记忆卡(app-settings-memory):借走卡片里那个 .doc 节点(它挂着编辑、溯源、
    保存后重载几套行为,搬节点比重画省事),正文只有裸的 .doc-in。
两条路都有各自的道理,不合并;补的是外观——浮层里的 .doc-in 按 .wf-note-ed 给同一组值。

边界:只在 .wf-sheet-body 下给。卡片内联展开时正文本就在卡片内衬里,再套一层就是卡中卡。
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


def _decl(selector):
    """取某条规则的声明体(第一处匹配)。"""
    m = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{(.*?)\}", CSS, re.S)
    assert m, "找不到规则:" + selector
    return m.group(1)


def _props(body, names):
    got = {}
    for n in names:
        m = re.search(re.escape(n) + r"\s*:\s*([^;]+);", body)
        if m:
            got[n] = m.group(1).strip()
    return got


SHEET_DOC = ".wf-sheet-body > .doc > .doc-in"
SAME = ("background", "border", "border-radius", "padding", "font-size", "line-height")


class 两处一致(unittest.TestCase):
    def test_浮层里的正文有内衬卡(self):
        got = _props(_decl(SHEET_DOC), SAME)
        for n in SAME:
            self.assertIn(n, got, "缺 " + n + ":正文会贴着浮层边缘,就是用户报的那个样子")

    def test_取值与会话那份逐项相同(self):
        """以会话那份为准(用户定)。逐项比而不是只看有没有——差一个 padding
        就是两种观感,而人会以为是两个功能。"""
        ref = _props(_decl(".wf-note-ed"), SAME)
        got = _props(_decl(SHEET_DOC), SAME)
        for n in SAME:
            self.assertEqual(got.get(n), ref.get(n), n + " 与会话那份不一致")


class 不越界(unittest.TestCase):
    def test_只作用于浮层(self):
        """卡片内联展开时正文已在卡片内衬里,再套一层是卡中卡。"""
        self.assertTrue(SHEET_DOC.startswith(".wf-sheet-body"))
        # 不能写成裸的 .doc-in:那会命中记忆列表里每一张收起的卡
        self.assertNotRegex(CSS, r"(?m)^\.doc-in\s*\{[^}]*border\s*:")

    def test_基准没被改动(self):
        """基准是任务备忘的内衬卡 .wf-note-ed。基准跟着变的话,这条对齐就成了互相追着跑。

        原来还钉着会话卡那条浮层(.wf-sheet .hs-sit-memory-doc)——它是当时的取值来源。
        2026-09-10 会话卡的「记忆」整块换成了「制品」,那条浮层与它的样式一并撤下,
        这里只剩基准本身。"""
        self.assertIn(".wf-note-ed {", CSS)
        self.assertNotIn("hs-sit-memory", CSS)


if __name__ == "__main__":
    unittest.main()
