"""非正常收尾的回合,产出只存在于我们这边。

投影按 transcript 整份覆盖(import_session),前提是「transcript 是权威记录、我们的存储是
它的子集」。这个前提有一处不成立:引擎只在一条 assistant 消息完整收尾时才落盘,回合被
打断、被看门狗超时杀掉、常驻进程中途崩的时候,流式吐出的那些字它不写。

2026-08-15 在 436ddefc 的记录里实证:13:09:27 用户发出「试试换成靛蓝色」,13:09:29 就是
`[Request interrupted by user]`,中间没有任何 assistant 条目。而 _finish_turn 是把这段内容
收下来了的(注释:即使异常退出也保存已累积内容),于是当时看得见、下次以 HTML 视图打开
(切视图、切引擎、重启后重开都走 /session/adopt)就被抹掉——界面上只剩用户那条消息。

判据落在「这一轮有没有正常收尾」,不落在「是不是被打断」:打断只是其中一种失败方式,
超时与崩溃走的是同一条收尾路径,新增的失败方式也该自动覆盖。
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))

_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv


class Bus:
    def publish(self, ev):
        pass


def _msg(mid, role, text, at, **info_extra):
    info = {"id": mid, "role": role, "sessionID": "s", "at": at}
    info.update(info_extra)
    return {"info": info, "parts": [{"id": mid + "_p", "type": "text", "text": text}]}


class LocalOnlyMarking(unittest.TestCase):
    """标记打在收尾那一处,覆盖所有非正常结束的分支。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, Bus())
        self.sid = self.ad.create_session()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, **ctx_extra):
        ctx = {"sid": self.sid, "msg_id": "msg_x", "parts": {}, "order": [], "t0": 1000}
        ctx["parts"]["p1"] = {"id": "p1", "type": "text", "text": "跑出来一半"}
        ctx["order"].append("p1")
        ctx.update(ctx_extra)
        self.ad._finish_turn(self.sid, ctx, ctx_extra.pop("_abnormal", None))
        return [m for m in self.ad.messages(self.sid)
                if (m.get("info") or {}).get("role") == "assistant"]

    def test_正常收尾不打标记(self):
        rows = self._run(ms=1200)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("local_only", rows[0]["info"])

    def test_打断的打标记(self):
        rows = self._run(aborted="interrupt")
        self.assertTrue(rows[0]["info"]["local_only"])
        self.assertEqual(rows[0]["info"]["turn_t0"], 1000)

    def test_异常收尾的打标记(self):
        """看门狗杀进程、常驻进程中途崩,走的是同一条收尾路径。"""
        ctx = {"sid": self.sid, "msg_id": "msg_y", "parts": {"p": {"id": "p", "type": "text",
               "text": "崩之前说了半句"}}, "order": ["p"], "t0": 2000}
        self.ad._finish_turn(self.sid, ctx, "常驻进程中断(退出码 -9)")
        rows = [m for m in self.ad.messages(self.sid)
                if (m.get("info") or {}).get("role") == "assistant"]
        self.assertTrue(rows[-1]["info"]["local_only"])

    def test_撤回的不留痕更不打标记(self):
        rows = self._run(aborted="recall")
        self.assertEqual(rows, [])


class ReinstateAfterRebuild(unittest.TestCase):
    """重建投影之后把带标记的补回去。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, Bus())
        self.sid = self.ad.create_session()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self, msgs):
        self.ad._rewrite_msgs(self.sid, msgs)

    def _texts(self, msgs):
        return [(m["info"]["role"], m["parts"][0]["text"]) for m in msgs]

    def test_记录里没有的那条补回来(self):
        self._store([
            _msg("u1", "user", "试试换成靛蓝色", 1000),
            _msg("a1", "assistant", "跑出来一半", 1100, local_only=True, turn_t0=1050),
        ])
        rebuilt = [_msg("t_u1", "user", "试试换成靛蓝色", 1000)]   # 记录里只有用户那条
        out = self.ad._reinstate_local_only(self.sid, rebuilt)
        self.assertEqual(self._texts(out),
                         [("user", "试试换成靛蓝色"), ("assistant", "跑出来一半")])

    def test_按时间位插入不落到末尾(self):
        """打断之后接着说话:补回的那条要落在后一个回合之前。"""
        self._store([_msg("a1", "assistant", "跑出来一半", 1100,
                          local_only=True, turn_t0=1050)])
        rebuilt = [_msg("t_u1", "user", "第一句", 1000),
                   _msg("t_u2", "user", "打断之后接着说", 2000),
                   _msg("t_a2", "assistant", "第二轮的回答", 2100)]
        out = self.ad._reinstate_local_only(self.sid, rebuilt)
        self.assertEqual([m["parts"][0]["text"] for m in out],
                         ["第一句", "跑出来一半", "打断之后接着说", "第二轮的回答"])

    def test_记录已经覆盖这一轮就不补(self):
        """打断发生在某个工具块收尾之后,记录里有这一轮的产出——以记录为准,不重复显示。"""
        self._store([_msg("a1", "assistant", "本地那份", 1100,
                          local_only=True, turn_t0=1050)])
        rebuilt = [_msg("t_u1", "user", "发话", 1000),
                   _msg("t_a1", "assistant", "记录里那份", 1080)]
        out = self.ad._reinstate_local_only(self.sid, rebuilt)
        self.assertEqual([m["parts"][0]["text"] for m in out], ["发话", "记录里那份"])

    def test_没有标记就原样返回(self):
        self._store([_msg("a1", "assistant", "正常回合", 1100)])
        rebuilt = [_msg("t_a1", "assistant", "记录里那份", 1100)]
        out = self.ad._reinstate_local_only(self.sid, rebuilt)
        self.assertEqual([m["parts"][0]["text"] for m in out], ["记录里那份"])

    def test_反复重建不会越补越多(self):
        """补回的那条会随投影一起写回存储,下一次重建时不能再补一遍。"""
        self._store([_msg("a1", "assistant", "跑出来一半", 1100,
                          local_only=True, turn_t0=1050)])
        rebuilt = [_msg("t_u1", "user", "发话", 1000)]
        once = self.ad._reinstate_local_only(self.sid, rebuilt)
        self.ad._rewrite_msgs(self.sid, once)
        twice = self.ad._reinstate_local_only(self.sid, list(rebuilt))
        self.assertEqual(len(twice), 2)
        self.assertEqual([m["parts"][0]["text"] for m in twice], ["发话", "跑出来一半"])


if __name__ == "__main__":
    unittest.main()
