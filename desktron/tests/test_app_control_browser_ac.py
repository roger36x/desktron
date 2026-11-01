"""浏览器 App Control 的 A/C 两档补齐(2026-09-04)。

A:标签视图不在时租约当场收口、导航与标签动作、按条件等待、选择器可复用、真实按键;
C:层级化可访问性观察(shadow root / 同源 iframe)、坐标点击兜底、系统弹窗与上传下载交给人、
可复用序列固化成 Skill。这里钉的是 Runtime、API、CLI、清单与原生桥的契约。
"""
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
if "deskcfg" not in sys.modules:
    sys.argv = ["test", os.path.join(ROOT, "frontend"), "0", "0", tempfile.mkdtemp()]

from app_control import api                      # noqa: E402
from app_control.capability import (issue as issue_session_capability,  # noqa: E402
                                    register_process as register_session_process)
from app_control.manifest import validate_agent_control   # noqa: E402
from app_control.runtime import AppControlError, AppControlRuntime   # noqa: E402

with open(os.path.join(ROOT, "frontend", "workbench.json"), encoding="utf-8") as f:
    BROWSER = next(a for a in json.load(f)["desktopApps"] if a["id"] == "browser")
APPS = [BROWSER]
SWIFT = open(os.path.join(ROOT, "desktop", "native", "main.swift"), encoding="utf-8").read()
SKILL = open(os.path.join(ROOT, ".claude", "skills", "app-control", "SKILL.md"), encoding="utf-8").read()


def load_cli():
    path = os.path.join(ROOT, "desktron", "bin", "app-control")
    loader = importlib.machinery.SourceFileLoader("app_control_cli", path)
    spec = importlib.util.spec_from_loader("app_control_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = [1000.0]
        self.rt = AppControlRuntime(os.path.join(self.tmp.name, "rt.sqlite3"),
                                    apps_provider=lambda: APPS, clock=lambda: self.now[0],
                                    evidence_dir=os.path.join(self.tmp.name, "ev"))
        self.rt.NOTIFY_DELAY_S = 10 ** 6
        self.rt.offer("browser", "tab-1", "w1", session_id="s1")
        self.lease = self.rt.claim("s1", "browser", "tab-1")["id"]

    def tearDown(self):
        self.rt.close(); self.tmp.cleanup()

    def run_op(self, op, ok=True, output=None, error="", version=""):
        """把一条动作走完:批准(若需要)→ 宿主取活 → 回写结果。"""
        if op["status"] == "waiting_user":
            op = self.rt.approve(op["id"], True, "ui_user")
        nxt = self.rt.next_operation("browser")
        self.assertIsNotNone(nxt); self.assertEqual(nxt["id"], op["id"])
        return self.rt.complete(op["id"], ok, output, error, version, nxt.get('dispatch_id', ''))

    def observe(self, title="发布管理", url="https://preview.test/release"):
        op = self.rt.inspect(self.lease, "s1")
        return self.run_op(op, output={"schema": "desk.browser.document/v1", "title": title,
                                       "url": url, "nodes": [], "handoff": {
                                           "current_step": "第 2 步", "unsaved": True,
                                           "submission_status": "unknown", "ticket_id": ""}}, version="1:7")


class 租约与标签(Base):
    def test_标签视图不在就当场撤租约并说明原因(self):
        """历史上 38/385 次失败都是 browser_tab_unavailable,而且同一租约会反复拿到它。"""
        op = self.rt.inspect(self.lease, "s1")
        self.run_op(op, ok=False, error="browser_tab_unavailable")
        with self.assertRaises(AppControlError) as ctx:
            self.rt.inspect(self.lease, "s1")
        self.assertEqual(ctx.exception.code, 409)
        self.assertIn("browser_tab_unavailable", str(ctx.exception))
        self.assertIn("重新 open", str(ctx.exception))

    def test_资源按会话可发现且带页面信息(self):
        self.observe()
        rows = self.rt.resources("s1")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["title"], rows[0]["url"], rows[0]["observation_version"]),
                         ("发布管理", "https://preview.test/release", "1:7"))
        self.assertEqual(rows[0]["handoff"], {"current_step": "第 2 步", "unsaved": True,
                                               "submission_status": "unknown", "ticket_id": ""})
        self.assertEqual(self.rt.resources("someone-else"), [])
        with self.assertRaises(AppControlError):
            self.rt.resources("")

    def test_待批账本附上页面归属(self):
        """条目要说清「在哪一页上做什么」,click 的入参里只有 node_id。"""
        self.observe()
        op = self.rt.act(self.lease, "s1", "click", {"selector": "#go"}, "1:7", "k1")
        pend = self.rt.pending_all()
        self.assertEqual(pend[0]["id"], op["id"])
        self.assertEqual((pend[0]["page_title"], pend[0]["page_url"]),
                         ("发布管理", "https://preview.test/release"))
        self.assertEqual(self.rt.approval_history()[0]["page_title"], "发布管理")

    def test_飞书文案认得新动作(self):
        R = AppControlRuntime
        self.assertEqual(R.__new__(R)._ask_label({"kind": "act", "name": "type"}), "在页面上输入文字")
        self.assertEqual(R.__new__(R)._ask_label({"kind": "act", "name": "close"}), "关闭这个标签")
        self.assertEqual(R.__new__(R)._ask_label({"kind": "act", "name": "drag"}), "拖动页面元素")


