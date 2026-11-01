"""App Control Runtime：显式委托、租约、审批、幂等与可恢复回执。"""
import base64
import json
import os
from _platform import database, assert_private_file
import sys
import tempfile
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
if "deskcfg" not in sys.modules:
    sys.argv = ["test", os.path.join(ROOT, "frontend"), "0", "0", tempfile.mkdtemp()]

from app_control.runtime import AppControlError, AppControlRuntime


CONTROL = {
    "protocol": "desk.app-control/v1alpha1",
    "identity": {"app": "test.browser", "version": "1", "adapter": "browser/v1"},
    "level": "L3",
    "resources": [
        {"type": "tab", "scope": "delegated-visible-tab"},
        {"type": "agent-tab", "scope": "agent-owned-isolated-tab"},
        {"type": "granted-tab", "scope": "agent-owned-identified-tab"},
    ],
    "observations": [{"name": "document", "schema": "test.document/v1"}],
    "actions": [
        {"name": "fill", "effect": "reversible_write", "approval": "risk_based",
         "timeout_ms": 1000, "idempotency": "keyed",
         "input_schema": {"type": "object", "required": ["node_id", "value"],
                          "additionalProperties": False,
                          "properties": {"node_id": {"type": "string"},
                                         "value": {"type": "string"}}}},
        {"name": "submit", "effect": "external_write", "approval": "always",
         "timeout_ms": 1000, "idempotency": "required",
         "input_schema": {"type": "object", "additionalProperties": False,
                          "properties": {}}},
        {"name": "close", "effect": "destructive", "approval": "always",
         "timeout_ms": 1000, "idempotency": "required",
         "input_schema": {"type": "object", "additionalProperties": False,
                          "properties": {}}},
    ],
    "permissions": {"delegation": "explicit", "lease": "exclusive",
                    "user_interaction": "pause", "takeover": "user_confirmed"},
    "events": ["ready", "changed", "action_completed", "failed", "waiting_user", "lease_revoked"],
    "receipts": {"required": True, "undo_when_declared": True},
}
APPS = [{"id": "browser", "label": "浏览器", "agent_control": CONTROL}]
DESK_CONTROL = {
    "protocol": "desk.app-control/v1alpha1",
    "identity": {"app": "test.desk", "version": "1", "adapter": "desk-surface/v1"},
    "level": "L1",
    "resources": [{"type": "desk-surface", "scope": "delegated-current-surface"}],
    "observations": [
        {"name": "surface", "schema": "desk.surface/v1"},
        {"name": "screenshot", "schema": "desk.image/v1", "sensitive": True,
         "input_schema": {"type": "object", "additionalProperties": False,
                          "properties": {"full_page": {"type": "boolean"}}}},
    ],
    "actions": [
        {"name": "open_page", "effect": "reversible_write", "approval": "never",
         "timeout_ms": 5000, "idempotency": "keyed",
         "input_schema": {"type": "object", "additionalProperties": False,
                          "required": ["page"],
                          "properties": {"page": {"type": "string",
                                                    "enum": ["apps"]}}}},
    ],
    "permissions": {"delegation": "explicit", "lease": "exclusive",
                    "user_interaction": "pause", "takeover": "user_confirmed"},
    "events": ["ready", "changed", "action_completed", "failed",
               "waiting_user", "lease_revoked"],
    "receipts": {"required": True, "undo_when_declared": True},
}
DESK_APP = {"id": "desk", "label": "Desktron", "agent_control": DESK_CONTROL}
NATIVE_CONTROL = {
    "protocol": "desk.app-control/v1alpha1",
    "identity": {"app": "test.native", "version": "1", "adapter": "macos-accessibility/v1"},
    "level": "L2",
    "native_application": {
        "bundle_id": "com.example.fixture",
        "target_binding": ["bundle_id", "pid", "window_id"],
        "code_requirement": 'anchor apple generic and identifier "com.example.fixture"',
        "projector": "ax-structural/v1",
        "allowed_roles": ["AXWindow"],
        "allowed_semantics": ["window"],
        "allowed_values": ["on"],
    },
    "resources": [{"type": "native-window", "scope": "delegated-application-window"}],
    "observations": [{"name": "window", "schema": "desk.native-window/v1"}],
    "actions": [
        {"name": "refresh", "effect": "reversible_write", "approval": "risk_based",
         "timeout_ms": 1000, "idempotency": "required",
         "input_schema": {"type": "object", "additionalProperties": False,
                          "properties": {}}},
        {"name": "reconnect", "effect": "external_write", "approval": "always",
         "timeout_ms": 1000, "idempotency": "required",
         "input_schema": {"type": "object", "additionalProperties": False,
                          "properties": {}}},
    ],
    "permissions": {"delegation": "explicit", "lease": "exclusive",
                    "user_interaction": "pause", "takeover": "user_confirmed"},
    "events": ["ready", "changed", "action_completed", "failed", "waiting_user", "lease_revoked"],
    "receipts": {"required": True, "undo_when_declared": True},
}
NATIVE_APP = {"id": "native-fixture", "label": "Native Fixture", "agent_control": NATIVE_CONTROL,
              "_native_manifest_source": "builtin"}


