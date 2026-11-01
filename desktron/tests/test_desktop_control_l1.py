"""Desktop Control L1：人选定原生窗口后的只读观察链路。"""
import copy
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
if "deskcfg" not in sys.modules:
    sys.argv = ["test", os.path.join(ROOT, "frontend"), "0", "0", tempfile.mkdtemp()]

from app_control.manifest import validate_agent_control
from app_control.runtime import AppControlError, AppControlRuntime

BUNDLE = "com.example.native"
REQUIREMENT = 'anchor apple generic and identifier "com.example.native"'
L1 = {
    "protocol": "desk.app-control/v1alpha1",
    "identity": {"app": "test.native", "version": "1", "adapter": "macos-accessibility/v1"},
    "level": "L1",
    "native_application": {
        "bundle_id": BUNDLE,
        "target_binding": ["bundle_id", "pid", "window_id"],
        "code_requirement": REQUIREMENT,
        "projector": "ax-structural/v1",
        "allowed_roles": ["AXWindow", "AXCheckBox", "AXStaticText"],
        "allowed_semantics": ["window", "toggle", "text"],
        "allowed_values": ["on", "off", "status_connected"],
        "label_values": {"Connected": "status_connected"},
    },
    "resources": [{"type": "native-window", "scope": "delegated-application-window"}],
    "observations": [{"name": "window", "schema": "desk.native-window/v1"}],
    "actions": [],
    "permissions": {"delegation": "explicit", "lease": "exclusive",
                    "user_interaction": "pause", "takeover": "user_confirmed"},
    "events": ["ready", "changed", "action_completed", "failed", "waiting_user", "lease_revoked"],
    "receipts": {"required": True, "undo_when_declared": True},
}
HOST = "host-one"
HOST_PID = 1234
GOOD = {"nodes": [
    {"ref": "n0", "role": "AXWindow", "semantic": "window"},
    {"ref": "n1", "parent": "n0", "role": "AXCheckBox", "semantic": "toggle",
     "value": "on", "enabled": True},
    {"ref": "n2", "parent": "n0", "role": "AXStaticText", "semantic": "text",
     "value": "status_connected"},
], "truncated": False}


def app(control=None, builtin=True):
    row = {"id": "native", "label": "Native", "agent_control": copy.deepcopy(control or L1)}
    if builtin:
        row["_native_manifest_source"] = "builtin"
    return row


