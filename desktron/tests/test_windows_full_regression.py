"""Real filesystem/SQLite regressions behind the Windows full-gate failures."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest
import oscompat
import usage_stats
from owned_process import writer_lock_path
from shepherd.history_retrieval import HistoryRetrieval
from shepherd.turn_inputs import TurnInputs
from _platform import version_cli

WINDOWS = pytest.mark.skipif(os.name != 'nt', reason='Windows CRT/NTFS/npm 契约')


class TrackedConnection(sqlite3.Connection):
    closed = False
    fail_secure_delete = False

    def execute(self, sql, *args, **kwargs):
        if self.fail_secure_delete and sql == 'PRAGMA secure_delete=ON':
            raise sqlite3.DatabaseError('synthetic damaged index')
        return super().execute(sql, *args, **kwargs)

    def close(self):
        self.closed = True
        super().close()


@contextmanager
def tracked_connections(monkeypatch):
    connect = sqlite3.connect
    opened = []
    def create(*args, **kwargs):
        db = connect(*args, factory=TrackedConnection, **kwargs)
        opened.append(db)
        return db
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, 'connect', create)
        yield opened


def test_usage_connections_commit_rollback_and_close_without_gc(tmp_path, monkeypatch):
    with tracked_connections(monkeypatch) as opened:
        with usage_stats._connect(tmp_path) as db:
            db.execute('CREATE TABLE regression(value TEXT)')
            db.execute('INSERT INTO regression VALUES(?)', ('committed',))
        with pytest.raises(RuntimeError, match='rollback'):
            with usage_stats._connect(tmp_path) as db:
                db.execute('INSERT INTO regression VALUES(?)', ('must rollback',))
                raise RuntimeError('rollback')
        with usage_stats._connect(tmp_path) as db:
            assert [row[0] for row in db.execute('SELECT value FROM regression')] == ['committed']
    assert len(opened) == 3 and all(db.closed for db in opened)
    (tmp_path / 'usage' / 'usage.sqlite3').unlink()


def test_damaged_index_initialization_closes_before_cleanup(tmp_path, monkeypatch):
    retrieval = HistoryRetrieval(tmp_path)
    with tracked_connections(monkeypatch) as opened:
        monkeypatch.setattr(TrackedConnection, 'fail_secure_delete', True)
        with pytest.raises(sqlite3.DatabaseError, match='synthetic damaged index'):
            with retrieval._db():
                pytest.fail('damaged initialization was accepted')
    assert len(opened) == 1 and opened[0].closed
    retrieval.path.unlink()


@WINDOWS
def test_windows_input_names_are_bounded_and_prior_digest_layout_is_readable(tmp_path):
    inputs = TurnInputs(tmp_path)
    public = inputs.public_input('合成输入')
    ref = inputs.write('conversation', 'conversation:1', public)
    current = Path(inputs.reference_path(ref))
    assert len(current.name) == len('turn-') + 64 + len('.json')
    assert ':' not in current.name
    old_name = 'turn-' + hashlib.sha256(b'conversation:1').hexdigest() + '.' + ref['input_hash'] + '.json'
    prior = current.with_name(old_name)
    current.rename(prior)
    assert inputs.read(ref) == public
    assert inputs.reference_path(ref) == str(prior)
    # A predispatch discard must not remove an already-persisted legacy source.
    before = prior.read_bytes()
    assert not inputs.discard(ref)
    assert prior.read_bytes() == before
    prior.unlink()


@WINDOWS
def test_windows_writer_identity_does_not_create_an_alternate_data_stream(tmp_path):
    first = Path(writer_lock_path(tmp_path, 'shepherd:binding-a'))
    second = Path(writer_lock_path(tmp_path, 'shepherd:binding-b'))
    assert first != second and ':' not in first.name
    first.write_bytes(b'writer fence')
    assert first.name in os.listdir(tmp_path)
    assert first.read_bytes() == b'writer fence'
    assert Path(writer_lock_path(tmp_path, 'adp-safe')).name == 'adp-safe.native-writer.lock'


@WINDOWS
def test_windows_junction_is_rejected_by_pathlib_safety_checks(tmp_path):
    outside = tmp_path / 'outside'; outside.mkdir()
    alias = tmp_path / 'alias'
    os.symlink(outside, alias, target_is_directory=True)
    assert oscompat.is_link(alias) and alias.is_symlink()
    inputs = TurnInputs(tmp_path / 'state')
    (tmp_path / 'state' / 'deleted').parent.mkdir()
    os.symlink(outside, tmp_path / 'state' / 'deleted', target_is_directory=True)
    from shepherd.physical_delete import marker_path
    from shepherd.store import Conflict
    with pytest.raises(Conflict, match='符号链接'):
        marker_path(inputs.root, 'conversation')
    assert not list(outside.iterdir())


@WINDOWS
def test_windows_crt_long_open_keeps_binary_bytes(tmp_path):
    path = tmp_path / ('long-' + 'x' * 180)
    assert len(str(path)) > 260
    payload = b'line\n\x1a\x00tail'
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            assert os.write(fd, payload) == len(payload)
        finally:
            os.close(fd)
        fd = os.open(path, os.O_RDONLY)
        try:
            assert os.read(fd, 100) == payload
        finally:
            os.close(fd)
    finally:
        # The test owns this one file; use the native long-path spelling for
        # cleanup rather than ignoring errors or depending on garbage collection.
        os.unlink('\\\\?\\' + str(path))


@WINDOWS
def test_windows_claude_npm_version_probe_is_exact_and_shell_free(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.fail('Node is required for the Windows Claude bridge regression')
    binary = version_cli(tmp_path / '含空格 version fixture', '2.1.283')
    module = Path(__file__).resolve().parents[1] / 'lib/shepherd/claude_binary_version.mjs'
    script = "const {claudeBinaryVersion}=await import(process.argv[1]); console.log(claudeBinaryVersion(process.argv[2],{}));"
    result = subprocess.run([node, '--input-type=module', '-e', script, module.as_uri(), str(binary)],
                            capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '2.1.283'
    assert not (tmp_path / 'shell-side-effect').exists()
