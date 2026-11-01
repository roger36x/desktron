"""Managed, pinned pi-subagents runtime used only by Desk's explicit Pi allowlist."""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from durable_json import locked

VERSION = "0.78.0"
PACKAGE = "pi-subagents"
DIRECT_DEPENDENCIES = {
    "@js-temporal/polyfill": "0.5.1",
    "acorn": "8.18.0",
    "jiti": "2.7.0",
    "undici": "8.10.2",
    "yaml": "2.8.3",
}
MANAGED_CONFIG = {
    "asyncByDefault": True,
    "fleetView": False,
    "asyncWidget": False,
    "waitTool": False,
    "forceTopLevelAsync": False,
    "worktree": False,
    "resultScanLogging": "off",
    "proactiveSkillSubagents": False,
    "scheduledRuns": {"enabled": False},
    "missions": {"enabled": False, "globalIndex": False},
    "intercomBridge": {"mode": "off", "resultDelivery": False},
    "control": {"enabled": False},
    "completionBatch": {"enabled": False},
    "maxSubagentDepth": 1,
    "maxSubagentSpawnsPerSession": 0,
    "maxSubagentSpawnsPerRun": 3,
    "maxActiveAsyncRunsPerSession": 3,
    "globalConcurrencyLimit": 3,
}
_lock = threading.Lock()


def home():
    try:
        from deskcfg import DATA_HOME
        base = os.path.dirname(DATA_HOME)
    except Exception:
        base = os.path.expanduser("~/.local/share/desktron")
    # Versioned immutable slot: self-update prepares the new code before stopping the old
    # Desk, so a future package upgrade must not replace modules under live Pi processes.
    return os.path.join(base, "runtime", "pi-subagents", VERSION + "-desk-v2")


def package_root(root=None):
    return os.path.join(os.fspath(root or home()), "node_modules", PACKAGE)


def extension_entry(root=None):
    return os.path.join(package_root(root), "index.js")


def capability_api(root=None):
    return os.path.join(package_root(root), "src", "api", "capability-ceiling.js")


def child_factory(root=None):
    return os.path.join(package_root(root), "src", "runs", "shared", "child-session.js")


def control_api(root=None):
    return os.path.join(package_root(root), "src", "api", "control-channel.js")


def agent_dir(root=None):
    return os.path.join(os.fspath(root or home()), "agent")


def config_path(root=None):
    return os.path.join(agent_dir(root), "extensions", "subagent", "config.json")


def temp_root(root=None):
    return os.path.join(os.fspath(root or home()), "state")


def _package_version(directory):
    with open(os.path.join(directory, "package.json"), encoding="utf-8") as stream:
        value = json.load(stream)
    return value.get("version") if isinstance(value, dict) else None


def available(root=None):
    base = package_root(root)
    node_modules = os.path.dirname(base)
    try:
        if (_package_version(base) != VERSION
                or not os.path.isfile(extension_entry(root))
                or not os.path.isfile(capability_api(root))
                or not os.path.isfile(child_factory(root))
                or not os.path.isfile(control_api(root))
                or json.loads(Path(config_path(root)).read_text(encoding="utf-8")) != MANAGED_CONFIG):
            return False
        return all(_package_version(os.path.join(node_modules, *name.split("/"))) == version
                   for name, version in DIRECT_DEPENDENCIES.items())
    except (OSError, ValueError, TypeError):
        return False


def lockfile():
    return Path(__file__).with_name("runtime") / "pi-subagents-package-lock.json"


def _write_private(target, body, directory):
    fd, temporary = tempfile.mkstemp(prefix=".package-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _manifest_body():
    return json.dumps({
        "name": "desktron-pi-subagents-runtime",
        "private": True,
        "dependencies": {PACKAGE: VERSION},
    }, ensure_ascii=False, indent=2) + "\n"


def _write_managed_config(directory):
    target = config_path(directory)
    parent = os.path.dirname(target)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    os.chmod(agent_dir(directory), 0o700)
    os.chmod(os.path.join(agent_dir(directory), "extensions"), 0o700)
    os.chmod(parent, 0o700)
    _write_private(target, json.dumps(MANAGED_CONFIG, ensure_ascii=False, indent=2) + "\n", parent)
    os.makedirs(temp_root(directory), mode=0o700, exist_ok=True)
    os.chmod(temp_root(directory), 0o700)


def _recover_swap(directory):
    current = os.path.join(directory, "node_modules")
    backup = os.path.join(directory, ".node_modules.previous")
    if not os.path.exists(current) and os.path.isdir(backup):
        os.replace(backup, current)
    elif os.path.isdir(current) and os.path.exists(backup):
        shutil.rmtree(backup, ignore_errors=True)


def _activate(directory, staged):
    current = os.path.join(directory, "node_modules")
    incoming = os.path.join(staged, "node_modules")
    backup = os.path.join(directory, ".node_modules.previous")
    if os.path.exists(backup):
        shutil.rmtree(backup)
    moved = False
    try:
        if os.path.exists(current):
            os.replace(current, backup)
            moved = True
        os.replace(incoming, current)
    except Exception:
        if moved and not os.path.exists(current) and os.path.exists(backup):
            os.replace(backup, current)
        raise
    finally:
        if os.path.isdir(current) and os.path.exists(backup):
            shutil.rmtree(backup, ignore_errors=True)


def ensure(root=None, npm=None):
    """Install beside the live tree, validate, then swap; never use user Pi packages."""
    with _lock:
        directory = os.path.abspath(os.fspath(root or home()))
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        # Keep the process lock outside the tree being repaired so crash recovery and a later
        # process cannot accidentally lock different inodes while node_modules is switched.
        with locked(directory + ".install"):
            _recover_swap(directory)
            if available(directory):
                return True, extension_entry(directory)
            # A valid package tree with only missing or altered policy state does not need
            # a network repair. Recreate the code-owned config, then verify the full slot.
            try:
                _write_managed_config(directory)
            except OSError as exc:
                return False, "pi-subagents 受管配置写入失败：" + str(exc)[:160]
            if available(directory):
                return True, extension_entry(directory)
            command = npm or shutil.which("npm")
            if not command:
                return False, "npm 不在 PATH 上；请重新运行 desktron prepare"
            parent = os.path.dirname(directory)
            staged = tempfile.mkdtemp(prefix=".pi-subagents-stage-", dir=parent)
            try:
                os.chmod(staged, 0o700)
                _write_private(os.path.join(staged, "package.json"), _manifest_body(), staged)
                _write_private(os.path.join(staged, "package-lock.json"),
                               lockfile().read_text(encoding="utf-8"), staged)
                try:
                    result = subprocess.run([
                        command, "ci", "--no-audit", "--no-fund", "--ignore-scripts",
                        "--omit=optional", "--omit=peer",
                    ], cwd=staged, capture_output=True, text=True, timeout=900)
                except Exception as exc:
                    return False, "pi-subagents 安装无法启动：" + str(exc)[:160]
                _write_managed_config(staged)
                if result.returncode or not available(staged):
                    detail = (result.stderr or result.stdout or "")[-300:]
                    return False, "pi-subagents 固定版本安装失败：" + detail
                _activate(directory, staged)
                _write_private(os.path.join(directory, "package.json"), _manifest_body(), directory)
                _write_private(os.path.join(directory, "package-lock.json"),
                               lockfile().read_text(encoding="utf-8"), directory)
                if not available(directory):
                    return False, "pi-subagents 切换后完整性检查失败；请重新运行 desktron prepare"
                return True, extension_entry(directory)
            finally:
                shutil.rmtree(staged, ignore_errors=True)
