"""Read committed Pi v3 sources without importing or running the installed SDK."""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import json
import os
import stat

from .store import Conflict


def read_entries(path, session_id, cwd):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or oscompat.loose_mode(before.st_mode) or before.st_size > 512 * 1024 * 1024):
                raise Conflict('Pi 历史文件类型、权限或大小无效')
            raw = stream.read(before.st_size + 1)
            after = os.fstat(stream.fileno())
        fingerprint = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if fingerprint(before) != fingerprint(after) or len(raw) != before.st_size:
            raise Conflict('Pi 历史读取期间发生变化')
        if not raw.endswith(b'\n'):
            raise Conflict('Pi 历史记录不完整')
        rows = [json.loads(line) for line in raw.splitlines()]
    except (OSError, ValueError, UnicodeError):
        raise Conflict('Pi 历史文件缺失或格式无效') from None
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise Conflict('Pi 历史记录格式无效')
    header, entries = rows[0], rows[1:]
    native_cwd = header.get('cwd')
    if (header.get('type') != 'session' or type(header.get('version')) is not int
            or header['version'] != 3 or header.get('id') != session_id
            or not isinstance(native_cwd, str) or not os.path.isabs(native_cwd)
            or os.path.realpath(native_cwd) != cwd):
        raise Conflict('Pi 历史 Session 身份或格式版本不匹配')
    seen = set()
    for entry in entries:
        identity = entry.get('id')
        if (not isinstance(identity, str) or not identity or identity in seen
                or not isinstance(entry.get('type'), str) or entry['type'] == 'session'
                or 'parentId' not in entry
                or entry['parentId'] is not None and (
                    not isinstance(entry['parentId'], str) or entry['parentId'] not in seen)):
            raise Conflict('Pi 历史树结构无效')
        seen.add(identity)
    return entries


def source_range(entries, query, codec):
    if 'roundId' not in query:
        start, end = query.get('start'), query.get('end')
        if (type(start) is not int or type(end) is not int
                or start < 0 or end <= start or end > len(entries)):
            raise Conflict('Pi source range invalid')
        return entries[start:end]
    boundaries = [[i for i, row in enumerate(entries) if row.get('type') == 'custom'
                   and row.get('customType') == marker
                   and isinstance(row.get('data'), dict)
                   and row['data'].get('roundId') == query['roundId']]
                  for marker in ('shepherd-round-start', 'shepherd-round-commit')]
    starts, commits = boundaries
    if len(starts) != 1 or len(commits) != 1 or starts[0] >= commits[0]:
        raise Conflict('Pi round boundary missing or ambiguous')
    start, end = starts[0], commits[0]
    first, last = entries[start]['data'], entries[end]['data']
    if (first.get('entryHash') != last.get('entryHash')
            or first.get('codec') != codec or last.get('codec') != codec):
        raise Conflict('Pi round marker mismatch')
    return entries[start:end + 1]
