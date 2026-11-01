"""App Control HTTP 边界：Agent 与原生宿主的权限不能混用。"""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
if "deskcfg" not in sys.modules:
    sys.argv = ["test", os.path.join(ROOT, "frontend"), "0", "0", tempfile.mkdtemp()]

from app_control import api
from app_control import capability as session_capability_module
from app_control.capability import (issue as issue_session_capability,
                                    register_process as register_session_process,
                                    revoke as revoke_session_capability,
                                    verify as verify_session_capability)
from app_control.runtime import AppControlRuntime
from test_app_control_runtime import APPS, DESK_APP, NATIVE_APP


class Handler:
    def __init__(self, body=None, token="", session_capability="", peer_pid=0):
        raw = json.dumps(body or {}).encode()
        self.rfile = io.BytesIO(raw)
        self.headers = {"Content-Length": str(len(raw))}
        if token:
            self.headers[api.HOST_HEADER] = token
        if session_capability:
            self.headers[api.SESSION_HEADER] = session_capability
        self.app_control_peer_pid = peer_pid
        self.result = None
        self.code = None

    def _send_json(self, value, code=200):
        self.result, self.code = value, code


class ApiBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = AppControlRuntime(os.path.join(self.tmp.name, "runtime.sqlite3"),
                                    apps_provider=lambda: APPS)
        self.old_runtime = api.runtime
        api.runtime = lambda: self.rt
        self.old_token = os.environ.get("DESKTRON_APP_CONTROL_HOST_TOKEN")
        os.environ["DESKTRON_APP_CONTROL_HOST_TOKEN"] = "host-only"
        self.session_ids = set()

    def tearDown(self):
        api.runtime = self.old_runtime
        if self.old_token is None:
            os.environ.pop("DESKTRON_APP_CONTROL_HOST_TOKEN", None)
        else:
            os.environ["DESKTRON_APP_CONTROL_HOST_TOKEN"] = self.old_token
        for sid in self.session_ids:
            revoke_session_capability(sid)
        self.rt.close()
        self.tmp.cleanup()

    def test_session_capability_is_stable_scoped_and_revocable(self):
        self.addCleanup(revoke_session_capability, "session-cap-a")
        first = issue_session_capability("session-cap-a")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        def stop(child):
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
        self.addCleanup(stop, proc)
        self.addCleanup(stop, other)
        register_session_process("session-cap-a", proc)
        self.assertEqual(issue_session_capability("session-cap-a"), first)
        self.assertTrue(verify_session_capability("session-cap-a", first, proc.pid))
        self.assertFalse(verify_session_capability("session-cap-a", first, other.pid))
        replacement = other
        register_session_process("session-cap-a", replacement)
        self.assertFalse(verify_session_capability("session-cap-a", first, proc.pid))
        self.assertTrue(verify_session_capability("session-cap-a", first, replacement.pid))
        replacement.terminate(); replacement.wait(timeout=2)
        self.assertFalse(verify_session_capability("session-cap-a", first, replacement.pid))
        self.assertFalse(verify_session_capability("session-cap-b", first, proc.pid))
        revoke_session_capability("session-cap-a")
        self.assertFalse(verify_session_capability("session-cap-a", first, proc.pid))
        self.assertNotEqual(issue_session_capability("session-cap-a"), first)

    def test_process_replacement_during_verify_rejects_old_request(self):
        sid = "session-cap-replace-race"
        token = issue_session_capability(sid)
        self.addCleanup(revoke_session_capability, sid)
        old = mock.Mock(pid=41001); old.poll.return_value = None
        new = mock.Mock(pid=41002); new.poll.return_value = None
        register_session_process(sid, old)
        entered, release, result = threading.Event(), threading.Event(), []

        def descendants(_peer, _roots):
            entered.set(); release.wait(2); return True

        with mock.patch.object(session_capability_module, "_descends_from", descendants):
            thread = threading.Thread(
                target=lambda: result.append(verify_session_capability(sid, token, 42001)))
            thread.start(); self.assertTrue(entered.wait(1))
            register_session_process(sid, new)
            release.set(); thread.join(2)
        self.assertEqual(result, [False])

    def test_revoke_during_verify_rejects_inflight_request(self):
        sid = "session-cap-revoke-race"
        token = issue_session_capability(sid)
        proc = mock.Mock(pid=43001); proc.poll.return_value = None
        register_session_process(sid, proc)
        entered, release, result = threading.Event(), threading.Event(), []

        def descendants(_peer, _roots):
            entered.set(); release.wait(2); return True

        with mock.patch.object(session_capability_module, "_descends_from", descendants):
            thread = threading.Thread(
                target=lambda: result.append(verify_session_capability(sid, token, 44001)))
            thread.start(); self.assertTrue(entered.wait(1))
            revoke_session_capability(sid)
            release.set(); thread.join(2)
        self.assertEqual(result, [False])

    def call(self, path, method="GET", body=None, token="", session_capability=None,
             peer_pid=None):
        _, _, query = path.partition("?")
        params = urllib.parse.parse_qs(query)
        sid = str((body or {}).get("session_id") or
                  (params.get("session_id") or [""])[0])
        capability = issue_session_capability(sid) if session_capability is None and sid else (
            session_capability or "")
        if sid and peer_pid is None:
            class CurrentProcess:
                pid = os.getpid()
                @staticmethod
                def poll(): return None
            register_session_process(sid, CurrentProcess())
            self.session_ids.add(sid)
            peer_pid = os.getpid()
        h = Handler(body, token, capability, int(peer_pid or 0))
        clean, _, query = path.partition("?")
        api.handle(h, clean, query, method)
        return h.code, h.result

    def test_agent_can_discover_but_cannot_create_delegation(self):
        code, result = self.call(api.PREFIX)
        self.assertEqual(code, 200)
        self.assertEqual(result["apps"][0]["id"], "browser")
        code, result = self.call(api.PREFIX + "/host/delegate", "POST", {
            "app_id": "browser", "resource_id": "tab-1", "window_id": "window-1"})
        self.assertEqual(code, 403)
        self.assertIn("原生宿主", result["error"])

    def test_desktop_status_is_readable_but_only_host_can_report_or_issue_ticket(self):
        code, status = self.call(api.PREFIX + "/desktop/status?session_id=session-status")
        self.assertEqual(code, 200)
        self.assertEqual(status["execution"]["status"], "unavailable")
        report = {"host_instance_id": os.environ.get("DESKTRON_HOST_INSTANCE_ID") or "host-one",
                  "host_bundle_id": "io.github.desktron",
                  "host_pid": os.getppid() if os.environ.get("DESKTRON_NATIVE_LAUNCH") == "1" else 1234,
                  "status": "not_trusted"}
        self.assertEqual(self.call(
            api.PREFIX + "/host/desktop-capability", "POST", report)[0], 403)
        code, reported = self.call(
            api.PREFIX + "/host/desktop-capability", "POST", report, token="host-only")
        self.assertEqual(code, 200)
        self.assertEqual(reported["capabilities"]["accessibility"]["status"], "not_trusted")

        self.rt.close()
        self.rt = AppControlRuntime(os.path.join(self.tmp.name, "native.sqlite3"),
                                    apps_provider=lambda: [*APPS, NATIVE_APP])
        body = {"app_id": "native-fixture", "bundle_id": "com.example.fixture",
                "pid": 4321, "window_id": "window-7", "allowed_actions": ["refresh"],
                "session_id": "session-native", "task_id": "task-native", "ttl_seconds": 60}
        self.assertEqual(self.call(
            api.PREFIX + "/host/desktop-tickets", "POST", body)[0], 403)
        code, issued = self.call(
            api.PREFIX + "/host/desktop-tickets", "POST", body, token="host-only")
        self.assertEqual(code, 201)
        self.assertTrue(issued["ticket"].startswith("dcap_"))
        self.assertEqual(self.rt.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)
        code, public = self.call(api.PREFIX + "/desktop/status?session_id=session-native")
        self.assertEqual(code, 200)
        self.assertNotIn("ticket", json.dumps(public))
        code, probe = self.call(
            api.PREFIX + "/probe?app_id=native-fixture&session_id=session-native")
        self.assertEqual(code, 200)
        self.assertEqual(probe["control"]["status"], "unavailable")
        self.assertEqual(probe["control"]["execution_reason"],
                         "accessibility_not_reported")
        self.assertNotEqual(probe["control"].get("action"), "delegate_visible_tab")
        code, _ = self.call(
            api.PREFIX + "/host/next?app_id=native-fixture", token="host-only")
        self.assertEqual(code, 409)
        self.assertEqual(self.rt.host_status("native-fixture")["status"], "unavailable")

    def test_native_delegation_then_agent_claim_and_inspect(self):
        code, lease = self.call(api.PREFIX + "/host/delegate", "POST", {
            "app_id": "browser", "resource_id": "tab-1", "window_id": "window-1"},
            token="host-only")
        self.assertEqual(code, 201)
        code, active = self.call(api.PREFIX + "/claim", "POST", {
            "session_id": "session-1", "app_id": "browser", "resource_id": "tab-1"})
        self.assertEqual((code, active["status"]), (200, "active"))
        code, operation = self.call(api.PREFIX + "/inspect", "POST", {
            "session_id": "session-1", "lease_id": lease["id"], "observation": "document"})
        self.assertEqual((code, operation["status"]), (202, "queued"))

    def test_agent_open_forwards_exact_main_frame_allowlist(self):
        code, operation = self.call(api.PREFIX + "/open", "POST", {
            "session_id": "session-search", "app_id": "browser",
            "url": "https://duckduckgo.com/?q=one", "isolated": True,
            "allowed_main_frame_hosts": ["duckduckgo.com"],
        })
        self.assertEqual(code, 202)
        self.assertEqual(operation["input"]["allowed_main_frame_hosts"],
                         ["duckduckgo.com"])
        code, result = self.call(api.PREFIX + "/open", "POST", {
            "session_id": "session-search", "app_id": "browser",
            "url": "https://duckduckgo.com/?q=one", "isolated": True,
            "allowed_main_frame_hosts": ["example.com"],
        })
        self.assertEqual(code, 422)
        self.assertIn("白名单", result["error"])

    def test_native_poll_marks_only_requested_host_online(self):
        self.rt.close()
        self.rt = AppControlRuntime(os.path.join(self.tmp.name, "hosts.sqlite3"),
                                    apps_provider=lambda: [*APPS, DESK_APP])
        code, offline = self.call(api.PREFIX + "/probe?app_id=desk&session_id=session-1")
        self.assertEqual(code, 200)
        self.assertEqual(offline["control"]["status"], "needs_authorization")
        self.assertEqual(offline["control"]["execution_status"], "unavailable")
        self.assertEqual(offline["control"]["execution_reason"], "host_unavailable")

        code, result = self.call(api.PREFIX + "/host/next?app_id=desk", token="host-only")
        self.assertEqual(code, 200)
        self.assertEqual(result["host"]["status"], "online")
        self.assertIsNone(result["operation"])
        code, online = self.call(api.PREFIX + "/probe?app_id=desk&session_id=session-1")
        self.assertEqual(code, 200)
        self.assertEqual(online["control"]["execution_status"], "blocked")
        self.assertEqual(online["control"]["execution_reason"],
                         "needs_authorization")
        self.assertEqual(self.rt.probe("session-1", "browser")["host"]["status"],
                         "unavailable")

    def test_anonymous_probe_reports_health_but_never_exposes_offered_lease(self):
        self.rt.offer("browser", "private-tab", "private-window")
        code, result = self.call(api.PREFIX + "/probe?app_id=browser")
        self.assertEqual(code, 200)
        self.assertNotIn("lease", result["control"])
        self.assertNotEqual(result["control"]["status"], "available_to_claim")

    def test_only_trusted_ui_can_list_and_revoke_identified_site_grant(self):
        self.rt.grant_site("browser", "https://cloud.example.net")
        sid = "session-grant-attacker"
        agent_capability = issue_session_capability(sid)
        class AgentProcess:
            pid = os.getpid()
            @staticmethod
            def poll(): return None
        register_session_process(sid, AgentProcess())
        self.session_ids.add(sid)

        # 即便调用者持有一条真实 Agent 会话能力且 peer PID 合法，也不是可信 UI。
        code, result = self.call(api.PREFIX + "/grants?app_id=browser",
                                 session_capability=agent_capability, peer_pid=os.getpid())
        self.assertEqual(code, 403)
        code, result = self.call(api.PREFIX + "/grants?app_id=browser", token="host-only")
        self.assertEqual(code, 200)
        self.assertEqual(result["grants"][0]["origin"], "https://cloud.example.net")

        code, result = self.call(api.PREFIX + "/grants", "DELETE", {
            "app_id": "browser", "origin": "https://cloud.example.net",
        }, session_capability=agent_capability, peer_pid=os.getpid())
        self.assertEqual(code, 403)
        self.assertEqual(len(self.rt.site_grants("browser")), 1)
        code, result = self.call(api.PREFIX + "/grants", "DELETE", {
            "app_id": "browser", "origin": "https://cloud.example.net",
        }, token="host-only")
        self.assertEqual(code, 200)
        self.assertTrue(result["revoked"])
        self.assertEqual(self.rt.site_grants("browser"), [])

    def test_审批决定后仍可从完整账本恢复(self):
        self.call(api.PREFIX + "/host/delegate", "POST", {
            "app_id": "browser", "resource_id": "tab-2", "window_id": "window-2",
            "session_id": "session-2"}, token="host-only")
        _, lease = self.call(api.PREFIX + "/claim", "POST", {
            "session_id": "session-2", "app_id": "browser", "resource_id": "tab-2"})
        _, op = self.call(api.PREFIX + "/act", "POST", {
            "session_id": "session-2", "lease_id": lease["id"], "action": "submit",
            "input": {}, "idempotency_key": "submit-api-1"})
        self.assertEqual(op["status"], "waiting_user")
        code, done = self.call(api.PREFIX + "/approvals/decide", "POST", {
            "operation_id": op["id"], "approved": True,
            "anchor": {"turn_id": "msg-2", "turn_seq": 2, "after_part_index": 1}},
            token="host-only")
        self.assertEqual((code, done["approval_state"]), (200, "approved:ui_user"))
        _, result = self.call(api.PREFIX + "/approvals?include_resolved=1&limit=20",
                              token="host-only")
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["approvals"][0]["id"], op["id"])
        self.assertEqual(result["approvals"][0]["turn_id"], "msg-2")

    def test_执行意图从请求透传到审批账本(self):
        self.call(api.PREFIX + "/host/delegate", "POST", {
            "app_id": "browser", "resource_id": "tab-intent",
            "window_id": "window-intent", "session_id": "session-intent"},
            token="host-only")
        _, lease = self.call(api.PREFIX + "/claim", "POST", {
            "session_id": "session-intent", "app_id": "browser",
            "resource_id": "tab-intent"})
        _, op = self.call(api.PREFIX + "/act", "POST", {
            "session_id": "session-intent", "lease_id": lease["id"],
            "action": "submit", "input": {}, "idempotency_key": "intent-api-1",
            "intent": "  提交本周值班安排  "})
        self.assertEqual(op["intent"], "提交本周值班安排")
        _, listed = self.call(api.PREFIX + "/approvals?include_resolved=1&limit=20",
                              token="host-only")
        self.assertEqual(listed["approvals"][0]["intent"], "提交本周值班安排")

    def test_清空审批历史只写信息卡标记(self):
        self.call(api.PREFIX + "/host/delegate", "POST", {
            "app_id": "browser", "resource_id": "tab-clear", "window_id": "window-clear",
            "session_id": "session-clear"}, token="host-only")
        _, lease = self.call(api.PREFIX + "/claim", "POST", {
            "session_id": "session-clear", "app_id": "browser", "resource_id": "tab-clear"})
        _, op = self.call(api.PREFIX + "/act", "POST", {
            "session_id": "session-clear", "lease_id": lease["id"], "action": "submit",
            "input": {}, "idempotency_key": "clear-api-1"})
        self.call(api.PREFIX + "/approvals/decide", "POST", {
            "operation_id": op["id"], "approved": False}, token="host-only")
        code, result = self.call(api.PREFIX + "/approvals/clear", "POST", {
            "before": 10**20}, token="host-only")
        self.assertEqual((code, result["cleared"]), (200, 1))
        _, listed = self.call(api.PREFIX + "/approvals?include_resolved=1&limit=20",
                              token="host-only")
        self.assertEqual(listed["approvals"][0]["id"], op["id"])
        self.assertIsNotNone(listed["approvals"][0]["approval_cleared_at"])

    def test_clear_legacy_cancelled_but_keep_real_pending(self):
        self.rt.offer("browser", "tab-clear", "window-clear", session_id="session-clear")
        lease = self.rt.claim("session-clear", "browser", "tab-clear")
        old = self.rt.act(lease["id"], "session-clear", "submit", {}, "", "old-cancelled")
        pending = self.rt.act(lease["id"], "session-clear", "submit", {}, "", "still-pending")
        with self.rt.db:
            self.rt.db.execute(
                "UPDATE operations SET status='cancelled',error='lease_revoked:agent_yield' WHERE id=?",
                (old["id"],))
        code, result = self.call(api.PREFIX + "/approvals/clear", "POST",
                                 {"before": 10**20}, token="host-only")
        self.assertEqual((code, result["cleared"]), (200, 1))
        _, result = self.call(api.PREFIX + "/approvals?include_resolved=1", token="host-only")
        history = {op["id"]: op for op in result["approvals"]}
        self.assertIsNotNone(history[old["id"]]["approval_cleared_at"])
        self.assertEqual(history[old["id"]]["status"], "cancelled")
        self.assertEqual(history[old["id"]]["approval_state"], "pending")
        self.assertIsNone(history[pending["id"]]["approval_cleared_at"])
        self.assertEqual([op["id"] for op in result["pending"]], [pending["id"]])

    def test_agent_cannot_list_decide_or_clear_approval_ledger(self):
        lease = self.rt.offer("browser", "tab-guard", "window-guard",
                              session_id="session-guard")
        lease = self.rt.claim("session-guard", "browser", "tab-guard")
        op = self.rt.act(lease["id"], "session-guard", "submit", {}, "", "guard-1")
        for path, method, body in (
                (api.PREFIX + "/approvals", "GET", None),
                (api.PREFIX + "/approvals/decide", "POST",
                 {"operation_id": op["id"], "approved": True}),
                (api.PREFIX + "/approvals/clear", "POST", {"before": 10**20})):
            code, _ = self.call(path, method, body)
            self.assertEqual(code, 403)
        self.assertEqual(self.rt.get_operation(op["id"])["status"], "waiting_user")

    def test_operation_routes_require_owner_session(self):
        lease = self.rt.offer("browser", "tab-owner", "window-owner",
                              session_id="session-owner")
        lease = self.rt.claim("session-owner", "browser", "tab-owner")
        op = self.rt.inspect(lease["id"], "session-owner")
        base = api.PREFIX + "/operations/" + op["id"]
        self.assertEqual(self.call(base)[0], 400)
        self.assertEqual(self.call(base + "?session_id=session-other")[0], 404)
        self.assertEqual(self.call(base + "?session_id=session-owner")[0], 200)
        with self.rt.db:
            self.rt.db.execute(
                "UPDATE operations SET status='succeeded',finished_at=? WHERE id=?",
                (self.rt.clock(), op["id"]))
        receipt = api.PREFIX + "/receipts/" + op["id"]
        self.assertEqual(self.call(receipt + "?session_id=session-other")[0], 404)
        self.assertEqual(self.call(receipt + "?session_id=session-owner")[0], 200)

    def test_known_victim_session_id_does_not_authorize_cross_session_reads(self):
        victim_capability = issue_session_capability("session-victim")
        attacker_capability = issue_session_capability("session-attacker")
        lease = self.rt.offer("browser", "tab-victim", "window-victim",
                              session_id="session-victim")
        lease = self.rt.claim("session-victim", "browser", "tab-victim")
        op = self.rt.inspect(lease["id"], "session-victim")
        self.assertNotEqual(victim_capability, attacker_capability)
        requests = [
            (api.PREFIX + "/probe?app_id=browser&session_id=session-victim", "GET", None),
            (api.PREFIX + "/resources?app_id=browser&session_id=session-victim", "GET", None),
            (api.PREFIX + "/sequence?app_id=browser&session_id=session-victim", "GET", None),
            (api.PREFIX + "/trace/%s?session_id=session-victim" % op["id"], "GET", None),
            (api.PREFIX + "/events?session_id=session-victim", "GET", None),
            (api.PREFIX + "/operations/%s?session_id=session-victim" % op["id"], "GET", None),
            (api.PREFIX + "/operations/%s/wait?session_id=session-victim&timeout=0" % op["id"], "GET", None),
            (api.PREFIX + "/receipts/%s?session_id=session-victim" % op["id"], "GET", None),
            (api.PREFIX + "/claim", "POST", {"session_id": "session-victim"}),
            (api.PREFIX + "/open", "POST", {"session_id": "session-victim",
                                                   "app_id": "browser",
                                                   "url": "https://example.com"}),
            (api.PREFIX + "/inspect", "POST", {"session_id": "session-victim",
                                                      "lease_id": lease["id"]}),
            (api.PREFIX + "/act", "POST", {"session_id": "session-victim",
                                                  "lease_id": lease["id"],
                                                  "action": "hover", "input": {}}),
            (api.PREFIX + "/yield", "POST", {"session_id": "session-victim",
                                                    "lease_id": lease["id"]}),
            (api.PREFIX + "/leases/%s/heartbeat" % lease["id"], "POST",
             {"session_id": "session-victim"}),
        ]
        for path, method, body in requests:
            self.assertEqual(self.call(
                path, method, body, session_capability=attacker_capability)[0], 403, path)
        self.assertEqual(self.call(
            requests[5][0], session_capability=victim_capability)[0], 200)

    def test_ticket_only_reads_and_decides_its_operation_once(self):
        ops = []
        for suffix in ("a", "b"):
            self.rt.offer("browser", "tab-" + suffix, "window-" + suffix,
                          session_id="session-" + suffix)
            lease = self.rt.claim("session-" + suffix, "browser", "tab-" + suffix)
            ops.append(self.rt.act(lease["id"], "session-" + suffix, "submit", {},
                                   "", "ticket-" + suffix))
        ticket = self.rt.create_approval_ticket(ops[0]["id"])
        code, result = self.call(api.PREFIX + "/approvals/ticket", "POST", {
            "operation_id": ops[0]["id"], "ticket": ticket})
        self.assertEqual((code, result["approval"]["id"]), (200, ops[0]["id"]))
        self.assertEqual(self.call(api.PREFIX + "/approvals/ticket", "POST", {
            "operation_id": ops[1]["id"], "ticket": ticket})[0], 403)
        code, done = self.call(api.PREFIX + "/approvals/decide", "POST", {
            "operation_id": ops[0]["id"], "approved": True, "ticket": ticket})
        self.assertEqual((code, done["approval_state"]), (200, "approved:remote_user"))
        self.assertEqual(self.call(api.PREFIX + "/approvals/ticket", "POST", {
            "operation_id": ops[0]["id"], "ticket": ticket})[0], 403)
        self.assertEqual(self.call(api.PREFIX + "/approvals/decide", "POST", {
            "operation_id": ops[1]["id"], "approved": True, "ticket": ticket})[0], 403)

        desktop_ticket = self.rt.create_approval_ticket(ops[1]["id"])
        self.assertEqual(self.call(api.PREFIX + "/approvals/decide", "POST", {
            "operation_id": ops[1]["id"], "approved": False}, token="host-only")[0], 200)
        self.assertEqual(self.call(api.PREFIX + "/approvals/ticket", "POST", {
            "operation_id": ops[1]["id"], "ticket": desktop_ticket})[0], 403)


