"""从会话里就地记一条任务:/task。

工作流会话的任务栏即使没有在办任务也常驻，展开即可创建第一条。/task 仍保留为键盘入口：
已经知道标题时不必先展开清单。写回走共用的 taskAdd,与侧栏、工作流页同一份 TASKS.md。
"""
import os
import re
import unittest

import _srcmap
from engine_adapters.command_manifest import resolve

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()
CORE = open(os.path.join(ROOT, "frontend", "app-core.js"), encoding="utf-8").read()
ADD = re.search(r"async function hsTaskQuickAdd\(v, args\)[\s\S]+?\n}", CHAT).group(0)


class SlashTask(unittest.TestCase):
    def test_命令登记在通用那一组(self):
        """与引擎无关:哪家引擎的会话都该能记任务。"""
        for engine in ('claude','codex','pi'):
            rows = resolve(engine=engine, shepherd=True, caps={})['commands']
            task = next(row for row in rows if row['name'] == '/task')
            self.assertEqual((task['owner'],task['transport'],task['history']),
                             ('desk','endpoint','none'))

    def test_写回走共用那条路(self):
        self.assertIn("await taskAdd(dom, title)", ADD)
        self.assertIn("function taskAdd(domainId, title)", CORE)

    def test_没有标题时说用法(self):
        self.assertIn('hsErrorNotice(v, "用法：/task 任务标题", "command")', ADD)
        self.assertNotIn('hsStatus(v, "error"', ADD)

    def test_不属于工作流的会话明说记不了(self):
        """会话可以不挂在任何工作流下,那时没有 TASKS.md 可写。"""
        self.assertIn("这段会话不属于任何工作流", ADD)

    def test_记完立刻同步任务栏(self):
        """任务栏已经常驻，记完仍需立刻刷新内容。"""
        self.assertIn("hsTasksSync(v)", ADD)


class UpdateErrorSaysWhat(unittest.TestCase):
    def test_404说成没重启(self):
        """这个端点是新加的,旧进程里根本不存在;把 404 原样摆出来等于让人去猜。"""
        seg = re.search(r"async function hsEngineUpdate\(v, engine, btn\)[\s\S]+?\n}", CHAT).group(0)
        self.assertIn("后端还在跑旧代码", seg)
        self.assertIn("/\\b404\\b/.test(msg)", seg)


if __name__ == "__main__":
    unittest.main()
