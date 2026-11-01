"""Read Codex revert segments without modifying native logs or their indexes."""
import glob
from contextlib import closing
import hashlib
import json
import mmap
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading


_LOCK = threading.RLock()
_VIEWS = {}


def head(path):
    with open(path, 'rb') as stream:
        row = json.loads(stream.readline())
    if row.get('type') != 'session_meta':
        raise ValueError('missing Codex session metadata')
    return row.get('payload') or {}


def indexed_path(roots, sid):
    """None means no index entry; an invalid indexed path must NOT fall back."""
    # sessions may be symlinked to ~/.codex while state_*.sqlite remains in the
    # managed CODEX_HOME. Prefer the configured home, not the symlink target.
    homes = dict.fromkeys([os.path.dirname(os.path.abspath(root)) for root in roots]
                          + [os.path.dirname(os.path.realpath(root)) for root in roots])
    for home in homes:
        databases = glob.glob(os.path.join(home, 'state_*.sqlite'))
        databases.sort(key=lambda p: int(Path(p).stem.split('_')[-1])
                       if Path(p).stem.split('_')[-1].isdigit() else -1, reverse=True)
        for database in databases:
            try:
                with closing(sqlite3.connect(Path(database).as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                    row = db.execute('SELECT rollout_path FROM threads WHERE id = ?', (sid,)).fetchone()
            except sqlite3.Error as exc:
                raise OSError('Cannot read Codex history index') from exc
            if row is None:
                continue
            path = os.path.realpath(row[0])
            if not any(os.path.commonpath((path, os.path.realpath(root))) == os.path.realpath(root)
                       for root in roots):
                raise ValueError('Codex history index points outside session roots')
            if head(path).get('id') != sid:
                raise ValueError('Codex history index identity mismatch')
            return path
    return None


def candidates(roots, sid):
    """Discover segments by exact thread identity, not by an arbitrary glob hit."""
    out = []
    for root in roots:
        for directory, _dirs, files in os.walk(root):
            for name in files:
                if not (name.startswith('rollout-') and name.endswith('.jsonl')
                        and (name.endswith('-' + sid + '.jsonl') or '-' + sid + '_' in name)):
                    continue
                path = os.path.realpath(os.path.join(directory, name))
                if os.path.commonpath((path, os.path.realpath(root))) != os.path.realpath(root):
                    continue
                if any(p == path for _meta, p in out):
                    continue
                try:
                    meta = head(path)
                except (OSError, ValueError):
                    continue
                if meta.get('id') == sid:
                    out.append((meta, path))
    return out


def _stamp(path):
    st = os.stat(path)
    return (path, st.st_ino, st.st_mtime_ns, st.st_size)


def _check_cut(path, offset, ordinal):
    if not isinstance(offset, int) or not isinstance(ordinal, int) or offset <= 0 or ordinal <= 0:
        raise ValueError('invalid Codex history boundary')
    with open(path, 'rb') as stream:
        if offset > os.fstat(stream.fileno()).st_size:
            raise ValueError('Codex history prefix is missing')
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as raw:
            if raw[offset - 1:offset] != b'\n':
                raise ValueError('Codex history boundary splits a record')
            start = raw.rfind(b'\n', 0, offset - 1) + 1
            last = json.loads(raw[start:offset])
            if last.get('ordinal') != ordinal - 1:
                raise ValueError('Codex history byte and ordinal boundaries disagree')


def _materialize(path, roots, cache_dir, seen):
    path = os.path.realpath(path)
    if path in seen or len(seen) >= 64:
        raise ValueError('cyclic or excessively deep Codex history')
    meta = head(path)
    base = meta.get('history_base')
    if not base:
        return path
    if not isinstance(base, dict):
        raise ValueError('invalid Codex history base')
    stamp = _stamp(path)
    offset, ordinal = base.get('end_byte_offset'), base.get('end_ordinal_exclusive')
    birth = str(meta.get('timestamp') or '')
    parent_id = str(base.get('thread_id') or '')
    if not birth or not parent_id:
        raise ValueError('Codex history base lacks identity')
    parent = None
    if offset != 0 or ordinal != 0:
        parents = [(str(m.get('timestamp') or ''), p) for m, p in candidates(roots, parent_id)
                   if p != path and str(m.get('timestamp') or '') < birth]
        if not parents:
            raise ValueError('Codex retained history segment is missing')
        parents.sort(reverse=True)
        if len(parents) > 1 and parents[0][0] == parents[1][0]:
            raise ValueError('ambiguous Codex history parent')
        parent = _materialize(parents[0][1], roots, cache_dir, seen | {path})
    signature = (stamp, _stamp(parent) if parent else None)
    key = (path, os.path.realpath(cache_dir))
    hit = _VIEWS.get(key)
    if hit and hit[0] == signature and os.path.isfile(hit[1]):
        return hit[1]
    # Revert before the first row has an empty inherited prefix.
    if offset == 0 and ordinal == 0:
        pass
    else:
        _check_cut(parent, offset, ordinal)
    with open(path, 'rb') as stream:
        if json.loads(stream.readline()).get('ordinal') != ordinal:
            raise ValueError('Codex segment does not continue the retained prefix')
    os.makedirs(cache_dir, mode=0o700, exist_ok=True)
    target = os.path.join(cache_dir, hashlib.sha256(path.encode()).hexdigest() + '.jsonl')
    fd, temporary = tempfile.mkstemp(prefix='.history-', dir=cache_dir)
    try:
        with os.fdopen(fd, 'wb') as output, open(path, 'rb') as new:
            if parent:
                with open(parent, 'rb') as old:
                    remaining = offset
                    while remaining:
                        block = old.read(min(remaining, 1024 * 1024))
                        if not block:
                            raise ValueError('Codex history prefix changed while reading')
                        output.write(block)
                        remaining -= len(block)
            shutil.copyfileobj(new, output)
            output.flush()
            os.fsync(output.fileno())
        if _stamp(path) != stamp or (parent and _stamp(parent) != signature[1]):
            raise ValueError('Codex history changed while building its view')
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    _VIEWS[key] = (signature, target)
    return target


def history_view(path, roots, cache_dir):
    """Keep legacy files unchanged; segmented files get a private derived view."""
    if os.path.dirname(os.path.realpath(path)) == os.path.realpath(cache_dir):
        return path
    try:
        meta = head(path)
    except ValueError:
        # Legacy fixtures/rollouts need not begin with session_meta.
        return path
    if not meta.get('history_base'):
        return path
    with _LOCK:
        return _materialize(path, roots, cache_dir, set())
