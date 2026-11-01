"""Leased profile contexts, synthetic records only; no CLI/model/auth requests."""
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from engine_profiles import ProfileError
from ordinary_engine_runtime import OrdinaryEngineRuntime

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX descriptor backend only')


@pytest.fixture(params=['claude', 'codex', 'pi'])
def runtime(tmp_path, request):
    root = tmp_path / 'data'
    root.mkdir(mode=0o700)
    return OrdinaryEngineRuntime(root, request.param)


def native(runtime, native_id, suffix=b''):
    engine = runtime.profile.engine
    header = {'claude': {'sessionId': native_id, 'type': 'user', 'cwd': '/same/project'},
              'codex': {'type': 'session_meta', 'payload': {'id': native_id, 'cwd': '/same/project'}},
              'pi': {'type': 'session', 'id': native_id, 'cwd': '/same/project', 'version': 3}}[engine]
    raw = (json.dumps(header) + '\n').encode() + suffix
    relative = {'claude': Path('projects/project') / (native_id + '.jsonl'),
                'codex': Path('sessions/date') / ('rollout-time-' + native_id + '.jsonl'),
                'pi': Path('sessions') / ('time_' + native_id + '.jsonl')}[engine]
    path = Path(runtime.binding.home) / relative
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o600)
    return path, raw


def registered(runtime, sid='desk-session', native_id='native-session'):
    path, raw = native(runtime, native_id)
    runtime.registry.register(sid, runtime.profile, runtime.binding, native_id,
                              hashlib.sha256(raw).hexdigest())
    return path, raw


def parent():
    return {'HOME': '/synthetic/global-home', 'PATH': '/synthetic/bin',
            'CODEX_HOME': '/global/codex', 'CLAUDE_CONFIG_DIR': '/global/claude',
            'PI_CODING_AGENT_DIR': '/global/pi', 'PI_CODING_AGENT_SESSION_DIR': '/global/history',
            'PI_SESSION_ID': 'external', 'PI_SESSION_FILE': '/external/file',
            'SYNTHETIC_AUTH': 'synthetic-value-not-a-credential'}


def test_resume_context_uses_registered_profile_not_parent_roots(runtime):
    path, raw = registered(runtime)
    env = parent()
    original = dict(env)
    with runtime.writer() as lease:
        ctx = lease.resume_context('desk-session', env)
        assert ctx.native_id == 'native-session'
        assert ctx.storage_revision == hashlib.sha256(raw).hexdigest()
        assert ctx.scope_id == runtime.binding.scope_id
        assert ctx.generation == runtime.binding.generation
        assert ctx.env['HOME'] == original['HOME']
        key = {'claude': 'CLAUDE_CONFIG_DIR', 'codex': 'CODEX_HOME', 'pi': 'PI_CODING_AGENT_DIR'}[ctx.engine]
        assert ctx.env[key] == runtime.binding.home
        assert '/global/' not in repr(ctx.session_args)
        assert 'SYNTHETIC_AUTH' not in repr(ctx)
        if ctx.engine == 'claude':
            assert ctx.session_args == ('--resume', 'native-session')
        elif ctx.engine == 'pi':
            assert ctx.session_args == ('--session-dir', str(Path(runtime.binding.home) / 'sessions'),
                                        '--session', str(path))
        else:
            assert ctx.session_args == ()  # RPC thread/resume, not a CLI guess
        assert env == original


def test_unregistered_or_same_cwd_record_never_resumed(runtime):
    native(runtime, 'native-session')
    with runtime.writer() as lease, pytest.raises(ProfileError, match='not_registered'):
        lease.resume_context('legacy-desk-session', parent())
    assert runtime.owned() == {}


