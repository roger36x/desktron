"""claude 的压缩收尾只有一条 system/status(2026-08-26 实测 CLI 2.1.245)。

抓到的原始帧:
  {"subtype":"status","status":"compacting", ...}                      开始
  {"subtype":"status","status":null,"compact_result":"failed",
   "compact_error":"Not enough messages to compact.", ...}             收尾
compact_boundary 一帧不发——它只写进 rollout 记录,由重建那条路认领。

于是收尾事件里既没有 pre/post 也没有 trigger。前端原来只认这三样,claude 这条路上
「上下文已压缩」那条分隔线因此从来没画出来过;compact_error 也整个丢掉了,
压缩被拒和压缩成功在界面上长得一模一样。
"""
import os
import re
import sys
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
CHAT = _srcmap.chat_src()

import engine_adapters as EA


class _Bus:
    def __init__(self):
        self.rows = []

    def publish(self, ev):
        self.rows.append(ev)


class 后端(unittest.TestCase):
    def setUp(self):
        self.ad = EA.ClaudeAdapter.__new__(EA.ClaudeAdapter)
        self.ad._live = {}
        self.ad._compact_state = {}
        self.ad._checkpoint_live = lambda *a, **k: None
        self.ad.bus = _Bus()
        self.ctx = {"sid": "s1"}

    def feed(self, **kw):
        """喂一帧,返回它发出的 session.compact 属性;这一帧没发事件就返回 None。"""
        obj = {"type": "system", "subtype": "status", "session_id": "e1"}
        obj.update(kw)
        n = len(self.ad.bus.rows)
        self.ad.handle_event(obj, self.ctx)
        if len(self.ad.bus.rows) == n:
            return None
        return self.ad.bus.rows[-1]["properties"]

    def test_开始帧挂上压缩态(self):
        p = self.feed(status="compacting")
        self.assertTrue(p["running"])
        self.assertTrue(p["started"])
        self.assertEqual(self.ctx["compact_t0"], p["started"])

    def test_收尾帧带出失败原因(self):
        self.feed(status="compacting")
        p = self.feed(status=None, compact_result="failed",
                      compact_error="Not enough messages to compact.")
        self.assertFalse(p["running"])
        self.assertEqual(p["result"], "failed")
        self.assertEqual(p["error"], "Not enough messages to compact.")
        self.assertEqual(self.ctx["compact_t0"], 0)

    def test_成功收尾也带result(self):
        """前端要靠它才知道该画分隔线——这一帧没有 pre/post/trigger 可依。"""
        self.feed(status="compacting")
        p = self.feed(status=None, compact_result="success")
        self.assertEqual(p["result"], "success")
        self.assertEqual(p["error"], "")

    def test_每回合开头那条不冒充压缩(self):
        """status=requesting 每回合都来一条,它跟压缩无关,索性一个事件都不发。

        原来是照发不误、靠字段全空让前端自己分辨。那样不够:压缩过之后,后续 status 帧里
        compact_result 可能仍带着上一次的值,于是压缩早就结束了还会再发一次「收尾」,
        前端据它在末尾又画一条分隔线(2026-08-26 用户实见「压缩完出现两条线」)。
        改成按「见过开始」配对,没配上的一律不发。"""
        self.assertIsNone(self.feed(status="requesting"))

    def test_落单的收尾帧不发事件(self):
        """没见过 compacting 就来的收尾帧,是上一次压缩的残值,不是新的一次。"""
        self.assertIsNone(self.feed(status=None, compact_result="success"))

    def test_同一次压缩只收一次尾(self):
        """配对是一次性的:收过尾之后再来同样的帧,不再重复发。"""
        self.feed(status="compacting")
        self.assertIsNotNone(self.feed(status=None, compact_result="success"))
        self.assertIsNone(self.feed(status=None, compact_result="success"))

    def test_失败原因截断(self):
        # 先喂开始帧:真机上失败那一次也照样先发 compacting(见文件头抓到的原始帧),
        # 而收尾事件现在要配得上一次开始才发。
        self.feed(status="compacting")
        p = self.feed(status=None, compact_result="failed", compact_error="x" * 900)
        self.assertEqual(len(p["error"]), 200)


