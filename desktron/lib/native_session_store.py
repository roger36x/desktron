"""Bounded, read-only snapshots of registered ordinary-engine native history.

Only the supplied private profile is scanned. This is not an external import
scanner or a resume implementation. It never runs an engine, edits history,
reads credentials, chooses the newest duplicate, or falls back to user HOME.
"""
from dataclasses import dataclass
import hashlib
import json
import os

import oscompat  # Windows 上补齐常量；私有存储仍按 require_backend() 拒绝
import stat

from engine_profiles import (EngineProfile, ProfileError, check_private,
                             private_directory, valid_id)
from session_ownership import OwnershipRegistry


@dataclass(frozen=True)
class NativeSnapshot:
    engine: str
    scope_id: str
    native_id: str
    relative_path: str
    revision: str
    raw: bytes


def _fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class NativeSessionStore:
    def __init__(self, profile, binding, registry, *, max_bytes=64 * 1024 * 1024,
                 max_entries=10000, max_depth=8):
        if not isinstance(profile, EngineProfile) or not isinstance(registry, OwnershipRegistry):
            raise ProfileError('native_store_requires_profile_and_ownership')
        for value in (max_bytes, max_entries, max_depth):
            if type(value) is not int or value <= 0:
                raise ProfileError('invalid_native_read_budget')
        self.profile, self.binding, self.registry = profile, binding, registry
        self.max_bytes, self.max_entries, self.max_depth = max_bytes, max_entries, max_depth

    def _matches(self, name, native_id):
        if self.binding.engine == 'claude':
            return name == native_id + '.jsonl'
        if self.binding.engine == 'codex':
            return name.startswith('rollout-') and name.endswith('-' + native_id + '.jsonl')
        return name.endswith('_' + native_id + '.jsonl')

    def _read(self, fd):
        before = check_private(fd)
        if before.st_size > self.max_bytes:
            raise ProfileError('native_history_read_budget_exceeded')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(self.max_bytes + 1)
        after = check_private(fd)
        if (_fingerprint(before) != _fingerprint(after) or len(raw) != before.st_size
                or len(raw) > self.max_bytes):
            raise ProfileError('native_history_changed_during_read')
        if not raw or not raw.endswith(b'\n'):
            raise ProfileError('native_history_incomplete')
        return raw, before

    def _identity(self, raw, native_id):
        # This is an identity check, not a complete native-format validator.
        # Recovery/continuation must separately validate the native format.
        if self.binding.engine == 'claude':
            # Some native control records precede the first session-bearing row.
            found = False
            for line in raw.split(b'\n', 128)[:128]:
                if len(line) > 1_048_576:
                    raise ProfileError('native_identity_header_too_large')
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError) as exc:
                    raise ProfileError('native_identity_header_invalid') from exc
                if not isinstance(value, dict):
                    raise ProfileError('native_identity_header_invalid')
                sid = value.get('sessionId')
                if sid is not None:
                    if sid != native_id:
                        raise ProfileError('native_identity_mismatch')
                    found = True
                # Stop at first session-bearing row. Full native validation is
                # deliberately not claimed for this storage primitive.
                if found:
                    return
            raise ProfileError('native_identity_missing')
        line = raw.split(b'\n', 1)[0]
        if len(line) > 1_048_576:
            raise ProfileError('native_identity_header_too_large')
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise ProfileError('native_identity_header_invalid') from exc
        if not isinstance(value, dict):
            raise ProfileError('native_identity_header_invalid')
        if self.binding.engine == 'codex':
            header = value.get('payload')
            match = (value.get('type') == 'session_meta' and isinstance(header, dict)
                     and header.get('id') == native_id)
        else:
            match = value.get('type') == 'session' and value.get('id') == native_id
        if not match:
            raise ProfileError('native_identity_mismatch')

    def snapshot(self, sid):
        row = self.registry.resolve(sid, self.profile, self.binding)
        result = self.inspect_private(row['native_id'], expected_revision=row['storage_revision'])
        if self.registry.resolve(sid, self.profile, self.binding) != row:
            raise ProfileError('ownership_changed_during_read')
        return result

    def inspect_private(self, native_id, *, expected_revision=None):
        """Verify one private native file before initial binding/revision publication.

        This is NOT an ownership resolver. Callers must hold a verified launch
        ticket and the Writer lease; reading ordinary history uses snapshot().
        """
        valid_id(native_id)
        self.profile.validate(self.binding)
        candidates, visited = [], 0

        def walk(directory, relative, depth):
            nonlocal visited
            if depth > self.max_depth:
                raise ProfileError('native_history_depth_budget_exceeded')
            for name in os.listdir(directory):
                visited += 1
                if visited > self.max_entries:
                    raise ProfileError('native_history_entry_budget_exceeded')
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise ProfileError('native_history_link_not_allowed')
                path = relative + (name,)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=directory)
                    try:
                        check_private(child, directory=True)
                        walk(child, path, depth + 1)
                    finally:
                        os.close(child)
                elif self._matches(name, native_id):
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
                    try:
                        raw, fingerprint = self._read(fd)
                        self._identity(raw, native_id)
                        digest = hashlib.sha256(raw).hexdigest()
                        if expected_revision is not None and digest != expected_revision:
                            raise ProfileError('native_storage_revision_mismatch')
                        # Re-check the named entry, not just the open file FD.
                        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                        if _fingerprint(current) != _fingerprint(fingerprint):
                            raise ProfileError('native_history_entry_changed')
                        candidates.append((path, raw, digest, fingerprint))
                        if len(candidates) > 1:
                            raise ProfileError('native_history_identity_ambiguous')
                    finally:
                        os.close(fd)

        roots = {'claude': ('projects',), 'codex': ('sessions', 'archived_sessions'),
                 'pi': ('sessions',)}[self.binding.engine]
        for name in roots:
            with private_directory(self.profile.root, (*self.profile.parts, name)) as directory:
                walk(directory, (name,), 0)
        if len(candidates) != 1:
            raise ProfileError('registered_native_history_missing')
        path, raw, digest, fingerprint = candidates[0]
        # A namespace switch during traversal invalidates the result.
        self.profile.validate(self.binding)
        with private_directory(self.profile.root, self.profile.parts) as directory:
            handles = []
            try:
                for component in path[:-1]:
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=directory)
                    handles.append(child)
                    check_private(child, directory=True)
                    directory = child
                current = os.stat(path[-1], dir_fd=directory, follow_symlinks=False)
                if _fingerprint(current) != _fingerprint(fingerprint):
                    raise ProfileError('native_history_entry_changed')
            finally:
                for fd in reversed(handles):
                    os.close(fd)
        return NativeSnapshot(self.binding.engine, self.binding.scope_id, native_id,
                              '/'.join(path), digest, raw)
