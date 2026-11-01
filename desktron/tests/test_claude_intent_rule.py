"""claude 的 Bash description 是模型自愿填的软字段,前端 hsToolObj 优先拿它当摘要;
没填就退回猜词表或摆命令原文——长命令、检索式截断在屏幕右边,读的人得自己解码。

2026-08-24 实测「desktron rc」这个会话最新一回合:同一个 sonnet-5、一字不差的
opts(model/effort/agent/mode 全同),46 次 Bash 调用 description 100% 缺失;同一模型
在另一段会话里是 34/34 100% 命中。没能定位到具体是哪一段上下文触发的,大概率是长会话/
多次压缩后的状态漂移,不是这个模型不具备这个能力——既然定位不到触发条件,就不等下次
复现,把软字段改成系统提示里的硬性要求兜底,与 codex 家族的 INTENT_RULE 同一处境
(codex 的工具本来就没有 description 字段,只能整段让模型手写 `# 意图`)。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv


class Bus:
    def publish(self, e):
        pass


class ClaudeIntentRule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, Bus())

    def tearDown(self):
        self.tmp.cleanup()

    def _prompt(self, opts=None):
        """--append-system-prompt 后面紧跟的那一项就是系统提示正文。"""
        cmd = self.ad.build_cmd("你好", "", opts or {})
        return cmd[cmd.index("--append-system-prompt") + 1]

    def test_起手提示里带上意图规则(self):
        body = self._prompt()
        self.assertIn("`description`", body)
        self.assertIn("十来个中文字", body)

    def test_规则说的是入参字段不是命令行注释(self):
        """claude 的 Bash 工具原生带 description 入参,不同于 codex 家族要靠命令行首行
        `# 意图` 手写——两条机制针对不同工具协议,写法不能混。"""
        body = self._prompt()
        self.assertIn("调用 Bash", body)
        self.assertNotIn("# <", body)
        self.assertNotIn("@intent", body)

    def test_不管有没有grounding都带上(self):
        """_grounding 是按会话动态拼的,可能是空字符串——意图规则不能跟着它一起消失。"""
        empty_grounding = self._prompt({"_grounding": ""})
        with_grounding = self._prompt({"_grounding": "〈工作流职责〉测试用的系统约定文本"})
        for body in (empty_grounding, with_grounding):
            self.assertIn("`description`", body)

    def test_不复述占位词(self):
        """规则本身要明确禁止模型把占位词原样抄进 description,否则等于没约束。"""
        body = self._prompt()
        self.assertIn("空泛文字", body)
