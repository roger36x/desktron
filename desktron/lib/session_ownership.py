"""Atomic ordinary-session ownership metadata, separate from native history.

No native scans, cwd inference, migration, deletion or credential reads happen
here. A native identity is registered only by a future verified adapter/import
caller. Unregistered legacy sessions must not be resumed through a private
profile by guessing UUIDs; they require explicit migration first.
"""
from contextlib import contextmanager
import copy
import os
import re

from engine_profiles import (ENGINES, MISSING, EngineProfile, ProfileBinding, ProfileError,
                             metadata_lock, private_directory, read_json,
                             replace_json, valid_id)

SHA256 = re.compile(r'[0-9a-f]{64}\Z')
RESERVED_REVISION = '0' * 64  # no persisted native bytes have been confirmed
OWNED_FIELDS = {'state', 'engine', 'scope_id', 'generation', 'native_id',
                'storage_revision', 'source', 'receipt_id'}


class OwnershipError(ProfileError):
    pass


def revision(value):
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise OwnershipError('invalid_storage_revision')
    return value


class OwnershipRegistry:
    def __init__(self, data_home):
        # Reuse the same trust-root normalization without importing deskcfg.
        self.root = EngineProfile(data_home, 'claude').root

    @contextmanager
    def _directory(self):
        # Keep the publication lock and initialization anchor OUTSIDE the
        # replaceable namespace. A missing initialized index/namespace is damage,
        # not first use. No automatic adoption or repair of interrupted setup.
        with private_directory(self.root, ()) as root:
            with metadata_lock(root, '.session-ownership-lock'):
                anchor = read_json(root, 'session-ownership-anchor.json')
                with private_directory(self.root, ('session-ownership',),
                                       create=anchor is MISSING) as directory:
                    info = os.fstat(directory)
                    identity = [info.st_dev, info.st_ino]
                    if anchor is MISSING:
                        if os.listdir(directory):
                            raise OwnershipError('unanchored_ownership_requires_explicit_adoption')
                        replace_json(directory, 'index.json', {'version': 1, 'sessions': {}})
                        anchor = {'version': 1, 'directory_identity': identity}
                        replace_json(root, 'session-ownership-anchor.json', anchor)
                    if (not isinstance(anchor, dict)
                            or set(anchor) != {'version', 'directory_identity'}
                            or type(anchor['version']) is not int or anchor['version'] != 1
                            or not isinstance(anchor['directory_identity'], list)
                            or len(anchor['directory_identity']) != 2
                            or any(type(n) is not int for n in anchor['directory_identity'])
                            or anchor['directory_identity'] != identity):
                        raise OwnershipError('ownership_directory_identity_invalid')
                    self._active_directory(root, directory, anchor)
                    yield directory
                    # Descriptor writes cannot follow links, but might land in
                    # a detached old directory. Never report that as success.
                    self._active_directory(root, directory, anchor)

    def _active_directory(self, root, directory, anchor):
        with private_directory(self.root, ()) as active_root:
            before, after = os.fstat(root), os.fstat(active_root)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise OwnershipError('ownership_data_root_changed')
            if read_json(active_root, 'session-ownership-anchor.json') != anchor:
                raise OwnershipError('ownership_anchor_changed')
        with private_directory(self.root, ('session-ownership',)) as active:
            before, after = os.fstat(directory), os.fstat(active)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise OwnershipError('ownership_directory_changed')

    def _load(self, directory):
        value = read_json(directory, 'index.json')
        if value is MISSING:
            raise OwnershipError('initialized_ownership_index_missing')
        if (not isinstance(value, dict) or set(value) != {'version', 'sessions'}
                or type(value['version']) is not int or value['version'] != 1
                or not isinstance(value['sessions'], dict)):
            raise OwnershipError('ownership_index_invalid')
        identities = set()
        for sid, row in value['sessions'].items():
            valid_id(sid)
            if not isinstance(row, dict):
                raise OwnershipError('ownership_row_invalid')
            if row == {'state': 'deleted'}:
                continue
            if (set(row) != OWNED_FIELDS or row['state'] not in ('owned', 'reserved')
                    or row['engine'] not in ENGINES
                    or not isinstance(row['generation'], str)
                    or not re.fullmatch(r'[0-9a-f]{32}', row['generation'])
                    or row['source'] not in ('created', 'imported', 'migrated')):
                raise OwnershipError('ownership_row_invalid')
            revision(row['scope_id'])
            revision(row['storage_revision'])
            if row['state'] == 'reserved' and (row['source'] != 'created'
                    or row['storage_revision'] != RESERVED_REVISION):
                raise OwnershipError('ownership_reservation_invalid')
            valid_id(row['native_id'])
            if row['source'] == 'created':
                if row['receipt_id'] != '':
                    raise OwnershipError('unexpected_ownership_receipt')
            else:
                valid_id(row['receipt_id'])
            identity = (row['engine'], row['scope_id'], row['native_id'])
            if identity in identities:
                raise OwnershipError('duplicate_native_ownership')
            identities.add(identity)
        return value

    def _check_profile(self, profile, binding):
        if not isinstance(profile, EngineProfile) or not isinstance(binding, ProfileBinding):
            raise OwnershipError('profile_binding_required')
        if profile.root != self.root:
            raise OwnershipError('ownership_data_root_mismatch')
        profile.validate(binding)

    def assert_unregistered(self, sid, profile, binding):
        """Reject legacy ownership, duplicates and tombstones before a new Writer."""
        valid_id(sid)
        self._check_profile(profile, binding)
        with self._directory() as directory:
            row = self._load(directory)['sessions'].get(sid)
            if row == {'state': 'deleted'}:
                raise OwnershipError('conversation_deleted')
            if row is not None:
                raise OwnershipError('ownership_conflict')
            self._check_profile(profile, binding)

    def reserve_created(self, sid, profile, binding, native_id):
        """Durable first-launch fence; never implies a model request may be retried.

        An unresolved reservation requires explicit recovery, even when the
        caller crashed before dispatch. Do not implicitly convert it to a new SID.
        """
        return self._register(sid, profile, binding, native_id, RESERVED_REVISION,
                              source='created', receipt_id='', state='reserved')

    def register(self, sid, profile, binding, native_id, storage_revision,
                 *, source='created', receipt_id=''):
        return self._register(sid, profile, binding, native_id, storage_revision,
                              source=source, receipt_id=receipt_id, state='owned')

    def _register(self, sid, profile, binding, native_id, storage_revision,
                  *, source, receipt_id, state):
        valid_id(sid)
        valid_id(native_id)
        revision(storage_revision)
        self._check_profile(profile, binding)
        if source not in ('created', 'imported', 'migrated'):
            raise OwnershipError('invalid_ownership_source')
        if source != 'created':
            valid_id(receipt_id)
        elif receipt_id != '':
            raise OwnershipError('unexpected_ownership_receipt')
        row = {'state': state, 'engine': binding.engine, 'scope_id': binding.scope_id,
               'generation': binding.generation, 'native_id': native_id,
               'storage_revision': storage_revision, 'source': source, 'receipt_id': receipt_id}
        with self._directory() as directory:
            with metadata_lock(directory):
                self._check_profile(profile, binding)
                value = self._load(directory)
                old = value['sessions'].get(sid)
                if old == {'state': 'deleted'}:
                    raise OwnershipError('conversation_deleted')
                completing = (state == 'owned' and old is not None
                              and old.get('state') == 'reserved'
                              and {**old, 'state': 'owned',
                                   'storage_revision': storage_revision} == row)
                if old is not None and (state == 'reserved' or (old != row and not completing)):
                    raise OwnershipError('ownership_conflict')
                for other_sid, other in value['sessions'].items():
                    if (other_sid != sid and other.get('state') in ('owned', 'reserved')
                            and (other['engine'], other['scope_id'], other['native_id'])
                            == (binding.engine, binding.scope_id, native_id)):
                        raise OwnershipError('native_session_already_owned')
                if old is None or completing:
                    value['sessions'][sid] = row
                    replace_json(directory, 'index.json', value)
        return copy.deepcopy(row)

    def resolve(self, sid, profile, binding):
        valid_id(sid)
        self._check_profile(profile, binding)
        with self._directory() as directory:
            with metadata_lock(directory):
                row = self._load(directory)['sessions'].get(sid)
                if row is None:
                    raise OwnershipError('native_binding_not_registered')
                if row['state'] == 'deleted':
                    raise OwnershipError('conversation_deleted')
                if row['state'] == 'reserved':
                    raise OwnershipError('native_creation_unconfirmed')
                if (row['engine'], row['scope_id'], row['generation']) != (
                        binding.engine, binding.scope_id, binding.generation):
                    raise OwnershipError('ownership_profile_mismatch')
                self._check_profile(profile, binding)
                return copy.deepcopy(row)

    def list_owned(self, profile, binding):
        """Explicit registrations only; never discover by cwd or native glob."""
        self._check_profile(profile, binding)
        with self._directory() as directory:
            with metadata_lock(directory):
                value = self._load(directory)
                self._check_profile(profile, binding)
                return {sid: copy.deepcopy(row) for sid, row in value['sessions'].items()
                        if row['state'] == 'owned' and (row['engine'], row['scope_id'],
                           row['generation']) == (binding.engine, binding.scope_id,
                                                 binding.generation)}

    def advance_revision(self, sid, profile, binding, expected, new):
        valid_id(sid)
        revision(expected)
        revision(new)
        self._check_profile(profile, binding)
        with self._directory() as directory:
            with metadata_lock(directory):
                value = self._load(directory)
                row = value['sessions'].get(sid)
                if row is None or row['state'] != 'owned':
                    raise OwnershipError('native_binding_not_registered')
                if (row['engine'], row['scope_id'], row['generation']) != (
                        binding.engine, binding.scope_id, binding.generation):
                    raise OwnershipError('ownership_profile_mismatch')
                self._check_profile(profile, binding)
                if row['storage_revision'] != expected:
                    raise OwnershipError('storage_revision_conflict')
                row['storage_revision'] = new
                replace_json(directory, 'index.json', value)
                return copy.deepcopy(row)

    def tombstone(self, sid, profile, binding, expected):
        """Metadata fence only; callers must stop Writers and clean content.

        Do not expose this as a user deletion endpoint until content cleanup and
        recovery fencing are wired up. The durable tombstone contains no native
        identity, origin, revision or text and cannot be registered again.
        """
        valid_id(sid)
        revision(expected)
        self._check_profile(profile, binding)
        with self._directory() as directory:
            with metadata_lock(directory):
                value = self._load(directory)
                row = value['sessions'].get(sid)
                if row == {'state': 'deleted'}:
                    return
                if row is None or row['state'] != 'owned':
                    raise OwnershipError('native_binding_not_registered')
                if (row['engine'], row['scope_id'], row['generation']) != (
                        binding.engine, binding.scope_id, binding.generation):
                    raise OwnershipError('ownership_profile_mismatch')
                if row['storage_revision'] != expected:
                    raise OwnershipError('storage_revision_conflict')
                self._check_profile(profile, binding)
                value['sessions'][sid] = {'state': 'deleted'}
                replace_json(directory, 'index.json', value)
