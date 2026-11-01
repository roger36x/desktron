"""Retiring Timeline must remove its execution paths, not just hide the icon."""
import ast
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "desktron/lib"
FRONT = ROOT / "frontend"


def route(relative):
    cfg = SimpleNamespace(REPO_ROOT="/synthetic-workspace", log=lambda *_: None)
    with patch.dict(sys.modules, {"deskcfg": cfg}):
        return runpy.run_path(str(LIB / relative))


def handler(body=None):
    raw = json.dumps(body or {}).encode("utf-8")
    return SimpleNamespace(headers={"Content-Length": str(len(raw))}, rfile=io.BytesIO(raw),
                           _send_json=lambda data, status=200: (data, status))


def test_retired_implementations_and_bundle_entry_are_gone():
    for name in ("lark_sync.py", "lark_events.py", "lark_topics.py", "lark_chatclass.py"):
        assert not (LIB / name).exists()
    assert not (FRONT / "app-rail-lark.js").exists()
    bundles = runpy.run_path(str(LIB / "frontend_bundle.py"))["ASSET_BUNDLES"]
    assert all("app-rail-lark.js" not in files for files in bundles.values())
    server = (LIB / "ui_server.py").read_text(encoding="utf-8")
    assert "_lark_sync_spawn" not in server
    assert "larkbot_api.start_background()" in server


@pytest.mark.parametrize("path", ["/workbench/lark-events", "/workbench/lark-events/focus",
                                   "/workbench/lark-events/attr"])
def test_event_routes_no_longer_claim_requests(path):
    api = route("feed/api.py")
    assert not api["owns"](path, "GET")
    assert api["handle"](handler(), path, "", "GET")[1] == 404
    assert api["owns"]("/workbench/lark-message", "GET")


@pytest.mark.parametrize("path", ["/workbench/lark-sync", "/workbench/lark-sync/start",
                                   "/workbench/lark-sync/stop"])
def test_sidecar_routes_cannot_restart_retired_agent(path):
    api = route("remote/api.py")
    assert not api["owns"](path, "POST")
    assert api["handle"](handler(), path, "", "POST")[1] == 404


def test_old_run_once_is_rejected_without_creating_a_thread():
    api = route("remote/api.py")
    with patch("threading.Thread", side_effect=AssertionError("must not start")):
        result, code = api["handle"](handler({"agent": "lark"}),
                                      "/workbench/agents/run-once", "", "POST")
    assert code == 422 and not result["ok"]


def test_memory_run_once_remains_available_without_running_it():
    api = route("remote/api.py")
    with patch("threading.Thread") as thread:
        result, code = api["handle"](handler({"agent": "memory"}),
                                      "/workbench/agents/run-once", "", "POST")
    assert code == 200 and result["started"]
    assert thread.call_args.kwargs["name"] == "agents-mem-once"
    thread.return_value.start.assert_called_once_with()


@pytest.mark.parametrize("legacy", [False, True])
def test_old_registry_entries_are_hidden_without_rewriting_data(tmp_path, legacy):
    keep = {"id": "notes", "page": "notepad", "targets": ["_desktop"]}
    retired = {"id": "old-flow", "page": "lark-events", "targets": ["_desktop"]}
    original = {"_desktop": [retired, keep]} if legacy else [retired, keep]
    path = tmp_path / "apps.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    before = path.read_bytes()
    source = ast.parse((LIB / "workbench.py").read_text(encoding="utf-8"))
    nodes = [n for n in source.body if isinstance(n, ast.FunctionDef)
             and n.name in {"_wb_apps_read", "_wb_apps_expand"}]
    env = {"json": json, "os": os, "WB_APPS": str(path), "FRONTEND": str(tmp_path),
           "_wb_domains_read": lambda: []}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "registry", "exec"), env)
    assert env["_wb_apps_read"]() == [keep]
    assert env["_wb_apps_expand"]([retired, keep]) == {"_desktop": [keep]}
    assert path.read_bytes() == before


def test_stale_frontend_cache_cannot_launch_timeline():
    source = (FRONT / "app-assets-apps.js").read_text(encoding="utf-8")
    start = source.index("function _openDomainApp(")
    end = source.index("\nasync function _renderAppsPage", start)
    all_start = source.index("function _allApps(")
    all_end = source.index("\nfunction _dockApps", all_start)
    js = """
let notice=''; const toast=s=>{notice=s};
const DeskI18n={tx:s=>s};
const desktopApps=[{id:'old',page:'lark-events'},{id:'notes',page:'notepad'}];
const domains=[], DESKTOP='_desktop'; const _dockAppKey=a=>a.id;
""" + source[start:end] + source[all_start:all_end] + """
console.log(JSON.stringify({result:_openDomainApp({page:'lark-events'},'_desktop'),
                           notice,apps:_allApps().map(x=>x.key)}));
"""
    result = json.loads(subprocess.check_output(["node", "-e", js], text=True, encoding="utf-8"))
    assert result == {"result": None, "notice": "此应用已下线", "apps": ["notes"]}


def test_shared_lark_reference_and_bot_are_preserved():
    from connectors.im import lark
    assert callable(lark.message_reference)
    assert callable(lark.user_open_id_cached)
    assert not hasattr(lark, "tag_groups_cached")
    assert not hasattr(lark, "spoken_chats_cached")
    assert (LIB / "larkbot/sidecar.py").is_file()
    assert (LIB / "larkbot/api.py").is_file()
    css = (FRONT / "style.css").read_text(encoding="utf-8")
    assert ".lke-" not in css and ".lark-events-page" not in css
    for name in ("app-panel-arrangement-system.js", "app-init.js", "app-settings-core.js"):
        source = (FRONT / name).read_text(encoding="utf-8")
        assert "st.lark" not in source
        assert 'kind === "lark"' not in source
        assert 'act === "lark-' not in source
