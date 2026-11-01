import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".claude/skills/pi-agent-setup/scripts/pi_setup.py"


def _module():
    spec = importlib.util.spec_from_file_location("pi_setup_skill", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scrub_removes_credentials_and_normalizes_home():
    mod = _module()
    value = mod.scrub(f"{Path.home()}/x api_key=secret-value Authorization: Bearer token-value")
    assert str(Path.home()) not in value
    assert "secret-value" not in value
    assert "token-value" not in value
    assert value.count("[REDACTED]") == 2


def test_missing_pi_plan_stops_before_provider_setup(monkeypatch):
    mod = _module()
    checks = {
        "node": {"ok": True}, "npm": {"ok": True}, "pi": {"ok": False},
        "catalog": {"ok": False},
    }
    actions = mod.next_actions(checks)
    assert [item["id"] for item in actions] == ["install-pi"]
    assert "@earendil-works/pi-coding-agent@latest" in actions[0]["command"]
    assert "--registry=https://registry.npmjs.org/" in actions[0]["command"]


def test_incompatible_pi_plan_requires_repair_before_provider(monkeypatch):
    mod = _module()
    checks = {"node":{"ok":True}, "npm":{"ok":True}, "pi":{"ok":True},
              "pi_protocol":{"ok":False}, "catalog":{"ok":False}}
    actions = mod.next_actions(checks)
    assert [item["id"] for item in actions] == ["repair-pi"]
    assert "@latest" in actions[0]["command"]


def test_snapshot_never_marks_incompatible_pi_ready(monkeypatch):
    mod = _module()
    monkeypatch.setattr(mod, "binary", lambda name, _args: {"name":name, "ok":True})
    monkeypatch.setattr(mod, "pi_protocol", lambda _ok: {"ok":False, "state":"repair_required"})
    monkeypatch.setattr(mod, "model_catalog", lambda _ok: {"ok":False, "state":"blocked"})
    value = mod.snapshot()
    assert value["state"] == "needs_repair"
    assert value["next_actions"][0]["id"] == "repair-pi"


def test_report_is_draft_until_forum_support_exists(monkeypatch):
    mod = _module()
    monkeypatch.setattr(mod, "snapshot", lambda: {
        "state": "needs_user", "platform": {}, "checks": {}, "next_actions": []})
    args = type("Args", (), {"error_code": "pi/auth", "summary": "登录失败 secret=abc123"})()
    draft = mod.report_draft(args)
    assert draft["ready_to_submit"] is False
    assert "secret=abc123" not in json.dumps(draft, ensure_ascii=False)
    assert "PI-AUTH" in draft["title"]
