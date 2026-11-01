"""Managed, pinned Claude Agent SDK dependency for the Shepherd connector."""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from durable_json import locked
from .claude_codec import SDK_VERSION

_lock = threading.Lock()


def home():
    try:
        from deskcfg import DATA_HOME
        base = os.path.dirname(DATA_HOME)
    except Exception:
        base = os.path.expanduser('~/.local/share/desktron')
    return os.path.join(base, 'runtime', 'claude-agent-sdk')


def sdk_root(root=None):
    return os.path.join(root or home(), 'node_modules', '@anthropic-ai', 'claude-agent-sdk')


def available(root=None):
    path = sdk_root(root)
    try:
        with open(os.path.join(path, 'package.json'), encoding='utf-8') as stream:
            package = json.load(stream)
        return (package.get('version') == SDK_VERSION
                and os.path.isfile(os.path.join(path, 'sdk.mjs')))
    except (OSError, ValueError, TypeError):
        return False


def lockfile():
    return Path(__file__).with_name('runtime') / 'claude-agent-sdk-package-lock.json'


def ensure(root=None, npm=None):
    """Install only the SDK package; its optional bundled Claude binaries stay omitted."""
    with _lock:
        directory = os.path.abspath(os.fspath(root or home()))
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try: os.chmod(directory, 0o700)
        except OSError: pass
        with locked(os.path.join(directory, 'install')):
            if available(directory):
                return True, sdk_root(directory)
            package = os.path.join(directory, 'package.json')
            lock_target = os.path.join(directory, 'package-lock.json')
            values = {
                package: json.dumps({'name': 'desktron-shepherd-claude-runtime', 'private': True,
                                     'dependencies': {'@anthropic-ai/claude-agent-sdk': SDK_VERSION}},
                                    ensure_ascii=False, indent=2) + '\n',
                lock_target: lockfile().read_text(encoding='utf-8'),
            }
            for target, body in values.items():
                fd, temporary = tempfile.mkstemp(prefix='.package-', dir=directory)
                try:
                    os.fchmod(fd, 0o600)
                    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                        stream.write(body); stream.flush(); os.fsync(stream.fileno())
                    os.replace(temporary, target)
                finally:
                    if os.path.exists(temporary): os.unlink(temporary)
            command = npm or shutil.which('npm')
            if not command:
                return False, 'npm 不在 PATH 上'
            try:
                result = subprocess.run([command, 'ci', '--no-audit', '--no-fund',
                                         '--ignore-scripts', '--omit=optional', '--legacy-peer-deps'],
                                        cwd=directory, capture_output=True, text=True,
                                        encoding='utf-8', errors='replace', timeout=600)
            except Exception as exc:
                return False, 'Claude Agent SDK 安装无法启动：' + str(exc)[:160]
            if result.returncode or not available(directory):
                return False, 'Claude Agent SDK 安装失败：' + (result.stderr or result.stdout or '')[-240:]
            return True, sdk_root(directory)