class 动作校验(Base):
    def test_节点动作必须有目标(self):
        for name in ("click", "fill", "select", "hover", "dblclick", "check", "uncheck",
                     "focus", "blur"):
            with self.assertRaises(AppControlError, msg=name) as ctx:
                self.rt.act(self.lease, "s1", name,
                            {"value": "x"} if name in ("fill", "select") else {}, "", "k-" + name)
            self.assertEqual(ctx.exception.code, 422)

    def test_拖拽两端都必须有稳定目标(self):
        with self.assertRaises(AppControlError) as ctx:
            self.rt.act(self.lease, "s1", "drag", {"source": {"selector": "#from"},
                                                     "target": {}}, "", "drag-1")
        self.assertIn("target 必须提供", str(ctx.exception))
        op = self.rt.act(self.lease, "s1", "drag", {
            "source": {"locator": {"text": "待处理"}},
            "target": {"selector": "#done"}}, "", "drag-2")
        self.assertEqual(op["status"], "waiting_user")

    def test_选择器可以代替一次性节点(self):
        op = self.rt.act(self.lease, "s1", "fill", {"selector": "#ver", "value": "6.4"}, "", "k1")
        self.assertIn(op["status"], ("queued", "waiting_user"))
        self.assertEqual(op["input"]["selector"], "#ver")

    def test_下拉支持单选与多选但不能混用(self):
        with self.assertRaises(AppControlError) as ctx:
            self.rt.act(self.lease, "s1", "select", {"selector": "#team"}, "", "select-1")
        self.assertIn("必须且只能提供", str(ctx.exception))
        with self.assertRaises(AppControlError):
            self.rt.act(self.lease, "s1", "select",
                        {"selector": "#team", "value": "A", "values": ["B"]}, "", "select-2")
        op = self.rt.act(self.lease, "s1", "select",
                         {"selector": "#team", "values": ["A", "B"]}, "", "select-3")
        self.assertEqual(op["input"]["values"], ["A", "B"])

    def test_等待状态必须绑定目标(self):
        with self.assertRaises(AppControlError) as ctx:
            self.rt.act(self.lease, "s1", "wait", {"state": "visible"}, "", "wait-1")
        self.assertIn("必须提供 selector 或 locator", str(ctx.exception))
        op = self.rt.act(self.lease, "s1", "wait",
                         {"locator": {"role": "button", "name": "发布"}, "state": "enabled"},
                         "", "wait-2")
        self.assertEqual(op["input"]["state"], "enabled")

    def test_locator可以作为稳定语义目标(self):
        locator = {"role": "button", "name": "发布", "exact": True}
        op = self.rt.act(self.lease, "s1", "click", {"locator": locator, "timeout_ms": 12000}, "", "k1")
        self.assertEqual(op["input"]["locator"], locator)
        with self.assertRaises(AppControlError) as ctx:
            self.rt.act(self.lease, "s1", "click", {"locator": {"exact": True}}, "", "k2")
        self.assertIn("locator 至少提供", str(ctx.exception))

    def test_坐标点击必须绑定最近一次观察(self):
        with self.assertRaises(AppControlError) as ctx:
            self.rt.act(self.lease, "s1", "coordinate_click", {"x": 10, "y": 20}, "", "k1")
        self.assertEqual(ctx.exception.code, 422)
        self.observe()
        op = self.rt.act(self.lease, "s1", "coordinate_click", {"x": 10, "y": 20}, "1:7", "k2")
        self.assertEqual(op["status"], "waiting_user")   # external_write 一律要人点头

    def test_观察参数按清单校验(self):
        with self.assertRaises(AppControlError) as ctx:
            self.rt.inspect(self.lease, "s1", "document", params={"bogus": 1})
        self.assertEqual(ctx.exception.code, 422)
        with self.assertRaises(AppControlError):
            self.rt.inspect(self.lease, "s1", "document", params={"max_nodes": 0})
        op = self.rt.inspect(self.lease, "s1", "document", params={"find": "发布"}, settle_ms=0)
        self.assertEqual(op["input"]["find"], "发布")
        self.assertEqual(op["input"]["settle_ms"], 0.0)
        net = self.rt.inspect(self.lease, "s1", "network",
                              params={"limit": 50, "initiator": "fetch", "since_ms": 10})
        self.assertEqual(net["input"]["limit"], 50)
        shot = self.rt.inspect(self.lease, "s1", "screenshot",
                               params={"clip": {"x": 1, "y": 2, "width": 300, "height": 200}})
        self.assertEqual(shot["input"]["clip"]["width"], 300)
        with self.assertRaises(AppControlError):
            self.rt.inspect(self.lease, "s1", "screenshot",
                            params={"full_page": True,
                                    "clip": {"x": 0, "y": 0, "width": 10, "height": 10}})

    def test_Playwright常用控件动作已声明且风险不降级(self):
        acts = {a["name"]: a for a in BROWSER["agent_control"]["actions"]}
        self.assertEqual(validate_agent_control(BROWSER["agent_control"]), [])
        self.assertEqual(acts["type"]["effect"], "reversible_write")
        self.assertIn("text", acts["type"]["input_schema"]["required"])
        self.assertEqual(acts["hover"]["effect"], "reversible_write")
        for name in ("fill", "select", "scroll", "type", "hover", "click", "press",
                     "dblclick", "check", "uncheck", "focus", "blur"):
            props = acts[name]["input_schema"]["properties"]
            self.assertIn("locator", props, name)
            self.assertIn("timeout_ms", props, name)
            self.assertEqual(set(props["locator"]["properties"]),
                             {"selector", "role", "name", "text", "test_id", "exact", "nth"})
        self.assertEqual((acts["close"]["effect"], acts["close"]["approval"]), ("destructive", "always"))
        self.assertEqual((acts["handoff"]["effect"], acts["handoff"]["approval"],
                          acts["handoff"]["idempotency"]),
                         ("reversible_write", "always", "required"))
        handoff = self.rt.act(self.lease, "s1", "handoff", {}, "", "handoff-1")
        self.assertEqual(handoff["status"], "waiting_user")
        self.assertEqual((acts["coordinate_click"]["effect"], acts["coordinate_click"]["approval"],
                          acts["coordinate_click"]["idempotency"]), ("external_write", "always", "required"))
        for name in ("dblclick", "check", "uncheck", "drag"):
            self.assertEqual((acts[name]["effect"], acts[name]["approval"], acts[name]["idempotency"]),
                             ("external_write", "always", "required"))
        self.assertEqual(set(acts["drag"]["input_schema"]["required"]), {"source", "target"})
        for name in ("back", "forward", "reload"):
            self.assertEqual(acts[name]["approval"], "never")
        wait = acts["wait"]["input_schema"]["properties"]
        for key in ("selector", "locator", "state", "url", "url_mode", "network_idle", "timeout_ms"):
            self.assertIn(key, wait)
        self.assertEqual(wait["state"]["enum"],
                         ["attached", "detached", "visible", "hidden", "enabled", "disabled",
                          "checked", "unchecked", "mixed"])
        self.assertEqual(wait["values"]["items"]["type"], "string")
        self.assertEqual(acts["select"]["input_schema"]["properties"]["values"]["minItems"], 1)

    def test_观察的input_schema必须是对象(self):
        control = json.loads(json.dumps(BROWSER["agent_control"]))
        control["observations"][0]["input_schema"] = "nope"
        self.assertTrue(any("input_schema" in e for e in validate_agent_control(control)))