class Runtime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "runtime.sqlite3")
        self.now = [1000.0]
        self.rt = AppControlRuntime(self.path, apps_provider=lambda: APPS,
                                    clock=lambda: self.now[0],
                                    evidence_dir=os.path.join(self.tmp.name, "evidence"))

    def tearDown(self):
        self.rt.close()
        self.tmp.cleanup()

    def lease(self):
        offered = self.rt.offer("browser", "tab-1", "window-1")
        self.assertEqual(offered["status"], "offered")
        return self.rt.claim("session-1", "browser", "tab-1")

    def test_意图只影响说明且会清洗占位内容(self):
        lease = self.lease()
        op = self.rt.act(lease["id"], "session-1", "submit", {}, "", "intent-1",
                         intent="  提交   本周值班安排  ")
        self.assertEqual(op["intent"], "提交 本周值班安排")
        self.assertEqual((op["effect"], op["status"], op["approval_state"]),
                         ("external_write", "waiting_user", "pending"))
        placeholder = self.rt.act(lease["id"], "session-1", "submit", {}, "",
                                  "intent-2", intent="待补充")
        self.assertEqual(placeholder["intent"], "")

    def test_desk_running_write_cannot_succeed_after_human_pause(self):
        self.rt.close()
        self.rt = AppControlRuntime(self.path, apps_provider=lambda: [*APPS, DESK_APP],
                                    clock=lambda: self.now[0],
                                    evidence_dir=os.path.join(self.tmp.name, "evidence"))
        self.rt.offer("desk", "desk-window:one:navigation", "window-one",
                      session_id="session-desk")
        lease = self.rt.claim("session-desk", "desk", "desk-window:one:navigation")
        op = self.rt.act(lease["id"], "session-desk", "open_page",
                         {"page": "apps"}, "", "desk-pause-1")
        running = self.rt.next_operation("desk")
        self.assertEqual(running["status"], "running")
        self.rt.pause(lease["id"], "user_interaction")
        done = self.rt.complete(op["id"], True, {"route": "apps"}, "", "v2")
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["error"], "human_intervention_reinspect_required")
        self.assertTrue(done["output"]["execution_uncertain"])
        self.assertTrue(done["output"]["requires_reinspect"])

    def test_desk_screenshot_requires_approval_and_uses_private_evidence(self):
        self.rt.close()
        self.rt = AppControlRuntime(self.path, apps_provider=lambda: [*APPS, DESK_APP],
                                    clock=lambda: self.now[0],
                                    evidence_dir=os.path.join(self.tmp.name, "evidence"))
        self.rt.offer("desk", "desk-window:one:navigation", "window-one",
                      session_id="session-desk")
        lease = self.rt.claim("session-desk", "desk", "desk-window:one:navigation")
        operation = self.rt.inspect(lease["id"], "session-desk", "screenshot",
                                    idempotency_key="desk-shot-1",
                                    params={"full_page": True})
        self.assertEqual(operation["status"], "waiting_user")
        self.assertIsNone(self.rt.next_operation("desk"))
        self.rt.approve(operation["id"], True)
        self.assertEqual(self.rt.next_operation("desk")["id"], operation["id"])
        png = b"\x89PNG\r\n\x1a\ndesk-fixture"
        done = self.rt.complete(operation["id"], True, {
            "mime_type": "image/png", "data": base64.b64encode(png).decode(),
            "width": 900, "height": 700, "full_page": True,
        })
        self.assertEqual(done["status"], "succeeded")
        self.assertNotIn("data", done["output"])
        self.assertTrue(done["output"]["full_page"])
        assert_private_file(done["output"]["path"])

    def test_desktop_status_requires_trust_and_intervention_monitor(self):
        empty = self.rt.desktop_status()
        self.assertEqual(empty["execution"], {
            "status": "unavailable", "reason": "accessibility_not_reported"})
        host_id = os.environ.get("DESKTRON_HOST_INSTANCE_ID") or "host-one"
        host_pid = os.getppid() if os.environ.get("DESKTRON_NATIVE_LAUNCH") == "1" else 1234
        reported = self.rt.report_desktop_capability(
            host_id, "io.github.desktron", host_pid, "not_trusted")
        self.assertEqual(reported["capabilities"]["accessibility"]["status"], "not_trusted")
        self.assertEqual(reported["execution"], {
            "status": "unavailable", "reason": "accessibility_not_trusted"})
        trusted = self.rt.report_desktop_capability(
            host_id, "io.github.desktron", host_pid, "trusted")
        self.assertEqual(trusted["execution"], {
            "status": "unavailable", "reason": "user_intervention_monitor_unavailable"})
        monitored = self.rt.report_desktop_capability(
            host_id, "io.github.desktron", host_pid, "trusted",
            monitoring_status="ready")
        self.assertEqual(monitored["execution"], {"status": "ready"})
        # 未受信任时不能单凭宿主自称监测就绪而开放执行。
        demoted = self.rt.report_desktop_capability(
            host_id, "io.github.desktron", host_pid, "not_trusted",
            monitoring_status="ready")
        self.assertEqual(demoted["capabilities"]["user_intervention_monitor"]["status"],
                         "unavailable")
        for invalid in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(AppControlError) as ctx:
                self.rt.report_desktop_capability(
                    host_id, "io.github.desktron", host_pid, "trusted", invalid)
            self.assertEqual(ctx.exception.code, 422)

    def test_desktop_status_rejects_out_of_order_report_and_resets_on_runtime_restart(self):
        host_id = os.environ.get("DESKTRON_HOST_INSTANCE_ID") or "host-one"
        host_pid = os.getppid() if os.environ.get("DESKTRON_NATIVE_LAUNCH") == "1" else 1234
        self.rt.report_desktop_capability(
            host_id, "io.github.desktron", host_pid, "not_trusted", 999.0)
        self.rt.report_desktop_capability(
            host_id, "io.github.desktron", host_pid, "trusted", 998.0)
        self.assertEqual(self.rt.desktop_status()["capabilities"]["accessibility"]["status"],
                         "not_trusted")
        self.rt.close()
        self.rt = AppControlRuntime(self.path, apps_provider=lambda: APPS,
                                    clock=lambda: self.now[0],
                                    evidence_dir=os.path.join(self.tmp.name, "evidence"))
        self.assertEqual(self.rt.desktop_status()["capabilities"]["accessibility"]["status"],
                         "not_reported")

    def test_desktop_capability_ticket_is_hashed_scoped_and_does_not_create_operation(self):
        self.rt.close()
        self.rt = AppControlRuntime(self.path, apps_provider=lambda: [*APPS, NATIVE_APP],
                                    clock=lambda: self.now[0],
                                    evidence_dir=os.path.join(self.tmp.name, "evidence"))
        issued = self.rt.issue_desktop_capability(
            "native-fixture", "com.example.fixture", 4321, "window-7", ["refresh"],
            "session-native", "task-native", 60)
        self.assertTrue(issued["ticket"].startswith("dcap_"))
        row = self.rt.db.execute("SELECT * FROM desktop_capability_tickets").fetchone()
        self.assertNotEqual(row["token_hash"], issued["ticket"])
        self.assertNotIn(issued["ticket"], "|".join(str(x) for x in row))
        self.assertEqual(self.rt.db.execute(
            "SELECT COUNT(*) FROM operations").fetchone()[0], 0)
        verified = self.rt.verify_desktop_capability(
            issued["ticket"], "native-fixture", "com.example.fixture", 4321,
            "window-7", "refresh", "session-native", "task-native")
        self.assertEqual(verified["allowed_actions"], ["refresh"])
        for mismatch in (
                ("other-app", "com.example.fixture", 4321, "window-7", "refresh"),
                ("native-fixture", "com.example.fixture", 4321, "window-8", "refresh"),
                ("native-fixture", "com.example.fixture", 4321, "window-7", "reconnect")):
            with self.assertRaises(AppControlError) as ctx:
                self.rt.verify_desktop_capability(
                    issued["ticket"], *mismatch, "session-native", "task-native")
            self.assertEqual(ctx.exception.code, 403)
        self.rt.close()
        self.rt = AppControlRuntime(self.path, apps_provider=lambda: [*APPS, NATIVE_APP],
                                    clock=lambda: self.now[0],
                                    evidence_dir=os.path.join(self.tmp.name, "evidence"))
        with self.assertRaises(AppControlError) as ctx:
            self.rt.verify_desktop_capability(
                issued["ticket"], "native-fixture", "com.example.fixture", 4321,
                "window-7", "refresh", "session-native", "task-native")
        self.assertEqual(ctx.exception.code, 403)
        issued = self.rt.issue_desktop_capability(
            "native-fixture", "com.example.fixture", 4321, "window-7", ["refresh"],
            "session-native", "task-native", 60)
        self.now[0] += 61
        with self.assertRaises(AppControlError) as ctx:
            self.rt.verify_desktop_capability(
                issued["ticket"], "native-fixture", "com.example.fixture", 4321,
                "window-7", "refresh", "session-native", "task-native")
        self.assertEqual(ctx.exception.code, 403)
        for ttl in (0, -1, 901):
            with self.assertRaises(AppControlError) as ctx:
                self.rt.issue_desktop_capability(
                    "native-fixture", "com.example.fixture", 4321, "window-7", ["refresh"],
                    "session-native", "task-native", ttl)
            self.assertEqual(ctx.exception.code, 422)
        boundary = self.rt.issue_desktop_capability(
            "native-fixture", "com.example.fixture", 4321, "window-7", ["refresh"],
            "session-native", "task-native", 900)
        self.assertEqual(boundary["expires_at"], self.now[0] + 900)

    def test_native_application_cannot_use_existing_lease_or_operation_paths(self):
        self.rt.close()
        self.rt = AppControlRuntime(self.path, apps_provider=lambda: [*APPS, NATIVE_APP],
                                    clock=lambda: self.now[0],
                                    evidence_dir=os.path.join(self.tmp.name, "evidence"))
        with self.assertRaises(AppControlError):
            self.rt.offer("native-fixture", "native-window:1", "window-1")
        with self.assertRaises(AppControlError):
            self.rt.open_tab("session-native", "native-fixture", "https://example.com")
        with self.assertRaises(AppControlError):
            self.rt.next_operation("native-fixture")
        self.assertEqual(self.rt.db.execute("SELECT COUNT(*) FROM leases").fetchone()[0], 0)
        self.assertEqual(self.rt.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)

    def test_desktop_ticket_requires_declared_application_and_exact_action_allowlist(self):
        with self.assertRaises(AppControlError):
            self.rt.issue_desktop_capability(
                "browser", "com.example.fixture", 1, "window", ["fill"], "session", "task")
        self.rt.close()
        self.rt = AppControlRuntime(self.path, apps_provider=lambda: [*APPS, NATIVE_APP],
                                    clock=lambda: self.now[0],
                                    evidence_dir=os.path.join(self.tmp.name, "evidence"))
        with self.assertRaises(AppControlError) as ctx:
            self.rt.issue_desktop_capability(
                "native-fixture", "com.example.fixture", 1, "window", ["unknown"],
                "session", "task")
        self.assertEqual(ctx.exception.code, 403)

    def test_旧数据库自动补充意图列(self):
        path = os.path.join(self.tmp.name, "legacy-intent.sqlite3")
        original = AppControlRuntime(path, apps_provider=lambda: APPS)
        original.close()
        with database(path) as db:
            db.execute("ALTER TABLE operations DROP COLUMN intent")
        migrated = AppControlRuntime(path, apps_provider=lambda: APPS)
        try:
            columns = {row["name"] for row in migrated.db.execute(
                "PRAGMA table_info(operations)")}
            self.assertIn("intent", columns)
            self.assertEqual(migrated.db.execute(
                "SELECT intent FROM operations LIMIT 1").fetchone(), None)
        finally:
            migrated.close()

    def test_agent_cannot_claim_without_user_offer(self):
        with self.assertRaises(AppControlError) as ctx:
            self.rt.claim("session-1", "browser", "tab-1")
        self.assertEqual(ctx.exception.code, 403)

    def test_clear_all_resolved_approvals_including_legacy_cancelled(self):
        lease = self.lease()
        states = [
            ("queued", "approved:ui_user"),
            ("running", "approved:ui_user"),
            ("succeeded", "approved:ui_user"),
            ("cancelled", "approved:ui_user"),
            ("rejected", "rejected:ui_user"),
            ("cancelled", "cancelled"),
            ("failed", "timeout"),
            ("cancelled", "pending"),  # 旧版 agent_yield 遗留记录
        ]
        ids = []
        for index, (status, approval) in enumerate(states):
            op = self.rt.act(lease["id"], "session-1", "submit", {}, "", f"clear-{index}")
            ids.append(op["id"])
        # 先建立全部动作，再模拟历史状态，避免 act 的正常超时检查影响 fixture。
        with self.rt.db:
            for op_id, (status, approval) in zip(ids, states):
                self.rt.db.execute(
                    "UPDATE operations SET status=?,approval_state=? WHERE id=?",
                    (status, approval, op_id))
        before = [dict(r) for r in self.rt.db.execute("SELECT * FROM operations ORDER BY id")]
        self.assertEqual(self.rt.clear_approval_history(1000)["cleared"], len(states))
        after = [dict(r) for r in self.rt.db.execute("SELECT * FROM operations ORDER BY id")]
        for original, cleared in zip(before, after):
            self.assertEqual(cleared.pop("approval_cleared_at"), 1000)
            original.pop("approval_cleared_at")
            self.assertEqual(cleared, original, "清空不得改写动作或审批结果")
        self.assertEqual(self.rt.clear_approval_history()["cleared"], 0)
        self.assertEqual(len(self.rt.approval_history()), len(states))

    def test_clear_does_not_expire_pending_or_cancel_approved_work(self):
        lease = self.lease()
        approved = self.rt.act(lease["id"], "session-1", "submit", {}, "", "allowed")
        self.rt.approve(approved["id"], True, "ui_user")
        pending = self.rt.act(lease["id"], "session-1", "submit", {}, "", "waiting")
        ordinary = self.rt.inspect(lease["id"], "session-1", idempotency_key="read")
        before = [dict(r) for r in self.rt.db.execute("SELECT * FROM operations ORDER BY id")]
        events = self.rt.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.now[0] += 4000  # 清空不得顺便执行到期处理
        self.assertEqual(self.rt.clear_approval_history(1000)["cleared"], 1)
        after = [dict(r) for r in self.rt.db.execute("SELECT * FROM operations ORDER BY id")]
        for original, cleared in zip(before, after):
            mark = cleared.pop("approval_cleared_at")
            self.assertEqual(mark, 5000 if original["id"] == approved["id"] else None)
            original.pop("approval_cleared_at")
            self.assertEqual(cleared, original)
        self.assertEqual(self.rt.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], events)
        self.assertNotEqual(pending["id"], ordinary["id"])

    def test_clear_respects_cutoff_and_preserves_new_decisions(self):
        lease = self.lease()
        old = self.rt.act(lease["id"], "session-1", "submit", {}, "", "old")
        self.rt.approve(old["id"], False)
        self.now[0] += 1
        new = self.rt.act(lease["id"], "session-1", "submit", {}, "", "new")
        self.rt.approve(new["id"], True)
        self.assertEqual(self.rt.clear_approval_history(1000)["cleared"], 1)
        self.assertIsNone(self.rt.get_operation(new["id"])["approval_cleared_at"])

    def test_yield_cancels_pending_without_overwriting_approval(self):
        lease = self.lease()
        allowed = self.rt.act(lease["id"], "session-1", "submit", {}, "", "allowed")
        self.rt.approve(allowed["id"], True, "ui_user")
        pending = self.rt.act(lease["id"], "session-1", "submit", {}, "", "waiting")
        self.rt.yield_lease(lease["id"], "session-1")
        self.assertEqual(self.rt.get_operation(pending["id"])["approval_state"], "cancelled")
        self.assertEqual(self.rt.get_operation(allowed["id"])["approval_state"], "approved:ui_user")
        self.assertEqual(self.rt.clear_approval_history()["cleared"], 2)

    def test_probe_distinguishes_unavailable_authorization_and_ready(self):
        probe = self.rt.probe("session-1")
        self.assertEqual(probe["transport"]["status"], "ready")
        self.assertEqual(probe["browser_plugin"]["status"], "browser_plugin_unavailable")
        self.assertEqual(probe["control"]["status"], "needs_authorization")
        self.assertEqual(probe["control"]["execution_status"], "unavailable")
        self.assertEqual(probe["control"]["execution_reason"], "host_unavailable")
        self.assertEqual(probe["self_open"]["isolated"]["status"], "unavailable")
        self.assertEqual(probe["self_open"]["isolated"]["reason"],
                         "host_unavailable")

        self.rt.offer("browser", "tab-1", "window-1", session_id="session-1")
        self.assertEqual(self.rt.probe("session-1")["control"]["status"],
                         "available_to_claim")
        lease = self.rt.claim("session-1", "browser", "tab-1")
        ready = self.rt.probe("session-1")
        self.assertEqual(ready["control"]["status"], "ready")
        self.assertEqual(ready["control"]["lease"]["id"], lease["id"])
        self.assertIn("isolated", ready["self_open"])
        self.assertEqual(ready["self_open"]["isolated"]["status"], "unavailable")

        self.rt.host_seen("browser")
        self.rt.grant_site("browser", "https://secret.example.net")
        executable = self.rt.probe("session-1")
        self.assertEqual(executable["control"]["execution_status"], "ready")
        self.assertEqual(executable["self_open"]["isolated"]["status"], "ready")
        self.assertNotIn("reason", executable["self_open"]["isolated"])
        self.assertNotIn("granted_origins", executable["self_open"]["identified"])
        self.assertNotIn("secret.example.net", json.dumps(executable))

    def test_desk_requires_explicit_surface_delegation_and_cannot_self_open(self):
        rt = AppControlRuntime(os.path.join(self.tmp.name, "desk.sqlite3"),
                               apps_provider=lambda: [*APPS, DESK_APP])
        try:
            probe = rt.probe("session-1", "desk")
            self.assertEqual(probe["control"]["status"], "needs_authorization")
            self.assertEqual(probe["control"]["action"],
                             "delegate_current_surface")
            self.assertEqual(probe["control"]["execution_status"], "unavailable")
            self.assertEqual(probe["control"]["execution_reason"], "host_unavailable")
            self.assertNotIn("self_open", probe)
            with self.assertRaises(AppControlError) as caught:
                rt.open_tab("session-1", "desk", "https://example.com")
            self.assertEqual(caught.exception.code, 403)
            self.assertEqual(rt.resources("session-1", "desk"), [])
        finally:
            rt.close()

    def test_host_status_is_per_app_and_expires(self):
        rt = AppControlRuntime(os.path.join(self.tmp.name, "host.sqlite3"),
                               apps_provider=lambda: [*APPS, DESK_APP],
                               clock=lambda: self.now[0])
        try:
            self.assertEqual(rt.probe("session-1", "desk")["host"]["status"],
                             "unavailable")
            self.assertEqual(rt.host_seen("desk")["status"], "online")
            online = rt.probe("session-1", "desk")
            self.assertEqual(online["host"]["status"], "online")
            self.assertEqual(online["control"]["execution_status"], "blocked")
            self.assertEqual(online["control"]["execution_reason"],
                             "needs_authorization")
            self.assertEqual(rt.probe("session-1", "browser")["host"]["status"],
                             "unavailable")
            self.now[0] += 6
            expired = rt.probe("session-1", "desk")
            self.assertEqual(expired["host"]["status"], "unavailable")
            self.assertEqual(expired["control"]["execution_status"], "unavailable")
            self.assertEqual(expired["control"]["execution_reason"],
                             "host_unavailable")
        finally:
            rt.close()

    def test_probe_keeps_ready_lease_but_marks_offline_host_unavailable(self):
        rt = AppControlRuntime(os.path.join(self.tmp.name, "offline-ready.sqlite3"),
                               apps_provider=lambda: [DESK_APP],
                               clock=lambda: self.now[0])
        try:
            rt.offer("desk", "desk-window:w1:navigation", "w1", 900, "s1")
            rt.claim("s1", "desk", "desk-window:w1:navigation")
            offline = rt.probe("s1", "desk")
            self.assertEqual(offline["control"]["status"], "ready")
            self.assertEqual(offline["control"]["execution_status"], "unavailable")
            self.assertEqual(offline["control"]["execution_reason"],
                             "host_unavailable")

            rt.host_seen("desk")
            online = rt.probe("s1", "desk")
            self.assertEqual(online["control"]["status"], "ready")
            self.assertEqual(online["control"]["execution_status"], "ready")
            self.assertNotIn("execution_reason", online["control"])
        finally:
            rt.close()

    def test_restart_gate_rejects_new_operation_and_dispatch(self):
        import lifecycle
        lease = self.lease()
        lifecycle._STATE = 'stopping'; lifecycle._OPERATION = 'a' * 32
        try:
            with self.assertRaisesRegex(AppControlError, '重启'):
                self.rt.act(lease['id'], 'session-1', 'fill',
                    {'node_id':'field','value':'x'}, idempotency_key='gated')
            self.assertIsNone(self.rt.next_operation('browser', lease['resource_id']))
        finally:
            lifecycle._STATE = 'running'; lifecycle._OPERATION = ''

    def test_desk_only_accepts_declared_semantic_actions(self):
        rt = AppControlRuntime(os.path.join(self.tmp.name, "desk-actions.sqlite3"),
                               apps_provider=lambda: [DESK_APP])
        try:
            rt.offer("desk", "desk-window:w1:navigation", "w1", 900, "s1")
            lease = rt.claim("s1", "desk", "desk-window:w1:navigation")
            op = rt.act(lease["id"], "s1", "open_page", {"page": "apps"},
                        idempotency_key="open-apps")
            self.assertEqual((op["status"], op["approval_state"]),
                             ("queued", "not_required"))
            with self.assertRaises(AppControlError) as caught:
                rt.act(lease["id"], "s1", "click", {"selector": "#settings-btn"})
            self.assertEqual(caught.exception.code, 404)
        finally:
            rt.close()

    def test_delegation_bound_to_one_session_cannot_be_stolen(self):
        offered = self.rt.offer("browser", "tab-1", "window-1", session_id="session-1")
        with self.assertRaises(AppControlError) as ctx:
            self.rt.claim("session-2", "browser", "tab-1")
        self.assertEqual(ctx.exception.code, 403)
        self.assertEqual(self.rt.claim("session-1", "browser", "tab-1")["id"], offered["id"])

    def test_inspect_runs_and_forms_durable_receipt(self):
        lease = self.lease()
        operation = self.rt.inspect(lease["id"], "session-1", idempotency_key="inspect-1")
        self.assertEqual(operation["status"], "queued")
        running = self.rt.next_operation("browser", "tab-1")
        self.assertEqual(running["status"], "running")
        done = self.rt.complete(running["id"], True, {"title": "Example"},
                                observation_version="doc-1")
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["after_version"], "doc-1")
        self.assertEqual(self.rt.receipt(done["id"])["output"]["title"], "Example")

    def test_external_write_waits_for_user_and_is_idempotent(self):
        lease = self.lease()
        operation = self.rt.act(lease["id"], "session-1", "submit", {},
                                idempotency_key="submit-1")
        self.assertEqual(operation["status"], "waiting_user")
        same = self.rt.act(lease["id"], "session-1", "submit", {},
                           idempotency_key="submit-1")
        self.assertEqual(same["id"], operation["id"])
        self.assertIsNone(self.rt.next_operation("browser", "tab-1"))
        approved = self.rt.approve(operation["id"], True)
        self.assertEqual(approved["status"], "queued")
        self.assertEqual(self.rt.next_operation("browser", "tab-1")["id"], operation["id"])

    def test_external_write_requires_idempotency_key(self):
        lease = self.lease()
        with self.assertRaises(AppControlError) as ctx:
            self.rt.act(lease["id"], "session-1", "submit", {})
        self.assertEqual(ctx.exception.code, 422)

    def test_sensitive_screenshot_waits_for_user(self):
        control = APPS[0]["agent_control"]
        control["observations"].append(
            {"name": "screenshot", "schema": "desk.image/v1", "sensitive": True})
        try:
            lease = self.lease()
            operation = self.rt.inspect(lease["id"], "session-1", "screenshot")
            self.assertEqual(operation["status"], "waiting_user")
            self.assertEqual(self.rt.pending_approval("browser", "tab-1")["id"], operation["id"])
        finally:
            control["observations"].pop()

    def test_approved_screenshot_becomes_private_evidence_file(self):
        control = APPS[0]["agent_control"]
        control["observations"].append(
            {"name": "screenshot", "schema": "desk.image/v1", "sensitive": True})
        try:
            lease = self.lease()
            operation = self.rt.inspect(lease["id"], "session-1", "screenshot")
            self.rt.approve(operation["id"], True)
            self.rt.next_operation("browser", "tab-1")
            png = b"\x89PNG\r\n\x1a\nfixture"
            done = self.rt.complete(operation["id"], True, {
                "mime_type": "image/png", "data": base64.b64encode(png).decode(),
                "width": 10, "height": 10})
            self.assertEqual(done["status"], "succeeded")
            self.assertNotIn("data", done["output"])
            with open(done["output"]["path"], "rb") as f:
                self.assertEqual(f.read(), png)
            assert_private_file(done["output"]["path"])
        finally:
            control["observations"].pop()

    def test_stale_observation_version_is_rejected(self):
        lease = self.lease()
        inspect = self.rt.inspect(lease["id"], "session-1")
        self.rt.next_operation("browser", "tab-1")
        self.rt.complete(inspect["id"], True, {"nodes": []}, observation_version="doc-2")
        with self.assertRaises(AppControlError) as ctx:
            self.rt.act(lease["id"], "session-1", "fill",
                        {"node_id": "doc-1:n1", "value": "x"}, expected_version="doc-1")
        self.assertEqual(ctx.exception.code, 409)

    def test_successful_mutation_advances_runtime_version(self):
        lease = self.lease()
        inspect = self.rt.inspect(lease["id"], "session-1")
        self.rt.next_operation("browser", "tab-1")
        self.rt.complete(inspect["id"], True, {"nodes": []}, observation_version="doc-1")
        fill = self.rt.act(lease["id"], "session-1", "fill",
                           {"node_id": "doc-1:n1", "value": "x"}, expected_version="doc-1")
        self.rt.next_operation("browser", "tab-1")
        self.rt.complete(fill["id"], True, {"url": "https://example.test"},
                         observation_version="doc-2")
        with self.assertRaises(AppControlError) as ctx:
            self.rt.act(lease["id"], "session-1", "fill",
                        {"node_id": "doc-1:n2", "value": "y"}, expected_version="doc-1")
        self.assertEqual(ctx.exception.code, 409)

    def test_user_takeover_revokes_lease_and_pending_work(self):
        lease = self.lease()
        operation = self.rt.inspect(lease["id"], "session-1")
        revoked = self.rt.revoke(lease["id"], "user_interaction")
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(self.rt.get_operation(operation["id"])["status"], "cancelled")

    def test_agent_can_yield_only_its_own_lease(self):
        lease = self.lease()
        with self.assertRaises(AppControlError) as ctx:
            self.rt.yield_lease(lease["id"], "session-2")
        self.assertEqual(ctx.exception.code, 403)
        self.assertEqual(self.rt.yield_lease(lease["id"], "session-1")["status"], "revoked")

    def test_takeover_cancels_running_operation_and_late_result_is_ignored(self):
        lease = self.lease()
        operation = self.rt.inspect(lease["id"], "session-1")
        self.rt.next_operation("browser", "tab-1")
        self.rt.revoke(lease["id"], "user_interaction")
        cancelled = self.rt.complete(operation["id"], True, {"too_late": True})
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(cancelled["output"])

    def test_running_operation_times_out_deterministically(self):
        lease = self.lease()
        operation = self.rt.inspect(lease["id"], "session-1")
        self.rt.next_operation("browser", "tab-1")
        self.now[0] += 16
        timed_out = self.rt.get_lease(lease["id"])
        self.assertEqual(timed_out["status"], "active")
        operation = self.rt.get_operation(operation["id"])
        self.assertEqual((operation["status"], operation["error"]),
                         ("failed", "operation_timeout"))

    def test_restart_fails_running_work_instead_of_replaying(self):
        lease = self.lease()
        operation = self.rt.inspect(lease["id"], "session-1")
        self.rt.next_operation("browser", "tab-1")
        self.rt.close()
        self.rt = AppControlRuntime(self.path, apps_provider=lambda: APPS,
                                    clock=lambda: self.now[0],
                                    evidence_dir=os.path.join(self.tmp.name, "evidence"))
        recovered = self.rt.get_operation(operation["id"])
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["error"], "runtime_restarted_during_execution")


