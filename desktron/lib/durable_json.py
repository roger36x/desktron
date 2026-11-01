"""Versioned JSON snapshots: serialized, durable, conflict-aware read/modify/write.

Keep the deployed file format. A stale caller may change independent fields, but
must never overwrite another caller's edit or turn an unreadable file into {}.
All writers must use this module; flock also serializes independent processes.
"""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import copy
from contextlib import contextmanager
import fcntl
import json
import os
import tempfile
import threading
import time

_guard = threading.Lock()
_locks = {}
_missing = object()


class ConflictError(ValueError):
    pass


class Snapshot(dict):
    def __init__(self, path, value):
        super().__init__(value)
        self.path = path
        self.base = copy.deepcopy(dict(value))


def _path(path):
    return os.path.abspath(os.fspath(path))


# Readers do not take the lock. On Windows, opening the target while a writer's
# os.replace is swapping it in fails with ERROR_ACCESS_DENIED (errno 13) for a few
# milliseconds; writers already retry the replace, readers retry the open.
_WINDOWS_READ_RETRY_S = 2.0


def _read_text(path):
    deadline = None
    delay = 0.01
    while True:
        try:
            with open(path, encoding='utf-8') as stream:
                return stream.read()
        except PermissionError:
            if not oscompat.IS_WINDOWS:
                raise
            now = time.monotonic()
            if deadline is None:
                deadline = now + _WINDOWS_READ_RETRY_S
            if now >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


def _load(path):
    try:
        value = json.loads(_read_text(path))
    except FileNotFoundError:
        # A dangling symlink is damage, not a new installation.
        if os.path.lexists(path):
            raise
        return {}
    if not isinstance(value, dict):
        raise ValueError('索引格式错误；原文件已保留：' + path)
    return value


def read(path):
    path = _path(path)
    return Snapshot(path, _load(path))


def read_value(path):
    """Return a fresh owned value for readers, without a write/CAS baseline.

    Nested values belong to this call. Use read() when a later write() is
    needed; a plain value is deliberately not accepted by write().
    """
    return _load(_path(path))


@contextmanager
def locked(path):
    path = _path(path)
    with _guard:
        lock = _locks.setdefault(path, threading.RLock())
    with lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def _merge(base, changed, current):
    if changed == base:
        return current
    if current == base or current == changed:
        return changed
    if isinstance(changed, dict) and isinstance(current, dict) and (base is _missing or isinstance(base, dict)):
        base = {} if base is _missing else base
        out = {}
        for key in base.keys() | changed.keys() | current.keys():
            value = _merge(base.get(key, _missing), changed.get(key, _missing), current.get(key, _missing))
            if value is not _missing:
                out[key] = value
        return out
    raise ConflictError('索引已被其他操作更新；请重新读取后重试')


def _replace(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.' + os.path.basename(path) + '-', dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        oscompat.fsync_directory(os.path.dirname(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write(path, snapshot):
    path = _path(path)
    if not isinstance(snapshot, Snapshot) or snapshot.path != path:
        raise ValueError('保存索引需要同一路径的读取快照')
    with locked(path):
        if snapshot.base and not os.path.exists(path):
            raise ConflictError('索引在读取后消失；禁止以旧快照新建覆盖')
        current = _load(path)
        result = _merge(snapshot.base, snapshot, current)
        if result != current:
            if os.path.exists(path):
                _replace(path + '.previous', current)
            _replace(path, result)
        # Refresh only after successful durable commit. Failed calls can retry.
        # Keep unchanged row objects alive: adapters can hold a reference to a
        # live turn's record across saves. Only reconcile differing fields.
        def refresh(target, source):
            for key in list(target):
                if key not in source:
                    del target[key]
            for key, value in source.items():
                if isinstance(target.get(key), dict) and isinstance(value, dict):
                    refresh(target[key], value)
                elif key not in target or target[key] != value:
                    target[key] = copy.deepcopy(value)
        refresh(snapshot, result)
        snapshot.base = copy.deepcopy(dict(result))
    return snapshot
