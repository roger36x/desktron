"""codex 家族的目标也要在正文里留痕(2026-08-23)。

此前只有 claude 那侧把目标的设与达成写成一条哨兵消息(`_group_transcript` 里那几行),
codex 的 `_group_rollout` 里 goal 一次都没出现。结果是:窄条能显示「现在挂着什么目标」
(goal() 从 rollout 里扫得出来),但「什么时候设的、到哪一轮达成的」在正文里回溯不到
——而那正是这条痕存在的理由(见前端 hsGoalRow 的注释)。

目标回执的形状照 _goal_from_rollout_output 认的那一种抄:response_item 里的
custom_tool_call_output,正文里带 {"goal": {threadId, objective, status, updatedAt}}。
"""
import json
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
    import engine_adapters as ea
finally:
    sys.argv = _argv

ESID = "019f7e78-5fce-7523-9dbf-4e0359d2f936"


def goal_row(objective, status, at="2026-08-23T10:00:00Z", thread=ESID):
    payload = {"type": "custom_tool_call_output", "call_id": "c1",
               "output": json.dumps({"goal": {"threadId": thread, "objective": objective,
                                              "status": status, "updatedAt": 1787500000}})}
    return {"type": "response_item", "timestamp": at, "payload": payload}


def user_row(text, at="2026-08-23T10:00:01Z"):
    return {"type": "response_item", "timestamp": at,
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": text}]}}


class Bus:
    def publish(self, e):
        pass


class GoalTrace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.CodexAdapter(self.tmp.name, self.tmp.name, Bus())
        self.sid = self.ad.create_session()["id"]
        self.ad._set_engine_sid(self.sid, ESID)

    def tearDown(self):
        self.tmp.cleanup()

    def _group(self, rows):
        path = os.path.join(self.tmp.name, "rollout-2026-08-23T10-00-00-%s.jsonl" % ESID)
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return self.ad._group_rollout(path, self.sid)

    def _goals(self, msgs):
        return [(m["info"]["goal"]["condition"], m["info"]["goal"]["met"])
                for m in msgs if (m.get("info") or {}).get("goal")]

    def test_设定留一条痕(self):
        msgs = self._group([user_row("开始"), goal_row("修好 rc", "active")])
        self.assertEqual(self._goals(msgs), [("修好 rc", False)])

    def test_达成再留一条(self):
        msgs = self._group([user_row("开始"), goal_row("修好 rc", "active"),
                            user_row("继续"), goal_row("修好 rc", "complete")])
        self.assertEqual(self._goals(msgs), [("修好 rc", False), ("修好 rc", True)])

    def test_每轮都写的_active_只画一次(self):
        """rollout 里目标挂多久就有多少条 active。三种都画的话,正文里会反复出现同一句。"""
        rows = [user_row("开始")]
        for _ in range(5):
            rows += [goal_row("修好 rc", "active"), user_row("继续")]
        self.assertEqual(self._goals(self._group(rows)), [("修好 rc", False)])

    def test_换了目标就再画一次(self):
        msgs = self._group([goal_row("甲", "active"), user_row("换个目标"),
                            goal_row("乙", "active")])
        self.assertEqual(self._goals(msgs), [("甲", False), ("乙", False)])

    def test_别的会话的回执不算(self):
        """回执带 threadId,对不上就不是这一段会话的。"""
        msgs = self._group([goal_row("别人的", "active", thread="别的-thread"),
                            user_row("我的话")])
        self.assertEqual(self._goals(msgs), [])

    def test_形状与_claude_那条一致(self):
        """两侧共用前端同一套渲染,字段对不上就画不出来。"""
        msgs = self._group([goal_row("修好 rc", "active")])
        info = [m["info"] for m in msgs if (m.get("info") or {}).get("goal")][0]
        self.assertEqual(info["role"], "user")
        self.assertEqual(set(info["goal"]), {"condition", "met"})
        self.assertTrue(info["id"].startswith("goal_"))
        self.assertTrue(info.get("at"))

    def test_哨兵不带正文(self):
        """它不是谁的发言,parts 必须是空的——不然会被当成一轮真的对话。"""
        msgs = self._group([goal_row("修好 rc", "active")])
        row = [m for m in msgs if (m.get("info") or {}).get("goal")][0]
        self.assertEqual(row["parts"], [])

    def test_目标不打断正文(self):
        """哨兵插在两轮之间,前后的用户发言都要还在。"""
        msgs = self._group([user_row("先问一句"), goal_row("修好 rc", "active"),
                            user_row("再问一句")])
        texts = ["".join(q.get("text") or "" for q in (m.get("parts") or [])
                         if q.get("type") == "text") for m in msgs]
        self.assertIn("先问一句", texts)
        self.assertIn("再问一句", texts)


if __name__ == "__main__":
    unittest.main()


class GoalBarStyle(unittest.TestCase):
    """「目标」与「任务」是输入框上沿并排的两条,字重要同一档。"""

    CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()

    def _rule(self, sel):
        import re
        m = re.search(r"\.hs-page %s \{([^}]*)\}" % re.escape(sel), self.CSS)
        assert m, sel + " 的规则不见了"
        return m.group(1)

    def test_两条的字重一致(self):
        """差一档字重,两行并排时读成两个层级(2026-08-23 用户实见)。"""
        a, b = self._rule(".hs-goal-k"), self._rule(".hs-tasks-k")
        self.assertIn("font-weight: 550", a)
        self.assertIn("font-weight: 550", b)

    def test_两条的色与字号也一致(self):
        for sel in (".hs-goal-k", ".hs-tasks-k"):
            self.assertIn("var(--accent)", self._rule(sel))
        for sel in (".hs-goal", ".hs-tasks"):
            self.assertIn("font-size: 14.5px", self._rule(sel))

    def test_任务标签按墨迹中线补偿(self):
        """行盒基线虽一致,墨迹观感仍需上移半像素。"""
        self.assertIn("translateY(-0.5px)", self._rule(".hs-tasks-k"))
