#!/usr/bin/env python3
"""Opt-in installation/cold-start acceptance in a DISPOSABLE Windows profile.

Never run against a working Desk installation. No prompts, credentials, forced
termination, process-name killing, sync-home, or retries of mutation requests.
The installer may download public runtime dependencies. GUI interaction beyond
window creation, model access and cross-version rollback remain separate tests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from release_windows import verify_archive, sha256, OFFLINE_CHECKS
from windows_release_gate import save_report, REQUIRED

BASE = "http://127.0.0.1:7765"


class Rejected(RuntimeError):
    """Only fixed diagnostic codes may be written to the public report."""


def require(condition, code):
    if not condition:
        raise Rejected(code)


def preflight(disposable, environ, home):
    require(sys.platform == "win32", "windows_required")
    require(disposable, "disposable_profile_consent_required")
    require(environ.get("USERPROFILE") and environ.get("LOCALAPPDATA")
            and environ.get("APPDATA"), "profile_environment_incomplete")
    require(Path(environ["USERPROFILE"]).resolve() == home.resolve(), "profile_identity_mismatch")
    # Presence checks only: never inspect private credentials/session contents.
    protected = [home / ".local/share/desktron", home / ".local/state/desktron",
                 home / ".config/desktron", home / ".pi", home / ".codex", home / ".claude",
                 Path(environ["LOCALAPPDATA"]) / "Programs/Desktron",
                 Path(environ["LOCALAPPDATA"]) / "Desktron",
                 Path(environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Desktron.lnk"]
    require(not any(path.exists() for path in protected), "profile_not_empty")
    require(not environ.get("HOME") or Path(environ["HOME"]).resolve() == home.resolve(),
            "home_environment_mismatch")
    actual = ps('@{USERPROFILE=[Environment]::GetFolderPath("UserProfile"); '
                'APPDATA=[Environment]::GetFolderPath("ApplicationData"); '
                'LOCALAPPDATA=[Environment]::GetFolderPath("LocalApplicationData")} | ConvertTo-Json -Compress', environ)
    require(all(Path(actual[name]).resolve() == Path(environ[name]).resolve()
                for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA")), "windows_profile_identity_mismatch")
    credential_prefixes = ("OPENAI_", "ANTHROPIC_", "CODEX_", "CLAUDE_", "PI_", "DESKTRON_", "XDG_")
    require(not any(name.upper().startswith(credential_prefixes) for name in environ),
            "engine_or_product_environment_present")
    clean_env = launch_environment(environ)
    engines = ps('[bool]@(Get-Command pi,codex,claude -ErrorAction SilentlyContinue) | ConvertTo-Json -Compress', clean_env)
    require(not engines, "existing_engine_cli_present")
    with socket.socket() as reservation:
        try:
            reservation.bind(("127.0.0.1", 7765))
        except OSError:
            raise Rejected("desk_port_in_use") from None


def powershell(env):
    return str(Path(env.get("SYSTEMROOT", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")


def ps(script, env, *, timeout=30):
    # Script is constant; file paths travel in the child environment, not code.
    prefix = '$ErrorActionPreference="Stop"; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; '
    result = subprocess.run(
        [powershell(env), "-NoProfile", "-NonInteractive", "-Command", prefix + script],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    require(result.returncode == 0, "windows_operation_failed")
    return json.loads(result.stdout)


def launch_environment(environ):
    # Simulate persistent user/machine environment, not a developer terminal's
    # transient PATH/proxy/API keys. Never import arbitrary persistent secrets.
    names = ("SYSTEMROOT", "WINDIR", "COMSPEC", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
             "TEMP", "TMP", "LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")
    env = {name.upper(): value for name, value in environ.items() if name.upper() in names}
    env.setdefault("SYSTEMROOT", r"C:\Windows")
    env["PATH"] = str(Path(env["SYSTEMROOT"]) / "System32")
    persistent = ps(
        '$r=@{}; foreach($n in @("Path","HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","NO_PROXY")) {'
        '$v=[Environment]::GetEnvironmentVariable($n,"User"); '
        'if(!$v){$v=[Environment]::GetEnvironmentVariable($n,"Machine")}; $r[$n]=$v}; '
        '$r.Path=([Environment]::GetEnvironmentVariable("Path","Machine")+";"+'
        '[Environment]::GetEnvironmentVariable("Path","User")); $r | ConvertTo-Json -Compress', env)
    env.update({key.upper(): value for key, value in persistent.items() if value})
    return env


def request(path, body=None):
    # The acceptance orchestrator itself must not send loopback traffic to a proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(BASE + path,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Desktron-Orchestrator": "deploy"})
    with opener.open(req, timeout=3) as response:
        require(response.status == 200, "acceptance_http_failed")
        raw = response.read(2 * 1024 * 1024)
    return raw if path == "/" else json.loads(raw)


def validate_runtime(probe, runtime, owner, revision):
    require(probe.get("desk") is True and probe.get("supervised") is True
            and probe.get("supervisor") == "native" and probe.get("role") == "local",
            "native_supervisor_required")
    require(probe.get("host_pid") == owner["pid"] and probe.get("rev") == revision,
            "backend_owner_or_revision_mismatch")
    for key in ("boot_id", "host_instance_id", "launch_generation", "pid", "host_pid"):
        require(bool(probe.get(key)) and runtime.get(key) == probe[key], "runtime_identity_mismatch")
    life = runtime.get("lifecycle", {})
    require(runtime.get("active_work") == 0 and life.get("active_work") == 0
            and life.get("state") == "running" and not life.get("operation_id")
            and not life.get("blockers"), "runtime_not_idle")


OWNER_CHECK = (
    '$p=Get-Process -Id ([int]$env:DESK_ACCEPT_PID); '
    'if($p.StartTime.ToUniversalTime().Ticks.ToString() -ne $env:DESK_ACCEPT_START '
    '-or $p.Path -ne $env:DESK_ACCEPT_EXE){throw "identity"}; '
)


class Desktop:
    def __init__(self, home, app_dir, env, revision, python):
        self.home, self.app_dir, self.env = home, app_dir, env
        self.revision, self.python = revision, python
        self.owner = None
        self.close_submitted = False
        self.launch_submitted = False
        self.restart_submitted = False
        self.probe = None

    def owner_env(self):
        require(self.owner is not None, "no_owned_host")
        return dict(self.env, DESK_ACCEPT_PID=str(self.owner["pid"]),
                    DESK_ACCEPT_START=self.owner["start"],
                    DESK_ACCEPT_EXE=str(self.app_dir / "Desktron.exe"))

    def ready(self, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                probe, runtime = request("/desk-probe"), request("/workbench/runtime")
                validate_runtime(probe, runtime, self.owner, self.revision)
                data = ps(OWNER_CHECK +
                    '$b=Get-Process -Id ([int]$env:DESK_ACCEPT_BACKEND); '
                    '$listeners=@(Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort 7765 -State Listen); '
                    '@{window=($p.MainWindowHandle.ToInt64() -ne 0); '
                    'listener=(@($listeners.OwningProcess) -contains [int]$env:DESK_ACCEPT_BACKEND); '
                    'python=($b.Path -eq $env:DESK_ACCEPT_PYTHON)} | ConvertTo-Json -Compress',
                    dict(self.owner_env(), DESK_ACCEPT_BACKEND=str(probe["pid"]),
                         DESK_ACCEPT_PYTHON=str(self.python)))
                require(data["window"] and data["python"] and data["listener"],
                        "window_managed_python_or_listener_identity_missing")
                page = request("/").lower()
                require(b"<html" in page or b"<!doctype html" in page, "frontend_missing")
                self.probe = probe
                return probe
            except (OSError, ValueError, Rejected, subprocess.SubprocessError):
                time.sleep(0.25)
        raise Rejected("native_readiness_not_confirmed")

    def launch(self):
        require(self.owner is None and not self.launch_submitted, "host_already_owned_or_launch_unknown")
        link = Path(self.env["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Desktron.lnk"
        self.launch_submitted = True
        self.owner = ps(
            '$s=New-Object -ComObject WScript.Shell; $l=$s.CreateShortcut($env:DESK_ACCEPT_LINK); '
            'if($l.TargetPath -ne $env:DESK_ACCEPT_EXE -or $l.WorkingDirectory -ne $env:DESK_ACCEPT_DIR)'
            '{throw "shortcut"}; $p=Start-Process -FilePath $env:DESK_ACCEPT_LINK -PassThru; '
            'if(!$p){throw "no process receipt"}; '
            '@{pid=$p.Id; start=$p.StartTime.ToUniversalTime().Ticks.ToString()} | ConvertTo-Json -Compress',
            dict(self.env, DESK_ACCEPT_LINK=str(link), DESK_ACCEPT_EXE=str(self.app_dir / "Desktron.exe"),
                 DESK_ACCEPT_DIR=str(self.app_dir)))
        self.close_submitted = False
        return self.ready()

    def close(self):
        # No second close after an unknown result. Leave the disposable VM for
        # manual inspection rather than forcing a process or retrying a mutation.
        require(not self.close_submitted, "close_already_submitted")
        before = self.ready(timeout=5)
        self.close_submitted = True
        closed = ps(OWNER_CHECK + '$p.CloseMainWindow() | ConvertTo-Json -Compress', self.owner_env())
        require(closed is True, "graceful_close_not_accepted")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            alive = ps(
                '$p=Get-Process -Id ([int]$env:DESK_ACCEPT_PID) -ErrorAction SilentlyContinue; '
                '$b=Get-Process -Id ([int]$env:DESK_ACCEPT_BACKEND) -ErrorAction SilentlyContinue; '
                '@{host=[bool]$p; backend=[bool]$b} | ConvertTo-Json -Compress',
                dict(self.owner_env(), DESK_ACCEPT_BACKEND=str(before["pid"])))
            if not alive["host"] and not alive["backend"]:
                with socket.socket() as reservation:
                    try:
                        reservation.bind(("127.0.0.1", 7765))
                    except OSError:
                        raise Rejected("port_not_released") from None
                self.owner = None
                self.launch_submitted = False
                return
            time.sleep(0.25)
        raise Rejected("graceful_close_not_confirmed")

    def restart(self):
        require(not self.restart_submitted, "restart_already_submitted_do_not_repeat")
        before = self.ready(timeout=5)  # Requires supervised identity before request.
        cli = self.home / ".local/share/desktron" / "home" / "desktron/bin/restart-desk"
        self.restart_submitted = True
        result = subprocess.run([str(self.python), str(cli), "--detach", "--yes",
                                 "--expected-boot-id", before["boot_id"],
                                 "--expected-host-instance-id", before["host_instance_id"]],
            env=dict(self.env, NO_PROXY="127.0.0.1,localhost"), capture_output=True,
            text=True, encoding="utf-8", timeout=20)
        require(result.returncode == 0, "restart_result_unknown_or_rejected")
        accepted = json.loads(result.stdout)
        require(accepted.get("ok") is True and accepted.get("state") in ("accepted", "completed")
                and accepted.get("old_boot_id") == before["boot_id"]
                and accepted.get("host_instance_id") == before["host_instance_id"], "restart_not_accepted")
        operation = accepted.get("operation_id", "")
        require(len(operation) == 32 and all(c in "0123456789abcdef" for c in operation),
                "restart_receipt_invalid")
        receipt = self.home / ".local/state/desktron/host-control" / ("response-" + operation + ".json")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                now = self.ready(timeout=1)
                done = json.loads(receipt.read_text(encoding="utf-8"))
                if (done.get("state") == "completed" and done.get("operation_id") == operation
                        and done.get("host_instance_id") == before["host_instance_id"]
                        and done.get("request_boot_id") == before["boot_id"]
                        and done.get("target_boot_id") == now["boot_id"]
                        and done.get("target_launch_generation") == now["launch_generation"]
                        and now["host_instance_id"] == before["host_instance_id"]
                        and done.get("request_launch_generation") == before["launch_generation"]
                        and done.get("request_pid") == before["pid"]
                        and now["boot_id"] != before["boot_id"]
                        and now["launch_generation"] != before["launch_generation"]):
                    return
            except (OSError, ValueError, Rejected):
                pass
            time.sleep(0.25)
        raise Rejected("restart_completion_unknown_do_not_repeat")


def validate_candidate_manifest(manifest):
    require(isinstance(manifest, dict) and manifest.get("source_dirty") is False
            and manifest.get("release_eligible") is True and manifest.get("model_requests") == "not_run",
            "formal_offline_candidate_required")
    gate = manifest.get("delivery_gate", {})
    require(gate.get("ok") is True and gate.get("mode") == "windows-delivery-offline"
            and gate.get("model_requests") == "not_run", "offline_delivery_evidence_missing")
    for name in REQUIRED:
        suite = gate.get("suites", {}).get(name, {})
        require(suite.get("ok") is True and suite.get("required") is True
                and isinstance(suite.get("collected"), int) and suite["collected"] > 0
                and suite.get("passed") == suite["collected"]
                and not any(suite.get(key, 0) for key in ("failed", "skipped", "deselected", "xfailed", "collection_errors"))
                and not suite.get("missing_required_cases"), "required_delivery_suite_not_executed")


def verify_installed(package, share, app_dir, manifest):
    for name, digest in manifest["files"].items():
        if name.startswith("payload/"):
            target = share / "home" / name[len("payload/"):]
        elif name in ("Desktron.exe", "release-rev", "release-build"):
            target = app_dir / name
        else:
            continue
        require(target.is_file() and sha256(target) == digest, "installed_file_integrity_mismatch")
    report = json.loads((share / "install-check.json").read_text(encoding="utf-8"))
    require(report.get("ok") is True and report.get("model_requests") == "not_run"
            and report.get("mode") == "isolated-offline"
            and all(report.get("checks", {}).get(name) == "passed" for name in OFFLINE_CHECKS),
            "installed_offline_acceptance_failed")


def fixture_sessions(share, create=False):
    digests = {}
    for engine in ("claude", "codex", "pi"):
        if create:
            for _ in range(2):
                # POST once only; a lost reply does not authorize another attempt.
                row = request("/session", {"engine": engine})
                require(isinstance(row, dict) and bool(row.get("id")), "empty_session_creation_failed")
        index = share / "data/adapter" / engine / "sessions.json"
        previous = Path(str(index) + ".previous")
        require(index.is_file() and previous.is_file(), "session_backup_missing")
        rows = json.loads(index.read_text(encoding="utf-8"))
        require(len(rows) == 2, "synthetic_session_count_changed")
        digests[engine] = hashlib.sha256(index.read_bytes()).hexdigest()
    return digests


def install(package, env):
    # Installer output can contain download/proxy diagnostics. Do not persist it.
    result = subprocess.run(
        [powershell(env), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(package / "install.ps1"), "-NoLaunch"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1800)
    require(result.returncode == 0, "installer_failed")


def run(args):
    report = {"schema": 1, "ok": False, "mode": "disposable-windows-installation",
              "artifact_sha256": args.sha256.lower(), "model_requests": "not_run",
              "checks": {}, "model_access": "not_run", "cross_version_rollback": "not_run",
              "limits": ["Does not verify visual rendering, browser/Agent interaction or model connectivity."]}
    desktop = None
    try:
        home = Path(os.environ.get("USERPROFILE", "."))
        preflight(args.disposable_profile, os.environ, home)
        require(len(args.sha256) == 64 and all(c in "0123456789abcdefABCDEF" for c in args.sha256),
                "trusted_artifact_sha256_required")
        require(sha256(args.archive) == args.sha256.lower(), "artifact_sha256_mismatch")
        with tempfile.TemporaryDirectory(prefix="desk-vm-acceptance-") as directory:
            import zipfile
            with zipfile.ZipFile(args.archive) as archive:
                names = [name for name in archive.namelist() if name.count("/") == 1
                         and name.endswith("/release-manifest.json")]
                require(len(names) == 1, "release_manifest_missing")
                manifest = json.loads(archive.read(names[0]))
            validate_candidate_manifest(manifest)
            package = verify_archive(args.archive, Path(directory) / "extract", manifest)
            report.update(revision=manifest["revision"], build=manifest["build"], architecture=manifest["architecture"])
            report["checks"]["artifact_integrity"] = "passed"
            install(package, os.environ.copy())
            share = home / ".local/share/desktron"
            app_dir = Path(os.environ["LOCALAPPDATA"]) / "Programs/Desktron"
            verify_installed(package, share, app_dir, manifest)
            report["checks"]["installation"] = "passed"
            spec = json.loads((share / "home" / "desktron/lib/provision/python-runtime.json").read_text(encoding="utf-8"))
            python = share / "runtime" / ("python-" + spec["version"] + "-windows-" + manifest["architecture"]) / "python.exe"
            env = launch_environment(os.environ)
            desktop = Desktop(home, app_dir, env, manifest["revision"], python)
            first = desktop.launch()
            report["checks"]["shortcut_native_cold_start"] = "passed"
            sessions = fixture_sessions(share, create=True)
            report["checks"]["empty_sessions_and_backups"] = "passed"
            desktop.close()
            second = desktop.launch()
            require(first["host_instance_id"] != second["host_instance_id"]
                    and first["boot_id"] != second["boot_id"]
                    and first["launch_generation"] != second["launch_generation"], "cold_reopen_identity_unchanged")
            require(fixture_sessions(share) == sessions, "cold_reopen_sessions_changed")
            report["checks"]["cold_reopen_preserves_sessions"] = "passed"
            desktop.restart()
            require(fixture_sessions(share) == sessions, "restart_sessions_changed")
            report["checks"]["supervised_restart"] = "passed"
            desktop.close()
            install(package, env)
            verify_installed(package, share, app_dir, manifest)
            require(fixture_sessions(share) == sessions, "reinstallation_sessions_changed")
            desktop.launch()
            desktop.close()
            report["checks"]["same_artifact_reinstallation"] = "passed"
            report["checks"]["graceful_exit_and_port_release"] = "passed"
            require(sha256(args.archive) == args.sha256.lower(), "artifact_changed_during_acceptance")
            report["ok"] = True
    except Rejected as error:
        report["error"] = str(error)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as error:
        report["error"] = type(error).__name__
    finally:
        # Do not automatically retry a quit/restart whose result is unknown.
        report["owned_host_may_remain"] = bool(desktop and (desktop.owner or desktop.launch_submitted))
        save_report(args.report, report)
    return 0 if report["ok"] else 1


def validate_report_path(report, archive, environ):
    require(report.suffix == ".json" and not report.exists() and not report.is_symlink()
            and report.resolve() != archive.resolve(), "new_separate_json_report_required")
    home = Path(environ.get("USERPROFILE", "."))
    protected = [home / name for name in (".local/share/desktron", ".local/state/desktron",
                 ".config/desktron", ".pi", ".codex", ".claude")]
    protected += [Path(environ.get("LOCALAPPDATA", ".")) / name for name in ("Programs/Desktron", "Desktron")]
    require(not any(report.resolve().is_relative_to(root.resolve()) for root in protected),
            "report_must_not_modify_product_or_credentials")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--sha256", required=True, help="Digest obtained from a trusted release source")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--disposable-profile", action="store_true", help="Consent to install and restart in this NEW test profile")
    args = parser.parse_args(argv)
    try:
        validate_report_path(args.report, args.archive, os.environ)
    except Rejected as error:
        print(str(error), file=sys.stderr)
        return 2
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
