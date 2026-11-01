"""Owned execution scratch. Never infer liveness from directory age."""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading

from durable_json import locked
from .recovery import process_identity, process_state
from .store import Conflict, InvalidState, valid_id

KIND = 'shepherd-execution-v1'


def _private(path, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.getuid() or oscompat.loose_mode(info.st_mode):
        raise Conflict('Unowned Shepherd scratch path')
    return info


def _root(root):
    root = Path(root) / 'execution'
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    _private(root, True)
    return root


def _scratch_prefix(binding_id):
    """Encode a Binding in the private directory name without prefix collisions."""
    binding_id = valid_id(binding_id, 'binding_id')
    return f'turn-{len(binding_id)}-{binding_id}-'


def _scratch_binding_from_name(name):
    """Return a Binding encoded by new scratch names; legacy names return None."""
    if not isinstance(name, str) or not name.startswith('turn-'):
        return None
    length_text, separator, remainder = name[5:].partition('-')
    if not separator or not length_text.isdigit():
        return None
    length = int(length_text)
    if length < 1 or len(remainder) <= length or remainder[length] != '-':
        return None
    try:
        return valid_id(remainder[:length], 'binding_id')
    except InvalidState:
        return None


def _binding_hint(directory):
    """Read the Binding identity from private scratch metadata."""
    _private(directory, True)
    path = directory / 'owner.json'
    if _private(path).st_size > 65536:
        raise Conflict('Shepherd scratch ownership metadata too large')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        data = json.load(stream)
    if not isinstance(data,dict):
        raise Conflict('Shepherd scratch ownership metadata invalid')
    try:
        binding_id = valid_id(data.get('binding_id'), 'binding_id')
    except InvalidState:
        raise Conflict('Shepherd scratch ownership metadata invalid') from None
    return binding_id, data


def _read(directory):
    info = _private(directory, True)
    _binding_id, data = _binding_hint(directory)
    processes = data.get('processes')
    if (data.get('kind') != KIND or data.get('name') != directory.name
            or data.get('inode') != info.st_ino or data.get('device') != info.st_dev
            or not isinstance(processes, list)
            or any(not isinstance(process, dict) for process in processes)
            or not isinstance(data.get('spawning'), bool)
            or not isinstance(data.get('released'), bool)):
        raise Conflict('Shepherd scratch ownership mismatch')
    return data


def _children_dead(data):
    return not data['spawning'] and all(
        process.get('released') is True or process_state(process) == 'dead'
        for process in data['processes'])


def sweep(root):
    """Only registered, unlocked scratch whose exact host and children are dead."""
    directory = _root(root)
    result = {'removed': 0, 'retained': 0}
    with locked(str(directory / '.gc')):
        for path in directory.glob('turn-*'):
            fd = None
            try:
                data = _read(path)
                lock = path / 'lease.lock'; _private(lock)
                fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if (data.get('pending_context') or
                        (not data.get('released',False) and process_state(data.get('host')) != 'dead') or not _children_dead(data)):
                    result['retained'] += 1
                    continue
                # Windows cannot unlink the open lease file. The outer .gc
                # lock still excludes competing sweepers until deletion ends.
                if oscompat.IS_WINDOWS:
                    os.close(fd)
                    fd = None
                # rmtree removes symlinks inside the owned tree without following them.
                shutil.rmtree(path)
                result['removed'] += 1
            except (OSError, ValueError, TypeError, Conflict):
                result['retained'] += 1
            finally:
                if fd is not None: os.close(fd)
    return result


class ExecutionScratch:
    def __init__(self, root, binding, turn_id):
        self.root = _root(root)
        self._mutex = threading.Lock()
        # Exact in-process object ownership lets a committed resident detach
        # without re-sampling a supervisor that may be between exit and reap.
        # Restart recovery still relies only on the durable identity below.
        self._registered_processes = {}
        with locked(str(self.root / '.gc')):
            self.path = Path(tempfile.mkdtemp(
                prefix=_scratch_prefix(binding['binding_id']), dir=self.root))
            info = _private(self.path, True)
            self.fd = os.open(self.path / 'lease.lock', os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            self.data = {'kind':KIND, 'name':self.path.name, 'inode':info.st_ino,
                'device':info.st_dev, 'host':process_identity(os.getpid()),
                'binding_id':binding['binding_id'], 'turn_id':turn_id,
                'processes':[], 'spawning':False, 'released':False}
            self._save()

    def _save(self):
        path = self.path / 'owner.next'
        fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(self.data, stream); stream.flush(); os.fsync(stream.fileno())
        os.replace(path, self.path / 'owner.json')
        oscompat.fsync_directory(self.path)

    def spawning(self):
        # A crash between fork and registration must retain scratch: the child
        # may still exist and its identity was not durably captured.
        with self._mutex:
            self.data['spawning'] = True
            self._save()

    def register(self, process):
        identity = process_identity(process)
        if not identity.get('start_token'):
            raise Conflict('Native process creation identity unavailable')
        key=id(process)
        with self._mutex:
            previous=self._registered_processes.get(key)
            if previous is not None:
                # A Codex capacity/routing retry continues the same turn on the same
                # resident app-server and reports that connection again. The exact
                # object with the same creation identity is already owned: accept it
                # once more without a duplicate row.
                # A different object reusing id(), or a changed identity, still fails.
                same=(previous[0] is process
                      and previous[1].get('pid') == identity.get('pid')
                      and previous[1].get('start_token') == identity.get('start_token'))
                if not same:
                    raise Conflict('Native process already registered for this turn')
                if self.data.get('spawning'):
                    self.data['spawning'] = False
                    self._save()
                return
            self.data['processes'].append({**identity, 'released':False})
            self.data['spawning'] = False
            self._save()
            self._registered_processes[key]=(process,identity)

    def release_process(self, process):
        """Mark the exact registered resident host as detached from this turn."""
        with self._mutex:
            registered=self._registered_processes.get(id(process))
            if registered is None or registered[0] is not process:
                raise Conflict('Shepherd 常驻进程释放身份不匹配')
            identity=registered[1]
            matches = [row for row in self.data['processes']
                       if row.get('pid') == identity.get('pid')
                       and row.get('start_token') == identity.get('start_token')]
            if len(matches) != 1 or matches[0].get('released') is True:
                raise Conflict('Shepherd 常驻进程释放身份不匹配')
            matches[0]['released'] = True
            self._save()
            self._registered_processes.pop(id(process),None)

    def __enter__(self): return self

    def retain_context(self, pending=True):
        """Do not collect a native context until its archive commit succeeds."""
        with self._mutex:
            self.data['pending_context'] = bool(pending)
            self._save()

    def __exit__(self, *_args):
        with locked(str(self.root / '.gc')):
            try:
                self.data['released']=True
                self._save()
                if not self.data.get('pending_context') and _children_dead(self.data):
                    # Identity checks and the outer .gc lock remain held while
                    # releasing Windows' delete-blocking handle and collecting.
                    if oscompat.IS_WINDOWS:
                        os.close(self.fd)
                        self.fd = None
                    shutil.rmtree(self.path)
            finally:
                if self.fd is not None:
                    os.close(self.fd)
                    self.fd = None
