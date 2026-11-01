"""会话 recap → journal（统一记忆引擎的输入层）。

不派 agent 事后读原始记录，而是回合收尾后趁上下文还在缓存里，
向同一段会话追加一句固定蒸馏问话。真机实测(claude,2026-08-28):cache 读 19k、
output 471,transcript 行数不变——零污染成立。
"""
import json
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
import inspect
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
LIB = os.path.join(os.path.dirname(__file__), "..", "lib")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea
    from engine_adapters import recap as rc
    import codex_appserver as ca
finally:
    sys.argv = _argv


class Bus:
    def publish(self, *a, **k):
        pass


def _claude(tmp):
    ad = ea.ClaudeAdapter(tmp, tmp, Bus())
    sid = ad.create_session("desk-iter")["id"]
    ad.index[sid]["engine_sid"] = "fake-uuid"
    ad.index[sid]["title"] = "测试会话"
    return ad, sid


def _complete(ad, sid, seq, opts=None, add_turn=True):
    """造一条真实用户回合边界；seq 是 generation,不能拿它冒充轮数。"""
    if add_turn:
        mid = f"u{seq}"
        ad._append_msg(sid, {"info": {"id": mid, "role": "user", "sessionID": sid,
                                      "seq": seq, "source": ad.SRC_USER},
                             "parts": [{"type": "text", "text": f"第 {seq} 轮"}]})
    ad._turn_seq[sid] = seq
    return {"seq": seq, "source": ad.SRC_USER, "opts": dict(opts or {})}


class ArmAndGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad, self.sid = _claude(self.tmp.name)
        self.calls = []
        self.ad.recap_generate = lambda s, run, snap: self.calls.append(s) or "做了 A;定了 B。"
        import deskcfg
        self._data_home = deskcfg.DATA_HOME
        deskcfg.DATA_HOME = self.tmp.name
        self._delay = rc.RECAP_DELAY_S
        self._request_delay = rc.RECAP_REQUEST_DELAY_S
        rc.RECAP_DELAY_S = 0.05
        rc.RECAP_REQUEST_DELAY_S = 0.05

    def tearDown(self):
        self.ad._recap_cancel(self.sid)
        rc.RECAP_DELAY_S = self._delay
        rc.RECAP_REQUEST_DELAY_S = self._request_delay
        import deskcfg
        deskcfg.DATA_HOME = self._data_home
        self.tmp.cleanup()

    def _wait(self):
        time.sleep(0.4)

    def _wait_for(self, predicate, timeout=10):
        """Recap projection runs on a worker thread; poll instead of trusting a fixed sleep."""
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.05)
        return predicate()

    def _request(self):
        p = self.ad._recap_request_path(self.sid)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"requested_at": time.time()}, f)
        return p

    def test_收尾后生成并落_journal(self):
        self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 2))
        self._wait()
        self.assertEqual(self.calls, [self.sid])
        p = os.path.join(self.tmp.name, ".memory", "desk-iter", "journal", "journal.md")
        self.assertTrue(os.path.exists(p), "journal 的 root 是 mem_root(.memory),传错一层不报错但落空(首验踩到)")
        with open(p, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("[recap:claude]「测试会话」 做了 A;定了 B。", body)
        state = json.load(open(self.ad._recap_state_path(), encoding="utf-8"))
        self.assertEqual(state["last_result"], "written")
        self.assertEqual(state["fails"], 0)

    def test_结构化事件投影任务且不污染_journal(self):
        item, _, err = __import__("task_ledger").apply(
            self.tmp.name, self.tmp.name, "desk-iter",
            {"intent": "create", "title": "实现任务账本"}, {"id": self.sid})
        self.assertFalse(err)
        self.ad.recap_generate = lambda s, run, snap: (
            'LOG:完成任务账本核心。\nEVENT:{"task_changes":[{"intent":"progress",'
            '"task_id":"' + item['t'] + '",'
            '"title":"实现任务账本","done":"核心完成","next":"接前端","confidence":"high"}],'
            '"memory_candidates":[{"kind":"decision","statement":"任务保存当前态，journal 保存流水",'
            '"reason":"跨任务边界"}]}')
        self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 20)); self._wait()
        path = os.path.join(self.tmp.name, "domains", "desk-iter", "TASKS.md")
        items = __import__("tasks").read_tasks(path)
        self.assertEqual(items[0]["title"], "实现任务账本")
        self.assertEqual(items[0]["state"], "doing")
        ledger_path = os.path.join(self.tmp.name, "task-ledger.json")
        def ledger():
            with open(ledger_path, encoding="utf-8") as stream:
                return json.load(stream)
        self.assertTrue(self._wait_for(lambda: "memory_candidates" in ledger()), "记忆候选没有落盘")
        self.assertEqual(len(ledger()["memory_candidates"]), 1)
        journal = open(os.path.join(self.tmp.name, ".memory", "desk-iter", "journal", "journal.md"), encoding="utf-8").read()
        self.assertIn("完成任务账本核心。", journal)
        self.assertNotIn("EVENT:", journal)

    def test_高置信_recap_也不能新建任务(self):
        self.ad.recap_generate = lambda *a: (
            'LOG:已完成旧事项。\nEVENT:{"task_changes":[{"intent":"create",'
            '"title":"改个说法重复旧任务","confidence":"high"}]}')
        self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 22)); self._wait()
        path = os.path.join(self.tmp.name, "domains", "desk-iter", "TASKS.md")
        self.assertEqual(__import__("tasks").read_tasks(path), [])

    def test_contract_tasks_are_not_sent_to_recap_for_completion(self):
        ledger = __import__('task_ledger')
        item, _, error = ledger.apply(self.tmp.name, self.tmp.name, 'desk-iter',
            {'intent':'create', 'title':'契约任务',
             'contract':{'criteria':[{'id':'report','kind':'artifact','summary':'报告交付'}]}},
            {'id': self.sid})
        self.assertFalse(error)
        snap = self.ad._recap_snapshot(self.sid, _complete(self.ad, self.sid, 25))
        self.assertNotIn(item['t'], [x['task_id'] for x in snap['task_bindings']])

    def test_recap_快照限制归属并拒绝覆盖后来的更新(self):
        ledger = __import__("task_ledger")
        def create(title, sid):
            return ledger.apply(self.tmp.name, self.tmp.name, "desk-iter",
                {"intent":"create", "title":title}, {"id":sid})[0]
        own, other = create("本会话任务", self.sid), create("其他会话任务", "other")
        snap = self.ad._recap_snapshot(self.sid, _complete(self.ad, self.sid, 23))
        self.assertEqual([x['task_id'] for x in snap['task_bindings']], [own['t']])
        self.assertIn(own['t'], rc.recap_prompt(snap))
        ledger.apply(self.tmp.name, self.tmp.name, "desk-iter",
            {"task_id":own['t'], "intent":"progress", "next":"新的验收条件"}, {"id":self.sid})
        self.ad._recap_project_event(self.sid, 23, {"task_changes":[
            {"task_id":own['t'], "intent":"complete", "done":"旧结果", "confidence":"high"},
            {"task_id":other['t'], "intent":"progress", "next":"错误更新"}]}, snap)
        self.assertEqual(ledger.get(self.tmp.name, "desk-iter", own['t'])['next'], "新的验收条件")
        self.assertNotIn('next', ledger.get(self.tmp.name, "desk-iter", other['t']))

    def test_UI调整后的开放任务仍进入新recap快照(self):
        ledger = __import__("task_ledger")
        item, _, err = ledger.apply(self.tmp.name, self.tmp.name, "desk-iter",
            {"intent": "create", "title": "等待明确完成"}, {"id": self.sid})
        self.assertFalse(err)
        ledger.ui_override(self.tmp.name, "desk-iter", item["t"], state="doing")
        snap = self.ad._recap_snapshot(self.sid, _complete(self.ad, self.sid, 24))
        self.assertEqual([x["task_id"] for x in snap["task_bindings"]], [item["t"]])
        self.assertEqual(snap["task_bindings"][0]["revision"],
                         ledger.get(self.tmp.name, "desk-iter", item["t"])["revision"])

    def test_损坏事件不阻断_journal(self):
        self.ad.recap_generate = lambda s, run, snap: "LOG:留下有效流水。\nEVENT:{broken"
        self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 21)); self._wait()
        journal = open(os.path.join(self.tmp.name, ".memory", "desk-iter", "journal", "journal.md"), encoding="utf-8").read()
        self.assertIn("留下有效流水。", journal)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "task-ledger.json")))

    def test_低置信_recap_不能自动建任务(self):
        self.ad.recap_generate = lambda s, run, snap: (
            'LOG:讨论了一个可能方向。\nEVENT:{"task_changes":[{"intent":"create",'
            '"title":"尚未拍板的方向","confidence":"low"}]}')
        self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 22)); self._wait()
        path = os.path.join(self.tmp.name, "domains", "desk-iter", "TASKS.md")
        self.assertEqual(__import__("tasks").read_tasks(path), [])

    def test_同一轮不重复(self):
        self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 2)); self._wait()
        self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 2, add_turn=False)); self._wait()
        self.assertEqual(len(self.calls), 1)

    def test_新轮再出一条(self):
        self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 2)); self._wait()
        self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 3)); self._wait()
        self.assertEqual(len(self.calls), 2)

    def test_在跑不发(self):
        """计时器竞态漏网的那种:到点时新轮已开始,recap 会拿到半截状态,放弃。"""
        self.ad._live[self.sid] = {"x": 1}
        self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 2)); self._wait()
        self.assertEqual(self.calls, [])

    def test_新轮布防会作废旧计时器(self):
        rc.RECAP_DELAY_S = 0.3
        self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 2))
        self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 3))
        time.sleep(0.7)
        self.assertEqual(len(self.calls), 1, "旧计时器该被 cancel,不然一段收尾出两条")

    def test_总开关(self):
        with mock.patch.dict(os.environ, {"DESKTRON_RECAP": "off"}):
            self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 2))
        self._wait()
        self.assertEqual(self.calls, [])

    def test_显式请求立即走同一隔离支线并在成功后清标记(self):
        rc.RECAP_DELAY_S = 10
        marker = self._request()
        self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 6))
        self._wait()
        self.assertEqual(self.calls, [self.sid])
        self.assertFalse(os.path.exists(marker))
        p = os.path.join(self.tmp.name, ".memory", "desk-iter", "journal", "journal.md")
        with open(p, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("[recap:claude]", body)

    def test_显式请求被新回合抢占时保留(self):
        marker = self._request()
        started = rc.threading.Event()

        def generate(sid, run, snap):
            started.set()
            run.cancelled.wait(1)
            return "不应落盘。"

        self.ad.recap_generate = generate
        self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 7))
        self.assertTrue(started.wait(1))
        self.ad._recap_cancel(self.sid)
        time.sleep(0.1)
        self.assertTrue(os.path.exists(marker), "抢占只取消支线，主动留存请求不能一起丢")

    def test_删除会话清理显式请求(self):
        marker = self._request()
        self.ad.delete_session(self.sid)
        self.assertFalse(os.path.exists(marker))

    def test_无实质进展不落盘(self):
        self.ad.recap_generate = lambda s, run, snap: "无实质进展"
        self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 2)); self._wait()
        p = os.path.join(self.tmp.name, ".memory", "desk-iter", "journal", "journal.md")
        self.assertFalse(os.path.exists(p))
        state = json.load(open(self.ad._recap_state_path(), encoding="utf-8"))
        self.assertEqual(state["last_result"], "empty")
        self.assertEqual(state["fails"], 0)

    def test_空结果记为失败并保留请求(self):
        marker = self._request()
        self.ad.recap_generate = lambda s, run, snap: ""
        self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 12)); self._wait()
        state = json.load(open(self.ad._recap_state_path(), encoding="utf-8"))
        self.assertEqual(state["fails"], 1)
        self.assertIn("未返回正文", state["last_error"]["detail"])
        self.assertTrue(os.path.exists(marker))

    def test_空结果带原因落进状态(self):
        def gen(s, run, snap):
            run.reason = "超时 120s"
            return ""
        self.ad.recap_generate = gen
        self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 13)); self._wait()
        state = json.load(open(self.ad._recap_state_path(), encoding="utf-8"))
        self.assertEqual(state["last_error"]["detail"], "recap 支线未返回正文:超时 120s")

    def test_各引擎都接入(self):
        for cls in (ea.ClaudeAdapter, ea.CodexAdapter, ea.PiAdapter):
            ad = cls(self.tmp.name, self.tmp.name, Bus())
            sid = ad.create_session()["id"]
            self.assertTrue(callable(ad.recap_generate))
            env = ad._recap_request_env(sid)
            self.assertEqual(env["DESKTRON_RECAP_REQUEST_PATH"],
                             ad._recap_request_path(sid))
            self.assertTrue(env["DESKTRON_RECAP_REQUEST_PATH"].startswith(ad.dir))

    def test_缓存过期静默跳过(self):
        self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 8))
        with self.ad._recap_lock:
            snap = self.ad._recap_snapshots[self.sid]
            snap["completed_at"] -= self.ad._recap_cache_window() + 1
        self.ad._recap_fire(self.sid, 8, snap)
        self.assertEqual(self.calls, [])

    def test_后台任务未完静默跳过(self):
        self.ad._bg_tasks[self.sid] = {"job": int(time.time() * 1000)}
        self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 9)); self._wait()
        self.assertEqual(self.calls, [])

    def test_运行中的支线可取消且不落盘(self):
        started = rc.threading.Event()
        runs = []

        def generate(sid, run, snap):
            runs.append(run)
            started.set()
            run.cancelled.wait(1)
            return "不应落盘。"

        self.ad.recap_generate = generate
        self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 10))
        self.assertTrue(started.wait(1))
        self.ad._recap_cancel(self.sid)
        time.sleep(0.1)
        self.assertTrue(runs[0].cancelled.is_set())
        p = os.path.join(self.tmp.name, ".memory", "desk-iter", "journal", "journal.md")
        self.assertFalse(os.path.exists(p))


class BranchCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    class Sink(io.StringIO):
        def close(self):
            pass   # 测试结束后还要检查控制帧

    class Proc:
        def __init__(self, stdout=""):
            self.stdin = BranchCommands.Sink()
            self.stdout = io.StringIO(stdout)
            self.stderr = io.StringIO("")
            self.returncode = 0
            self.pid = 99999999

        def wait(self, timeout=None):
            return self.returncode

        def communicate(self, timeout=None):
            return self.stdout.read(), self.stderr.read()

        def kill(self):
            self.returncode = -9

    @staticmethod
    def snap(ad, sid, opts=None):
        return {"engine_sid": (ad.index.get(sid) or {}).get("engine_sid") or "",
                "opts": dict(opts or {}), "cwd": ad.session_cwd(sid),
                "seq": 1, "turns": 1, "completed_at": time.time()}

    def test_claude_支线命令形状(self):
        """与正常回合同参数(同 system prompt 缓存前缀才对得上)+ 三样:
        --resume 原会话、--no-session-persistence(零污染)、--max-turns 1。"""
        ad, sid = _claude(self.tmp.name)
        seen = {}

        proc = None

        def fake_popen(cmd, **kw):
            nonlocal proc
            seen["cmd"] = cmd
            proc = self.Proc(
                '{"type":"assistant","message":{"content":[{"type":"text","text":"日志"}]}}\n'
                '{"type":"result","subtype":"success"}\n')
            return proc

        run = rc._RecapRun(sid, 1)
        snap = self.snap(ad, sid, {"_grounding": "当时的系统约定"})
        with mock.patch.object(rc.subprocess, "Popen", fake_popen):
            out = rc.claude_recap(ad, sid, run, snap)
        self.assertEqual(out, "日志")
        c = seen["cmd"]
        self.assertIn("--no-session-persistence", c)
        self.assertIn("--max-turns", c)
        self.assertEqual(c[c.index("--permission-mode") + 1], "manual")
        i = c.index("--resume")
        self.assertEqual(c[i + 1], "fake-uuid")
        self.assertIn("--append-system-prompt", c, "system prompt 不带,缓存前缀就对不上,全价重付")
        self.assertIn("当时的系统约定", c[c.index("--append-system-prompt") + 1],
                      "必须复用主回合快照,不能到点后重算 grounding")

    def test_claude_peer_在进程启动前移除工具设置与_mcp(self):
        ad, sid = _claude(self.tmp.name)
        seen = {}

        def fake_popen(cmd, **kw):
            seen["cmd"] = cmd
            return self.Proc(
                '{"type":"assistant","message":{"content":[{"type":"text","text":"答复"}]}}\n'
                '{"type":"result","subtype":"success"}\n')

        snap = self.snap(ad, sid)
        snap["source"] = "peer"
        run = rc._RecapRun(sid, 1)
        with mock.patch.object(rc.subprocess, "Popen", fake_popen):
            out = rc.claude_recap(ad, sid, run, snap)
        self.assertEqual(out, "答复")
        cmd = seen["cmd"]
        self.assertIn(ad.fork_flag, cmd)
        self.assertTrue(run.prepared.is_set())
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertIn("--disable-slash-commands", cmd)
        self.assertIn("--restricted", cmd)

    def test_claude_assistant_tool_use_也会丢弃结果(self):
        ad, sid = _claude(self.tmp.name)
        proc = self.Proc(
            '{"type":"assistant","message":{"content":['
            '{"type":"tool_use","name":"Bash","input":{}},'
            '{"type":"text","text":"不应采用"}]}}\n'
            '{"type":"result","subtype":"success"}\n')
        snap = self.snap(ad, sid)
        snap["source"] = "peer"
        with mock.patch.object(rc.subprocess, "Popen", lambda cmd, **kw: proc):
            self.assertEqual(rc.claude_recap(ad, sid, rc._RecapRun(sid, 1), snap), "")

    def test_codex_fork_使用软链后的真实会话根(self):
        managed = os.path.join(self.tmp.name, "managed-codex")
        user_home = os.path.join(self.tmp.name, "user-codex")
        os.makedirs(os.path.join(user_home, "sessions"))
        os.makedirs(managed)
        os.symlink(os.path.join(user_home, "sessions"), os.path.join(managed, "sessions"))
        ad = mock.Mock()
        ad.MANAGED_HOME_ENV = "CODEX_HOME"
        ad.APPSERVER_BIN = "codex"
        ad.spawn_env.return_value = {"CODEX_HOME": managed}
        env = rc._fork_engine_env(ad, {})
        self.assertEqual(env["CODEX_HOME"], os.path.realpath(user_home))

    def test_claude_工具尝试会即时拒绝并丢弃结果(self):
        ad, sid = _claude(self.tmp.name)
        proc = self.Proc(
            '{"type":"control_request","request_id":"tool-1","request":'
            '{"subtype":"can_use_tool","tool_name":"Bash","input":{}}}\n'
            '{"type":"assistant","message":{"content":[{"type":"text","text":"不应采用"}]}}\n'
            '{"type":"result","subtype":"success"}\n')
        with mock.patch.object(rc.subprocess, "Popen", lambda cmd, **kw: proc):
            out = rc.claude_recap(
                ad, sid, rc._RecapRun(sid, 1), self.snap(ad, sid))
        self.assertEqual(out, "")
        replies = [json.loads(x) for x in proc.stdin.getvalue().splitlines()]
        denied = [x for x in replies if x.get("type") == "control_response"]
        self.assertEqual(denied[0]["response"]["response"]["behavior"], "deny")

    def test_claude_出错回空(self):
        ad, sid = _claude(self.tmp.name)

        with mock.patch.object(rc.subprocess, "Popen",
                               lambda cmd, **kw: self.Proc(
                                   '{"type":"result","subtype":"success","is_error":true}\n')):
            self.assertEqual(rc.claude_recap(
                ad, sid, rc._RecapRun(sid, 1), self.snap(ad, sid)), "")

