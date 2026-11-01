"""Content-erasing Shepherd deletion with a body-free anti-resurrection sentinel."""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from durable_json import locked
from owned_process import writer_lock_path
from .store import Conflict, InvalidState, valid_id

_VERSION = 1


def _root(root):
    path = Path(os.path.abspath(os.fspath(root)))
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    return path


def marker_path(root, conversation_id):
    sid = valid_id(conversation_id, 'conversation_id')
    base = _root(root); directory = base / 'deleted'
    if os.path.lexists(directory) and directory.is_symlink():
        raise Conflict('Shepherd 删除标记目录不能是符号链接')
    return directory / (sid + '.json')


def is_deleted(root, conversation_id):
    path = marker_path(root, conversation_id)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or oscompat.loose_mode(info.st_mode):
            raise InvalidState('Shepherd 删除标记权限无效')
        with os.fdopen(fd, encoding='utf-8', closefd=False) as stream:
            value = json.load(stream)
    finally:
        os.close(fd)
    if (not isinstance(value, dict) or value.get('version') != _VERSION
            or value.get('conversation_id') != conversation_id
            or value.get('state') != 'deleted'):
        raise InvalidState('Shepherd 删除标记无效')
    return True


def deleted_ids(root):
    directory = _root(root) / 'deleted'
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    out = []
    for name in names:
        if not name.endswith('.json'):
            continue
        sid = name[:-5]
        try:
            if is_deleted(root, sid):
                out.append(sid)
        except (InvalidState, OSError, ValueError):
            continue
    return sorted(out)


def deletion_guard(root, conversation_id):
    return locked(str(marker_path(root, conversation_id)) + '.transaction')


def mark_deleted(root, conversation_id):
    """Persist the only long-lived deletion record; it contains no conversation body."""
    sid = valid_id(conversation_id, 'conversation_id')
    path = marker_path(root, sid)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with deletion_guard(root, sid):
        if is_deleted(root, sid):
            return str(path)
        fd, temporary = tempfile.mkstemp(prefix='.deleted-', dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'version':_VERSION, 'conversation_id':sid, 'state':'deleted'},
                          stream, sort_keys=True, separators=(',', ':'))
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
            oscompat.fsync_directory(path.parent)
        finally:
            try: os.unlink(temporary)
            except FileNotFoundError: pass
    return str(path)


def _safe_parent(root, path):
    root = Path(root); path = Path(path)
    expected = os.path.realpath(root)
    parent = os.path.realpath(path.parent)
    if os.path.commonpath((expected, parent)) != expected:
        raise Conflict('Shepherd 物理删除路径越过私有目录')
    current = root
    try: parts = path.parent.relative_to(root).parts
    except ValueError: raise Conflict('Shepherd 物理删除路径越过私有目录') from None
    for part in parts:
        current = current / part
        if os.path.lexists(current) and current.is_symlink():
            raise Conflict('Shepherd 物理删除路径不能经过符号链接')


