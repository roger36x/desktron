"""codex 接入 HTML 会话视图:启动参数与事件翻译。

事件样本取自本机实跑(codex-cli 0.146.0),不是照文档编的——它的 JSON
形状随版本漂,拿真实输出钉住才有意义。

翻译这一层要保证的是:前端那条流式路径不必分引擎。所以断言落在契约的形状上——
建块用 message.updated,出字用 message.part.delta,工具卡用 message.part.updated 且
state.status 归一到 running/done/error(codex 原样是 completed/failed)。
"""
import copy
import json
import os
import re
import sys
import tempfile
import unittest

import _srcmap
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_argv = sys.argv                                    # deskcfg 从 argv 派生端口与根目录,
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]   # 借用一下再还回去
try:
    import engine_adapters as ea  # noqa: E402
    import deskcfg  # noqa: E402
finally:
    sys.argv = _argv                                # unittest.main 还要读它


CODEX_LOG = [
    '{"type":"thread.started","thread_id":"019fdd8d-f9e2-7d13-a903-12c9189752ef"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message",'
    '"text":"我这就运行命令。","phase":"commentary"}}',
    '{"type":"item.started","item":{"id":"item_1","type":"command_execution",'
    '"command":"/bin/bash -lc \'echo hi\'","aggregated_output":"","exit_code":null,'
    '"status":"in_progress"}}',
    '{"type":"item.completed","item":{"id":"item_1","type":"command_execution",'
    '"command":"/bin/bash -lc \'echo hi\'","aggregated_output":"hi\\n","exit_code":0,'
    '"status":"completed"}}',
    '{"type":"item.completed","item":{"id":"item_2","type":"agent_message",'
    '"text":"命令已完成。","phase":"final_answer"}}',
    '{"type":"turn.completed","usage":{"input_tokens":35369,"cached_input_tokens":28160,'
    '"cache_write_input_tokens":0,"output_tokens":102,"reasoning_output_tokens":0}}',
]


class Recorder(ea.EventBus):
    def __init__(self):
        super().__init__()
        self.events = []

    def publish(self, ev):
        self.events.append(ev)

    def types(self):
        return [e["type"] for e in self.events]

    def of(self, t):
        return [e["properties"] for e in self.events if e["type"] == t]


class Base(unittest.TestCase):
    cls = None
    log = ()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Recorder()
        self.ad = self.cls(self.tmp.name, self.tmp.name, self.bus)
        self.ad._set_engine_sid = lambda sid, esid: None    # 落盘与侧栏登记不在本用例范围
        self.ctx = {"sid": "adp_1", "msg_id": "msg_a_1", "parts": {}, "order": [],
                    "last_tool": None, "t0": 0}

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self):
        for ln in self.log:
            self.ad.handle_event(json.loads(ln), self.ctx)

    def parts(self):
        return [self.ctx["parts"][k] for k in self.ctx["order"]]


