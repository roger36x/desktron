"""Native execution runners for Shepherd-owned bindings."""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import tempfile
import threading
import time
import uuid

from .claude_bridge_snapshot import bridge_path
from .claude_codec import (CODEC_VERSION as CLAUDE_CODEC_VERSION,
                           static_system_prompt)
from .execution_budget import runner_timeouts
from .source_receipts import SourceReceipts
from .store import Conflict, InvalidState
from .runner_process import NativeProcess
from protected_paths import MACOS_AGENT_PROTECTED_DIRS

CLAUDE_BRIDGE_PROTOCOL_VERSION = 3


# These variables describe the Claude process which launched Desk, not the next
# Claude process that Desk is about to start. Inheriting them after a Desk
# restart can pin Shepherd to a temporary config directory and an expired
# messaging credential. Provider credentials (ANTHROPIC_*) are deliberately
# untouched; explicit per-run overrides are merged separately by claude_runner.
CLAUDE_TRANSIENT_ENV = frozenset({
    'CLAUDECODE', 'CLAUDE_AGENT_SDK_VERSION', 'CLAUDE_CODE_CHILD_SESSION',
    'CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING',
    'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_EXECPATH',
    'CLAUDE_CODE_MESSAGING_SOCKET', 'CLAUDE_CODE_MESSAGING_TOKEN',
    'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CONFIG_DIR', 'CLAUDE_EFFORT', 'CLAUDE_PID',
})


def clean_claude_parent_env(env):
    return {key: value for key, value in dict(env or {}).items()
            if key not in CLAUDE_TRANSIENT_ENV}


class ReceiptRunner:
    def recover_result(self, round_id):
        receipt = self.receipts.read(self.binding, round_id, required=False)
        restore = getattr(self.connector, 'recover_terminal_receipt', None)
        if receipt is None and callable(restore):
            receipt = restore(round_id)
        locator = (receipt or {}).get('locator') or {}
        state = locator.get('terminal_state')
        if state not in ('completed', 'failed_recoverable', 'interrupted_confirmed'):
            return None  # Older boundary-only receipts do not prove terminal state.
        self.connector.verify()
        return {'source':self._source(round_id), 'terminal_state':state,
                'native_revision':locator.get('native_revision', '')}


