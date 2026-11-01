"""ctx["seq"] 是回合序号,不是「本回合第几条 API 消息」的计数器。

两者曾经同名(2026-09-01 之前)。claude 解析流式事件时,每收到一个 message_start 就把
ctx["seq"] 自增一次——而 ctx["seq"] 是 cli_turn._seq_next() 给的回合序号(毫秒时刻,
全局单调),recap 生成完之后要拿快照里的它与适配器的 _turn_seq 比对,判断「期间有没有
来过新回合」。一回合里 claude 发几条 API 消息,回合序号就被顶高几,复核于是永远不等。

后果特别隐蔽:recap 照常触发、照常调模型、照常生成两百多字,然后在复核那一步被当成
「过期结果」丢掉——丢弃是正常分支,不报错、不留痕。表现只是「claude 的会话永远没有
回顾」,而 codex、pi 都正常(它们不走这段解析)。查了两天,最后靠临时探针打出
`轮次未变=False(_turn_seq=1788247170509 seq=1788247170526)` 才定位,两个值差 17,
正是那一回合的 API 消息条数。

所以这里钉两件事:回合序号不被解析流程改写;pid 的去重计数走独立的键。
"""
import os
import re
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "desktron", "lib", "engine_adapters", "claude.py")
with open(SRC, encoding="utf-8") as f:
    CODE = f.read()


class TurnSeqIsReadOnly(unittest.TestCase):
    def test_解析流程不写回合序号(self):
        """claude.py 里不许出现对 ctx["seq"] 的赋值。它由 cli_turn 一处产生,
        这一侧只读——写它就是在改「这是第几轮」,而 recap 的复核正是比它。"""
        self.assertNotIn('ctx["seq"] =', CODE)
        self.assertNotIn("ctx['seq'] =", CODE)
        self.assertFalse(re.search(r'ctx\[\s*["\']seq["\']\s*\]\s*=', CODE),
                         "ctx[\"seq\"] 被赋值了——回合序号会被顶高,recap 复核永远不等")

    def test_每条_api_消息的计数走独立的键(self):
        """pid 要在一回合内区分多条 API 消息(content index 会重置),这个需求是真的,
        只是不能借用回合序号那个键。"""
        self.assertIn('ctx["msg_seq"] = ctx.get("msg_seq", 0) + 1', CODE)

    def test_pid_拼接用的是_msg_seq(self):
        """漏改任何一处,同一回合里不同 API 消息的 part 会撞 id。"""
        pids = re.findall(r'f\'\{ctx\["msg_id"\]\}_m\{ctx\.get\((["\'])(\w+)\1', CODE)
        self.assertTrue(pids, "没找到 pid 拼接,选择器要跟着代码改")
        for _q, key in pids:
            self.assertEqual(key, "msg_seq", f"pid 用了 {key},应当是 msg_seq")

    def test_发到总线的_seq_仍是回合序号(self):
        """界面据它把内容归到哪一轮(见 cli_turn._turn_mark),不能换成 msg_seq。"""
        self.assertIn('seq=ctx.get("seq") or 0', CODE)


if __name__ == "__main__":
    unittest.main()
