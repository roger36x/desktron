"""Explicit platform contracts for offline fixtures; never fabricate capabilities."""
from contextlib import contextmanager
import os
import json
from pathlib import Path
import sqlite3
import stat
import queue
import threading

import oscompat
import pytest


def assert_private_file(path, mode=0o600):
    info = os.stat(path)
    assert stat.S_ISREG(info.st_mode)
    assert info.st_uid == os.getuid()
    # POSIX mode bits are not an ACL proof on Windows. The Windows contract
    # validates regular files/identity; installation owns the private ACL root.
    if not oscompat.IS_WINDOWS:
        assert stat.S_IMODE(info.st_mode) == mode


def assert_private_directory(path):
    info = os.stat(path)
    assert stat.S_ISDIR(info.st_mode)
    assert not Path(path).is_symlink()
    if not oscompat.IS_WINDOWS:
        assert stat.S_IMODE(info.st_mode) == 0o700


@contextmanager
def database(path):
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


def version_cli(path, version):
    """Real version process, using a native npm JS entry on Windows."""
    path = Path(path)
    if os.name == 'nt':
        entry = path.with_suffix('.mjs')
        entry.write_text('console.log(' + json.dumps(version + ' (Claude Code)') + ');\n', encoding='utf-8')
        path = path.with_suffix('.cmd')
        path.write_text('@node "%~dp0' + entry.name + '" %*\n', encoding='utf-8')
    else:
        path.write_text('#!/bin/sh\nprintf "' + version + ' (Claude Code)\\n"\n', encoding='utf-8')
        path.chmod(0o700)
    return path


def readline(stream, timeout):
    """Bounded pipe read; Windows select only accepts sockets."""
    result = queue.Queue()
    def read():
        try:
            result.put((stream.readline(), None))
        except Exception as error:
            result.put((None, error))
    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    value, error = result.get(timeout=timeout)
    if error:
        raise error
    return value


def symlink(target, link, directory=False):
    # The product convenience fallback may produce a hard link/copy. Those
    # are NOT a symlink fixture and must not stand in for a rejection test.
    native = getattr(oscompat, 'native_symlink', os.symlink)
    try:
        native(target, link, target_is_directory=directory)
    except OSError as error:
        if oscompat.IS_WINDOWS and getattr(error, 'winerror', None) == 1314:
            pytest.skip('真实符号链接需要 Windows 开发者模式或创建链接权限')
        raise
