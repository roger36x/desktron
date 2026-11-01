import os, sys, unittest
import subprocess
import threading
import time
import re
import tempfile
import pytest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import codex_appserver as app


CATALOG = {
    "models": [{
        "id": "gpt-5.6-sol", "model": "gpt-5.6-sol", "displayName": "GPT-5.6-Sol",
        "isDefault": True, "defaultReasoningEffort": "low",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low", "description": "fast"},
            {"reasoningEffort": "xhigh", "description": "deep"}],
    }],
    "modes": [{"name": "Plan", "mode": "plan", "reasoning_effort": "medium"},
              {"name": "Default", "mode": "default", "reasoning_effort": None}],
}


CATALOG_TWO_MODELS = {
    "models": CATALOG["models"] + [{
        "id": "gpt-5.6-terra", "model": "gpt-5.6-terra", "displayName": "GPT-5.6-Terra",
        "isDefault": False, "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "medium", "description": "balanced"},
            {"reasoningEffort": "high", "description": "deep"}],
    }],
    "modes": CATALOG["modes"],
}


class FakeConnection:
    last = None
    instances = []

    def __init__(self, cwd=None, env=None, binary="codex", opts=None):
        self.proc = mock.Mock()
        self.proc.poll.return_value = None
        self.calls = []
        self.opts = dict(opts or {})
        self.closed = 0
        FakeConnection.last = self
        FakeConnection.instances.append(self)

    def initialize(self, on_notification=None, overall=None):
        self.calls.append(("initialize", {}))

    def request(self, method, params=None, on_notification=None, timeout=30, **kw):
        self.calls.append((method, params or {}))
        self.kwargs = getattr(self, "kwargs", {})
        self.kwargs[method] = kw
        if method in ("thread/start", "thread/resume"):
            return {"thread": {"id": "thread-1"}, "model": "gpt-5.6-sol"}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        if method == "thread/read":
            return {"thread": {"id": "thread-1", "turns": [
                {"id": "turn-1", "status": "completed"}]}}
        if method == "mcpServerStatus/list":
            return {"data": [
                {"name": "files", "authStatus": "unsupported", "tools": {}},
                {"name": "browser", "authStatus": "bearerToken", "tools": {}}]}
        return {}

    def read(self, timeout=30):
        return {"method": "turn/completed", "params": {
            "threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}}}

    def close(self):
        self.closed += 1


class CodexAppServer(unittest.TestCase):
    def setUp(self):
        app._drop_all_sessions()
        FakeConnection.instances = []

    def tearDown(self):
        app._drop_all_sessions()

    def test_connection_spawn_is_registered_and_cleaned_when_constructor_fails(self):
        proc = mock.Mock()
        proc.stdin = mock.Mock(); proc.stdout = mock.Mock(); proc.stderr = mock.Mock()
        events = []
        with mock.patch.object(app.subprocess, 'Popen', return_value=proc), \
             mock.patch.object(app.selectors, 'DefaultSelector',
                               side_effect=OSError(24, 'Too many open files')), \
             mock.patch.object(app.threading.Thread, 'start',
                               side_effect=OSError(24, 'Too many open files')), \
             mock.patch.object(app._Connection, '_close_process_tree') as close_tree:
            with self.assertRaisesRegex(OSError, 'Too many open files'):
                app._Connection(cwd='/tmp', env={},
                    on_process=lambda process, status: events.append((process, status)))
        self.assertEqual(events, [(proc, 'active'), (proc, 'stopped')])
        close_tree.assert_called_once_with()
        proc.stdin.close.assert_called_once_with()
        proc.stdout.close.assert_called_once_with()
        proc.stderr.close.assert_called_once_with()

    def test_rename_uses_native_control_before_model_turn(self):
        states = []
        with mock.patch.object(app, "_Connection", FakeConnection), mock.patch.object(app, "catalog", return_value=CATALOG):
            app.run_turn("/work", {}, "", "hello", {}, session_name="Desk SOT", on_name_sync=states.append)
        methods = [m for m, _ in FakeConnection.last.calls]
        self.assertLess(methods.index("thread/name/set"), methods.index("turn/start"))
        self.assertEqual(dict(FakeConnection.last.calls)["thread/name/set"],
                         {"threadId": "thread-1", "name": "Desk SOT"})
        self.assertEqual(states, ["synced"])

    def test_unsupported_rename_keeps_normal_turn(self):
        class OldConnection(FakeConnection):
            def request(self, method, *args, **kwargs):
                if method == "thread/name/set":
                    raise app.AppServerError("Method not found")
                return super().request(method, *args, **kwargs)
        states = []
        with mock.patch.object(app, "_Connection", OldConnection), mock.patch.object(app, "catalog", return_value=CATALOG):
            app.run_turn("/work", {}, "", "hello", {}, session_name="Desk SOT", on_name_sync=states.append)
        self.assertEqual(states, ["unsupported"])
        self.assertIn("turn/start", dict(FakeConnection.last.calls))

    def test_plan_model_effort进入真实_turn_start(self):
        with mock.patch.object(app, "_Connection", FakeConnection), mock.patch.object(app, "catalog", return_value=CATALOG):
            tid = app.run_turn("/work", {}, "", "做事", {
                "collaboration_mode": "plan", "model": "gpt-5.6-sol",
                "effort": "xhigh", "sandbox": "read-only"}, system_text="纪律")
        self.assertEqual(tid, "thread-1")
        calls = dict(FakeConnection.last.calls)
        self.assertEqual(calls["thread/start"]["sandbox"], "read-only")
        self.assertEqual(calls["thread/start"]["developerInstructions"], "纪律")
        turn = calls["turn/start"]
        self.assertEqual(turn["collaborationMode"]["mode"], "plan")
        self.assertEqual(turn["collaborationMode"]["settings"], {
            "model": "gpt-5.6-sol", "reasoning_effort": "xhigh"})
        self.assertNotIn("summary", turn)

    def test_目录解析后的实际模型在派发前回传(self):
        selected = []
        with mock.patch.object(app, "_Connection", FakeConnection), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            app.run_turn("/work", {}, "", "做事", {"model": "missing-alias"},
                         on_model_selected=selected.append)
        self.assertEqual(selected, ["gpt-5.6-sol"])
        self.assertEqual(dict(FakeConnection.last.calls)["turn/start"]
                         ["collaborationMode"]["settings"]["model"], selected[0])

    def test_effort未选择时使用mode预设(self):
        model, effort, mode = app._selected(CATALOG, {"collaboration_mode": "plan"})
        self.assertEqual((model, effort, mode), ("gpt-5.6-sol", "medium", "plan"))

    def test_codex不发送非标准扩展字段(self):
        with mock.patch.object(app, "_Connection", FakeConnection), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            app.run_turn("/work", {}, "", "做事", {"model": "gpt-5.6-sol"})
        self.assertNotIn("modelBackendVariant", dict(FakeConnection.last.calls)["turn/start"])

    def test_服务端请求按各自协议明确拒绝(self):
        conn = object.__new__(app._Connection)
        sent = []
        conn._send = sent.append
        cases = {
            # 拒绝的字面量取自请求自带的 availableDecisions;请求里没给时回退 cancel
            # ——实测 codex 给的是 ["accept", {...}, "cancel"],此前硬写的 decline 服务端
            # 不认,命令会被判失败而不是被拒(2026-08-16)。
            "item/commandExecution/requestApproval": {"decision": "cancel"},
            "item/fileChange/requestApproval": {"decision": "cancel"},
            "item/permissions/requestApproval": {"permissions": {}},
            "applyPatchApproval": {"decision": {"denied": {
                "rejection": "Desktron GUI 当前没有审批交互。"}}},
            "execCommandApproval": {"decision": {"denied": {
                "rejection": "Desktron GUI 当前没有审批交互。"}}},
            "item/tool/requestUserInput": {"answers": {}},
            "mcpServer/elicitation/request": {"action": "decline"},
        }
        for i, (method, result) in enumerate(cases.items(), 1):
            conn._deny_server_request({"jsonrpc": "2.0", "id": i, "method": method})
            self.assertEqual(sent[-1], {"jsonrpc": "2.0", "id": i, "result": result})

    def test_拒绝取请求自带的决定字面量(self):
        """availableDecisions 是服务端给的可选项,不同引擎、不同版本各不相同,照它挑才认。"""
        实测 = {"availableDecisions": ["accept",
                                     {"acceptWithExecpolicyAmendment": {}}, "cancel"]}
        self.assertEqual(app.pick_decision(实测, True), "accept")
        self.assertEqual(app.pick_decision(实测, False), "cancel")
        # 对象形态的选项(批准并放宽策略)不替人做主,只在纯字符串里挑
        self.assertEqual(app.pick_decision({"availableDecisions": [{"x": {}}]}, True), "accept")
        # 换一套枚举照样认
        另一套 = {"availableDecisions": ["approve", "deny"]}
        self.assertEqual(app.pick_decision(另一套, True), "approve")
        self.assertEqual(app.pick_decision(另一套, False), "deny")

    def test_有人接管审批时连接层不再当场拒绝(self):
        """接管之后这条请求挂着等人答,连接层一个字节都不该回——回了服务端就往下走了。"""
        conn = object.__new__(app._Connection)
        sent = []
        conn._send = sent.append
        conn.approval_hook = lambda msg, c: True
        msg = {"jsonrpc": "2.0", "id": 7, "method": "item/commandExecution/requestApproval"}
        self.assertTrue(conn.take_approval(msg))
        self.assertEqual(sent, [])
        self.assertEqual(conn._awaiting, 1)
        conn.answer_approval(7, {"decision": "accept"})
        self.assertEqual(sent[-1], {"jsonrpc": "2.0", "id": 7, "result": {"decision": "accept"}})
        self.assertEqual(conn._awaiting, 0)

    def test_两条读循环都经统一处置(self):
        """审批请求在 request() 与跑回合那条循环里都会落。两处都得走 handle_server_request,
        只改一处的后果实见过:服务端停在 waitingOnApproval,Desk 这侧静默到超时
        (2026-08-16)。裸调 _deny_server_request 的地方只该有它自己那一处。"""
        src = open(app.__file__, encoding="utf-8").read()
        naked = [ln.strip() for ln in src.split("\n")
                 if "_deny_server_request(" in ln and "def " not in ln
                 and "handle_server_request" not in ln]
        # 剩下的应当只有 handle_server_request 内部那一行
        self.assertEqual(len(naked), 1, naked)
        # 定义一处 + 主请求、主回合、recap 临时支线三条读循环各一处调用
        self.assertEqual(src.count("def handle_server_request"), 1)
        self.assertEqual(src.count(".handle_server_request(msg)"), 3)

    def test_接管方不接手时照旧拒绝(self):
        conn = object.__new__(app._Connection)
        sent = []
        conn._send = sent.append
        conn.approval_hook = lambda msg, c: False
        msg = {"jsonrpc": "2.0", "id": 8, "method": "item/commandExecution/requestApproval"}
        self.assertFalse(conn.take_approval(msg))
        conn._deny_server_request(msg)
        self.assertEqual(sent[-1]["result"], {"decision": "cancel"})

    def test_未知服务端请求返回_jsonrpc_错误(self):
        conn = object.__new__(app._Connection)
        sent = []
        conn._send = sent.append
        conn._deny_server_request({"jsonrpc": "2.0", "id": 7, "method": "future/request"})
        self.assertEqual(sent[0]["error"]["code"], -32601)

    def test_同一Desk会话复用连接且只绑定一次thread(self):
        with mock.patch.object(app, "_Connection", FakeConnection), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            tid = app.run_turn("/work", {}, "", "第一轮", {"sandbox": "read-only"},
                               session_id="desk-1")
            app.run_turn("/work", {}, tid, "第二轮", {"sandbox": "read-only"},
                         session_id="desk-1")

        self.assertEqual(len(FakeConnection.instances), 1)
        conn = FakeConnection.instances[0]
        methods = [method for method, _params in conn.calls]
        self.assertEqual(methods.count("initialize"), 1)
        self.assertEqual(methods.count("thread/start"), 1)
        self.assertEqual(methods.count("thread/resume"), 0)
        self.assertEqual(methods.count("turn/start"), 2)
        self.assertEqual(conn.closed, 0)

    def test_切换模型只影响下一轮_turn_不重建连接(self):
        with mock.patch.object(app, "_Connection", FakeConnection), \
             mock.patch.object(app, "catalog", return_value=CATALOG_TWO_MODELS):
            tid = app.run_turn("/work", {}, "", "第一轮", {
                "sandbox": "read-only", "model": "gpt-5.6-sol"}, session_id="desk-1")
            app.run_turn("/work", {}, tid, "第二轮", {
                "sandbox": "read-only", "model": "gpt-5.6-terra"}, session_id="desk-1")

        self.assertEqual(len(FakeConnection.instances), 1)
        conn = FakeConnection.instances[0]
        self.assertEqual([m for m, _ in conn.calls].count("thread/resume"), 0)
        turns = [p for m, p in conn.calls if m == "turn/start"]
        self.assertEqual(turns[-1]["collaborationMode"]["settings"]["model"], "gpt-5.6-terra")

    def test_压缩窗口作为appserver配置且切换后重建连接(self):
        self.assertEqual(app._appserver_argv("codex", {"autocompact": "400000"})[-2:],
                         ["-c", "model_auto_compact_token_limit=400000"])
        self.assertEqual(app._compact_limit({"autocompact":"400000"},
                                            "/opt/desk/bin/codex"),400000)
        with mock.patch.object(app, "_Connection", FakeConnection), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            tid = app.run_turn("/work", {}, "", "第一轮", {
                "sandbox": "read-only", "autocompact": "200000"}, session_id="desk-1")
            app.run_turn("/work", {}, tid, "第二轮", {
                "sandbox": "read-only", "autocompact": "400000"}, session_id="desk-1")

        self.assertEqual(len(FakeConnection.instances), 2)
        self.assertEqual(FakeConnection.instances[0].closed, 1)
        self.assertEqual(FakeConnection.instances[1].opts["autocompact"], "400000")
        methods = [method for method, _params in FakeConnection.instances[1].calls]
        self.assertEqual(methods.count("thread/resume"), 1)

    def test_冷恢复旧thread时不把新模型传给_resume(self):
        with mock.patch.object(app, "_Connection", FakeConnection), \
             mock.patch.object(app, "catalog", return_value=CATALOG_TWO_MODELS):
            app.run_turn("/work", {}, "thread-old", "继续", {
                "sandbox": "read-only", "model": "gpt-5.6-terra"}, session_id="desk-1")

        calls = dict(FakeConnection.last.calls)
        self.assertNotIn("model", calls["thread/resume"])
        self.assertEqual(
            calls["turn/start"]["collaborationMode"]["settings"]["model"], "gpt-5.6-terra")

    def test_环境状态只在常驻进程冷启动时上报(self):
        first, second = [], []
        with mock.patch.object(app, "_Connection", FakeConnection), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            tid = app.run_turn("/work", {}, "", "第一轮", {"sandbox": "read-only"},
                               session_id="desk-1", on_environment=first.append)
            app.run_turn("/work", {}, tid, "第二轮", {"sandbox": "read-only"},
                         session_id="desk-1", on_environment=second.append)

        self.assertEqual(first[0]["stage"], "process")
        self.assertNotIn("mcpServerStatus/list", dict(FakeConnection.last.calls))
        self.assertEqual(first[-1]["state"], "ready")
        # 没有 startupStatus 通知就不猜 MCP 就绪数；线程就绪不代表所有工具就绪。
        self.assertEqual(first[-1]["mcp"]["ready"], 0)
        self.assertEqual(second, [])

    def test_投影历史在归档提交后复用同一进程与线程(self):
        class ProjectedConnection(FakeConnection):
            def request(inner, method, params=None, on_notification=None, timeout=30, **kw):
                result = super().request(method, params, on_notification, timeout, **kw)
                if method == "thread/read":
                    result["thread"]["path"] = "/tmp/projected-rollout.jsonl"
                return result

        snapshots = []
        with mock.patch.object(app, "_Connection", ProjectedConnection), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            first = app.run_turn("/work", {}, "", "第一轮", {"sandbox":"read-only"},
                session_id="shepherd:binding", history_items=[],
                resident_context="context-a", resident_through_seq=0,
                session_recap=False,
                on_working_context=lambda thread,path:snapshots.append(
                    (thread,path,ProjectedConnection.last.closed)))
            conn = ProjectedConnection.last
            self.assertTrue(app.commit_session("shepherd:binding", conn, 1))
            second = app.run_turn("/work", {}, "", "第二轮", {"sandbox":"read-only"},
                session_id="shepherd:binding", history_items=[],
                resident_context="context-a", resident_through_seq=1,
                session_recap=False,
                on_working_context=lambda thread,path:snapshots.append(
                    (thread,path,ProjectedConnection.last.closed)))

        self.assertEqual((first,second),("thread-1","thread-1"))
        self.assertEqual(len(ProjectedConnection.instances),1)
        methods=[method for method,_params in conn.calls]
        self.assertEqual(methods.count("initialize"),1)
        self.assertEqual(methods.count("thread/start"),1)
        self.assertEqual(methods.count("turn/start"),2)
        self.assertEqual([closed for _thread,_path,closed in snapshots],[0,0])

    def test_投影历史校验后同轮图片才派发(self):
        class ProjectedConnection(FakeConnection):
            def request(inner, method, params=None, on_notification=None, timeout=30, **kw):
                result = super().request(method, params, on_notification, timeout, **kw)
                if method == 'thread/read':
                    result['thread']['path']='/tmp/projected-rollout.jsonl'
                return result
        order=[]
        def verified(thread,path):
            order.append(('verified',thread,path))
        def submitting(thread):
            order.append(('submitting',thread))
        with mock.patch.object(app,'_Connection',ProjectedConnection), \
             mock.patch.object(app,'catalog',return_value=CATALOG):
            app.run_turn('/work',{},'', 'question',{}, image_paths=['/tmp/current.png'],
                history_items=[{'type':'message','role':'user','content':[]}],
                on_history_injected=verified,on_turn_submitting=submitting)
        methods=[name for name,_params in ProjectedConnection.last.calls]
        self.assertLess(methods.index('thread/inject_items'),methods.index('thread/read'))
        self.assertLess(methods.index('thread/read'),methods.index('turn/start'))
        self.assertEqual(order,[('verified','thread-1','/tmp/projected-rollout.jsonl'),
                                ('submitting','thread-1')])
        self.assertEqual(dict(ProjectedConnection.last.calls)['turn/start']['input'],[
            {'type':'text','text':'question'},
            {'type':'localImage','path':'/tmp/current.png'}])

    def test_投影历史校验失败时图片回合不派发(self):
        class ProjectedConnection(FakeConnection):
            def request(inner, method, params=None, on_notification=None, timeout=30, **kw):
                result = super().request(method, params, on_notification, timeout, **kw)
                if method == 'thread/read':
                    result['thread']['path']='/tmp/projected-rollout.jsonl'
                return result
        submitted=[]
        def reject(_thread,_path):
            raise RuntimeError('history mismatch')
        with mock.patch.object(app,'_Connection',ProjectedConnection), \
             mock.patch.object(app,'catalog',return_value=CATALOG):
            with self.assertRaisesRegex(RuntimeError,'history mismatch'):
                app.run_turn('/work',{},'', 'question',{}, image_paths=['/tmp/current.png'],
                    history_items=[{'type':'message','role':'user','content':[]}],
                    on_history_injected=reject,on_turn_submitting=submitted.append)
        methods=[name for name,_params in ProjectedConnection.last.calls]
        self.assertIn('thread/inject_items',methods)
        self.assertIn('thread/read',methods)
        self.assertNotIn('turn/start',methods)
        self.assertEqual(submitted,[])

    def test_投影历史游标不匹配时冷起并重建(self):
        class ProjectedConnection(FakeConnection):
            def request(inner, method, params=None, on_notification=None, timeout=30, **kw):
                result = super().request(method, params, on_notification, timeout, **kw)
                if method == "thread/read":
                    result["thread"]["path"] = "/tmp/projected-rollout.jsonl"
                return result

        kwargs = dict(session_id="shepherd:binding", history_items=[],
            resident_context="context-a", session_recap=False,
            on_working_context=lambda *_args:None)
        with mock.patch.object(app, "_Connection", ProjectedConnection), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            app.run_turn("/work", {}, "", "第一轮", {}, resident_through_seq=0, **kwargs)
            first=ProjectedConnection.last
            app.run_turn("/work", {}, "", "恢复轮", {}, resident_through_seq=7, **kwargs)

        self.assertEqual(len(ProjectedConnection.instances),2)
        self.assertEqual(first.closed,1)

    def test_配置警告在就绪后仍保留(self):
        class WarningConnection(FakeConnection):
            def initialize(self, on_notification=None, overall=None):
                super().initialize(on_notification, overall)
                on_notification("configWarning", {
                    "summary": "配置项已弃用", "details": "请改用新字段"})

        events = []
        with mock.patch.object(app, "_Connection", WarningConnection), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            app.run_turn("/work", {}, "", "做事", {"sandbox": "read-only"},
                         session_id="desk-1", on_environment=events.append)

        self.assertEqual(events[-1]["state"], "warning")
        self.assertIn("配置项已弃用", "\n".join(events[-1]["details"]))

    def test_skill_budget_warning_visible_before_and_after_ready(self):
        warning = "Skill descriptions were shortened to fit the 2% skills context budget. Codex can still see every skill."
        for phase in ("thread/start", "turn/start"):
            with self.subTest(phase=phase):
                app._drop_all_sessions()
                class BudgetConnection(FakeConnection):
                    def request(self, method, params=None, on_notification=None, timeout=30, **kw):
                        if method == phase and on_notification:
                            on_notification("warning", {"message": warning})
                        return super().request(method, params, on_notification, timeout, **kw)
                events = []
                with mock.patch.object(app, "_Connection", BudgetConnection), mock.patch.object(app, "catalog", return_value=CATALOG):
                    app.run_turn("/work", {}, "", "test", {}, binary="codex", label="Codex",
                                 session_id="budget", on_environment=events.append)
                self.assertEqual(events[-1]["state"], "warning")
                self.assertEqual(events[-1]["label"], "Codex · 技能描述已缩短")
                self.assertIn("技能仍全部可见", events[-1]["details"][0])
                self.assertIn(warning, events[-1]["details"][0])

    def test_恢复旧thread的模型差异按Desk配置忽略(self):
        class ModelResumeWarning(FakeConnection):
            def request(self, method, params=None, on_notification=None, timeout=30, **kw):
                if method == "thread/resume" and on_notification:
                    on_notification("warning", {"message":
                        "This session was recorded with model `gpt-5.5` but is resuming "
                        "with `gpt-5.6-sol`. Consider switching back to `gpt-5.5`."})
                return super().request(method, params, on_notification, timeout, **kw)

        events = []
        with mock.patch.object(app, "_Connection", ModelResumeWarning), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            app.run_turn("/work", {}, "thread-old", "继续", {"sandbox": "read-only"},
                         session_id="desk-1", on_environment=events.append)

        self.assertEqual(events[-1]["state"], "ready")
        self.assertEqual(events[-1]["details"], [])

    def test_MCP_cancelled不是启动故障(self):
        class CancelledConnection(FakeConnection):
            def __init__(self, cwd=None, env=None, binary="codex"):
                super().__init__(cwd, env)
                self._cancel_sent = False

            def read(self, timeout=30):
                if not self._cancel_sent:
                    self._cancel_sent = True
                    return {"method": "mcpServer/startupStatus/updated", "params": {
                        "name": "codex_apps", "status": "cancelled", "threadId": "thread-1"}}
                return super().read(timeout)

        events = []
        with mock.patch.object(app, "_Connection", CancelledConnection), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            app.run_turn("/work", {}, "", "做事", {"sandbox": "read-only"},
                         session_id="desk-1", on_environment=events.append)

        self.assertEqual(events[-1]["state"], "ready")
        self.assertEqual(events[-1]["details"], [])
        self.assertEqual(events[-1]["mcp"]["failed"], 0)

    def test_App_Control启动失败后丢弃连接以便下一轮重试(self):
        class FailedAppControl(FakeConnection):
            def initialize(self, on_notification=None, overall=None):
                super().initialize(on_notification, overall)
                on_notification("mcpServer/startupStatus/updated", {
                    "name": "desk_app_control", "status": "failed",
                    "error": "No such file or directory (os error 2)"})

        events = []
        with mock.patch.object(app, "_Connection", FailedAppControl), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            app.run_turn("/work", {}, "", "做事", {"sandbox": "read-only"},
                         session_id="desk-1", on_environment=events.append)

        self.assertEqual(FakeConnection.last.closed, 1)
        self.assertNotIn("desk-1", app.live_session_ids())
        self.assertEqual(events[-1]["state"], "warning")
        self.assertIn("No such file", "\n".join(events[-1]["details"]))

    def test_ready之后迟到的MCP_ready不重复弹就绪(self):
        class LateMcpReady(FakeConnection):
            def __init__(self, cwd=None, env=None, binary="codex"):
                super().__init__(cwd, env)
                self.n = 0

            def read(self, timeout=30):
                self.n += 1
                if self.n == 1:
                    return {"method": "mcpServer/startupStatus/updated", "params": {
                        "name": "codex_apps", "status": "ready", "threadId": "thread-1"}}
                return super().read(timeout)

        events = []
        with mock.patch.object(app, "_Connection", LateMcpReady), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            app.run_turn("/work", {}, "", "做事", {"sandbox": "read-only"},
                         session_id="desk-1", on_environment=events.append)

        ready_events = [x for x in events if x["state"] == "ready"]
        self.assertEqual(len(ready_events), 1)

    def test_会话设置变化后重建连接并resume一次(self):
        with mock.patch.object(app, "_Connection", FakeConnection), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            tid = app.run_turn("/work", {}, "", "第一轮", {"sandbox": "read-only"},
                               session_id="desk-1")
            app.run_turn("/work", {}, tid, "第二轮", {"sandbox": "workspace-write"},
                         session_id="desk-1")

        self.assertEqual(len(FakeConnection.instances), 2)
        self.assertEqual(FakeConnection.instances[0].closed, 1)
        methods = [method for method, _params in FakeConnection.instances[1].calls]
        self.assertEqual(methods.count("thread/resume"), 1)

    def test_常驻进程退出后下一轮自动重建(self):
        with mock.patch.object(app, "_Connection", FakeConnection), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            tid = app.run_turn("/work", {}, "", "第一轮", {"sandbox": "read-only"},
                               session_id="desk-1")
            FakeConnection.instances[0].proc.poll.return_value = 1
            app.run_turn("/work", {}, tid, "第二轮", {"sandbox": "read-only"},
                         session_id="desk-1")

        self.assertEqual(len(FakeConnection.instances), 2)
        methods = [method for method, _params in FakeConnection.instances[1].calls]
        self.assertEqual(methods.count("thread/resume"), 1)

    def test_常驻连接忽略上一轮迟到的完成事件(self):
        class LateCompletion(FakeConnection):
            def __init__(self, cwd=None, env=None, binary="codex"):
                super().__init__(cwd, env)
                self.turn_no = 0
                self.read_no = 0

            def request(self, method, params=None, on_notification=None, timeout=30, **kw):
                self.calls.append((method, params or {}))
                if method == "thread/start":
                    return {"thread": {"id": "thread-1"}}
                if method == "turn/start":
                    self.turn_no += 1
                    if self.turn_no == 2 and on_notification:
                        on_notification("turn/completed", {
                            "turn": {"id": "turn-1", "status": "completed"}})
                    return {"turn": {"id": f"turn-{self.turn_no}"}}
                if method == "thread/read":
                    return {"thread": {"id": "thread-1", "turns": [
                        {"id": f"turn-{self.turn_no}", "status": "completed"}]}}
                return {}

            def read(self, timeout=30):
                self.read_no += 1
                if self.turn_no == 1:
                    if self.read_no == 1:
                        return {"method": "item/completed", "params": {
                            "turnId": "turn-1", "item": {"type": "agentMessage",
                            "phase": "final_answer", "text": "完成"}}}
                    raise app.AppServerTimeout("漏发正式收尾")
                return {"method": "turn/completed", "params": {
                    "turn": {"id": "turn-2", "status": "completed"}}}

        seen = []
        with mock.patch.object(app, "_Connection", LateCompletion), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            tid = app.run_turn("/work", {}, "", "第一轮", {"sandbox": "read-only"},
                               session_id="desk-1")
            app.run_turn("/work", {}, tid, "第二轮", {"sandbox": "read-only"},
                         session_id="desk-1",
                         on_notification=lambda method, params: seen.append((method, params)))

        completed_ids = [((params.get("turn") or {}).get("id"))
                         for method, params in seen if method == "turn/completed"]
        self.assertEqual(completed_ids, ["turn-2"])


class CapacityRetry(unittest.TestCase):
    """Codex capacity is retryable only before model output or tools."""

    def setUp(self):
        app._drop_all_sessions()
        FakeConnection.instances = []

    def tearDown(self):
        app._drop_all_sessions()

    @staticmethod
    def _capacity(turn_id):
        return {"method":"turn/completed", "params":{"threadId":"thread-1", "turn":{
            "id":turn_id, "status":"failed", "error":{
                "message":"Selected model is at capacity. Please try a different model.",
                "codexErrorInfo":"serverOverloaded"}}}}

    def test_only_structured_server_overloaded_is_eligible(self):
        self.assertTrue(app._server_overloaded_error({"codexErrorInfo":"serverOverloaded"}))
        self.assertTrue(app._server_overloaded_error(
            {"codexErrorInfo":{"type":"serverOverloaded"}}))
        self.assertFalse(app._server_overloaded_error(
            {"message":"Selected model is at capacity. Please try again."}))
        self.assertFalse(app._server_overloaded_error(
            {"codexErrorInfo":"rateLimitExceeded"}))
        self.assertFalse(app._capacity_semantic_event("item/completed", {"item":{
            "type":"userMessage", "content":[{"type":"text", "text":"执行"}]}}))
        self.assertFalse(app._capacity_semantic_event("rawResponseItem/completed", {"item":{
            "type":"message", "role":"developer", "content":[{"type":"input_text",
            "text":"historical instructions"}]}}))
        self.assertTrue(app._capacity_semantic_event("item/completed", {"item":{
            "type":"agentMessage", "text":"开始"}}))

    def test_workspace_routing_classifier_is_exact_and_remote_only(self):
        self.assertTrue(app._workspace_routing_error({
            "message":"workspace routing discovery failed", "codexErrorInfo":"other"}))
        self.assertTrue(app._workspace_routing_error({
            "message":"Workspace Routing Discovery Failed.", "codex_error_info":"other"}))
        self.assertFalse(app._workspace_routing_error({
            "message":"workspace routing discovery failed"}))
        self.assertFalse(app._workspace_routing_error({
            "message":"workspace routing discovery failed",
            "codexErrorInfo":{"type":"other"}}))
        self.assertFalse(app._workspace_routing_error({
            "message":"workspace routing discovery failed", "codexErrorInfo":"auth"}))
        self.assertFalse(app._workspace_routing_error({
            "message":"workspace root discovery failed", "codexErrorInfo":"other"}))
        self.assertFalse(app._workspace_routing_error({
            "message":"workspace routing discovery failed for /work/repo"}))

    def test_workspace_routing_diagnostic_redacts_credentials(self):
        diagnostic = app._retry_error_diagnostic({
            "message":"workspace routing discovery failed token=secret-value",
            "codexErrorInfo":{"type":"other", "access_token":"secret-value",
                              "refresh_token":123456},
            "additionalDetails":"bearer abc.def.ghi"})
        self.assertNotIn("secret-value", str(diagnostic))
        self.assertNotIn("abc.def.ghi", str(diagnostic))
        self.assertEqual(diagnostic["codexErrorInfo"]["type"], "other")
        self.assertEqual(diagnostic["codexErrorInfo"]["refresh_token"], "[redacted]")

    def test_same_error_text_without_typed_reason_is_not_retried(self):
        class TextOnly(FakeConnection):
            def read(self, timeout=30):
                return {"method":"turn/completed", "params":{"threadId":"thread-1",
                    "turn":{"id":"turn-1", "status":"failed", "error":{
                        "message":"Selected model is at capacity. Please try again."}}}}
        seen = []
        with mock.patch.object(app, "_Connection", TextOnly), \
             mock.patch.object(app, "catalog", return_value=CATALOG), \
             mock.patch.object(app, "CAPACITY_RETRY_DELAYS", (0,)):
            with self.assertRaisesRegex(app.AppServerError, "at capacity"):
                app.run_turn("/work", {}, "", "执行", {},
                             on_notification=lambda m,p: seen.append((m,p)))
        self.assertEqual(len([1 for m,_p in TextOnly.last.calls if m == "turn/start"]), 1)
        self.assertFalse([1 for m,_p in seen if m == "turn/retry"])

    def test_capacity_retries_same_thread_with_empty_input_and_no_model_switch(self):
        capacity = self._capacity
        class Engine(FakeConnection):
            def __init__(self, cwd=None, env=None, binary="codex", opts=None):
                super().__init__(cwd, env, binary, opts)
                self.turn_no = 0; self.user_echo = False
            def request(self, method, params=None, on_notification=None, timeout=30, **kw):
                if method == "turn/start":
                    self.calls.append((method, params or {})); self.turn_no += 1
                    self.user_echo = True
                    return {"turn":{"id":f"turn-{self.turn_no}"}}
                return super().request(method, params, on_notification, timeout, **kw)
            def read(self, timeout=30):
                if self.user_echo:
                    self.user_echo = False
                    return {"method":"item/completed", "params":{"threadId":"thread-1",
                        "turnId":f"turn-{self.turn_no}", "item":{"id":f"user-{self.turn_no}",
                        "type":"userMessage", "content":[]}}}
                if self.turn_no < 3:
                    return capacity(f"turn-{self.turn_no}")
                return {"method":"turn/completed", "params":{"threadId":"thread-1",
                    "turn":{"id":"turn-3", "status":"completed"}}}

        seen, submitted, started = [], [], []
        with mock.patch.object(app, "_Connection", Engine), \
             mock.patch.object(app, "catalog", return_value=CATALOG), \
             mock.patch.object(app, "CAPACITY_RETRY_DELAYS", (0, 0)):
            got = app.run_turn("/work", {}, "", "原问题", {"model":"gpt-5.6-sol"},
                image_paths=["/tmp/input.png"], session_id="desk-capacity",
                on_notification=lambda m,p: seen.append((m,p)),
                on_turn_submitting=lambda tid: submitted.append(tid) or True,
                on_turn_started=lambda tid,turn: started.append((tid,turn)))

        self.assertEqual(got, "thread-1")
        self.assertEqual(len(Engine.instances), 1)
        calls = [p for m,p in Engine.last.calls if m == "turn/start"]
        self.assertEqual([x["input"] for x in calls], [
            [{"type":"text", "text":"原问题"}, {"type":"localImage", "path":"/tmp/input.png"}],
            [{"type":"text", "text":""}], [{"type":"text", "text":""}]])
        self.assertEqual({x["collaborationMode"]["settings"]["model"] for x in calls},
                         {"gpt-5.6-sol"})
        self.assertEqual(submitted, ["thread-1"], "durable dispatch fence must cross once")
        self.assertEqual([turn for _tid,turn in started], ["turn-1", "turn-2", "turn-3"])
        self.assertFalse([p for m,p in seen if m == "turn/completed"
                          and p["turn"]["status"] == "failed"])
        retries = [p for m,p in seen if m == "turn/retry"]
        self.assertEqual([(p["active"], p["attempt"]) for p in retries],
                         [(True, 1), (True, 2), (False, 2)])
        self.assertEqual([p["turn"]["id"] for m,p in seen if m == "turn/completed"],
                         ["turn-3"])

    def test_workspace_routing_retries_once_on_same_thread_without_resending_input(self):
        class Routing(FakeConnection):
            def __init__(self, cwd=None, env=None, binary="codex", opts=None):
                super().__init__(cwd, env, binary, opts)
                self.turn_no = 0; self.raw_history_sent = False
            def request(self, method, params=None, on_notification=None, timeout=30, **kw):
                if method == "turn/start":
                    self.calls.append((method, params or {})); self.turn_no += 1
                    return {"turn":{"id":f"turn-{self.turn_no}"}}
                return super().request(method, params, on_notification, timeout, **kw)
            def read(self, timeout=30):
                if self.turn_no == 1 and not self.raw_history_sent:
                    self.raw_history_sent = True
                    return {"method":"rawResponseItem/completed", "params":{
                        "threadId":"thread-1", "turnId":"turn-1", "item":{
                            "type":"message", "role":"developer", "content":[{
                                "type":"input_text", "text":"historical instructions"}]}}}
                if self.turn_no == 1:
                    return {"method":"turn/completed", "params":{"threadId":"thread-1",
                        "turn":{"id":"turn-1", "status":"failed", "error":{
                            "message":"workspace routing discovery failed",
                            "codexErrorInfo":"other", "additionalDetails":"route lookup timed out"}}}}
                return {"method":"turn/completed", "params":{"threadId":"thread-1",
                    "turn":{"id":"turn-2", "status":"completed"}}}

        seen, submitted = [], []
        with mock.patch.object(app, "_Connection", Routing), \
             mock.patch.object(app, "catalog", return_value=CATALOG), \
             mock.patch.object(app, "WORKSPACE_ROUTING_RETRY_DELAYS", (0,)):
            got = app.run_turn("/work", {}, "", "原问题", {},
                image_paths=["/tmp/input.png"], session_id="desk-routing",
                on_notification=lambda m,p: seen.append((m,p)),
                on_turn_submitting=lambda tid: submitted.append(tid) or True)

        self.assertEqual(got, "thread-1")
        calls = [p for m,p in Routing.last.calls if m == "turn/start"]
        self.assertEqual([x["input"] for x in calls], [
            [{"type":"text", "text":"原问题"},
             {"type":"localImage", "path":"/tmp/input.png"}],
            [{"type":"text", "text":""}]])
        self.assertEqual(submitted, ["thread-1"])
        self.assertEqual(len(Routing.instances), 1)
        retries = [p for m,p in seen if m == "turn/retry"]
        self.assertEqual([(p["active"], p["attempt"], p["max"])
                          for p in retries], [(True, 1, 1), (False, 1, 1)])
        self.assertEqual(retries[0]["signal"], "turn.workspaceRoutingDiscovery")
        self.assertEqual(retries[0]["native_error"], {
            "message":"workspace routing discovery failed",
            "codexErrorInfo":"other", "additionalDetails":"route lookup timed out"})
        self.assertFalse([p for m,p in seen if m == "turn/completed"
                          and p["turn"]["status"] == "failed"])

    def test_workspace_routing_after_tool_progress_is_not_retried(self):
        class UnsafeRouting(FakeConnection):
            def __init__(self, cwd=None, env=None, binary="codex", opts=None):
                super().__init__(cwd, env, binary, opts); self.n = 0
            def read(self, timeout=30):
                self.n += 1
                if self.n == 1:
                    return {"method":"item/started", "params":{"threadId":"thread-1",
                        "turnId":"turn-1", "item":{"id":"tool-1",
                        "type":"commandExecution", "status":"inProgress"}}}
                return {"method":"turn/completed", "params":{"threadId":"thread-1",
                    "turn":{"id":"turn-1", "status":"failed", "error":{
                        "message":"workspace routing discovery failed",
                        "codexErrorInfo":"other"}}}}
        seen = []
        with mock.patch.object(app, "_Connection", UnsafeRouting), \
             mock.patch.object(app, "catalog", return_value=CATALOG), \
             mock.patch.object(app, "WORKSPACE_ROUTING_RETRY_DELAYS", (0,)):
            with self.assertRaisesRegex(app.AppServerError, "workspace routing"):
                app.run_turn("/work", {}, "", "执行", {},
                    on_notification=lambda m,p: seen.append((m,p)))
        self.assertEqual(len([1 for m,_p in UnsafeRouting.last.calls if m == "turn/start"]), 1)
        self.assertFalse([p for m,p in seen if m == "turn/retry"])
        failed = [p for m,p in seen if m == "turn/completed"]
        self.assertEqual(failed[0]["turn"]["error"]["codexErrorInfo"], "other")

    def test_workspace_routing_after_approval_request_is_not_retried(self):
        class ApprovalRouting(FakeConnection):
            def __init__(self, cwd=None, env=None, binary="codex", opts=None):
                super().__init__(cwd, env, binary, opts); self.n = 0
            def handle_server_request(self, _msg):
                return False
            def read(self, timeout=30):
                self.n += 1
                if self.n == 1:
                    return {"id":9, "method":"item/commandExecution/requestApproval",
                            "params":{"threadId":"thread-1", "turnId":"turn-1"}}
                return {"method":"turn/completed", "params":{"threadId":"thread-1",
                    "turn":{"id":"turn-1", "status":"failed", "error":{
                        "message":"workspace routing discovery failed",
                        "codexErrorInfo":"other"}}}}
        seen = []
        with mock.patch.object(app, "_Connection", ApprovalRouting), \
             mock.patch.object(app, "catalog", return_value=CATALOG), \
             mock.patch.object(app, "WORKSPACE_ROUTING_RETRY_DELAYS", (0,)):
            with self.assertRaisesRegex(app.AppServerError, "workspace routing"):
                app.run_turn("/work", {}, "", "执行", {},
                    on_notification=lambda m,p: seen.append((m,p)))
        self.assertEqual(len([1 for m,_p in ApprovalRouting.last.calls if m == "turn/start"]), 1)
        self.assertFalse([p for m,p in seen if m == "turn/retry"])

    def test_workspace_routing_exhaustion_publishes_original_terminal_once(self):
        class RoutingDown(FakeConnection):
            def __init__(self, cwd=None, env=None, binary="codex", opts=None):
                super().__init__(cwd, env, binary, opts); self.turn_no = 0
            def request(self, method, params=None, on_notification=None, timeout=30, **kw):
                if method == "turn/start":
                    self.calls.append((method, params or {})); self.turn_no += 1
                    return {"turn":{"id":f"turn-{self.turn_no}"}}
                return super().request(method, params, on_notification, timeout, **kw)
            def read(self, timeout=30):
                return {"method":"turn/completed", "params":{"threadId":"thread-1",
                    "turn":{"id":f"turn-{self.turn_no}", "status":"failed", "error":{
                        "message":"workspace routing discovery failed",
                        "codexErrorInfo":"other", "additionalDetails":"backend unavailable"}}}}
        seen = []
        with mock.patch.object(app, "_Connection", RoutingDown), \
             mock.patch.object(app, "catalog", return_value=CATALOG), \
             mock.patch.object(app, "WORKSPACE_ROUTING_RETRY_DELAYS", (0,)):
            with self.assertRaisesRegex(app.AppServerError, "workspace routing"):
                app.run_turn("/work", {}, "", "原问题", {}, session_id="desk-routing-down",
                    on_notification=lambda m,p: seen.append((m,p)))
        self.assertEqual(len([1 for m,_p in RoutingDown.last.calls if m == "turn/start"]), 2)
        failed = [p for m,p in seen if m == "turn/completed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["turn"]["id"], "turn-2")
        self.assertEqual(failed[0]["turn"]["error"]["additionalDetails"],
                         "backend unavailable")
        self.assertNotIn("desk-routing-down", app.live_session_ids())

    def test_wall_clock_rollback_cannot_extend_total_retry_budget(self):
        mono=[100.0];wall=[100.0];calls=[]
        def attempt(*_args,**kwargs):
            calls.append(kwargs['hard_timeout'])
            mono[0]=111.0;wall[0]=50.0
            raise app._CapacityUnavailable('thread-1',{
                'message':'full','codexErrorInfo':'serverOverloaded'})
        with mock.patch.object(app,'_run_turn_once',side_effect=attempt), \
             mock.patch.object(app,'CAPACITY_RETRY_DELAYS',(0,)), \
             mock.patch.object(app.time,'monotonic',side_effect=lambda:mono[0]), \
             mock.patch.object(app.time,'time',side_effect=lambda:wall[0]):
            with self.assertRaisesRegex(app.AppServerError,'full'):
                app.run_turn('/work',{},'','执行',{},hard_timeout=10)
        self.assertEqual(calls,[10])

    def test_capacity_exhaustion_publishes_one_final_failure(self):
        capacity = self._capacity
        class Full(FakeConnection):
            def __init__(self, cwd=None, env=None, binary="codex", opts=None):
                super().__init__(cwd, env, binary, opts); self.turn_no = 0
            def request(self, method, params=None, on_notification=None, timeout=30, **kw):
                if method == "turn/start":
                    self.calls.append((method, params or {})); self.turn_no += 1
                    return {"turn":{"id":f"turn-{self.turn_no}"}}
                return super().request(method, params, on_notification, timeout, **kw)
            def read(self, timeout=30):
                return capacity(f"turn-{self.turn_no}")

        seen = []
        with mock.patch.object(app, "_Connection", Full), \
             mock.patch.object(app, "catalog", return_value=CATALOG), \
             mock.patch.object(app, "CAPACITY_RETRY_DELAYS", (0, 0)):
            with self.assertRaisesRegex(app.AppServerError, "at capacity"):
                app.run_turn("/work", {}, "", "原问题", {}, session_id="desk-full",
                    on_notification=lambda m,p: seen.append((m,p)))
        failed = [p for m,p in seen if m == "turn/completed"
                  and p["turn"]["status"] == "failed"]
        self.assertEqual([p["turn"]["id"] for p in failed], ["turn-3"])
        self.assertNotIn("desk-full", app.live_session_ids())

    def test_tool_or_output_before_capacity_fails_closed_without_retry(self):
        capacity = self._capacity
        class Unsafe(FakeConnection):
            def __init__(self, cwd=None, env=None, binary="codex", opts=None):
                super().__init__(cwd, env, binary, opts); self.n = 0
            def read(self, timeout=30):
                self.n += 1
                if self.n == 1:
                    return {"method":"item/started", "params":{"threadId":"thread-1",
                        "turnId":"turn-1", "item":{"id":"tool-1", "type":"commandExecution",
                        "status":"inProgress"}}}
                return capacity("turn-1")

        seen = []
        with mock.patch.object(app, "_Connection", Unsafe), \
             mock.patch.object(app, "catalog", return_value=CATALOG), \
             mock.patch.object(app, "CAPACITY_RETRY_DELAYS", (0,)):
            with self.assertRaisesRegex(app.AppServerError, "at capacity"):
                app.run_turn("/work", {}, "", "执行", {}, session_id="desk-unsafe",
                    on_notification=lambda m,p: seen.append((m,p)))
        self.assertEqual(len([1 for m,_p in seen if m == "turn/retry"]), 0)
        self.assertEqual(len([1 for m,_p in Unsafe.last.calls if m == "turn/start"]), 1)

    def test_user_cancel_during_backoff_stops_without_starting_another_turn(self):
        capacity = self._capacity
        state = {"failed":False, "checks":0}
        class Cancelled(FakeConnection):
            def read(self, timeout=30):
                state["failed"] = True
                return capacity("turn-1")
        def cancelled():
            if not state["failed"]:
                return False
            state["checks"] += 1
            return state["checks"] >= 2
        seen = []
        with mock.patch.object(app, "_Connection", Cancelled), \
             mock.patch.object(app, "catalog", return_value=CATALOG), \
             mock.patch.object(app, "CAPACITY_RETRY_DELAYS", (10,)):
            with self.assertRaisesRegex(app.AppServerError, "at capacity"):
                app.run_turn("/work", {}, "", "执行", {}, session_id="desk-cancel",
                    cancelled=cancelled, on_notification=lambda m,p: seen.append((m,p)))
        self.assertEqual(len([1 for m,_p in Cancelled.last.calls if m == "turn/start"]), 1)
        retry = [p for m,p in seen if m == "turn/retry"]
        self.assertEqual([(p["active"], p.get("reason")) for p in retry],
                         [(True, None), (False, "cancelled")])
        self.assertNotIn("desk-cancel", app.live_session_ids())

    def test_retry_projection_failure_releases_held_resident_writer(self):
        class Full(FakeConnection):
            def read(self, timeout=30):
                return CapacityRetry._capacity("turn-1")
        def broken(method, _params):
            if method == "turn/retry":
                raise RuntimeError("projection failed")
        with mock.patch.object(app, "_Connection", Full), \
             mock.patch.object(app, "catalog", return_value=CATALOG), \
             mock.patch.object(app, "CAPACITY_RETRY_DELAYS", (0,)):
            with self.assertRaisesRegex(RuntimeError, "projection failed"):
                app.run_turn("/work", {}, "", "执行", {}, session_id="desk-projection",
                             on_notification=broken)
        self.assertNotIn("desk-projection", app.live_session_ids())
        self.assertEqual(Full.last.closed, 1)

class RequestDeadline(unittest.TestCase):
    """一条请求必须有总时限。

    timeout 只是「两帧之间的静默上限」,期间任何通知都会让它重新计时;缺了总时限,一个
    永远不被应答的请求可以无限等下去。2026-08-10 实见:app-server 挂在一条到模型 API 的
    连接上八分半没有响应,Desk 这侧既不报错也不收尾,回合的 _live 一直不出栈,侧栏持续闪烁,
    日志里一个字都没有。
    """

    def _conn(self, reader):
        conn = object.__new__(app._Connection)
        conn._next_id = 1
        conn._send = lambda o: None
        conn.read = reader
        return conn

    def test_通知不断也会到总时限(self):
        conn = self._conn(lambda timeout=30: {"method": "noise/updated", "params": {}})
        with self.assertRaises(app.AppServerError) as cm:
            conn.request("thread/resume", {}, overall=0.05)
        self.assertIn("thread/resume", str(cm.exception))
        self.assertIn("未被应答", str(cm.exception))

    def test_正常应答不受影响(self):
        conn = self._conn(lambda timeout=30: {"id": 1, "result": {"ok": True}})
        self.assertEqual(conn.request("initialize", {}), {"ok": True})

    def test_起手阶段也在动态绝对上限之内(self):
        """静默上限不是总时限；启动步骤受独立 hard_timeout 约束。"""
        with mock.patch.object(app, "_Connection", FakeConnection), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            app.run_turn("/work", {}, "", "做事", {"sandbox": "read-only"},
                         timeout=900, hard_timeout=7200)
        kw = FakeConnection.last.kwargs
        for method in ("thread/start", "turn/start"):
            self.assertIn("overall", kw[method], method)
            self.assertTrue(900 < kw[method]["overall"] <= 7200, (method, kw[method]))


class QuietTurn(unittest.TestCase):
    """一条安静的长命令(sleep、大范围检索、等网络)跑着时,app-server 一个通知都不发。

    此前读循环把 30 秒无声当故障抛出去,回合在半路断掉:界面上是「还没做完就已处理」,
    落盘的助手消息以工具块收尾、ms 为空(turn/completed 从未到达)。
    2026-08-10 实测:`sleep 120` 的回合在 177 秒处报「app-server 30 秒内没有响应」。
    """

    def _run(self, conn_cls):
        with mock.patch.object(app, "_Connection", conn_cls), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            return app.run_turn("/work", {}, "", "做事", {"sandbox": "read-only"})

    def test_进程还活着就接着等(self):
        class Quiet(FakeConnection):
            def __init__(self, cwd=None, env=None, binary="codex"):
                super().__init__(cwd, env)
                self.proc.poll.return_value = None      # 进程活着
                self.n = 0

            def read(self, timeout=30):
                self.n += 1
                if self.n <= 3:                          # 连着三次读空
                    raise app.AppServerTimeout("app-server 30 秒内没有响应")
                return {"method": "turn/completed", "params": {
                    "turn": {"id": "turn-1", "status": "completed"}}}

        self.assertEqual(self._run(Quiet), "thread-1")
        self.assertEqual(Quiet.last.n, 4)                # 前三次静默没有把回合打断

    def test_静默窗口只告警且恢复后清除(self):
        clock = [100.0]
        observed = []
        class SoftStall(FakeConnection):
            def __init__(self, cwd=None, env=None, binary="codex"):
                super().__init__(cwd, env)
                self.proc.poll.return_value = None
                self.n = 0
            def handle_server_request(self, _msg):
                return True
            def read(self, timeout=30):
                self.n += 1
                if self.n == 1:
                    clock[0] += 1.1
                    raise app.AppServerTimeout("still running")
                if self.n == 2:
                    return {"method":"thread/tokenUsage/updated", "params":{
                        "threadId":"thread-1", "turnId":"turn-1",
                        "tokenUsage":{"total":{"totalTokens":7}}}}
                if self.n == 3:
                    return {"method":"item/agentMessage/delta", "params":{
                        "threadId":"thread-1", "turnId":"turn-1",
                        "itemId":"m1", "delta":""}}
                if self.n == 4:
                    return {"method":"item/agentMessage/delta", "params":{
                        "threadId":"thread-1", "itemId":"m1", "delta":"identity-less"}}
                if self.n == 5:
                    return {"method":"item/agentMessage/delta", "params":{
                        "turnId":"turn-1", "itemId":"m1", "delta":"missing-thread"}}
                if self.n == 6:
                    return {"method":"item/agentMessage/delta", "params":{
                        "threadId":"other", "turnId":"other-turn",
                        "itemId":"m2", "delta":"foreign"}}
                if self.n == 7:
                    return {"id":9, "method":"item/commandExecution/requestApproval",
                            "params":{"threadId":"thread-1", "turnId":"turn-1"}}
                if self.n == 8:
                    return {"method":"item/agentMessage/delta", "params":{
                        "threadId":"thread-1", "turnId":"turn-1",
                        "itemId":"m1", "delta":"answer"}}
                return {"method":"turn/completed", "params":{
                    "threadId":"thread-1",
                    "turn":{"id":"turn-1", "status":"completed"}}}
        base_thread = threading.Thread
        class InlineNotify(base_thread):
            # Stall observations are delivered on a helper thread. Run that delivery
            # inline so the recorded read count is the one at emission, not whenever
            # a loaded runner happens to schedule the helper.
            def start(self):
                if getattr(self._target, "__name__", "") == "notify":
                    self.run()
                else:
                    super().start()
        with mock.patch.object(app, "_Connection", SoftStall), \
             mock.patch.object(app, "catalog", return_value=CATALOG), \
             mock.patch.object(app.threading, "Thread", InlineNotify), \
             mock.patch.object(app.time, "time", side_effect=lambda:clock[0]), \
             mock.patch.object(app.time, "monotonic", side_effect=lambda:clock[0]):
            result = app.run_turn("/work", {}, "", "做事", {"sandbox":"read-only"},
                                  timeout=1, hard_timeout=10,
                                  on_stall=lambda row:observed.append((row,SoftStall.last.n)))
        self.assertEqual(result,"thread-1")
        deadline=time.monotonic()+1
        while len(observed)<2 and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual([(row['active'],n) for row,n in observed],[(True,1),(False,8)])
        self.assertEqual([row['sequence'] for row,_n in observed],[1,2])

    def test_阻塞的停滞观察不延迟硬上限(self):
        entered=threading.Event();release=threading.Event()
        class Forever(FakeConnection):
            def read(self, timeout=30):
                raise app.AppServerTimeout("still running")
        started=time.monotonic()
        try:
            with mock.patch.object(app,"_Connection",Forever), \
                 mock.patch.object(app,"catalog",return_value=CATALOG):
                with self.assertRaisesRegex(app.AppServerError,"最长时限"):
                    app.run_turn("/work",{},"","做事",{},timeout=.01,hard_timeout=.2,
                        on_stall=lambda _row:(entered.set(),release.wait(2)))
            self.assertTrue(entered.wait(1))
            self.assertLess(time.monotonic()-started,1)
        finally:
            release.set()

    def test_进程已死则照常报错(self):
        class Dead(FakeConnection):
            def __init__(self, cwd=None, env=None, binary="codex"):
                super().__init__(cwd, env)
                self.proc.poll.return_value = 1          # 已退出

            def read(self, timeout=30):
                raise app.AppServerTimeout("app-server 30 秒内没有响应")

        with self.assertRaises(app.AppServerError):
            self._run(Dead)

    def test_当前回合的迟到自动压缩获得一次有限储备(self):
        for binary in ('codex',):
            with self.subTest(binary=binary):
                clock=[100.0]
                class LateCompaction(FakeConnection):
                    def __init__(self, cwd=None, env=None, binary='codex', opts=None):
                        super().__init__(cwd,env,binary,opts);self.n=0
                    def read(self,timeout=30):
                        self.n+=1
                        if self.n==1:
                            clock[0]=109.5
                            return {'method':'item/started','params':{
                                'threadId':'thread-1','turnId':'turn-1','item':{
                                    'id':'compact-1','type':'contextCompaction','status':'inProgress'}}}
                        if self.n==2:
                            clock[0]=110.5
                            raise app.AppServerTimeout('crossed old deadline')
                        return {'method':'turn/completed','params':{
                            'threadId':'thread-1','turn':{'id':'turn-1','status':'completed'}}}
                with mock.patch.object(app,'_Connection',LateCompaction), \
                     mock.patch.object(app,'catalog',return_value=CATALOG), \
                     mock.patch.object(app.time,'time',side_effect=lambda:clock[0]), \
                     mock.patch.object(app.time,'monotonic',side_effect=lambda:clock[0]):
                    self.assertEqual(app.run_turn('/work',{},'', '做事',{},binary=binary,
                                                  timeout=1,hard_timeout=10),'thread-1')

    def test_外来或重复压缩事件不能续期(self):
        cases=(('foreign','other-turn',109.5,110.5),
               ('missing',None,109.5,110.5),
               ('duplicate','turn-1',107.0,116.0))
        for name,second_turn,first_at,second_at in cases:
            with self.subTest(case=name):
                clock=[100.0]
                class InvalidExtension(FakeConnection):
                    def __init__(self,cwd=None,env=None,binary='codex',opts=None):
                        super().__init__(cwd,env,binary,opts);self.n=0
                    def read(self,timeout=30):
                        self.n+=1
                        if self.n==1:
                            clock[0]=first_at
                            params={'item':{'id':'compact-1','type':'contextCompaction',
                                            'status':'inProgress'}}
                            if second_turn is not None:
                                params.update(threadId='thread-1',turnId=second_turn)
                            return {'method':'item/started','params':params}
                        if name=='duplicate' and self.n==2:
                            clock[0]=second_at
                            return {'method':'item/started','params':{
                                'threadId':'thread-1','turnId':'turn-1','item':{
                                    'id':'compact-2','type':'contextCompaction','status':'inProgress'}}}
                        clock[0]=110.5 if name in ('foreign','missing') else 118.0
                        raise app.AppServerTimeout('deadline')
                with mock.patch.object(app,'_Connection',InvalidExtension), \
                     mock.patch.object(app,'catalog',return_value=CATALOG), \
                     mock.patch.object(app.time,'time',side_effect=lambda:clock[0]), \
                     mock.patch.object(app.time,'monotonic',side_effect=lambda:clock[0]):
                    with self.assertRaisesRegex(app.AppServerError,'最长时限'):
                        app.run_turn('/work',{},'', '做事',{},timeout=1,hard_timeout=10)

    def test_墙钟回拨不能延长整回合上限(self):
        wall=[100.0];mono=[100.0]
        class Rollback(FakeConnection):
            def read(self,timeout=30):
                wall[0]=50.0;mono[0]=111.0
                raise app.AppServerTimeout('deadline')
        with mock.patch.object(app,'_Connection',Rollback), \
             mock.patch.object(app,'catalog',return_value=CATALOG), \
             mock.patch.object(app.time,'time',side_effect=lambda:wall[0]), \
             mock.patch.object(app.time,'monotonic',side_effect=lambda:mono[0]):
            with self.assertRaisesRegex(app.AppServerError,'最长时限 10 秒'):
                app.run_turn('/work',{},'', '做事',{},timeout=1,hard_timeout=10)

    def test_整回合上限仍然生效(self):
        class Forever(FakeConnection):
            def __init__(self, cwd=None, env=None, binary="codex"):
                super().__init__(cwd, env)
                self.proc.poll.return_value = None

            def read(self, timeout=30):
                raise app.AppServerTimeout("x")

        with mock.patch.object(app, "_Connection", Forever), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            with self.assertRaises(app.AppServerError) as cm:
                app.run_turn("/work", {}, "", "做事", {"sandbox": "read-only"}, timeout=0)
        self.assertIn("最长时限", str(cm.exception))

    def test_静默是_AppServerError_的一档(self):
        # request() 那侧等的是一条响应,静默在那里仍然是故障;子类关系保证旧的捕获不失效
        self.assertTrue(issubclass(app.AppServerTimeout, app.AppServerError))

    def test_最终正文后漏发_turn_completed_也能收尾(self):
        class MissingTurnCompleted(FakeConnection):
            def __init__(self, cwd=None, env=None, binary="codex"):
                super().__init__(cwd, env)
                self.proc.poll.return_value = None
                self.n = 0

            def read(self, timeout=30):
                self.n += 1
                if self.n == 1:
                    return {"method": "item/completed", "params": {
                        "threadId": "thread-1", "turnId": "turn-1", "item": {
                            "type": "agentMessage", "phase": "final_answer", "text": "完成。"}}}
                raise app.AppServerTimeout("app-server 没有继续发事件")

        seen = []
        with mock.patch.object(app, "_Connection", MissingTurnCompleted), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            tid = app.run_turn("/work", {}, "", "做事", {"sandbox": "read-only"},
                               on_notification=lambda method, params: seen.append(method))
        self.assertEqual(tid, "thread-1")
        self.assertEqual(seen, ["item/completed", "turn/completed"])

    def test_node_repl显式超时覆盖完整工具生命周期(self):
        class HungNodeRepl(FakeConnection):
            def __init__(self, cwd=None, env=None, binary="codex"):
                super().__init__(cwd, env, binary)
                self.n = 0
                self.interrupts = []

            def request_nowait(self, method, params=None):
                self.interrupts.append((method, params or {}))

            def read(self, timeout=30):
                self.n += 1
                if self.n == 1:
                    return {"method": "item/started", "params": {
                        "threadId": "thread-1", "turnId": "turn-1", "item": {
                            "id": "mcp-1", "type": "mcpToolCall", "status": "inProgress",
                            "server": "node_repl", "tool": "js",
                            "arguments": {"code": "nodeRepl.write(true)", "timeout_ms": 100}}}}
                raise app.AppServerTimeout("仍在插件握手")

        started = time.monotonic()
        with mock.patch.object(app, "_Connection", HungNodeRepl), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            with self.assertRaises(app.AppServerError) as cm:
                app.run_turn("/work", {}, "", "探测", {"sandbox": "read-only"})
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn("node_repl_timeout", str(cm.exception))
        self.assertEqual(HungNodeRepl.last.interrupts[0][0], "turn/interrupt")
        self.assertEqual(HungNodeRepl.last.closed, 1)

    def test_node_repl默认外层超时不超过十秒(self):
        deadline = app._node_repl_deadline({
            "type": "mcpToolCall", "server": "node-repl", "tool": "js", "arguments": {}},
            now=100.0)
        self.assertEqual(deadline, 110.0)
        self.assertIsNone(app._node_repl_deadline({
            "type": "mcpToolCall", "server": "files", "tool": "read"}, now=100.0))

    def test_Codex派出子代理后的临时idle不算回合完成(self):
        """spawn_agent 调用会先完成,但它派出的子代理仍在运行。

        Codex 此时可能把主 thread 置 idle；这只是父线程暂时没有动作,真正的协议终点仍是
        turn/completed。若按 idle 收尾,Desk 会在子代理运行期间错误显示「已完成」。
        """
        class SpawnedAgent(FakeConnection):
            FRAMES = [
                ("item/started", {"item": {
                    "id": "spawn-1", "type": "collabToolCall", "status": "inProgress"}}),
                ("item/completed", {"item": {
                    "id": "spawn-1", "type": "collabToolCall", "status": "completed"}}),
                ("thread/status/changed", {"status": {"type": "idle"}}),
                ("item/completed", {"item": {
                    "id": "note-1", "type": "agentMessage", "phase": "commentary",
                    "text": "等待子代理返回。"}}),
                ("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}}),
            ]

            def __init__(self, cwd=None, env=None, binary="codex"):
                super().__init__(cwd, env, binary)
                self.i = 0

            def read(self, timeout=30):
                if self.i >= len(self.FRAMES):
                    raise AssertionError("Codex 已读完测试帧却仍未收尾")
                method, params = self.FRAMES[self.i]
                self.i += 1
                params = dict(params, threadId="thread-1", turnId="turn-1")
                return {"method": method, "params": params}

        with mock.patch.object(app, "_Connection", SpawnedAgent), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            app.run_turn("/work", {}, "", "调研", {"sandbox": "read-only"}, binary="codex")

        self.assertEqual(SpawnedAgent.last.i, len(SpawnedAgent.FRAMES))


class TurnCompletion(unittest.TestCase):
    """codex 家族的收尾判据:优先认引擎自己的 turn/completed,idle 只是兜底。

    2026-09-09 实测:纯问答、单工具、三步多工具三种回合的收尾帧完全一致,
    thread/status/changed(idle) 之后同一毫秒就是 turn/completed。这条协议终点带引擎自己
    算的 durationMs,比 Desk 按 idle 合成的那份准,所以窗口只该在它缺席时才轮到。
    """

    def setUp(self):
        app._drop_all_sessions()

    def tearDown(self):
        app._drop_all_sessions()

    def _run(self, frames, binary="codex"):
        class Engine(FakeConnection):
            def __init__(self, cwd=None, env=None, binary="codex", opts=None):
                super().__init__(cwd, env, binary, opts)
                self.i = 0

            def read(self, timeout=30):
                if self.i >= len(frames):
                    raise app.AppServerTimeout("引擎静默")
                method, params = frames[self.i]
                self.i += 1
                return {"method": method, "params": dict(
                    params, threadId="thread-1", turnId="turn-1")}

        seen = []
        with mock.patch.object(app, "_Connection", Engine), \
             mock.patch.object(app, "catalog", return_value=CATALOG):
            # 收不了尾时按超时抛错,而不是挂到默认的 900 秒。
            app.run_turn("/work", {}, "", "做事", {"sandbox": "read-only"},
                         binary=binary, label="Codex", timeout=3,
                         on_notification=lambda m, p: seen.append((m, p)))
        return seen

    def test_引擎的turn_completed优先于idle合成(self):
        seen = self._run([
            ("item/completed", {"item": {
                "id": "msg-1", "type": "agentMessage", "phase": "final_answer",
                "text": "收到"}}),
            ("thread/status/changed", {"status": {"type": "idle"}}),
            ("turn/completed", {"turn": {
                "id": "turn-1", "status": "completed", "durationMs": 6772}}),
        ])
        done = [p for m, p in seen if m == "turn/completed"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["turn"]["durationMs"], 6772)

    def test_原生回读终态记录来源(self):
        """缺帧回读有显式来源，不能伪装为收到原生通知。"""
        src = open(os.path.join(os.path.dirname(__file__), "..", "lib",
                                "codex_appserver.py"), encoding="utf-8").read()
        body = src[src.index("def run_turn("):]
        for mark in ("按原生回读确认终态", '"terminalSource": "thread/read"'):
            self.assertIn(mark, body)


class NoOrphans(unittest.TestCase):
    """收掉整棵进程树,不留孤儿。

    `codex` 是一层 node shim,真正干活的是它 spawn 的平台二进制。只 terminate shim 的话,
    那个二进制会被 launchd 收养继续跑,并且一直攥着 rollout——同一段会话从此发不出消息,
    而报错还写着「正在别处使用(终端那侧)」,把人引向别处。
    2026-08-10 实见:一个 ppid=1 的孤儿占着 Desk Opt 的记录,而 ui_server 一直活着。
    """

    def test_连接自成进程组(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "lib",
                                "codex_appserver.py"), encoding="utf-8").read()
        m = re.search(r"self\.proc = subprocess\.Popen\([\s\S]+?\)\n", src)
        self.assertIn("start_new_session=True", m.group(0))

    def test_普通连接也经过父死看护(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "lib",
                                "codex_appserver.py"), encoding="utf-8").read()
        init = src[src.index("class _Connection:"):src.index("    def _drain_stderr", src.index("class _Connection:"))]
        self.assertIn("argv = owned_command(argv, lock_path=lock_path)", init)

    def test_普通连接按Desk会话持有writer锁(self):
        proc = mock.Mock(); proc.stdin=mock.Mock(); proc.stdout=mock.Mock(); proc.stderr=mock.Mock()
        proc.stdout.fileno.return_value=10
        with mock.patch.object(app, '_appserver_argv', return_value=['codex','app-server']), \
             mock.patch.object(app.subprocess, 'Popen', return_value=proc) as spawn, \
             mock.patch.object(app.threading.Thread, 'start'), \
             mock.patch.object(app.selectors, 'DefaultSelector') as selector:
            selector.return_value.register=mock.Mock()
            app._Connection(cwd='/tmp', env={'DESKTRON_NATIVE_WRITER_SESSION_ID':'adp_lock'},
                            binary='/opt/desk/bin/codex', writer_lock_root='/private/desk-data')
        argv=spawn.call_args.args[0]
        self.assertEqual(argv[3],os.path.join('/private/desk-data','adapter','codex',
                                              'adp_lock.native-writer.lock'))
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])

    def test_close_按组发信号(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "lib",
                                "codex_appserver.py"), encoding="utf-8").read()
        m = re.search(r"    def _close_process_tree\(self\):[\s\S]+?(?=\n\n\n|\n    def |\nclass |\n_)", src)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertIn("os.killpg", seg)
        self.assertIn("signal.SIGTERM", seg)
        self.assertIn("signal.SIGKILL", seg)          # 还有活口就补一刀
        # poll() 不可信:宿主装了 SIGCHLD 收割器,状态被抢走之后两个方向都会误判
        self.assertNotIn("if self.proc.poll() is None:", seg)

    def test_close_releases_streams_and_selector_even_if_process_cleanup_fails(self):
        conn = object.__new__(app._Connection)
        conn.proc = mock.Mock()
        conn._selector = mock.Mock()
        conn._close_process_tree = mock.Mock(side_effect=RuntimeError("cleanup failed"))
        with self.assertRaises(RuntimeError):
            conn.close()
        conn.close()
        conn._close_process_tree.assert_called_once()
        conn._selector.close.assert_called_once()
        for stream in (conn.proc.stdin, conn.proc.stdout, conn.proc.stderr):
            stream.close.assert_called_once()

    def test_只收当前连接后代中的node_repl进程组(self):
        ps = """100 1 100 codex app-server --stdio
101 100 100 codex native
102 101 102 /Applications/ChatGPT.app/x/node_repl
103 102 102 /tmp/kernel.js
200 1 200 /Applications/ChatGPT.app/x/node_repl
"""
        fake = mock.Mock(stdout=ps)
        with mock.patch.object(subprocess, "run", return_value=fake):
            self.assertEqual(app._node_repl_process_groups(100), {102})

    @unittest.skipUnless(__import__("shutil").which("codex"), "本机没有 codex")
    @pytest.mark.native
    def test_真起一个再收掉_不留活口(self):
        home = tempfile.TemporaryDirectory(prefix='desk-codex-close-')
        self.addCleanup(home.cleanup)
        env = dict(os.environ, CODEX_HOME=home.name)
        env.pop('DESKTRON_NATIVE_WRITER_SESSION_ID', None)
        conn = app._Connection(cwd="/tmp", env=env)
        try:
            time.sleep(2)
            self.assertIsNone(conn.proc.poll(), '\n'.join(conn._stderr))
            kids = subprocess.run(["pgrep", "-P", str(conn.proc.pid)],
                                  capture_output=True, text=True).stdout.split()
            pids = [conn.proc.pid] + [int(x) for x in kids]
            self.assertEqual(os.getpgid(conn.proc.pid), conn.proc.pid)   # 自成一组
        finally:
            conn.close()
        time.sleep(1.5)
        alive = []
        for p in pids:
            try:
                os.kill(p, 0)
                alive.append(p)
            except OSError:
                pass
        self.assertEqual(alive, [], "close 之后还有活口:%s" % alive)


if __name__ == "__main__":
    unittest.main()

class ApprovalPausesTurnEnd(unittest.TestCase):
    """等人批准时不能按 final_answer 收口(2026-09-02 手机端实见)。

    收到 final_answer 之后,回合循环只等两秒静默就按协议终点收尾。而挂着待人审批时
    服务端在等人点头、本来就不会再发帧——两秒一到,回合被判结束,界面给这一回合挂上
    时间戳与复制/分支那一排(那是「已结束」的记号);人一批准它又继续吐字,于是底下
    写着结束时间、上面还在「处理中」。request() 那条路早就按 _awaiting 放宽过,
    跑回合这条漏了。
    """

    def test_回合循环按待审批放宽静默上限(self):
        src = open(app.__file__, encoding="utf-8").read()
        line = next(x for x in src.splitlines() if "conn.read(min(quiet" in x)
        self.assertIn("tool_left", line)          # node_repl 硬超时仍然压得住
        seg = src[src.index('waiting = getattr(conn, "_awaiting", 0)'):]
        seg = seg[:seg.index("msg = conn.read(")]
        self.assertIn('getattr(conn, "_awaiting", 0)', seg)
        self.assertIn("2 if final_answer[0] else 30", seg)   # 不等人时两档判据不变