class 前端(unittest.TestCase):
    """前端这一支是照上面那批帧改的,断言它认得出 result 与 error。"""

    def seg(self):
        m = re.search(r'if \(type === "session\.compact"\) \{[\s\S]+?\n    return;\n  \}', CHAT)
        assert m, "session.compact 分支不见了"
        return m.group(0)

    def test_收尾帧要配得上一次开始(self):
        """前后端各做一道,互为兜底。

        后端那道按 ctx 配对,只管 claude 这条路;前端这道挂在 v 上,管所有引擎——codex
        手动 /compact 就另有一处双发(compact_session 在 thread/compacted 的中继里发一次
        running=False,返回后又无条件发一次),同一次压缩两条收尾帧,前端这道能一并挡住。

        标记放在 v 上而不是 DOM 上是有意的:对账重建会整片换掉 DOM,标记不能跟着没;
        而刷新是新视图,标记本就该是空的——那一路的分隔线由记录重建画,不走这里。"""
        s = self.seg()
        self.assertIn("v.cmpLive = true", s)
        self.assertIn("if (!v.cmpLive) return;", s)
        self.assertIn("v.cmpLive = false;", s)
        # 普通 native 终态仍先配对；只有带 operation receipt 的外层手动终态可独立收口。
        self.assertLess(s.index("if (!v.cmpLive) return;"),
                        s.index("hsCompactRow(", s.index("if (!v.cmpLive) return;")))
        self.assertIn("manualTerminal", s)

    def test_只有result也画分隔线(self):
        """原来的条件是 p.pre || p.post || p.trigger,claude 三样都没有。"""
        s = self.seg()
        self.assertIn("p.pre || p.post || p.trigger || p.result", s)

    def test_失败单独报出来(self):
        s = self.seg()
        at = s.index('p.result === "failed"')
        self.assertLess(at, s.index("hsCompactRow"), "失败要排在画线之前,别把被拒当成压过了")
        self.assertIn("p.error", s[at:at + 400])

    def test_失败时不画分隔线(self):
        """压缩被拒还摆一条「上下文已压缩」,比什么都不显示更误导。"""
        s = self.seg()
        at = s.index('if (p.result === "failed"')
        draw = s.index('hsCompactRow(', at)
        self.assertIn('} else if', s[at:draw])

    def test_留了观测日志且不刷屏(self):
        """症状是「事件到没到前端」分不出来。日志要有,但每回合的 requesting 不该记。"""
        s = self.seg()
        self.assertIn("deskLog(", s)
        at = s.index("deskLog(")
        head = s[:at]
        self.assertIn("p.running || p.result || p.error", head[-200:])

    def test_不在这里另叫对账(self):
        """此刻 v.busy 还是真的,hsReconcileNow 会当场退出;回合收尾那次才管用。"""
        self.assertNotIn("hsReconcile(v)", self.seg())




