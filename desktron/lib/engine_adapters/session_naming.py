"""Desk 名称是权威；原生名称只作为可重试的副本。"""
import json
import queue
import time
import uuid
from contextlib import nullcontext

from session_names import read_titles, resolve_title


def prepare(adapter, sid):
    owner = getattr(adapter, 'owner', adapter)
    # TurnRunnerMixin is also used by small protocol adapters and test doubles.
    # Naming is an optional capability; lacking its store must not block a turn.
    if not isinstance(getattr(owner, 'index', None), dict) or not getattr(owner, 'workspace', ''):
        return {'title': '', 'native_title': '', 'key': ('', 0), 'notice': ''}
    with getattr(owner, '_lock', nullcontext()):
        row = dict(owner.index.get(sid) or {})
    custom = read_titles(owner.workspace, 'custom-titles.json')
    managed = next((custom[k] for k in (row.get('engine_sid'), sid)
                    if isinstance(custom.get(k), str) and custom[k].strip()), '')
    title = resolve_title(row, (row.get('engine_sid'), sid), custom,
                          read_titles(owner.workspace, 'auto-titles.json')) or '新会话'
    epoch = getattr(adapter, '_name_epoch', {}).get(sid, 0)
    key = (title, epoch)
    return {'title': title, 'native_title': managed.strip() or (title if row.get('desk_name_fallback') else ''), 'key': key,
            'notice': ''}  # Titles are control-plane data, never model instructions.


def delivered(adapter, sid, ticket, ctx, abnormal):
    """Compatibility callback; model turns no longer acknowledge name notices.

    Native name synchronization has its own control-plane acknowledgment.
    """
    return None


def invalidate(adapter, sid):
    if not hasattr(adapter, '_name_epoch'):
        adapter._name_epoch = {}
    adapter._name_epoch[sid] = adapter._name_epoch.get(sid, 0) + 1


def status(adapter, sid, title, state):
    owner = getattr(adapter, 'owner', adapter)
    changed = False
    with owner._lock:
        row = owner.index.get(sid)
        value = {'state': state, 'title': title, 'engine': adapter.name}
        if row is not None and row.get('name_sync') != value:
            row['name_sync'] = value
            owner._save_index()
            changed = True
    if changed and state in ('failed', 'unsupported'):
        adapter._env_publish(sid, {
            'state': 'warning', 'stage': 'session_name',
            'label': '原生名称同步失败' if state == 'failed' else '当前引擎会话不支持原生改名',
            'details': ['Desk 名称已保存；原生名称副本未同步，不影响 Desk 会话身份。']})
    adapter._trace(sid, {'ev': 'session.name.sync', 'engine': adapter.name, 'state': state})


def sync_pipe(adapter, ctx, ep):
    """Called with exclusive reader ownership, before the user frame is sent.

    These control frames do not invoke the model. Unknown methods are explicit
    compatibility states; timeouts/closed pipes fail the send, never inject /rename.
    """
    title = (ctx.get('_session_name') or {}).get('native_title')
    if not title or adapter.name not in ('claude', 'pi'):
        return
    if getattr(ep, 'desk_name_unsupported', False):
        status(adapter, ctx['sid'], title, 'unsupported')
        return
    def fail(message):
        status(adapter, ctx['sid'], title, 'failed')
        raise RuntimeError(message)

    def request(payload, claude=False):
        rid = 'desk-name-' + uuid.uuid4().hex
        frame = ({'type': 'control_request', 'request_id': rid, 'request': payload}
                 if claude else dict(payload, id=rid))
        ep.proc.stdin.write(json.dumps(frame, ensure_ascii=False) + '\n')
        ep.proc.stdin.flush()
        deadline = time.monotonic() + 8
        while True:
            try:
                line = ep.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                fail('原生会话名称同步未获确认')
            if line is None:
                fail('原生会话名称同步时连接已关闭')
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if claude and obj.get('type') == 'control_response':
                response = obj.get('response') or {}
                if response.get('request_id') == rid:
                    return response.get('subtype') == 'success', str(response.get('error') or '')
            elif not claude and obj.get('type') == 'response' and obj.get('id') == rid:
                return bool(obj.get('success')), str(obj.get('error') or '')
            adapter.handle_event(obj, ctx)
            if time.monotonic() >= deadline:
                fail('原生会话名称同步未获确认')
    if adapter.name == 'claude':
        if not getattr(ep, 'desk_name_initialized', False):
            ok, error = request({'subtype': 'initialize'}, claude=True)
            if not ok:
                status(adapter, ctx['sid'], title, 'failed')
                return
            ep.desk_name_initialized = True
        ok, error = request({'subtype': 'rename_session', 'title': title}, claude=True)
    else:
        ok, error = request({'type': 'set_session_name', 'name': title})
    unsupported = any(x in error.lower() for x in ('unknown command', 'unknown request',
                       'unsupported', 'not supported', 'does not support', 'unrecognized'))
    if unsupported:
        ep.desk_name_unsupported = True
    status(adapter, ctx['sid'], title, 'synced' if ok else 'unsupported' if unsupported else 'failed')
