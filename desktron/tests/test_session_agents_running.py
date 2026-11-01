"""会话列表里的「子代理在跑」这一维。

侧栏那颗点原来只看 busy(会话主线在不在跑)。异步派出的子代理活过派它出来的那个回合,
主线答完之后 busy 转假、GUI 会话的 alive 本来就是假,而后台确实还有活在跑,点却不闪了
(2026-08-14 用户实见)。所以会话列表补一个 agents 字段:此刻在跑的子代理个数。

判据不另造一套,复用 subagents() 那一份(见 test_subagent_status)。这里核三件事:
计数只认 running;入口按引擎会话 uuid 走,终端那侧开的、不在适配器索引里的会话也算得出;
以及列表接口不为这一维付时间——请求只贴后台算好的数,且只对进程还在的会话算。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FRONTEND = os.path.join(ROOT, "frontend")
PANEL = os.path.join(FRONTEND, "app-panel.js")
STYLE = os.path.join(FRONTEND, "style.css")
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
_argv = sys.argv
sys.argv = ["ui_server.py", FRONTEND, "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv

ESID = "ed6d6e9e-2020-4f58-89a2-5750d28db8a4"
ASYNC_RESULT = ("Async agent launched successfully.\nagentId: %s (internal ID - do not "
                "mention to user.)\nThe agent is working in the background.")


class Bus:
    def publish(self, ev):
        pass


def _call(tuid):
    return {"type": "assistant", "uuid": "c-" + tuid, "timestamp": "2026-08-14T09:10:00.000Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tuid, "name": "Agent",
                 "input": {"description": "某件事", "subagent_type": "general-purpose"}}]}}


def _result(tuid, text):
    return {"type": "user", "uuid": "r-" + tuid, "timestamp": "2026-08-14T09:10:01.000Z",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tuid,
                 "content": [{"type": "text", "text": text}]}]}}


def _notif(aid):
    """跑完时注入的那条通知。时刻取此刻:它要与子代理记录的 mtime 放在一起比(记录在通知
    之后又动过=被恢复、正接着跑),写死一个过去的时刻会让样本看起来都像刚被恢复过。"""
    from datetime import datetime, timezone
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:23] + "Z"
    body = ("<task-notification>\n<task-id>%s</task-id>\n<status>completed</status>\n"
            "</task-notification>" % aid)
    return {"type": "user", "uuid": "n-" + aid, "timestamp": at,
            "origin": {"kind": "task-notification"},
            "message": {"role": "user", "content": body}}


def _absorbed_notif(aid):
    """回合进行中交回:CLI 把通知并进当前回合,只记成 attachment,不再另写 user 条目。"""
    from datetime import datetime, timezone
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:23] + "Z"
    body = ("<task-notification>\n<task-id>%s</task-id>\n<status>completed</status>\n"
            "</task-notification>" % aid)
    return {"type": "attachment", "uuid": "q-" + aid, "timestamp": at,
            "attachment": {"type": "queued_command", "prompt": body,
                           "commandMode": "task-notification",
                           "origin": {"kind": "task-notification", "producer": "session-task"}}}


class RunningAgentsCount(unittest.TestCase):
    """适配器这一侧:按引擎会话 uuid 问「有几个在跑」。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, ESID + ".jsonl")
        self.dir = os.path.join(self.tmp.name, ESID, "subagents")
        os.makedirs(self.dir)
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, Bus())
        self.ad.index = {"s1": {"engine_sid": ESID}}
        self.ad._transcript_path = lambda esid: self.path
        self._rollout([])

    def tearDown(self):
        self.tmp.cleanup()

    def _rollout(self, rows):
        with open(self.path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def _agent(self, aid, tuid):
        p = os.path.join(self.dir, "agent-%s.jsonl" % aid)
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "timestamp": "2026-08-14T09:10:00.000Z",
                                "message": {"content": "去查一件事"}}, ensure_ascii=False) + "\n")
        with open(p[:-len(".jsonl")] + ".meta.json", "w", encoding="utf-8") as f:
            json.dump({"agentType": "general-purpose", "toolUseId": tuid,
                       "description": "某件事", "spawnDepth": 1}, f)

    def test_只数还在跑的那些(self):
        self._agent("a1111111111111111", "toolu_01aaaaaaaaaaaaaaaaaaaaaa")
        self._agent("a2222222222222222", "toolu_01bbbbbbbbbbbbbbbbbbbbbb")
        self._agent("a3333333333333333", "toolu_01cccccccccccccccccccccc")
        self._rollout([_call("toolu_01aaaaaaaaaaaaaaaaaaaaaa"),
                       _result("toolu_01aaaaaaaaaaaaaaaaaaaaaa", ASYNC_RESULT % "a1111111111111111"),
                       _call("toolu_01bbbbbbbbbbbbbbbbbbbbbb"),
                       _result("toolu_01bbbbbbbbbbbbbbbbbbbbbb", ASYNC_RESULT % "a2222222222222222"),
                       _call("toolu_01cccccccccccccccccccccc"),
                       _result("toolu_01cccccccccccccccccccccc", ASYNC_RESULT % "a3333333333333333"),
                       _notif("a3333333333333333")])
        self.assertEqual([a["status"] for a in self.ad.subagents("s1")].count("running"), 2)
        self.assertEqual(self.ad.running_agents(ESID), 2)

    def test_并进当前回合的通知也算交回(self):
        self._agent("a1111111111111111", "toolu_01aaaaaaaaaaaaaaaaaaaaaa")
        self._agent("a2222222222222222", "toolu_01bbbbbbbbbbbbbbbbbbbbbb")
        self._rollout([_call("toolu_01aaaaaaaaaaaaaaaaaaaaaa"),
                       _result("toolu_01aaaaaaaaaaaaaaaaaaaaaa", ASYNC_RESULT % "a1111111111111111"),
                       _call("toolu_01bbbbbbbbbbbbbbbbbbbbbb"),
                       _result("toolu_01bbbbbbbbbbbbbbbbbbbbbb", ASYNC_RESULT % "a2222222222222222"),
                       _absorbed_notif("a1111111111111111")])
        self.assertEqual(self.ad.running_agents(ESID), 1)

    def test_普通用户消息里的通知字样不算交回(self):
        self._agent("a1111111111111111", "toolu_01aaaaaaaaaaaaaaaaaaaaaa")
        typed = _notif("a1111111111111111")
        typed.pop("origin")
        typed["message"]["content"] = "帮我看看这段:" + typed["message"]["content"]
        self._rollout([_call("toolu_01aaaaaaaaaaaaaaaaaaaaaa"),
                       _result("toolu_01aaaaaaaaaaaaaaaaaaaaaa", ASYNC_RESULT % "a1111111111111111"),
                       typed])
        self.assertEqual(self.ad.running_agents(ESID), 1)

    def test_一个都没派出时是零(self):
        self.assertEqual(self.ad.running_agents(ESID), 0)

    def test_索引里没有这段会话也算得出(self):
        """终端那侧开的会话不在适配器索引里(索引记的是 GUI 会话),而它照样派子代理。
        入口按引擎会话 uuid 走,进程实例那一维给不出就不参与,不因此把在跑的判死。"""
        self.ad.index = {}
        self._agent("a1111111111111111", "toolu_01aaaaaaaaaaaaaaaaaaaaaa")
        self._rollout([_call("toolu_01aaaaaaaaaaaaaaaaaaaaaa"),
                       _result("toolu_01aaaaaaaaaaaaaaaaaaaaaa", ASYNC_RESULT % "a1111111111111111")])
        self.assertEqual(self.ad.subagents("s1"), [])   # 按 Desk 会话 id 问,查不到就没有
        self.assertEqual(self.ad.running_agents(ESID), 1)

    def test_不派子代理的引擎恒为零(self):
        self.assertEqual(ea.CliAdapter.running_agents(self.ad, ESID + "-x"), 0)


