"""安排管理面：列表、创建、测试与启停。"""
from io import BytesIO
import json
import os
import sys
from unittest import mock


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    from arrangement import api, store
    from larkbot import store as agent_store
finally:
    sys.argv = _argv


class H:
    def __init__(self, body=None):
        raw = json.dumps(body or {}).encode()
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = BytesIO(raw)
        self.out = None
        self.code = 0

    def _send_json(self, obj, code=200):
        self.out, self.code = obj, code
        return obj


def _call(path, method="GET", body=None):
    h = H(body)
    api.handle(h, path, "", method)
    return h


def _agent(root):
    saved, err = agent_store.save(root, "weekly-agent", {"name": "周报 Agent", "kind": "agent", "engine": "codex"})
    assert saved and not err


def test_create_list_toggle_and_delete(tmp_path, monkeypatch):
    root = str(tmp_path)
    _agent(root)
    monkeypatch.setattr(api, "REPO_ROOT", root)
    monkeypatch.setattr(api, "_me", lambda: "desktop")
    with mock.patch.object(api.peers, "is_hop", return_value=True):
        made = _call(api.PREFIX, "POST", {"instruction": "汇总昨天的改动", "agent_id": "weekly-agent",
                                           "trigger": {"kind": "weekdays", "time": "09:00"}})
        assert made.code == 200 and made.out["ok"]
        aid = made.out["item"]["id"]
        listed = _call(api.PREFIX)
        assert [x["id"] for x in listed.out["items"]] == [aid]
        paused = _call(f"{api.PREFIX}/{aid}/toggle", "POST")
        assert paused.out["item"]["enabled"] is False
        deleted = _call(f"{api.PREFIX}/{aid}", "DELETE")
        assert deleted.out == {"ok": True}


def test_test_run_uses_unsaved_declaration(tmp_path, monkeypatch):
    root = str(tmp_path)
    _agent(root)
    monkeypatch.setattr(api, "REPO_ROOT", root)
    with mock.patch.object(api.peers, "is_hop", return_value=True), \
         mock.patch.object(api.scheduler, "start_run", return_value=({"id": "run_test"}, "")) as start:
        got = _call(api.PREFIX + "/test", "POST", {"instruction": "检查索引", "agent_id": "weekly-agent"})
    assert got.code == 202 and got.out["run"]["id"] == "run_test"
    assert start.call_args.kwargs["test"] is True
    assert store.list_all(root) == []


def test_create_without_agent_uses_desk_identity(tmp_path, monkeypatch):
    root = str(tmp_path)
    monkeypatch.setattr(api, "REPO_ROOT", root)
    monkeypatch.setattr(api, "_me", lambda: "desktop")
    with mock.patch.object(api.peers, "is_hop", return_value=True):
        made = _call(api.PREFIX, "POST", {
            "instruction": "汇总昨天的改动",
            "trigger": {"kind": "daily", "time": "09:00"},
        })
    assert made.code == 200 and made.out["ok"]
    assert made.out["item"]["agent_id"] == "_desk"


def test_session_id_survives_create_and_edit(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "REPO_ROOT", str(tmp_path))
    with mock.patch.object(api.peers, "is_hop", return_value=True):
        made = _call(api.PREFIX, "POST", {
            "instruction": "整理当前会话",
            "session_id": "adp_context_rail",
            "trigger": {"kind": "manual"},
        })
        assert made.code == 200
        row = made.out["item"]
        assert row["session_id"] == "adp_context_rail"
        edited = _call(f"{api.PREFIX}/{row['id']}", "PUT", {"instruction": "继续整理"})
    assert edited.out["item"]["session_id"] == "adp_context_rail"


def test_new_agent_payload_cannot_bypass_existing_agent_check(tmp_path, monkeypatch):
    root = str(tmp_path)
    monkeypatch.setattr(api, "REPO_ROOT", root)
    monkeypatch.setattr(api, "_me", lambda: "desktop")
    body = {
        "instruction": "整理项目周报",
        "agent_id": "agent-weekly123",
        "new_agent": {"id": "agent-weekly123", "name": "周报助手"},
        "trigger": {"kind": "weekly", "weekday": 4, "time": "17:00"},
    }
    with mock.patch.object(api.peers, "is_hop", return_value=True):
        made = _call(api.PREFIX, "POST", body)
    assert made.code == 422 and not made.out["ok"]
    assert "没有这个 Agent" in made.out["error"]
    assert agent_store.load(root, "agent-weekly123") is None


def test_unknown_agent_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "REPO_ROOT", str(tmp_path))
    with mock.patch.object(api.peers, "is_hop", return_value=True):
        got = _call(api.PREFIX, "POST", {"instruction": "做一次", "agent_id": "missing"})
    assert got.code == 422
    assert "没有这个 Agent" in got.out["error"]