class ClaudeForkReason(unittest.TestCase):
    """支线回空必须说清为什么(2026-09-08:claude 连着三次「未返回正文」,状态里只有这七个字)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, stdout, stderr=""):
        ad, sid = _claude(self.tmp.name)
        proc = BranchCommands.Proc(stdout)
        proc.stderr = io.StringIO(stderr)
        run = rc._RecapRun(sid, 1)
        with mock.patch.object(rc.subprocess, "Popen", lambda cmd, **kw: proc):
            out = rc.claude_recap(ad, sid, run, BranchCommands.snap(ad, sid))
        return out, run

    def test_result_出错记下子类型与状态码(self):
        out, run = self._run('{"type":"result","subtype":"error_during_execution","is_error":true,'
                             '"api_error_status":529}\n', stderr="overloaded\n")
        self.assertEqual(out, "")
        self.assertIn("result error_during_execution", run.reason)
        self.assertIn("api=529", run.reason)
        self.assertIn("stderr:overloaded", run.reason)

    def test_没有_result_帧也有说法(self):
        out, run = self._run("", stderr="No conversation found with session ID: x\n")
        self.assertEqual(out, "")
        self.assertTrue(run.reason.startswith("无 result 帧"), run.reason)
        self.assertIn("No conversation found", run.reason)

    def test_工具尝试记为原因(self):
        _, run = self._run('{"type":"control_request","request_id":"t","request":'
                           '{"subtype":"can_use_tool","tool_name":"Bash","input":{}}}\n'
                           '{"type":"result","subtype":"success"}\n')
        self.assertEqual(run.reason, "尝试调用工具")

    def test_成功时不留原因(self):
        out, run = self._run('{"type":"assistant","message":{"content":[{"type":"text","text":"日志"}]}}\n'
                             '{"type":"result","subtype":"success"}\n')
        self.assertEqual(out, "日志")
        self.assertEqual(run.reason, "")


class CodexFamilyFork(unittest.TestCase):
    class Conn:
        frames = []
        instances = []

        def __init__(self, cwd=None, env=None, binary="codex", opts=None):
            self.binary = binary
            self.opts = dict(opts or {})
            self.calls = []
            self.nowait = []
            self.queue = list(self.frames)
            self.proc = mock.Mock(pid=99999998)
            self.closed = False
            self.instances.append(self)

        def initialize(self, **kw):
            self.calls.append(("initialize", {}))

        def request(self, method, params=None, on_notification=None, **kw):
            self.calls.append((method, params or {}))
            if method == "config/read":
                off = {name: False for name in (
                    "shell_tool", "apps", "browser_use", "computer_use", "multi_agent",
                    "hooks", "plugins", "image_generation", "tool_suggest",
                    "web_search_cached", "web_search_request", "artifact", "code_mode",
                    "code_mode_host", "enable_mcp_apps", "js_repl", "multi_agent_v2",
                    "request_permissions_tool", "search_tool", "tool_search", "unified_exec",
                    "view_image", "skill_search", "sleep_tool", "current_time_reminder",
                    "deferred_executor", "in_app_local_automation", "psp",
                    "standalone_web_search", "unified_exec_tty", "memories",
                    "external_agent_memory_import", "memory_self_evolution")}
                return {"config": {"web_search": "disabled", "features": off,
                                    "orchestrator": {"mcp": {"enabled": False}}}}
            if method == "thread/fork":
                return {"thread": {"id": "fork-1"}}
            if method == "turn/start":
                return {"turn": {"id": "turn-1"}}
            return {}

        def read(self, timeout=1):
            if not self.queue:
                raise ca.AppServerTimeout("done")
            method, params = self.queue.pop(0)
            return {"method": method, "params": dict(params, threadId="fork-1", turnId="turn-1")}

        def handle_server_request(self, msg):
            self.calls.append(("server-request", msg))

        def request_nowait(self, method, params=None):
            self.nowait.append((method, params or {}))

        def close(self):
            self.closed = True

    def setUp(self):
        self.Conn.instances = []

    def test_codex_ephemeral_fork_取_final_answer(self):
        self.Conn.frames = [
            ("item/started", {"item": {"id": "compact", "type": "contextCompaction"}}),
            ("item/completed", {"item": {"id": "compact", "type": "contextCompaction"}}),
            ("item/agentMessage/delta", {"itemId": "m1", "delta": "日"}),
            ("item/completed", {"item": {"id": "m1", "type": "agentMessage",
                                           "phase": "final_answer", "text": "日志。"}}),
            ("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}}),
        ]
        with mock.patch.object(ca, "_Connection", self.Conn):
            out = ca.recap_fork("source-1", "总结", binary="codex")
        self.assertEqual(out, "日志。")
        conn = self.Conn.instances[-1]
        fork = dict(conn.calls)["thread/fork"]
        self.assertTrue(fork["ephemeral"])
        self.assertTrue(fork["excludeTurns"])
        self.assertTrue(fork["deferGoalContinuation"])
        self.assertNotIn("thread/delete", [m for m, _ in conn.calls])

    def test_流断开的自动重连不算支线失败(self):
        """长会话的 recap 第一帧就断流,Codex 自己重连 2/5…5/5 后照常给出正文。
        此前任何 error 通知都按终局失败收场,这条会话的 recap 永远结不了,回收闸每分钟
        重开一条 app-server 再失败一次(2026-09-07 实证)。"""
        retry = {"error": {"message": "Reconnecting... 2/5",
                           "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": None}}},
                 "willRetry": True}
        self.Conn.frames = [
            ("item/completed", {"item": {"id": "u1", "type": "userMessage"}}),
            ("error", retry),
            ("error", dict(retry, error={"message": "Reconnecting... 3/5"})),
            ("item/completed", {"item": {"id": "m1", "type": "agentMessage",
                                           "phase": "final_answer", "text": "日志。"}}),
            ("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}}),
        ]
        with mock.patch.object(ca, "_Connection", self.Conn):
            out = ca.recap_fork("source-1", "总结", binary="codex")
        self.assertEqual(out, "日志。")

    def test_终局错误仍按失败收场(self):
        self.Conn.frames = [
            ("error", {"error": {"message": "quota exceeded"}, "willRetry": False}),
        ]
        with mock.patch.object(ca, "_Connection", self.Conn):
            with self.assertRaises(ca.AppServerError):
                ca.recap_fork("source-1", "总结", binary="codex")

    def test_旧版仅缺_defer_仍保留_ephemeral(self):
        class OldConn(self.Conn):
            def request(self, method, params=None, on_notification=None, **kw):
                if method == "thread/fork" and "deferGoalContinuation" in (params or {}):
                    self.calls.append((method, params or {}))
                    raise ca.AppServerError("unknown field deferGoalContinuation")
                return super().request(method, params, on_notification, **kw)

        OldConn.frames = [
            ("item/completed", {"item": {"id": "m1", "type": "agentMessage",
                                           "phase": "final_answer", "text": "日志。"}}),
            ("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}}),
        ]
        OldConn.instances = []
        with mock.patch.object(ca, "_Connection", OldConn):
            out = ca.recap_fork("source-1", "总结", binary="codex")
        self.assertEqual(out, "日志。")
        forks = [p for m, p in OldConn.instances[-1].calls if m == "thread/fork"]
        self.assertEqual(len(forks), 2)
        self.assertTrue(forks[-1]["ephemeral"])
        self.assertNotIn("deferGoalContinuation", forks[-1])
        self.assertNotIn("thread/delete", [m for m, _ in OldConn.instances[-1].calls])

    def test_no_tools_在_appserver_初始化前关闭执行面(self):
        self.Conn.frames = [
            ("item/completed", {"item": {"id": "m1", "type": "agentMessage",
                                           "phase": "final_answer", "text": "答复"}}),
            ("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}}),
        ]
        forked = []
        with mock.patch.object(ca, "_Connection", self.Conn):
            self.assertEqual(ca.recap_fork("source-1", "问询", binary="codex",
                                           require_ephemeral=True, no_tools=True,
                                           on_forked=forked.append), "答复")
        self.assertEqual(forked, ["fork-1"])
        self.assertEqual(self.Conn.instances[-1].opts, {"_no_tools": True})
        argv = ca._appserver_argv("codex", {"_no_tools": True})
        self.assertNotIn("--search", argv)
        self.assertIn("shell_tool", argv)
        self.assertIn("orchestrator.mcp.enabled=false", argv)
        self.assertIn('web_search="disabled"', argv)
        self.assertIn("apps", argv)
        self.assertIn("memories", argv)
        self.assertIn("external_agent_memory_import", argv)
        self.assertIn("features.memory_self_evolution=false", argv)

    def test_no_tools_配置闸未生效时在_fork_前失败(self):
        class LeakyConn(self.Conn):
            instances = []
            def request(self, method, params=None, on_notification=None, **kw):
                if method == "config/read":
                    self.calls.append((method, params or {}))
                    return {"config": {"web_search": "disabled", "features": {},
                                        "orchestrator": {"mcp": {"enabled": True}}}}
                return super().request(method, params, on_notification, **kw)

        with mock.patch.object(ca, "_Connection", LeakyConn):
            with self.assertRaisesRegex(ca.AppServerError, "无法关闭全部工具"):
                ca.recap_fork("source-1", "问询", binary="codex",
                              require_ephemeral=True, no_tools=True)
        self.assertNotIn("thread/fork", [m for m, _ in LeakyConn.instances[-1].calls])

    def test_peer问询拒绝降级为持久fork(self):
        class NoEphemeralConn(self.Conn):
            def request(self, method, params=None, on_notification=None, **kw):
                if method == "thread/fork" and (params or {}).get("ephemeral"):
                    self.calls.append((method, params or {}))
                    raise ca.AppServerError("unknown field ephemeral")
                return super().request(method, params, on_notification, **kw)

        NoEphemeralConn.frames = []
        NoEphemeralConn.instances = []
        with mock.patch.object(ca, "_Connection", NoEphemeralConn):
            with self.assertRaisesRegex(ca.AppServerError, "ephemeral"):
                ca.recap_fork("source-1", "问询", binary="codex",
                              read_only=True, require_ephemeral=True)
        forks = [p for m, p in NoEphemeralConn.instances[-1].calls if m == "thread/fork"]
        self.assertTrue(forks)
        self.assertTrue(all(p.get("ephemeral") for p in forks))

    def test_当前版_defer_与_ephemeral_冲突时降级(self):
        class CurrentConn(self.Conn):
            def request(self, method, params=None, on_notification=None, **kw):
                if (method == "thread/fork" and "deferGoalContinuation" in (params or {})
                        and (params or {}).get("ephemeral")):
                    self.calls.append((method, params or {}))
                    raise ca.AppServerError(
                        "`deferGoalContinuation` cannot be combined with `ephemeral`")
                return super().request(method, params, on_notification, **kw)

        CurrentConn.frames = [
            ("item/completed", {"item": {"id": "m1", "type": "agentMessage",
                                           "phase": "final_answer", "text": "日志。"}}),
            ("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}}),
        ]
        CurrentConn.instances = []
        with mock.patch.object(ca, "_Connection", CurrentConn):
            out = ca.recap_fork("source-1", "总结", binary="codex")
        self.assertEqual(out, "日志。")
        forks = [p for m, p in CurrentConn.instances[-1].calls if m == "thread/fork"]
        self.assertEqual(len(forks), 2)
        self.assertNotIn("deferGoalContinuation", forks[-1])
        self.assertTrue(forks[-1]["ephemeral"])

    def test_旧会话ordinal重复时用临时规范化快照fork(self):
        tmp = tempfile.TemporaryDirectory()
        source = os.path.join(tmp.name, ".codex", "sessions", "2026", "09", "01",
                              "rollout-source-1.jsonl")
        os.makedirs(os.path.dirname(source), exist_ok=True)
        with open(source, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session_meta", "ordinal": 0,
                                "payload": {"id": "source-1", "session_id": "source-1"}}) + "\n")
            for ordinal in (1, 1, 2):
                f.write(json.dumps({"type": "event_msg", "ordinal": ordinal,
                                    "payload": {"type": "token_count"}}) + "\n")

        class ProjectionConn(self.Conn):
            normalized = []

            def request(self, method, params=None, on_notification=None, **kw):
                params = params or {}
                self.calls.append((method, params))
                if method == "thread/read":
                    return {"thread": {"id": "source-1", "path": source}}
                if method == "thread/fork" and "path" not in params:
                    if params.get("threadId") == "source-1":
                        raise ca.AppServerError(
                            "failed to prepare paginated fork: thread-store internal error: "
                            "thread history projection for source-1 expected ordinal 2, got 1")
                    if "deferGoalContinuation" in params:
                        raise ca.AppServerError(
                            "`deferGoalContinuation` cannot be combined with `ephemeral`")
                    return {"thread": {"id": "fork-1"}}
                if method == "thread/resume":
                    with open(params["path"], encoding="utf-8") as f:
                        rows = [json.loads(line) for line in f]
                    self.normalized = [row["ordinal"] for row in rows]
                    self.snapshot_id = rows[0]["payload"]["id"]
                    self.snapshot_path = params["path"]
                    self.assert_snapshot = (params["threadId"] == ""
                                            and rows[0]["payload"]["session_id"] == self.snapshot_id)
                    return {"thread": {"id": self.snapshot_id}}
                if method == "turn/start":
                    return {"turn": {"id": "turn-1"}}
                return {}

        ProjectionConn.frames = [
            ("item/completed", {"item": {"id": "m1", "type": "agentMessage",
                                           "phase": "final_answer", "text": "修复后的日志。"}}),
            ("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}}),
        ]
        ProjectionConn.instances = []
        try:
            with mock.patch.object(ca, "_Connection", ProjectionConn):
                out = ca.recap_fork("source-1", "总结", binary="codex")
            self.assertEqual(out, "修复后的日志。")
            self.assertEqual(ProjectionConn.instances[-1].normalized, [0, 1, 2, 3])
            self.assertTrue(ProjectionConn.instances[-1].assert_snapshot)
            calls = ProjectionConn.instances[-1].calls
            self.assertIn(("thread/read", {"threadId": "source-1", "includeTurns": False}), calls)
            resumed = [p for m, p in calls if m == "thread/resume"]
            self.assertEqual(len(resumed), 1)
            repaired = [p for m, p in calls
                        if m == "thread/fork" and p.get("threadId") != "source-1"]
            self.assertEqual(len(repaired), 2)
            self.assertTrue(repaired[-1]["ephemeral"])
            self.assertNotIn("deferGoalContinuation", repaired[-1])
            self.assertIn(("thread/delete", {"threadId": ProjectionConn.instances[-1].snapshot_id}), calls)
            self.assertFalse(os.path.exists(ProjectionConn.instances[-1].snapshot_path))
        finally:
            tmp.cleanup()

    def test_peer_ordinal损坏时拒绝创建持久修复快照(self):
        class BrokenProjection(self.Conn):
            def request(self, method, params=None, on_notification=None, **kw):
                self.calls.append((method, params or {}))
                if method == "thread/fork":
                    raise ca.AppServerError(
                        "thread history projection for source-1 expected ordinal 2, got 1")
                return super().request(method, params, on_notification, **kw)

        BrokenProjection.instances = []
        with mock.patch.object(ca, "_Connection", BrokenProjection):
            with self.assertRaisesRegex(ca.AppServerError, "expected ordinal"):
                ca.recap_fork("source-1", "问询", binary="codex",
                              require_ephemeral=True, no_tools=True)
        methods = [name for name, _ in BrokenProjection.instances[-1].calls]
        self.assertNotIn("thread/read", methods)
        self.assertNotIn("thread/resume", methods)

    def test_协议错误向上暴露(self):
        class BadConn(self.Conn):
            def initialize(self, **kw):
                raise ca.AppServerError("认证失败")

        BadConn.instances = []
        with mock.patch.object(ca, "_Connection", BadConn):
            with self.assertRaisesRegex(ca.AppServerError, "认证失败"):
                ca.recap_fork("source-1", "总结", binary="codex")

    def test_任何工具尝试都会中止且丢弃结果(self):
        self.Conn.frames = [
            ("item/started", {"item": {"id": "cmd", "type": "commandExecution"}}),
        ]
        with mock.patch.object(ca, "_Connection", self.Conn):
            out = ca.recap_fork("source-1", "总结", binary="codex")
        self.assertEqual(out, "")
        self.assertTrue(any(m == "turn/interrupt" for m, _ in self.Conn.instances[-1].nowait))

    def test_适配器按自身二进制复用同一实现(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            seen = []
            for cls, binary in ((ea.CodexAdapter, "codex"),):
                ad = cls(tmp.name, tmp.name, Bus())
                sid = ad.create_session()["id"]
                ad.index[sid]["engine_sid"] = "source"
                snap = BranchCommands.snap(ad, sid)
                with mock.patch.object(ca, "recap_fork",
                                       lambda *a, **kw: seen.append(kw["binary"]) or "日志"):
                    self.assertEqual(ad.recap_generate(sid, rc._RecapRun(sid, 1), snap), "日志")
            self.assertEqual(seen, ["codex"])
        finally:
            tmp.cleanup()


class EvictRecap(unittest.TestCase):
    """资源回收是可靠性闸：recap 同步成功才释放，失败保留并重试。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad, self.sid = _claude(self.tmp.name)
        self.ad._turn_seq[self.sid] = 1787300000000   # seq 是毫秒时间戳量纲(_seq_next)
        self.calls = []
        self.ad.recap_generate = lambda s, run, snap: self.calls.append(s) or "做了 X。"
        self.ad._recap_arm(
            self.sid, _complete(self.ad, self.sid, 1787300000000, add_turn=True))

    def tearDown(self):
        self.ad._recap_cancel(self.sid)
        self.tmp.cleanup()

    def _evict(self):
        return rc.recap_before_evict(self.sid)

    def test_回收补枪并落判重(self):
        """提前执行后要落判重,第二次回收不能重复生成。"""
        self._evict()
        self.assertEqual(self.calls, [self.sid])
        self._evict()
        self.assertEqual(len(self.calls), 1, "判重要落住,回收路径不能每次都重打")

    def test_回收同步等待生成完成(self):
        order = []

        def generate(sid, run, snap):
            time.sleep(0.08)
            order.append("recap")
            return "完成。"

        self.ad.recap_generate = generate
        t0 = time.monotonic()
        self.assertTrue(self._evict())
        self.assertGreaterEqual(time.monotonic() - t0, 0.07)
        self.assertEqual(order, ["recap"])

    def test_失败不算完成并可重试(self):
        attempts = []

        def generate(sid, run, snap):
            attempts.append(sid)
            if len(attempts) == 1:
                raise RuntimeError("暂时失败")
            return "第二次成功。"

        self.ad.recap_generate = generate
        self.assertFalse(self._evict())
        self.assertNotIn(self.sid, self.ad._recap_done)
        self.assertTrue(self._evict())
        self.assertEqual(len(attempts), 2)

    def test_缓存已冷时回收仍强制完成(self):
        with self.ad._recap_lock:
            self.ad._recap_snapshots[self.sid]["completed_at"] -= self.ad._recap_cache_window() + 1
        self.assertTrue(self._evict())
        self.assertEqual(self.calls, [self.sid])

    def test_零轮会话不打(self):
        sid2 = self.ad.create_session("desk-iter")["id"]
        self.ad.index[sid2]["engine_sid"] = "f2"
        self.assertTrue(rc.recap_before_evict(sid2))
        self.assertEqual(self.calls, [])

    def test_快照意外缺失时从完成态恢复(self):
        sid2 = self.ad.create_session("desk-iter")["id"]
        self.ad.index[sid2]["engine_sid"] = "f3"
        self.ad._turn_seq[sid2] = 123
        _complete(self.ad, sid2, 123)
        self.ad._recap_snapshots.pop(sid2, None)
        self.assertTrue(rc.recap_before_evict(sid2))
        self.assertEqual(self.calls, [sid2])

    def test_在跑不打(self):
        self.ad._live[self.sid] = {"x": 1}
        self.assertFalse(self._evict())
        self.assertEqual(self.calls, [])

    def test_journal_落盘按会话回合幂等(self):
        self.ad._recap_journal(self.sid, 456, "同一条。")
        self.ad._recap_journal(self.sid, 456, "同一条。")
        p = os.path.join(self.tmp.name, ".memory", "desk-iter", "journal", "journal.md")
        with open(p, encoding="utf-8") as f:
            body = f.read()
        self.assertEqual(body.count("[recap:claude]「测试会话」 同一条。"), 1)

    def test_claude_回收先完成_recap_再杀进程(self):
        order = []

        class Proc:
            def kill(inner):
                order.append("kill")

        ep = ea._EngineProc(Proc(), ("k",))
        ea._PERSIST[self.sid] = ep
        try:
            with mock.patch.object(rc, "recap_before_evict",
                                   side_effect=lambda sid, owner=None: order.append("recap") or True):
                self.assertTrue(ea._persist_evict(self.sid, ep))
        finally:
            ea._PERSIST.pop(self.sid, None)
        self.assertEqual(order, ["recap", "kill"])

    def test_codex_回收先完成_recap_再关连接(self):
        order = []

        class Conn:
            def close(inner):
                order.append("close")

        entry = ca._SessionConnection(Conn(), ("k",))
        ca._SESSIONS[self.sid] = entry
        try:
            with mock.patch.object(rc, "recap_before_evict",
                                   side_effect=lambda sid, owner=None: order.append("recap") or True):
                self.assertTrue(ca.evict_session(self.sid, entry))
        finally:
            ca._SESSIONS.pop(self.sid, None)
        self.assertEqual(order, ["recap", "close"])

    def test_回收点已接线(self):
        with open(os.path.join(ROOT, "desktron", "lib", "engine_adapters", "persist.py"),
                  encoding="utf-8") as f:
            src = f.read()
        i = src.index("def _persist_trim")
        j = src.index("def _persist_reap")
        k = src.index("threading.Thread(target=_persist_reap")
        self.assertIn("_persist_evict", src[i:j], "trim 挤出那条没接")
        self.assertIn("_persist_evict", src[j:k], "reap 闲置那条没接")
        with open(os.path.join(ROOT, "desktron", "lib", "codex_appserver.py"),
                  encoding="utf-8") as f:
            codex = f.read()
        self.assertIn('_resident.add_pool("codex", _session_roster, evict_session)', codex)


