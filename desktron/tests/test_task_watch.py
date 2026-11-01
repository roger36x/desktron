"""TASKS.md 的变化看护(2026-08-25 用户实报:一个会话里改了任务,别的会话要等好一会儿)。

原来没有任何事件通道:后端改任务不推,SSE 又只按 sessionID 分派,不挂在会话上的事件
整条被丢掉。于是跨会话的感知全靠各页自己轮询——TaskDock 单源 5 秒、聚合 30 秒,
工作流页 5 秒,任务页打开之后干脆不刷。

改法是把这一轮挪到服务端:本机 stat 一份文件是微秒级,而客户端问一趟经隧道要
0.23~1.9 秒(同日实测)。这样延迟从 5 秒降到 1 秒,远端那一页还能少一串跨区往返。

要盯住的是两条写入路径都得覆盖:人在面板上勾选走 API,agent 是直接 Edit 那份纯文本。
只在 API 里推事件的话,恰恰漏掉常见的那条——所以这里看的是文件本身,不是 API 调用。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
import task_watch  # noqa: E402


class FakeBus:
    def __init__(self):
        self.evs = []

    def publish(self, ev):
        self.evs.append(ev)

    def domains(self):
        return [e["properties"]["domain"] for e in self.evs if e.get("type") == "task.updated"]


class Scan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = os.path.join(self.tmp, "domains")
        os.makedirs(os.path.join(self.base, "risk"))
        os.makedirs(os.path.join(self.base, "cont", "sub"))
        self.write("risk", "- [ ] a\n")
        self.write("cont/sub", "- [ ] b\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, rel):
        return os.path.join(self.base, rel.replace("/", os.sep), "TASKS.md")

    def write(self, rel, body):
        with open(self.path(rel), "w", encoding="utf-8") as f:
            f.write(body)

    def test_两层都扫得到(self):
        """容器的自定义子区落在 domains/<容器>/<子区>/TASKS.md,只扫一层会漏掉。"""
        got = task_watch._scan(self.tmp)
        self.assertIn(self.path("risk"), got)
        self.assertIn(self.path("cont/sub"), got)

    def test_没有清单的目录不进来(self):
        os.makedirs(os.path.join(self.base, "空工作流"))
        got = task_watch._scan(self.tmp)
        self.assertEqual(len([p for p in got if "空工作流" in p]), 0)

    def test_domains不存在时给空而不是抛(self):
        """新装的机器上还没有这个目录,看护不该因此起不来。"""
        self.assertEqual(task_watch._scan(tempfile.mkdtemp()), {})

    def test_指纹跟着内容走(self):
        a = task_watch._scan(self.tmp)[self.path("risk")]
        time.sleep(0.01)
        self.write("risk", "- [ ] a\n- [ ] c\n")
        b = task_watch._scan(self.tmp)[self.path("risk")]
        self.assertNotEqual(a, b)

    def test_不读内容只看stat(self):
        """这一轮每秒都要跑,读一遍内容是白费的开销。"""
        fp = task_watch._fp(self.path("risk"))
        st = os.stat(self.path("risk"))
        self.assertEqual(fp, (st.st_mtime_ns, st.st_size))

    def test_反推工作流号(self):
        self.assertEqual(task_watch._domain_of(self.tmp, self.path("risk")), "risk")
        self.assertEqual(task_watch._domain_of(self.tmp, self.path("cont/sub")), "cont:sub")
        self.assertEqual(task_watch._ledger_domain("risk:abcd"), "risk")
        self.assertEqual(task_watch._ledger_domain("cont:sub:abcd"), "cont:sub")

    def test_账本快照只读取任务行(self):
        data = os.path.join(self.tmp, "data")
        os.makedirs(data)
        with open(os.path.join(data, "task-ledger.json"), "w", encoding="utf-8") as stream:
            json.dump({"tasks": {"risk:abcd": {"next": "验收", "revision": 2}},
                       "receipts": {"r1": {"task": "risk:abcd"}}}, stream)
        got = task_watch._ledger_snapshot(data)
        self.assertEqual(list(got), ["risk:abcd"])
        self.assertIn('"next":"验收"', got["risk:abcd"])

    def test_同轮清单与账本变化只广播一次(self):
        data = os.path.join(self.tmp, "data")
        os.makedirs(data)
        bus = FakeBus()
        outer = self

        class OneIteration:
            calls = 0
            def is_set(self):
                self.calls += 1
                return self.calls >= 3
            def wait(self, _seconds):
                outer.write("risk", "- [~] changed\n")
                with open(os.path.join(data, "task-ledger.json"), "w", encoding="utf-8") as stream:
                    json.dump({"tasks": {"risk:abcd": {"next": "验收", "revision": 1}}}, stream)

        task_watch._loop(self.tmp, bus, OneIteration(), data)
        self.assertEqual(bus.domains(), ["risk"])


class Watch(unittest.TestCase):
    """跑真的看护线程,看它推什么。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = os.path.join(self.tmp, "domains")
        os.makedirs(os.path.join(self.base, "risk"))
        self.write("- [ ] a\n")
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        self.bus = FakeBus()
        task_watch.SCAN_SEC = 0.05      # 测试里别真等一秒
        task_watch.start(self.tmp, self.bus, self.data)
        time.sleep(0.2)                 # 让它把基线建起来

    def tearDown(self):
        task_watch.stop()
        task_watch.SCAN_SEC = 1.0
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def p(self):
        return os.path.join(self.base, "risk", "TASKS.md")

    def write(self, body):
        with open(self.p, "w", encoding="utf-8") as f:
            f.write(body)

    def wait(self, n=1, timeout=3.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if len(self.bus.domains()) >= n:
                return True
            time.sleep(0.02)
        return False

    def test_第一轮不推(self):
        """进程刚起来没人在等,全推一遍只会让每一页白取一次。"""
        self.assertEqual(self.bus.domains(), [])

    def test_改了就推(self):
        self.write("- [ ] a\n- [ ] b\n")
        self.assertTrue(self.wait(), "改了文件却没推出来")
        self.assertEqual(self.bus.domains()[0], "risk")

    def test_没变就不推(self):
        time.sleep(0.4)
        self.assertEqual(self.bus.domains(), [], "没动它却推了")

    def test_删掉也算变(self):
        """整份清单被删,页面上那几条得跟着消失。"""
        os.remove(self.p)
        self.assertTrue(self.wait(), "删了文件没推")

    def test_新建工作流的清单也认(self):
        os.makedirs(os.path.join(self.base, "新来的"))
        with open(os.path.join(self.base, "新来的", "TASKS.md"), "w", encoding="utf-8") as f:
            f.write("- [ ] x\n")
        self.assertTrue(self.wait(), "新建的清单没认")
        self.assertIn("新来的", self.bus.domains())

    def test_账本进展变化也推对应工作流(self):
        with open(os.path.join(self.data, "task-ledger.json"), "w", encoding="utf-8") as stream:
            json.dump({"tasks": {"risk:abcd": {"next": "等待验收", "revision": 1}}}, stream)
        self.assertTrue(self.wait(), "账本变了却没推")
        self.assertEqual(self.bus.domains(), ["risk"])

    def test_只改receipt不推任务更新(self):
        path = os.path.join(self.data, "task-ledger.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({"tasks": {}, "receipts": {"r1": {"task": "risk:abcd"}}}, stream)
        time.sleep(0.2)
        self.assertEqual(self.bus.domains(), [])

    def test_扫一次失败不结束看护(self):
        """扫描期间目录被删之类:这一轮跳过就好,线程不能死——死了之后再没人推,
        而页面那一侧的轮询已经撤掉了,表现就是任务永远不刷新。"""
        orig = task_watch._scan
        calls = []

        def boom(root):
            calls.append(1)
            if len(calls) <= 2:
                raise OSError("扫描炸了")
            return orig(root)

        task_watch._scan = boom
        try:
            time.sleep(0.3)
            self.write("- [ ] 之后还能推\n")
            self.assertTrue(self.wait(timeout=3.0), "扫描失败之后看护就不干活了")
        finally:
            task_watch._scan = orig

    def test_重复start只起一个(self):
        """ui_server 重载时会再调一次,起两个的话每次改动推两遍。"""
        a = task_watch.start(self.tmp, self.bus)
        b = task_watch.start(self.tmp, self.bus)
        self.assertIs(a, b)




class RegistryWatch(unittest.TestCase):
    """注册表(.workbench/apps.json、domains.json)也归这个线程看。

    agent 在对话里部署一个 app、reconcile 补默认工作区依赖,都不经前端 UI。页面原先靠
    每 2.5 秒问一次 /workbench/apps-rev 才知道——经隧道 30 秒里 12 次、每次换回 0 字节,
    是当时最密的一条轮询(2026-08-25 实测)。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, ".workbench"))
        os.makedirs(os.path.join(self.tmp, "domains"))
        for n in ("apps.json", "domains.json"):
            with open(os.path.join(self.tmp, ".workbench", n), "w", encoding="utf-8") as f:
                f.write("[]")
        self.bus = FakeBus()
        task_watch.SCAN_SEC = 0.05
        task_watch.start(self.tmp, self.bus)
        time.sleep(0.2)

    def tearDown(self):
        task_watch.stop()
        task_watch.SCAN_SEC = 1.0
        shutil.rmtree(self.tmp, ignore_errors=True)

    def apps(self):
        return [e for e in self.bus.evs if e.get("type") == "apps.updated"]

    def wait(self, timeout=3.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.apps():
                return True
            time.sleep(0.02)
        return False

    def test_两份都看(self):
        got = task_watch._scan_reg(self.tmp)
        self.assertEqual(len(got), 2, "少看了一份:%s" % list(got))

    def test_第一轮不推(self):
        self.assertEqual(self.apps(), [])

    def test_改了就推(self):
        with open(os.path.join(self.tmp, ".workbench", "apps.json"), "w", encoding="utf-8") as f:
            f.write('[{"name": "x"}]')
        self.assertTrue(self.wait(), "改了却没推")

    def test_rev与apps_rev端点同口径(self):
        """两条路(事件与兜底轮询)要认同一个数,否则前端比对时会来回打架。
        端点取的是两份文件 mtime 的最大值、毫秒。"""
        reg = task_watch._scan_reg(self.tmp)
        want = max(int(os.stat(p).st_mtime * 1000)
                   for p in reg)
        self.assertLessEqual(abs(task_watch._rev_of(reg) - want), 1,
                             "rev 口径与 /workbench/apps-rev 对不上")

    def test_推的那条带rev(self):
        with open(os.path.join(self.tmp, ".workbench", "domains.json"), "w", encoding="utf-8") as f:
            f.write('[{"id": "y"}]')
        self.assertTrue(self.wait())
        self.assertIn("rev", self.apps()[0]["properties"])

    def test_没变就不推(self):
        time.sleep(0.4)
        self.assertEqual(self.apps(), [])

    def test_与任务那条互不干扰(self):
        """同一个线程扫两类,一类出问题不该带走另一类。"""
        os.makedirs(os.path.join(self.tmp, "domains", "risk"))
        with open(os.path.join(self.tmp, "domains", "risk", "TASKS.md"), "w", encoding="utf-8") as f:
            f.write("- [ ] a\n")
        t0 = time.time()
        while time.time() - t0 < 3.0:
            if [e for e in self.bus.evs if e.get("type") == "task.updated"]:
                break
            time.sleep(0.02)
        self.assertTrue([e for e in self.bus.evs if e.get("type") == "task.updated"],
                        "加了注册表看护之后,任务那条不推了")

if __name__ == "__main__":
    unittest.main()
