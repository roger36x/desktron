"""Ownership contracts using only synthetic native identities and revisions."""
import json
import multiprocessing
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from engine_profiles import EngineProfile, ProfileError
import session_ownership as ownership
from session_ownership import OwnershipError, OwnershipRegistry

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX descriptor backend only')
REV = 'a' * 64
NEXT = 'b' * 64


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path / 'data'
    root.mkdir(mode=0o700)
    profile = EngineProfile(root, 'codex')
    binding = profile.prepare()
    return root, profile, binding, OwnershipRegistry(root)


def test_registration_is_durable_idempotent_and_detached(fixture):
    root, p, b, registry = fixture
    row = registry.register('desk-1', p, b, 'native-1', REV)
    row['native_id'] = 'changed'
    assert registry.resolve('desk-1', p, b)['native_id'] == 'native-1'
    assert registry.register('desk-1', p, b, 'native-1', REV) == registry.resolve('desk-1', p, b)
    assert OwnershipRegistry(root).resolve('desk-1', p, b)['storage_revision'] == REV
    path = root / 'session-ownership' / 'index.json'
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_nlink == 1


def test_missing_legacy_binding_is_not_inferred_or_created(fixture):
    root, p, b, registry = fixture
    native = Path(b.home) / 'sessions' / 'legacy.jsonl'
    native.write_text('synthetic global-looking legacy record')
    with pytest.raises(OwnershipError, match='not_registered'):
        registry.resolve('legacy', p, b)
    assert json.loads((root / 'session-ownership' / 'index.json').read_text())['sessions'] == {}
    assert native.read_text() == 'synthetic global-looking legacy record'


def test_conflict_and_duplicate_native_are_not_overwritten(fixture):
    _, p, b, registry = fixture
    registry.register('desk-1', p, b, 'native-1', REV)
    with pytest.raises(OwnershipError, match='ownership_conflict'):
        registry.register('desk-1', p, b, 'native-2', REV)
    with pytest.raises(OwnershipError, match='already_owned'):
        registry.register('desk-2', p, b, 'native-1', REV)
    assert registry.resolve('desk-1', p, b)['native_id'] == 'native-1'


def test_same_uuid_is_separated_by_engine_scope(fixture):
    root, p, b, registry = fixture
    other = EngineProfile(root, 'claude')
    other_binding = other.prepare()
    registry.register('codex-desk', p, b, 'same-native-id', REV)
    registry.register('claude-desk', other, other_binding, 'same-native-id', REV)
    with pytest.raises(OwnershipError, match='profile_mismatch'):
        registry.resolve('codex-desk', other, other_binding)
    assert registry.resolve('claude-desk', other, other_binding)['engine'] == 'claude'


@pytest.mark.parametrize('source', ['imported', 'migrated'])
def test_noncreated_binding_requires_receipt_identity(fixture, source):
    _, p, b, registry = fixture
    with pytest.raises(ProfileError):
        registry.register('desk-1', p, b, 'native-1', REV, source=source)
    row = registry.register('desk-1', p, b, 'native-1', REV,
                            source=source, receipt_id='receipt-1')
    assert row['receipt_id'] == 'receipt-1'


@pytest.mark.parametrize('sid', ['', '..', '../outside', 'a/b', 'a' * 129, 'a\n'])
def test_invalid_public_ids_do_not_create_registry(fixture, sid):
    root, p, b, registry = fixture
    with pytest.raises(ProfileError):
        registry.register(sid, p, b, 'native-1', REV)
    assert not (root / 'session-ownership').exists()


@pytest.mark.parametrize('rev', ['', 'ABC', 'f' * 63, 'F' * 64, None, 1])
def test_invalid_revision_rejected(fixture, rev):
    _, p, b, registry = fixture
    with pytest.raises(OwnershipError, match='invalid_storage_revision'):
        registry.register('desk-1', p, b, 'native-1', rev)


def test_wrong_data_root_not_accepted(fixture, tmp_path):
    _, p, b, _ = fixture
    another = tmp_path / 'another'
    another.mkdir(mode=0o700)
    with pytest.raises(OwnershipError, match='root_mismatch'):
        OwnershipRegistry(another).register('desk-1', p, b, 'native-1', REV)
    assert list(another.iterdir()) == []


