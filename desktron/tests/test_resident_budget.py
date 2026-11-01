"""常驻引擎进程的内存预算(2026-08-23)。

改之前每个引擎各管各的池,闸都写在「同时留几个会话」上:claude 是 _PERSIST_MAX、
codex 家族是 _SESSION_MAX,默认都是 6。当天实测的单位成本差一个数量级——一条常驻
claude 进程 450~510MB 且随对话增长,一份 codex app-server 连同它的三个子进程约 60MB。
同样写 6,前者上限 3GB、后者 0.36GB;而两池互不相识,claude 池满了只挤 claude,
哪怕 codex 那边正闲着五个更该收的。

用户实见 Desktron 涨到 5.73GB,并且会回落一点——回落正是闲置三十分钟被各自的 reaper
收掉,所以不是泄漏,是闸的口径不对。这里把闸换成按真实 RSS 跨引擎结账。
"""
import os
import sys
import time
import unittest

import _srcmap
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import resident
finally:
    sys.argv = _argv

MB = 1 << 20


class Fake:
    """一个假的池:自己记着有哪些常驻项,被收掉就从表里去掉。"""

    def __init__(self, rows):
        self.rows = dict(rows)          # sid -> (pid, busy, at)
        self.dropped = []

    def list(self):
        return [(sid, pid, busy, at) for sid, (pid, busy, at) in self.rows.items()]

    def drop(self, sid):
        self.rows.pop(sid, None)
        self.dropped.append(sid)