def test_missing_private_file_does_not_resume_external_copy(runtime, tmp_path, monkeypatch):
    path, raw = registered(runtime)
    outside = tmp_path / 'global-copy.jsonl'
    outside.write_bytes(raw)
    path.unlink()
    monkeypatch.setenv('HOME', str(tmp_path))
    with runtime.writer() as lease, pytest.raises(ProfileError, match='history_missing'):
        lease.resume_context('desk-session', parent())
    assert outside.read_bytes() == raw


def test_resume_revision_published_only_with_active_ticket_and_cas(runtime):
    path, raw = registered(runtime)
    with runtime.writer() as lease:
        ctx = lease.resume_context('desk-session', parent())
        updated = raw + b'{"type":"synthetic-event"}\n'
        path.write_bytes(updated)
        # Public history must not observe uncommitted native bytes.
        with pytest.raises(ProfileError, match='revision_mismatch'):
            runtime.snapshot('desk-session')
        result = lease.publish()
        assert result.raw == updated
        assert result.revision != ctx.storage_revision
        assert runtime.snapshot('desk-session') == result
        with pytest.raises(ProfileError, match='not_pending'):
            lease.publish()
    with pytest.raises(ProfileError, match='not_active'):
        lease.publish()


def test_conflicting_publication_not_overwritten(runtime):
    path, raw = registered(runtime)
    old = hashlib.sha256(raw).hexdigest()
    with runtime.writer() as lease:
        lease.resume_context('desk-session', parent())
        runtime.registry.advance_revision('desk-session', runtime.profile, runtime.binding,
                                          old, '0' * 64)
        with pytest.raises(ProfileError, match='revision_conflict'):
            lease.publish()
    assert runtime.owned()['desk-session']['storage_revision'] == '0' * 64
    assert path.read_bytes() == raw


def test_busy_writer_is_explicit_and_new_object_cannot_bypass_lock(runtime):
    other = OrdinaryEngineRuntime(runtime.profile.root, runtime.profile.engine)
    with runtime.writer():
        with pytest.raises(ProfileError, match='native_writer_busy'):
            with other.writer():
                pytest.fail('second Writer entered')
    with other.writer() as lease:
        assert lease.active


def test_lock_replacement_rejected_before_publication(runtime):
    registered(runtime)
    lock = Path(runtime.profile.root) / ('.ordinary-writer-' + runtime.profile.engine + '.lock')
    with pytest.raises(ProfileError, match='lock_changed'):
        with runtime.writer() as lease:
            lease.resume_context('desk-session', parent())
            lock.rename(lock.with_suffix('.detached'))
            lock.touch(mode=0o600)
            other = OrdinaryEngineRuntime(runtime.profile.root, runtime.profile.engine)
            with pytest.raises(ProfileError, match='lock_changed'):
                with other.writer():
                    pytest.fail('replacement lock admitted a second Writer')
            with pytest.raises(ProfileError, match='lock_changed'):
                lease.publish()
    assert runtime.owned()['desk-session']['storage_revision'] != '0' * 64


def test_writer_exception_releases_lease_but_does_not_publish(runtime):
    path, raw = registered(runtime)
    with pytest.raises(RuntimeError, match='synthetic-failure'):
        with runtime.writer() as lease:
            lease.resume_context('desk-session', parent())
            path.write_bytes(raw + b'{"type":"synthetic-event"}\n')
            raise RuntimeError('synthetic-failure')
    with runtime.writer() as lease, pytest.raises(ProfileError, match='revision_mismatch'):
        lease.resume_context('desk-session', parent())


def test_tombstone_blocks_resume_and_creation(runtime):
    _, raw = registered(runtime)
    runtime.registry.tombstone('desk-session', runtime.profile, runtime.binding,
                               hashlib.sha256(raw).hexdigest())
    with runtime.writer() as lease:
        with pytest.raises(ProfileError, match='deleted'):
            lease.resume_context('desk-session', parent())
        with pytest.raises(ProfileError, match='deleted'):
            lease.new_context('desk-session', parent())


