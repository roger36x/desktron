"""Private ordinary-engine profiles; no global discovery or credential copying.

This is a staged storage primitive, not yet the ordinary adapters' default.
Callers must prepare/validate a profile before constructing a native process and
must use OwnershipRegistry to authorize an existing native binding. POSIX-only
until descriptor/no-follow and Windows ACL equivalents have been implemented.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os

import oscompat  # Windows 上补齐常量；私有存储仍按 require_backend() 拒绝
from pathlib import Path
import re
import stat
import uuid

ENGINES = ('claude', 'codex', 'pi')
ID = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?\Z')
MAX_METADATA = 1_048_576
MISSING = object()


class ProfileError(ValueError):
    """Safe storage could not be proven; never fall back to the user's HOME."""


def valid_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ProfileError('invalid_storage_identity')
    return value


def require_backend():
    if (os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW')
            or os.open not in os.supports_dir_fd or os.mkdir not in os.supports_dir_fd):
        raise ProfileError('private_storage_backend_unsupported')


def check_private(fd, *, directory=False):
    info = os.fstat(fd)
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(info.st_mode) or info.st_uid != os.getuid()
            or oscompat.loose_mode(info.st_mode) or (not directory and info.st_nlink != 1)):
        raise ProfileError('private_storage_identity_or_permissions_invalid')
    return info


@contextmanager
def private_directory(root, parts, *, create=False):
    """Walk relative to directory handles; never repair or follow unsafe paths.

    DATA_HOME is an existing caller-supplied trust root. Its final component must
    not be a symlink. Parents (e.g. macOS /tmp) are normalized by the caller.
    Every managed directory below that root must be private and owned.
    """
    require_backend()
    handles = []
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        handles.append(fd)
        root_info = os.fstat(fd)
        if root_info.st_uid != os.getuid() or root_info.st_mode & 0o022:
            raise ProfileError('data_root_owner_or_write_permissions_invalid')
        for name in parts:
            valid_id(name)
            if create:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            handles.append(fd)
            check_private(fd, directory=True)
        yield fd
    except OSError as exc:
        raise ProfileError('private_storage_unavailable') from exc
    finally:
        for fd in reversed(handles):
            os.close(fd)


def _filename(name):
    if not isinstance(name, str) or not re.fullmatch(r'\.?[A-Za-z0-9][A-Za-z0-9._-]*', name):
        raise ProfileError('invalid_metadata_filename')


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ProfileError('private_metadata_duplicate_field')
        value[key] = item
    return value


def read_json(directory, name, *, missing=MISSING):
    _filename(name)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return missing
    except OSError as exc:
        raise ProfileError('private_metadata_unavailable') from exc
    try:
        info = check_private(fd)
        if info.st_size > MAX_METADATA:
            raise ProfileError('private_metadata_too_large')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_METADATA + 1)
        if len(raw) > MAX_METADATA:
            raise ProfileError('private_metadata_too_large')
        return json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProfileError('private_metadata_corrupt') from exc
    finally:
        os.close(fd)


def replace_json(directory, name, value):
    """Atomic, fsynced metadata replacement; caller holds the publication lock."""
    _filename(name)
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    if len(encoded) > MAX_METADATA:
        raise ProfileError('private_metadata_too_large')
    temporary = '.metadata-' + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