def _unlink(root, path):
    path = Path(path); _safe_parent(root, path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _remove_exact_with_sidecars(root, path):
    """Remove an exact durable-json/file family without globbing another identity."""
    path = Path(path)
    for suffix in ('', '.previous', '.lock', '.transaction.lock', '.coord.lock',
                   '-wal', '-shm', '.bootstrap-config'):
        _unlink(root, Path(str(path) + suffix))


def _binding_rows(root, conversation_id, state):
    rows = list((state or {}).get('bindings', {}).values())
    pending = (state or {}).get('pending_switch') or {}
    staged = []
    for intent in list(pending.get('retarget_history') or []) + [pending.get('retarget') or {}]:
        if isinstance(intent, dict) and isinstance(intent.get('binding'), dict):
            staged.append(intent['binding'])
    by_id = {}
    for row in rows + staged:
        if isinstance(row, dict) and row.get('binding_id'):
            by_id[valid_id(row['binding_id'], 'binding_id')] = row
    # A crash during Binding creation can leave an ownership receipt before the
    # Coordinator stages the Binding. The receipt is the authoritative link for
    # collecting that orphan without guessing from native directory names.
    ownership = Path(root) / 'ownership'
    if ownership.exists() and not ownership.is_symlink():
        from .ownership import Ownership
        store = Ownership(str(ownership))
        for path in ownership.glob('*.json'):
            try:
                bid = valid_id(path.stem, 'binding_id')
                row = store.read(bid)
                if row.get('conversation_id') == conversation_id:
                    by_id.setdefault(bid, row)
            except (InvalidState, OSError, ValueError):
                continue
    return list(by_id.values())


def _lock_released(path):
    try: fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError: return False
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally: os.close(fd)


def release_residents(root, state):
    """Prove every idle resident Writer is gone before deleting its private HOME."""
    root = Path(root)
    bindings = _binding_rows(root, state.get('conversation_id'), state)
    from .resident_process import drop, roster
    live = {row[0] for row in roster()}
    for binding in bindings:
        bid = binding['binding_id']
        if bid in live and not drop(bid):
            raise Conflict('Shepherd 常驻 Writer 尚未确认退出，不能物理删除')
    remaining = {row[0] for row in roster()}
    if any(row['binding_id'] in remaining for row in bindings):
        raise Conflict('Shepherd 常驻 Writer 尚未确认退出，不能物理删除')
    try:
        from codex_appserver import drop_session, live_session_ids
        for binding in bindings:
            native_id = 'shepherd:' + binding['binding_id']
            if native_id in live_session_ids():
                drop_session(native_id)
        remaining = live_session_ids()
        if any('shepherd:' + row['binding_id'] in remaining for row in bindings):
            raise Conflict('Shepherd Codex Writer 尚未确认退出，不能物理删除')
    except ImportError:
        pass
    for binding in bindings:
        bid = binding['binding_id']
        candidates = [root/'resident'/'writer-locks'/(bid+'.lock')]
        adapter = root/'adapter'
        if adapter.exists():
            candidates.extend(Path(writer_lock_path(path, 'shepherd:' + bid))
                              for path in adapter.iterdir() if path.is_dir())
        for path in candidates:
            _safe_parent(root, path)
            if os.path.lexists(path) and not _lock_released(path):
                raise Conflict('Shepherd Writer 锁尚未释放，不能物理删除')


def _remove_claude_payloads(root, binding_ids):
    directory = root / 'native' / 'claude'
    if not directory.exists() or directory.is_symlink():
        return
    expected = {str(directory / (bid + '.sqlite3')) for bid in binding_ids}
    for pattern in ('.shepherd-claude-*.json', '.claude-bootstrap-*.json'):
        for path in directory.glob(pattern):
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    info = os.fstat(fd)
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                            or oscompat.loose_mode(info.st_mode) or info.st_size > 64 * 1024 * 1024):
                        continue
                    with os.fdopen(fd, encoding='utf-8', closefd=False) as stream:
                        payload = json.load(stream)
                finally:
                    os.close(fd)
                db_path = str(payload.get('dbPath') or '') if isinstance(payload, dict) else ''
                if os.path.abspath(db_path) in expected:
                    _unlink(root, path)
            except (OSError, ValueError, TypeError):
                continue


def _remove_scratch(root, binding_ids):
    if not binding_ids:
        return
    directory = root / 'execution'
    _safe_parent(root, directory / '.gc')
    if not directory.exists():
        return
    from .execution_scratch import (_binding_hint, _children_dead, _private, _read,
                                    _scratch_binding_from_name)
    from .recovery import process_state
    _private(directory, True)
    with locked(str(directory / '.gc')):
        for path in directory.glob('turn-*'):
            fd = None
            try:
                binding_id = _scratch_binding_from_name(path.name)
                if binding_id is None:
                    # Legacy random names have no identity before owner.json. A
                    # broken legacy directory cannot be attributed safely, so it
                    # is retained for the ordinary scratch sweeper instead of
                    # blocking deletion of every unrelated conversation.
                    try:
                        binding_id, _hint = _binding_hint(path)
                    except (OSError, ValueError, TypeError, Conflict):
                        continue
                if binding_id not in binding_ids:
                    continue
                lease = path / 'lease.lock'
                _private(lease)
                fd = os.open(lease, os.O_RDWR | os.O_NOFOLLOW)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                data = _read(path)
                if data.get('binding_id') != binding_id:
                    raise Conflict('Shepherd scratch ownership mismatch')
                if ((not data.get('released', False)
                        and process_state(data.get('host')) != 'dead')
                        or not _children_dead(data)):
                    raise Conflict('Shepherd 执行临时目录仍由 Writer 持有，不能物理删除')
                # Windows cannot unlink an open lease. The .gc lock still
                # excludes scratch creation/sweep until this deletion completes.
                if oscompat.IS_WINDOWS:
                    os.close(fd)
                    fd = None
                shutil.rmtree(path)
            except BlockingIOError:
                raise Conflict('Shepherd 执行临时目录仍被占用，不能物理删除') from None
            finally:
                if fd is not None:
                    os.close(fd)