class 序列固化(Base):
    def test_只收可移植的成功动作(self):
        self.observe()
        # 时钟不动:同一 created_at 的动作也必须按入队顺序固化
        self.run_op(self.rt.act(self.lease, "s1", "fill", {"selector": "#ver", "value": "6.4"}, "1:7", "k1"))
        self.run_op(self.rt.act(self.lease, "s1", "select", {"selector": "#teams", "values": ["A", "B"]},
                                "1:7", "k1b"))
        self.run_op(self.rt.act(self.lease, "s1", "click", {"node_id": "1:3"}, "", "k2"))
        self.run_op(self.rt.act(self.lease, "s1", "click", {"selector": "#submit"}, "", "k3"))
        self.run_op(self.rt.act(self.lease, "s1", "click",
                                {"locator": {"role": "button", "name": "确认"}}, "", "k3b"))
        self.run_op(self.rt.act(self.lease, "s1", "handoff", {}, "", "handoff"))
        self.run_op(self.rt.act(self.lease, "s1", "wait", {"selector": ".done"}, "", "k4"))
        failed = self.rt.act(self.lease, "s1", "click", {"selector": "#never"}, "", "k5")
        self.run_op(failed, ok=False, error="selector_not_found")
        seq = self.rt.successful_sequence("s1")
        self.assertEqual(seq["schema"], "desk.app-control.sequence/v1")
        self.assertEqual([s["action"] for s in seq["steps"]], ["click", "click", "wait"])
        self.assertEqual(seq["steps"][0]["input"], {"selector": "#submit"})
        self.assertEqual(seq["steps"][1]["input"]["locator"]["name"], "确认")

    def test_CLI校验sequence并固化成Skill(self):
        cli = load_cli()
        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            json.dump({"schema": "desk.app-control.sequence/v1",
                       "steps": [{"action": "click", "input": {"node_id": "1:3"}}]}, f)
        with self.assertRaises(RuntimeError) as ctx:
            cli.read_sequence(bad)
        self.assertIn("selector", str(ctx.exception))
        good = os.path.join(self.tmp.name, "good.json")
        with open(good, "w", encoding="utf-8") as f:
            json.dump({"schema": "desk.app-control.sequence/v1",
                       "steps": [{"action": "click", "input": {"selector": "#submit"}}]}, f)

        class Args:
            file, name, output = good, "release-check", os.path.join(self.tmp.name, "skills")
        out = cli.export_sequence_skill(Args)
        self.assertEqual(out["steps"], 1)
        self.assertTrue(os.path.exists(os.path.join(Args.output, "release-check", "SKILL.md")))
        self.assertTrue(os.path.exists(os.path.join(Args.output, "release-check", "sequence.json")))
        Args.name = "../escape"
        with self.assertRaises(RuntimeError):
            cli.export_sequence_skill(Args)
        Args.name = "Bad Name"
        with self.assertRaises(RuntimeError):
            cli.export_sequence_skill(Args)

    def test_CLI有tabs与sequence子命令(self):
        src = open(os.path.join(ROOT, "desktron", "bin", "app-control"), encoding="utf-8").read()
        for k in ('sub.add_parser("tabs"', 'sub.add_parser("sequence"', 'seq.add_parser("capture"',
                  'seq.add_parser("run"', 'seq.add_parser("skill"', 'inspect.add_argument("--input"',
                  'sub.add_parser("trace"'):
            self.assertIn(k, src)


