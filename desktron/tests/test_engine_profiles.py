"""Synthetic, offline profile contracts; never touch a real engine or HOME."""
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import engine_profiles as profiles
from engine_profiles import EngineProfile, ProfileError

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX descriptor backend only')


@pytest.fixture
def root(tmp_path):
    path = tmp_path / 'data'
    path.mkdir(mode=0o700)
    return path


@pytest.mark.parametrize('engine', profiles.ENGINES)
def test_profile_stable_private_layout(root, engine):
    profile = EngineProfile(root, engine)
    binding = profile.prepare()
    assert EngineProfile(root, engine).prepare() == binding
    assert binding.engine == engine
    assert len(binding.scope_id) == 64
    assert Path(binding.home) == root / 'engine-profiles' / engine / 'ordinary'
    for path in (root / 'engine-profiles', Path(binding.home).parent, Path(binding.home),
                 *(Path(binding.home) / name for name in profile.native_dirs)):
        assert path.stat().st_mode & 0o777 == 0o700
    assert (Path(binding.home) / 'profile.json').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('engine', profiles.ENGINES)
def test_environment_rewrites_inherited_roots_without_modifying_home(root, engine):
    profile = EngineProfile(root, engine)
    binding = profile.prepare()
    parent = {'HOME': '/synthetic/home', 'PATH': '/synthetic/bin',
              'CODEX_HOME': '/external/codex', 'CLAUDE_CONFIG_DIR': '/external/claude',
              'PI_CODING_AGENT_DIR': '/external/pi', 'PI_CODING_AGENT_SESSION_DIR': '/external/history',
              'PI_SESSION_ID': 'old-session', 'PI_SESSION_FILE': '/external/old.jsonl'}
    original = dict(parent)
    env = profile.launch_env(binding, parent)
    assert parent == original
    assert env['HOME'] == parent['HOME']
    assert 'PI_SESSION_ID' not in env and 'PI_SESSION_FILE' not in env
    assert env['DESKTRON_ENGINE_SCOPE'] == binding.scope_id
    selected = {'claude': 'CLAUDE_CONFIG_DIR', 'codex': 'CODEX_HOME', 'pi': 'PI_CODING_AGENT_DIR'}
    assert env[selected[engine]] == binding.home
    for key in selected.values():
        assert (key in env) == (key == selected[engine])
    if engine == 'pi':
        assert env['PI_CODING_AGENT_SESSION_DIR'] == str(Path(binding.home) / 'sessions')
        assert profile.session_args(binding) == ['--session-dir', env['PI_CODING_AGENT_SESSION_DIR']]
    else:
        assert profile.session_args(binding) == []


def test_scope_and_cache_partition(root, tmp_path):
    profiles_by_engine = [EngineProfile(root, name) for name in profiles.ENGINES]
    bindings = [p.prepare() for p in profiles_by_engine]
    assert len({b.scope_id for b in bindings}) == 3
    p, b = profiles_by_engine[0], bindings[0]
    key = p.cache_key(b, '/synthetic/bin/claude', 'credential-slot-1')
    assert key != p.cache_key(b, '/synthetic/bin/claude', 'credential-slot-2')
    assert key != p.cache_key(b, '/synthetic/bin/claude-new', 'credential-slot-1')
    another = tmp_path / 'another'
    another.mkdir(mode=0o700)
    assert EngineProfile(another, 'claude').prepare().scope_id != b.scope_id
    with pytest.raises(ProfileError):
        p.cache_key(b, 'claude', 'credential-slot-1')
    with pytest.raises(ProfileError):
        p.cache_key(b, '/synthetic/bin/claude', '../credential')


@pytest.mark.parametrize('value', ['other', '', '../codex', 'codex/ordinary'])
def test_unknown_engine_rejected(root, value):
    with pytest.raises(ProfileError, match='unsupported_engine'):
        EngineProfile(root, value)
    assert list(root.iterdir()) == []


@pytest.mark.parametrize('at', ['root', 'namespace', 'engine', 'ordinary', 'sessions'])
def test_symlink_directories_are_never_followed(root, tmp_path, at):
    outside = tmp_path / 'outside'
    outside.mkdir(mode=0o700)
    target = {'root': root, 'namespace': root / 'engine-profiles',
              'engine': root / 'engine-profiles/codex',
              'ordinary': root / 'engine-profiles/codex/ordinary',
              'sessions': root / 'engine-profiles/codex/ordinary/sessions'}[at]
    if at == 'sessions':
        p = EngineProfile(root, 'codex')
        p.prepare()
        target.rmdir()
    else:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target == root:
            target.rmdir()
    target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ProfileError):
        EngineProfile(root, 'codex').prepare()
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('name', ['profile.json', 'config.toml', 'auth.json', 'state_5.sqlite'])
@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'public', 'fifo'])
def test_unsafe_metadata_is_not_read_repaired_or_overwritten(root, tmp_path, name, kind):
    p = EngineProfile(root, 'codex')
    p.prepare()
    dest = Path(p.home) / name
    if dest.exists():
        dest.unlink()
    outside = tmp_path / 'outside-file'
    outside.write_text('synthetic-private-data')
    outside.chmod(0o600)
    if kind == 'symlink':
        dest.symlink_to(outside)
    elif kind == 'hardlink':
        os.link(outside, dest)
    elif kind == 'public':
        dest.write_text('{}')
        dest.chmod(0o644)
    else:
        os.mkfifo(dest, 0o600)
    with pytest.raises((ProfileError, OSError)):
        p.prepare()
    assert outside.read_text() == 'synthetic-private-data'
    assert dest.lstat()