if __name__ == "__main__":
    unittest.main()

class HostTokenIsolation(unittest.TestCase):
    """宿主令牌不进 Agent 环境(2026-09-02)。

    各引擎子进程环境都是 dict(os.environ) 整份复制;令牌若留在进程环境里,每个 Agent 会话
    都拿得到,持有者可调 /host/approve 给自己的外部写入放行。api 模块导入时把它摘走,
    只留在模块变量里;测试仍可事后往环境里放令牌(_host 的回退),不影响既有用例。
    """

    def test_导入时从进程环境摘走令牌(self):
        src = open(os.path.join(os.path.dirname(api.__file__), "api.py"), encoding="utf-8").read()
        self.assertIn('HOST_TOKEN = os.environ.pop("DESKTRON_APP_CONTROL_HOST_TOKEN", "")', src)
        self.assertIn("(HOST_TOKEN, os.environ.get(", src)

    def test_模块变量里的令牌可用于宿主端点(self):
        old = api.HOST_TOKEN
        env_before = os.environ.pop("DESKTRON_APP_CONTROL_HOST_TOKEN", None)
        api.HOST_TOKEN = "captured-only"
        try:
            class H:
                headers = {"X-Desk-App-Control-Host": "captured-only"}
            api._host(H())          # 环境里没有令牌,只靠模块变量也能过
            H.headers = {"X-Desk-App-Control-Host": "wrong"}
            with self.assertRaises(api.AppControlError):
                api._host(H())
        finally:
            api.HOST_TOKEN = old
            if env_before is not None:
                os.environ["DESKTRON_APP_CONTROL_HOST_TOKEN"] = env_before

    def test_导入后进程环境里不再有令牌(self):
        # 在干净子进程里验:带令牌启动 → 导入 api → 环境里没了、模块变量里有了。
        # 这就是各引擎 dict(os.environ) 拿不到它的全部依据。
        import subprocess, sys
        lib = os.path.join(os.path.dirname(api.__file__), "..")
        # deskcfg 在导入时读 argv(前端目录、两个端口、工作区),照本文件顶部的写法给它
        code = ("import os, sys, tempfile; sys.path.insert(0, %r); "
                "sys.argv = ['t', %r, '0', '0', tempfile.mkdtemp()]; "
                "from app_control import api; "
                "print('ENV' if 'DESKTRON_APP_CONTROL_HOST_TOKEN' in os.environ else 'CLEAN', api.HOST_TOKEN)"
                % (os.path.abspath(lib), os.path.join(ROOT, "frontend")))
        env = dict(os.environ, DESKTRON_APP_CONTROL_HOST_TOKEN="t-subprocess")
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), ["CLEAN", "t-subprocess"])

    def test_agent_session_capability_is_injected_forwarded_and_revoked(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        with open(os.path.join(root, "desktron", "lib", "engine_adapters",
                               "cli_turn.py"), encoding="utf-8") as f:
            runner = f.read()
        with open(os.path.join(root, "desktron", "lib", "engine_adapters",
                               "cli_store.py"), encoding="utf-8") as f:
            store = f.read()
        for name in ("app-control", "app-control-mcp"):
            with open(os.path.join(root, "desktron", "bin", name),
                      encoding="utf-8") as f:
                client = f.read()
            self.assertIn("X-Desk-App-Control-Session", client)
            self.assertIn("DESKTRON_APP_CONTROL_SESSION_CAPABILITY", client)
        marker = 'env["DESKTRON_APP_CONTROL_SESSION_CAPABILITY"] ='
        self.assertIn(marker, runner)
        self.assertGreater(runner.index(marker), runner.index("env.update(extra_env)"))
        self.assertIn("revoke_app_control_capability(sid)", store)