class 失败诊断(Base):
    def test_trace限定会话并隐藏输入值(self):
        self.observe()
        op = self.rt.act(self.lease, "s1", "fill", {"selector": "#token", "value": "secret"},
                         "1:7", "trace-1")
        self.run_op(op, ok=False, output={"diagnostic": {"last_error": "element_occluded"},
                                         "private": "omit"}, error="element_occluded")
        trace = self.rt.trace(op["id"], "s1")
        self.assertEqual(trace["operation"]["input"]["value"], "[redacted]")
        self.assertNotIn("private", trace["output"])
        self.assertEqual(trace["page"]["title"], "发布管理")
        self.assertTrue(trace["timeline"])
        select = self.rt.act(self.lease, "s1", "select",
                             {"selector": "#team", "values": ["secret-a", "secret-b"]},
                             "1:7", "trace-2")
        self.assertEqual(self.rt.trace(select["id"], "s1")["operation"]["input"]["values"], "[redacted]")
        with self.assertRaises(AppControlError):
            self.rt.trace(op["id"], "someone-else")


class 接口(Base):
    def setUp(self):
        super().setUp()
        self.old = api.runtime
        api.runtime = lambda: self.rt

    def tearDown(self):
        api.runtime = self.old
        super().tearDown()

    def call(self, path, method="GET", body=None):
        class H:
            def __init__(s):
                raw = json.dumps(body or {}).encode()
                s.rfile = io.BytesIO(raw); s.headers = {
                    "Content-Length": str(len(raw)),
                    api.SESSION_HEADER: issue_session_capability("s1"),
                }
                s.result = None; s.code = None
                s.app_control_peer_pid = os.getpid()
            def _send_json(s, value, code=200): s.result, s.code = value, code
        class CurrentProcess:
            pid = os.getpid()
            @staticmethod
            def poll(): return None
        register_session_process("s1", CurrentProcess())
        h = H()
        clean, _, query = path.partition("?")
        api.handle(h, clean, query, method)
        return h.code, h.result

    def test_资源与序列端点(self):
        self.observe()
        code, out = self.call("/workbench/app-control/resources?session_id=s1&app_id=browser")
        self.assertEqual(code, 200)
        self.assertEqual(out["resources"][0]["title"], "发布管理")
        code, out = self.call("/workbench/app-control/sequence?session_id=s1")
        self.assertEqual(code, 200)
        self.assertEqual(out["schema"], "desk.app-control.sequence/v1")

    def test_inspect端点透传观察参数(self):
        code, out = self.call("/workbench/app-control/inspect", "POST", {
            "lease_id": self.lease, "session_id": "s1", "observation": "document",
            "input": {"find": "发布"}, "settle_ms": 0})
        self.assertEqual(code, 202)
        self.assertEqual(out["input"]["find"], "发布")

    def test_trace端点按会话返回诊断(self):
        op = self.rt.act(self.lease, "s1", "hover", {"selector": "#menu"}, "", "trace-api")
        self.run_op(op, ok=False, output={"diagnostic": {"attempts": 4}}, error="element_occluded")
        code, out = self.call("/workbench/app-control/trace/%s?session_id=s1" % op["id"])
        self.assertEqual(code, 200)
        self.assertEqual(out["output"]["diagnostic"]["attempts"], 4)