class Budget(unittest.TestCase):
    def setUp(self):
        self._saved = dict(resident._pools)
        resident._pools.clear()
        resident._last.update({"at": 0.0, "rss": {}})

    def tearDown(self):
        resident._pools.clear()
        resident._pools.update(self._saved)
        resident._last.update({"at": 0.0, "rss": {}})

    def _rss(self, table):
        """把 pid → 字节数钉死,不去跑 ps。"""
        return mock.patch.object(resident, "_tree_rss", lambda: dict(table))

    def test_没超预算不收(self):
        a = Fake({"s1": (101, False, 1.0), "s2": (102, False, 2.0)})
        resident.add_pool("claude", a.list, a.drop)
        with self._rss({101: 300 * MB, 102: 300 * MB}):
            gone = resident.make_room(budget=1000 * MB)
        self.assertEqual(gone, [])
        self.assertEqual(a.dropped, [])

    def test_超了就收到预算之下(self):
        a = Fake({"s1": (101, False, 1.0), "s2": (102, False, 2.0), "s3": (103, False, 3.0)})
        resident.add_pool("claude", a.list, a.drop)
        with self._rss({101: 500 * MB, 102: 500 * MB, 103: 500 * MB}):
            resident.make_room(budget=1100 * MB)
        self.assertEqual(a.dropped, ["s1"], "该收最久没用的那一个,而且收够就停")

    def test_跨引擎一起排(self):
        """这是个数闸办不到的那一半:claude 池满了只挤 claude,
        哪怕 codex 那边闲着一个更久没用的。"""
        c = Fake({"c1": (201, False, 50.0)})
        x = Fake({"x1": (301, False, 10.0)})
        resident.add_pool("claude", c.list, c.drop)
        resident.add_pool("codex", x.list, x.drop)
        with self._rss({201: 500 * MB, 301: 500 * MB}):
            gone = resident.make_room("claude", "c1", budget=600 * MB)
        self.assertEqual(gone, [("codex", "x1")], "没有跨引擎挑,收的还是自己这一池")

    def test_在跑的一律不收(self):
        """收一个在跑的等于把用户这一回合的活弄丢。全都在跑就让它超一会儿。"""
        a = Fake({"s1": (101, True, 1.0), "s2": (102, True, 2.0)})
        resident.add_pool("claude", a.list, a.drop)
        with self._rss({101: 900 * MB, 102: 900 * MB}):
            gone = resident.make_room(budget=100 * MB)
        self.assertEqual(gone, [])
        self.assertEqual(a.dropped, [])

    def test_保住准入的那一个(self):
        """正在为它腾地方,不能反手把它收了。"""
        a = Fake({"keep": (101, False, 1.0), "other": (102, False, 2.0)})
        resident.add_pool("claude", a.list, a.drop)
        with self._rss({101: 500 * MB, 102: 500 * MB}):
            resident.make_room("claude", "keep", budget=600 * MB)
        self.assertEqual(a.dropped, ["other"])

    def test_按进程树算不按单个进程(self):
        """codex 那一份里壳只有 5MB,占地方的全是它的子进程;只算壳等于把这一池当成免费的。"""
        with mock.patch.object(resident.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="\n".join([
                "  100     1  5120", "  101   100 40960", "  102   101 11264", "  103   100  6144"]))
            resident._last.update({"at": 0.0, "rss": {}})
            got = resident._tree_rss()
        self.assertEqual(got[100], (5120 + 40960 + 11264 + 6144) << 10)
        self.assertEqual(got[101], (40960 + 11264) << 10)

    def test_父子成环不死递归(self):
        """ps 是快照,进程刚换过父时表里可能自成环。"""
        with mock.patch.object(resident.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="  200   201 1024\n  201   200 1024")
            resident._last.update({"at": 0.0, "rss": {}})
            got = resident._tree_rss()
        self.assertIn(200, got)

    def test_收不动就停_同一个不重试(self):
        """drop 没能真把它摘掉时(池子实现有问题、或那一项刚被别的线程换掉),
        不能对着同一个目标一遍遍收。原来只卡了 32 次上限,拿盘上真进程演算时
        看到它对同一个 sid 收了 32 遍(2026-08-23)。"""
        class Stuck(Fake):
            def drop(self, sid):
                self.dropped.append(sid)      # 只记,不真的摘掉
        a = Stuck({"s1": (101, False, 1.0), "s2": (102, False, 2.0)})
        resident.add_pool("claude", a.list, a.drop)
        with self._rss({101: 900 * MB, 102: 900 * MB}):
            resident.make_room(budget=100 * MB)
        self.assertEqual(a.dropped, ["s1", "s2"], "同一个目标被重试了")

    def test_收得动就不必试第二个(self):
        a = Fake({"s1": (101, False, 1.0), "s2": (102, False, 2.0)})
        resident.add_pool("claude", a.list, a.drop)
        rss = {101: 900 * MB, 102: 100 * MB}
        with mock.patch.object(resident, "_tree_rss", lambda: {k: v for k, v in rss.items()
                                                              if k in {p for _, (p, _, _) in a.rows.items()}}):
            resident.make_room(budget=500 * MB)
        self.assertEqual(a.dropped, ["s1"])

    def test_列不出来的池不拖累别人(self):
        def boom():
            raise RuntimeError("这一池坏了")
        good = Fake({"s1": (101, False, 1.0)})
        resident.add_pool("bad", boom, lambda s: None)
        resident.add_pool("claude", good.list, good.drop)
        with self._rss({101: 900 * MB}):
            resident.make_room(budget=100 * MB)
        self.assertEqual(good.dropped, ["s1"])

    def test_预算取物理内存的一份并卡上下限(self):
        self.assertGreaterEqual(resident.budget_bytes(), 1536 * MB)
        self.assertLessEqual(resident.budget_bytes(), 4 << 30)

    def test_预算可覆盖(self):
        with mock.patch.dict(os.environ, {"DESKTRON_RESIDENT_BUDGET_MB": "800"}):
            self.assertEqual(resident.budget_bytes(), 800 * MB)

    def test_现状报得出来(self):
        a = Fake({"s1": (101, False, time.time() - 30)})
        resident.add_pool("claude", a.list, a.drop)
        with self._rss({101: 200 * MB}):
            snap = resident.snapshot()
        self.assertEqual(snap["items"][0]["rss_mb"], 200.0)
        self.assertEqual(snap["items"][0]["pool"], "claude")
        self.assertGreaterEqual(snap["items"][0]["idle_s"], 29)
        self.assertFalse(snap["over"])


