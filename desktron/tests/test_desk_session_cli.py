"""面向 Agent 的会话管理命令。"""
import json
import runpy
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "bin" / "desk-session"


def load():
    return runpy.run_path(str(SOURCE), run_name="test_desk_session")


def test_rename_defaults_to_current_session(capsys):
    module = load()
    calls = []
    module["main"].__globals__.update(
        ME="adp/current", _post=lambda path, body: calls.append((path, body)) or {
            "ok": True, "session_id": "adp/current", "title": "短名称"})
    assert module["main"](["rename", " 短名称 "]) == 0
    assert calls == [("/workbench/sessions/adp%2Fcurrent/title", {"title": " 短名称 "})]
    assert json.loads(capsys.readouterr().out) == {
        "ok": True, "session_id": "adp/current", "title": "短名称"}


def test_rename_can_target_explicit_session(capsys):
    module = load()
    calls = []
    module["main"].__globals__.update(
        ME="", _post=lambda path, body: calls.append((path, body)) or {
            "ok": True, "session_id": "other", "title": "新名称"})
    assert module["main"](["rename", "新名称", "--session", "other"]) == 0
    assert calls[0][0] == "/workbench/sessions/other/title"


def test_rename_requires_session_identity(capsys):
    module = load()
    module["main"].__globals__["ME"] = ""
    assert module["main"](["rename", "新名称"]) == 2
    assert "--session" in capsys.readouterr().err


def test_prepare_installs_desk_session():
    installer = (SOURCE.parent / "desktron").read_text(encoding="utf-8")
    assert "desk-msg desk-session desk-task" in installer