class PiRunner(ReceiptRunner):
    def __init__(self, connector, *, on_event=None, on_process=None, on_stall=None,
                 on_review=None, options=None, system_text='', env=None, timeout=900,
                 command=None, user_frame=None):
        self.connector = connector
        self.binding = connector.binding
        self.on_event = on_event
        self.on_process = on_process
        self.on_stall = on_stall
        self.on_review = on_review
        self.options = dict(options or {})
        self.system_text = str(system_text or '')
        from pi_extensions import without_legacy_web_secrets
        self.env = without_legacy_web_secrets(env or os.environ)
        self.timeout = timeout
        self.command = command
        self.user_frame = user_frame
        self.process = None
        self.cancelled = threading.Event()
        self.receipts = SourceReceipts(connector.store_root)

    def run(self, *, prompt, round_id, on_dispatched=None,
            on_native_locator=None, **_kwargs):
        if not isinstance(prompt, str):
            raise InvalidState('Pi Shepherd prompt 必须是文本')
        self.connector.ownership.verify(self.binding, self.connector.ownership_nonce)
        self.connector.bootstrap()
        before = self.connector.verify()
        from pi_extensions import without_legacy_web_secrets
        runtime_env = without_legacy_web_secrets(self.env)
        harness_dir = str(Path(__file__).parents[1] / 'engine_adapters')
        command = [shutil.which('pi') or 'pi', '--mode', 'rpc', '--offline',
                   '--session', self.connector.path, '--no-extensions',
                   '--extension', os.path.join(harness_dir, 'pi_gate.ts'),
                   '--extension', os.path.join(harness_dir, 'pi_harness_loop.ts'),
                   '--extension', os.path.join(harness_dir, 'pi_autocompact.ts'), '--no-approve']
        from pi_extensions import (
            append_system_prompt_if_missing, enforce_prompt_isolation, enforce_session_path,
            provider_extension_args, web_extension_args,
        )
        command = enforce_prompt_isolation(command)
        command += provider_extension_args()
        command += web_extension_args(runtime_env)
        model = str(self.options.get('model') or '').strip()
        provider = str(self.options.get('provider') or '').strip()
        if model: command += ['--model', model]
        if provider and '/' not in model: command += ['--provider', provider]
        effort = str(self.options.get('effort') or '').strip()
        if effort: command += ['--thinking', effort]
        if self.system_text: command += ['--append-system-prompt', self.system_text]
        if self.command is not None:
            command = enforce_prompt_isolation(self.command)
            command = enforce_session_path(command, self.connector.path)
            command = append_system_prompt_if_missing(command, self.system_text)
        budget = runner_timeouts(self.options, idle_timeout=self.timeout)
        self.process = process = NativeProcess(command, cwd=self.connector.native_cwd,
            env=runtime_env, timeout=budget['idle_timeout'], hard_timeout=budget['hard_timeout'],
            operation='compact' if prompt == '/compact' else 'turn',
            cancel_frame={'type':'abort'}, on_process=self.on_process, on_stall=self.on_stall)
        terminal = ''
        settled = False
        try:
            if not callable(on_dispatched):
                raise InvalidState('Pi Shepherd 缺少持久化派发闸')
            on_dispatched(process=process.proc)
            process.send(self.user_frame or {'type':'prompt','message':prompt})
            if self.cancelled.is_set(): process.cancel()
            for event in process.events():
                if self.on_event: self.on_event(event)
                if event.get('type') == 'message_end':
                    message = event.get('message') or {}
                    if message.get('role') == 'assistant':
                        # Automatic retry may succeed after an earlier error.
                        from .pi_terminal import assistant_terminal
                        terminal = assistant_terminal(message)
                if event.get('type') == 'agent_settled':
                    settled = True
                    # Pi may encode an aborted provider request as an error
                    # message. Its settled event, not that message, proves the
                    # cancelled tool/model loop has actually stopped.
                    terminal = ('interrupted_confirmed' if self.cancelled.is_set()
                                else terminal or 'completed')
                    process.end_input()
        finally:
            process.close()
        if not settled: raise Conflict('Pi Shepherd 执行缺少 agent_settled 终态')
        after = self.connector.verify()
        start, end = int(before.get('entries') or 0), int(after.get('entries') or 0)
        if end <= start: raise Conflict('Pi Shepherd 回合没有持久化来源')
        from .source_codec import PI_PROJECTION_VERSION
        self.receipts.write(self.binding, round_id, {'start_seq':start, 'end_seq':end,
            'projection_version':PI_PROJECTION_VERSION,
            'terminal_state':terminal, 'native_revision':after.get('native_revision') or self.connector.revision()})
        return {'source':self._source(round_id),'terminal_state':terminal,
                'native_revision':after.get('native_revision') or self.connector.revision()}

    def cancel(self):
        self.cancelled.set()
        if self.process: self.process.cancel()
        return True

    def _source(self, entry_id):
        sid = self.connector.native_session_id
        return {'kind':'native_round','engine':'pi','session_id':sid,'entry_id':entry_id,
                'source_key':f'pi:{sid}:{entry_id}'}