def test_unmarked_nonempty_home_is_not_silently_adopted(root):
    home = root / 'engine-profiles' / 'codex' / 'ordinary'
    home.mkdir(parents=True, mode=0o700)
    home.parent.chmod(0o700)
    home.parent.parent.chmod(0o700)
    (home / 'old-history.jsonl').write_text('old synthetic record')
    with pytest.raises(ProfileError, match='explicit_adoption'):
        EngineProfile(root, 'codex').prepare()
    assert not (home / 'profile.json').exists()


@pytest.mark.parametrize('marker', [{}, {'version': True, 'engine': 'codex', 'generation': 'a' * 32},
                                 {'version': 1, 'engine': 'pi', 'generation': 'a' * 32},
                                 {'version': 1, 'engine': 'codex', 'generation': '../wrong'}])
def test_marker_identity_strict(root, marker):
    p = EngineProfile(root, 'codex')
    p.prepare()
    path = Path(p.home) / 'profile.json'
    path.write_text(json.dumps(marker))
    with pytest.raises(ProfileError, match='marker_invalid'):
        p.prepare()
    assert json.loads(path.read_text()) == marker


def test_corrupt_marker_is_not_recreated(root):
    p = EngineProfile(root, 'codex')
    p.prepare()
    marker = Path(p.home) / 'profile.json'
    marker.write_text('{broken')
    with pytest.raises(ProfileError, match='corrupt'):
        p.prepare()
    assert marker.read_text() == '{broken'


def test_generation_or_directory_replacement_invalidates_binding(root):
    p = EngineProfile(root, 'pi')
    binding = p.prepare()
    with pytest.raises(ProfileError, match='changed'):
        p.launch_env(dataclasses.replace(binding, generation='0' * 32), {})
    home = Path(p.home)
    home.rename(home.with_name('old'))
    replacement = p.prepare()
    assert replacement.generation != binding.generation
    with pytest.raises(ProfileError, match='changed'):
        p.session_args(binding)


def test_profile_swap_inside_validation_cannot_return_stale_launch_identity(root, monkeypatch):
    p = EngineProfile(root, 'pi')
    binding = p.prepare()
    original = p._binding
    swapped = False
    def replace_after_initial_check(directory):
        nonlocal swapped
        result = original(directory)
        if not swapped:
            swapped = True
            home = Path(p.home)
            home.rename(home.with_name('detached'))
            EngineProfile(root, 'pi').prepare()
        return result
    monkeypatch.setattr(p, '_binding', replace_after_initial_check)
    with pytest.raises(ProfileError, match='profile_changed'):
        p.launch_env(binding, {'HOME': '/synthetic/home'})


def test_no_parent_fallback_after_prepare_failure(root, monkeypatch):
    p = EngineProfile(root, 'codex')
    binding = p.prepare()
    (Path(binding.home) / 'sessions').chmod(0o755)
    with pytest.raises(ProfileError):
        p.launch_env(binding, {'CODEX_HOME': '/external/home'})


def test_descriptor_swap_cannot_write_into_external_directory(root, tmp_path, monkeypatch):
    outside = tmp_path / 'outside'
    outside.mkdir(mode=0o700)
    p = EngineProfile(root, 'codex')
    original = profiles.replace_json
    def swap_before_publication(fd, name, value):
        home = Path(p.home)
        home.rename(home.with_name('detached'))
        home.symlink_to(outside, target_is_directory=True)
        original(fd, name, value)
    monkeypatch.setattr(profiles, 'replace_json', swap_before_publication)
    with pytest.raises(ProfileError):
        p.prepare()
    assert list(outside.iterdir()) == []
    assert (Path(p.home).with_name('detached') / 'profile.json').exists()


def test_unsupported_backend_fails_without_creating_data(root, monkeypatch):
    monkeypatch.setattr(profiles.os, 'supports_dir_fd', set())
    with pytest.raises(ProfileError, match='backend_unsupported'):
        EngineProfile(root, 'pi').prepare()
    assert list(root.iterdir()) == []


def test_launch_environment_is_observed_by_offline_child(root):
    p = EngineProfile(root, 'pi')
    binding = p.prepare()
    env = p.launch_env(binding, {'HOME': '/synthetic/home'})
    result = subprocess.run([sys.executable, '-I', '-c',
                            'import json,os;print(json.dumps(dict(os.environ)))'],
                            env=env, capture_output=True, text=True, check=True)
    observed = json.loads(result.stdout)
    assert observed['PI_CODING_AGENT_SESSION_DIR'] == str(Path(p.home) / 'sessions')
    assert observed['HOME'] == '/synthetic/home'
    assert 'CODEX_HOME' not in observed


@pytest.mark.parametrize('mode', [0o777, 0o770, 0o775, 0o757])
def test_writable_data_root_rejected_before_profile_creation(root, mode):
    root.chmod(mode)
    with pytest.raises(ProfileError, match='write_permissions'):
        EngineProfile(root, 'codex').prepare()
    assert list(root.iterdir()) == []


def test_readonly_shared_root_permissions_allowed(root):
    root.chmod(0o755)
    p = EngineProfile(root, 'pi')
    p.validate(p.prepare())


def test_null_marker_not_mistaken_for_missing(root):
    p = EngineProfile(root, 'pi')
    p.prepare()
    marker = Path(p.home) / 'profile.json'
    marker.write_text('null')
    with pytest.raises(ProfileError, match='marker_invalid'):
        p.prepare()
    assert marker.read_text() == 'null'
