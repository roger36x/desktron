"""Safety/protocol tests only. Never install or close the developer's Desk."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/dev"))
import windows_vm_acceptance as vm


def environment(tmp_path):
    return {"USERPROFILE": str(tmp_path), "HOME": str(tmp_path),
            "LOCALAPPDATA": str(tmp_path / "local"), "APPDATA": str(tmp_path / "roaming"),
            "SYSTEMROOT": str(tmp_path / "windows")}


def fake_preflight(monkeypatch, tmp_path):
    monkeypatch.setattr(vm.sys, "platform", "win32")
    monkeypatch.setattr(vm, "ps", lambda script, env, **kw:
                        environment(tmp_path) if "GetFolderPath" in script else False)
    monkeypatch.setattr(vm, "launch_environment", lambda env: dict(env))
    sock = mock.MagicMock()
    monkeypatch.setattr(vm.socket, "socket", lambda: sock)
    return sock


def test_clean_profile_guard_is_presence_only_and_reserves_no_running_port(tmp_path, monkeypatch):
    sock = fake_preflight(monkeypatch, tmp_path)
    vm.preflight(True, environment(tmp_path), tmp_path)
    sock.__enter__.return_value.bind.assert_called_once_with(("127.0.0.1", 7765))


@pytest.mark.parametrize("relative", [".local/share/desktron", ".local/state/desktron",
    ".config/desktron", ".pi", ".codex", ".claude", "local/Programs/Desktron", "local/Desktron",
    "roaming/Microsoft/Windows/Start Menu/Programs/Desktron.lnk"])
def test_existing_product_or_login_profile_refused_before_operations(tmp_path, monkeypatch, relative):
    fake_preflight(monkeypatch, tmp_path)
    (tmp_path / relative).mkdir(parents=True)
    operation = mock.Mock(side_effect=AssertionError("must not launch an operation"))
    monkeypatch.setattr(vm, "ps", operation)
    with pytest.raises(vm.Rejected, match="profile_not_empty"):
        vm.preflight(True, environment(tmp_path), tmp_path)
    operation.assert_not_called()


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DESKTRON_HOME", "XDG_CONFIG_HOME"])
def test_inherited_provider_or_product_environment_refused(tmp_path, monkeypatch, name):
    fake_preflight(monkeypatch, tmp_path)
    env = dict(environment(tmp_path), **{name: "placeholder"})
    with pytest.raises(vm.Rejected, match="environment_present"):
        vm.preflight(True, env, tmp_path)


def test_real_windows_profile_must_match_not_just_spoofed_environment(tmp_path, monkeypatch):
    fake_preflight(monkeypatch, tmp_path)
    monkeypatch.setattr(vm, "ps", lambda *args, **kwargs: environment(tmp_path / "different"))
    with pytest.raises(vm.Rejected, match="windows_profile_identity_mismatch"):
        vm.preflight(True, environment(tmp_path), tmp_path)


@pytest.mark.parametrize("name", ["APPDATA", "LOCALAPPDATA"])
def test_spoofed_application_data_folders_are_not_used(tmp_path, monkeypatch, name):
    fake_preflight(monkeypatch, tmp_path)
    env = dict(environment(tmp_path))
    env[name] = str(tmp_path / "different")
    with pytest.raises(vm.Rejected, match="windows_profile_identity_mismatch"):
        vm.preflight(True, env, tmp_path)


def test_port_in_use_is_never_adopted(tmp_path, monkeypatch):
    sock = fake_preflight(monkeypatch, tmp_path)
    sock.__enter__.return_value.bind.side_effect = OSError("fixture occupied port")
    with pytest.raises(vm.Rejected, match="desk_port_in_use"):
        vm.preflight(True, environment(tmp_path), tmp_path)


def test_explicit_disposable_profile_consent_required(tmp_path, monkeypatch):
    fake_preflight(monkeypatch, tmp_path)
    with pytest.raises(vm.Rejected, match="consent_required"):
        vm.preflight(False, environment(tmp_path), tmp_path)


def test_system_engine_cli_is_not_borrowed_by_fresh_profile(tmp_path, monkeypatch):
    fake_preflight(monkeypatch, tmp_path)
    monkeypatch.setattr(vm, "ps", lambda script, *args, **kwargs:
                        environment(tmp_path) if "GetFolderPath" in script else True)
    with pytest.raises(vm.Rejected, match="existing_engine_cli_present"):
        vm.preflight(True, environment(tmp_path), tmp_path)


def test_cold_start_environment_has_no_terminal_credentials_or_transient_proxy(tmp_path, monkeypatch):
    env = dict(environment(tmp_path), OPENAI_API_KEY="placeholder", PI_AGENT_DIR="private-override",
               PATH="developer-node", HTTP_PROXY="http://localhost:1")
    monkeypatch.setattr(vm, "ps", lambda *args, **kwargs: {"Path": "persistent-machine;persistent-user",
                        "HTTPS_PROXY": "http://localhost:2", "HTTP_PROXY": None})
    result = vm.launch_environment(env)
    assert result["PATH"] == "persistent-machine;persistent-user"
    assert "Path" not in result
    assert "OPENAI_API_KEY" not in result and "PI_AGENT_DIR" not in result
    assert "HTTP_PROXY" not in result
    assert result["HTTPS_PROXY"] == "http://localhost:2"


@pytest.mark.parametrize("relative", [".pi/report.json", ".codex/report.json",
    ".claude/report.json", ".local/share/desktron/report.json", "local/Programs/Desktron/report.json",
    "local/Desktron/WebView2/report.json"])
def test_reports_cannot_modify_product_or_credentials(tmp_path, relative):
    with pytest.raises(vm.Rejected, match="must_not_modify"):
        vm.validate_report_path(tmp_path / relative, tmp_path / "candidate.zip", environment(tmp_path))


def test_reports_cannot_overwrite_existing_evidence(tmp_path):
    path = tmp_path / "report.json"
    path.write_text("fixture evidence", encoding="utf-8")
    with pytest.raises(vm.Rejected, match="new_separate"):
        vm.validate_report_path(path, tmp_path / "candidate.zip", environment(tmp_path))
    assert path.read_text(encoding="utf-8") == "fixture evidence"


def facts():
    probe = {"desk": True, "role": "local", "supervised": True, "supervisor": "native",
             "rev": "fixture-revision", "host_pid": 42, "pid": 43, "boot_id": "first-boot",
             "host_instance_id": "fixture-host", "launch_generation": "first-generation"}
    runtime = {**probe, "active_work": 0, "lifecycle": {"active_work": 0, "state": "running",
                                                        "operation_id": "", "blockers": []}}
    return probe, runtime


@pytest.mark.parametrize("mutation", [
    lambda p, r: p.update(host_pid=99),
    lambda p, r: p.update(rev="different"),
    lambda p, r: p.update(supervised=False),
    lambda p, r: r.update(boot_id="foreign"),
    lambda p, r: r.update(active_work=1),
    lambda p, r: r["lifecycle"].update(operation_id="pending"),
    lambda p, r: r["lifecycle"].update(blockers=[{"kind": "fixture", "count": 1}]),
])
def test_foreign_or_busy_runtime_not_accepted(tmp_path, mutation):
    probe, runtime = facts()
    mutation(probe, runtime)
    with pytest.raises(vm.Rejected):
        vm.validate_runtime(probe, runtime, {"pid": 42}, "fixture-revision")


def desktop(tmp_path):
    app = vm.Desktop(tmp_path, tmp_path / "app", environment(tmp_path), "fixture-revision", tmp_path / "python.exe")
    app.owner = {"pid": 42, "start": "fixture-create-time"}
    app.launch_submitted = True
    app.ready = mock.Mock(return_value=facts()[0])
    return app


def test_claimed_probe_identity_cannot_replace_kernel_listener_identity(tmp_path, monkeypatch):
    app = vm.Desktop(tmp_path, tmp_path / "app", environment(tmp_path), "fixture-revision", tmp_path / "python.exe")
    app.owner = {"pid": 42, "start": "fixture"}
    probe, runtime = facts()
    monkeypatch.setattr(vm, "request", lambda path: probe if path == "/desk-probe" else runtime)
    monkeypatch.setattr(vm, "ps", lambda *args, **kwargs: {"window": True, "python": True, "listener": False})
    clock = iter([0, 0, 61])
    monkeypatch.setattr(vm.time, "monotonic", lambda: next(clock, 61))
    monkeypatch.setattr(vm.time, "sleep", lambda n: None)
    with pytest.raises(vm.Rejected, match="readiness_not_confirmed"):
        app.ready()


def test_orchestrator_http_calls_only_probe_frontend_runtime_and_empty_session():
    tree = ast.parse(Path(vm.__file__).read_text(encoding="utf-8"))
    paths = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "request":
            assert isinstance(node.args[0], ast.Constant)
            paths.append(node.args[0].value)
    assert set(paths) == {"/desk-probe", "/workbench/runtime", "/", "/session"}


def test_graceful_close_uses_exact_process_identity_never_kill(tmp_path, monkeypatch):
    app = desktop(tmp_path)
    operation = mock.Mock(side_effect=[True, {"host": False, "backend": False}])
    monkeypatch.setattr(vm, "ps", operation)
    monkeypatch.setattr(vm.socket, "socket", mock.MagicMock())
    app.close()
    assert app.owner is None and not app.launch_submitted
    script, env = operation.call_args_list[0].args
    assert "CloseMainWindow" in script and "StartTime" in script and "$p.Path" in script
    assert env["DESK_ACCEPT_PID"] == "42"
    assert env["DESK_ACCEPT_START"] == "fixture-create-time"
    source = Path(vm.__file__).read_text(encoding="utf-8")
    for forbidden in ("Stop-Process", "taskkill", ".terminate(", ".kill(", "os.kill("):
        assert forbidden not in source


def test_unknown_close_result_is_not_repeated(tmp_path, monkeypatch):
    app = desktop(tmp_path)
    operation = mock.Mock(return_value=False)
    monkeypatch.setattr(vm, "ps", operation)
    with pytest.raises(vm.Rejected, match="not_accepted"):
        app.close()
    with pytest.raises(vm.Rejected, match="already_submitted"):
        app.close()
    assert operation.call_count == 1 and app.owner


def test_launch_without_process_receipt_is_marked_unknown_not_retried(tmp_path, monkeypatch):
    app = desktop(tmp_path)
    app.owner = None
    app.launch_submitted = False
    operation = mock.Mock(side_effect=vm.Rejected("no_receipt"))
    monkeypatch.setattr(vm, "ps", operation)
    with pytest.raises(vm.Rejected):
        app.launch()
    with pytest.raises(vm.Rejected, match="launch_unknown"):
        app.launch()
    assert app.launch_submitted and app.owner is None
    assert operation.call_count == 1


@pytest.mark.parametrize("complete", [True, False])
def test_restart_uses_native_cli_pinned_identity_and_requires_completed_receipt(tmp_path, monkeypatch, complete):
    app = desktop(tmp_path)
    before = facts()[0]
    now = {**before, "boot_id": "second-boot", "launch_generation": "second-generation"}
    app.ready = mock.Mock(side_effect=[before, now])
    op = "a" * 32
    receipt = tmp_path / ".local/state/desktron/host-control" / ("response-" + op + ".json")
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"state": "completed" if complete else "accepted", "operation_id": op,
        "host_instance_id": before["host_instance_id"], "request_boot_id": before["boot_id"],
        "request_launch_generation": before["launch_generation"], "request_pid": before["pid"],
        "target_boot_id": now["boot_id"], "target_launch_generation": now["launch_generation"]}), encoding="utf-8")
    operation = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps({
        "ok": True, "state": "accepted", "operation_id": op, "old_boot_id": before["boot_id"],
        "host_instance_id": before["host_instance_id"]})))
    monkeypatch.setattr(vm.subprocess, "run", operation)
    clock = iter([0, 0, 61])
    monkeypatch.setattr(vm.time, "monotonic", lambda: next(clock, 61))
    monkeypatch.setattr(vm.time, "sleep", lambda n: None)
    if complete:
        app.restart()
    else:
        with pytest.raises(vm.Rejected, match="completion_unknown"):
            app.restart()
    with pytest.raises(vm.Rejected, match="restart_already_submitted"):
        app.restart()
    assert operation.call_count == 1
    argv = operation.call_args.args[0]
    assert argv[2:4] == ["--detach", "--yes"]
    assert argv[argv.index("--expected-boot-id") + 1] == before["boot_id"]
    assert argv[argv.index("--expected-host-instance-id") + 1] == before["host_instance_id"]


def test_installer_failure_does_not_log_or_retry_native_output(tmp_path, monkeypatch):
    operation = mock.Mock(return_value=SimpleNamespace(returncode=1))
    monkeypatch.setattr(vm.subprocess, "run", operation)
    with pytest.raises(vm.Rejected, match="installer_failed"):
        vm.install(tmp_path, environment(tmp_path))
    assert operation.call_count == 1
    assert operation.call_args.kwargs["stdout"] is subprocess.DEVNULL
    assert operation.call_args.kwargs["stderr"] is subprocess.DEVNULL


def candidate_manifest():
    return {"source_dirty": False, "release_eligible": True, "model_requests": "not_run",
        "delivery_gate": {"ok": True, "mode": "windows-delivery-offline", "model_requests": "not_run",
            "suites": {name: {"ok": True, "required": True, "collected": 1, "passed": 1,
                              "skipped": 0, "failed": 0} for name in vm.REQUIRED}}}


@pytest.mark.parametrize("mutation", [
    lambda m: m.update(source_dirty=True),
    lambda m: m.update(model_requests="run"),
    lambda m: m["delivery_gate"].update(mode="unknown"),
    lambda m: m["delivery_gate"]["suites"].pop(vm.REQUIRED[0]),
    lambda m: m["delivery_gate"]["suites"][vm.REQUIRED[0]].update(skipped=1),
    lambda m: m["delivery_gate"]["suites"][vm.REQUIRED[0]].update(passed=0),
    lambda m: m["delivery_gate"]["suites"][vm.REQUIRED[0]].update(collected=0, passed=0),
])
def test_vm_cannot_promote_unverified_or_experimental_candidate(mutation):
    manifest = candidate_manifest()
    vm.validate_candidate_manifest(manifest)
    mutation(manifest)
    with pytest.raises(vm.Rejected):
        vm.validate_candidate_manifest(manifest)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell parser only")
def test_all_native_control_scripts_parse_without_executing_them(tmp_path):
    def literal(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name) and node.id == "OWNER_CHECK":
            return vm.OWNER_CHECK
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return literal(node.left) + literal(node.right)
        raise AssertionError("Native command must be constant, not interpolated code")

    tree = ast.parse(Path(vm.__file__).read_text(encoding="utf-8"))
    scripts = [literal(node.args[0]) for node in ast.walk(tree)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ps"]
    source = tmp_path / "scripts.json"
    source.write_text(json.dumps(scripts), encoding="utf-8")
    parser = '$ErrorActionPreference="Stop"; $scripts=Get-Content $env:DESK_ACCEPT_SCRIPTS -Raw -Encoding UTF8 | ConvertFrom-Json; '
    parser += 'foreach($script in $scripts){$errors=$null; [System.Management.Automation.Language.Parser]::ParseInput($script,[ref]$null,[ref]$errors) | Out-Null; if($errors){throw "syntax"}}; '
    parser += '@{parsed=$scripts.Count} | ConvertTo-Json -Compress'
    result = subprocess.run([vm.powershell(os.environ), "-NoProfile", "-NonInteractive", "-Command", parser],
        env=dict(os.environ, DESK_ACCEPT_SCRIPTS=str(source)), capture_output=True,
        # PowerShell cold start alone exceeded 30 s on a loaded hosted runner (run 37672982426).
        text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0
    assert json.loads(result.stdout)["parsed"] == len(scripts) and len(scripts) >= 7


def test_manual_workflow_is_not_push_or_pr_triggered_and_never_uploads_native_logs():
    import yaml
    source = (ROOT / ".github/workflows/windows-vm-acceptance.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(source)
    events = workflow.get("on", workflow.get(True))
    assert set(events) == {"workflow_dispatch"}
    assert "--disposable-profile" in source
    assert "--sha256 $env:ACCEPTANCE_SHA256" in source
    assert "persist-credentials: false" in source
    assert "windows-vm-acceptance.json" in source
    assert "native-host.log" not in source and "desktron-web.log" not in source


def test_full_orchestration_binds_evidence_without_model_or_native_operations(tmp_path, monkeypatch):
    import zipfile
    env = environment(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    manifest = {**candidate_manifest(), "revision": "a" * 40, "build": "1", "architecture": "x64"}
    archive = tmp_path / "candidate.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("package/release-manifest.json", json.dumps(manifest))
    package = tmp_path / "package"
    package.mkdir()
    spec = tmp_path / ".local/share/desktron" / "home" / "desktron/lib/provision/python-runtime.json"
    spec.parent.mkdir(parents=True)
    spec.write_text(json.dumps({"version": "3.12.14"}), encoding="utf-8")
    monkeypatch.setattr(vm, "preflight", lambda *args: None)
    monkeypatch.setattr(vm, "verify_archive", lambda *args: package)
    monkeypatch.setattr(vm, "verify_installed", lambda *args: None)
    monkeypatch.setattr(vm, "launch_environment", lambda env: dict(env))
    events = []
    monkeypatch.setattr(vm, "install", lambda *args: events.append("install"))
    monkeypatch.setattr(vm, "fixture_sessions", lambda *args, **kwargs: {"fixture": "digest"})

    class FakeDesktop:
        owner = None
        launch_submitted = False
        sequence = 0

        def __init__(self, *args):
            pass

        def launch(self):
            self.sequence += 1
            events.append("launch")
            return {"host_instance_id": str(self.sequence), "boot_id": str(self.sequence),
                    "launch_generation": str(self.sequence)}

        def close(self):
            events.append("close")

        def restart(self):
            events.append("restart")

    monkeypatch.setattr(vm, "Desktop", FakeDesktop)
    args = SimpleNamespace(disposable_profile=True, sha256=vm.sha256(archive),
                           archive=archive, report=tmp_path / "report.json")
    assert vm.run(args) == 0
    assert events == ["install", "launch", "close", "launch", "restart", "close", "install", "launch", "close"]
    report = json.loads(args.report.read_text(encoding="utf-8"))
    assert report["artifact_sha256"] == vm.sha256(archive)
    assert report["model_requests"] == "not_run" and report["model_access"] == "not_run"
    assert report["cross_version_rollback"] == "not_run"
    assert all(state == "passed" for state in report["checks"].values())


def test_wrong_trusted_zip_hash_refuses_before_extraction_or_install(tmp_path, monkeypatch):
    archive = tmp_path / "candidate.zip"
    archive.write_bytes(b"fixture")
    args = SimpleNamespace(disposable_profile=True, sha256="0" * 64,
                           archive=archive, report=tmp_path / "report.json")
    monkeypatch.setattr(vm, "preflight", lambda *args: None)
    operation = mock.Mock(side_effect=AssertionError("no installation"))
    monkeypatch.setattr(vm, "install", operation)
    assert vm.run(args) == 1
    operation.assert_not_called()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    assert report["error"] == "artifact_sha256_mismatch" and report["checks"] == {}


def test_failed_preflight_leaves_no_installation_or_native_operations(tmp_path, monkeypatch):
    args = SimpleNamespace(disposable_profile=False, sha256="0" * 64,
                           archive=tmp_path / "candidate.zip", report=tmp_path / "report.json")
    monkeypatch.setattr(vm, "preflight", mock.Mock(side_effect=vm.Rejected("profile_not_empty")))
    install = mock.Mock(side_effect=AssertionError("no installation"))
    monkeypatch.setattr(vm, "install", install)
    assert vm.run(args) == 1
    install.assert_not_called()
    result = json.loads(args.report.read_text(encoding="utf-8"))
    assert result["checks"] == {} and not result["ok"]
    assert result["model_requests"] == "not_run"
    assert result["error"] == "profile_not_empty"
    assert not result["owned_host_may_remain"]
