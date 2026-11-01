"""聊天框上沿那条任务栏:取数失败不等于没有任务(2026-08-26 手机实见,间歇消失)。

链路是 /session 查归属 → 取该工作流任务 → 画栏。原来任何一步失败都直接藏栏,
又只在回合收尾时再同步——一次瞬时超时(经隧道 P90 到过 2.2s,api 上限 8s)就是
「消失到下次对话为止」。手机上收发消息都正常,唯独这条栏没了,正是这个形态。

钉五件:错误与「确认没有」分开(null vs "");失败不藏已显示的栏;失败后有重试;
失败有日志(下次出现要查得到,不能再靠猜);工作流没有任务时栏仍常驻。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()


def seg(name, upto=r"\n(?:async )?function "):
    m = re.search(r"(?:async )?function %s\([\s\S]+?(?=%s)" % (name, upto), CHAT)
    assert m, name + " 不见了"
    return m.group(0)


class Resilience(unittest.TestCase):
    def test_错误与确认无归属分开(self):
        """/session 失败返回 null;真查到「这段会话不属于任何工作流」才是空串。"""
        s = seg("hsSessionDomain")
        self.assertIn("return null", s)

    def test_失败走保留而不是隐藏(self):
        s = seg("hsTasksSync")
        self.assertIn('dom === null', s)
        m = re.search(r"dom === null.*?\{(.+?)\}", s)
        self.assertIn("hsTasksBail", m.group(1), "session 失败该走保留+重试,不是藏栏")
        m2 = re.search(r"catch \{ (\w+)\(v, box", s)
        self.assertEqual(m2.group(1), "hsTasksBail", "取任务失败同理")

    def test_只有确认无归属才隐藏(self):
        """非工作流会话不显示；工作流即使没有在办任务也保留入口。"""
        s = seg("hsTasksSync")
        self.assertIn('if (!dom) { hsTasksHide(v, box); return; }', s)
        self.assertNotIn("if (!items.length)", s)
        self.assertIn("box.hidden = false", s)

    def test_失败不动已显示的栏(self):
        """hsTasksBail 里不许出现 hidden = true / hsTasksHide:保留上一份,别闪没。"""
        s = seg("hsTasksBail")
        self.assertNotIn("hsTasksHide", s)
        self.assertNotIn("hidden = true", s)

    def test_失败后重试且不叠加(self):
        s = seg("hsTasksBail")
        self.assertIn("setTimeout", s)
        self.assertIn("hsTasksSync(v)", s)
        self.assertIn("if (v._tasksRetryT) return", s, "重试定时器要防叠加")
        self.assertIn("isConnected", s, "视图已拆就别再同步")

    def test_失败有日志(self):
        """间歇性问题全靠日志留痕:这次就是因为没日志,只能从代码反推。"""
        self.assertIn("deskLog", seg("hsTasksBail"))


if __name__ == "__main__":
    unittest.main()