def test_revision_compare_and_swap(fixture):
    _, p, b, registry = fixture
    registry.register('desk-1', p, b, 'native-1', REV)
    registry.advance_revision('desk-1', p, b, REV, NEXT)
    with pytest.raises(OwnershipError, match='revision_conflict'):
        registry.advance_revision('desk-1', p, b, REV, 'c' * 64)
    assert registry.resolve('desk-1', p, b)['storage_revision'] == NEXT


def test_tombstone_does_not_delete_native_or_allow_late_registration(fixture):
    root, p, b, registry = fixture
    native = Path(b.home) / 'sessions' / 'synthetic.jsonl'
    native.write_text('synthetic history')
    registry.register('desk-1', p, b, 'native-1', REV)
    with pytest.raises(OwnershipError, match='revision_conflict'):
        registry.tombstone('desk-1', p, b, NEXT)
    registry.tombstone('desk-1', p, b, REV)
    registry.tombstone('desk-1', p, b, REV)
    raw = json.loads((root / 'session-ownership' / 'index.json').read_text())
    assert raw['sessions']['desk-1'] == {'state': 'deleted'}
    with pytest.raises(OwnershipError, match='conversation_deleted'):
        registry.resolve('desk-1', p, b)
    with pytest.raises(OwnershipError, match='conversation_deleted'):
        registry.register('desk-1', p, b, 'native-1', REV)
    assert native.read_text() == 'synthetic history'


@pytest.mark.parametrize('value', ['null', '{broken', '[]', '{"version":true,"sessions":{}}',
                                  '{"version":1,"sessions":{"desk-1":{"state":"unknown"}}}'])
def test_corrupt_registry_not_replaced(fixture, value):
    root, p, b, registry = fixture
    registry.register('desk-1', p, b, 'native-1', REV)
    path = root / 'session-ownership' / 'index.json'
    path.write_text(value)
    with pytest.raises(ProfileError):
        registry.register('desk-2', p, b, 'native-2', REV)
    assert path.read_text() == value


@pytest.mark.parametrize('target', ['index.json', '.metadata-lock'])
@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'public'])
def test_linked_registry_or_lock_never_changes_external_file(fixture, tmp_path, target, kind):
    root, p, b, registry = fixture
    registry.register('desk-1', p, b, 'native-1', REV)
    path = root / 'session-ownership' / target
    path.unlink()
    external = tmp_path / 'external'
    external.write_text('external synthetic content')
    external.chmod(0o600)
    if kind == 'symlink':
        path.symlink_to(external)
    elif kind == 'hardlink':
        os.link(external, path)
    else:
        path.write_text('{}')
        path.chmod(0o644)
    with pytest.raises((ProfileError, OSError)):
        registry.register('desk-2', p, b, 'native-2', REV)
    assert external.read_text() == 'external synthetic content'


def test_failed_publication_keeps_previous_snapshot(fixture, monkeypatch):
    _, p, b, registry = fixture
    registry.register('desk-1', p, b, 'native-1', REV)
    def fail(*args):
        raise OSError('synthetic publication failure')
    monkeypatch.setattr(ownership, 'replace_json', fail)
    with pytest.raises(ProfileError, match='unavailable'):
        registry.advance_revision('desk-1', p, b, REV, NEXT)
    assert registry.resolve('desk-1', p, b)['storage_revision'] == REV


def _register_process(root, sid, queue):
    try:
        p = EngineProfile(root, 'codex')
        b = p.prepare()
        OwnershipRegistry(root).register(sid, p, b, 'native-' + sid, REV)
        queue.put('ok')
    except Exception as exc:
        queue.put(type(exc).__name__)


def test_concurrent_processes_do_not_lose_registration(fixture):
    root, p, b, registry = fixture
    context = multiprocessing.get_context('fork')
    queue = context.Queue()
    children = [context.Process(target=_register_process, args=(str(root), 'desk-' + str(i), queue))
                for i in range(8)]
    try:
        for child in children:
            child.start()
        assert [queue.get(timeout=10) for _ in children] == ['ok'] * 8
        for child in children:
            child.join(timeout=10)
            assert child.exitcode == 0
        for i in range(8):
            assert registry.resolve('desk-' + str(i), p, b)['native_id'] == 'native-desk-' + str(i)
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join()
        queue.close()


