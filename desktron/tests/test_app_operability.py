"""新应用不能因创建入口不同而漏掉 Agent 可操作性声明。"""
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

from app_control.manifest import validate_app_operability
from workbench import _app_validate


class AppOperabilityValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "frontend", "workbench.json"), encoding="utf-8") as f:
            cls.apps = json.load(f)["desktopApps"]
        cls.browser = next(a for a in cls.apps if a["id"] == "browser")

    def test_official_apps_make_an_explicit_choice(self):
        for app in self.apps:
            self.assertEqual(validate_app_operability(app, required=True), [], app["id"])

    def test_new_registration_rejects_an_omitted_choice(self):
        app = {"id": "x", "label": "X", "agentic": True,
               "who": "role", "what": "output", "how": "steps"}
        errors = _app_validate(app, require_operability=True)
        self.assertTrue(any("agent_operability 缺失" in e for e in errors), errors)

    def test_legacy_validation_can_read_an_omitted_choice(self):
        app = {"id": "x", "label": "X", "agentic": True,
               "who": "role", "what": "output", "how": "steps"}
        self.assertFalse(any("agent_operability" in e for e in _app_validate(app)))

    def test_legacy_control_is_still_validated(self):
        app = {"agent_control": {}}
        self.assertTrue(validate_app_operability(app, required=False))

    def test_none_requires_a_reason_and_forbids_control(self):
        app = {"agent_operability": "none", "agent_control": self.browser["agent_control"]}
        errors = validate_app_operability(app, required=True)
        self.assertTrue(any("必须填写" in e for e in errors), errors)
        self.assertTrue(any("不得声明" in e for e in errors), errors)

    def test_control_level_must_match_choice(self):
        app = json.loads(json.dumps(self.browser))
        app["agent_operability"] = "L2"
        errors = validate_app_operability(app, required=True)
        self.assertTrue(any("必须与" in e for e in errors), errors)


class CreationEntrypoints(unittest.TestCase):
    def test_vibing_prompt_and_skill_both_require_the_choice(self):
        with open(os.path.join(ROOT, "frontend", "app-assets-create.js"), encoding="utf-8") as f:
            prompt = f.read()
        with open(os.path.join(ROOT, ".claude", "skills", "make-app", "SKILL.md"), encoding="utf-8") as f:
            skill = f.read()
        self.assertIn("agent_operability:none/L1/L2/L3/L4", prompt)
        self.assertIn("Agent 可操作性（每个应用都必须选择）", skill)

    def test_every_new_write_path_uses_strict_validation(self):
        for rel in ("desktron/lib/app/api.py", "desktron/lib/pkgstore.py"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                self.assertIn("require_operability=True", f.read(), rel)


if __name__ == "__main__":
    unittest.main()
