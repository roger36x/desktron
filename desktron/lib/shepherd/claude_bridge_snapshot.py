"""Process-pinned Claude JS bridge bundle.

sync-home may replace product files while the Desk Python process is alive. Read
all mutually dependent bridge modules at import time and materialize those exact
bytes for every child launched by this process.
"""
import atexit
import os
from pathlib import Path
import shutil
import tempfile
import threading

_NAMES = ('claude_runner.mjs', 'claude_resident_runner.mjs', 'claude_bootstrap.mjs',
          'claude_store_bridge.mjs', 'claude_sqlite_store.mjs',
          'claude_memory_store.mjs', 'claude_unified_bootstrap.mjs',
          'claude_sdk_options.mjs', 'claude_binary_version.mjs',
          'claude_dispatch.mjs', 'fallback_receipt.mjs')
_SOURCE = Path(__file__).resolve().parent
_BUNDLE = {name: (_SOURCE / name).read_bytes() for name in _NAMES}
_LOCK = threading.Lock()
_ROOT = ''


def bridge_path(name):
    if name not in _BUNDLE:
        raise ValueError('unknown Claude bridge module')
    global _ROOT
    with _LOCK:
        if not _ROOT:
            root = tempfile.mkdtemp(prefix='desktron-claude-bridge-')
            os.chmod(root, 0o700)
            for filename, data in _BUNDLE.items():
                path = os.path.join(root, filename)
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o500)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data); stream.flush(); os.fsync(stream.fileno())
            _ROOT = root
        return os.path.join(_ROOT, name)


def _cleanup():
    if _ROOT:
        shutil.rmtree(_ROOT, ignore_errors=True)


atexit.register(_cleanup)
