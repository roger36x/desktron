"""Profile-bound launch/auth contexts and leased native revision publication.

Staged ordinary-adapter integration API, not enabled by engine._adapter yet.
No process/model requests, credential copies, global scans or legacy migration.
A turn caller must hold the lease for the entire native Writer lifetime, including
resident idle time, and publish only after native persistence has settled.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import os

import oscompat  # Windows 上补齐常量；私有存储仍按 require_backend() 拒绝
import uuid

from engine_profiles import (EngineProfile, ProfileError, check_private,
                             private_directory, read_json, replace_json, MISSING)
from native_session_store import NativeSessionStore
from session_ownership import OwnershipRegistry


@dataclass(frozen=True)
class EngineCommandContext:
    engine: str
    scope_id: str
    generation: str
    native_id: str
    session_args: tuple
    # Environment may contain credentials inherited from the caller. Never repr it.
    env: dict = field(repr=False)
    storage_revision: str = ''


class NativeWriterLease:
    """One profile-wide Writer at a time until per-binding leases are integrated."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.active = False
        self.ticket = None
        self.published = False

    def _check(self):
        if not self.active:
            raise ProfileError('native_writer_lease_not_active')
        self._verify()

    def new_context(self, sid, parent):
        self._check()
        if self.ticket is not None:
            raise ProfileError('native_writer_ticket_already_prepared')
        self.runtime.registry.assert_unregistered(sid, self.runtime.profile,
                                                 self.runtime.binding)
        engine = self.runtime.profile.engine
        if engine == 'codex':
            # thread/start allocates its ID inside app-server. Do not adopt a
            # caller-supplied ID as a created thread without the native receipt.
            raise ProfileError('codex_creation_receipt_integration_required')
        native_id = str(uuid.uuid4())
        try:
            self.runtime.store.inspect_private(native_id)
        except ProfileError as exc:
            if str(exc) != 'registered_native_history_missing':
                raise
        else:
            raise ProfileError('created_native_identity_already_exists')
        args = tuple(self.runtime.profile.session_args(self.runtime.binding))
        args += ('--session-id', native_id)
        ctx = self.runtime._context(native_id, args, parent)
        self._check()
        self.runtime.registry.reserve_created(sid, self.runtime.profile,
                                              self.runtime.binding, native_id)
        self._check()
        self.ticket = (sid, 'created', ctx)
        return ctx

    def resume_context(self, sid, parent):
        self._check()
        if self.ticket is not None:
            raise ProfileError('native_writer_ticket_already_prepared')
        snap = self.runtime.store.snapshot(sid)
        engine = self.runtime.profile.engine
        args = tuple(self.runtime.profile.session_args(self.runtime.binding))
        if engine == 'claude':
            args += ('--resume', snap.native_id)
        elif engine == 'pi':
            # Always resume an exact verified private path; a missing record
            # must not silently become a new session with the same UUID.
            args += ('--session', os.path.join(self.runtime.binding.home,
                                              snap.relative_path))
        # Codex resume is a JSON-RPC operation: native_id is passed to the
        # profile-bound app-server by the adapter, never as a guessed CLI flag.
        ctx = self.runtime._context(snap.native_id, args, parent, snap.revision)
        self._check()
        self.ticket = (sid, 'resumed', ctx)
        return ctx

    def publish(self):
        """Commit verified bytes after the caller confirms native Writer quiescence.

        Holding a lease is not itself proof that an independently spawned CLI
        stopped. Production adapters must tie this call to their native result /
        persistence boundary; that integration is intentionally not enabled yet.
        """
        self._check()
        if self.ticket is None or self.published:
            raise ProfileError('native_writer_publication_not_pending')
        sid, kind, ctx = self.ticket
        snap = self.runtime.store.inspect_private(ctx.native_id)
        self._check()
        if kind == 'created':
            row = self.runtime.registry.register(sid, self.runtime.profile,
                      self.runtime.binding, ctx.native_id, snap.revision)
        else:
            row = self.runtime.registry.advance_revision(sid, self.runtime.profile,
                      self.runtime.binding, ctx.storage_revision, snap.revision)
        # The namespace and native bytes must still be the published revision.
        result = self.runtime.store.snapshot(sid)
        if result.revision != row['storage_revision']:
            raise ProfileError('native_revision_publication_changed')
        self._check()
        self.published = True
        return result