class CodexFamilyRunner(ReceiptRunner):
    def __init__(self, connector, *, on_event=None, on_approval=None, on_stall=None,
                 options=None, system_text='', runtime_roots=None, env=None, timeout=900,
                 image_paths=None, on_process=None):
        self.connector = connector
        self.binding = connector.binding
        self.on_event = on_event
        self.on_approval = on_approval
        self.on_stall = on_stall
        self.options = dict(options or {})
        self.system_text = str(system_text or '')
        self.runtime_roots = list(runtime_roots or [])
        self.env = dict(env or os.environ)
        self.timeout = timeout
        self.image_paths = image_paths
        self.on_process = on_process
        self.cancelled = threading.Event()
        self.connection = None
        self.thread_id = ''
        self.turn_id = ''
        self._cancel_timer = None
        self._connection_lock = threading.Lock()
        self.receipts = SourceReceipts(connector.store_root)

    def run(self, *, prompt, round_id, conversation_id, on_dispatched=None,
            on_native_locator=None, **_kwargs):
        if not isinstance(prompt, str):
            raise InvalidState('Codex-family Shepherd prompt 必须是文本')
        self.connector._available(); self.connector._safe_home()
        self.connector.ownership.verify(self.binding, self.connector.ownership_nonce)
        from codex_appserver import run_turn
        ids = {}
        terminal = {}
        def started(thread_id, turn_id):
            ids.update(thread_id=str(thread_id or ''), turn_id=str(turn_id or ''))
            self._turn_started(ids['thread_id'], ids['turn_id'])
            if callable(on_native_locator):
                on_native_locator({'turn_id': ids['turn_id']})
        def event(method, params):
            if method == 'turn/started' and self.cancelled.is_set(): self.cancel()
            if method == 'turn/completed' and ids.get('turn_id') and (params.get('turn') or {}).get('id') == ids['turn_id']:
                terminal.update(params.get('turn') or {})
                self._disconnected()
            if self.on_event: self.on_event(method, params)
        env = dict(self.env)
        env['CODEX_HOME'] = self.connector.home
        returned = None
        dispatched = False
        for attempt in range(5):
            try:
                def submitting(_thread_id):
                    nonlocal dispatched
                    if not callable(on_dispatched):
                        raise InvalidState('Codex-family Shepherd 缺少持久化派发闸')
                    with self._connection_lock:
                        process = self.connection.proc if self.connection is not None else None
                    on_dispatched(process=process)
                    dispatched = True
                    return True
                returned = run_turn(
                    self.connector.native_cwd, env, self.connector.native_session_id,
                    prompt, self.options, system_text=self.system_text,
                    turn_system_text=(
                        '【本回合说明】以下内容只对当前回合有效；如与历史内容冲突，以本条为准。\n'
                        + self.system_text if self.connector.engine == 'codex' else None),
                    runtime_roots=self.runtime_roots, on_notification=event,
                    timeout=self.timeout,
                    hard_timeout=runner_timeouts(self.options, idle_timeout=self.timeout)['hard_timeout'],
                    session_id='',
                    image_paths=self.image_paths, on_process=self.on_process,
                    on_connection=self._connected,
                    cancelled=self.cancelled.is_set,
                    binary=self.connector.binary, label='Codex',
                    on_approval=self.on_approval, on_turn_started=started,
                    on_turn_submitting=submitting,
                    on_model_selected=lambda model:event('model/selected', {'model':model}),
                    history_items=None, strict_boundary=True,
                    on_stall=self.on_stall)
                break
            except Exception as exc:
                # A terminal failed turn still has durable native history.
                if terminal.get('status') == 'failed' and ids.get('turn_id'):
                    returned = self.connector.native_session_id
                    break
                # Reopening the whole turn is safe only before the durable dispatch fence.
                # Once submitting() has run, recovery must inspect native evidence instead of
                # risking a duplicate turn (and duplicate tool side effects).
                if dispatched or 'active writer' not in str(exc).lower() or attempt == 4:
                    raise
                time.sleep(.25 * (attempt + 1))
            finally:
                self._disconnected()
        if returned != self.connector.native_session_id or ids.get('thread_id') != returned:
            raise Conflict('Codex-family Runner 返回了错误 Thread')
        turn_id = ids.get('turn_id') or ''
        if not turn_id:
            raise Conflict('Codex-family Runner 缺少原生 Turn ID')
        state = {'completed':'completed', 'failed':'failed_recoverable',
                 'interrupted':'interrupted_confirmed'}.get(terminal.get('status'))
        if not state or terminal.get('id') != turn_id:
            raise Conflict('Codex-family Runner 缺少匹配的原生终态')
        verification = self.connector.verify()
        self.receipts.write(self.binding, round_id, {'turn_id': turn_id,
            'projection_version':2,
            'terminal_state':state, 'native_revision':verification['native_revision']})
        return {'source': self._source(round_id), 'terminal_state': state,
                'native_revision': verification['native_revision']}

    def cancel(self):
        self.cancelled.set()
        with self._connection_lock:
            conn = self.connection
            thread_id, turn_id = self.thread_id, self.turn_id
            # A connection is registered before initialize, projected-history
            # injection and turn/start. Killing it here can turn an Esc from the
            # previous/setup phase into a process failure for the first unified
            # turn. The fallback belongs to one identified native turn only.
            if conn is not None and thread_id and turn_id and self._cancel_timer is None:
                self._cancel_timer = threading.Timer(
                    3, self._cancel_expired, args=(conn, thread_id, turn_id))
                self._cancel_timer.daemon = True
                self._cancel_timer.start()
        if conn is not None and thread_id and turn_id:
            def notify():
                with self._connection_lock:
                    if self.connection is not conn or (self.thread_id, self.turn_id) != (thread_id, turn_id):
                        return
                # Pipe writes can block; keep the cancellation timer/cleanup lock free.
                try:
                    conn.request_nowait('turn/interrupt', {
                        'threadId':thread_id, 'turnId':turn_id})
                except Exception:
                    pass  # Missing terminal receipt remains reconciliation, not success.
            threading.Thread(target=notify, daemon=True).start()
        return True

    def _connected(self, conn):
        with self._connection_lock:
            self.connection = conn
            self.thread_id = self.turn_id = ''
        if self.cancelled.is_set(): self.cancel()

    def _turn_started(self, thread_id, turn_id):
        # Unified execution uses a fresh native thread, not the binding ID.
        current = (str(thread_id or ''), str(turn_id or ''))
        with self._connection_lock:
            if (self.thread_id, self.turn_id) != current and self._cancel_timer:
                self._cancel_timer.cancel()
                self._cancel_timer = None
            self.thread_id, self.turn_id = current
        if self.cancelled.is_set(): self.cancel()

    def _disconnected(self):
        with self._connection_lock:
            self.connection = None
            self.thread_id = self.turn_id = ''
            if self._cancel_timer: self._cancel_timer.cancel()
            self._cancel_timer = None

    def _cancel_expired(self, conn, thread_id, turn_id):
        with self._connection_lock:
            if (self.connection is not conn
                    or (self.thread_id, self.turn_id) != (thread_id, turn_id)):
                return
            # run_turn owns closing readers; only signal the process group still
            # serving the exact native turn for which this timer was armed.
            try: os.killpg(conn.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError): pass

    def _source(self, entry_id):
        engine, session_id = self.connector.engine, self.connector.native_session_id
        return {'kind':'native_round','engine':engine,'session_id':session_id,
                'entry_id':entry_id,'source_key':f'{engine}:{session_id}:{entry_id}'}