def test_existing_lark_bot_can_be_bound_and_tested(tmp_path, monkeypatch):
    root = str(tmp_path)
    saved, err = agent_store.save(root, "babyshark", {"name": "BabyShark", "engine": "codex"})
    assert saved and not err
    monkeypatch.setattr(api, "REPO_ROOT", root)
    with mock.patch.object(api.peers, "is_hop", return_value=True):
        got = _call(api.PREFIX, "POST", {"instruction": "做一次", "agent_id": "babyshark"})
    assert got.code == 200 and got.out["item"]["agent_id"] == "babyshark"
    aid = got.out["item"]["id"]
    with mock.patch.object(api.peers, "is_hop", return_value=True), \
         mock.patch.object(api.scheduler, "start_run", return_value=({"id": "run_bot"}, "")) as start:
        edited = _call(f"{api.PREFIX}/{aid}", "PUT", {"instruction": "检查 Bot 配置"})
        tested = _call(api.PREFIX + "/test", "POST", {"instruction": "检查", "agent_id": "babyshark"})
    assert edited.code == 200 and edited.out["item"]["agent_id"] == "babyshark"
    assert tested.code == 202
    assert start.call_args.args[1]["agent_id"] == "babyshark"


def test_explicit_desk_identity_can_be_used_for_new_arrangement(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "REPO_ROOT", str(tmp_path))
    with mock.patch.object(api.peers, "is_hop", return_value=True):
        got = _call(api.PREFIX, "POST", {"instruction": "做一次", "agent_id": "_desk"})
    assert got.code == 200 and got.out["ok"]
    assert got.out["item"]["agent_id"] == "_desk"


def test_desk_identity_can_test_an_unsaved_arrangement(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "REPO_ROOT", str(tmp_path))
    with mock.patch.object(api.peers, "is_hop", return_value=True), \
         mock.patch.object(api.scheduler, "start_run", return_value=({"id": "run_desk"}, "")) as start:
        got = _call(api.PREFIX + "/test", "POST", {"instruction": "检查索引"})
    assert got.code == 202 and got.out["run"]["id"] == "run_desk"
    assert start.call_args.args[1]["agent_id"] == "_desk"
    assert start.call_args.kwargs["test"] is True


def test_custom_repeat_survives_create_read_and_edit(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "REPO_ROOT", str(tmp_path))
    trigger = {"kind": "custom", "unit": "week", "every": 2, "weekdays": [4, 0],
               "start_date": "2090-01-01", "end_date": "2090-12-31", "time": "10:35"}
    with mock.patch.object(api.peers, "is_hop", return_value=True):
        made = _call(api.PREFIX, "POST", {"instruction": "整理周报", "trigger": trigger})
        assert made.code == 200
        row = made.out["item"]
        trigger["weekdays"] = [0, 4]
        assert row["trigger"] == trigger
        assert row["next_at"] > 0
        listed = _call(api.PREFIX)
        assert listed.out["items"][0]["trigger"] == trigger
        edited = _call(f"{api.PREFIX}/{row['id']}", "PUT", {"instruction": "整理详细周报"})
        assert edited.code == 200
        assert edited.out["item"]["trigger"] == trigger
        assert edited.out["item"]["next_at"] == row["next_at"]


def test_expired_custom_repeat_cannot_be_resumed(tmp_path, monkeypatch):
    root = str(tmp_path)
    monkeypatch.setattr(api, "REPO_ROOT", root)
    row = store.create(root, {"instruction": "每日检查", "agent_id": "_desk", "enabled": False,
        "trigger": {"kind": "custom", "unit": "day", "every": 1, "start_date": "2000-01-01",
                    "end_date": "2000-01-02", "time": "09:00"}, "next_at": 0})
    with mock.patch.object(api.peers, "is_hop", return_value=True):
        got = _call(f"{api.PREFIX}/{row['id']}/toggle", "POST")
    assert got.code == 422 and "已结束" in got.out["error"]
    assert store.get(root, row["id"])["enabled"] is False


def test_expired_once_cannot_be_resumed_silently(tmp_path, monkeypatch):
    root = str(tmp_path)
    monkeypatch.setattr(api, "REPO_ROOT", root)
    row = store.create(root, {"instruction": "一次", "agent_id": "_desk", "enabled": False,
        "trigger": {"kind": "once", "date": "2000-01-01", "time": "09:00"}, "next_at": 0})
    with mock.patch.object(api.peers, "is_hop", return_value=True):
        got = _call(f"{api.PREFIX}/{row['id']}/toggle", "POST")
    assert got.code == 422 and "时间已过" in got.out["error"]
    assert store.get(root, row["id"])["enabled"] is False
