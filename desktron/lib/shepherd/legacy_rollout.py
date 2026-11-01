"""Read a legacy private rollout without starting or upgrading its native writer."""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import json
import os
import stat

from .store import Conflict

_MAX_BYTES = 512 * 1024 * 1024
_RECORDS = {'session_meta', 'response_item', 'history_mutation', 'compacted',
            'world_state', 'turn_context', 'event_msg', 'token_usage_record',
            'guardian_assessment', 'inter_agent_communication_metadata'}


def read_rollout(home, *, engine, thread_id, cwd):
    """Find one owned source under the binding HOME; return one stable snapshot.

    Directory descriptors and O_NOFOLLOW keep discovery/read within that HOME.
    No chmod, native resume, database migration, or fallback to a user HOME.
    The caller still verifies ownership, receipts and committed source hashes.
    """
    candidates = []
    visited = 0

    def walk(fd, path, depth=0):
        nonlocal visited
        if depth > 8:
            raise Conflict('Codex-family 历史目录层级超限')
        for name in os.listdir(fd):
            visited += 1
            if visited > 100000:
                raise Conflict('Codex-family 历史目录条目超限')
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            target = name.startswith('rollout-') and name.endswith(thread_id + '.jsonl')
            if stat.S_ISLNK(info.st_mode):
                raise Conflict('Codex-family 历史目录不能经过符号链接')
            if stat.S_ISDIR(info.st_mode):
                # Externalized content is independently verified on expansion.
                if name.endswith('.artifacts'):
                    continue
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if os.fstat(child).st_uid != os.getuid():
                        raise Conflict('Codex-family 历史目录所有权无效')
                    walk(child, os.path.join(path, name), depth + 1)
                finally:
                    os.close(child)
            elif target:
                source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(source, 'rb') as stream:
                    before = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                            or before.st_size > _MAX_BYTES):
                        raise Conflict('Codex-family Rollout 类型、所有权或大小无效')
                    raw = stream.read(_MAX_BYTES + 1)
                    after = os.fstat(stream.fileno())
                fingerprint = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
                if fingerprint(before) != fingerprint(after) or len(raw) != before.st_size:
                    raise Conflict('Codex-family Rollout 读取期间发生变化')
                candidates.append((os.path.join(path, name), raw))
                if len(candidates) > 1:
                    raise Conflict('Codex-family 历史 Rollout 不唯一')

    if oscompat.IS_WINDOWS:
        candidates = _windows_candidates(home, thread_id)
    else:
        try:
            fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                if os.fstat(fd).st_uid != os.getuid():
                    raise Conflict('Codex-family HOME 所有权无效')
                base = home
                for name in ('sessions', 'archived_sessions'):
                    try:
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    except FileNotFoundError:
                        continue
                    try:
                        if os.fstat(child).st_uid != os.getuid():
                            raise Conflict('Codex-family 历史目录所有权无效')
                        walk(child, os.path.join(base, name))
                    finally:
                        os.close(child)
            finally:
                os.close(fd)
        except OSError:
            raise Conflict('Codex-family 历史 Rollout 缺失或不可安全读取') from None
    if len(candidates) != 1:
        raise Conflict('Codex-family 历史 Rollout 缺失或不唯一')
    path, raw = candidates[0]
    if not raw.endswith(b'\n'):
        raise Conflict('Codex-family 历史记录不完整')
    try:
        records = [json.loads(line) for line in raw.splitlines()]
    except (ValueError, UnicodeError):
        raise Conflict('Codex-family 历史记录 JSON 无效') from None
    if (not records
            or any(not isinstance(row, dict) or not isinstance(row.get('type'), str)
                   or row['type'] not in _RECORDS
                   or not isinstance(row.get('payload'), dict) for row in records)
            or records[0]['type'] != 'session_meta'):
        raise Conflict('Codex-family 历史记录格式不受支持')
    metas = [row['payload'] for row in records if row['type'] == 'session_meta']
    native_cwd = metas[0].get('cwd') if len(metas) == 1 else None
    if (len(metas) != 1 or metas[0].get('id') != thread_id
            or not isinstance(metas[0].get('cli_version'), str)
            or not metas[0]['cli_version'].strip()
            or not isinstance(native_cwd, str) or not os.path.isabs(native_cwd)
            or os.path.realpath(native_cwd) != cwd):
        raise Conflict('Codex-family 历史 Session 身份或格式版本不匹配')
    return path, raw.splitlines(keepends=True)


def _windows_candidates(home, thread_id):
    """Windows 不能以目录句柄遍历。按路径遍历，并拒绝经过符号链接或 junction。"""
    candidates = []
    visited = 0

    def read_target(full):
        with open(full, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_BYTES:
                raise Conflict('Codex-family Rollout 类型、所有权或大小无效')
            raw = stream.read(_MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
        fingerprint = lambda s: (s.st_size, s.st_mtime_ns)
        if fingerprint(before) != fingerprint(after) or len(raw) != before.st_size:
            raise Conflict('Codex-family Rollout 读取期间发生变化')
        return raw

    def walk(path, depth=0):
        nonlocal visited
        if depth > 8:
            raise Conflict('Codex-family 历史目录层级超限')
        for name in os.listdir(path):
            visited += 1
            if visited > 100000:
                raise Conflict('Codex-family 历史目录条目超限')
            full = os.path.join(path, name)
            info = os.lstat(full)
            if stat.S_ISLNK(info.st_mode) or oscompat.is_link(full):
                raise Conflict('Codex-family 历史目录不能经过符号链接')
            if stat.S_ISDIR(info.st_mode):
                if name.endswith('.artifacts'):
                    continue
                walk(full, depth + 1)
            elif name.startswith('rollout-') and name.endswith(thread_id + '.jsonl'):
                candidates.append((full, read_target(full)))
                if len(candidates) > 1:
                    raise Conflict('Codex-family 历史 Rollout 不唯一')

    try:
        if oscompat.is_link(home) or not os.path.isdir(home):
            raise FileNotFoundError(home)
        for name in ('sessions', 'archived_sessions'):
            child = os.path.join(home, name)
            if not os.path.lexists(child):
                continue
            if oscompat.is_link(child) or not os.path.isdir(child):
                raise Conflict('Codex-family 历史目录所有权无效')
            walk(child)
    except OSError:
        raise Conflict('Codex-family 历史 Rollout 缺失或不可安全读取') from None
    return candidates
