"""Read-only child projection from conversation-owned native stores.

Never put a private thread ID in the public session index, mutate a shared worker,
search a user's ordinary CLI HOME, or start a connector while serving /agents.
Pi already stores children under the public conversation. Claude's private
SessionStore currently disables Agent/Task, so no Claude child is advertised.
"""
import copy
import json
import os
import re
import time
from pathlib import Path

from durable_json import read, read_value, write

_ID = re.compile(r'^[A-Za-z0-9_-]{1,160}$')
_THREAD = re.compile(r'^[0-9a-fA-F-]{8,64}$')


def _bounded_messages(rows, limit_bytes):
    """Return a newest-first byte-bounded JSON projection for archived children."""
    try:
        budget = max(0, int(limit_bytes or 0))
    except (TypeError, ValueError):
        budget = 0
    if not budget:
        return rows
    kept, used = [], 2  # surrounding JSON array
    for row in reversed(rows):
        size = len(json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) + 1
        if used + size > budget:
            break
        kept.append(row)
        used += size
    return list(reversed(kept))


def _contained(path, root):
    """Reject symlink hops, including an owned HOME replaced with a symlink."""
    path, root = Path(path).absolute(), Path(root).absolute()
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    current = root
    if current.is_symlink():
        return False
    for part in parts:
        current /= part
        if current.is_symlink():
            return False
    return True


def _stamp(path):
    st = os.stat(path, follow_symlinks=False)
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _validated_rollout(path, reader):
    """Read once, strictly; UI metadata readers swallow failures and cannot retire HOME."""
    before = _stamp(path)
    head, start, prompt, last, ended = None, 0, '', '', 0
    calls, agents = {}, {}
    with open(path, 'rb') as stream:
        while raw := stream.readline():
            if not raw.strip():
                continue
            row = json.loads(raw)  # Malformed/truncated bytes must retain HOME.
            if not isinstance(row, dict):
                raise ValueError('Invalid native child rollout record')
            if head is None:
                head = row
            if not start and row.get('type') == 'inter_agent_communication_metadata':
                start, prompt = stream.tell(), ''
            payload = row.get('payload') or {}
            if row.get('type') == 'event_msg' and isinstance(payload, dict):
                if payload.get('type') == 'task_complete':
                    ended = int(payload.get('completed_at') or 0) * 1000 or reader._iso_ms(row.get('timestamp'))
                    last = '已交回'
                elif payload.get('type') == 'task_started':
                    ended = 0
            for item in reader._rollout_items(row):
                kind = str(item.get('type') or '')
                if kind == 'message' and item.get('role') == 'user' and not prompt:
                    prompt = str(item.get('message') or reader._rollout_text(item.get('content')) or '').strip()
                if kind in ('function_call', 'custom_tool_call'):
                    name = item.get('name') or ''
                    if name:
                        last = '调用 ' + name
                elif kind == 'message' and item.get('role') == 'assistant':
                    last = '正在输出'
                cid = item.get('call_id')
                if not cid:
                    continue
                value = item.get('output') if kind.endswith('_output') else (item.get('arguments') or item.get('input'))
                try:
                    value = json.loads(value) if isinstance(value, str) else value
                except ValueError:
                    continue  # Optional title hints, not invalid rollout framing.
                if not isinstance(value, dict):
                    continue
                if kind in ('function_call', 'custom_tool_call') and item.get('name') in reader._SPAWN_TOOLS:
                    text = str(value.get('description') or value.get('message') or value.get('prompt') or '').strip()
                    if text:
                        calls[cid] = text.splitlines()[0][:120]
                elif kind in ('function_call_output', 'custom_tool_call_output') and value.get('agent_id'):
                    agents[cid] = value['agent_id']
    payload = (head or {}).get('payload')
    if (not head or head.get('type') != 'session_meta' or not isinstance(payload, dict)
            or not isinstance(payload.get('id'), str) or not _THREAD.fullmatch(payload['id'])):
        raise ValueError('Native child rollout has no verified session identity')
    if before != _stamp(path):
        raise OSError('Native child rollout changed during cleanup')
    spawn = reader._spawn_of(head)
    name = str(spawn.get('agent_nickname') or payload.get('agent_nickname') or '').strip()
    agent_path = str(spawn.get('agent_path') or payload.get('agent_path') or '').rstrip('/')
    prompt = prompt.splitlines()[0][:120] if prompt else ''
    task = (agent_path.rsplit('/', 1)[-1] if agent_path else
            name if re.search(r'[\u4e00-\u9fff\s]', name) else prompt or name)
    summary = {'tail': (last, ended), 'task': task,
               'spawned': {aid: calls[cid] for cid, aid in agents.items() if cid in calls}}
    return head, start, before, summary