class ClaudeRunner(ReceiptRunner):
    """Run Claude Code through Agent SDK's SessionStore bridge.

    Authentication and inference remain in the installed Claude Code process;
    the SDK supplies the custom private persistence boundary.
    """
    def __init__(self, connector, *, on_event=None, on_permission=None, on_stall=None,
                 timeout=900, env=None, options=None, system_text='', on_process=None,
                 user_frame=None, runtime_roots=None):
        self.connector = connector
        self.binding = connector.binding
        self.on_event = on_event
        self.on_permission = on_permission
        self.on_stall = on_stall
        self.timeout = timeout
        self.env = dict(env or {})
        self.options = dict(options or {})
        self.system_text = str(system_text or '')
        self.on_process = on_process
        self.user_frame = user_frame
        self.runtime_roots = list(runtime_roots or [])
        self.process = None
        self.cancelled = threading.Event()
        self.receipts = SourceReceipts(connector.store_root if hasattr(connector, 'store_root')
                                       else os.path.dirname(os.path.dirname(connector.native.root)))
        self.bridge = bridge_path('claude_runner.mjs')

    def prepare_input(self):
        if self.connector.connector_version != CLAUDE_CODEC_VERSION:
            raise Conflict('Claude Bash cwd 策略尚未验证当前 Connector 版本')
        from .image_transport import ClaudeImageTransport
        if not hasattr(self, 'image_transport'):
            self.image_transport = ClaudeImageTransport(self.connector.store_root,
                self.connector.binding['conversation_id'])
        self.user_frame = self.image_transport.frame(self.user_frame)
        # Private legacy stores cannot be rewritten: reject oversized history
        # until normal unified-storage promotion supplies a disposable view.
        entries = self.image_transport.entries(self.connector.native.load(), transform=False)
        self.image_transport.validate_request(entries, self.user_frame,
            system=self.system_text, grounding=self.options.get('_grounding', ''))

    def run(self, *, prompt, round_id, on_dispatched=None,
            on_native_locator=None, **_kwargs):
        if not isinstance(prompt, str):
            raise InvalidState('Claude Shepherd prompt 必须是文本')
        self.prepare_input()
        self.connector._available()
        self.connector.native.initialize()
        system_prompt = static_system_prompt(self.system_text)
        if self.binding.get('prompt_snapshot_policy'):
            if self.connector.native.load():
                system_prompt = self.connector.prompt_snapshot_text()
            self.connector.ensure_prompt_snapshot(system_prompt)
            system_prompt = self.connector.prompt_snapshot_text()
        grounding_text = str(self.options.get('_grounding') or '')
        grounding_uuid = str(uuid.uuid4()) if grounding_text else ''
        grounding_block = (f'<system-reminder data-desktron-grounding="{grounding_uuid}">\n'
                           f'{grounding_text}\n</system-reminder>' if grounding_uuid else '')
        grounding_sha256 = (hashlib.sha256(grounding_block.encode()).hexdigest()
                            if grounding_block else '')
        dispatch_id = str(uuid.uuid4())
        payload = {
            'dispatchId': dispatch_id,
            'sdkRoot': self.connector.sdk_root,
            'dbPath': self.connector.native.path,
            'sessionId': self.connector.native_session_id,
            'cwd': self.connector.native_cwd,
            'claudeBinary': self.connector.claude_binary,
            'prompt': prompt,
            'userFrame': self.user_frame,
            'groundingText': grounding_text,
            'groundingUuid': grounding_uuid,
            'permissionMode': str(self.options.get('mode') or ('default' if self.on_permission else 'dontAsk')),
            'interactivePermissions': bool(self.on_permission),
            'model': str(self.options.get('model') or ''),
            'effort': str(self.options.get('effort') or ''),
            'systemPrompt': system_prompt,
            'promptSnapshotPolicy': str(self.binding.get('prompt_snapshot_policy') or ''),
            'bridgeProtocolVersion': CLAUDE_BRIDGE_PROTOCOL_VERSION,
            'bashCwdPolicy': {'codec':CLAUDE_CODEC_VERSION, 'mode':'reset_each_call'},
            'protectedPaths': list(MACOS_AGENT_PROTECTED_DIRS),
            'allowedPaths': list(self.runtime_roots),
            # Retry-After waits stay inside the one native request. The bridge
            # interrupts that request after one hour; it never submits a second
            # Shepherd Turn from outside Claude's own retry loop.
            'retryBudgetMs': 60 * 60 * 1000,
            'env': self.env or None,
        }
        fd, path = tempfile.mkstemp(prefix='.shepherd-claude-', suffix='.json',
                                    dir=self.connector.native.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False)
                stream.flush(); os.fsync(stream.fileno())
            budget = runner_timeouts(self.options, idle_timeout=self.timeout)
            self.process = process = NativeProcess(
                [self.connector.node_binary, self.bridge, path],
                cwd=self.connector.native_cwd, env=clean_claude_parent_env(os.environ),
                timeout=budget['idle_timeout'], hard_timeout=budget['hard_timeout'],
                operation='compact' if prompt == '/compact' else 'turn',
                cancel_frame={'type':'cancel'}, on_process=self.on_process,
                on_stall=self.on_stall)
            process.begin_claude_startup()
            terminal = None
            try:
                if self.cancelled.is_set(): process.cancel()
                for row in process.events():
                    if row.get('type') == 'event':
                        if self.on_event: self.on_event(row.get('event') or {})
                    elif row.get('type') == 'retry_budget_expired':
                        if self.on_event:
                            self.on_event({'type':'system', 'subtype':'retry_budget_expired',
                                'retry_budget_ms':int(row.get('retryBudgetMs') or 0)})
                    elif row.get('type') == 'prepared':
                        protocol = row.get('bridgeProtocolVersion', 1)
                        if protocol not in (1, CLAUDE_BRIDGE_PROTOCOL_VERSION):
                            raise Conflict('Claude Shepherd Bridge 协议不兼容，请重启 Desk')
                        if not callable(on_dispatched):
                            raise InvalidState('Claude Shepherd 缺少持久化派发闸')
                        locator = {'start_seq': int(row.get('startSeq')),
                                   'native_revision': str(row.get('nativeRevision') or '')}
                        if row.get('groundingUuid'):
                            if str(row['groundingUuid']) != grounding_uuid:
                                raise Conflict('Claude Grounding 派发标识不匹配')
                            locator.update(grounding_uuid=grounding_uuid,
                                           grounding_sha256=grounding_sha256)
                        on_dispatched(process=process.proc, native_locator=locator)
                        process.begin_claude_response(dispatch_id)
                        process.send({'type':'start'})
                    elif row.get('type') == 'permission':
                        decision = self.on_permission(row) if self.on_permission else {'allow':False}
                        if not isinstance(decision, dict): decision = {'allow':bool(decision)}
                        process.send({'type':'permission_result','id':row.get('id'),
                            'allow':bool(decision.get('allow')),
                            'updatedInput':decision.get('updatedInput'),
                            'message':decision.get('message') or ''})
                    elif row.get('type') == 'result':
                        terminal = row
            finally:
                process.close()
            if terminal is None:
                raise Conflict('Claude Shepherd 执行失败')
            locator = {'start_seq': int(terminal['startSeq']),
                       'projection_version':2,
                       'end_seq': int(terminal['endSeq']),
                       'terminal_uuid': str(terminal.get('terminalUuid') or ''),
                       **({'grounding_uuid':grounding_uuid,
                           'grounding_sha256':grounding_sha256}
                          if grounding_uuid else {}),
                       'terminal_state':terminal['terminalState'],
                       'native_revision':terminal['nativeRevision']}
            self.receipts.write(self.binding, round_id, locator)
            return {'source': self._source(round_id),
                    'terminal_state': terminal['terminalState'],
                    'native_revision': terminal['nativeRevision']}
        finally:
            try: os.unlink(path)
            except FileNotFoundError: pass

    def cancel(self):
        self.cancelled.set()
        if self.process: self.process.cancel()
        return True

    def _source(self, entry_id):
        return {'kind':'native_round','engine':'claude',
                'session_id':self.connector.native_session_id,'entry_id':entry_id,
                'source_key':f'claude:{self.connector.native_session_id}:{entry_id}'}