@contextmanager
def metadata_lock(directory, name='.metadata-lock'):
    require_backend()
    _filename(name)
    import fcntl
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                 0o600, dir_fd=directory)
    try:
        before = check_private(fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise ProfileError('private_metadata_lock_changed')
        yield
        after = check_private(fd)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
            raise ProfileError('private_metadata_lock_changed')
    finally:
        os.close(fd)


@dataclass(frozen=True)
class ProfileBinding:
    engine: str
    scope_id: str
    generation: str
    home: str
    directory_identity: tuple


class EngineProfile:
    def __init__(self, data_home, engine):
        if engine not in ENGINES:
            raise ProfileError('unsupported_engine')
        # Do not resolve the final component: a configured symlink must fail.
        path = Path(os.path.abspath(os.fspath(data_home)))
        self.root = str(path.parent.resolve() / path.name)
        self.engine = engine
        self.parts = ('engine-profiles', engine, 'ordinary')
        self.home = str(Path(self.root).joinpath(*self.parts))

    @property
    def native_dirs(self):
        return {'claude': ('projects',), 'codex': ('sessions', 'archived_sessions',
                'thread-writer-locks'), 'pi': ('sessions',)}[self.engine]

    def _marker(self, directory):
        value = read_json(directory, 'profile.json')
        base_fields = {'version', 'engine', 'generation'}
        if (not isinstance(value, dict) or set(value) not in (
                base_fields, base_fields | {'writer_lock_identity'})
                or type(value['version']) is not int or value['version'] != 1
                or value['engine'] != self.engine
                or not isinstance(value['generation'], str)
                or not re.fullmatch(r'[0-9a-f]{32}', value['generation'])):
            raise ProfileError('private_profile_marker_invalid')
        if 'writer_lock_identity' in value:
            identity = value['writer_lock_identity']
            if (not isinstance(identity, list) or len(identity) != 2
                    or any(type(n) is not int or n < 0 for n in identity)):
                raise ProfileError('private_profile_writer_identity_invalid')
        return value

    def _binding(self, directory):
        value = self._marker(directory)
        info = check_private(directory, directory=True)
        scope = hashlib.sha256(json.dumps([self.root, self.engine, value['generation']],
                                         separators=(',', ':')).encode()).hexdigest()
        return ProfileBinding(self.engine, scope, value['generation'], self.home,
                              (info.st_dev, info.st_ino))

    def prepare(self):
        with private_directory(self.root, self.parts, create=True) as directory:
            with metadata_lock(directory):
                if read_json(directory, 'profile.json') is MISSING:
                    if set(os.listdir(directory)) != {'.metadata-lock'}:
                        raise ProfileError('unmarked_profile_requires_explicit_adoption')
                    replace_json(directory, 'profile.json', {'version': 1,
                                 'engine': self.engine, 'generation': uuid.uuid4().hex})
                binding = self._binding(directory)
                for name in self.native_dirs:
                    with private_directory(self.root, (*self.parts, name), create=True):
                        pass
        self.validate(binding)
        return binding

    def validate(self, binding):
        if not isinstance(binding, ProfileBinding):
            raise ProfileError('profile_binding_required')
        with private_directory(self.root, self.parts) as directory:
            if self._binding(directory) != binding:
                raise ProfileError('private_profile_changed')
        for name in self.native_dirs:
            with private_directory(self.root, (*self.parts, name)):
                pass
        # CLI reads/writes these files itself. Reject pre-existing linked or
        # publicly readable config/auth/index inputs, without reading secrets.
        names = {'claude': ('settings.json', '.credentials.json'),
                 'codex': ('auth.json', 'config.toml', 'state_5.sqlite', 'session_index.jsonl'),
                 'pi': ('auth.json', 'settings.json', 'models.json')}[self.engine]
        with private_directory(self.root, self.parts) as directory:
            for name in names:
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
                except FileNotFoundError:
                    continue
                try:
                    check_private(fd)
                finally:
                    os.close(fd)
        # Checks above reopen subdirectories by path. Prove that they still
        # belong to the same profile before returning a launch/cache identity.
        with private_directory(self.root, self.parts) as directory:
            if self._binding(directory) != binding:
                raise ProfileError('private_profile_changed')

    def writer_lock_identity(self, binding):
        """Independent initialization receipt; loss of root lock/anchor is damage."""
        self.validate(binding)
        with private_directory(self.root, self.parts) as directory:
            if self._binding(directory) != binding:
                raise ProfileError('private_profile_changed')
            identity = self._marker(directory).get('writer_lock_identity', MISSING)
        self.validate(binding)
        return identity

    def record_writer_lock_identity(self, binding, identity):
        if (not isinstance(identity, list) or len(identity) != 2
                or any(type(n) is not int or n < 0 for n in identity)):
            raise ProfileError('private_profile_writer_identity_invalid')
        self.validate(binding)
        with private_directory(self.root, self.parts) as directory:
            with metadata_lock(directory):
                if self._binding(directory) != binding:
                    raise ProfileError('private_profile_changed')
                marker = self._marker(directory)
                old = marker.get('writer_lock_identity', MISSING)
                if old is not MISSING and old != identity:
                    raise ProfileError('native_writer_lock_changed')
                if old is MISSING:
                    replace_json(directory, 'profile.json',
                                 {**marker, 'writer_lock_identity': list(identity)})
        self.validate(binding)

    def launch_env(self, binding, parent):
        self.validate(binding)
        env = dict(parent)
        for key in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'PI_CODING_AGENT_DIR',
                    'PI_CODING_AGENT_SESSION_DIR', 'PI_SESSION_ID', 'PI_SESSION_FILE'):
            env.pop(key, None)
        env['DESKTRON_ENGINE_SCOPE'] = binding.scope_id
        env['DESKTRON_ENGINE_GENERATION'] = binding.generation
        if self.engine == 'claude':
            env['CLAUDE_CONFIG_DIR'] = binding.home
        elif self.engine == 'codex':
            env['CODEX_HOME'] = binding.home
        else:
            env['PI_CODING_AGENT_DIR'] = binding.home
            env['PI_CODING_AGENT_SESSION_DIR'] = os.path.join(binding.home, 'sessions')
        return env

    def session_args(self, binding):
        self.validate(binding)
        # Pi project settings are read before project trust; the explicit CLI
        # option has precedence over those settings and inherited env overrides.
        return ['--session-dir', os.path.join(binding.home, 'sessions')] if self.engine == 'pi' else []

    def cache_key(self, binding, binary, auth_context):
        self.validate(binding)
        # An opaque credential-slot identity, NEVER a key/token or auth file.
        valid_id(auth_context)
        if not isinstance(binary, str) or not os.path.isabs(binary):
            raise ProfileError('absolute_engine_binary_required')
        return (binding.engine, binding.scope_id, binding.generation, binary, auth_context)