if __name__ == "__main__":
    unittest.main()

class PauseAndResume(unittest.TestCase):
    """人碰页面是让开,不是收回(2026-09-02)。

    manifest 声明 user_interaction: pause,而实现一直走 revoke——用户看一眼、点一下
    页面,租约就永久没了,得从头重新委托。这一组钉住暂停语义:动作被挡但说得出原因、
    暂停期间不派活、恢复后重新观察、明确收回仍然是终态。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = AppControlRuntime(os.path.join(self.tmp.name, "rt.sqlite3"),
                                    apps_provider=lambda: APPS)
        self.rt.offer("browser", "tab-1", "win", 900, "s1")
        self.lease = self.rt.claim("s1", "browser", "tab-1")["id"]

    def tearDown(self):
        self.rt.close()
        self.tmp.cleanup()

    def test_暂停后动作被挡且原因可辨(self):
        self.rt.pause(self.lease, "user_interaction")
        self.assertEqual(self.rt.get_lease(self.lease)["status"], "paused")
        with self.assertRaises(AppControlError) as caught:
            self.rt.inspect(self.lease, "s1", "document")
        # 必须与 revoked 分得开:Agent 该等,不该重新委托
        self.assertIn("暂停", str(caught.exception))
        self.assertEqual(caught.exception.code, 409)

    def test_暂停期间不派活恢复后重新观察(self):
        op = self.rt.inspect(self.lease, "s1", "document")["id"]
        self.rt.pause(self.lease, "user_interaction")
        self.assertIsNone(self.rt.next_operation("browser", "tab-1"))   # 让开了就不派活
        self.rt.resume(self.lease)
        self.assertEqual(self.rt.get_lease(self.lease)["status"], "active")
        self.assertEqual(self.rt.get_operation(op)['status'], 'cancelled')
        self.assertIsNone(self.rt.next_operation('browser', 'tab-1'))
        op = self.rt.inspect(self.lease, 's1', 'document')['id']
        got = self.rt.next_operation("browser", "tab-1")
        self.assertEqual(got["id"], op)
        self.assertEqual(got["status"], "running")

    def test_重复暂停只续时不报错(self):
        self.rt.pause(self.lease, "user_interaction")
        self.rt.pause(self.lease, "user_interaction")   # 每次按键都会来一趟
        self.assertEqual(self.rt.get_lease(self.lease)["status"], "paused")

    def test_明确收回仍是终态(self):
        self.rt.pause(self.lease, "user_interaction")
        self.rt.revoke(self.lease, "user_takeover")
        with self.assertRaises(AppControlError):
            self.rt.resume(self.lease)              # 收回之后不能被自动恢复救回来
        self.assertEqual(self.rt.get_lease(self.lease)["status"], "revoked")

    def test_暂停期间到期照样释放(self):
        self.rt.pause(self.lease, "user_interaction")
        self.rt.clock = lambda: time.time() + 4000   # TTL 上限 3600
        with self.assertRaises(AppControlError):
            self.rt.resume(self.lease)
        self.assertEqual(self.rt.get_lease(self.lease)["status"], "expired")

    def test_动作本身就是心跳(self):
        """长任务不该因为 TTL 到点丢租约,而 CLI 没有心跳命令。

        把「还在干活」当心跳:每次入队顺带续期,比另开一条心跳路径少一处要维护的东西。
        """
        base = time.time()
        before = self.rt.get_lease(self.lease)["expires_at"]   # setUp 的 claim 给了 300 秒
        self.rt.clock = lambda: base + 250                     # 快到点时来了一次动作
        self.rt.inspect(self.lease, "s1", "document")
        after = self.rt.get_lease(self.lease)["expires_at"]
        self.assertGreater(after, before + 800)                # 从这一刻起重新算 900
        self.rt.clock = lambda: base + 400                     # 原 TTL 已过,续期后仍在
        self.assertEqual(self.rt.get_lease(self.lease)["status"], "active")

    def test_探测能报出暂停(self):
        self.rt.pause(self.lease, "user_interaction")
        control = self.rt.probe("s1", "browser")["control"]
        self.assertEqual(control["status"], "paused")
        self.assertEqual(control["reason"], "user_interaction")

    def test_desk真人介入也取消旧观察并要求重新观察(self):
        rt = AppControlRuntime(os.path.join(self.tmp.name, "desk-pause.sqlite3"),
                               apps_provider=lambda: [DESK_APP])
        try:
            rt.offer("desk", "desk-window:w1:navigation", "w1", 900, "s1")
            lease = rt.claim("s1", "desk", "desk-window:w1:navigation")
            op = rt.inspect(lease["id"], "s1", "surface")
            paused = rt.pause(lease["id"], "user_interaction")
            self.assertEqual(paused["needs_inspect"], 1)
            self.assertEqual(rt.get_operation(op["id"])["status"], "cancelled")
            rt.resume(lease["id"])
            with self.assertRaises(AppControlError) as caught:
                rt.act(lease["id"], "s1", "open_page", {"page": "apps"},
                       idempotency_key="before-reinspect")
            self.assertEqual(caught.exception.code, 409)
            fresh = rt.inspect(lease["id"], "s1", "surface")
            self.assertEqual(rt.next_operation("desk", lease["resource_id"])["id"], fresh["id"])
            rt.complete(fresh["id"], True, {"schema": "desk.surface/v1"},
                        observation_version="navigation:1")
            unlocked = rt.act(lease["id"], "s1", "open_page", {"page": "apps"},
                              expected_version="navigation:1",
                              idempotency_key="after-reinspect")
            self.assertEqual(unlocked["status"], "queued")
        finally:
            rt.close()

class AgentOpensOwnTab(unittest.TestCase):
    """Agent 自行开页的两档授权(2026-09-02)。

    逐标签点「交给 Agent」挡的不是「操作网页」,是「用你的身份操作网页」——Desk 内置
    浏览器用持久且全标签共享的存储,任何新标签一开就带着用户的内网 SSO。所以按身份分档:
    隔离标签没有用户身份,能力上限等于一个刚装好的浏览器,无需任何人点头;带身份的
    仍要人点头,但粒度从「每个标签」放宽到「每个站点」。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = AppControlRuntime(os.path.join(self.tmp.name, "rt.sqlite3"),
                                    apps_provider=lambda: APPS)

    def tearDown(self):
        self.rt.close()
        self.tmp.cleanup()

    def test_隔离标签无需点头且租约自签(self):
        op = self.rt.open_tab("s1", "browser", "https://example.com/x", isolated=True)
        self.assertEqual(op["status"], "queued")            # 不进 waiting_user
        self.assertEqual(op["approval_state"], "not_required")
        self.assertTrue(op["resource_id"].startswith("agent-"))
        lease = self.rt.get_lease(op["lease_id"])
        self.assertEqual(lease["status"], "active")         # 无需 offer→claim 两步
        self.assertEqual(lease["kind"], "agent_isolated")
        self.assertTrue(self.rt.next_operation("browser", op["resource_id"]))

    def test_主框架域名白名单只能收紧当前站点(self):
        op = self.rt.open_tab("s1", "browser", "https://html.duckduckgo.com/html/",
                              isolated=True, allowed_main_frame_hosts=["html.duckduckgo.com"])
        self.assertEqual(op["input"]["allowed_main_frame_hosts"], ["html.duckduckgo.com"])
        with self.assertRaises(AppControlError):
            self.rt.open_tab("s1", "browser", "https://html.duckduckgo.com/html/",
                             isolated=True, allowed_main_frame_hosts=["example.com"])
        with self.assertRaises(AppControlError):
            self.rt.open_tab("s1", "browser", "https://html.duckduckgo.com/html/",
                             isolated=True, allowed_main_frame_hosts="html.duckduckgo.com")

    def test_Agent隔离标签关闭免审批(self):
        op = self.rt.open_tab("s1", "browser", "https://example.com/x", isolated=True)
        closed = self.rt.act(op["lease_id"], "s1", "close", {}, idempotency_key="close-1")
        self.assertEqual(closed["status"], "queued")
        self.assertEqual(closed["approval_state"], "not_required")

    def test_用户委托标签关闭仍需审批(self):
        self.rt.offer("browser", "tab-close", "window-1")
        lease = self.rt.claim("s1", "browser", "tab-close")
        closed = self.rt.act(lease["id"], "s1", "close", {}, idempotency_key="close-2")
        self.assertEqual(closed["status"], "waiting_user")

    def test_带身份首次要人批(self):
        op = self.rt.open_tab("s1", "browser", "https://cloud.example.net/a", isolated=False)
        self.assertEqual(op["status"], "waiting_user")
        self.assertIsNone(self.rt.next_operation("browser", op["resource_id"]))
        self.assertTrue(op["resource_id"].startswith("user-"))
        self.assertEqual(self.rt.get_lease(op["lease_id"])["kind"], "agent_identified")

    def test_批准即授权同站不再问(self):
        first = self.rt.open_tab("s1", "browser", "https://cloud.example.net/a", isolated=False)
        self.rt.approve(first["id"], True, "native_user")
        self.assertEqual([g["origin"] for g in self.rt.site_grants("browser")],
                         ["https://cloud.example.net"])
        again = self.rt.open_tab("s1", "browser", "https://cloud.example.net/b", isolated=False)
        self.assertEqual(again["status"], "queued")          # 同站不再问
        other = self.rt.open_tab("s1", "browser", "https://elsewhere.example/x", isolated=False)
        self.assertEqual(other["status"], "waiting_user")    # 换站点仍要问

    def test_拒绝不留授权(self):
        op = self.rt.open_tab("s1", "browser", "https://cloud.example.net/a", isolated=False)
        self.rt.approve(op["id"], False, "native_user")
        self.assertEqual(self.rt.site_grants("browser"), [])

    def test_授权按origin不按域名(self):
        op = self.rt.open_tab("s1", "browser", "https://cloud.example.net/a", isolated=False)
        self.rt.approve(op["id"], True, "native_user")
        # http 与 https、不同端口是不同的信任面,不能被一次授权覆盖
        plain = self.rt.open_tab("s1", "browser", "http://cloud.example.net/a", isolated=False)
        self.assertEqual(plain["status"], "waiting_user")

    def test_授权会过期(self):
        base = time.time()
        op = self.rt.open_tab("s1", "browser", "https://cloud.example.net/a", isolated=False)
        self.rt.approve(op["id"], True, "native_user")
        self.rt.clock = lambda: base + 86400 + 60           # 默认一天
        self.assertEqual(self.rt.site_grants("browser"), [])
        again = self.rt.open_tab("s1", "browser", "https://cloud.example.net/b", isolated=False)
        self.assertEqual(again["status"], "waiting_user")

    def test_可撤销单个站点授权(self):
        first = self.rt.open_tab("s1", "browser", "https://cloud.example.net/a", isolated=False)
        self.rt.approve(first["id"], True, "native_user")
        other = self.rt.open_tab("s1", "browser", "https://elsewhere.example/a", isolated=False)
        self.rt.approve(other["id"], True, "native_user")

        result = self.rt.revoke_site("browser", "https://cloud.example.net/path")

        self.assertTrue(result["revoked"])
        self.assertEqual([g["origin"] for g in self.rt.site_grants("browser")],
                         ["https://elsewhere.example"])
        again = self.rt.open_tab("s1", "browser", "https://cloud.example.net/b", isolated=False)
        self.assertEqual(again["status"], "waiting_user")

    def test_只放行http和https(self):
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "", "ftp://x/y"):
            with self.assertRaises(AppControlError):
                self.rt.open_tab("s1", "browser", bad, isolated=True)

    def _with_screenshot(self):
        control = APPS[0]["agent_control"]
        control["observations"].append(
            {"name": "screenshot", "schema": "desk.image/v1", "sensitive": True,
             "redaction": "visible-sensitive-elements"})
        self.addCleanup(control["observations"].pop)
        return control

    def test_隔离标签截图免逐次审批(self):
        self._with_screenshot()
        op = self.rt.open_tab("s1", "browser", "https://example.com/x", isolated=True)
        shot = self.rt.inspect(op["lease_id"], "s1", "screenshot")
        self.assertEqual(shot["status"], "queued")
        self.assertEqual(shot["approval_state"], "not_required")

    def test_带身份标签截图仍逐次审批(self):
        self._with_screenshot()
        op = self.rt.open_tab("s1", "browser", "https://cloud.example.net/a", isolated=False)
        self.rt.approve(op["id"], True, "native_user")      # 站点授权只免开页那一问
        shot = self.rt.inspect(op["lease_id"], "s1", "screenshot")
        self.assertEqual(shot["status"], "waiting_user")
        self.assertEqual(shot["approval_state"], "pending")

    def test_用户委托标签截图仍逐次审批(self):
        self._with_screenshot()
        self.rt.offer("browser", "tab-9", "window-1")
        lease = self.rt.claim("s1", "browser", "tab-9")
        shot = self.rt.inspect(lease["id"], "s1", "screenshot")
        self.assertEqual(shot["status"], "waiting_user")

    def test_脱敏声明不受审批分级影响(self):
        control = self._with_screenshot()
        spec = control["observations"][-1]
        op = self.rt.open_tab("s1", "browser", "https://example.com/x", isolated=True)
        self.rt.inspect(op["lease_id"], "s1", "screenshot")
        self.assertEqual(spec["redaction"], "visible-sensitive-elements")
        self.assertTrue(spec["sensitive"])
