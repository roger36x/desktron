"""Read Desk-owned child evidence. No package-global scans or native SID inference."""
import json
import os
import re
import time
from pathlib import Path

SAFE = re.compile(r'^[a-zA-Z0-9_-]+$')
MAX_LOG_BYTES = 8 * 1024 * 1024
STARTUP_GRACE_MS = 5_000


class PiSubagentViewMixin:
    def _child_root(self, sid):
        if sid not in self.index or not SAFE.fullmatch(str(sid)):
            return None
        return Path(self.dir) / 'subagents' / sid

    def subagents(self, sid):
        root = self._child_root(sid)
        if root is None or not root.is_dir():
            return []
        if (root/'_calls').is_dir():
            from pi_children import observe
            observe(self)
        ctx = self._live.get(sid) or {}
        proc = ctx.get('proc')
        rows = []
        for directory in root.iterdir():
            if not SAFE.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
                continue
            try:
                row = json.loads((directory / 'state.json').read_text(encoding='utf-8'))
                if not isinstance(row, dict) or row.get('id') != directory.name or row.get('sessionID') != sid:
                    continue
                running = row.get('status') == 'running'
                # Sidecars identify the Desk-owned supervisor. `workerPid` is the
                # Node child and remains diagnostic only. Legacy rows have only
                # Node's PID, so accept either the current supervisor or one of its
                # direct children while that exact tool call is still active.
                owner_pid = row.get('pid')
                proc_matches = bool(proc is not None and proc.poll() is None and (
                    proc.pid == owner_pid or ('workerPid' not in row and running
                        and row.get('toolCallId') in (ctx.get('_pi_tool') or {})
                        and self._child_pid(proc.pid, owner_pid))))
                invocation_matches = row.get('toolCallId') in (ctx.get('_pi_tool') or {})
                # The child writes its sidecar inside tool.execute while Pi lifecycle
                # events reach the adapter on a separate stream. Permit only this
                # bounded startup race; an old or future-dated unmatched row still closes.
                startup_age = time.time() * 1000 - float(row.get('started') or 0)
                startup_grace = 0 <= startup_age < STARTUP_GRACE_MS
                if row.get('runtime') == 'desk-pi-child-v1':
                    from pi_children import running as child_running
                    proc_matches = child_running(self.dir, sid, directory.name)
                    invocation_matches = True
                if row.get('runtime') == 'pi-subagents-v2':
                    # Native sync fanout can also use detached child processes.
                    # Its parent invocation, not the child's OS parent, owns the wait.
                    proc_matches = bool(proc is not None and proc.poll() is None)
                    if row.get('mode') == 'async':
                        # Upstream owns this lifecycle after the parent exits.
                        proc_matches = invocation_matches = True
                    elif 0 <= time.time()*1000-float(row.get('updated') or 0) < STARTUP_GRACE_MS:
                        # Native tool completion can precede the passive terminal
                        # projection by one refresh interval. Do not flash "gone".
                        proc_matches = invocation_matches = True
                if running and (not proc_matches or (not invocation_matches and not startup_grace)):
                    row.update(status='done', outcome='gone', ended=row.get('updated'))
                row['size'] = (directory / 'events.jsonl').stat().st_size if (directory / 'events.jsonl').exists() else 0
                rows.append({k: row[k] for k in ('id', 'kind', 'task', 'started', 'updated', 'ended',
                    'status', 'outcome', 'last', 'phase', 'size', 'model', 'error', 'error_code',
                    'error_kind', 'exit_code', 'upstream_code', 'retryable') if k in row})
            except (OSError, ValueError, TypeError):
                continue
        return sorted(rows, key=lambda row: row.get('started', 0))

    def subagent_log(self, sid, aid, limit_bytes=None, limit=None):
        root = self._child_root(sid)
        if root is None or not SAFE.fullmatch(str(aid)):
            return []
        directory = root / aid
        if directory.is_symlink() or not any(row['id'] == aid for row in self.subagents(sid)):
            return []
        path = directory / 'events.jsonl'
        try:
            with path.open('rb') as stream:
                size = os.fstat(stream.fileno()).st_size
                max_bytes = self._log_limit_bytes(limit_bytes)
                truncated = size > max_bytes
                if truncated:
                    stream.seek(size - max_bytes)
                    stream.readline()
                lines = stream.read(max_bytes).splitlines()
        except OSError:
            return []
        messages, tools, current = [], {}, None
        def message(role='assistant'):
            row = {'info': {'role': role}, 'parts': []}
            messages.append(row)
            return row
        def text_part(row, kind, text):
            row['parts'].append({'type': kind, 'text': text})
        if truncated:
            text_part(message(), 'text', f'记录较长，当前展示末尾 {self._format_bytes(max_bytes)}。')
        for line in lines:
            try:
                event = json.loads(line)
                kind = event.get('type')
                if kind == 'message_start':
                    current = message()
                elif kind == 'message_update':
                    if current is None:
                        current = message()
                    delta = event.get('assistantMessageEvent') or {}
                    typ = 'reasoning' if delta.get('type') == 'thinking_delta' else 'text'
                    index = delta.get('contentIndex', 0)
                    part = next((p for p in current['parts'] if p.get('_index') == index), None)
                    if part is None:
                        part = {'type': typ, 'text': '', '_index': index}
                        current['parts'].append(part)
                    part['text'] += str(delta.get('delta') or '')
                elif kind == 'message_end':
                    native = event.get('message') or {}
                    if native.get('role') == 'toolResult':
                        part = tools.get(native.get('toolCallId'))
                        if part:
                            part['state'].update(status='error' if native.get('isError') else 'completed',
                                output=self._child_text(native.get('content')))
                        continue
                    if current is None:
                        current = message(native.get('role', 'assistant'))
                    current['parts'] = [p for p in current['parts'] if p['type'] == 'tool']
                    for block in native.get('content') or []:
                        if not isinstance(block, dict):
                            continue
                        if block.get('type') in ('text', 'thinking'):
                            text_part(current, 'reasoning' if block['type'] == 'thinking' else 'text',
                                      block.get('text') or block.get('thinking') or '')
                        elif block.get('type') == 'toolCall':
                            part = {'id': block['id'], 'type': 'tool', 'tool': block.get('name'),
                                'state': {'status': 'running', 'input': block.get('arguments') or {}}}
                            current['parts'].append(part)
                            tools[block['id']] = part
                    current = None
                elif kind in ('tool_execution_start', 'tool_execution_end'):
                    cid = event.get('toolCallId')
                    if not cid:
                        continue
                    part = tools.get(cid)
                    if part is None:
                        part = {'id': cid, 'type': 'tool', 'tool': event.get('toolName'),
                            'state': {'status': 'running', 'input': event.get('args') or {}}}
                        message()['parts'].append(part)
                        tools[cid] = part
                    if kind == 'tool_execution_end':
                        part['state'].update(status='error' if event.get('isError') else 'completed',
                            output=self._child_text((event.get('result') or {}).get('content')))
            except (ValueError, KeyError, TypeError, AttributeError):
                continue  # An append in progress may leave a partial final line.
        rows = [m for m in messages if m['parts']]
        try:
            n = max(0, int(limit or 0))
        except (TypeError, ValueError):
            n = 0
        return rows[-n:] if n else rows

    @staticmethod
    def _child_pid(parent, child):
        """Compatibility for pre-fix sidecars that stored Node's child PID."""
        try:
            if not isinstance(parent, int) or not isinstance(child, int) or parent <= 0 or child <= 0:
                return False
            import subprocess
            result = subprocess.run(['ps', '-p', str(child), '-o', 'ppid='],
                                    capture_output=True, text=True, timeout=1, check=False)
            return result.returncode == 0 and int(result.stdout.strip() or 0) == parent
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            return False

    @staticmethod
    def _child_text(content):
        if isinstance(content, str):
            return content
        return '\n'.join(str(b.get('text') or '') for b in content or [] if isinstance(b, dict) and b.get('type') == 'text')

    @staticmethod
    def _format_bytes(n):
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = MAX_LOG_BYTES
        if n % (1024 * 1024) == 0:
            return f'{n // (1024 * 1024)} MiB'
        if n % 1024 == 0:
            return f'{n // 1024} KiB'
        return f'{n} B'

    @staticmethod
    def _log_limit_bytes(limit_bytes):
        try:
            n = int(limit_bytes)
        except (TypeError, ValueError):
            return MAX_LOG_BYTES
        return max(1, min(MAX_LOG_BYTES, n))

    def running_agents(self, esid):
        sid = esid if esid in self.index else self._sid_by_esid(esid)
        return sum(row.get('status') == 'running' for row in self.subagents(sid)) if sid else 0
