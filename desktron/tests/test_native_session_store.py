"""Private native snapshot contracts; synthetic files, no native model calls."""
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from engine_profiles import EngineProfile, ProfileError
from session_ownership import OwnershipRegistry
from native_session_store import NativeSessionStore

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX descriptor backend only')


@pytest.fixture(params=['claude', 'codex', 'pi'])
def source(tmp_path, request):
    root = tmp_path / 'data'
    root.mkdir(mode=0o700)
    profile = EngineProfile(root, request.param)
    binding = profile.prepare()
    registry = OwnershipRegistry(root)
    native_id = 'synthetic-native-id'
    header = {'claude': {'sessionId': native_id, 'type': 'user'},
              'codex': {'type': 'session_meta', 'payload': {'id': native_id}},
              'pi': {'type': 'session', 'id': native_id, 'version': 3}}[request.param]
    raw = (json.dumps(header) + '\n').encode()
    relative = {'claude': Path('projects/project/synthetic-native-id.jsonl'),
                'codex': Path('sessions/2026/10/04/rollout-time-synthetic-native-id.jsonl'),
                'pi': Path('sessions/time_synthetic-native-id.jsonl')}[request.param]
    path = Path(binding.home) / relative
    parent = Path(binding.home)
    for name in relative.parts[:-1]:
        parent /= name
        parent.mkdir(mode=0o700, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o600)
    digest = hashlib.sha256(raw).hexdigest()
    registry.register('desk-1', profile, binding, native_id, digest)
    return root, profile, binding, registry, path, raw


def test_registered_snapshot_scoped_and_immutable(source):
    root, p, b, registry, path, raw = source
    snapshot = NativeSessionStore(p, b, registry).snapshot('desk-1')
    assert snapshot.raw == raw
    assert snapshot.revision == hashlib.sha256(raw).hexdigest()
    assert snapshot.engine == b.engine and snapshot.scope_id == b.scope_id
    assert snapshot.relative_path == path.relative_to(b.home).as_posix()
    assert set(registry.list_owned(p, b)) == {'desk-1'}


def test_same_cwd_and_native_identity_in_external_home_not_scanned(source, tmp_path, monkeypatch):
    _, p, b, registry, path, raw = source
    external = tmp_path / 'external-home'
    external.mkdir(mode=0o700)
    (external / 'copy.jsonl').write_bytes(raw)
    monkeypatch.setenv('HOME', str(external))
    monkeypatch.setenv('CODEX_HOME', str(external))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(external))
    monkeypatch.setenv('PI_CODING_AGENT_SESSION_DIR', str(external))
    path.unlink()
    with pytest.raises(ProfileError, match='history_missing'):
        NativeSessionStore(p, b, registry).snapshot('desk-1')
    assert (external / 'copy.jsonl').read_bytes() == raw


def test_unregistered_file_not_discovered(source):
    _, p, b, registry, _, _ = source
    with pytest.raises(ProfileError, match='not_registered'):
        NativeSessionStore(p, b, registry).snapshot('outside-sid')
    registry.tombstone('desk-1', p, b, registry.resolve('desk-1', p, b)['storage_revision'])
    assert registry.list_owned(p, b) == {}
    with pytest.raises(ProfileError, match='deleted'):
        NativeSessionStore(p, b, registry).snapshot('desk-1')


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'public', 'fifo'])
def test_unsafe_source_never_read(source, tmp_path, kind):
    _, p, b, registry, path, raw = source
    path.unlink()
    outside = tmp_path / 'outside'
    outside.write_bytes(raw)
    outside.chmod(0o600)
    if kind == 'symlink':
        path.symlink_to(outside)
    elif kind == 'hardlink':
        os.link(outside, path)
    elif kind == 'public':
        path.write_bytes(raw)
        path.chmod(0o644)
    else:
        os.mkfifo(path, 0o600)
    with pytest.raises(ProfileError):
        NativeSessionStore(p, b, registry).snapshot('desk-1')
    assert outside.read_bytes() == raw


def test_changed_history_requires_revision_cas(source):
    _, p, b, registry, path, raw = source
    new = raw + b'{"type":"synthetic-event"}\n'
    path.write_bytes(new)
    with pytest.raises(ProfileError, match='revision_mismatch'):
        NativeSessionStore(p, b, registry).snapshot('desk-1')
    registry.advance_revision('desk-1', p, b, hashlib.sha256(raw).hexdigest(),
                              hashlib.sha256(new).hexdigest())
    assert NativeSessionStore(p, b, registry).snapshot('desk-1').raw == new


def test_duplicate_history_is_ambiguous_not_newest(source):
    _, p, b, registry, path, raw = source
    duplicate_dir = path.parent / 'duplicate'
    duplicate_dir.mkdir(mode=0o700)
    duplicate = duplicate_dir / path.name
    duplicate.write_bytes(raw)
    duplicate.chmod(0o600)
    with pytest.raises(ProfileError, match='ambiguous'):
        NativeSessionStore(p, b, registry).snapshot('desk-1')


@pytest.mark.parametrize('damage', ['truncated', 'invalid-json', 'wrong-identity'])
def test_untrusted_source_identity_rejected_even_with_matching_hash(source, damage):
    _, p, b, registry, path, raw = source
    if damage == 'truncated':
        new = raw.rstrip(b'\n')
    elif damage == 'invalid-json':
        new = b'not-json\n'
    else:
        new = raw.replace(b'synthetic-native-id', b'external-native-id')
    path.write_bytes(new)
    registry.advance_revision('desk-1', p, b, hashlib.sha256(raw).hexdigest(),
                              hashlib.sha256(new).hexdigest())
    with pytest.raises(ProfileError):
        NativeSessionStore(p, b, registry).snapshot('desk-1')


def test_read_size_budget_is_fail_closed(source):
    _, p, b, registry, _, raw = source
    with pytest.raises(ProfileError, match='budget_exceeded'):
        NativeSessionStore(p, b, registry, max_bytes=len(raw) - 1).snapshot('desk-1')


def test_named_entry_replacement_during_read_detected(source, monkeypatch):
    _, p, b, registry, path, raw = source
    store = NativeSessionStore(p, b, registry)
    read = store._read
    def replace(fd):
        result = read(fd)
        path.rename(path.with_suffix('.old'))
        path.write_bytes(raw)
        path.chmod(0o600)
        return result
    monkeypatch.setattr(store, '_read', replace)
    with pytest.raises(ProfileError, match='entry_changed'):
        store.snapshot('desk-1')


def test_namespace_switch_after_read_cannot_return_old_snapshot(source, monkeypatch):
    _, p, b, registry, path, _ = source
    store = NativeSessionStore(p, b, registry)
    read = store._read
    def detach(fd):
        result = read(fd)
        home = Path(b.home)
        home.rename(home.with_name('detached'))
        home.mkdir(mode=0o700)
        return result
    monkeypatch.setattr(store, '_read', detach)
    with pytest.raises(ProfileError):
        store.snapshot('desk-1')