class DesktopL1(unittest.TestCase):
    def setUp(self):
        # 在 Desk 内跑测试时会继承真实宿主身份；这里需要模拟宿主换代。
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DESKTRON_HOST_INSTANCE_ID", None)
        os.environ.pop("DESKTRON_NATIVE_LAUNCH", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "rt.sqlite3")
        self.now = [1000.0]
        self.apps = [app()]
        self.rt = self.open()

    def open(self):
        return AppControlRuntime(self.path, apps_provider=lambda: self.apps,
                                 clock=lambda: self.now[0],
                                 evidence_dir=os.path.join(self.tmp.name, "ev"))

    def tearDown(self):
        self.rt.close()
        self.tmp.cleanup()

    def ready(self, instance=HOST, status="trusted", monitoring="ready"):
        self.now[0] += 0.001
        return self.rt.report_desktop_capability(
            instance, "io.github.desktron", HOST_PID, status,
            monitoring_status=monitoring)

    def select(self, session="s1", **kw):
        args = dict(app_id="native", bundle_id=BUNDLE, pid=4321,
                    process_start_token="1700000000.123456", code_requirement=REQUIREMENT,
                    window_id="88", host_instance_id=HOST, host_pid=HOST_PID,
                    session_id=session)
        args.update(kw)
        return self.rt.select_desktop_target(**args)

    def claimed(self):
        self.ready()
        out = self.select()
        lease = self.rt.claim("s1", "native")
        self.assertEqual(lease["id"], out["lease"]["id"])
        return lease, out["target"]

    def dispatch(self, lease):
        op = self.rt.inspect(lease["id"], "s1", "window")
        work = self.rt.desktop_next_operation("native", HOST)
        self.assertEqual(work["operation"]["id"], op["id"])
        return op, work

    def finish(self, work, output=GOOD, ok=True, error="", **kw):
        target = work["target"]
        args = dict(dispatch_id=work["operation"]["dispatch_id"], host_instance_id=HOST,
                    target_generation=target["target_generation"],
                    interaction_epoch=target["interaction_epoch"])
        args.update(kw)
        return self.rt.desktop_complete(work["operation"]["id"], ok=ok, output=output,
                                        error=error, **args)

    def assertCode(self, code, fn, *a, **kw):
        with self.assertRaises(AppControlError) as ctx:
            fn(*a, **kw)
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))

    # ---- 建立目标 ----

    def test_selection_requires_trusted_host_with_intervention_monitor(self):
        self.assertCode(409, self.select)
        self.ready(monitoring="unavailable")
        self.assertCode(409, self.select)
        self.ready(status="not_trusted", monitoring="ready")
        self.assertCode(409, self.select)
        self.ready()
        self.assertCode(409, self.select, host_instance_id="other-host")
        self.now[0] += 31
        self.assertCode(409, self.select)
        self.assertEqual(self.rt.db.execute("SELECT COUNT(*) FROM leases").fetchone()[0], 0)

    def test_selection_validates_exact_identity(self):
        self.ready()
        self.assertCode(403, self.select, bundle_id="com.example.other")
        self.assertCode(403, self.select, code_requirement='identifier "com.example.native"')
        for bad in ({"pid": 1}, {"pid": HOST_PID}, {"pid": "x"},
                    {"process_start_token": ""}, {"process_start_token": "a b"},
                    {"window_id": "w-1"}, {"window_id": ""}, {"session_id": ""},
                    {"ttl_seconds": 5}, {"ttl_seconds": 901}):
            self.assertCode(422, self.select, **bad)
        self.assertEqual(self.rt.db.execute("SELECT COUNT(*) FROM leases").fetchone()[0], 0)

    def test_runtime_workbench_manifest_cannot_borrow_native_permission(self):
        self.apps = [app(builtin=False)]
        self.ready()
        self.assertCode(403, self.select)
        self.assertEqual(self.rt.desktop_apps_for_host(), [])
        self.apps = [app()]
        listed = self.rt.desktop_apps_for_host()
        self.assertEqual([(x["id"], x["bundle_id"], x["projector"]) for x in listed],
                         [("native", BUNDLE, "ax-structural/v1")])
        self.assertEqual(listed[0]["label_values"], {"Connected": "status_connected"})
        self.assertEqual(set(listed[0]["allowed_roles"]), {"AXWindow", "AXCheckBox", "AXStaticText"})

    def test_selection_is_session_bound_and_replaces_previous_target(self):
        self.ready()
        first = self.select()
        self.assertCode(403, self.rt.claim, "s2", "native")
        second = self.select(window_id="89")
        old = self.rt.get_lease(first["lease"]["id"])
        self.assertEqual((old["status"], old["revoked_reason"]),
                         ("revoked", "native_replaced_by_user_delegation"))
        self.assertNotEqual(first["target"]["target_generation"],
                            second["target"]["target_generation"])
        self.assertEqual(self.rt.claim("s1", "native")["id"], second["lease"]["id"])

    # ---- 观察 ----

    def test_read_only_observation_is_projected_and_versioned(self):
        lease, target = self.claimed()
        op, work = self.dispatch(lease)
        self.assertEqual((op["effect"], op["status"], op["approval_state"]),
                         ("read_only", "queued", "not_required"))
        self.assertEqual(work["target"]["pid"], 4321)
        self.assertEqual(work["target"]["code_requirement"], REQUIREMENT)
        done = self.finish(work)
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["output"]["schema"], "desk.native-window/v1")
        self.assertEqual([n["ref"] for n in done["output"]["nodes"]], ["n0", "n1", "n2"])
        self.assertTrue(done["after_version"].startswith(target["target_generation"][3:11]))
        res = self.rt.resources("s1", "native")
        self.assertEqual(res[0]["observation_version"], done["after_version"])
        # 迟到的重复回执不能改写已落地结果。
        again = self.finish(work, output={"nodes": [{"ref": "n0", "role": "AXWindow",
                                                     "semantic": "window"}]})
        self.assertEqual(again["output"], done["output"])

    def test_projection_rejects_any_unlisted_content(self):
        bad_outputs = [
            {"nodes": [{"ref": "n0", "role": "AXWindow", "semantic": "window",
                        "title": "secret-node.example.com"}]},
            {"nodes": [{"ref": "n0", "role": "AXTextField", "semantic": "window"}]},
            {"nodes": [{"ref": "n0", "role": "AXWindow", "semantic": "password"}]},
            {"nodes": [{"ref": "n0", "role": "AXStaticText", "semantic": "text",
                        "value": "vmess://abc"}]},
            {"nodes": [{"ref": "n1", "parent": "n0", "role": "AXWindow",
                        "semantic": "window"}]},
            {"nodes": [{"ref": "x", "role": "AXWindow", "semantic": "window"}]},
            {"nodes": [{"ref": "n0", "role": "AXWindow", "semantic": "window",
                        "enabled": "yes"}]},
            {"nodes": [{"ref": "n%d" % i, "role": "AXWindow", "semantic": "window"}
                       for i in range(201)]},
            {"nodes": [], "truncated": False},
            {"nodes": GOOD["nodes"], "screenshot": "data"},
            "raw text",
        ]
        lease, _ = self.claimed()
        for output in bad_outputs:
            _, work = self.dispatch(lease)
            done = self.finish(work, output=output)
            self.assertEqual((done["status"], done["error"], done["output"]),
                             ("failed", "native_observation_rejected", None), output)
        self.assertEqual(self.rt.resources("s1", "native")[0]["observation_version"], "")
        self.assertEqual(self.rt.get_lease(lease["id"])["status"], "active")

    def test_agent_cannot_pass_params_or_act_or_use_generic_host_paths(self):
        lease, _ = self.claimed()
        self.assertCode(422, self.rt.inspect, lease["id"], "s1", "window", params={"depth": 9})
        self.assertCode(404, self.rt.inspect, lease["id"], "s1", "screenshot")
        self.assertCode(409, self.rt.act, lease["id"], "s1", "click", {}, "", "k1")
        op, work = self.dispatch(lease)
        self.assertCode(409, self.rt.complete, op["id"], True, GOOD)
        self.assertCode(409, self.rt.next_operation, "native")
        self.assertEqual(self.rt.get_operation(op["id"])["status"], "running")
        self.assertEqual(self.finish(work)["status"], "succeeded")

    def test_host_errors_are_enumerated_and_identity_errors_revoke(self):
        lease, _ = self.claimed()
        _, work = self.dispatch(lease)
        done = self.finish(work, ok=False, output=None, error="AXTitle=node-password-123")
        self.assertEqual(done["error"], "native_observation_failed")
        self.assertNotIn("password", str(self.rt.get_operation(done["id"])))
        _, work = self.dispatch(lease)
        done = self.finish(work, ok=False, output=None, error="native_identity_changed")
        self.assertEqual(done["error"], "native_identity_changed")
        self.assertEqual(self.rt.get_lease(lease["id"])["status"], "revoked")
        self.assertCode(409, self.rt.inspect, lease["id"], "s1", "window")

    def test_receipt_must_match_dispatch_host_and_generation(self):
        lease, _ = self.claimed()
        op, work = self.dispatch(lease)
        stale = self.finish(work, dispatch_id="other")
        self.assertEqual(stale["status"], "running")
        done = self.finish(work, target_generation="tg_other")
        self.assertEqual(done["error"], "native_target_generation_changed")
        self.assertEqual(self.rt.get_lease(lease["id"])["status"], "revoked")
        lease, _ = self.claimed()
        _, work = self.dispatch(lease)
        done = self.finish(work, host_instance_id="impostor")
        self.assertEqual((done["status"], done["error"]), ("failed", "native_host_mismatch"))
        self.assertEqual(self.rt.get_lease(lease["id"])["status"], "revoked")

    # ---- 人介入与漂移 ----

    def test_user_interaction_invalidates_in_flight_receipt_until_reinspect(self):
        lease, target = self.claimed()
        op, work = self.dispatch(lease)
        after = self.rt.desktop_target_event(lease["id"], target["target_generation"],
                                             HOST, "user_interaction")
        self.assertEqual(after["interaction_epoch"], 1)
        self.assertEqual(self.rt.get_lease(lease["id"])["status"], "paused")
        self.assertEqual(self.rt.get_operation(op["id"])["status"], "cancelled")
        late = self.finish(work)
        self.assertEqual(late["status"], "cancelled")
        self.assertCode(409, self.rt.inspect, lease["id"], "s1", "window")
        self.rt.desktop_target_event(lease["id"], target["target_generation"], HOST, "user_idle")
        self.assertEqual(self.rt.get_lease(lease["id"])["status"], "active")
        _, work = self.dispatch(lease)
        self.assertEqual(work["target"]["interaction_epoch"], 1)
        self.assertEqual(self.finish(work)["status"], "succeeded")

    def test_epoch_change_between_dispatch_and_receipt_is_rejected(self):
        lease, target = self.claimed()
        _, work = self.dispatch(lease)
        self.rt.db.execute("UPDATE desktop_targets SET interaction_epoch=5 WHERE lease_id=?",
                           (lease["id"],))
        self.rt.db.commit()
        done = self.finish(work, interaction_epoch=5)
        self.assertEqual(done["error"], "human_intervention_reinspect_required")

    def test_generic_pause_also_advances_interaction_epoch(self):
        lease, target = self.claimed()
        _, work = self.dispatch(lease)
        self.rt.pause(lease["id"], "user_interaction")
        row = self.rt.db.execute("SELECT interaction_epoch FROM desktop_targets").fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(self.finish(work)["status"], "cancelled")

    def test_terminal_target_events_revoke_and_stale_generation_is_ignored(self):
        for event in ("identity_changed", "window_closed", "process_exited", "host_released"):
            lease, target = self.claimed()
            self.assertCode(409, self.rt.desktop_target_event, lease["id"], "tg_old",
                            HOST, event)
            self.assertCode(409, self.rt.desktop_target_event, lease["id"],
                            target["target_generation"], "other-host", event)
            self.assertEqual(self.rt.get_lease(lease["id"])["status"], "active")
            out = self.rt.desktop_target_event(lease["id"], target["target_generation"],
                                               HOST, event)
            self.assertEqual(out["status"], "invalidated")
            self.assertEqual(self.rt.get_lease(lease["id"])["revoked_reason"], "native_" + event)
            self.assertCode(409, self.rt.inspect, lease["id"], "s1", "window")
        self.assertCode(422, self.rt.desktop_target_event, lease["id"],
                        target["target_generation"], HOST, "click")

    def test_host_change_trust_loss_and_restart_invalidate_targets(self):
        lease, _ = self.claimed()
        self.ready(instance="host-two")
        self.assertEqual(self.rt.get_lease(lease["id"])["revoked_reason"], "native_host_mismatch")
        self.now[0] += 1
        lease, _ = self.claimed()
        self.ready(monitoring="unavailable")
        self.assertEqual(self.rt.get_lease(lease["id"])["revoked_reason"],
                         "native_host_capability_lost")
        lease, _ = self.claimed()
        self.rt.close()
        self.rt = self.open()
        self.assertEqual(self.rt.get_lease(lease["id"])["revoked_reason"], "runtime_restarted")
        row = self.rt.db.execute("SELECT status FROM desktop_targets WHERE lease_id=?",
                                 (lease["id"],)).fetchone()
        self.assertEqual(row[0], "invalidated")

    def test_stale_host_status_blocks_dispatch_and_manifest_change_revokes(self):
        lease, _ = self.claimed()
        op = self.rt.inspect(lease["id"], "s1", "window")
        self.now[0] += 31
        self.assertIsNone(self.rt.desktop_next_operation("native", HOST))
        self.assertCode(409, self.rt.inspect, lease["id"], "s1", "window")
        self.ready()
        changed = copy.deepcopy(L1)
        changed["native_application"]["allowed_values"].append("status_disconnected")
        self.apps = [app(changed)]
        self.assertIsNone(self.rt.desktop_next_operation("native", HOST))
        self.assertEqual(self.rt.get_lease(lease["id"])["revoked_reason"],
                         "native_manifest_changed")
        self.assertEqual(self.rt.get_operation(op["id"])["status"], "cancelled")

    def test_lease_expiry_invalidates_target(self):
        lease, _ = self.claimed()
        self.now[0] += 1000
        self.ready()
        self.assertEqual(self.rt.get_lease(lease["id"])["status"], "expired")
        row = self.rt.db.execute("SELECT status FROM desktop_targets").fetchone()
        self.assertEqual(row[0], "invalidated")

    def test_host_status_sync_reports_runtime_side_revocation(self):
        lease, target = self.claimed()
        generation = target["target_generation"]
        self.assertEqual(self.rt.desktop_target_status(lease["id"], generation, HOST)["status"],
                         "active")
        self.assertEqual(self.rt.desktop_target_status(lease["id"], "tg_x", HOST)["status"],
                         "unknown")
        self.assertEqual(self.rt.desktop_target_status(lease["id"], generation, "other")["status"],
                         "unknown")
        self.rt.yield_lease(lease["id"], "s1")
        out = self.rt.desktop_target_status(lease["id"], generation, HOST)
        self.assertEqual((out["status"], out["lease_status"]), ("invalidated", "revoked"))

    def test_probe_marks_untrusted_native_manifest_unavailable(self):
        self.apps = [app(builtin=False)]
        self.ready()
        probe = self.rt.probe("s1", "native")
        self.assertEqual(probe["control"]["execution_reason"], "native_manifest_untrusted")

    def test_probe_reports_native_delegation_state(self):
        probe = self.rt.probe("s1", "native")
        self.assertEqual(probe["control"]["execution_reason"], "accessibility_not_reported")
        self.ready()
        probe = self.rt.probe("s1", "native")
        self.assertEqual(probe["control"]["status"], "needs_authorization")
        self.assertEqual(probe["control"]["action"], "delegate_native_window")
        self.assertNotIn("self_open", probe)
        self.select()
        self.assertEqual(self.rt.probe("s1", "native")["control"]["status"],
                         "available_to_claim")