def test_unknown_or_linked_native_revision_not_published(runtime, tmp_path):
    path, raw = registered(runtime)
    with runtime.writer() as lease:
        lease.resume_context('desk-session', parent())
        path.unlink()
        outside = tmp_path / 'outside.jsonl'
        outside.write_bytes(raw)
        outside.chmod(0o600)
        path.symlink_to(outside)
        with pytest.raises(ProfileError, match='link_not_allowed'):
            lease.publish()
    assert outside.read_bytes() == raw


def test_created_binding_only_after_verified_native_persistence(runtime):
    if runtime.profile.engine == 'codex':
        with runtime.writer() as lease, pytest.raises(ProfileError, match='receipt_integration_required'):
            lease.new_context('desk-new', parent())
        assert runtime.owned() == {}
        return
    with runtime.writer() as lease:
        ctx = lease.new_context('desk-new', parent())
        assert ctx.session_args[-2:] == ('--session-id', ctx.native_id)
        assert runtime.owned() == {}
        with pytest.raises(ProfileError, match='history_missing'):
            lease.publish()
        _, raw = native(runtime, ctx.native_id)
        assert lease.publish().raw == raw
    assert runtime.owned()['desk-new']['source'] == 'created'
    # Repeated creation is not a retry of the native model request.
    with runtime.writer() as lease, pytest.raises(ProfileError, match='ownership_conflict'):
        lease.new_context('desk-new', parent())


def test_single_lease_cannot_hold_multiple_launch_tickets(runtime):
    registered(runtime)
    with runtime.writer() as lease:
        lease.resume_context('desk-session', parent())
        with pytest.raises(ProfileError, match='already_prepared'):
            lease.resume_context('desk-session', parent())


def test_auth_status_and_login_use_execution_profile_without_copying_tokens(runtime):
    if runtime.profile.engine == 'pi':
        with pytest.raises(ProfileError, match='not_integrated'):
            runtime.auth_context(parent())
        with pytest.raises(ProfileError, match='not_integrated'):
            runtime.auth_context(parent(), operation='login')
        return
    before = sorted(p.name for p in Path(runtime.binding.home).iterdir())
    login = runtime.auth_context(parent(), operation='login')
    status = runtime.auth_context(parent(), operation='status')
    assert login.scope_id == status.scope_id == runtime.binding.scope_id
    assert login.native_id == status.native_id == ''
    expected = {'claude': (('auth', 'login'), ('auth', 'status')),
                'codex': (('login',), ('login', 'status'))}[runtime.profile.engine]
    assert (login.session_args, status.session_args) == expected
    assert sorted(p.name for p in Path(runtime.binding.home).iterdir()) == before
    assert 'SYNTHETIC_AUTH' not in repr(login)
    with pytest.raises(ProfileError, match='not_integrated'):
        runtime.auth_context(parent(), operation='unknown')


def test_profile_replacement_invalidates_all_command_contexts(runtime):
    home = Path(runtime.binding.home)
    home.rename(home.with_name('detached'))
    home.mkdir(mode=0o700)
    with pytest.raises(ProfileError):
        runtime.auth_context(parent())
    with pytest.raises(ProfileError):
        with runtime.writer():
            pytest.fail('replaced profile accepted')


