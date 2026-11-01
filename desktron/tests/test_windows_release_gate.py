"""Delivery gates fail closed; fixtures never install or call a model."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/dev"))
import release_windows as release
import windows_release_gate as gate


@pytest.mark.parametrize("body,options,ok", [
    ("def test_ok(): pass\n", "", True),
    ("import pytest\n@pytest.mark.skip(reason='fixture')\ndef test_ok(): pass\n", "", False),
    ("import pytest\n@pytest.mark.xfail\ndef test_ok(): assert False\n", "", False),
    ("import pytest\n@pytest.mark.xfail\ndef test_ok(): pass\n", "", False),
    ("def test_keep(): pass\ndef test_omit(): pass\n", "-k test_keep", False),
    ("import pytest\n@pytest.fixture\ndef bad():\n yield\n assert False\ndef test_ok(bad): pass\n", "", False),
    ("# Empty test file\n", "", False),
    ("def broken(\n", "", False),
    ("import threading\ndef test_ok():\n t=threading.Thread(target=lambda: 1/0)\n t.start()\n t.join()\n", "", False),
])
def test_required_worker_cannot_pass_without_complete_execution(tmp_path, body, options, ok):
    fixture = tmp_path / "test_gate_fixture.py"
    fixture.write_text(body, encoding="utf-8")
    report = tmp_path / "report.json"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts/dev/windows_release_gate.py"),
         "--worker", str(fixture), "--required", "--report", str(report)],
        cwd=tmp_path, env=dict(os.environ, PYTEST_ADDOPTS=options),
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["ok"] is ok
    assert (proc.returncode == 0) is ok
    assert "fixture" not in report.read_text(encoding="utf-8")


def test_missing_node_and_pi_cannot_silently_skip_the_required_sdk_suite(tmp_path):
    report = tmp_path / "report.json"
    env = dict(os.environ, PATH=str(Path(sys.executable).parent), PYTEST_ADDOPTS="")
    env["PYTHONPATH"] = str(ROOT / "desktron/lib")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts/dev/windows_release_gate.py"), "--worker",
         str(ROOT / "desktron/tests/test_pi_sdk_discovery.py"), "--required", "--report", str(report)],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    result = json.loads(report.read_text(encoding="utf-8"))
    assert proc.returncode != 0 and not result["ok"]
    assert result["skipped"] > 0
    assert result["missing_required_cases"]


def test_named_delivery_scenarios_cannot_be_removed_or_under_parameterized():
    coverage = gate.Coverage()
    coverage.collected = coverage.passed = {"test_proxy.py::test_route[environment]"}
    assert coverage.result(0, True)["ok"]
    result = coverage.result(0, True, {"test_route": 4, "test_state": 1})
    assert not result["ok"]
    assert result["missing_required_cases"] == ["test_route", "test_state"]


def test_optional_platform_skips_are_distinct_from_required_dependency_skips():
    coverage = gate.Coverage()
    coverage.collected = coverage.skipped = {"test_fixture.py::test_posix_only"}
    assert coverage.result(0, False)["ok"]
    assert not coverage.result(0, True)["ok"]


def test_non_windows_gate_rejects_instead_of_accepting_windows_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.sys, "platform", "linux")
    report = tmp_path / "report.json"
    assert gate.run(report) == 1
    result = json.loads(report.read_text(encoding="utf-8"))
    assert not result["ok"] and result["suites"] == {}
    assert result["model_requests"] == "not_run"


def test_wrong_python_version_cannot_certify_the_managed_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.sys, "platform", "win32")
    monkeypatch.setattr(gate.platform, "python_version", lambda: "0.0.0")
    report = tmp_path / "report.json"
    assert gate.run(report) == 1
    result = json.loads(report.read_text(encoding="utf-8"))
    assert not result["ok"] and result["suites"] == {}
    assert "managed Python" in result["error"]


def test_report_replaces_previous_success_atomically(tmp_path):
    path = tmp_path / "report.json"
    gate.save_report(path, {"ok": True})
    gate.save_report(path, {"ok": False})
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": False}
    assert list(tmp_path.iterdir()) == [path]


def _archive(tmp_path, *, dirty=False, mutate=None):
    stage = tmp_path / "package"
    stage.mkdir()
    files = {
        "Desktron.exe": b"synthetic-exe",
        "install.ps1": b"# synthetic installer",
        "release-rev": b"fixture-revision\n", "release-build": b"123\n",
        "payload/.release-rev": b"fixture-revision\n", "payload/.release-build": b"123\n",
        "payload/desktron/lib/provision/install_check.py": b"# synthetic check",
        "payload/frontend/index.html": b"<!doctype html>",
    }
    for name, body in files.items():
        path = stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    manifest = release.write_manifest(stage, "fixture-revision", "123", "x64", dirty,
                                      {"ok": True, "model_requests": "not_run"})
    if mutate:
        mutate(stage)
    archive = tmp_path / "candidate.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        for path in stage.rglob("*"):
            if path.is_file():
                stream.write(path, "package/" + path.relative_to(stage).as_posix())
    return archive, manifest


def test_final_archive_integrity_and_provenance(tmp_path):
    archive, manifest = _archive(tmp_path)
    package = release.verify_archive(archive, tmp_path / "extract", manifest)
    assert (package / "Desktron.exe").read_bytes() == b"synthetic-exe"
    assert manifest["release_eligible"] and not manifest["source_dirty"]
    assert manifest["model_requests"] == "not_run"
    assert len(release.sha256(archive)) == 64


@pytest.mark.parametrize("mutation", [
    lambda stage: (stage / "Desktron.exe").write_bytes(b"tampered"),
    lambda stage: (stage / "install.ps1").unlink(),
    lambda stage: (stage / "extra.txt").write_text("unexpected", encoding="utf-8"),
    lambda stage: (stage / "release-manifest.json").write_text("{}", encoding="utf-8"),
])
def test_archive_tamper_missing_extra_or_changed_manifest_is_rejected(tmp_path, mutation):
    archive, manifest = _archive(tmp_path, mutate=mutation)
    with pytest.raises(ValueError):
        release.verify_archive(archive, tmp_path / "extract", manifest)


def test_matching_hash_does_not_mask_host_payload_revision_mismatch(tmp_path):
    archive, manifest = _archive(tmp_path)
    marker = tmp_path / "package/payload/.release-rev"
    marker.write_text("different-revision\n", encoding="utf-8")
    manifest = release.write_manifest(tmp_path / "package", "fixture-revision", "123", "x64", False, {})
    # write_manifest should not hash a previous manifest into itself.
    assert "release-manifest.json" not in manifest["files"]
    with zipfile.ZipFile(archive, "w") as stream:
        for path in (tmp_path / "package").rglob("*"):
            if path.is_file():
                stream.write(path, "package/" + path.relative_to(tmp_path / "package").as_posix())
    with pytest.raises(ValueError, match="provenance"):
        release.verify_archive(archive, tmp_path / "extract", manifest)


@pytest.mark.parametrize("name", ["../outside", "/absolute", "package/../../outside",
                                  "C:/absolute/file", "package\\outside", "other/file"])
def test_archive_unsafe_paths_or_multiple_roots_are_rejected(tmp_path, name):
    archive, manifest = _archive(tmp_path)
    with zipfile.ZipFile(archive, "a") as stream:
        stream.writestr(name, "fixture")
    with pytest.raises(ValueError):
        release.verify_archive(archive, tmp_path / "extract", manifest)
    assert not (tmp_path / "outside").exists()


def test_duplicate_archive_entries_are_rejected(tmp_path):
    archive, manifest = _archive(tmp_path)
    with zipfile.ZipFile(archive, "a") as stream:
        with pytest.warns(UserWarning):
            stream.writestr("package/Desktron.exe", "duplicate")
    with pytest.raises(ValueError, match="duplicate"):
        release.verify_archive(archive, tmp_path / "extract", manifest)


def test_dirty_experimental_artifact_is_not_release_eligible(tmp_path):
    _, manifest = _archive(tmp_path, dirty=True)
    assert manifest["source_dirty"]
    assert manifest["release_eligible"] is False


@pytest.mark.parametrize("failure", [None, "exit", "missing-check", "model-request", "wrong-mode"])
def test_artifact_acceptance_requires_every_offline_check(tmp_path, monkeypatch, failure):
    archive, manifest = _archive(tmp_path)

    def run(argv, **kwargs):
        assert Path(argv[1]) == tmp_path / "extract/package/payload/desktron/lib/provision/install_check.py"
        assert Path(argv[argv.index("--root") + 1]) == tmp_path / "extract/package/payload"
        report = {"ok": True, "mode": "isolated-offline", "model_requests": "not_run",
                  "checks": {name: "passed" for name in release.OFFLINE_CHECKS}}
        if failure == "missing-check":
            report["checks"].pop("session_creation_offline")
        if failure == "model-request":
            report["model_requests"] = "run"
        if failure == "wrong-mode":
            report["mode"] = "production"
        Path(argv[argv.index("--report") + 1]).write_text(json.dumps(report), encoding="utf-8")
        return SimpleNamespace(returncode=1 if failure == "exit" else 0)

    monkeypatch.setattr(release.subprocess, "run", run)
    if failure is None:
        assert release.accept_archive(archive, tmp_path / "extract", manifest)["ok"]
    else:
        with pytest.raises(ValueError, match="acceptance"):
            release.accept_archive(archive, tmp_path / "extract", manifest)


def _mock_release(tmp_path, monkeypatch, *, dirty=False, changed=False):
    root = tmp_path / "repo"
    (root / "scripts/windows").mkdir(parents=True)
    (root / "scripts/windows/install.ps1").write_text("# fixture", encoding="utf-8")
    (root / "manifest.yaml").write_text("version: 1\n", encoding="utf-8")
    exe = root / "fixture.exe"
    exe.write_bytes(b"synthetic-exe")
    monkeypatch.setattr(release, "ROOT", str(root))
    monkeypatch.setattr(release.sys, "platform", "win32")
    monkeypatch.setattr(release.platform, "machine", lambda: "AMD64")
    monkeypatch.setenv("DESKTRON_BUILD_NUMBER", "1234")
    monkeypatch.setattr(release.windows_host, "git_rev", lambda root: "fixture-revision")
    monkeypatch.setattr(release.windows_host, "build_exe", lambda *args: str(exe))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "status" in argv:
            count = sum("status" in call for call in calls)
            status = " M fixture.py\n" if dirty or (changed and count > 1) else ""
            return SimpleNamespace(returncode=0, stdout=status)
        if argv[1].endswith("windows_release_gate.py"):
            Path(argv[argv.index("--report") + 1]).write_text(
                json.dumps({"ok": True, "model_requests": "not_run"}), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    def stage(_root, dest):
        path = Path(dest) / "desktron/lib/provision/install_check.py"
        path.parent.mkdir(parents=True)
        path.write_text("# fixture", encoding="utf-8")
        return 1

    monkeypatch.setattr(release.subprocess, "run", run)
    monkeypatch.setattr(release, "stage_payload", stage)
    monkeypatch.setattr(release, "accept_archive", lambda *args: {"ok": True, "model_requests": "not_run"})
    return root, calls


def test_failed_final_zip_acceptance_never_publishes_a_candidate(tmp_path, monkeypatch):
    root, _ = _mock_release(tmp_path, monkeypatch)

    def fail(*args):
        raise ValueError("fixture acceptance failure")

    monkeypatch.setattr(release, "accept_archive", fail)
    assert release.main([]) == 1
    assert not (root / "dist").exists()


def test_source_change_during_build_never_publishes_a_candidate(tmp_path, monkeypatch):
    root, _ = _mock_release(tmp_path, monkeypatch, changed=True)
    assert release.main([]) == 1
    assert not (root / "dist").exists()


def test_dirty_tree_refused_before_build_or_gate(tmp_path, monkeypatch):
    root, calls = _mock_release(tmp_path, monkeypatch, dirty=True)
    assert release.main([]) == 2
    assert len(calls) == 1 and "status" in calls[0]
    assert not (root / "dist").exists()


def test_experimental_zip_and_evidence_are_bound_to_same_bytes(tmp_path, monkeypatch):
    root, _ = _mock_release(tmp_path, monkeypatch, dirty=True)
    assert release.main(["--allow-dirty"]) == 0
    archives = list((root / "dist").glob("*.zip"))
    assert len(archives) == 1 and archives[0].stem.endswith("-experimental")
    archive = archives[0]
    evidence = json.loads(Path(str(archive) + ".acceptance.json").read_text(encoding="utf-8"))
    assert evidence["sha256"] == release.sha256(archive)
    assert evidence["release_eligible"] is False
    with zipfile.ZipFile(archive) as stream:
        manifest = json.loads(stream.read(archive.stem + "/release-manifest.json"))
    assert manifest["release_eligible"] is False
    assert not list((root / "dist").glob("*.partial"))


def test_ci_prepares_sdk_and_runs_final_zip_gate_in_both_utf8_modes():
    source = (ROOT / ".github/workflows/check.yml").read_text(encoding="utf-8")
    assert "python-utf8: ['0', '1']" in source
    assert "@earendil-works/pi-coding-agent@1.0.2" in source
    assert "run: python scripts/dev/release_windows.py" in source
    assert "dist/desktron-windows-*.zip.acceptance.json" in source
    for name in gate.REQUIRED:
        assert name in gate.REQUIRED_CASES


def test_checkout_byte_policy_and_updated_reviews_agree():
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in attrs
    reviews = json.loads((ROOT / "scripts/public-tree-review.json").read_text(encoding="utf-8"))
    entries = {row["path"]: row for row in reviews["reviews"]}
    for name in ("docs/release.md", "desktron/tests/test_codex_appserver.py"):
        data = (ROOT / name).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(data).hexdigest() == entries[name]["sha256"]


def test_installer_refuses_live_app_before_any_provisioning_or_payload_write():
    source = (ROOT / "scripts/windows/install.ps1").read_text(encoding="utf-8-sig")
    refusal = source.index("if ($running)")
    assert refusal < source.index("if (-not (Test-WebView2))")
    assert refusal < source.index("$Python = Install-DeskPython")
    assert refusal < source.index("& robocopy.exe")
    assert source.count("if ($running)") == 1
    assert "不能作为交付验收通过的证据" in source