class OrdinaryEngineRuntime:
    def __init__(self, data_home, engine):
        self.profile = EngineProfile(data_home, engine)
        self.binding = self.profile.prepare()
        self.registry = OwnershipRegistry(data_home)
        self.store = NativeSessionStore(self.profile, self.binding, self.registry)

    def _context(self, native_id, args, parent, revision=''):
        env = self.profile.launch_env(self.binding, parent)
        return EngineCommandContext(self.binding.engine, self.binding.scope_id,
                self.binding.generation, native_id, tuple(args), env, revision)

    def auth_context(self, parent, *, operation='status'):
        """Use the actual private execution profile; never clone writable tokens.

        Pi provider status is queried through its private RPC client, not a
        made-up 'pi login status' command. Interactive Pi login is not integrated.
        """
        engine = self.profile.engine
        commands = {'claude': {'login': ('auth', 'login'), 'status': ('auth', 'status')},
                    'codex': {'login': ('login',), 'status': ('login', 'status')}}
        if engine not in commands or operation not in commands[engine]:
            raise ProfileError('profile_auth_operation_not_integrated')
        return self._context('', commands[engine][operation], parent)

    def owned(self):
        return self.registry.list_owned(self.profile, self.binding)

    def snapshot(self, sid):
        return self.store.snapshot(sid)

    @contextmanager
    def writer(self):
        """Nonblocking POSIX lease anchored outside the replaceable profile.

        Loss of the lock, trust root or profile is an explicit failure. A crash
        leaves no implicit permission to re-dispatch an unknown native request.
        """
        self.profile.validate(self.binding)
        import fcntl
        name = '.ordinary-writer-' + self.profile.engine + '.lock'
        anchor_name = 'ordinary-writer-' + self.profile.engine + '-anchor.json'
        with private_directory(self.profile.root, ()) as root:
            anchor = read_json(root, anchor_name)
            marker_identity = self.profile.writer_lock_identity(self.binding)
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            if anchor is MISSING:
                if marker_identity is not MISSING:
                    raise ProfileError('initialized_native_writer_lock_anchor_missing')
                # A pre-existing unanchored lock is damage / interrupted setup,
                # not permission to adopt a replacement lock. Exclusive create
                # makes concurrent first use fail closed until the anchor exists.
                try:
                    fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root)
                except FileExistsError as exc:
                    raise ProfileError('native_writer_lock_anchor_missing') from exc
            else:
                if marker_identity is MISSING:
                    raise ProfileError('native_writer_initialization_receipt_missing')
                if (not isinstance(anchor, dict) or set(anchor) != {'version', 'lock_identity'}
                        or type(anchor['version']) is not int or anchor['version'] != 1
                        or not isinstance(anchor['lock_identity'], list)
                        or len(anchor['lock_identity']) != 2
                        or any(type(n) is not int for n in anchor['lock_identity'])):
                    raise ProfileError('native_writer_lock_anchor_invalid')
                fd = os.open(name, flags, dir_fd=root)
            lease = NativeWriterLease(self)
            try:
                before = check_private(fd)
                identity = [before.st_dev, before.st_ino]
                if anchor is MISSING:
                    os.fsync(root)
                    # Publish the independent receipt first. Interruption here
                    # leaves a damaged initialization, never a fresh install.
                    self.profile.record_writer_lock_identity(self.binding, identity)
                    anchor = {'version': 1, 'lock_identity': identity}
                    replace_json(root, anchor_name, anchor)
                elif anchor['lock_identity'] != identity or marker_identity != identity:
                    raise ProfileError('native_writer_lock_changed')
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ProfileError('native_writer_busy') from exc

                def verify():
                    check_private(fd)
                    current = os.stat(name, dir_fd=root, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                        raise ProfileError('native_writer_lock_changed')
                    if read_json(root, anchor_name) != anchor:
                        raise ProfileError('native_writer_lock_anchor_changed')
                    with private_directory(self.profile.root, ()) as active:
                        a, b = os.fstat(root), os.fstat(active)
                        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                            raise ProfileError('native_writer_data_root_changed')
                    if self.profile.writer_lock_identity(self.binding) != identity:
                        raise ProfileError('native_writer_initialization_receipt_changed')

                verify()
                lease._verify = verify
                lease.active = True
                try:
                    yield lease
                finally:
                    lease.active = False
                    verify()
            finally:
                os.close(fd)