def _load_term():
    base = tempfile.mkdtemp(prefix="agents-run-test-")
    repo, front, data = (os.path.join(base, x) for x in ("repo", "frontend", "data"))
    for d in (repo, front, data):
        os.makedirs(d, exist_ok=True)
    sys.argv = ["test", front, "0", "0", repo]
    if "domains" in sys.modules and not hasattr(sys.modules["domains"], "_domain_for_cwd"):
        del sys.modules["domains"]
    stub = types.ModuleType("termpty")
    stub.pty_open = lambda *a, **k: None
    stub.pty_kill = lambda *a, **k: None
    stub.pty_input = lambda *a, **k: None
    sys.modules.setdefault("termpty", stub)
    import term  # noqa: E402
    return term, base


class AnnotateSessionRows(unittest.TestCase):
    """会话列表这一侧:谁要算、什么时候算、字段怎么贴。"""

    @classmethod
    def setUpClass(cls):
        cls.term, cls.base = _load_term()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        self.asked = []
        counts = self.counts = {}

        class Ad:
            def running_agents(_self, esid):
                self.asked.append(esid)
                return counts.get(esid, 0)

        self._real = self.term._adapter
        self.term._adapter = lambda name: Ad()
        with self.term._AGENTS_LOCK:
            self.term._AGENTS_RUN.clear()

    def tearDown(self):
        self.term._adapter = self._real
        with self.term._AGENTS_LOCK:
            self.term._AGENTS_RUN.clear()

    def _annotate(self, rows):
        """算一趟并等后台那条线程落数:第一次拉到的必然是空的,下一拍才有数。"""
        out = self.term._term_agents_annotate([dict(r) for r in rows])
        for _ in range(200):
            with self.term._AGENTS_LOCK:
                if not self.term._AGENTS_WORKING:
                    break
            time.sleep(0.01)
        return out, self.term._term_agents_annotate([dict(r) for r in rows])

    def test_在跑的个数贴到行上没在跑的不带这个字段(self):
        rows = [{"id": "e1", "engine": "claude", "alive": False, "busy": False, "resident": True},
                {"id": "e2", "engine": "claude", "alive": True, "busy": False}]
        self.counts["e1"] = 2
        first, second = self._annotate(rows)
        self.assertEqual([r.get("agents") for r in first], [None, None])   # 首拉:缓存还空着
        self.assertEqual([r.get("agents") for r in second], [2, None])

    def test_只问进程还在的会话(self):
        """子代理活在派它出来的那个引擎进程里,进程没了它必然跟着没。休眠会话不必算——
        会话列表是侧栏几秒一拉的接口,逐条算一遍冷起要几秒。"""
        rows = [{"id": "live", "engine": "claude", "alive": True, "busy": False},
                {"id": "busy", "engine": "claude", "alive": False, "busy": True},
                {"id": "res", "engine": "claude", "alive": False, "busy": False, "resident": True},
                {"id": "dorm", "engine": "claude", "alive": False, "busy": False},
                {"id": "other", "engine": "cursor-agent", "alive": True, "busy": False}]
        self._annotate(rows)
        self.assertEqual(sorted(self.asked), ["busy", "live", "res"])

    def test_请求本身不算只读缓存(self):
        """算的是后台那条线程。缓存还新鲜时,一趟连适配器都不问。"""
        rows = [{"id": "e1", "engine": "claude", "alive": True, "busy": False}]
        self._annotate(rows)
        self.asked.clear()
        self.term._term_agents_annotate([dict(r) for r in rows])
        self.assertEqual(self.asked, [])

    def test_取引擎会话uuid而不是内部序号(self):
        """codex 的活会话行,id 是 PTY 内部序号,记录要按 resume_id 找。"""
        rows = [{"id": "pty-3", "resume_id": "u-9", "engine": "codex", "alive": True}]
        self.counts["u-9"] = 1
        _first, second = self._annotate(rows)
        self.assertEqual(self.asked, ["u-9"])
        self.assertEqual(second[0].get("agents"), 1)

    def test_进程没了的那条不留旧数(self):
        rows = [{"id": "e1", "engine": "claude", "alive": True, "busy": False}]
        self.counts["e1"] = 3
        self._annotate(rows)
        gone = [{"id": "e1", "engine": "claude", "alive": False, "busy": False}]
        self.assertIsNone(self.term._term_agents_annotate(gone)[0].get("agents"))

    def test_列表接口把这一维贴上去(self):
        src = open(os.path.join(ROOT, "desktron", "lib", "session", "api.py"), encoding="utf-8").read()
        self.assertIn("snapshot = _term_snapshot()", src)
        import inspect
        self.assertIn("_term_agents_annotate(rows)", inspect.getsource(self.term._term_snapshot))