class NativeManifest(unittest.TestCase):
    def errors(self, mutate):
        control = copy.deepcopy(L1)
        mutate(control)
        return validate_agent_control(control)

    def test_valid_l1_manifest(self):
        self.assertEqual(validate_agent_control(L1), [])

    def test_l1_rejects_unsafe_declarations(self):
        cases = [
            lambda c: c["actions"].append({"name": "connect", "effect": "external_write",
                                           "approval": "always", "timeout_ms": 1000,
                                           "idempotency": "required"}),
            lambda c: c["native_application"].update(code_requirement="anchor apple generic"),
            lambda c: c["native_application"].update(projector="ocr/v1"),
            lambda c: c["native_application"]["label_values"].update({"x": "raw"}),
            lambda c: c["native_application"].update(allowed_values=[]),
            lambda c: c["native_application"].update(allowed_roles=["AXWindow", "AXWindow"]),
            lambda c: c["native_application"].update(dump_text=True),
            lambda c: c["observations"][0].update(schema="desk.native-screenshot/v1"),
            lambda c: c["observations"][0].update(input_schema={
                "type": "object", "properties": {"depth": {"type": "integer"}}}),
        ]
        for mutate in cases:
            self.assertTrue(self.errors(mutate), mutate)

    def test_builtin_shadowrocket_manifest_is_l1_read_only(self):
        import json
        with open(os.path.join(ROOT, "frontend", "workbench.json"), encoding="utf-8") as f:
            rows = json.load(f)["nativeApps"]
        control = next(r for r in rows if r["id"] == "shadowrocket")["agent_control"]
        self.assertEqual(validate_agent_control(control), [])
        self.assertEqual((control["level"], control["actions"]), ("L1", []))
        native = control["native_application"]
        self.assertEqual(native["bundle_id"], "com.liguangming.Shadowrocket")
        self.assertIn('identifier "com.liguangming.Shadowrocket"', native["code_requirement"])
        self.assertTrue(native["code_requirement"].startswith("anchor apple generic"))
        self.assertTrue(set(native["label_values"].values()) <= set(native["allowed_values"]))