class ShepherdSubagentsMixin:
    def _child_readers(self, sid, only_binding=None, strict=False):
        if sid not in self.index:
            return
        self._check_namespace(sid)
        state = self.service.store.load_raw(sid)
        if not state or state.get('tombstone'):
            return
        for bid, binding in (state.get('bindings') or {}).items():
            if only_binding is not None and bid != only_binding:
                continue
            engine = binding.get('engine')
            if (engine != 'codex' or not _ID.fullmatch(bid)
                    or binding.get('binding_id') != bid
                    or binding.get('conversation_id') != sid):
                continue
            unified = str(binding.get('connector_version') or '').startswith('unified-v1:')
            home = (Path(self.shepherd_root) / 'resident' / 'codex-family' / engine / bid / 'home'
                    if unified else Path(self.shepherd_root) / 'native' / engine / bid / 'home')
            if not _contained(home, self.shepherd_root):
                if strict:
                    raise ValueError('Native child HOME escaped conversation')
                continue
            if not home.is_dir():
                continue
            # Live display keeps the ordinary Codex-family parser/cache logic.
            # Only this shallow reader receives scoped roots and a private index.
            reader = copy.copy(self._worker(engine))
            native_dirs = ('sessions', 'archived_sessions')
            reader.ROLLOUT_DIRS = tuple(str(home / sub) for sub in native_dirs)
            original_head = reader._rollout_head
            reader._rollout_head = lambda path, read=original_head, root=home: (
                read(path) if _contained(path, root) else {})
            original_files = reader._subagent_files
            reader._subagent_files = lambda esid, read=original_files, root=home: [
                (path, head) for path, head in read(esid) if _contained(path, root)]
            # Child detail parsing must not follow imported parent-history links.
            reader._rollout_view = lambda path: path
            # Unified app-server creates its actual thread ID at launch; it is
            # not the synthetic native_session_id in the Binding. Discover only
            # root session_meta rows inside this Binding's owned HOME.
            parents, verified = {}, {}
            def walk_error(error):
                if strict and not (isinstance(error, FileNotFoundError)
                                   and error.filename in reader.ROLLOUT_DIRS):
                    raise error
            for root in reader.ROLLOUT_DIRS:
                if not _contained(root, home):
                    if strict:
                        raise ValueError('Native child directory escaped HOME')
                    continue
                for directory, dirs, files in os.walk(root, onerror=walk_error):
                    if strict and any(not _contained(Path(directory) / name, home) for name in dirs):
                        raise ValueError('Native child directory escaped HOME')
                    dirs[:] = [name for name in dirs if _contained(Path(directory) / name, home)]
                    for name in files:
                        if not (name.startswith('rollout-') and name.endswith('.jsonl')):
                            continue
                        path = os.path.join(directory, name)
                        if strict:
                            if not _contained(path, home):
                                raise ValueError('Native child rollout escaped HOME')
                            verified[path] = _validated_rollout(path, reader)
                            head = verified[path][0]
                        else:
                            head = reader._rollout_head(path)
                        payload = head.get('payload') or {}
                        if (head.get('type') != 'session_meta' or not isinstance(payload, dict)
                                or reader._spawn_of(head)):
                            continue
                        esid = payload.get('id')
                        if not isinstance(esid, str) or not _THREAD.fullmatch(esid):
                            continue
                        if not unified and esid != binding.get('native_session_id'):
                            continue
                        path = os.path.join(directory, name)
                        candidate = (str(payload.get('timestamp') or ''), path)
                        if candidate > parents.get(esid, ('', '')):
                            parents[esid] = candidate
            if strict:
                # No TTL, mtime heuristic or swallowed discovery error may turn
                # an unreadable child into an empty list before HOME deletion.
                children = [(path, entry[0]) for path, entry in verified.items()
                            if reader._spawn_of(entry[0])]
                if any(reader._spawn_of(head).get('parent_thread_id') not in parents
                       for _, head in children):
                    raise ValueError('Native child parent could not be verified')
                reader._subagent_files = lambda esid, rows=children, spawn=reader._spawn_of: [
                    (path, head) for path, head in rows if spawn(head).get('parent_thread_id') == esid]
                reader._verified_rollouts = verified
                # Reuse row shaping, but never its forgiving native I/O helpers.
                # All metadata now comes from the same fully validated reads.
                reader._subagent_tail = lambda path, data=verified: data[path][3]['tail']
                reader._subagent_task = lambda path, head, data=verified: data[path][3]['task']
                reader._spawn_index = lambda esid, data=verified, paths=parents: data[paths[esid][1]][3]['spawned']
            reader._rollout_path = lambda esid, paths=parents, root=home: (
                path if (path := paths.get(esid, ('', ''))[1]) and _contained(path, root) else '')
            ctx = self._live.get(sid) or {}
            active = state.get('active_binding_id') == bid and ctx.get('producer_engine') == engine
            for esid in sorted(parents):
                reader.index = {sid: {'engine_sid': esid}}
                reader._live = {sid: ctx} if active else {}
                yield bid, reader, esid, bool(active)

    @staticmethod
    def _child_id(engine, bid, aid):
        # Native IDs can collide across engines/bindings; details must route to
        # the original Producer even when the conversation switches engines.
        return 'shepherd:' + engine + ':' + bid + ':' + aid

    def _child_archive_path(self, sid, bid):
        if sid not in self.index or not _ID.fullmatch(sid) or not _ID.fullmatch(bid):
            raise ValueError('Invalid Shepherd child archive identity')
        # This subtree shares the public archive's physical-delete boundary.
        path = Path(self.shepherd_root) / 'archive' / sid / 'subagents' / (bid + '.json')
        if not _contained(path, self.shepherd_root):
            raise ValueError('Shepherd child archive escaped conversation')
        return path

    def _archived_children(self, sid):
        root = self._child_archive_path(sid, 'probe').parent
        if root.is_dir():
            for path in root.glob('*.json'):
                if not _contained(path, self.shepherd_root):
                    continue
                value = read_value(path)
                if value.get('sessionID') == sid:
                    yield from (value.get('children') or {}).values()

    def _archive_native_children(self, sid, bid):
        from shepherd.physical_delete import deletion_guard, is_deleted
        # mark_deleted takes this same guard. It either waits for this archive
        # and then purges it, or fences this callback before any file is created.
        with deletion_guard(self.shepherd_root, sid):
            if is_deleted(self.shepherd_root, sid) or sid not in self.index:
                return
            self._archive_native_children_locked(sid, bid)

    def _archive_native_children_locked(self, sid, bid):
        """Called before native HOME disposal, never by a GET or background scan.

        Preserve the ordinary parser's child-only projection, not a copy of the
        disposable parent context. Failure prevents HOME deletion. A child with
        no native terminal is marked gone when its owning app-server is closed.
        """
        if sid not in self.index:
            return  # deletion must never resurrect conversation artifacts
        saved = None
        for _, reader, esid, _live in self._child_readers(sid, only_binding=bid, strict=True):
            files = {head['payload']['id']: path for path, head in reader._subagent_files(esid)}
            rows = reader._subagents_of(esid, False)
            if {row['id'] for row in rows} != set(files):
                raise OSError('Native child projection omitted a discovered rollout')
            for row in rows:
                if saved is None:
                    path = self._child_archive_path(sid, bid)
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    saved = read(path)
                    saved['sessionID'] = sid
                aid = self._child_id(reader.name, bid, row['id'])
                native_path = files[row['id']]
                _, start, stamp, _ = reader._verified_rollouts[native_path]
                if stamp != _stamp(native_path):
                    raise OSError('Native child rollout changed before projection')
                # subagent_log normalizes a failed parse (None) to []; that is
                # appropriate for display, never for a destructive archive.
                messages = reader._group_rollout(native_path, sid, start, exact=bool(start))
                if messages is None or stamp != _stamp(native_path):
                    raise OSError('Native child rollout could not be archived')
                if row.get('status') == 'running':
                    row = {**row, 'status': 'done', 'outcome': 'gone', 'ended': int(time.time() * 1000)}
                log_path = path.with_suffix('') / (row['id'] + '.json')
                if not _THREAD.fullmatch(row['id']) or not _contained(log_path, self.shepherd_root):
                    raise ValueError('Invalid Shepherd child log identity')
                log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                log = read(log_path)
                log.update(sessionID=sid, id=aid, messages=messages)
                write(log_path, log)
                # List polling reads only bounded metadata, never every child's transcript.
                saved.setdefault('children', {})[aid] = {
                    'row': {**row, 'id': aid, 'engine': reader.name}}
        if saved is not None:
            write(path, saved)

    def subagents(self, sid):
        if sid not in self.index:
            return []
        rows = {item['row']['id']: item['row'] for item in self._archived_children(sid)}
        rows.update((row['id'], row) for row in self._worker('pi').subagents(sid))
        for bid, reader, esid, live in self._child_readers(sid):
            for row in reader._subagents_of(esid, live):
                aid = self._child_id(reader.name, bid, row['id'])
                rows[aid] = {**row, 'id': aid, 'engine': reader.name}
        return sorted(rows.values(), key=lambda row: row.get('started') or 0)

    def subagent_log(self, sid, aid, limit_bytes=None, limit=None):
        if sid not in self.index or not isinstance(aid, str):
            return []
        if not aid.startswith('shepherd:'):
            return self._worker('pi').subagent_log(sid, aid, limit_bytes=limit_bytes, limit=limit)
        parts = aid.split(':')
        if len(parts) != 4 or not _THREAD.fullmatch(parts[3]):
            return []
        _, engine, bid, native_id = parts
        for binding_id, reader, _esid, _live in self._child_readers(sid):
            if binding_id == bid and reader.name == engine:
                rows = reader.subagent_log(
                    sid, native_id, limit_bytes=limit_bytes, limit=limit or 200)
                if rows:
                    return rows
        for item in self._archived_children(sid):
            if item['row']['id'] == aid:
                log_path = self._child_archive_path(sid, bid).with_suffix('') / (native_id + '.json')
                if not _contained(log_path, self.shepherd_root):
                    return []
                log = read_value(log_path)
                if log.get('sessionID') == sid and log.get('id') == aid:
                    rows = log.get('messages') or []
                    if limit:
                        rows = rows[-limit:]
                    return _bounded_messages(rows, limit_bytes)
        return []

    def running_agents(self, sid):
        return sum(row.get('status') == 'running' for row in self.subagents(sid))