class Sweep(unittest.TestCase):
    """周期结账。准入那一道挡不住这次实见的情形:三个会话就地长大,期间一次新准入都没有,
    而两池各自的 reaper 只看闲置时长、不看内存,于是没有任何一处会去收。"""

    def setUp(self):
        self._saved = dict(resident._pools)
        resident._pools.clear()
        resident._last.update({"at": 0.0, "rss": {}})
        self._on = resident._sweeping["on"]

    def tearDown(self):
        resident._pools.clear()
        resident._pools.update(self._saved)
        resident._sweeping["on"] = self._on
        resident._last.update({"at": 0.0, "rss": {}})

    def test_不新建会话也会收(self):
        """就地长大的那一路:名单一条没变,只是 RSS 涨了。"""
        a = Fake({"s1": (101, False, 1.0), "s2": (102, False, 9.0)})
        resident.add_pool("claude", a.list, a.drop)
        with mock.patch.object(resident, "_tree_rss", lambda: {101: 100 * MB, 102: 100 * MB}):
            self.assertEqual(resident.make_room(budget=1000 * MB), [])
        with mock.patch.object(resident, "_tree_rss", lambda: {101: 900 * MB, 102: 900 * MB}):
            gone = resident.make_room(budget=1000 * MB)
        self.assertEqual(gone, [("claude", "s1")], "长大之后没有人去收")

    def test_一轮结账真的会去收(self):
        """循环体本身:不是只把 make_room 摆在那儿,是每一轮都调它。"""
        a = Fake({"s1": (101, False, 1.0)})
        resident.add_pool("claude", a.list, a.drop)
        with mock.patch.object(resident, "_tree_rss", lambda: {101: 9999 * MB}):
            gone = resident.sweep_once()
        self.assertEqual(gone, [("claude", "s1")])
        self.assertEqual(resident._sweeping["last"], [("claude", "s1")])

    def test_循环体调的就是它(self):
        import inspect
        self.assertIn("sweep_once()", inspect.getsource(resident._sweep_loop))

    def test_只起一条线程(self):
        with mock.patch.object(resident.threading, "Thread") as th:
            resident._sweeping["on"] = False
            resident.start_sweep()
            resident.start_sweep()
        self.assertEqual(th.call_count, 1)

    def test_收了谁报得出来(self):
        a = Fake({"s1": (101, False, 1.0)})
        resident.add_pool("claude", a.list, a.drop)
        with mock.patch.object(resident, "_tree_rss", lambda: {101: 900 * MB}):
            resident.make_room(budget=100 * MB)
            resident._sweeping["last"] = [("claude", "s1")]
            snap = resident.snapshot()
        self.assertEqual(snap["last_evicted"], ["claude/s1"])


class Wiring(unittest.TestCase):
    ADP = _srcmap.engine_adapters_src()
    CDX = open(os.path.join(LIB, "codex_appserver.py"), encoding="utf-8").read()
    AGT = open(os.path.join(LIB, "agents_status.py"), encoding="utf-8").read()

    def test_两池都登记了(self):
        self.assertIn('_resident.add_pool("claude", _persist_roster, _persist_evict)', self.ADP)
        self.assertIn('_resident.add_pool("codex", _session_roster, evict_session)', self.CDX)

    def test_准入前先按内存结账(self):
        self.assertIn('resident.make_room("claude", keep_sid)', self.ADP)
        self.assertIn('resident.make_room("codex", keep_sid)', self.CDX)

    def test_个数那道闸留着(self):
        """极小的进程无限增多同样不好,内存闸挡不住那一种。"""
        self.assertIn("_PERSIST_MAX", self.ADP)
        self.assertIn("_SESSION_MAX", self.CDX)

    def test_周期结账起得来(self):
        ui = _srcmap.ui_server_src()
        self.assertIn('__import__("resident").start_sweep()', ui)

    def test_报进状态接口(self):
        self.assertIn('"resident": resident_status(),', self.AGT)

    def test_登记失败不拖垮引擎(self):
        """resident 出问题不该让引擎起不来——它只是省资源的,不是必需件。"""
        for src in (self.ADP, self.CDX):
            i = src.index("add_pool(")
            self.assertIn("except Exception:", src[i - 300:i + 200])


if __name__ == "__main__":
    unittest.main()