@pytest.mark.parametrize('damage', ['index', 'namespace', 'anchor'])
def test_lost_initialized_registry_cannot_erase_tombstone(fixture, damage):
    root, p, b, registry = fixture
    registry.register('desk-1', p, b, 'native-1', REV)
    registry.tombstone('desk-1', p, b, REV)
    directory = root / 'session-ownership'
    if damage == 'index':
        (directory / 'index.json').unlink()
    elif damage == 'namespace':
        directory.rename(root / 'detached-ownership')
    else:
        (root / 'session-ownership-anchor.json').unlink()
    with pytest.raises(ProfileError):
        OwnershipRegistry(root).register('desk-1', p, b, 'native-1', REV)
    if damage == 'index':
        assert not (directory / 'index.json').exists()
    elif damage == 'namespace':
        assert not directory.exists()
    else:
        assert not (root / 'session-ownership-anchor.json').exists()


@pytest.mark.parametrize('operation', ['register', 'resolve', 'advance', 'tombstone'])
def test_namespace_swap_cannot_report_success(fixture, monkeypatch, operation):
    root, p, b, registry = fixture
    registry.register('desk-1', p, b, 'native-1', REV)
    original = ownership.replace_json
    original_load = registry._load
    replaced = False
    def swap():
        nonlocal replaced
        if not replaced:
            replaced = True
            (root / 'session-ownership').rename(root / 'detached-ownership')
            (root / 'session-ownership').mkdir(mode=0o700)
    def publication(directory, name, value):
        swap()
        original(directory, name, value)
    def load(directory):
        result = original_load(directory)
        swap()
        return result
    if operation == 'resolve':
        monkeypatch.setattr(registry, '_load', load)
    else:
        monkeypatch.setattr(ownership, 'replace_json', publication)
    with pytest.raises(ProfileError, match='directory_changed'):
        if operation == 'register':
            registry.register('desk-2', p, b, 'native-2', REV)
        elif operation == 'resolve':
            registry.resolve('desk-1', p, b)
        elif operation == 'advance':
            registry.advance_revision('desk-1', p, b, REV, NEXT)
        else:
            registry.tombstone('desk-1', p, b, REV)
    assert list((root / 'session-ownership').iterdir()) == []
    with pytest.raises(ProfileError, match='identity_invalid'):
        OwnershipRegistry(root).register('desk-3', p, b, 'native-3', REV)


@pytest.mark.parametrize('target', ['namespace', 'root'])
def test_lock_replacement_during_publication_cannot_report_success(fixture, monkeypatch, target):
    root, p, b, registry = fixture
    registry.register('desk-1', p, b, 'native-1', REV)
    original = ownership.replace_json
    def replace_lock(directory, name, value):
        lock = (root / 'session-ownership/.metadata-lock' if target == 'namespace'
                else root / '.session-ownership-lock')
        lock.unlink()
        lock.write_text('')
        lock.chmod(0o600)
        original(directory, name, value)
    monkeypatch.setattr(ownership, 'replace_json', replace_lock)
    with pytest.raises(ProfileError):
        registry.advance_revision('desk-1', p, b, REV, NEXT)


def test_interrupted_first_initialization_is_not_automatically_adopted(fixture, monkeypatch):
    root, p, b, registry = fixture
    original = ownership.replace_json
    def interrupt(directory, name, value):
        if name == 'session-ownership-anchor.json':
            raise OSError('synthetic initialization interruption')
        return original(directory, name, value)
    monkeypatch.setattr(ownership, 'replace_json', interrupt)
    with pytest.raises(ProfileError, match='unavailable'):
        registry.register('desk-1', p, b, 'native-1', REV)
    assert not (root / 'session-ownership-anchor.json').exists()
    monkeypatch.setattr(ownership, 'replace_json', original)
    with pytest.raises(ProfileError, match='explicit_adoption'):
        registry.register('desk-1', p, b, 'native-1', REV)
    assert json.loads((root / 'session-ownership/index.json').read_text())['sessions'] == {}