class Codex(Base):
    cls = ea.CodexAdapter
    log = CODEX_LOG

    def test_新建与续接的启动参数(self):
        new = self.ad.build_cmd("你好", "", {})
        self.assertEqual(new[:3], ["codex", "exec", "--json"])
        self.assertTrue(new[-1].endswith("你好"))
        old = self.ad.build_cmd("你好", "tid-1", {"mode": "read-only"})
        self.assertEqual(old[:4], ["codex", "exec", "resume", "tid-1"])
        # 沙箱一律经 -c:0.146 起 `exec resume` 不认 -s,两条路必须同一套写法
        self.assertNotIn("-s", old)
        self.assertIn('sandbox_mode="read-only"', old)

    def test_旧mode迁到sandbox_协作mode单独保存(self):
        got = self.ad.normalize_options({"mode": "read-only", "collaboration_mode": "plan",
                                         "model": "gpt-5.6-sol", "effort": "xhigh",
                                         "autocompact": "400000"})
        self.assertEqual(got["sandbox"], "read-only")
        self.assertEqual(got["collaboration_mode"], "plan")
        self.assertEqual(got["autocompact"], "400000")
        self.assertNotIn("mode", got)

    def test_模型目录按版本与命名档位排序(self):
        def model(model_id):
            return {"id": model_id, "model": model_id, "displayName": model_id,
                    "supportedReasoningEfforts": []}

        raw = {"models": [
            model("DeepSeek V4.1 Flash"),
            model("gpt-5.6-luna"),
            model("gpt-5.6-sol"),
            model("gpt-6-astra"),
            model("gpt-5.6-terra"),
            model("gpt-5.5"),
        ], "modes": []}
        with mock.patch("codex_appserver.catalog", return_value=raw):
            models = self.ad.options_catalog()["models"]
        self.assertEqual([row["id"] for row in models], [
            "DeepSeek V4.1 Flash",
            "gpt-6-astra",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.5",
        ])

    def test_压缩窗口进入exec退路且非法值不落盘(self):
        cmd = self.ad.build_cmd("继续", "tid-1", {"autocompact": "600000"})
        self.assertIn("model_auto_compact_token_limit=600000", cmd)
        self.assertNotIn("autocompact", self.ad.normalize_options({"autocompact": "950000"}))

    def test_GUI临时id在首轮后迁到引擎id(self):
        row = self.ad.create_session("desk-iter")
        del self.ad._set_engine_sid
        with mock.patch("term._term_migrate_identity") as migrate, \
             mock.patch("term.term_meta_register") as register:
            self.ad._set_engine_sid(row["id"], "thread-1")
        migrate.assert_called_once_with(
            {"id": row["id"], "engine": "codex", "domain": "desk-iter"}, "thread-1")
        register.assert_called_once_with("thread-1", "codex", "desk-iter", task=False)

    def test_GUI命令把_goenv_放在工作区可写目录(self):
        # Desk 自身启动测试时可能已经给父进程设了 GOENV；本用例验证的是「用户未显式
        # 提供 GOENV」这一支,不能继承运行测试的 Desk 进程值。
        with mock.patch.dict(os.environ, {"GOENV": ""}):
            env = self.ad.spawn_env({})
        self.assertEqual(env["GOENV"], os.path.join(self.tmp.name, ".workbench", "codex-go-env"))

    def test_选项按会话保存_不在会话间串值(self):
        a = self.ad.create_session()["id"]
        b = self.ad.create_session()["id"]
        self.ad.update_session_options(a, {"collaboration_mode": "plan", "effort": "high"})
        self.ad.update_session_options(b, {"collaboration_mode": "default", "effort": "low"})
        self.assertEqual(self.ad.session_options(a)["effort"], "high")
        self.assertEqual(self.ad.session_options(b)["effort"], "low")

    def test_appserver_item沿用契约翻译(self):
        self.ad._app_event("item/started", {"item": {
            "id": "cmd", "type": "commandExecution", "command": "echo hi",
            "aggregatedOutput": "", "exitCode": None, "status": "inProgress"}}, self.ctx)
        self.ad._app_event("item/completed", {"item": {
            "id": "cmd", "type": "commandExecution", "command": "echo hi",
            "aggregatedOutput": "hi\n", "exitCode": 0, "status": "completed"}}, self.ctx)
        tool = self.parts()[0]
        self.assertEqual(tool["state"]["status"], "done")
        self.assertEqual(tool["state"]["output"], "hi\n")

    def test_MCP调用不冒充子代理调度(self):
        self.ad._app_event("item/started", {"item": {
            "id": "mcp", "type": "mcpToolCall", "server": "desk_app_control",
            "tool": "browser_status", "arguments": {}, "status": "inProgress"}}, self.ctx)
        tool = self.parts()[0]
        self.assertEqual(tool["tool"], "mcp")
        self.assertEqual(tool["state"]["input"]["description"],
                         "desk_app_control.browser_status")

    def test_appserver进程登记为当前会话的控制根(self):
        import codex_appserver

        proc = mock.Mock(pid=24680)
        proc.poll.return_value = None

        def run_turn(*args, **kwargs):
            kwargs["on_process"](proc)
            return "thread-1"

        self.ctx.update(text="做事", saved=[], opts={})
        with mock.patch.object(codex_appserver, "run_turn", side_effect=run_turn), \
                mock.patch.object(self.ad, "_note_proc") as note:
            self.assertIsNone(self.ad._turn_dispatch(self.ctx, [], {}, None))
        note.assert_called_once_with("adp_1", proc)

    def test_appserver_commentary与正文仍然分流(self):
        self.ad._app_event("item/completed", {"item": {
            "id": "c", "type": "agentMessage", "text": "先检查。", "phase": "commentary"}}, self.ctx)
        self.ad._app_event("item/completed", {"item": {
            "id": "f", "type": "agentMessage", "text": "完成。", "phase": "final_answer"}}, self.ctx)
        self.assertEqual([(p["type"], p["text"]) for p in self.parts()],
                         [("reasoning", "先检查。"), ("text", "完成。")])

    def test_appserver没有token通知也会下发耗时(self):
        self.ad._app_event("turn/completed", {"turn": {
            "status": "completed", "durationMs": 4321}}, self.ctx)
        stats = self.bus.of("session.stats")[-1]
        self.assertEqual(stats["ms"], 4321)
        self.assertEqual(stats["tokens"], 0)

    def test_appserver环境异常经SSE发布并写入会话(self):
        import codex_appserver

        session = self.ad.create_session()
        self.ctx.update(sid=session["id"], text="做事", saved=[], opts={})

        def run_turn(*args, **kwargs):
            kwargs["on_environment"]({
                "state": "warning", "stage": "ready", "label": "Codex 已就绪，1 项异常",
                "details": ["MCP browser：需要重新登录"],
                "mcp": {"ready": 1, "failed": 1, "total": 2}})
            return "thread-1"

        with mock.patch.object(codex_appserver, "run_turn", side_effect=run_turn):
            self.ad._turn_dispatch(self.ctx, [], {}, None)

        env = self.bus.of("session.environment")[-1]
        self.assertEqual(env["state"], "warning")
        self.assertEqual(self.ad.session_environment(session["id"])["stage"], "ready")

    def test_主动中断冷启动不留下环境错误(self):
        import codex_appserver

        session = self.ad.create_session()
        self.ctx.update(sid=session["id"], text="做事", saved=[], opts={}, aborted="interrupt")

        def run_turn(*args, **kwargs):
            kwargs["on_environment"]({
                "state": "error", "stage": "error", "label": "Codex 启动失败",
                "details": ["app-server 已退出"], "mcp": {}})
            return "thread-1"

        with mock.patch.object(codex_appserver, "run_turn", side_effect=run_turn):
            self.ad._turn_dispatch(self.ctx, [], {}, None)

        self.assertEqual(self.bus.of("session.environment")[-1]["state"], "ready")
        self.assertIsNone(self.ad.session_environment(session["id"]))

    def test_compact命令写入用户气泡投影(self):
        import codex_appserver

        session = self.ad.create_session()
        sid = session["id"]
        self.ad.index[sid]["engine_sid"] = "thread-1"
        during = {}

        def compact_thread(*args, **kwargs):
            during.update(self.ad.turn_state(sid))
            return {"ok": True}

        with mock.patch.object(codex_appserver, "compact_thread", side_effect=compact_thread), \
                mock.patch.object(self.ad, "import_session", return_value=self.ad.index[sid]):
            out = self.ad.compact_session(sid)

        self.assertTrue(out["ok"])
        self.assertTrue(during["compact"]["running"])
        self.assertEqual(during["compacting"], during["compact"]["started"])
        self.assertEqual(self.ad.turn_state(sid)["compact"]["result"], "success")
        users = [m for m in self.ad.messages(sid) if (m.get("info") or {}).get("role") == "user"]
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["parts"][0]["text"], "/compact")

    def test_compact历史导入异常也会结束可查状态(self):
        import codex_appserver

        sid = self.ad.create_session()["id"]
        self.ad.index[sid]["engine_sid"] = "thread-1"
        with mock.patch.object(codex_appserver, "compact_thread", return_value={"ok": True}), \
                mock.patch.object(self.ad, "import_session", side_effect=RuntimeError("导入失败")):
            out = self.ad.compact_session(sid)

        self.assertFalse(out["ok"])
        state = self.ad.turn_state(sid)
        self.assertNotIn("compacting", state)
        self.assertEqual(state["compact"]["result"], "failed")
        self.assertIn("导入失败", state["compact"]["error"])

    def test_Codex打断兜底摘掉appserver会话(self):
        import codex_appserver

        sid = self.ad.create_session()["id"]
        ctx = {"sid": sid}
        self.ad._live[sid] = ctx
        proc = mock.Mock()
        with mock.patch.object(codex_appserver, "drop_session") as drop:
            self.ad._interrupt_fallback(sid, ctx, proc)
        drop.assert_called_once_with(sid)
        proc.kill.assert_not_called()

    def test_Codex软中断无proc也安排兜底(self):
        sid = self.ad.create_session()["id"]
        ctx = {"sid": sid, "t0": 123}
        self.ad._live[sid] = ctx
        with mock.patch.object(self.ad, "_soft_interrupt", return_value=True) as soft, \
                mock.patch.object(ea.threading, "Timer") as timer:
            out = self.ad.abort(sid, recall=False)
        soft.assert_called_once_with(sid, None)
        timer.assert_called_once()
        timer.return_value.start.assert_called_once()
        self.assertEqual(timer.call_args.args[0], self.ad.INTERRUPT_FALLBACK_SECONDS)
        self.assertEqual(ctx["aborted"], "interrupt")
        self.assertNotIn("drop_appserver_after_abort", ctx)
        self.assertEqual(ctx["interrupt_snapshot"]["order"], [])
        self.assertEqual(out["text"], "")
        self.assertEqual(out["state"], "cancelling")
        self.assertEqual(out["contract"], 1)
        self.assertEqual(out["interruptID"], ctx["interrupt_id"])
        self.assertEqual(self.bus.of("session.state")[-1]["state"], "cancelling")

    def test_Codex软中断正常收尾保留appserver会话(self):
        import codex_appserver

        sid = self.ad.create_session()["id"]
        ctx = {"sid": sid, "text": "做事", "saved": [], "opts": {}}
        with mock.patch.object(codex_appserver, "run_turn", return_value="thread-1"), \
                mock.patch.object(codex_appserver, "drop_session") as drop:
            self.assertIsNone(self.ad._turn_dispatch(ctx, [], {}, None))
        drop.assert_not_called()

    def test_Codex中断收尾只落Esc前快照(self):
        sid = self.ad.create_session()["id"]
        early = {"id": "p1", "type": "text", "messageID": "msg_a_int",
                 "sessionID": sid, "text": "已看到的内容"}
        late = {"id": "p2", "type": "text", "messageID": "msg_a_int",
                "sessionID": sid, "text": "Esc 后才到的内容"}
        ctx = {"sid": sid, "msg_id": "msg_a_int", "parts": {"p1": early, "p2": late},
               "order": ["p1", "p2"], "last_tool": None, "t0": 1, "aborted": "interrupt",
               "interrupt_snapshot": {"order": ["p1"], "parts": {"p1": copy.deepcopy(early)}}}
        self.ad._finish_turn(sid, ctx, None)
        assistant = [m for m in self.ad.messages(sid) if (m.get("info") or {}).get("role") == "assistant"]
        self.assertEqual(len(assistant), 1)
        self.assertEqual([p["text"] for p in assistant[0]["parts"]], ["已看到的内容"])

    def test_Codex_active_writer退避重试(self):
        import codex_appserver

        sid = self.ad.create_session()["id"]
        ctx = {"sid": sid, "text": "继续", "saved": [], "opts": {}}
        calls = [RuntimeError("thread 1 already has an active writer"), "thread-1"]
        # 只模拟适配器的时钟，不改全局 time.sleep，避免诊断子进程的等待混入断言。
        clock = mock.Mock(wraps=ea.time)
        clock.sleep = mock.Mock()
        with mock.patch.object(codex_appserver, "run_turn", side_effect=calls) as run, \
                mock.patch.object(codex_appserver, "drop_session") as drop, \
                mock.patch.object(codex_appserver, "writer_probe", return_value="test writer"), \
                mock.patch("engine_adapters.codex.time", clock):
            self.assertIsNone(self.ad._turn_dispatch(ctx, [], {}, None))
        self.assertEqual(run.call_count, 2)
        drop.assert_called_once_with(sid)
        clock.sleep.assert_called_once_with(0.5)

    def test_续接只用_resume_也认的标志(self):
        """`codex exec resume` 的参数表是 `codex exec` 的子集。首回合与续接走的是这两条不同的路,
        只在一边成立的写法要到第二回合才炸(实测掉过一次:--color never 在 resume 上直接退出 2)。
        允许表照 0.146 的 `codex exec resume --help` 抄。"""
        ok = {"--json", "--skip-git-repo-check", "-c", "-m", "--last", "--all", "-i",
              "--strict-config", "--enable", "--disable", "--ephemeral", "--ignore-user-config",
              "--ignore-rules", "--output-schema", "-o",
              "--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"}
        for opts in ({}, {"mode": "read-only", "model": "gpt-5", "effort": "high"},
                     {"mode": "danger-full-access"}):
            cmd = self.ad.build_cmd("你好", "tid-1", opts)
            flags = [x for x in cmd[4:-1] if x.startswith("-")]
            self.assertTrue(set(flags) <= ok, (opts, set(flags) - ok))

    def test_越权档走bypass而不是sandbox_mode(self):
        cmd = self.ad.build_cmd("x", "", {"mode": "danger-full-access"})
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertNotIn("approval_policy=\"never\"", cmd)

    def test_首回合前置角色正文_续接不再重复(self):
        self.ad.system_body = lambda opts: "纪律正文"
        for sid in ('', 'tid-1'):
            cmd = self.ad.build_cmd('你好', sid, {})
            self.assertEqual(cmd[-1], '你好')
            self.assertTrue(any(value.startswith('developer_instructions=') and '纪律正文' in value
                                for value in cmd))

    def test_需要用户动作的内容必须进正文(self):
        body = self.ad.system_body({})
        self.assertIn("需要用户执行", body)
        self.assertIn("final_answer", body)
        self.assertIn("不得只写在 `commentary`", body)

    def test_首回合前模型名来自appserver默认项(self):
        self.ad.options_catalog = lambda: {"models": [
            {"id": "gpt-5.6-sol", "label": "GPT-5.6-Sol", "default": True}]}
        self.assertEqual(self.ad.config_label(), "codex · GPT-5.6-Sol")

    def test_thread_id_即引擎会话锚点(self):
        seen = {}
        self.ad._set_engine_sid = lambda sid, esid: seen.setdefault("esid", esid)
        self.feed()
        self.assertEqual(seen["esid"], "019fdd8d-f9e2-7d13-a903-12c9189752ef")
        self.assertEqual(self.ctx["esid"], "019fdd8d-f9e2-7d13-a903-12c9189752ef")

    def test_整块出字也走_delta(self):
        self.feed()
        deltas = self.bus.of("message.part.delta")
        self.assertEqual([d["delta"] for d in deltas], ["我这就运行命令。", "命令已完成。"])
        self.assertEqual([d["field"] for d in deltas], ["reasoning", "text"])

    def test_commentary进处理中_final_answer才进正文(self):
        self.feed()
        parts = self.parts()
        self.assertEqual([(p["type"], p.get("text")) for p in parts if p["type"] != "tool"], [
            ("reasoning", "我这就运行命令。"),
            ("text", "命令已完成。"),
        ])

    def test_同一条_item_只画一次(self):
        self.feed()
        self.ad.handle_event(json.loads(CODEX_LOG[2]), self.ctx)   # 重放 item.completed
        self.assertEqual(len(self.bus.of("message.part.delta")), 2)

    def test_历史rollout也按phase分流(self):
        rows = [
            {"timestamp": "2026-08-09T04:00:00Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"text": "开始"}]}},
            {"timestamp": "2026-08-09T04:00:01Z", "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "phase": "commentary",
                "content": [{"text": "我先检查。"}]}},
            {"timestamp": "2026-08-09T04:00:02Z", "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "phase": "final_answer",
                "content": [{"text": "检查完成。"}]}},
        ]
        path = os.path.join(self.tmp.name, "rollout.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

        msgs = self.ad._group_rollout(path, "adp_1")
        assistant = [m for m in msgs if m["info"]["role"] == "assistant"][0]
        self.assertEqual([(p["type"], p["text"]) for p in assistant["parts"]], [
            ("reasoning", "我先检查。"), ("text", "检查完成。"),
        ])

    def test_rollout助手使用自身时刻而不是下一条用户消息(self):
        rows = [
            {"timestamp": "2026-08-09T04:00:00Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"text": "第一问"}]}},
            {"timestamp": "2026-08-09T04:00:01Z", "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "phase": "final_answer",
                "content": [{"text": "第一答"}]}},
            {"timestamp": "2026-08-09T04:00:02Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"text": "第二问"}]}},
        ]
        rollout = os.path.join(self.tmp.name, "rollout-order.jsonl")
        with open(rollout, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

        msgs = self.ad._group_rollout(rollout, "adp_1")
        self.assertEqual([m["info"]["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual(msgs[1]["info"]["at"], 1786248001000)
        self.assertLess(msgs[1]["info"]["at"], msgs[2]["info"]["at"])
        self.assertNotEqual(msgs[1]["info"]["id"], msgs[2]["info"]["id"])

    def test_rollout图片标记恢复为图片块(self):
        sid = "adp_1"
        upload = os.path.join(self.ad.dir, "uploads", sid)
        os.makedirs(upload)
        path = os.path.join(upload, "shot.png")
        with open(path, "wb") as f:
            f.write(b"png-bytes")
        rows = [{"timestamp": "2026-08-09T04:00:00Z", "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"text":
                f'看这里<image name=[Image #1] path="{path}"></image>'}]}}]
        rollout = os.path.join(self.tmp.name, "rollout-image.jsonl")
        with open(rollout, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        parts = self.ad._group_rollout(rollout, sid)[0]["parts"]
        self.assertEqual([p["type"] for p in parts], ["text", "image"])
        self.assertEqual(parts[0]["text"], "看这里")
        # 2026-08-20 起正文里存引用不存内容:图本来就在这段会话的 uploads 下,
        # 读出来转成 base64 再塞回正文是白做一遍,而且每次打开会话都要重传重解析。
        self.assertEqual(parts[1]["url"], f"/session/{sid}/asset/shot.png")
        self.assertNotIn("data_b64", parts[1])
        self.assertEqual(self.ad.asset_path(sid, "shot.png"), os.path.realpath(path))

    def test_rollout图片不能读取会话目录外文件(self):
        outside = os.path.join(self.tmp.name, "outside.png")
        with open(outside, "wb") as f:
            f.write(b"secret")
        parts = self.ad._rollout_user_parts(
            f'正文<image path="{outside}"></image>', "msg_u", "adp_1")
        self.assertEqual([(p["type"], p.get("text")) for p in parts], [("text", "正文")])

    def test_旧消息里的图片标记读取时修复(self):
        sid = self.ad.create_session()["id"]
        upload = os.path.join(self.ad.dir, "uploads", sid)
        os.makedirs(upload)
        path = os.path.join(upload, "old.png")
        with open(path, "wb") as f:
            f.write(b"old")
        mid = "msg_u_old"
        self.ad._append_msg(sid, {"info": {"id": mid, "role": "user", "sessionID": sid},
            "parts": [{"id": mid + "_p0", "type": "text",
                       "text": f'说明<image path="{path}"></image>'}]})
        self.assertEqual([p["type"] for p in self.ad.messages(sid)[0]["parts"]], ["text", "image"])

    def test_rollout跳过_agents_注入(self):
        rows = [
            {"timestamp": "2026-08-09T04:00:00Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"text":
                    "# AGENTS.md instructions for /work\n\n<INSTRUCTIONS>系统内容</INSTRUCTIONS>"}]}},
            {"timestamp": "2026-08-09T04:00:01Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"text": "真人问题"}]}},
        ]
        rollout = os.path.join(self.tmp.name, "rollout-agents.jsonl")
        with open(rollout, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        msgs = self.ad._group_rollout(rollout, "adp_1")
        self.assertEqual([m["parts"][0]["text"] for m in msgs], ["真人问题"])

    def test_codex导入会话按尾窗读取并只保留最近回合(self):
        row = self.ad.create_session()
        rollout = os.path.join(self.tmp.name, "rollout-window.jsonl")
        rows = []
        for i in range(8):
            rows.extend([
                {"timestamp": f"2026-08-09T04:00:{i * 2:02d}Z", "type": "response_item",
                 "payload": {"type": "message", "role": "user",
                             "content": [{"text": "问题 " + str(i)}]}},
                {"timestamp": f"2026-08-09T04:00:{i * 2 + 1:02d}Z", "type": "response_item",
                 "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                             "content": [{"text": "回答 " + str(i)}]}},
            ])
        with open(rollout, "w", encoding="utf-8") as f:
            for item in rows:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        with mock.patch.object(self.ad, "_rollout_path", return_value=rollout), \
                mock.patch.object(self.ad, "_external_title", return_value="标题"):
            self.ad.import_session("019ee670-40e5-7972-abaf-b945ef812bb9", row["id"], limit=2)
        users = [m["parts"][0]["text"] for m in self.ad.messages(row["id"])
                 if (m.get("info") or {}).get("role") == "user"]
        # 窗口按需要的回合数往回数,不是按固定字节。要 2 轮就不该把 8 轮全搬回来——
        # 退回整份读的话最早那轮会跟着出现,这条就是守着它。
        self.assertEqual(users[-2:], ["问题 6", "问题 7"])
        self.assertNotIn("问题 0", users)
        self.assertLess(len(users), 8)

    def test_codex尾窗从半个回合切入时从下一条用户消息开始(self):
        row = self.ad.create_session()
        rollout = os.path.join(self.tmp.name, "rollout-partial-window.jsonl")
        rows = []
        for i in range(5):
            rows.extend([
                {"timestamp": f"2026-08-09T04:00:{i * 2:02d}Z", "type": "response_item",
                 "payload": {"type": "message", "role": "user",
                             "content": [{"text": "问题 " + str(i)}]}},
                {"timestamp": f"2026-08-09T04:00:{i * 2 + 1:02d}Z", "type": "response_item",
                 "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                             "content": [{"text": "回答 " + str(i)}]}},
            ])
        with open(rollout, "wb") as f:
            for item in rows:
                f.write((json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8"))
        with open(rollout, "rb") as f:
            raw = f.read()
        start = raw.index("问题 2".encode("utf-8")) + 2
        with mock.patch.object(self.ad, "_rollout_path", return_value=rollout), \
                mock.patch.object(self.ad, "_external_title", return_value="标题"), \
                mock.patch.object(self.ad, "_rollout_window_start",
                                  lambda p, size, marks: start):
            self.ad.import_session("019ee670-40e5-7972-abaf-b945ef812bb9", row["id"], limit=None)
        roles = [(m["info"]["role"], m["parts"][0].get("text")) for m in self.ad.messages(row["id"])]
        self.assertEqual(roles[0], ("user", "问题 3"))
        self.assertNotIn(("assistant", "回答 2"), roles)

    def test_命令卡_从执行中到已完成(self):
        for ln in self.log[:4]:
            self.ad.handle_event(json.loads(ln), self.ctx)
        tool = [p for p in self.parts() if p["type"] == "tool"][0]
        self.assertEqual(tool["tool"], "bash")
        self.assertEqual(tool["state"]["status"], "running")
        self.ad.handle_event(json.loads(self.log[4]), self.ctx)
        self.assertEqual(tool["state"]["status"], "done")
        self.assertEqual(tool["state"]["input"]["command"], "echo hi")   # 外壳已剥
        self.assertIn("hi", tool["state"]["output"])

    def test_剥掉_bash_lc_外壳(self):
        """codex 把每条命令包在 `/bin/bash -lc "…"` 里。前端取首词当宾语,不剥壳的话
        一整列过程行全是「已执行 /bin/bash」(2026-08-09 界面上实见)。"""
        u = self.ad._unwrap_cmd
        self.assertEqual(u('/bin/bash -lc "pwd && rg --files"'), "pwd && rg --files")
        self.assertEqual(u("/bin/bash -lc 'echo hi'"), "echo hi")
        self.assertEqual(u('bash -c "ls -la"'), "ls -la")
        self.assertEqual(u("sh -c 'make test'"), "make test")
        self.assertEqual(u("python3 script.py"), "python3 script.py")   # 没有壳,原样
        self.assertEqual(u("/bin/bash"), "/bin/bash")                   # 只有壳名,原样

    def test_functions_exec包装脚本显示实际任务而不是_const(self):
        raw = '''const results = await Promise.all([
          tools.exec_command({"cmd":"git status --short"}),
          tools.exec_command({"cmd":"rg -n \\"session\\" frontend/app-chat.js"})
        ]);'''
        got = self.ad._command_input(raw)
        self.assertEqual(got["description"], "检查仓库状态 等 2 项")
        self.assertNotIn("const", got["description"])

    def test_英文目标与后续中文之间保留空格(self):
        raw = '''const results = await Promise.all([
          tools.exec_command({"cmd":"sed -n '1,20p' frontend/app-chat.js"}),
          tools.exec_command({"cmd":"rg -n diff frontend/app-chat.js"}),
          tools.exec_command({"cmd":"node --check frontend/app-chat.js"})
        ]);'''
        self.assertEqual(self.ad._command_input(raw)["description"], "读取 app-chat.js 等 3 项")

    def test_代码模式标题可作为工具摘要(self):
        raw = '''// @exec: {"max_output_tokens": 20000}
        const setup = await tools.mcp__node_repl__js({title:"连接应用界面", code:"..."});'''
        self.assertEqual(self.ad._command_input(raw)["description"], "连接应用界面")

    def test_python脚本显示脚本名(self):
        got = self.ad._command_input("python3 .claude/skills/journal/scripts/journal_append.py --request")
        self.assertEqual(got["description"], "运行 journal_append.py")

    def test_codex命令摘要补充_git与脚本意图(self):
        cases = {
            "git log --oneline -5": "查看提交历史",
            "git show --stat HEAD": "查看提交差异",
            "git branch --show-current": "查看分支",
            "git rev-parse --show-toplevel": "解析 Git 引用",
            "git ls-files frontend": "列出仓库文件",
            "python3 - <<'PY'\nimport ast\nast.parse('x=1')\nPY": "运行语法检查脚本",
            "python3 - <<'PY'\nimport subprocess\nsubprocess.run(['git','status'])\nPY": "运行 Git 检查脚本",
            "cat <<'EOF'\nhello\nEOF": "运行脚本",
        }
        for cmd, desc in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(self.ad._command_input(cmd)["description"], desc)

    def test_codex搜索卡兼容_appserver_不同字段(self):
        samples = [
            {"type": "web_search", "query": "alpha"},
            {"type": "web_search", "queries": ["alpha"]},
            {"type": "web_search", "input": {"query": "alpha"}},
            {"type": "web_search", "arguments": {"query": "alpha"}},
            {"type": "web_search", "arguments": '{"query":"alpha"}'},
        ]
        for item in samples:
            with self.subTest(item=item):
                inp = self.ad._tool_input(item)
                self.assertEqual(inp["pattern"], "alpha")
                self.assertEqual(inp["description"], "搜索 alpha")

    def test_旧存储读取时补齐命令摘要(self):
        sid = self.ad.create_session()["id"]
        mid = "msg_a_old"
        self.ad._append_msg(sid, {"info": {"id": mid, "role": "assistant", "sessionID": sid},
            "parts": [{"id": mid + "_k0", "type": "tool", "tool": "bash",
                       "state": {"status": "done", "input": {
                           "command": 'const r = tools.exec_command({cmd: "git diff --check"})'},
                           "output": ""}}]})
        inp = self.ad.messages(sid)[0]["parts"][0]["state"]["input"]
        self.assertEqual(inp["description"], "检查代码差异")

    def test_rollout导入直接写入摘要(self):
        tool, inp = self.ad._rollout_tool("exec", '''const r = await tools.exec_command({
          cmd: "rg -n \\"session\\" frontend/app-chat.js"});''')
        self.assertEqual(tool, "bash")
        self.assertEqual(inp["description"], "查找 session")

    def test_codex手动标题不覆盖引擎索引(self):
        row = self.ad.create_session()
        esid = "019ee670-40e5-7972-abaf-b945ef812bb9"
        self.ad.index[row["id"]].update(engine_sid=esid, title="原生名")
        with mock.patch("term._custom_title_for", return_value="Desk Opt"), \
                mock.patch("term._codex_sessions", return_value=[{"id": esid, "title": "原生名"}]):
            changed = self.ad.sync_external_title(esid)
        self.assertEqual(changed, [])
        self.assertEqual(self.ad.index[row["id"]]["title"], "原生名")

    def test_导入会话优先使用侧栏标题(self):
        row = self.ad.create_session()
        rollout = os.path.join(self.tmp.name, "rollout.jsonl")
        with open(rollout, "w", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": "2026-08-09T04:00:00Z", "type": "response_item",
                                "payload": {"type": "message", "role": "user",
                                            "content": [{"text": "旧的首条问题"}]}},
                               ensure_ascii=False) + "\n")
        with mock.patch.object(self.ad, "_rollout_path", return_value=rollout), \
                mock.patch.object(self.ad, "_external_title", return_value="Desk Opt"):
            got = self.ad.import_session("019ee670-40e5-7972-abaf-b945ef812bb9", row["id"])
        self.assertEqual(got["title"], "Desk Opt")

    def test_rollout对账保留Desk量到的回合耗时(self):
        row = self.ad.create_session()
        mid = "msg_a_old"
        self.ad._append_msg(row["id"], {"info": {
            "id": mid, "role": "assistant", "sessionID": row["id"], "ms": 4321},
            "parts": [{"id": mid + "_t", "type": "text", "text": "完成。"}]})
        rollout = os.path.join(self.tmp.name, "rollout-ms.jsonl")
        rows = [
            {"timestamp": "2026-08-09T04:00:00Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"text": "开始"}]}},
            {"timestamp": "2026-08-09T04:00:01Z", "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "phase": "final_answer",
                "content": [{"text": "完成。"}]}},
        ]
        with open(rollout, "w", encoding="utf-8") as f:
            for item in rows:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        with mock.patch.object(self.ad, "_rollout_path", return_value=rollout), \
                mock.patch.object(self.ad, "_external_title", return_value="标题"):
            self.ad.import_session("019ee670-40e5-7972-abaf-b945ef812bb9", row["id"])
        assistant = next(m for m in self.ad.messages(row["id"])
                         if (m.get("info") or {}).get("role") == "assistant")
        self.assertEqual(assistant["info"]["ms"], 4321)

    def test_rollout对账不会让下一轮清理吞掉上一轮回复(self):
        row = self.ad.create_session()
        rollout = os.path.join(self.tmp.name, "rollout-two-turns.jsonl")
        rows = [
            {"timestamp": "2026-08-09T04:00:00Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"text": "第一问"}]}},
            {"timestamp": "2026-08-09T04:00:01Z", "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "phase": "final_answer",
                "content": [{"text": "第一答"}]}},
            {"timestamp": "2026-08-09T04:00:02Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"text": "第二问"}]}},
        ]
        with open(rollout, "w", encoding="utf-8") as f:
            for item in rows:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        with mock.patch.object(self.ad, "_rollout_path", return_value=rollout), \
                mock.patch.object(self.ad, "_external_title", return_value="标题"):
            self.ad.import_session("019ee670-40e5-7972-abaf-b945ef812bb9", row["id"])

        self.ad._drop_since(row["id"], 1786248002000)
        texts = ["".join(p.get("text") or "" for p in m.get("parts") or [])
                 for m in self.ad.messages(row["id"])]
        self.assertIn("第一答", texts)

    def test_处理中不让rollout导入覆盖消息投影(self):
        row = self.ad.create_session()
        sid = row["id"]
        self.ad._append_msg(sid, {"info": {"id": "msg_u_live", "role": "user",
                                            "sessionID": sid},
                                  "parts": [{"id": "p", "type": "text", "text": "正在处理"}]})
        self.ad._live[sid] = {"t0": 1}
        rollout = os.path.join(self.tmp.name, "rollout-stale.jsonl")
        with open(rollout, "w", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": "2026-08-09T04:00:00Z", "type": "response_item",
                                "payload": {"type": "message", "role": "user",
                                            "content": [{"text": "旧记录"}]}}) + "\n")
        with mock.patch.object(self.ad, "_rollout_path", return_value=rollout):
            self.ad.import_session("019ee670-40e5-7972-abaf-b945ef812bb9", sid)
        self.assertEqual(self.ad.messages(sid)[0]["parts"][0]["text"], "正在处理")

    def test_处理中快照在后端重载后恢复(self):
        self.ctx["parts"]["k1"] = {"id": "k1", "type": "tool", "tool": "bash",
            "messageID": self.ctx["msg_id"], "sessionID": self.ctx["sid"],
            "state": {"status": "running", "input": {"description": "读取 engine_adapters.py"},
                      "output": "partial"}}
        self.ctx["order"] = ["k1"]
        self.ad._checkpoint_live(self.ctx, force=True)

        reloaded = self.cls(self.tmp.name, self.tmp.name, Recorder())
        got = reloaded.live_message(self.ctx["sid"])
        self.assertTrue(got["info"]["recovered"])
        self.assertFalse(got["info"]["running"])
        self.assertTrue(got["info"]["interrupted"])
        self.assertTrue(got["info"]["local_only"])
        self.assertEqual(got["parts"][0]["state"]["status"], "error")
        self.assertIn("应用重载", got["parts"][0]["state"]["output"])

    def test_重载快照在下一轮开始前固化(self):
        sid = self.ad.create_session()["id"]
        started = int(ea.time.time() * 1000) - 1000
        self.ad._append_msg(sid, {"info": {
            "id": "msg_u_old", "role": "user", "sessionID": sid,
            "seq": started, "source": "user"},
            "parts": [{"id": "msg_u_old_p0", "type": "text", "text": "上一问"}]})
        old = {"sid": sid, "msg_id": "msg_a_old", "t0": started,
               "seq": started, "source": "user",
               "parts": {"r1": {"id": "r1", "type": "reasoning", "text": "已产生的过程",
                                  "messageID": "msg_a_old", "sessionID": sid}},
               "order": ["r1"]}
        self.ad._checkpoint_live(old, force=True)

        reloaded = self.cls(self.tmp.name, self.tmp.name, Recorder())
        # 页面先读过一次并不会消费快照；真正的安全闸在下一轮覆盖它之前。
        self.assertEqual(reloaded.live_message(sid)["parts"][0]["text"], "已产生的过程")
        with mock.patch.object(reloaded, "grounding", return_value={"scene": "", "constant": ""}), \
                mock.patch.object(reloaded, "build_cmd", return_value=[]), \
                mock.patch.object(reloaded, "spawn_env", return_value={}), \
                mock.patch.object(reloaded, "_turn_dispatch", return_value=None), \
                mock.patch.object(reloaded, "_recap_arm"):
            reloaded._run(sid, "继续", {}, [])

        msgs = reloaded.messages(sid)
        self.assertEqual([(m["info"]["role"], m["info"]["id"]) for m in msgs], [
            ("user", "msg_u_old"), ("assistant", "msg_a_old"),
            ("user", msgs[-1]["info"]["id"]),
        ])
        saved = msgs[1]
        self.assertEqual(saved["parts"][0]["text"], "已产生的过程")
        self.assertTrue(saved["info"]["interrupted"])
        self.assertTrue(saved["info"]["local_only"])
        self.assertEqual(saved["info"]["seq"], started)

    def test_正常收尾清理处理中快照(self):
        sid = self.ad.create_session()["id"]
        ctx = {"sid": sid, "msg_id": "msg_a_finish", "parts": {}, "order": [],
               "last_tool": None, "t0": 1}
        self.ad._checkpoint_live(ctx, force=True)
        self.assertTrue(os.path.exists(self.ad._live_checkpoint_path(sid)))
        self.ad._finish_turn(sid, ctx, None)
        self.assertFalse(os.path.exists(self.ad._live_checkpoint_path(sid)))

    def test_普通静默与软停滞使用同一观察阈值(self):
        self.assertEqual(self.ad.STALL_SECONDS, self.ad.TURN_TIMEOUT)
        self.assertGreaterEqual(self.ad.STALL_SECONDS, 900)

    def test_改了多个文件时给文件名而不是只报数量(self):
        ch = [{"path": "/w/a.py", "kind": "update"}, {"path": "/w/b.py", "kind": "update"},
              {"path": "/w/c.py", "kind": "update"}]
        item = {"id": "i", "type": "file_change", "status": "completed", "changes": ch}
        self.assertEqual(self.ad._tool_input(item), {"description": "a.py、b.py、c.py"})
        self.assertEqual(self.ad._tool_input({**item, "changes": ch[:1]}),
                         {"file_path": "/w/a.py"})
        many = self.ad._tool_input({**item, "changes": ch + [{"path": "/w/d.py", "kind": "add"}]})
        self.assertEqual(many["description"], "a.py、b.py、c.py 等 4 个文件")

    def test_改了哪些文件写进展开区(self):
        """不给 output 的话,界面上是一条能点开、点开却什么都没有的行。"""
        item = {"id": "i", "type": "file_change", "status": "completed",
                "changes": [{"path": "/w/a.py", "kind": "update"}, {"path": "/w/d.py", "kind": "add"}]}
        self.ad.handle_event({"type": "item.completed", "item": item}, self.ctx)
        out = self.parts()[0]["state"]["output"]
        self.assertIn("修改  /w/a.py", out)
        self.assertIn("新增  /w/d.py", out)

    def test_变更详情以结构化diff交给前端(self):
        item = {"type": "file_change", "changes": [
            {"path": "/w/a.py", "kind": "update", "diff": "@@ -1 +1 @@\n-old\n+new"},
            {"path": "/w/b.js", "kind": "update", "diff": "@@ -2 +2 @@\n-x\n+y"}]}
        got = self.ad._tool_input(item)
        self.assertEqual(got["description"], "a.py、b.js")
        self.assertEqual(got["changes"][0]["file_path"], "/w/a.py")
        self.assertIn("+new", got["changes"][0]["diff"])

    def test_rollout补丁事件恢复为可展开的编辑卡(self):
        rows = [
            {"timestamp": "2026-08-09T04:00:00Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"text": "修改文件"}]}},
            {"timestamp": "2026-08-09T04:00:01Z", "type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "exec", "call_id": "c1",
                "input": "const r = await tools.apply_patch(patch);"}},
            {"timestamp": "2026-08-09T04:00:02Z", "type": "event_msg", "payload": {
                "type": "patch_apply_end", "success": True, "changes": {
                    "/w/a.py": {"type": "update", "unified_diff": "@@ -1 +1 @@\n-old\n+new"}}}},
            {"timestamp": "2026-08-09T04:00:03Z", "type": "response_item", "payload": {
                "type": "custom_tool_call_output", "call_id": "c1", "output": "{}"}},
        ]
        rollout = os.path.join(self.tmp.name, "rollout-patch.jsonl")
        with open(rollout, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        msgs = self.ad._group_rollout(rollout, "adp_1")
        tools = [p for m in msgs for p in m.get("parts") or [] if p.get("type") == "tool"]
        self.assertEqual([p["tool"] for p in tools], ["edit"])
        self.assertIn("+new", tools[0]["state"]["input"]["diff"])
        self.assertIn("+new", tools[0]["state"]["output"])

    def test_退出码不为零一律记失败(self):
        """命令跑完了但没跑成,codex 把这两件事分在 status 与 exit_code 两个字段上。"""
        item = {"id": "i", "type": "command_execution", "command": "/bin/bash -lc 'false'",
                "aggregated_output": "", "exit_code": 1, "status": "completed"}
        self.ad.handle_event({"type": "item.completed", "item": item}, self.ctx)
        self.assertEqual(self.parts()[0]["state"]["status"], "error")

    def test_失败的退出码写进结果(self):
        item = {"id": "i9", "type": "command_execution", "command": "false",
                "aggregated_output": "", "exit_code": 1, "status": "failed"}
        self.ad.handle_event({"type": "item.completed", "item": item}, self.ctx)
        tool = self.parts()[0]
        self.assertEqual(tool["state"]["status"], "error")
        self.assertIn("退出码 1", tool["state"]["output"])

    def test_用量与耗时随收尾报一次(self):
        self.feed()
        st = self.bus.of("session.stats")[-1]
        self.assertEqual(st["tokens"], 35369 + 102 + 28160)
        self.assertTrue(self.ctx["ms"] >= 0)

    def test_引擎自报的失败转成_session_error(self):
        self.ad.handle_event({"type": "turn.failed", "error": {"message": "额度不足"}}, self.ctx)
        self.assertEqual(self.bus.of("session.error")[0]["error"]["message"], "额度不足")

    def test_不认识的_item_不画空壳(self):
        self.ad.handle_event({"type": "item.completed",
                              "item": {"id": "iX", "type": "未来的新类型"}}, self.ctx)
        self.assertEqual(self.parts(), [])


class Caps(unittest.TestCase):
    """能力位:前端按它显隐入口,不按引擎名写 if。"""

    def test_出字粒度与权限回路各引擎不同(self):
        self.assertTrue(ea.ClaudeAdapter.streams_text)
        self.assertTrue(ea.CodexAdapter.streams_text)     # app-server 逐字发,2026-08-10 接上
        self.assertTrue(ea.ClaudeAdapter.asks_permission)
        # codex 家族 2026-08-16 接上审批:app-server 的 requestApproval 转成同一个
        # permission.asked,approvalPolicy 随之从 never 放到 on-request(见 test_codex_approval)
        self.assertTrue(ea.CodexAdapter.asks_permission)

    def test_分支只有_claude_有(self):
        self.assertTrue(ea.ClaudeAdapter.fork_flag)
        self.assertIsNone(ea.CodexAdapter.fork_flag)


class CodexAppStreaming(unittest.TestCase):
    """codex 经 app-server 是逐字发的(实测「你好世界」分两帧到,字段是 itemId + delta)。

    此前 _app_event 只认 item/started 与 item/completed,delta 全丢:正文要等整块写完
    才一次落下,界面上是长时间空白然后突然出现一整段,与「没在动」分不出来。
    """

    def setUp(self):
        self.bus = Recorder()
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.CodexAdapter(self.tmp.name, self.tmp.name, self.bus)
        self.ad._set_engine_sid = lambda *a: None
        self.ctx = {"sid": "adp_1", "msg_id": "m1", "parts": {}, "order": [],
                    "last_tool": None, "t0": 0}

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self, *events):
        for method, params in events:
            self.ad._app_event(method, params, self.ctx)

    def parts(self):
        return [self.ctx["parts"][k] for k in self.ctx["order"]]

    def _msg(self, iid, phase, text=""):
        return {"item": {"type": "agentMessage", "id": iid, "text": text, "phase": phase}}

    def test_逐字出字(self):
        self.feed(("item/started", self._msg("x", "final_answer")),
                  ("item/agentMessage/delta", {"itemId": "x", "delta": "你好"}),
                  ("item/agentMessage/delta", {"itemId": "x", "delta": "世界"}))
        self.assertEqual([(p["type"], p["text"]) for p in self.parts()], [("text", "你好世界")])
        self.assertEqual([d["delta"] for d in self.bus.of("message.part.delta")], ["你好", "世界"])

    def test_完成帧不重复delta并发布权威快照(self):
        self.feed(("item/started", self._msg("x", "final_answer")),
                  ("item/agentMessage/delta", {"itemId": "x", "delta": "你好世界"}),
                  ("item/completed", self._msg("x", "final_answer", "你好世界")))
        self.assertEqual(len(self.parts()), 1)
        self.assertEqual([d["delta"] for d in self.bus.of("message.part.delta")], ["你好世界"])
        snapshots = self.bus.of("message.part.updated")
        self.assertEqual([(x["part"]["type"], x["part"]["text"]) for x in snapshots],
                         [("text", "你好世界")])

    def test_丢帧时以完成帧补齐差额(self):
        self.feed(("item/started", self._msg("x", "final_answer")),
                  ("item/agentMessage/delta", {"itemId": "x", "delta": "你好"}),
                  ("item/completed", self._msg("x", "final_answer", "你好世界")))
        self.assertEqual(self.parts()[0]["text"], "你好世界")
        self.assertEqual([d["delta"] for d in self.bus.of("message.part.delta")], ["你好", "世界"])
        self.assertEqual(self.bus.of("message.part.updated")[-1]["part"]["text"], "你好世界")

    def test_前缀对不上时完成帧权威替换(self):
        self.feed(("item/started", self._msg("x", "final_answer")),
                  ("item/agentMessage/delta", {"itemId": "x", "delta": "甲"}),
                  ("item/completed", self._msg("x", "final_answer", "乙丙")))
        self.assertEqual(self.parts()[0]["text"], "乙丙")
        snap = self.bus.of("message.part.updated")[-1]["part"]
        self.assertEqual(snap["text"], "乙丙")
        self.assertTrue(snap["replace"])
        self.assertNotIn("replace", self.parts()[0])

    def test_commentary_进处理中_final_answer_进正文(self):
        self.feed(("item/started", self._msg("c", "commentary")),
                  ("item/agentMessage/delta", {"itemId": "c", "delta": "我先检查"}),
                  ("item/started", self._msg("f", "final_answer")),
                  ("item/agentMessage/delta", {"itemId": "f", "delta": "检查完了"}))
        self.assertEqual([(p["type"], p["text"]) for p in self.parts()],
                         [("reasoning", "我先检查"), ("text", "检查完了")])

    def test_没有_itemId_或空_delta_一律忽略(self):
        self.feed(("item/agentMessage/delta", {"delta": "x"}),
                  ("item/agentMessage/delta", {"itemId": "y", "delta": ""}))
        self.assertEqual(self.parts(), [])

    def test_contextCompaction按自动压缩成对上报(self):
        self.feed(("item/started", {"item": {
            "type": "contextCompaction", "id": "cmp1", "status": "inProgress"}}))
        rows = self.bus.of("session.compact")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trigger"], "auto")
        self.assertTrue(rows[0]["running"])
        self.assertGreater(rows[0]["started"], 0)
        self.assertEqual(self.ctx["compact_t0"], rows[0]["started"])

        self.feed(("item/completed", {"item": {
            "type": "contextCompaction", "id": "cmp1", "status": "completed"}}))
        rows = self.bus.of("session.compact")
        self.assertEqual([r["running"] for r in rows], [True, False])
        self.assertEqual([r["trigger"] for r in rows], ["auto", "auto"])
        self.assertEqual(self.ctx["compact_t0"], 0)
        self.assertEqual(self.parts(), [])


class FileChangeShapes(unittest.TestCase):
    """改动种类在两条路上形状不同:`codex exec --json` 给字符串 "update",
    app-server 给对象 {"type": "update", "move_path": null}。

    原来直接拿它当字典键,遇到 app-server 那种就是 TypeError(cannot use 'dict' as a dict
    key),整个回合在改文件那一步炸掉——只有改文件的回合会走到,所以不碰文件的探针一直
    照不出来。2026-08-10 从日志里逮到原话才定位。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Recorder()
        self.ad = ea.CodexAdapter(self.tmp.name, self.tmp.name, self.bus)
        self.ad._set_engine_sid = lambda *a: None
        self.ctx = {"sid": "s", "msg_id": "m", "parts": {}, "order": [],
                    "last_tool": None, "t0": 0}

    def tearDown(self):
        self.tmp.cleanup()

    def _out(self):
        return [self.ctx["parts"][k] for k in self.ctx["order"]][0]["state"]["output"]

    def test_对象形状的_kind_不再炸(self):
        self.ad._app_event("item/completed", {"item": {
            "type": "fileChange", "id": "fc1", "status": "completed", "changes": [
                {"path": "/w/n.py", "kind": {"type": "update", "move_path": None},
                 "diff": "@@ -1,2 +1,2 @@\n-a = 1\n+a = 6\n"}]}}, self.ctx)
        out = self._out()
        self.assertIn("修改  /w/n.py", out)
        self.assertIn("+a = 6", out)          # 带 diff 时展开区给正文,不只是清单

    def test_字符串形状照旧(self):
        self.ad._item({"type": "file_change", "id": "f2", "status": "completed", "changes": [
            {"path": "/w/a.py", "kind": "add"}, {"path": "/w/b.py", "kind": "delete"}]},
            self.ctx, done=True)
        self.assertEqual(self._out().splitlines(), ["新增  /w/a.py", "删除  /w/b.py"])

    def test_认不出的种类原样带过(self):
        self.assertEqual(ea.CodexAdapter._change_kind({"kind": {"type": "future"}}), "future")
        self.assertEqual(ea.CodexAdapter._change_kind({}), "改动")


class StallClock(unittest.TestCase):
    """卡住的探测看 ctx["_line_at"],而 app-server 那条路此前从不刷新它。

    结果:回合开始 STALL_SECONDS 之后 gap 只增不减,session.stalled 每 5 秒发一次,
    界面一直停在「等待中」,秒数还是从回合开始算的;只有某条命令正在执行时 gap 被强制
    归零,才短暂显示真状态(2026-08-10 实见:大部分时候都是「等待中」)。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.CodexAdapter(self.tmp.name, self.tmp.name, Recorder())
        self.ad._set_engine_sid = lambda *a: None
        self.ctx = {"sid": "s", "msg_id": "m", "parts": {}, "order": [],
                    "last_tool": None, "t0": 0, "_line_at": 0}

    def tearDown(self):
        self.tmp.cleanup()

    def test_任何通知都刷新计时(self):
        for method, params in (
            ("thread/tokenUsage/updated", {"tokenUsage": {}}),
            ("item/agentMessage/delta", {"itemId": "x", "delta": "字"}),
            ("model/rerouted", {"toModel": "m"}),
        ):
            self.ctx["_line_at"] = 0
            self.ad._app_event(method, params, self.ctx)
            self.assertGreater(self.ctx["_line_at"], 0, method)

    def test_不认识的通知也算还在动(self):
        self.ad._app_event("future/notification", {}, self.ctx)
        self.assertGreater(self.ctx["_line_at"], 0)


class Rollout(unittest.TestCase):
    """重建时耗时不能丢。

    回合跑完时 _finish_turn 落过一次 ms,但存储会被 import_session 整份覆盖,于是重启之后
    「已处理」后面那个时间就没了(2026-08-10 实见)。rollout 的 task_complete 自己记着这一笔
    (duration_ms),取回来即可,不必另算。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.CodexAdapter(self.tmp.name, self.tmp.name, Recorder())

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rows):
        p = os.path.join(self.tmp.name, "rollout.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return p

    def _rows(self, dur):
        msg = lambda role, text: {
            "timestamp": "2026-08-10T04:00:0%d.000Z" % (1 if role == "user" else 2),
            "type": "response_item",
            "payload": {"type": "message", "role": role, "content": [{"text": text}]}}
        rows = [msg("user", "做事"),
                dict(msg("assistant", "做完了"), payload={
                    "type": "message", "role": "assistant", "phase": "final_answer",
                    "content": [{"text": "做完了"}]})]
        if dur is not None:
            rows.append({"timestamp": "2026-08-10T04:00:03.000Z", "type": "event_msg",
                         "payload": {"type": "task_complete", "duration_ms": dur}})
        rows.append({"timestamp": "2026-08-10T04:00:04.000Z", "type": "response_item",
                     "payload": {"type": "message", "role": "user", "content": [{"text": "下一句"}]}})
        return rows

    def _assistant(self, dur):
        msgs = self.ad._group_rollout(self._write(self._rows(dur)), "adp_x") or []
        return [m for m in msgs if m["info"]["role"] == "assistant"]

    def test_耗时随重建带回来(self):
        a = self._assistant(52382)
        self.assertTrue(a)
        self.assertEqual(a[0]["info"]["ms"], 52382)

    def test_没有这一笔就不编(self):
        self.assertIsNone(self._assistant(None)[0]["info"].get("ms"))

    def test_耗时不串到下一个回合(self):
        rows = self._rows(1234)
        rows += [{"timestamp": "2026-08-10T04:00:05.000Z", "type": "response_item",
                  "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                              "content": [{"text": "第二次"}]}}]
        msgs = self.ad._group_rollout(self._write(rows), "adp_x") or []
        a = [m for m in msgs if m["info"]["role"] == "assistant"]
        self.assertEqual(a[0]["info"].get("ms"), 1234)
        self.assertIsNone(a[1]["info"].get("ms"))


class AbnormalTurnIsLogged(unittest.TestCase):
    """回合异常收尾的说明此前只经 SSE 发一次:页面没开、或那一帧没被渲染,它就没了,
    事后只能从「助手消息以工具块收尾、ms 为空」反推。排障要的恰恰是这句原话。"""

    def test_异常说明进日志(self):
        tmp = tempfile.TemporaryDirectory()
        bus = Recorder()
        ad = ea.CodexAdapter(tmp.name, tmp.name, bus)
        import deskcfg
        seen = []
        real, deskcfg.log = deskcfg.log, lambda tag, msg: seen.append((tag, msg))
        try:
            ctx = {"sid": "adp_1", "msg_id": "m1", "parts": {}, "order": [], "t0": 0}
            ad._finish_turn("adp_1", ctx, "app-server 已退出")
        finally:
            deskcfg.log = real
            tmp.cleanup()
        self.assertEqual(seen[0][0], "codex")
        self.assertIn("app-server 已退出", seen[0][1])
        self.assertTrue(bus.of("session.error"))


class LoadBudget(unittest.TestCase):
    """开机预热与重建回合数是内存与首屏的两个旋钮,值本身要钉住。

    2026-08-10 实测:三个常年不退的 claude PTY 就占了 1.1 GB,开机预热 15 个足以在启动那
    二十秒里把内存吃满,而其中大部分当天并不会被点开;同一段会话按 50 回合重建是 2.8 MB、
    99 条消息,按 25 回合是 1.5 MB、50 条——而 hsLoad 每次打开会话都要把这一份整个取回来。
    """

    def test_各引擎的重建回合数一致(self):
        import inspect
        for cls in (ea.ClaudeAdapter, ea.CodexAdapter):
            self.assertEqual(
                inspect.signature(cls.import_session).parameters["limit"].default, 25,
                cls.__name__)

    def test_会话预热已随_tui_撤掉(self):
        """预热 resume 的是 PTY 引擎会话:开机把最近几段拉活,点开秒进。GUI 会话没有可预热的
        对象——进程按回合起、跑完就退,整段随 TUI 一起撤(2026-08-20)。留下的只有 codex
        模型目录那一份预热,它跟会话无关。"""
        src = _srcmap.ui_server_src()
        self.assertNotIn("_prewarm_sessions", src)
        self.assertNotIn("DESKTRON_PREWARM", src)
        self.assertIn("_prewarm_codex_catalog", src)


class StoreTrim(unittest.TestCase):
    """存储只是引擎记录的投影,增长要有上限。

    原来只有 import_session 在重建那一刻截断,之后每跑一回合就往文件末尾追加、再无上限,
    于是一路涨回去——2026-08-10 实测一段会话攒到 98 条消息、6.5 MB,而 hsLoad 每次打开
    都要把这一份整个取回来、解析、渲染。更早的回合并没有丢:三家的 /history 都从引擎自己的
    记录翻(claude transcript / codex rollout),这里不留第二份。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, Recorder())
        self.sid = self.ad.create_session()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _fill(self, turns):
        for i in range(turns):
            self.ad._append_msg(self.sid, {
                "info": {"id": f"msg_u_{i}", "role": "user", "sessionID": self.sid},
                "parts": [{"id": f"u{i}", "type": "text", "text": f"第 {i} 问"}]})
            self.ad._append_msg(self.sid, {
                "info": {"id": f"msg_a_{i}", "role": "assistant", "sessionID": self.sid},
                "parts": [{"id": f"a{i}", "type": "text", "text": "答"}]})

    def _turns(self):
        return [m["info"]["id"] for m in self.ad.messages(self.sid)
                if m["info"]["role"] == "user"]

    def test_只留最近那一段(self):
        self._fill(40)
        self.ad._trim_store(self.sid)
        left = self._turns()
        self.assertEqual(len(left), self.ad.STORE_TURNS)
        self.assertEqual(left[-1], "msg_a_39".replace("a", "u"))   # 末尾是最新那一轮

    def test_不从回合中间切开(self):
        """切点必须落在真人发言上,否则重建出来的头一条是半截助手消息。"""
        self._fill(40)
        self.ad._trim_store(self.sid)
        self.assertEqual(self.ad.messages(self.sid)[0]["info"]["role"], "user")

    def test_没超过上限就不动文件(self):
        self._fill(3)
        before = os.path.getmtime(self.ad._msg_path(self.sid))
        self.ad._trim_store(self.sid)
        self.assertEqual(os.path.getmtime(self.ad._msg_path(self.sid)), before)

    def test_幂等(self):
        self._fill(40)
        self.ad._trim_store(self.sid)
        first = self._turns()
        self.ad._trim_store(self.sid)
        self.assertEqual(self._turns(), first)

    def test_回合收尾时会截断(self):
        src = _srcmap.engine_adapters_src()
        m = re.search(r"def _finish_turn\(self, sid, ctx, abnormal\)[\s\S]+?\n    def ", src)
        self.assertIn("_trim_store(sid)", m.group(0))

    def test_与重建口径一致(self):
        import inspect
        self.assertEqual(
            inspect.signature(ea.ClaudeAdapter.import_session).parameters["limit"].default,
            ea.CliAdapter.STORE_TURNS)


class FollowsTheEngineRecord(unittest.TestCase):
    """在 macOS 终端里跟同一段会话聊天,GUI 要跟着更新。

    机制是现成的:hsFollow 每十秒查一次 /pulse,签名变了就 adopt 重建。但 pulse 此前只有
    claude 实现,codex 拿到空对象,前端在 `if (!st.size) return` 就退了——
    codex 的 GUI 完全不跟进(2026-08-10 实见)。
    """

    def test_各引擎都实现了_pulse(self):
        """要防的是「从基类白捡一个空实现」。2026-08-27 分拆后实现落在各引擎自己的
        mixin 里(不再在类本体 __dict__),断言改成:解析出来的函数不是基类那一份。"""
        base = getattr(ea.CliAdapter, "transcript_stat", None)
        for cls in (ea.ClaudeAdapter, ea.CodexAdapter):
            fn = getattr(cls, "transcript_stat", None)
            self.assertIsNotNone(fn, cls.__name__)
            self.assertIsNot(fn, base, cls.__name__ + " 不该拿基类的实现顶数")

    def test_codex_读_rollout_的大小与时刻(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            ad = ea.CodexAdapter(tmp.name, tmp.name, Recorder())
            path = os.path.join(tmp.name, "rollout.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write("x" * 100)
            ad._rollout_path = lambda esid: path
            ad.index["s1"] = {"engine_sid": "019ee670-40e5-7972-abaf-b945ef812bb9"}
            st = ad.transcript_stat("s1")
            self.assertEqual(st["size"], 100)
            self.assertGreater(st["mtime"], 0)
            # 签名要随内容变,否则前端比对不出「那边有新东西」
            with open(path, "a", encoding="utf-8") as f:
                f.write("y" * 50)
            self.assertEqual(ad.transcript_stat("s1")["size"], 150)
        finally:
            tmp.cleanup()

    def test_没有引擎锚点时给空对象(self):
        """前端据此退出:没有锚点的会话没什么可跟进的。"""
        tmp = tempfile.TemporaryDirectory()
        try:
            for cls in (ea.ClaudeAdapter, ea.CodexAdapter):
                ad = cls(tmp.name, tmp.name, Recorder())
                self.assertEqual(ad.transcript_stat("查无此会话"), {})
        finally:
            tmp.cleanup()


class AdoptDomain(unittest.TestCase):
    """搬迁进来的会话要带着归属出生。

    原来 import_session 一律 create_session() 不带参数,索引里的 domain 恒为空——本机 42 段
    claude 会话只有 1 段有归属。凡是要按会话查工作流的地方(输入框上沿那条任务栏)就全落空。
    """

    def _adapter(self, meta):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fake = type(sys)("term")
        fake._term_meta_read = lambda: meta
        patch = mock.patch.dict(sys.modules, {"term": fake})
        patch.start()
        self.addCleanup(patch.stop)
        ad = ea.ClaudeAdapter(tmp.name, tmp.name, Recorder())
        return ad, ad.adopt_domain

    def test_归属抄自会话表登记的那一份(self):
        ad, adopt = self._adapter({"uuid-1": {"domain": "desk-iter"}})
        self.assertEqual(adopt("uuid-1"), "desk-iter")
        self.assertEqual(ad.create_session(adopt("uuid-1"))["domain"], "desk-iter")

    def test_归属为空时退到第一个引用它的工作流(self):
        _, adopt = self._adapter({"uuid-1": {"domain": "", "refs": ["desk-iter", "other"]}})
        self.assertEqual(adopt("uuid-1"), "desk-iter")

    def test_查无此会话给空串_不编一个归属出来(self):
        _, adopt = self._adapter({})
        self.assertEqual(adopt("uuid-1"), "")
        self.assertEqual(adopt(""), "")


if __name__ == "__main__":
    unittest.main()
