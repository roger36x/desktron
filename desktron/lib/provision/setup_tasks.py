"""Durable setup operations for interactive setup terminals.

Commands, input, credentials and raw authentication output are never persisted.
A task identity is committed before its terminal starts. Reconciliation observes
the existing terminal, but never replays work.
"""
import hashlib
import math
import os
import re
import time
import uuid

from durable_json import read, write, locked

_BOOT = uuid.uuid4().hex
_owned = {}
_ACTIVE = {'starting', 'waiting_terminal', 'running'}
_TERMINAL = 'terminal'


AUTH_COMMANDS = ('lark-cli auth login', 'claude auth login', 'codex login')


def _description(command):
    # Only known, credential-free command names are retained.
    # Never store a shell command or arbitrary user-supplied title.
    auth = command if command in AUTH_COMMANDS else ''
    return dict(title='登录配置' if auth else '首次设置操作', auth_command=auth)


def _path():
    from deskcfg import DATA_HOME
    return os.path.join(DATA_HOME, 'setup-tasks.json')


def _terminal(sid):
    import termpty
    return termpty.pty_info(sid)


def _executor(row):
    return row.get('executor') or _TERMINAL


def _read(path):
    rows = read(path)
    states = _ACTIVE | {'succeeded', 'failed', 'interrupted'}
    for key, row in rows.items():
        executor = _executor(row) if isinstance(row, dict) else ''
        if (not isinstance(row, dict) or row.get('id') != key
                or not isinstance(row.get('state'), str) or row['state'] not in states
                or executor != _TERMINAL
                or not isinstance(row.get('terminal_id', ''), str)
                or not isinstance(row.get('boot'), str) or not isinstance(row.get('fingerprint'), str)
                or not all(type(row.get(k)) in (int, float) and math.isfinite(row[k])
                           for k in ('created_at', 'updated_at'))):
            raise ValueError('安装任务记录格式错误；原文件已保留，禁止以空任务重试')
    return rows


def _reconcile(rows):
    changed = False
    now = time.time()
    for row in rows.values():
        if row['state'] not in _ACTIVE:
            continue
        info = _terminal(row.get('terminal_id', '')) if row.get('boot') == _BOOT else None
        mismatched = row.get('boot') != _BOOT or (info and info.get('pid') != row.get('pid'))
        # EOF and waitpid run in different threads; leave time to record the exit.
        vanished = not info and now - row['updated_at'] > 30
        if mismatched or vanished:
            row.update(state='interrupted', updated_at=now)
            changed = True
    return changed


def _public(row):
    if not row:
        return None
    executor = _executor(row)
    info = _terminal(row.get('terminal_id', '')) if row.get('boot') == _BOOT else None
    # Durable files may outlive the code that wrote them. Project only the fixed
    # schema so legacy or damaged records cannot expose commands or extra fields.
    fields = ('id', 'state', 'created_at', 'updated_at', 'terminal_id',
              'exit_code', 'signal')
    result = {key: row[key] for key in fields if key in row}
    auth = row.get('auth_command') if row.get('auth_command') in AUTH_COMMANDS else ''
    result.update(output='', title='登录配置' if auth else '首次设置操作',
                  auth_command=auth, executor=executor,
                  attachable=bool(info and info.get('pid') == row.get('pid')
                                  and row['state'] in _ACTIVE))
    return result


def recent():
    path = _path()
    with locked(path + '.coord'):
        rows = _read(path)
        if _reconcile(rows):
            write(path, rows)
        return [_public(row) for row in sorted(rows.values(), key=lambda r: r['created_at'], reverse=True)[:20]]


def latest():
    rows = recent()
    return next((r for r in rows if r['state'] in _ACTIVE), rows[0] if rows else None)


def start(request_id, command, spawn):
    """Start the existing interactive PTY executor."""
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', request_id):
        raise ValueError('安装任务请求编号无效')
    fingerprint = hashlib.sha256(command.encode()).hexdigest()
    path = _path()
    with locked(path + '.coord'):
        rows = _read(path)
        if _reconcile(rows):
            write(path, rows)
        if request_id in rows:
            row = rows[request_id]
            if row['fingerprint'] != fingerprint:
                raise ValueError('不能改变已有安装任务的命令或目标')
            return _public(row)
        active = next((row for row in rows.values() if row['state'] in _ACTIVE), None)
        if active:
            return _public(active)
        now = time.time()
        row = dict(id=request_id, state='starting', fingerprint=fingerprint, boot=_BOOT,
                   executor=_TERMINAL, created_at=now, updated_at=now,
                   terminal_id='', pid=None, output='', **_description(command))
        rows[request_id] = row
        write(path, rows)  # Fail here BEFORE creating any process.
        try:
            session = spawn()
            if not session:
                raise RuntimeError('无法创建安装终端')
            sid = session['id']
            info = _terminal(sid) or {}
            rows[request_id].update(terminal_id=sid, pid=info.get('pid'),
                                    state='waiting_terminal', updated_at=time.time())
            write(path, rows)
            _owned[sid] = request_id
        except Exception:
            if 'session' in locals() and session:
                import termpty
                termpty.pty_kill(session['id'])
            rows[request_id].update(state='failed', updated_at=time.time())
            write(path, rows)
            raise
        return _public(rows[request_id])


def started(sid):
    key = _owned.get(sid)
    if not key:
        return
    path = _path()
    with locked(path + '.coord'):
        rows = _read(path)
        if key in rows and rows[key]['state'] == 'waiting_terminal':
            rows[key].update(state='running', updated_at=time.time())
            write(path, rows)


def finish_terminal(sid, info, output):
    key = _owned.get(sid)
    if not key:
        return
    path = _path()
    with locked(path + '.coord'):
        rows = _read(path)
        row = rows.get(key)
        if not row or row.get('boot') != _BOOT or _executor(row) != _TERMINAL:
            return
        code = info.get('code')
        state = 'succeeded' if code == 0 else ('failed' if code is not None else 'interrupted')
        row.update(state=state, exit_code=code, signal=info.get('sig'), updated_at=time.time())
        # Login terminal output is never retained; it can contain one-time codes.
        write(path, rows)
        _owned.pop(sid, None)
