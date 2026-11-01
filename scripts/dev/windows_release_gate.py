#!/usr/bin/env python3
"""Windows delivery regressions. Required suites must execute, not skip.

No provider prompts, login or production installation are performed. Each file
runs in a separate pytest process; reports contain counts, never test output.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = (
    "test_pi_sdk_discovery.py",
    "test_pi_http_proxy.py",
    "test_pi_protocol_probe.py",
    "test_codex_pipe_transport.py",
    "test_shepherd_utf8_process.py",
    "test_utf8_protocol_contracts.py",
    "test_windows_native.py",
    "test_windows_long_paths.py",
)
# Named scenarios are part of the gate contract: removing a test must not
# silently remove the corresponding delivery guarantee.
REQUIRED_CASES = {
    "test_pi_sdk_discovery.py": {"test_installed_pi_bridge_initializes_and_reads_state_without_dispatch": 1},
    "test_pi_http_proxy.py": {"test_real_headless_bridge_proxy_route_before_ready_without_model_request": 4},
    "test_pi_protocol_probe.py": {
        "test_cold_pi_probe_over_old_fifteen_second_budget_passes_full_contract": 1,
        "test_pi_probe_still_rejects_changed_capability_without_version_cache": 1,
        "test_pi_probe_hang_is_bounded_and_phase_is_visible": 1,
        "test_windows_probe_profile_and_temp_do_not_fall_back_to_real_user": 1,
        "test_timeout_retains_progress_before_disposable_state_is_removed": 1,
        "test_progress_write_failure_does_not_change_sdk_admission": 1,
        "test_timeout_diagnostic_never_echoes_native_command_or_error": 3,
        "test_sdk_budget_is_explicit_for_both_probe_modes": 4,
        "test_progress_rejects_untrusted_or_oversize_fields": 9,
        "test_owned_probe_cleans_up_child_if_protection_fails": 1,
        "test_failed_pi_probe_still_blocks_native_spawn_and_message_dispatch": 2,
    },
    "test_codex_pipe_transport.py": {
        "test_windows_pipe_never_uses_socket_selector": 1,
        "test_catalog_fetch_uses_same_rpc_transport_without_turn_requests": 1,
        "test_close_wakes_concurrent_long_protocol_wait": 1,
    },
    "test_shepherd_utf8_process.py": {"test_policy_and_runner_initialization_under_gbk_locale": 1},
    "test_utf8_protocol_contracts.py": {
        "test_codex_schema_documents_are_utf8_and_reject_invalid_bytes": 1,
        "test_pi_child_evidence_roundtrips_utf8_without_replacement": 1,
        "test_onboarding_progress_is_utf8_across_save_read_and_listing": 1,
        "test_peer_reader_owns_its_json_pipe_encoding_without_ambient_io_override": 2,
        "test_memory_pi_pipe_is_real_utf8_and_invalid_bytes_fail_closed": 2,
        "test_pi_child_sidebar_reads_native_utf8_sidecar": 1,
        "test_ci_keeps_full_regression_in_both_utf8_modes": 1,
        "test_json_engine_pipe_declarations_do_not_depend_on_locale": 1,
    },
    "test_windows_native.py": {
        "test_json_index_commit_and_backup": 1, "test_new_session_tree_killpg": 1,
        "test_echo_roundtrip": 1, "test_listening_and_peer_pid": 1,
    },
    "test_windows_long_paths.py": {
        "test_real_windows_create_read_stat_enumerate_rename_delete": 1,
        "test_real_windows_short_parent_long_filename_stat_and_cleanup": 1,
        "test_real_windows_resident_rebuilds_twice_without_touching_store": 1,
        "test_real_windows_resident_rejects_outside_and_junction_parent": 1,
        "test_real_windows_recursive_cleanup_does_not_follow_junction": 1,
        "test_real_windows_long_store_identity_and_link_ancestor": 1,
        "test_real_windows_bytes_and_relative_paths_keep_enumeration_identity": 1,
        "test_real_windows_relative_symlink_target_is_relative_to_long_link": 1,
        "test_real_windows_long_junction_failure_removes_only_owned_directory": 1,
        "test_junction_payload_has_one_nt_namespace_and_byte_offsets": 3,
        "test_resident_cleanup_validates_deleted_parent_not_only_home": 3,
        "test_install_is_idempotent_and_missing_paths_still_fail": 1,
        "test_posix_install_is_a_noop_in_fresh_process": 1,
    },
}
REGRESSION = (
    "test_windows_port.py",
    "test_durable_json.py",
    "test_install_acceptance.py",
    "test_codex_appserver.py",
    "test_codex_catalog_cache.py",
    "test_pi_catalog_cache.py",
    "test_windows_release_gate.py",
    "test_windows_vm_acceptance.py",
    "test_restart_desk.py",
)


class Coverage:
    """Fail closed on empty selection, deselection, skip, xfail or teardown failure."""
    def __init__(self):
        self.collected = set()
        self.deselected = set()
        self.passed = set()
        self.skipped = set()
        self.failed = set()
        self.xfailed = set()
        self.collection_errors = 0

    def pytest_collection_finish(self, session):
        self.collected = {item.nodeid for item in session.items}

    def pytest_deselected(self, items):
        self.deselected.update(item.nodeid for item in items)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors += 1

    def pytest_runtest_logreport(self, report):
        if hasattr(report, "wasxfail"):
            self.xfailed.add(report.nodeid)
        if report.skipped:
            self.skipped.add(report.nodeid)
        if report.failed:
            self.failed.add(report.nodeid)
        if report.when == "call" and report.passed:
            self.passed.add(report.nodeid)

    def result(self, exit_code, required, cases=None):
        executed = bool(self.collected) and self.passed == self.collected
        clean = not (self.skipped or self.deselected or self.xfailed)
        missing = [name for name, minimum in (cases or {}).items()
                   if sum(node.rsplit("::", 1)[-1].split("[", 1)[0] == name
                          for node in self.passed) < minimum]
        return {
            "ok": exit_code == 0 and not self.failed and not self.collection_errors and not missing
                  and (executed and clean if required else bool(self.collected)),
            "required": required, "exit_code": int(exit_code),
            "collected": len(self.collected), "passed": len(self.passed),
            "skipped": len(self.skipped), "deselected": len(self.deselected),
            "failed": len(self.failed), "xfailed": len(self.xfailed),
            "collection_errors": self.collection_errors, "missing_required_cases": missing,
        }


def save_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".gate-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def worker(selector, report, required):
    import pytest
    # deskcfg parses argv at import time. No production data or workspace.
    with tempfile.TemporaryDirectory(prefix="desk-delivery-test-") as directory:
        os.environ["DESKTRON_DATA_HOME"] = str(Path(directory) / "data")
        os.environ["DESKTRON_WORKSPACE"] = str(Path(directory) / "workspace")
        sys.argv = ["test", str(ROOT / "frontend"), "0", "0", directory]
        coverage = Coverage()
        args = ["-q", "--tb=short", "--color=no", "-p", "no:cacheprovider",
                "-o", "addopts=", "-o",
                "markers=native: explicit engine test\ndesktop: explicit GUI test",
                "-W", "error::pytest.PytestUnhandledThreadExceptionWarning",
                "-W", "error::pytest.PytestUnraisableExceptionWarning",
                "-m", "not native and not desktop", selector]
        if not required and Path(selector).name == "test_install_acceptance.py":
            # POSIX signal termination semantics are not Windows TerminateProcess.
            args += ["-k", "not interrupted_cli_reaps"]
        code = pytest.main(args, plugins=[coverage])
        result = coverage.result(code, required, REQUIRED_CASES.get(Path(selector).name) if required else None)
        if result["ok"] and Path(selector).name == "test_pi_sdk_discovery.py":
            from shepherd.pi_connector import pi_sdk_root
            package = Path(pi_sdk_root()) / "package.json"
            result["pi_sdk"] = json.loads(package.read_text(encoding="utf-8"))["version"]
            result["node"] = subprocess.run(
                ["node", "-p", "process.versions.node"], check=True, capture_output=True,
                text=True, encoding="utf-8", timeout=10).stdout.strip()
        save_report(report, result)
        return 0 if result["ok"] else 1


def run(report_path):
    report = {
        "schema": 1, "ok": False, "mode": "windows-delivery-offline",
        "model_requests": "not_run", "python": platform.python_version(),
        "architecture": platform.machine(), "python_utf8": sys.flags.utf8_mode,
        "suites": {},
        "limits": ["Does not verify installer, native GUI cold start, login or model access."],
    }
    if sys.platform != "win32":
        report["error"] = "Windows runner required"
        save_report(report_path, report)
        return 1
    spec = json.loads((ROOT / "desktron/lib/provision/python-runtime.json").read_text(encoding="utf-8"))
    if report["python"] != spec["version"]:
        report["error"] = "Use the product managed Python version for delivery acceptance"
        save_report(report_path, report)
        return 1
    with tempfile.TemporaryDirectory(prefix="desk-delivery-gate-") as directory:
        env = os.environ.copy()
        env.pop("PYTEST_ADDOPTS", None)
        env.pop("DESKTRON_NATIVE_WRITER_SESSION_ID", None)
        env.pop("DESKTRON_DESKTOP_TESTS", None)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT / "desktron/lib"), str(ROOT / "desktron/tests")])
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        for name in (*REQUIRED, *REGRESSION):
            required = name in REQUIRED
            output = Path(directory) / (name + ".json")
            argv = [sys.executable, str(Path(__file__).resolve()), "--worker",
                    str(ROOT / "desktron/tests" / name), "--report", str(output)]
            if required:
                argv.append("--required")
            try:
                proc = subprocess.run(argv, cwd=ROOT, env=env, timeout=600)
                result = json.loads(output.read_text(encoding="utf-8"))
                if proc.returncode:
                    result["ok"] = False
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                result = {"ok": False, "required": required, "error": type(error).__name__}
            report["suites"][name] = result
            # Preserve evidence on failure; never allow a previous report to pass.
            save_report(report_path, report)
        report["ok"] = all(result["ok"] for result in report["suites"].values())
    save_report(report_path, report)
    return 0 if report["ok"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--worker")
    parser.add_argument("--required", action="store_true")
    args = parser.parse_args(argv)
    if args.worker:
        return worker(args.worker, args.report, args.required)
    return run(args.report)


if __name__ == "__main__":
    raise SystemExit(main())