class 手动压缩(unittest.TestCase):
    """2026-09-04 实测 CLI 2.1.260:手动 /compact 跑了 210 秒,stdout 上一帧 status 都没发。

    从日志能读出为什么:紧挨着的上一次自动压缩已经 announce 过 compacting(还带 30 秒一次
    的心跳),引擎不为第二次重复 announce;而那次自动压缩的 compact_result=failed 恰好落在
    手动这一轮的开头。照原来的写法,界面上先是什么都没有,再报一次并不存在的失败。

    所以手动压缩的始末改由 Desk 自己记:人敲的是什么我们知道,不等引擎开口。
    """

    def setUp(self):
        self.ad = EA.ClaudeAdapter.__new__(EA.ClaudeAdapter)
        self.ad._live = {}
        self.ad._compact_state = {}
        self.ad._checkpoint_live = lambda *a, **k: None
        self.ad.bus = _Bus()
        self.ad._set_engine_sid = lambda *a, **k: None
        self.ctx = {"sid": "s1", "text": "/compact"}
        self.landed = True
        self.ad._compact_landed = lambda esid, since: self.landed

    def init(self, text="/compact"):
        self.ctx["text"] = text
        self.ad.handle_event({"type": "system", "subtype": "init",
                              "session_id": "e1"}, self.ctx)
        return self.ad.bus.rows[-1]["properties"] if self.ad.bus.rows else None

    def result(self):
        n = len(self.ad.bus.rows)
        self.ad.handle_event({"type": "result", "subtype": "success",
                              "usage": {}, "duration_ms": 1}, self.ctx)
        cmp = [r for r in self.ad.bus.rows[n:] if r["type"] == "session.compact"]
        return cmp[-1]["properties"] if cmp else None

    def test_敲下就开压缩态(self):
        p = self.init()
        self.assertTrue(p["running"])
        self.assertEqual(p["trigger"], "manual")
        self.assertTrue(self.ctx["compact_manual"])
        # compact_t0 要留在 ctx 里:压缩期间刷新页面,进度条靠 /state 的这个起点续着画
        self.assertEqual(self.ctx["compact_t0"], p["started"])

    def test_普通消息不开(self):
        self.assertIsNone(self.init("盘点一下改动"))
        self.assertFalse(self.ctx.get("compact_manual"))

    def test_带指示的compact照样认(self):
        self.assertTrue(self.init("/compact 只留结论")["running"])

    def test_期间的引擎收尾帧不作数(self):
        """上一次自动压缩的残帧落在这一轮里,不能把刚开的压缩态收掉。"""
        self.init()
        n = len(self.ad.bus.rows)
        self.ad.handle_event({"type": "system", "subtype": "status", "status": None,
                              "compact_result": "failed",
                              "compact_error": "Not enough messages to compact."}, self.ctx)
        self.assertEqual(len(self.ad.bus.rows), n)
        self.assertTrue(self.ctx["compact_t0"])

    def test_回合终态收尾并按记录判成败(self):
        self.init()
        p = self.result()
        self.assertFalse(p["running"])
        self.assertEqual(p["result"], "success")
        self.assertEqual(p["trigger"], "manual")   # 前端据它画那条分隔线
        self.assertEqual(self.ctx["compact_t0"], 0)
        self.assertFalse(self.ctx["compact_manual"])

    def test_记录里没落下摘要就是失败(self):
        self.landed = False
        self.init()
        p = self.result()
        self.assertEqual(p["result"], "failed")
        self.assertTrue(p["error"])

    def test_收过尾不再重复发(self):
        self.init()
        self.assertIsNotNone(self.result())
        self.assertIsNone(self.result())


class 压缩摘要落盘判定(unittest.TestCase):
    """成败去记录里取:那条 isCompactSummary 是实打实的凭据,stdout 上的话不一定有。"""

    def setUp(self):
        self.ad = EA.ClaudeAdapter.__new__(EA.ClaudeAdapter)

    def _path(self, rows):
        import json as _json
        import tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        for r in rows:
            f.write(_json.dumps(r) + "\n")
        f.close()
        self.ad._transcript_path = lambda esid, _p=f.name: _p
        return f.name

    def test_这一轮之后的摘要算数(self):
        self._path([{"type": "user", "isCompactSummary": True,
                     "timestamp": "2026-09-04T09:24:25.622Z"}])
        since = 1788513600000   # 2026-09-04T09:20:00Z
        self.assertTrue(self.ad._compact_landed("e1", since))

    def test_上一次的旧摘要不算(self):
        self._path([{"type": "user", "isCompactSummary": True,
                     "timestamp": "2026-09-04T08:00:00.000Z"}])
        self.assertTrue(not self.ad._compact_landed("e1", 1788513600000))

    def test_没有摘要就是没压过(self):
        self._path([{"type": "user", "timestamp": "2026-09-04T09:24:25.622Z"}])
        self.assertFalse(self.ad._compact_landed("e1", 1788513600000))

    def test_记录不存在不报错(self):
        self.ad._transcript_path = lambda esid: "/nonexistent/x.jsonl"
        self.assertFalse(self.ad._compact_landed("e1", 1))


if __name__ == "__main__":
    unittest.main()
