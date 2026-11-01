"""Agent Control manifest 的权限相关字段必须 fail closed。"""
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

from app_control.manifest import validate_agent_control, validate_value


class ManifestValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "frontend", "workbench.json"), encoding="utf-8") as f:
            manifest = json.load(f)
            apps = manifest["desktopApps"]
        cls.browser = next(a for a in apps if a["id"] == "browser")["agent_control"]
        cls.desk = next(a for a in manifest["hostApps"] if a["id"] == "desk")["agent_control"]

    def test_browser_manifest_is_valid(self):
        self.assertEqual(validate_agent_control(self.browser), [])

    def test_desk_manifest_is_valid_and_has_no_agent_owned_resource(self):
        self.assertEqual(validate_agent_control(self.desk), [])
        scopes = {resource["scope"] for resource in self.desk["resources"]}
        self.assertEqual(scopes, {"delegated-current-surface"})
        observations = {row["name"]: row for row in self.desk["observations"]}
        self.assertEqual(set(observations), {"surface", "screenshot"})
        self.assertTrue(observations["screenshot"]["sensitive"])
        self.assertEqual(observations["screenshot"]["redaction"], "desk-protected")
        self.assertEqual({row["name"] for row in self.desk["actions"]},
                         {"open_page", "toggle_panel"})
        self.assertTrue(all(row["effect"] == "reversible_write"
                            for row in self.desk["actions"]))

    def test_external_write_cannot_lower_approval_or_idempotency(self):
        control = json.loads(json.dumps(self.browser))
        click = next(a for a in control["actions"] if a["name"] == "click")
        click["approval"] = "risk_based"
        click["idempotency"] = "keyed"
        errors = validate_agent_control(control)
        self.assertTrue(any("approval=always" in e for e in errors), errors)
        self.assertTrue(any("幂等键" in e for e in errors), errors)

    def test_native_application_requires_exact_identity_and_forbids_global_desktop_scope(self):
        control = json.loads(json.dumps(self.desk))
        control["native_application"] = {
            "bundle_id": "com.example.fixture",
            "target_binding": ["bundle_id", "pid", "window_id"],
            "code_requirement": 'anchor apple generic and identifier "com.example.fixture"',
            "projector": "ax-structural/v1",
            "allowed_roles": ["AXWindow"],
            "allowed_semantics": ["window"],
            "allowed_values": ["on"],
        }
        control["resources"] = [{
            "type": "native-window", "scope": "delegated-application-window"}]
        control["observations"] = [{"name": "window", "schema": "desk.native-window/v1"}]
        self.assertEqual(validate_agent_control(control), [])
        # 第一切片的最小声明已不足以开放观察：必须固定签名要求与投影白名单。
        minimal = json.loads(json.dumps(control))
        for key in ("code_requirement", "projector", "allowed_roles"):
            minimal["native_application"].pop(key)
        self.assertEqual(len(validate_agent_control(minimal)), 3)
        control["native_application"]["bundle_id"] = "*"
        self.assertTrue(any("bundle_id" in e for e in validate_agent_control(control)))
        control["native_application"]["bundle_id"] = "com.example.fixture"
        control["resources"][0]["scope"] = "global-desktop"
        self.assertTrue(any("delegated-application-window" in e
                            for e in validate_agent_control(control)))
        control["resources"][0]["scope"] = "delegated-application-window"
        control["native_application"]["target_binding"] = ["bundle_id"]
        self.assertTrue(any("精确绑定" in e for e in validate_agent_control(control)))

    def test_action_names_must_be_unique(self):
        control = json.loads(json.dumps(self.browser))
        control["actions"].append(dict(control["actions"][0]))
        self.assertTrue(any("名称重复" in e for e in validate_agent_control(control)))

    def test_input_schema_rejects_missing_and_unknown_fields(self):
        schema = next(a for a in self.browser["actions"] if a["name"] == "fill")["input_schema"]
        errors = validate_value(schema, {"node_id": "n1", "extra": 1})
        self.assertTrue(any("value" in e and "缺失" in e for e in errors), errors)
        self.assertTrue(any("extra" in e and "未声明" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
