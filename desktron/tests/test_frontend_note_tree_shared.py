"""会话任务栏与工作流页共用同一套任务详情浮层。"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
CHAT = _srcmap.chat_src()
PANEL = _srcmap.panel_src()


def rule(sel):
    # 只取完整的基准选择器，不能把专用阅读面中的后代选择器当成共用壳。
    m = re.search(r"(?m)^" + re.escape(sel) + r"\s*\{([^}]*)\}", CSS)
    assert m, "没有这条规则:" + sel
    return m.group(1)


def fn(src, name):
    m = re.search(r"\n(?:async )?function %s\(.*?\n}" % re.escape(name), src, re.S)
    assert m, "%s 不见了" % name
    return m.group(0)


class WorkflowStyleIsTheBaseline(unittest.TestCase):
    def test_任务树沿用工作流排版(self):
        base = rule(".wf-note-tree")
        self.assertIn("dashed", base)
        self.assertIn("var(--surface-inset)", base)
        row = rule(".wf-node-row")
        self.assertIn("padding: 6px 6px", row)
        self.assertIn("border-radius: 8px", row)
        self.assertIn("cursor: pointer", row)
        self.assertIn("padding: 2px 6px 8px 21px", rule(".wf-node-child"))

    def test_详情浮层负责统一宽高与滚动(self):
        self.assertIn("width: min(720px, 100%)", rule(".wf-sheet"))
        self.assertIn("max-height: min(var(--wf-sheet-max-h), 100%)", rule(".wf-sheet"))
        self.assertIn("overflow: auto", rule(".wf-sheet-body"))


class OneTaskDetailRenderer(unittest.TestCase):
    def test_任务栏直接调用工作流详情(self):
        seg = fn(CHAT, "hsTasksSync")
        self.assertIn("wfOpenTaskSheet(t, dom)", seg)
        self.assertNotIn("hsTaskMemo", seg)

    def test_两入口最终复用单一Markdown详情(self):
        seg = fn(PANEL, "wfOpenTaskSheet")
        detail = fn(PANEL, "_wfTaskMarkdownMount")
        self.assertIn("_wfTaskMarkdownMount(area, domId, t", seg)
        self.assertIn("md.render(detail.markdown)", detail)
        self.assertIn('_wfMemoPost(domId, t, "memo-get")', detail)
        self.assertNotIn("_wfNoteEd(", detail)
        self.assertNotIn("_wfTaskFlowMount(area, t, domId)", seg)

    def test_旧只读卡片实现已删除(self):
        self.assertNotIn("function hsTaskMemo", CHAT)
        self.assertNotIn('className = "hs-memo"', CHAT)
        self.assertNotIn(".hs-memo", CSS)

    def test_操作详情时不误收起任务列表(self):
        seg = fn(CHAT, "hsTasksOutside")
        self.assertIn('t.closest(".wf-sheet")', seg)


if __name__ == "__main__":
    unittest.main()