def purge(root, conversation_id, state, *, remove_history=True):
    """Erase all conversation/binding-scoped content after a durable delete marker exists."""
    sid = valid_id(conversation_id, 'conversation_id')
    root = _root(root)
    if not is_deleted(root, sid):
        raise Conflict('Shepherd 物理删除缺少持久删除标记')
    bindings = _binding_rows(root, sid, state)
    binding_ids = {row['binding_id'] for row in bindings}
    release_residents(root, state)
    _remove_scratch(root, binding_ids)
    _remove_claude_payloads(root, binding_ids)

    if remove_history:
        import sqlite3
        from .history_retrieval import HistoryError, HistoryRetrieval
        retrieval = HistoryRetrieval(str(root))
        try:
            retrieval.remove(sid, vacuum=True)
        except (sqlite3.Error, HistoryError):
            # The index is disposable and may contain bytes from this deleted
            # conversation. If it cannot isolate rows safely, erase the whole
            # cache; later reads rebuild it from surviving authoritative stores.
            _remove_exact_with_sidecars(root, retrieval.path)
        _remove_exact_with_sidecars(root, root/'retrieval'/'locks'/sid)

    for directory in ('archive', 'inputs', 'requests', 'image-variants'):
        _unlink(root, root / directory / sid)
    for directory in ('compact-operations', 'recovery-events'):
        _remove_exact_with_sidecars(root, root / directory / (sid + '.json'))
    _remove_exact_with_sidecars(root, root / 'migrations' / 'native-to-unified-v1' / (sid + '.json'))
    backups = root / 'reconciliation-backups'
    if backups.exists():
        for path in backups.iterdir():
            if path.name == sid or path.name.startswith(sid + '-'):
                _unlink(root, path)

    for binding in bindings:
        bid = binding['binding_id']; engine = str(binding.get('engine') or '')
        _unlink(root, root / 'sources' / bid)
        _remove_exact_with_sidecars(root, root / 'ownership' / (bid + '.json'))
        _remove_exact_with_sidecars(root, root / 'resident' / 'writer-locks' / (bid + '.lock'))
        _unlink(root, root / 'resident' / 'claude' / bid)
        _unlink(root, root / 'resident' / 'codex-family' / engine / bid)
        if engine == 'claude':
            _remove_exact_with_sidecars(root, root / 'native' / 'claude' / (bid + '.sqlite3'))
        elif engine == 'pi':
            _remove_exact_with_sidecars(root, root / 'native' / 'pi' / (bid + '.jsonl'))
            _unlink(root, root / 'native' / 'pi' / bid)
        elif engine == 'codex':
            _unlink(root, root / 'native' / engine / bid)
        adapter = root / 'adapter'
        if adapter.exists() and not adapter.is_symlink():
            for directory in adapter.iterdir():
                if directory.is_dir() and not directory.is_symlink():
                    _remove_exact_with_sidecars(
                        root, writer_lock_path(directory, 'shepherd:' + bid))

    # State is removed last: until this point it remains the crash-retry cleanup manifest.
    _unlink(root, root / sid)
    return {'conversation_id':sid, 'deleted':True, 'bindings':len(bindings)}