class Clip(unittest.TestCase):
    def test_按句边界截断(self):
        t = ("一二三四五六七八。" * 100)
        got = rc._clip(t, 100)
        self.assertLessEqual(len(got), 100)
        self.assertTrue(got.endswith("。"))

    def test_短文本原样(self):
        self.assertEqual(rc._clip("短。", 100), "短。")


class ManagedJournalRequest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.script = os.path.join(
            ROOT, ".claude", "skills", "journal", "scripts", "journal_append.py")
        self.gate = os.path.join(
            ROOT, ".claude", "skills", "memory", "scripts", "codex_gate.py")
        self.claude_write_gate = os.path.join(
            ROOT, ".claude", "skills", "memory", "scripts", "mem_gate_hook.py")
        self.claude_bash_gate = os.path.join(
            ROOT, ".claude", "skills", "memory", "scripts", "claude_bash_gate.py")

    def tearDown(self):
        self.tmp.cleanup()

    def test_旧_note_命令在受管会话中只落请求标记(self):
        marker = os.path.join(self.tmp.name, "requests", "one.json")
        env = dict(os.environ, DESKTRON_RECAP_REQUEST_PATH=marker,
                   DESKTRON_REPO_ROOT=self.tmp.name)
        got = subprocess.run(
            [sys.executable, self.script, "--note", "主会话自己写的摘要不应采用", "--key", "旧索引"],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=10, encoding='utf-8')
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(os.path.isfile(marker))
        self.assertIn("recap requested", got.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, ".memory")))

    def test_受管会话直接改_journal_会被写闸拒绝(self):
        env = dict(os.environ, DESKTRON_RECAP_REQUEST_PATH=os.path.join(self.tmp.name, "r"))
        payload = {"tool_name": "apply_patch", "cwd": self.tmp.name, "tool_input": {
            "command": "*** Begin Patch\n*** Update File: .memory/desk/journal/journal.md\n*** End Patch"}}
        got = subprocess.run([sys.executable, self.gate], input=json.dumps(payload), env=env,
                             text=True, capture_output=True, timeout=10, encoding='utf-8')
        # 全量测试进程会装 Desk 的 SIGCHLD 收割器，子进程真实 rc=2 可能被洗成 0；
        # hook 的可观察拒绝正文不会丢，按它断言（同仓内其他子进程测试的既有约定）。
        self.assertIn("隔离 recap", got.stderr)

    def test_受管会话直接_curator_append_会被写闸拒绝(self):
        env = dict(os.environ, DESKTRON_RECAP_REQUEST_PATH=os.path.join(self.tmp.name, "r"))
        payload = {"tool_name": "Bash", "cwd": self.tmp.name,
                   "tool_input": {"command": "curator --root .memory journal append --topic desk --text x"}}
        got = subprocess.run([sys.executable, self.gate], input=json.dumps(payload), env=env,
                             text=True, capture_output=True, timeout=10, encoding='utf-8')
        self.assertIn("journal_append.py --request", got.stderr)

    def test_claude_write_与_bash_同样只允许请求(self):
        env = dict(os.environ, DESKTRON_RECAP_REQUEST_PATH=os.path.join(self.tmp.name, "r"))
        journal = os.path.join(self.tmp.name, ".memory", "desk", "journal", "journal.md")
        write = {"tool_name": "Write", "cwd": self.tmp.name,
                 "tool_input": {"file_path": journal}}
        got = subprocess.run([sys.executable, self.claude_write_gate], input=json.dumps(write),
                             env=env, text=True, capture_output=True, timeout=10, encoding='utf-8')
        self.assertEqual(got.returncode, 0)
        decision = json.loads(got.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("隔离 recap", decision["permissionDecisionReason"])

        bash = {"tool_name": "Bash", "cwd": self.tmp.name,
                "tool_input": {"command": "curator --root .memory journal append --topic desk --text x"}}
        got = subprocess.run([sys.executable, self.claude_bash_gate], input=json.dumps(bash),
                             env=env, text=True, capture_output=True, timeout=10, encoding='utf-8')
        self.assertEqual(got.returncode, 0)
        decision = json.loads(got.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("journal_append.py --request", decision["permissionDecisionReason"])

    def test_外部维护环境仍可直接追加(self):
        from memory_engine.gate import session_journal_blocked
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(session_journal_blocked(
                os.path.join(self.tmp.name, ".memory", "desk", "journal", "journal.md")))


class MigrationContract(unittest.TestCase):
    """recap 是唯一自动会话采集入口;旧文件只留在历史提交与运行态数据里。"""

    def test_旧采集代码与压缩钩子已退役(self):
        lib = os.path.join(ROOT, "desktron", "lib", "memory_engine")
        self.assertFalse(os.path.exists(os.path.join(lib, "capture.py")))
        self.assertFalse(os.path.exists(os.path.join(
            ROOT, ".claude", "skills", "memory", "scripts", "mem_flush_hook.py")))
        with open(os.path.join(ROOT, ".claude", "settings.json"), encoding="utf-8") as f:
            hooks = (json.load(f).get("hooks") or {})
        self.assertNotIn("SessionStart", hooks)

    def test_Host_不再暴露离线会话源(self):
        from memory_engine.providers import Host
        params = inspect.signature(Host).parameters
        self.assertNotIn("sources", params)
        self.assertNotIn("workspace_dirs", params)
        self.assertNotIn("topic_of", params)


if __name__ == "__main__":
    unittest.main()


class ShutdownAndCancelAccounting(unittest.TestCase):
    """终局 recap 与取消记账(2026-09-01)。

    活跃会话的每一条 recap 都可能被下一个回合抢占,真正保底的是终局那一次;而抢占
    本身必须留痕——原来它三处都不写,于是「从没跑成」与「从没跑过」外部看不出区别。
    """

    def test_关停要先跑终局recap再摘进程(self):
        src = open(os.path.join(LIB, "engine_adapters", "persist.py"), encoding="utf-8").read()
        self.assertIn("atexit.register(_persist_shutdown)", src)
        seg = src[src.index("def _persist_shutdown"):src.index("atexit.register(_persist_shutdown)")]
        self.assertIn("recap_on_shutdown()", seg)
        # 顺序:先 recap 再 drop。反了 _PERSIST 就空了,找不到该给谁收尾
        self.assertLess(seg.index("recap_on_shutdown()"), seg.index("_persist_drop(s)"))

    def test_关停不因单段会话卡住(self):
        src = open(os.path.join(LIB, "engine_adapters", "recap.py"), encoding="utf-8").read()
        seg = src[src.index("def recap_on_shutdown"):src.index("def recap_before_evict")]
        self.assertIn("threading.Thread", seg)   # 各会话并行,不串行等
        self.assertIn("t.join(", seg)            # 但统一等一个上限
        self.assertIn("daemon=True", seg)

    def test_关停取消遇到recap锁竞争仍按总时限返回(self):
        class Adapter:
            def __init__(self):
                self.index = {'s': {}}
                self.recap_generate = object()
                self._recap_lock = __import__('threading').Lock()
                self._recap_timers = {};self._recap_snapshots = {}
                self._recap_runs = {}
            _recap_cancel = rc.RecapMixin._recap_cancel
        ad = Adapter();run = rc._RecapRun('s',1);ad._recap_runs['s']=run
        proc=mock.Mock(pid=123456789);run._proc=proc;run._lock.acquire()
        try:
            with mock.patch('engine_adapters.persist._ADAPTERS',[ad]), \
                 mock.patch('engine_adapters.persist._PERSIST',{'s':object()}), \
                 mock.patch.object(rc,'recap_on',return_value=True), \
                 mock.patch.object(rc,'recap_before_evict',side_effect=lambda _sid, _ad=None: time.sleep(.2)), \
                 mock.patch.object(rc._RecapRun,'_stop') as stop:
                started=time.monotonic();rc.recap_on_shutdown(timeout=.01)
                self.assertLess(time.monotonic()-started,.1)
                self.assertTrue(run.cancelled.is_set())
                stop.assert_called_once_with(proc, force=True)
        finally:
            run._lock.release()

    def test_expired_shutdown_launch_budget_still_cancels_existing_run(self):
        class Adapter:
            index = {'s': {}}
            recap_generate = object()
            _recap_cancel = mock.Mock()
        ad = Adapter()
        ep = type('EP', (), {'owner': ad})()
        with mock.patch('engine_adapters.persist._PERSIST', {'s': ep}), \
             mock.patch.object(rc, 'recap_on', return_value=True), \
             mock.patch.object(rc.threading.Thread, 'start') as start:
            rc.recap_on_shutdown(timeout=0)
        start.assert_not_called()
        ad._recap_cancel.assert_called_once_with('s', blocking=False)

    def test_关停线程与取消使用同一适配器(self):
        class Adapter:
            def __init__(self): self.index={'s':{}};self.recap_generate=object()
        selected=Adapter();other=Adapter();seen=[]
        ep=type('EP',(),{'owner':selected})()
        with mock.patch('engine_adapters.persist._ADAPTERS',[other,selected]), \
             mock.patch('engine_adapters.persist._PERSIST',{'s':ep}), \
             mock.patch.object(rc,'recap_on',return_value=True), \
             mock.patch.object(rc,'recap_before_evict',side_effect=lambda sid,ad=None: seen.append((sid,ad)) or True):
            rc.recap_on_shutdown(timeout=.2)
        self.assertEqual(len(seen),1)
        self.assertEqual(seen[0][0],'s')
        self.assertIs(seen[0][1],selected)

    def test_关停墓碑阻止迟到run且晚绑定仍强杀(self):
        class Adapter:
            def __init__(self):
                self._recap_shutdown=__import__('threading').Event()
                self._recap_lock=__import__('threading').Lock()
                self._recap_snapshots={'s':{'seq':1}}
                self._recap_timers={};self._recap_runs={};self._recap_done={}
            _recap_fire=rc.RecapMixin._recap_fire
        ad=Adapter();ad._recap_shutdown.set()
        self.assertFalse(ad._recap_fire('s',1,ad._recap_snapshots['s']))
        self.assertEqual(ad._recap_runs,{})
        run=rc._RecapRun('s',1);run.cancel_nowait();proc=mock.Mock(pid=123456789)
        with mock.patch.object(rc._RecapRun,'_stop') as stop:
            run.bind(proc)
        stop.assert_called_once_with(proc,force=True)

    def test_取消单独记一档且不计失败(self):
        src = open(os.path.join(LIB, "engine_adapters", "recap.py"), encoding="utf-8").read()
        seg = src[src.index("def _recap_record"):src.index("def _recap_cache_ttl")]
        self.assertIn("cancelled=False", seg)
        self.assertIn('state["cancels"]', seg)
        self.assertIn('state["last_cancel"]', seg)
        # 取消档要排在 ok 之前判,否则会被当成成功
        self.assertLess(seg.index("if cancelled:"), seg.index("elif ok:"))
        # 不能进失败计数:被抢占是正常结果,不是故障
        fail_branch = seg[seg.index("else:"):]
        self.assertIn('state["fails"]', fail_branch)
        self.assertNotIn('state["fails"]', seg[seg.index("if cancelled:"):seg.index("elif ok:")])

    def test_复核未通过要记成取消(self):
        src = open(os.path.join(LIB, "engine_adapters", "recap.py"), encoding="utf-8").read()
        seg = src[src.index("def _recap_fire"):src.index("def _recap_journal")]
        self.assertIn("elif not valid:", seg)
        self.assertIn("self._recap_record(False, cancelled=True)", seg)

    def test_取消计数透出到状态页(self):
        src = open(os.path.join(LIB, "agents_status.py"), encoding="utf-8").read()
        self.assertIn('"cancels": int(state.get("cancels") or 0)', src)
        self.assertIn('"last_cancel": int(state.get("last_cancel") or 0)', src)

    def test_空闲等待落在缓存窗口内(self):
        from engine_adapters import recap as R
        # 2026-10-02 起按「用户离开」触发：连续对话中途被抢占不丢内容，最后一次停顿时
        # 生成的那条覆盖全部。等待时间必须给缓存窗口留出一半余量。
        self.assertGreaterEqual(R.RECAP_DELAY_S, 60)
        for engine, ttl in R.RECAP_CACHE_TTL_BY_ENGINE.items():
            ad = type("A", (R.RecapMixin,), {"name": engine})()
            self.assertLessEqual(ad._recap_idle_delay(), ad._recap_cache_window() * 0.5)
            self.assertLessEqual(ad._recap_cache_window() + R.RECAP_LAST_REQUEST_ALLOWANCE_S,
                                 ttl * R.RECAP_CACHE_SAFE_RATIO + 1e-6)


class IdleTriggerTitleAndUsage(unittest.TestCase):
    """2026-10-02 aae5：空闲触发、任务完成立即触发、标题独立生成、旁路用量记账。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad, self.sid = _claude(self.tmp.name)
        import deskcfg
        self._data_home = deskcfg.DATA_HOME
        deskcfg.DATA_HOME = self.tmp.name

    def tearDown(self):
        self.ad._recap_cancel(self.sid)
        import deskcfg
        deskcfg.DATA_HOME = self._data_home
        self.tmp.cleanup()

    def test_普通收尾按空闲等待_任务完成立即发(self):
        with mock.patch.object(self.ad, "_recap_schedule") as schedule:
            self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 2))
        self.assertEqual(schedule.call_args.args[2], self.ad._recap_idle_delay())
        with mock.patch.object(self.ad, "_recap_new_completions", return_value=1), \
                mock.patch.object(self.ad, "_recap_schedule") as schedule:
            self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 3))
        self.assertEqual(schedule.call_args.args[2], rc.RECAP_REQUEST_DELAY_S)

    def test_新完成任务只计一次(self):
        with mock.patch("tasks.read_tasks", return_value=[{"t": "t1", "state": "done"}]), \
                mock.patch("task_ledger.linked_to_session", return_value=[{"task_id": "t1"}]):
            self.assertEqual(self.ad._recap_new_completions(self.sid, "desk-iter"), 0)
            self.assertEqual(self.ad._recap_new_completions(self.sid, "desk-iter"), 0)
        self.ad._recap_seen_done[self.sid] = set()
        with mock.patch("tasks.read_tasks", return_value=[{"t": "t1", "state": "done"}]), \
                mock.patch("task_ledger.linked_to_session", return_value=[{"task_id": "t1"}]):
            self.assertEqual(self.ad._recap_new_completions(self.sid, "desk-iter"), 1)

    def test_配置了_Pi_通道时标题单独生成且_recap_不再附带(self):
        from memory_engine import project_memory
        saved = []
        done = __import__("threading").Event()
        with mock.patch.object(project_memory, "pi_models", return_value=(["deepseek/deepseek-flash"], "")), \
                mock.patch.object(project_memory, "run_short",
                                  return_value=("「图片接入」", "deepseek/deepseek-flash", [],
                                                {"input": 50, "output": 5})), \
                mock.patch.object(rc, "_save_autotitle",
                                  side_effect=lambda *a: (saved.append(a), done.set())), \
                mock.patch.object(rc, "_want_autotitle", return_value=True), \
                mock.patch.object(self.ad, "_recap_schedule") as schedule:
            self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 2))
            self.assertTrue(done.wait(5))
        self.assertEqual(saved, [(self.sid, "图片接入", "fake-uuid")])
        self.assertFalse(schedule.call_args.args[1]["want_title"])
        from usage_stats import sidecar_summary
        rows = sidecar_summary(self.tmp.name)
        self.assertEqual([(r["kind"], r["model"], r["outcome"], r["input"]) for r in rows],
                         [("title", "deepseek/deepseek-flash", "ok", 50)])

    def test_未配置_Pi_通道时标题仍随_recap(self):
        with mock.patch.object(rc, "_want_autotitle", return_value=True), \
                mock.patch.object(self.ad, "_recap_schedule") as schedule:
            self.ad._recap_arm(self.sid, _complete(self.ad, self.sid, 2))
        self.assertTrue(schedule.call_args.args[1]["want_title"])

    def test_recap_用量计入旁路账本(self):
        def generate(_sid, run, _snap):
            run.cache_usage = {"input_tokens": 12, "output_tokens": 40,
                               "cache_read_input_tokens": 90000, "cache_creation_input_tokens": 800}
            return "做了 A。"
        self.ad.recap_generate = generate
        ctx = _complete(self.ad, self.sid, 2, opts={"model": "claude-opus-5-5"})
        self.ad._recap_snapshots[self.sid] = snap = self.ad._recap_snapshot(self.sid, ctx)
        self.assertTrue(self.ad._recap_fire(self.sid, 2, snap))
        from usage_stats import sidecar_summary
        [row] = sidecar_summary(self.tmp.name)
        self.assertEqual((row["kind"], row["engine"], row["model"], row["outcome"]),
                         ("recap", "claude", "claude-opus-5-5", "ok"))
        self.assertEqual((row["input"], row["output"], row["cached"], row["cache_write"]), (12, 40, 90000, 800))
        self.assertAlmostEqual(row["cache_ratio"], 90000 / 90812, places=4)

    def test_标题清洗(self):
        self.assertEqual(rc._clean_title("标题：《记忆引擎降本》。\n多余"), "记忆引擎降本")
        self.assertEqual(rc._clean_title(""), "")
