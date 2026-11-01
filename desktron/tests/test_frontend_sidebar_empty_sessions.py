"""侧栏空会话状态不重复铺操作，创建入口收进稳定位置。"""
import os
import unittest

import _srcmap


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SIDEBAR = _srcmap.rail_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


class SidebarEmptySessions(unittest.TestCase):
    def test_工作流空态不再占一整行(self):
        self.assertNotIn('sessionEmptyAction(', SIDEBAR)
        self.assertNotIn('e.textContent = "还没有会话"', SIDEBAR)
        self.assertIn('if (section) section.hidden = true;', SIDEBAR)

    def test_聊天空态只展示真实会话(self):
        self.assertIn('if (!list.length) return;', SIDEBAR)
        self.assertNotIn('e.textContent = "没有会话"', SIDEBAR)

    def test_工作流标题没有空态专用按钮(self):
        self.assertNotIn('proj-new-chat', SIDEBAR)
        self.assertNotIn('pf-no-sessions', SIDEBAR)
        self.assertNotIn('proj-new-chat', CSS)
        self.assertIn('{ ico: WF_MENU_ICONS.conversation, label: DeskI18n.t("wf.newConversation"), fn: () => newTerminal(curEngine, p.id) }', SIDEBAR)


if __name__ == "__main__":
    unittest.main()