def test_new_uuid_collision_does_not_adopt_an_existing_private_record(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    root.mkdir(mode=0o700)
    runtime = OrdinaryEngineRuntime(root, 'claude')
    import uuid
    collision = uuid.UUID('00000000-0000-4000-8000-000000000001')
    native(runtime, str(collision))
    monkeypatch.setattr('ordinary_engine_runtime.uuid.uuid4', lambda: collision)
    with runtime.writer() as lease, pytest.raises(ProfileError, match='already_exists'):
        lease.new_context('desk-new', parent())
    assert runtime.owned() == {}


@pytest.mark.parametrize('engine', ['claude', 'pi'])
def test_unconfirmed_creation_survives_reopen_and_never_auto_retries(tmp_path, engine):
    root = tmp_path / 'data'
    root.mkdir(mode=0o700)
    runtime = OrdinaryEngineRuntime(root, engine)
    with pytest.raises(RuntimeError, match='crash'):
        with runtime.writer() as lease:
            ctx = lease.new_context('desk-new', parent())
            native(runtime, ctx.native_id)
            raise RuntimeError('synthetic crash before publication')
    reopened = OrdinaryEngineRuntime(root, engine)
    assert reopened.owned() == {}
    with reopened.writer() as lease:
        with pytest.raises(ProfileError, match='unconfirmed'):
            lease.resume_context('desk-new', parent())
        with pytest.raises(ProfileError, match='ownership_conflict'):
            lease.new_context('desk-new', parent())


def test_reservation_blocks_cross_sid_adoption_and_wrong_native_completion(runtime):
    runtime.registry.reserve_created('desk-one', runtime.profile, runtime.binding,
                                      'native-one')
    with pytest.raises(ProfileError, match='already_owned'):
        runtime.registry.register('desk-two', runtime.profile, runtime.binding,
                                   'native-one', '1' * 64)
    with pytest.raises(ProfileError, match='ownership_conflict'):
        runtime.registry.register('desk-one', runtime.profile, runtime.binding,
                                   'native-two', '1' * 64)
    with pytest.raises(ProfileError, match='ownership_conflict'):
        runtime.registry.reserve_created('desk-one', runtime.profile, runtime.binding,
                                          'native-one')
    with pytest.raises(ProfileError, match='not_registered'):
        runtime.registry.advance_revision('desk-one', runtime.profile, runtime.binding,
                                           '0' * 64, '1' * 64)
    with pytest.raises(ProfileError, match='not_registered'):
        runtime.registry.tombstone('desk-one', runtime.profile, runtime.binding, '0' * 64)
    assert runtime.owned() == {}


@pytest.mark.parametrize('damage', ['missing', 'null', 'bad-inode', 'bad-type'])
def test_initialized_writer_anchor_damage_never_reinitializes(runtime, damage):
    with runtime.writer():
        pass
    root = Path(runtime.profile.root)
    anchor = root / ('ordinary-writer-' + runtime.profile.engine + '-anchor.json')
    before = (root / ('.ordinary-writer-' + runtime.profile.engine + '.lock')).stat()
    if damage == 'missing':
        anchor.unlink()
    else:
        value = {'null': None, 'bad-inode': {'version': 1, 'lock_identity': [0, 0]},
                 'bad-type': {'version': True, 'lock_identity': [before.st_dev, before.st_ino]}}[damage]
        anchor.write_text(json.dumps(value))
    with pytest.raises(ProfileError):
        with runtime.writer():
            pytest.fail('damaged lease anchor accepted')


def test_unanchored_lock_requires_explicit_recovery(runtime):
    lock = Path(runtime.profile.root) / ('.ordinary-writer-' + runtime.profile.engine + '.lock')
    lock.touch(mode=0o600)
    with pytest.raises(ProfileError, match='anchor_missing'):
        with runtime.writer():
            pytest.fail('adopted unanchored lock')


def test_active_lease_rejects_profile_switch_before_context(runtime):
    registered(runtime)
    home = Path(runtime.binding.home)
    with pytest.raises(ProfileError):
        with runtime.writer() as lease:
            home.rename(home.with_name('detached'))
            home.mkdir(mode=0o700)
            with pytest.raises(ProfileError):
                lease.resume_context('desk-session', parent())


@pytest.mark.parametrize('engine', ['claude', 'pi'])
def test_registered_uuid_collision_rejected_even_when_native_file_missing(tmp_path, monkeypatch, engine):
    import uuid
    collision = uuid.UUID('00000000-0000-4000-8000-000000000002')
    root = tmp_path / 'data'
    root.mkdir(mode=0o700)
    runtime = OrdinaryEngineRuntime(root, engine)
    path, _ = registered(runtime, native_id=str(collision))
    path.unlink()
    monkeypatch.setattr('ordinary_engine_runtime.uuid.uuid4', lambda: collision)
    with runtime.writer() as lease:
        with pytest.raises(ProfileError, match='already_owned'):
            lease.new_context('desk-new', parent())
        assert lease.ticket is None
    assert set(runtime.owned()) == {'desk-session'}


def test_simultaneous_lock_and_anchor_loss_cannot_admit_second_writer(runtime):
    root = Path(runtime.profile.root)
    lock = root / ('.ordinary-writer-' + runtime.profile.engine + '.lock')
    anchor = root / ('ordinary-writer-' + runtime.profile.engine + '-anchor.json')
    with pytest.raises(ProfileError):
        with runtime.writer():
            lock.rename(lock.with_suffix('.detached'))
            anchor.unlink()
            other = OrdinaryEngineRuntime(root, runtime.profile.engine)
            with pytest.raises(ProfileError, match='initialized_native_writer'):
                with other.writer():
                    pytest.fail('second Writer entered after dual metadata loss')
            assert not lock.exists() and not anchor.exists()


def test_initialization_interruption_after_profile_receipt_is_not_first_use(runtime, monkeypatch):
    import ordinary_engine_runtime as module
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic anchor publication failure')
    monkeypatch.setattr(module, 'replace_json', fail)
    with pytest.raises(RuntimeError, match='publication failure'):
        with runtime.writer():
            pytest.fail('incomplete initialization accepted')
    monkeypatch.undo()
    with pytest.raises(ProfileError, match='initialized_native_writer'):
        with runtime.writer():
            pytest.fail('recreated initialized lock/anchor')


def test_missing_independent_profile_receipt_never_adopts_root_anchor(runtime):
    with runtime.writer():
        pass
    marker = Path(runtime.binding.home) / 'profile.json'
    value = json.loads(marker.read_text())
    del value['writer_lock_identity']
    marker.write_text(json.dumps(value))
    with pytest.raises(ProfileError, match='initialization_receipt_missing'):
        with runtime.writer():
            pytest.fail('adopted root anchor without independent receipt')


def test_writer_exclusion_holds_across_python_processes(runtime):
    import subprocess
    code = '''
import sys
from engine_profiles import ProfileError
from ordinary_engine_runtime import OrdinaryEngineRuntime
r = OrdinaryEngineRuntime(sys.argv[1], sys.argv[2])
try:
    with r.writer():
        sys.exit(3)
except ProfileError as exc:
    sys.exit(0 if str(exc) == 'native_writer_busy' else 4)
'''
    env = {'PATH': os.environ.get('PATH', ''), 'HOME': runtime.profile.root,
           'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'lib')}
    with runtime.writer():
        result = subprocess.run([sys.executable, '-c', code, runtime.profile.root,
                                 runtime.profile.engine], env=env, capture_output=True,
                                timeout=10)
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.parametrize('engine', ['claude', 'pi'])
def test_abrupt_child_exit_releases_lease_without_losing_creation_fence(tmp_path, engine):
    import subprocess
    root = tmp_path / 'data'
    root.mkdir(mode=0o700)
    code = '''
import os, sys
from ordinary_engine_runtime import OrdinaryEngineRuntime
r = OrdinaryEngineRuntime(sys.argv[1], sys.argv[2])
with r.writer() as lease:
    lease.new_context('desk-crashed', {})
    os._exit(0)
'''
    env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(tmp_path),
           'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'lib')}
    result = subprocess.run([sys.executable, '-c', code, str(root), engine], env=env,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    runtime = OrdinaryEngineRuntime(root, engine)
    with runtime.writer() as lease:
        with pytest.raises(ProfileError, match='ownership_conflict'):
            lease.new_context('desk-crashed', {})
        with pytest.raises(ProfileError, match='unconfirmed'):
            lease.resume_context('desk-crashed', {})
