"""make-arrangement 技能的参数与本地 API 契约。"""
import importlib.util
import json
from pathlib import Path
import sys
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".claude/skills/make-arrangement/scripts/create_arrangement.py"
SPEC = importlib.util.spec_from_file_location("make_arrangement_script", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Response:
    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.data


def test_script_omits_agent_to_use_desk_and_preserves_schedule(monkeypatch, capsys):
    monkeypatch.delenv("DESKTRON_SESSION_ID", raising=False)
    captured = {}

    def open_request(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response({"ok": True, "item": {"id": "arr_1", "name": "晨报",
                                                       "backend": "desktop", "agent_id": "_desk",
                                                       "trigger": {"kind": "daily", "time": "09:00"}}})

    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--name", "晨报", "--instruction",
                                        "汇总昨天的变化", "--kind", "daily", "--time", "09:00"])
    with mock.patch.object(MODULE.urllib.request, "urlopen", side_effect=open_request):
        assert MODULE.main() == 0
    assert captured == {
        "url": "http://127.0.0.1:7765/workbench/arrangements",
        "body": {"name": "晨报", "instruction": "汇总昨天的变化",
                 "trigger": {"kind": "daily", "time": "09:00"}, "enabled": True},
        "timeout": 20,
    }
    assert json.loads(capsys.readouterr().out)["agent_id"] == "_desk"


def test_script_associates_arrangement_with_current_session(monkeypatch):
    monkeypatch.setenv("DESKTRON_SESSION_ID", "adp_current")
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--instruction", "检查", "--kind", "manual"])
    with mock.patch.object(MODULE.urllib.request, "urlopen", return_value=Response({"ok": True})) as send:
        assert MODULE.main() == 0
    assert json.loads(send.call_args.args[0].data)["session_id"] == "adp_current"


def test_script_sends_once_and_interval_with_existing_bot(monkeypatch):
    for flags, trigger in [
        (["--kind", "once", "--date", "2030-09-08", "--time", "14:35"],
         {"kind": "once", "date": "2030-09-08", "time": "14:35"}),
        (["--kind", "interval", "--minutes", "30"], {"kind": "interval", "minutes": 30}),
    ]:
        monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--instruction", "检查",
                                            "--agent-id", "demo-bot", "--backend", "server", *flags])
        with mock.patch.object(MODULE.urllib.request, "urlopen", return_value=Response({"ok": True})) as send:
            assert MODULE.main() == 0
        req = send.call_args.args[0]
        assert req.full_url.endswith("?backend=server")
        body = json.loads(req.data)
        assert body["trigger"] == trigger
        assert body["agent_id"] == "demo-bot"