HARNESS = r"""
SRC
const out = CASES.map((s) => [termState(s), termStatTip(s)]);
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class SidebarDotState(unittest.TestCase):
    def _run(self, cases):
        js = _srcmap.panel_src()
        src = 'const _termReadPending = new Map();\n' + '\n'.join(
            re.search(r"function " + name + r"\([^\n]*\).*?\n\}\n", js, re.S).group(0)
            for name in ('termIdentityIds', 'termCompletionKeys', 'termCompletionRead',
                         'termCompletionUnread', 'termState', 'termStatTip'))
        out = subprocess.run(["node", "-e", HARNESS.replace("SRC", src)
                              .replace("CASES", json.dumps(cases, ensure_ascii=False))],
                             capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_子代理在跑而主线闲着的也算在动(self):
        got = self._run([
            {"busy": True, "alive": False, "agents": 2},        # 主线在跑:仍是工作中
            {"busy": False, "alive": False, "agents": 2},       # GUI 会话:主线答完了,子代理还在跑
            {"busy": False, "alive": True, "agents": 1},        # 活终端:同上
            {"busy": False, "alive": False, "unread": True},    # 没有子代理:四档照旧
            {"busy": False, "alive": True, "unread": True},
            {"busy": False, "alive": True},
        ])
        self.assertEqual([x[0] for x in got],
                         ["working", "agents", "agents", "unread", "unread", "idle"])
        self.assertEqual([x[1] for x in got],
                         ["工作中", "子代理工作中，2 个", "子代理工作中",
                          "已完成，有未读", "已完成，有未读", "空闲"])

    def test_复用现有那条呼吸(self):
        # 与主线在跑那一档一字不差:同一条 ti-halo、同一个节奏,不另给一种动法。
        with open(STYLE, encoding="utf-8") as f:
            css = f.read()
        agents = re.search(r"\.ti-stat\.st-agents \.ti-pt::after \{([^}]+)\}", css)
        working = re.search(r"\.ti-stat\.st-working \.ti-pt::after \{([^}]+)\}", css)
        self.assertTrue(agents and working)
        self.assertIn("animation: ti-halo", agents.group(1))
        self.assertEqual(agents.group(1).strip(), working.group(1).strip())

    def test_轮询也更新悬浮说明(self):
        body = re.search(r"function updateTermRow\(.*?\n\}\n", _srcmap.panel_src(), re.S).group(0)
        self.assertIn("termStatTip(t)", body)


if __name__ == "__main__":
    unittest.main()