class 原生桥(unittest.TestCase):
    def test_click与hover送真实鼠标(self):
        seg = SWIFT[SWIFT.index("private func executeAppControl"):SWIFT.index("private func waitForPageSettled")]
        self.assertIn('["click", "coordinate_click", "hover", "dblclick", "check", "uncheck"].contains(name)', seg)
        self.assertIn("runNativePointer(", seg)
        run = SWIFT[SWIFT.index("private func runNativePointer"):SWIFT.index("private func nativeKeyEvents")]
        self.assertIn('"name": "prepare_pointer"', run)
        self.assertIn("view.mouseMoved(with: moved)", run)
        self.assertIn("view.mouseDown(with: down); view.mouseUp(with: up)", run)
        self.assertIn('settleInput["input_path"] = "native_pointer"', run)

    def test_拖拽双击与勾选走真实鼠标(self):
        self.assertIn("private func runNativeDrag", SWIFT)
        drag = SWIFT[SWIFT.index("private func runNativeDrag"):SWIFT.index("private func nativeKeyEvents")]
        for k in ('"name": "prepare_drag"', "view.mouseDragged(with: dragged)",
                  '"input_path": "native_drag"'):
            self.assertIn(k, drag)
        for k in ("name == \"dblclick\" ? 2 : 1", '"desired_checked"', "element_not_checkable",
                  "radio_cannot_uncheck", "check_state_not_reached"):
            self.assertIn(k, SWIFT)

    def test_press与type送真实按键(self):
        self.assertIn("private func nativeKeyEvents(name: String, input: [String: Any])", SWIFT)
        self.assertIn("view.keyDown(with: down); view.keyUp(with: up)", SWIFT)
        seg = SWIFT[SWIFT.index("private func executeAppControl"):SWIFT.index("private func waitForPageSettled")]
        self.assertIn('name == "press" || name == "type"', seg)
        self.assertIn("runNativeInput(", seg)

    def test_真实按键不被当成人在操作(self):
        seg = SWIFT[SWIFT.index('if message.name == "appControlUser"'):]
        seg = seg[:seg.index("return") + 6]
        # 判据要在 pause 之前、作为同一个条件的一部分:送键那一小段里的交互不暂停租约
        self.assertLess(seg.index("appControlNativeInputUntil[id]"),
                        seg.index('pauseAppControl(id: id, reason: "user_interaction")'))
        run = SWIFT[SWIFT.index("private func runNativeInput"):SWIFT.index("private func nativeKeyEvents")]
        # 先落焦点,再送键,再回页面看变化
        self.assertLess(run.index('"name": "focus"'), run.index("view.keyDown(with: down)"))
        self.assertLess(run.index("view.keyDown(with: down)"), run.index('"name": "settle"'))
        self.assertIn("appControlNativeInputUntil[resourceId] = Date()", run)

    def test_页面脚本有焦点与安定命令(self):
        for k in ("command.name === 'focus'", "command.name === 'blur'", "command.name === 'settle'", "command.name === 'type'",
                  "command.name === 'hover'", "requires_reinspect", "modal_stack", "opaque_frames",
                  "shadow_root", "frame_id", "'wait_timeout'", "deepQuery(", "deepElementFromPoint("):
            self.assertIn(k, SWIFT, k)

    def test_locator自动等待可操作并追踪网络空闲(self):
        for k in ("locatorMatches", "locator_ambiguous", "locator_timeout", "element_not_visible",
                  "element_disabled", "element_not_stable", "element_occluded", "waitForTarget",
                  "fetch_xhr_idle_and_dom_quiet_500ms", "data-desk-network-pending"):
            self.assertIn(k, SWIFT, k)
        self.assertNotIn("el.dispatchEvent(new EventType", SWIFT)

    def test_条件等待与多选由页面桥执行(self):
        for k in ("targetState === 'detached'", "targetState === 'visible'", "targetState === 'hidden'",
                  "targetState === 'enabled'", "targetState === 'disabled'", "target_matched",
                  "select_target_invalid", "select_multiple_not_allowed", "select_option_not_found",
                  "selected_count"):
            self.assertIn(k, SWIFT, k)

    def test_自定义控件状态与遮挡诊断由页面桥提供(self):
        for k in ("associated_control", "state_sources", "selected_values", "targetState === 'checked'",
                  "targetState === 'unchecked'", "menu_stack", "popover_stack", "visible_internal",
                  "clickable_region", "candidate_points", "occluder", "effect_verified", "delivery_only"):
            self.assertIn(k, SWIFT, k)
        for k in ("handoffState", "submission_status: 'unknown'"):
            self.assertIn(k, SWIFT, k)
        self.assertIn('d["resource_id"] = resourceId', SWIFT)

    def test_locator可按序号消除歧义(self):
        for action in BROWSER["agent_control"]["actions"]:
            locator = action.get("input_schema", {}).get("properties", {}).get("locator")
            if locator:
                self.assertEqual(locator["properties"]["nth"]["type"], "integer", action["name"])
        self.assertIn("Number.isInteger(locator.nth)", SWIFT)
        self.assertIn("candidates.length + locator.nth", SWIFT)

    def test_网络观察只返回脱敏资源元数据(self):
        for k in ("desk.browser.network/v1", "safeResourceUrl", "query_fragment_headers_bodies_omitted",
                  "getEntriesByType?.('resource')", "transfer_size", "response_bodies: 'unavailable'"):
            self.assertIn(k, SWIFT, k)
        network = next(x for x in BROWSER["agent_control"]["observations"] if x["name"] == "network")
        self.assertEqual(network["redaction"], "origin-path-only")
        self.assertEqual(set(network["input_schema"]["properties"]),
                         {"limit", "since_ms", "initiator", "url_contains"})

    def test_截图支持整页与可视区裁剪(self):
        shot = next(x for x in BROWSER["agent_control"]["observations"] if x["name"] == "screenshot")
        self.assertEqual(set(shot["input_schema"]["properties"]), {"full_page", "clip"})
        for k in ("WKSnapshotConfiguration", "screenshot_clip_outside_viewport", "content_width",
                  "content_height", "maxPixels", '"full_page": fullPage', '"truncated": truncated'):
            self.assertIn(k, SWIFT, k)

    def test_弹窗上传下载交给人(self):
        self.assertIn("WKDownloadDelegate", SWIFT)
        for reason in ("javascript_dialog", "file_upload", "file_download"):
            self.assertIn('reason: "%s"' % reason, SWIFT)
        self.assertIn("javascript_dialogs: 'needs_user'", SWIFT)
        # 人处理完要接回,不能把租约永远停在「暂停」
        seg = SWIFT[SWIFT.index("runJavaScriptAlertPanelWithMessage"):SWIFT.index("private func appControlNeedsUser")]
        self.assertGreaterEqual(seg.count("self?.resumeAppControl(id: id)"), 4)

    def test_导航与标签动作(self):
        seg = SWIFT[SWIFT.index("private func executeAppControl"):SWIFT.index("private func waitForPageSettled")]
        self.assertIn('["back", "forward", "reload"].contains(name)', seg)
        self.assertIn('name == "close"', seg)
        self.assertIn('name == "activate"', seg)
        self.assertIn('name == "handoff"', seg)
        self.assertIn('runBrowserAgentCommand("takeover"', seg)
        # 关闭要先形成回执再销毁,否则自己会被撤租约取消
        close = seg[seg.index('name == "close"'):seg.index('name == "activate"')]
        self.assertLess(close.index("finishAppControl(opId, ok: true"), close.index("runBrowserAgentCommand"))
        browser = open(os.path.join(ROOT, "frontend", "app-browser.js"), encoding="utf-8").read()
        self.assertIn("window.__browserAgentCommand = (req) =>", browser)
        self.assertIn('req.action === "handoff"', browser)


class 技能说明(unittest.TestCase):
    def test_写清了新能力与交给人的情形(self):
        for k in ("app-control tabs", "coordinate_click", "sequence capture", "sequence run",
                  "needs_user", "browser_tab_unavailable", "network_idle", "selector", "locator",
                  "真实鼠标", "requires_reinspect", "inspect network"):
            self.assertIn(k, SKILL, k)


if __name__ == "__main__":
    unittest.main()