NATIVE_SWIFT = open(os.path.join(ROOT, "desktop", "native", "native_control.swift"),
                    encoding="utf-8").read()
MAIN_SWIFT = open(os.path.join(ROOT, "desktop", "native", "main.swift"), encoding="utf-8").read()


class NativeHostSource(unittest.TestCase):
    def test_native_host_never_writes_ax_or_synthesizes_input(self):
        code = "\n".join(line for line in NATIVE_SWIFT.splitlines()
                         if not line.strip().startswith("//"))
        for forbidden in ("AXUIElementPerformAction", "AXUIElementSetAttributeValue",
                          "CGEventPost", "CGEvent(", "AXIsProcessTrustedWithOptions",
                          "kAXTrustedCheckOptionPrompt", "kCGWindowName", "osascript",
                          "NSAppleScript", "CGWindowListCreateImage", "SCScreenshot"):
            self.assertNotIn(forbidden, code, forbidden)

    def test_native_host_binds_identity_and_skips_secret_fields(self):
        for required in ("SecCodeCheckValidity", "PROC_PIDTBSDINFO", "kAXSecureTextFieldSubrole",
                         '"AXTextField"', "labelValues[", "maxNodes = 200",
                         "NativeIdentity.verify(target)", "let after = NativeIdentity.verify(target)",
                         "AXUIElementSetMessagingTimeout(element, 0.5)",
                         "/host/desktop-target-status?",
                         '"user_interaction"', '"user_idle"'):
            self.assertIn(required, NATIVE_SWIFT, required)
        # 监测只在委托期间安装，没有目标就卸载。
        self.assertIn("removeMonitor()", NATIVE_SWIFT.split("private func finishIfIdle()")[1][:400])

    def test_main_host_wires_bridge_capability_and_restart_reset(self):
        self.assertIn('cfg.userContentController.add(self, name: "nativeControl")', MAIN_SWIFT)
        self.assertIn('"monitoring_status": nativeControl.monitoringStatus', MAIN_SWIFT)
        ready = MAIN_SWIFT.split('self.startupLog("backend ready")')[1][:300]
        self.assertIn("nativeControl.resetForBackendRestart()", ready)
        with open(os.path.join(ROOT, "desktron", "bin", "desktron"), encoding="utf-8") as f:
            build = f.read()
        self.assertIn('"$NATIVE_CONTROL" "$SRC"', build)

    def test_frontend_entry_is_bundled_and_session_bound(self):
        with open(os.path.join(ROOT, "frontend", "app-native-control.js"), encoding="utf-8") as f:
            js = f.read()
        with open(os.path.join(ROOT, "desktron", "lib", "frontend_bundle.py"),
                  encoding="utf-8") as f:
            self.assertIn('"app-native-control.js"', f.read())
        with open(os.path.join(ROOT, "frontend", "app-settings-core.js"), encoding="utf-8") as f:
            self.assertNotIn("mountNativeControlSettings", f.read())
        self.assertIn('action: "resourceChanged"', js)
        self.assertIn("data-app-control-control-plane", js)
        self.assertNotRegex(js, r"AX[A-Z][a-z]")  # 页面不接触任何 AX 角色或属性

    @unittest.skipUnless(sys.platform == "darwin" and __import__("shutil").which("swiftc"),
                         "需要 macOS swiftc")
    def test_real_process_identity_primitives(self):
        import json
        import shutil
        import subprocess
        with tempfile.TemporaryDirectory(prefix="desk-native-identity-") as tmp:
            driver = os.path.join(tmp, "main.swift")
            shutil.copy(os.path.join(os.path.dirname(__file__), "fixtures",
                                     "native_identity_driver.swift"), driver)
            binary = os.path.join(tmp, "driver")
            subprocess.run(["swiftc", os.path.join(ROOT, "desktop", "native", "native_control.swift"),
                            driver, "-o", binary], check=True, capture_output=True, text=True,
                           timeout=240)
            result = subprocess.run([binary], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "token_stable": True, "token_distinct": True, "token_missing_for_dead": True,
            "requirement_ok": True, "requirement_wrong_id": False,
            "requirement_malformed": False, "no_windows": True})


if __name__ == "__main__":
    unittest.main()
