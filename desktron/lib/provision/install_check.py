"""Offline installation acceptance using the real backend in an empty sandbox.

No existing HOME, credentials, engine environment, scheduler or running Desk is
reused. A passing report proves boot/import/routes, not login or model access.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def isolated_env(root: Path, sandbox: Path) -> dict[str, str]:
    home = sandbox / "home"
    home.mkdir()
    workspace = sandbox / "workspace"
    workspace.mkdir()
    # Whitelist, not a credential-name blacklist: API/provider/host tokens and
    # user package/config overrides must not survive into the acceptance child.
    return {
        "HOME": str(home), "TMPDIR": str(sandbox),
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
        "LANG": "en_US.UTF-8", "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": str(root / "desktron/lib"),
        "XDG_CONFIG_HOME": str(sandbox / "engine-home"),
        "XDG_DATA_HOME": str(sandbox / "data"),
        "DESKTRON_ENGINE_HOME": str(sandbox / "engine-home"),
        "DESKTRON_DATA_HOME": str(sandbox / "data"),
        "DESKTRON_WORKSPACE": str(workspace),
        "DESKTRON_DREAM": "off", "MEMORY_ENGINE_DREAM": "off",
        "DESKTRON_INSTALL_CHECK": "1", "DESKTRON_REMOTE_BIND": "loopback",
        "DESKTRON_ROLE": "local",
        **_windows_env(home, sandbox),
    }


def _windows_env(home: Path, sandbox: Path) -> dict[str, str]:
    """Windows 子进程离不开的系统变量；用户目录同样指向沙箱。

    Python 在 Windows 上按 USERPROFILE 展开 ~，Winsock 初始化需要 SYSTEMROOT。
    """
    if sys.platform != "win32":
        return {}
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return {
        "USERPROFILE": str(home), "HOMEDRIVE": home.drive, "HOMEPATH": str(home)[len(home.drive):],
        "LOCALAPPDATA": str(sandbox / "localappdata"), "APPDATA": str(sandbox / "appdata"),
        "TEMP": str(sandbox), "TMP": str(sandbox),
        "SYSTEMROOT": system_root, "WINDIR": os.environ.get("WINDIR", system_root),
        "COMSPEC": os.environ.get("COMSPEC", system_root + r"\System32\cmd.exe"),
        "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), system_root + r"\System32", system_root]),
    }


def _get(opener, url: str, timeout: float) -> bytes:
    with opener.open(url, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError("HTTP acceptance returned a non-200 response")
        return response.read(2 * 1024 * 1024)


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _diagnostic(log: Path, root: Path, sandbox: Path) -> str:
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        tail = lines[-1] if lines else "backend produced no output"
    except OSError:
        tail = "backend log unavailable"
    return tail.replace(str(root), "<payload>").replace(str(sandbox), "<sandbox>")[:400]


def check(root: Path, timeout: float = 30) -> dict:
    root = root.resolve()
    report = {
        "schema": 1, "checked_at": datetime.now(timezone.utc).isoformat(),
        "ok": False, "mode": "isolated-offline",
        "python": platform.python_version(), "architecture": platform.machine(),
        "checks": {}, "model_requests": "not_run",
        "limits": ["Does not verify native supervisor, existing login, model access or browser interaction."],
    }
    if sys.version_info < (3, 11):
        report["error"] = "Python >=3.11 required"
        return report
    backend = root / "desktron/lib/ui_server.py"
    frontend = root / "frontend"
    if not backend.is_file() or not (frontend / "index.html").is_file():
        report["error"] = "Incomplete payload: backend or frontend missing"
        return report
    if importlib.util.find_spec("yaml") is None:
        report["error"] = "Missing Python runtime package PyYAML; run desktron prepare"
        return report
    if importlib.util.find_spec("PIL") is None:
        report["error"] = "Missing Python runtime package Pillow; run desktron prepare"
        return report
    if sys.platform == "win32" and importlib.util.find_spec("winpty") is None:
        report["error"] = "Missing Python runtime package pywinpty; run the Windows installer again"
        return report
    report["checks"]["python_packages"] = "passed"
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="desk-install-check-") as directory:
        sandbox = Path(directory)
        env = isolated_env(root, sandbox)
        log = sandbox / "backend.log"
        # Reserve a random loopback port, then release immediately before spawn.
        # The tiny bind race fails explicitly; pid verification prevents accepting
        # some other process's /desk-probe response.
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        proc = None
        try:
            with log.open("wb") as stream:
                proc = subprocess.Popen(
                    [sys.executable, str(backend), str(frontend), str(port), "0", env["DESKTRON_WORKSPACE"]],
                    cwd=env["DESKTRON_WORKSPACE"], env=env,
                    stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                deadline = started + timeout
                probe = None
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        raise RuntimeError("Backend exited before readiness: " + _diagnostic(log, root, sandbox))
                    try:
                        value = json.loads(_get(opener, f"http://127.0.0.1:{port}/desk-probe",
                                                min(1, max(0.01, deadline - time.monotonic()))))
                    except (OSError, ValueError, urllib.error.URLError):
                        time.sleep(0.05)
                        continue
                    if not isinstance(value, dict) or value.get("desk") is not True or value.get("pid") != proc.pid:
                        raise RuntimeError("Probe identity mismatch; did not accept another backend")
                    if not value.get("boot_id"):
                        raise RuntimeError("Probe has no backend boot identity")
                    if value.get("role") != "local" or value.get("supervised") is not False:
                        raise RuntimeError("Isolated probe must not impersonate a production supervisor")
                    probe = value
                    break
                if probe is None:
                    raise RuntimeError("Backend startup timed out: " + _diagnostic(log, root, sandbox))
                report["checks"]["backend_probe"] = "passed"
                page = _get(opener, f"http://127.0.0.1:{port}/", 3)
                if b"<html" not in page.lower() and b"<!doctype html" not in page.lower():
                    raise RuntimeError("Frontend entry did not return HTML")
                report["checks"]["frontend_entry"] = "passed"
                # Boot/read-only checks miss failures after an index rename.
                # Create empty sessions twice per engine in this sandbox only:
                # exercise both the initial commit and last-good backup, without
                # starting an engine or sending a prompt/model request.
                for engine in ("claude", "codex", "pi"):
                    created = []
                    for _ in range(2):
                        request = urllib.request.Request(
                            f"http://127.0.0.1:{port}/session",
                            data=json.dumps({"engine": engine}).encode("utf-8"),
                            headers={"Content-Type": "application/json"}, method="POST",
                        )
                        row = json.loads(_get(opener, request, 5))
                        sid = row.get("id") if isinstance(row, dict) else None
                        if not isinstance(sid, str) or not sid or sid in created:
                            raise RuntimeError(f"Empty {engine} session creation failed")
                        created.append(sid)
                    index = sandbox / "data" / "adapter" / engine / "sessions.json"
                    persisted = json.loads(index.read_text(encoding="utf-8"))
                    previous = json.loads(Path(str(index) + ".previous").read_text(encoding="utf-8"))
                    if not all(sid in persisted for sid in created) or created[0] not in previous:
                        raise RuntimeError(f"Empty {engine} sessions were not persisted with a backup")
                report["checks"]["session_creation_offline"] = "passed"
                # Loading a Runner used to fail only on the first user turn:
                # context_policy.json contains UTF-8 text. Disable Python UTF-8
                # mode here so a Windows locale cannot be masked by the installer.
                initialize = subprocess.run(
                    [sys.executable, "-X", "utf8=0", "-c",
                     "from types import SimpleNamespace; import sys; "
                     "from shepherd.unified_runners import UnifiedRunner; "
                     "[UnifiedRunner(SimpleNamespace(engine=e, store_root=sys.argv[1], "
                     "binding={'binding_id':'fixture','conversation_id':'fixture'}), "
                     "command=['unused'], env={}) for e in ('claude','codex','pi')]; "
                     "print('shepherd-ready')", env["DESKTRON_WORKSPACE"]],
                    env=env, cwd=env["DESKTRON_WORKSPACE"], stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                )
                if initialize.returncode or initialize.stdout.strip() != "shepherd-ready":
                    raise RuntimeError("Shepherd offline runner initialization failed")
                report["checks"]["shepherd_initialization_offline"] = "passed"
                status = subprocess.run(
                    [sys.executable, "-m", "shepherd.cli", "status", "--offline"], env=env,
                    cwd=env["DESKTRON_WORKSPACE"], stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=timeout,
                )
                if status.returncode:
                    raise RuntimeError("Shepherd status failed with exit code %s" % status.returncode)
                connectors = json.loads(status.stdout).get("connectors", {})
                if set(connectors) != {"claude", "codex", "pi"}:
                    raise RuntimeError("Shepherd status returned an incomplete connector matrix")
                if any(item.get("availability", {}).get("reason") != "not_checked"
                       or item.get("enabled") is not False for item in connectors.values()):
                    raise RuntimeError("Offline Shepherd status must not probe or enable engines")
                report["checks"]["shepherd_status_offline"] = "passed"
                report["ok"] = True
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            report["error"] = str(error).replace(str(root), "<payload>").replace(str(sandbox), "<sandbox>")[:500]
        finally:
            if proc is not None:
                _stop(proc)
    report["duration_s"] = round(time.monotonic() - started, 3)
    return report


def save_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".install-check-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 120:
        parser.error("--timeout must be >0 and <=120")
    def interrupted(*_):
        # Default SIGTERM would bypass Python finally blocks and orphan the
        # separately-sessioned backend; unwind through check() cleanup instead.
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted)
    code = None
    try:
        report = check(args.root, args.timeout)
    except KeyboardInterrupt:
        report = {"schema": 1, "ok": False, "mode": "isolated-offline",
                  "checks": {}, "model_requests": "not_run",
                  "error": "Installation check interrupted; owned backend cleaned up"}
        code = 130
    finally:
        signal.signal(signal.SIGTERM, previous)
    if args.report:
        save_report(args.report, report)
    print(json.dumps(report, ensure_ascii=False))
    return code if code is not None else (0 if report["ok"] else 1)


if __name__ == "__main__":
    raise SystemExit(main())
