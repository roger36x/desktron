#!/usr/bin/env python3
"""产出 Windows 发行包：payload + Desktron.exe + install.ps1 → dist/desktron-windows-<架构>-v<版本>-<时间>.zip。

在 Windows 上运行(需要 Rust 工具链与 Node)。用户侧：解压 → 运行 install.ps1。

用法：python scripts/dev/release_windows.py [--allow-dirty] [--target x86_64-pc-windows-msvc]
"""
import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import payload  # noqa: E402
import windows_host  # noqa: E402

ARCH = {"x86_64-pc-windows-msvc": "x64", "aarch64-pc-windows-msvc": "arm64"}
OFFLINE_CHECKS = {
    "python_packages", "backend_probe", "frontend_entry", "session_creation_offline",
    "shepherd_initialization_offline", "shepherd_status_offline",
}


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_manifest(stage, rev, build, arch, dirty, gate):
    """Provenance and file integrity, not a signature or model-access claim."""
    from pathlib import Path
    stage = Path(stage)
    files = {path.relative_to(stage).as_posix(): sha256(path)
             for path in sorted(stage.rglob("*"))
             if path.is_file() and path != stage / "release-manifest.json"}
    manifest = {
        "schema": 1, "revision": rev, "build": build, "architecture": arch,
        "source_dirty": dirty, "release_eligible": not dirty,
        "verification_scope": "offline-payload", "model_requests": "not_run",
        "delivery_gate": gate, "files": files,
    }
    with (stage / "release-manifest.json").open("w", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return manifest


def verify_archive(archive_path, destination, expected):
    """Verify the actual ZIP before running acceptance on its extracted payload."""
    from pathlib import Path, PurePosixPath
    destination = Path(destination)
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or not names:
            raise ValueError("Archive has duplicate entries or is empty")
        roots = set()
        for name in names:
            path = PurePosixPath(name)
            if (path.is_absolute() or ".." in path.parts or "\\" in name
                    or ":" in name or len(path.parts) < 2):
                raise ValueError("Unsafe archive path")
            roots.add(path.parts[0])
        if len(roots) != 1:
            raise ValueError("Archive must contain one package root")
        archive.extractall(destination)
    package = destination / roots.pop()
    manifest = json.loads((package / "release-manifest.json").read_text(encoding="utf-8"))
    if manifest != expected:
        raise ValueError("Archive manifest differs from the build manifest")
    actual = {path.relative_to(package).as_posix(): sha256(path)
              for path in package.rglob("*")
              if path.is_file() and path != package / "release-manifest.json"}
    if actual != manifest["files"]:
        raise ValueError("Archive file integrity mismatch")
    for marker, value in (("release-rev", manifest["revision"]),
                          ("release-build", manifest["build"]),
                          ("payload/.release-rev", manifest["revision"]),
                          ("payload/.release-build", manifest["build"])):
        if (package / marker).read_text(encoding="utf-8").strip() != value:
            raise ValueError("Native host and payload provenance mismatch")
    for required in ("Desktron.exe", "install.ps1", "payload/desktron/lib/provision/install_check.py"):
        if not (package / required).is_file():
            raise ValueError("Incomplete release archive")
    return package


def accept_archive(archive_path, destination, manifest):
    package = verify_archive(archive_path, destination, manifest)
    report_path = os.path.join(destination, "artifact-install-check.json")
    proc = subprocess.run(
        [sys.executable, str(package / "payload/desktron/lib/provision/install_check.py"),
         "--root", str(package / "payload"), "--report", report_path], timeout=180)
    with open(report_path, encoding="utf-8") as stream:
        report = json.load(stream)
    if (proc.returncode or report.get("ok") is not True
            or report.get("mode") != "isolated-offline"
            or report.get("model_requests") != "not_run"
            or any(report.get("checks", {}).get(name) != "passed" for name in OFFLINE_CHECKS)):
        raise ValueError("Extracted ZIP failed offline acceptance")
    return report


def tracked_files(root):
    out = subprocess.run(["git", "-C", root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                         capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p and os.path.lexists(os.path.join(root, p))]


def stage_payload(root, dest):
    """只取 git 已知文件，再按 payload-excludes 过滤(与 release.sh 两步一致)。"""
    patterns = payload.load_patterns()
    count = 0
    for rel in tracked_files(root):
        rel = rel.replace("\\", "/")
        parts = rel.split("/")
        if any(payload.excluded("/".join(parts[:i]), patterns) for i in range(1, len(parts) + 1)):
            continue
        src = os.path.join(root, *parts)
        target = os.path.join(dest, *parts)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(src, target)
        count += 1
    return count


def payload_hygiene(dest):
    pattern = re.compile(r"ou_[a-z0-9]{10,}|oc_[a-z0-9]{10,}|\bt-[a-zA-Z0-9_]{16,}\b")
    hits = []
    docs = os.path.join(dest, "desk-docs")
    for folder, _dirs, files in os.walk(docs):
        for name in files:
            path = os.path.join(folder, name)
            try:
                text = open(path, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            if pattern.search(text):
                hits.append(os.path.relpath(path, dest))
    if os.path.exists(os.path.join(dest, "knowledge")):
        hits.append("knowledge/")
    return hits


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-dirty", action="store_true", help="允许未提交改动(仅试打包)")
    parser.add_argument("--target", default="x86_64-pc-windows-msvc", choices=sorted(ARCH))
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        print("release_windows: 需要在 Windows 上运行。", file=sys.stderr)
        return 2
    status = subprocess.run(["git", "-C", ROOT, "status", "--porcelain"],
                            capture_output=True, text=True, encoding="utf-8", check=True).stdout
    dirty = bool(status.strip())
    if dirty and not args.allow_dirty:
        print("发布被拒绝：工作树含未提交改动。先完成评审并形成干净快照。", file=sys.stderr)
        return 2
    manifest = open(os.path.join(ROOT, "manifest.yaml"), encoding="utf-8").read()
    version = (re.search(r"(?m)^version:\s*(\d+)", manifest) or [None, "0"])[1]
    rev = windows_host.git_rev(ROOT)
    if rev == "dev":
        print("发布被拒绝：无法确定源码 revision。", file=sys.stderr)
        return 2
    if ARCH[args.target] != {"AMD64": "x64", "ARM64": "arm64", "x86_64": "x64"}.get(platform.machine()):
        print("发布被拒绝：需要与目标架构一致的原生 Windows 验收机。", file=sys.stderr)
        return 2
    build = os.environ.get("DESKTRON_BUILD_NUMBER") or time.strftime("%Y%m%d%H%M%S", time.gmtime())
    if not build.isdigit():
        print("DESKTRON_BUILD_NUMBER 必须是正整数", file=sys.stderr)
        return 2
    arch = ARCH[args.target]
    name = "desktron-windows-%s-v%s-%s%s" % (
        arch, version, build, "-experimental" if dirty else "")
    work = tempfile.mkdtemp(prefix="desk-release-")
    stage = os.path.join(work, name)
    try:
        print("[1/7] 公共树审查与 Windows 离线交付门禁 …")
        if subprocess.run([sys.executable, os.path.join(ROOT, "scripts/check_public_tree.py"),
                           "--reviewed"], cwd=ROOT, timeout=120).returncode:
            return 1
        gate_path = os.path.join(work, "windows-delivery-gate.json")
        if subprocess.run([sys.executable, os.path.join(ROOT, "scripts/dev/windows_release_gate.py"),
                           "--report", gate_path], cwd=ROOT, timeout=1800).returncode:
            return 1
        with open(gate_path, encoding="utf-8") as stream:
            gate = json.load(stream)
        if gate.get("ok") is not True or gate.get("model_requests") != "not_run":
            return 1
        print("[2/7] 构建 Windows 原生壳 …")
        exe = windows_host.build_exe(ROOT, args.target)
        os.makedirs(stage)
        shutil.copy2(exe, os.path.join(stage, "Desktron.exe"))
        windows_host.write_resources(stage, rev, build)
        print("[3/7] 暂存 payload(排除见 payload-excludes.txt)…")
        dest = os.path.join(stage, "payload")
        count = stage_payload(ROOT, dest)
        print("  %d 个文件；构建官方 App 静态资产 …" % count)
        builder = os.path.join(ROOT, "scripts", "dev", "build_builtin_apps.py")
        if subprocess.run([sys.executable, builder, os.path.join(dest, "frontend", "app-assets")]).returncode:
            return 1
        hits = payload_hygiene(dest)
        if hits:
            print("payload 卫生检查未通过：" + ", ".join(hits), file=sys.stderr)
            return 1
        for marker, value in ((".release-rev", rev), (".release-build", build)):
            with open(os.path.join(dest, marker), "w", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
        print("[4/7] 写 install.ps1 与发行清单 …")
        shutil.copy2(os.path.join(ROOT, "scripts", "windows", "install.ps1"), os.path.join(stage, "install.ps1"))
        manifest_data = write_manifest(stage, rev, build, arch, dirty, gate)
        print("[5/7] 生成候选 ZIP（验收前不进入 dist）…")
        candidate = os.path.join(work, name + ".zip")
        with zipfile.ZipFile(candidate, "w", zipfile.ZIP_DEFLATED) as archive:
            for folder, _dirs, files in os.walk(stage):
                for file in files:
                    path = os.path.join(folder, file)
                    archive.write(path, os.path.relpath(path, work))
        print("[6/7] 解包校验与最终 payload 隔离验收 …")
        acceptance = accept_archive(candidate, os.path.join(work, "verify"), manifest_data)
        # A shared working tree must not change unnoticed during a long build.
        final_status = subprocess.run(["git", "-C", ROOT, "status", "--porcelain"],
                                      capture_output=True, text=True, encoding="utf-8", check=True).stdout
        if not dirty and (final_status.strip() or windows_host.git_rev(ROOT) != rev):
            raise ValueError("Source snapshot changed during release")
        print("[7/7] 发布已验收的同一个 ZIP 与摘要证据 …")
        os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
        out = os.path.join(ROOT, "dist", name + ".zip")
        if os.path.exists(out):
            raise ValueError("Refusing to overwrite an existing release")
        digest = sha256(candidate)
        evidence = {
            "schema": 1, "artifact": os.path.basename(out), "sha256": digest,
            "revision": rev, "build": build, "architecture": arch,
            "source_dirty": dirty, "release_eligible": not dirty,
            "delivery_status": "experimental" if dirty else "offline-candidate",
            "installer_acceptance": "not_run", "native_cold_start": "not_run",
            "model_access": "not_run", "model_requests": "not_run",
            "delivery_gate": gate, "artifact_acceptance": acceptance,
            "limits": ["ZIP payload checked; installer, native GUI cold start and model access require separate acceptance."],
        }
        with open(out + ".acceptance.json", "w", encoding="utf-8") as stream:
            json.dump(evidence, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        with open(out + ".sha256", "w", encoding="ascii") as stream:
            stream.write(digest + "  " + os.path.basename(out) + "\n")
        partial = out + ".partial"
        try:
            shutil.copy2(candidate, partial)
            os.rename(partial, out)
        finally:
            if os.path.exists(partial):
                os.unlink(partial)
        print("试验包（不可正式交付）：" + out if dirty else "产物：" + out)
        print("SHA-256：" + digest)
        print("用户侧：解压后在包目录运行 powershell -ExecutionPolicy Bypass -File .\\install.ps1")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print("发布被拒绝：" + type(error).__name__, file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
