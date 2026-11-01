"""Native loops with ephemeral stores and host-owned durable event capture."""
import copy
import hashlib
import json
import os
import oscompat
from pathlib import Path
import re
import shutil
import shlex
import stat
import threading
import uuid

import cli_path

from .store import Conflict, InvalidState
from .coordinator import digest
from .context_budget import IMAGE_TOKEN_RESERVE, estimate_context_tokens
from .runners import ReceiptRunner, clean_claude_parent_env, CodexFamilyRunner
from .runner_process import NativeProcess
from .execution_budget import runner_timeouts
from .source_receipts import SourceReceipts
from .claude_codec import static_system_prompt
from .claude_bridge_snapshot import bridge_path
from .execution_scratch import ExecutionScratch, sweep
from .phase_timing import phase_call
from protected_paths import MACOS_AGENT_PROTECTED_DIRS


def _resident_codex_home(connector):
    return (Path(connector.store_root) / 'resident' / 'codex-family' /
            connector.engine / connector.binding['binding_id'] / 'home')


def _resident_claude_home(connector):
    return (Path(connector.store_root) / 'resident' / 'claude' /
            connector.binding['binding_id'] / 'home')


def _claude_project_resources_digest(cwd):
    """Fingerprint project resources loaded once when a resident query starts."""
    root = Path(cwd)
    selected = [root / 'CLAUDE.md', root / '.claude' / 'settings.json']
    for folder in (root / '.claude' / 'skills', root / '.claude' / 'commands'):
        try:
            selected.extend(path for path in folder.rglob('*') if path.is_file())
        except OSError:
            continue
    value = hashlib.sha256()
    for path in sorted(set(selected), key=lambda item: os.fspath(item))[:4096]:
        try:
            stat = path.stat()
            value.update(os.fspath(path.relative_to(root)).encode('utf-8', 'surrogateescape'))
            value.update(str(stat.st_size).encode('ascii'))
            if stat.st_size <= 2 * 1024 * 1024:
                value.update(path.read_bytes())
            else:
                value.update(str(stat.st_mtime_ns).encode('ascii'))
        except (OSError, ValueError):
            continue
    return value.hexdigest()


def _refresh_claude_subscription(env, binary):
    """Refresh the keychain OAuth token before the SDK copies it into a resume dir.

    The SDK strips refreshToken from that copy, so a stale keychain token turns
    into a first-turn 401 (see claude_auth.ensure_fresh_subscription). Test
    doubles set DESK_SHEPHERD_CLAUDE_BINARY and must never touch the keychain.
    """
    if env.get('DESK_SHEPHERD_CLAUDE_BINARY'):return 'skipped'
    try:
        import claude_auth
        return claude_auth.ensure_fresh_subscription(binary,env={**os.environ,**env})
    except Exception:
        return 'failed'


def _resident_writer_lock(connector):
    return str(Path(connector.store_root) / 'resident' / 'writer-locks' /
               (connector.binding['binding_id'] + '.lock'))


def _clear_private_tree(path, store_root):
    path=Path(path);root=Path(store_root)
    real_root=os.path.realpath(root)
    # The deleted object is HOME's parent, not HOME. Both must remain strictly
    # below the store; accepting root/home would otherwise delete the store.
    real_parent=os.path.realpath(path.parent)
    if (real_parent == real_root or
            os.path.commonpath((real_parent,real_root)) != real_root or
            os.path.commonpath((os.path.realpath(path),real_root)) != real_root):
        raise Conflict('Shepherd 常驻 HOME 越过统一存储目录')
    if os.path.lexists(root) and root.is_symlink():
        raise Conflict('Shepherd 常驻 HOME 不能经过符号链接')
    current=root
    for part in path.relative_to(root).parts:
        current=current/part
        if os.path.lexists(current) and current.is_symlink():
            raise Conflict('Shepherd 常驻 HOME 不能经过符号链接')
    if path.parent.exists():
        shutil.rmtree(path.parent, onerror=_remove_private_readonly_file)


def _remove_private_readonly_file(function, path, exc_info):
    """Retry only Windows unlink of a private, singly-linked readonly file.

    Git objects in a disposable HOME can be readonly. Do not suppress access
    denial, sharing violations, directory errors or redirected/multiply-linked
    files; their original failure must still block host creation.
    """
    error = exc_info[1]
    if (not oscompat.IS_WINDOWS or function is not os.unlink
            or not isinstance(error, PermissionError)
            or getattr(error, 'winerror', None) != 5):
        raise error
    info = os.lstat(path)
    attributes = getattr(info, 'st_file_attributes', 0)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not attributes & stat.FILE_ATTRIBUTE_READONLY):
        raise error
    os.chmod(path, info.st_mode | stat.S_IWRITE)
    function(path)


class UnifiedRunner(ReceiptRunner):
    def __init__(self, connector, *, on_event=None, on_process=None, on_stall=None,
                 on_review=None, on_runtime=None, options=None, system_text='', env=None, timeout=900, command=None,
                 user_frame=None, on_permission=None, on_approval=None,
                 runtime_roots=None, image_paths=None):
        self.connector=connector; self.binding=connector.binding
        self._project_event=on_event; self.on_event=self._observe_event
        self.on_process=on_process; self.on_stall=on_stall
        self.on_review=on_review; self.on_runtime=on_runtime
        self.options=dict(options or {})
        self.static_system_text=str(system_text or '')
        from .history_prompt import history_prompt
        from prompt_locale import detect
        self.history_hint=history_prompt(connector, detect(system_text))
        self.system_text='\n\n'.join(x for x in (system_text,self.history_hint) if x)
        if connector.engine == 'pi':
            from pi_extensions import without_legacy_web_secrets
            self.env=without_legacy_web_secrets(env or os.environ)
        else:
            self.env=dict(env or os.environ)
        self.timeout=timeout
        self.command=command; self.user_frame=user_frame
        def guarded(callback):
            def invoke(*args):
                self.context_tracker.progress=True
                return callback(*args)
            return invoke if callback else None
        self.on_permission=guarded(on_permission); self.on_approval=guarded(on_approval)
        self.runtime_roots=runtime_roots; self.image_paths=image_paths
        self.process=None; self.controller=None
        self.cancelled=threading.Event()
        self.receipts=SourceReceipts(connector.store_root)
        self.checkpoint=None; self.degraded_context=None
        self.operation='turn'
        from .context_admission import RejectionTracker
        self.context_tracker=RejectionTracker(connector.engine)
        self.context_retryable=False
        self.context_retry_error=''
        self.on_context_retry=None
        self._context_retry_pending=False
        self.context_retries=0
        self._context_dispatched=False

    def _timed(self, stage, function, *args, **kwargs):
        return phase_call(self, stage, function, *args, **kwargs)

    def prepare_input(self):
        if self.connector.engine == 'claude':
            self._prepare_claude_input()

    def _prepare_claude_input(self):
        from .image_transport import ClaudeImageTransport
        if not hasattr(self, 'image_transport'):
            self.image_transport = ClaudeImageTransport(self.connector.store_root,
                self.connector.binding['conversation_id'])
        self.user_frame = self.image_transport.frame(self.user_frame)
        self.image_transport.validate_request([], self.user_frame,
            system=self.system_text, grounding=self.options.get('_grounding', ''))

    def _claude_execution_entries(self, entries, prompt):
        self._prepare_claude_input()
        # Over budget, older history images are compressed or omitted in this
        # disposable view only; current-turn images and the archive are unchanged.
        return self.image_transport.fit_entries(entries, self.user_frame, prompt=prompt,
            system=self.system_text, grounding=self.options.get('_grounding', ''))

    def _observe_event(self, event, *args):
        if self._context_dispatched:self.context_tracker.observe(event,*args)
        if self._project_event:self._project_event(event,*args)

    def _review_enabled(self):
        return (self.operation == 'turn' and self.connector.engine == 'pi'
                and bool(self.options.get('_execution_review')))

    def _review_snapshot(self, snapshot):
        if callable(self.on_review) and isinstance(snapshot, dict):
            self.on_review(dict(snapshot))

    def _review_due(self, snapshot):
        self._review_snapshot(snapshot)
        prompt = self.process.review_prompt()
        if not prompt:
            return
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        if self._context_dispatched:
            self.connector.archive.append(self.binding, self.round_id,
                {'type':'runtime', 'execution_review_prompt_sha256':prompt_hash})
        request_id = 'execution-review-' + uuid.uuid4().hex
        self._review_request = request_id
        self.process.send({'id':request_id, 'type':'steer', 'message':prompt})
        current = self.process.review_snapshot()
        if current is not None:self._review_snapshot(current)

    def _review_response(self, event):
        request_id = getattr(self, '_review_request', '')
        if not request_id or not isinstance(event, dict) or event.get('type') != 'response':
            return False
        if event.get('id') != request_id and event.get('command') != 'steer':
            return False
        self._review_request = ''
        if (event.get('id') == request_id and event.get('command') == 'steer'
                and event.get('success') is True):
            self._review_run_started = False
            self._review_snapshot(self.process.accept_review())
            return True
        self._review_snapshot(self.process.reject_review(
            str(event.get('error') or 'Pi steer rejected')))
        raise Conflict('Shepherd 长任务复核的 Pi steering 未被原生端确认')

    def _archive_workspace_routing_retry(self, params, ids):
        """Persist bounded routing diagnostics before the retry can hide its failed turn."""
        native_error=params.get('native_error') if isinstance(params.get('native_error'),dict) else {}
        self.connector.archive.append(self.binding,self.round_id,{'type':'runtime',
            'workspace_routing_retry':{
                'attempt':int(params.get('attempt') or 0),
                'max':int(params.get('max') or 0),
                'delay_ms':int(params.get('delay_ms') or 0),
                'signal':str(params.get('signal') or '')[:100],
                'cwd':self.connector.native_cwd,
                'runtime_workspace_roots':list(self.runtime_roots or []),
                'thread_id':ids.get('thread_id') or '',
                'turn_id':ids.get('turn_id') or '',
                'native_error':copy.deepcopy(native_error)}})

    def system_prompt_hash(self):
        """Hash the final system prefix without exposing it to the trace."""
        if self.connector.engine == 'pi' and self.command:
            from pi_extensions import append_system_prompt_if_missing, appended_system_prompts
            args=append_system_prompt_if_missing(self.command,self.static_system_text)
            args=append_system_prompt_if_missing(args,self.history_hint)
            return digest('\n\n'.join(appended_system_prompts(args)))
        return digest(self.system_text)

    def prepare_context_retry(self):
        # The Harness calls this only after the failed source is verified/committed.
        allowed=self.context_retryable and not self.cancelled.is_set() and self.operation!='compact'
        self._context_retry_pending=allowed
        return allowed

    def _verify_fallback_receipt(self, receipt):
        expected=getattr(self,'fallback_receipt',None)
        if expected and receipt!=expected:
            raise Conflict('降级上下文原生回读校验失败，未派发')

    def _source(self, turn_id): return self.connector._ref(turn_id)

    def cancel(self):
        self.cancelled.set()
        if self.process: self.process.cancel()
        if self.controller: self.controller.cancel()
        return True

    def run(self, *, prompt, round_id, expected_offset, on_dispatched=None,
            on_native_locator=None, attempt_id='', **_kwargs):
        if not isinstance(prompt,str) or not callable(on_dispatched):
            raise InvalidState('Unified runner requires text and durable dispatch fence')
        self._timed('runner.verify', self.connector.verify)
        self._timed('runner.prepare_input', self.prepare_input)
        from .context_admission import RejectionTracker, FALLBACK_HISTORY_TOKENS
        self.context_tracker=RejectionTracker(self.connector.engine)
        self.context_retryable=False
        self._context_dispatched=False
        self.checkpoint=None
        self.history_recovery=None
        window=runner_timeouts(self.options)['context_window']
        key, checkpoint, rounds=self._timed('runner.context', self.connector.context, {**self.options,
            '_context_window':window,
            '_context_admission':True,
            '_history_hint':self.history_hint,
            '_history_hint_in_system':True,
            '_system_hash':self.options.get('_checkpoint_system_hash') or digest(self.system_text),
            # All engines retain native admission when the catalog/config has
            # no window. Unknown capacity is not evidence of an over-budget
            # request; bounded history loading and rejection recovery still apply.
            '_require_context_budget':window>0,
            '_current_tokens':estimate_context_tokens({'system':self.system_text,
                'input':self.user_frame if self.user_frame is not None else prompt,
                'grounding':('' if self.options.get('_context_carrier') == 'system'
                             else self.options.get('_grounding',''))}) +
                (len(self.image_paths or []) * IMAGE_TOKEN_RESERVE
                 if self.connector.engine == 'codex' else 0)})
        native=(checkpoint or {}).get('payload',{}).get('native')
        degraded=(native.get('degraded_context')
                  if isinstance(native,dict) and isinstance(native.get('degraded_context'),dict)
                  else None)
        self.degraded_context=copy.deepcopy(degraded)
        from .context_fallback import text_receipt
        self.fallback_receipt=text_receipt(checkpoint)
        portable_id=(native.get('portable_checkpoint_id')
                     if isinstance(native,dict) else '')
        self.round_id=round_id; self.expected_offset=expected_offset
        self.attempt_id=str(attempt_id or '')
        self.operation = 'compact' if prompt == '/compact' else 'turn'
        def dispatch(**kwargs):
            # Create recovery evidence BEFORE marking the request dispatched.
            # Claude's native anchor is committed in the same transaction as
            # runtime metadata; other engines retain the legacy runtime-only row.
            native_anchor=kwargs.pop('native_anchor',None)
            if self.connector.engine=='claude' and native_anchor is None:
                raise InvalidState('Unified Claude dispatch requires native anchor')
            grounding_runtime=None
            if native_anchor is not None and native_anchor.get('grounding_uuid'):
                grounding=str(self.options.get('_grounding') or '')
                marker=(f'<system-reminder data-desktron-grounding="{native_anchor["grounding_uuid"]}">\n'
                        +grounding+'\n</system-reminder>')
                grounding_runtime={'type':'runtime',
                    'grounding_uuid':native_anchor['grounding_uuid'],
                    'grounding_sha256':hashlib.sha256(marker.encode()).hexdigest()}
            self._timed('runner.predispatch_archive', self.connector.archive.begin_predispatch, self.binding,round_id,{
                'type':'runtime','model':self.options.get('model',''),
                'effort':self.options.get('effort',''),'engine':self.connector.engine},
                native_anchor=native_anchor,grounding_runtime=grounding_runtime,
                image_transport=(self.image_transport.receipt()
                    if self.connector.engine == 'claude' and self.image_transport.derivations else None),
                attempt_id=self.attempt_id if native_anchor is not None else None)
            try:
                on_dispatched(**{**kwargs, 'native_locator':{'turn_id':round_id}})
            except BaseException:
                # The model request has not crossed the Coordinator's durable
                # dispatch fence. Preserve the anchor as abandoned evidence and
                # release the uncommitted sequence number for a later binding.
                try:
                    self.connector.archive.abandon_predispatch(self.binding,round_id,
                        attempt_id=self.attempt_id if native_anchor is not None else None)
                except BaseException as abandon_error:
                    error=Conflict('Shepherd pre-dispatch evidence cleanup failed')
                    error.preserve_reserved=True
                    raise error from abandon_error
                raise
            # Keep the frozen pre-dispatch anchor schema unchanged, including
            # abandon/recovery on older builds. This independent diagnostic is
            # durable before the bridge ACK releases the actual native request.
            self.connector.archive.append(self.binding,round_id,{'type':'runtime',
                'context_admission':dict(getattr(self.connector,'admission',{}))})
            self._context_dispatched=True
            if self._context_retry_pending:
                self._context_retry_pending=False
                self.context_retries+=1
                if self.on_context_retry:self.on_context_retry()
        self._resident_candidate=None
        self._codex_resident_candidate=None
        if degraded is not None:
            # A bounded fallback replaces the old native generation. Do not let
            # a resident host silently retain the exact context that admission
            # just proved too large or previously rejected.
            from .resident_process import drop
            from codex_appserver import drop_session
            self._timed('runner.resident_drop', drop, self.binding['binding_id'])
            self._timed('runner.resident_drop', drop_session, 'shepherd:'+self.binding['binding_id'])
        unresolved_scope={
            'engine':self.connector.engine,'cwd':self.connector.native_cwd,
            'model':self.options.get('model',''),'provider':self.options.get('provider',''),
            'unresolved':True}
        from .tool_call_ids import history_projection_version
        projection_version=history_projection_version(self.connector.engine)
        if projection_version:
            unresolved_scope['history_projection']=projection_version
        self._resident_context_key=key or digest(unresolved_scope)
        try:
            # Scratch belongs only to this invocation, not to the user's ordinary sessions.
            self._timed('runner.scratch_sweep', sweep, self.connector.store_root)
            from contextlib import ExitStack
            with ExitStack() as scratch_stack:
                execution=self._timed('runner.scratch_open', scratch_stack.enter_context,
                    ExecutionScratch(self.connector.store_root,self.binding,round_id))
                self.execution=execution
                self.directory=execution.path
                if self.cancelled.is_set(): raise Conflict('Unified turn cancelled before dispatch')
                if self.connector.engine=='claude':
                    terminal=self._claude(prompt,rounds,checkpoint,dispatch,on_native_locator)
                elif self.connector.engine=='pi':
                    terminal=self._pi(prompt,rounds,checkpoint,dispatch,on_native_locator)
                else:
                    terminal=self._codex(prompt,rounds,checkpoint,dispatch,on_native_locator)
                admission=getattr(self.connector,'admission',{})
                history_tokens=int(admission.get('history_tokens') or 0)
                if (terminal=='failed_recoverable' and self.context_tracker.rejected
                        and history_tokens>0 and not self.cancelled.is_set()):
                    self.connector.archive.append(self.binding,round_id,{'type':'runtime',
                        # Keep the frozen receipt readable by older versions. The
                        # versioned detail is sealed in the SAME source transaction.
                        'context_rejection':{'context_key':admission['context_key'],
                            'history_tokens':min(FALLBACK_HISTORY_TOKENS,history_tokens//2)},
                        'context_rejection_details':{'version':1,
                            'reason':self.context_tracker.rejection_reason,
                            'history_tokens_before':history_tokens}})
                    self.context_retryable=(history_tokens>0 and
                        self.context_tracker.retryable(terminal,self.cancelled.is_set()))
                saved_context = ({'key':key or 'unresolved:'+round_id,
                                  'through_seq':expected_offset,'native':self.checkpoint}
                                 if self.checkpoint is not None and terminal=='completed' else None)
                # Seal source and working context together before disposable native
                # storage is removed. Commit-only recovery can then adopt both.
                verified=self.connector.archive.finish_verified(
                    self.binding,round_id,terminal,working_context=saved_context)
                revision=verified['revision']
                if saved_context is not None:
                    execution.retain_context(False)
                if self._resident_candidate is not None:
                    from .resident_process import drop, release, rotate_after_commit
                    process=self._resident_candidate
                    if process.proc.poll() is None:
                        execution.release_process(process.proc)
                        if not release(self.binding['binding_id'],process,expected_offset):
                            raise Conflict('Shepherd 常驻进程提交身份已经失效')
                        # A native checkpoint is the natural generation boundary:
                        # retaining the pre-compaction SDK heap would keep the very
                        # history the checkpoint made replaceable. Claude additionally
                        # rotates an oversized idle host after a committed turn. Both
                        # paths evict only a cache; the next turn cold-starts from the
                        # atomic Working Context saved above.
                        if rotate_after_commit(self.binding['binding_id'],process,
                                engine=self.connector.engine,checkpoint=self.checkpoint is not None):
                            self._resident_candidate=None
                    else:
                        # The source and working context are already sealed. A host
                        # that exits after its terminal is only a lost cache; leave
                        # its recorded dead child attached to scratch for safe GC.
                        drop(self.binding['binding_id'],process)
                        self._resident_candidate=None
                    self.process=None
                if self._codex_resident_candidate is not None:
                    from codex_appserver import commit_session
                    session_id,connection=self._codex_resident_candidate
                    if connection.proc.poll() is None:
                        execution.release_process(connection.proc)
                    if not commit_session(session_id,connection,expected_offset):
                        raise Conflict('Shepherd Codex-family 常驻提交身份已经失效')
            self.receipts.write(self.binding,round_id,
                                self.connector.terminal_locator(
                                    round_id,terminal,revision,verified['source']))
            result={'source':self._source(round_id),'terminal_state':terminal,
                    'native_revision':revision}
            if degraded is not None:
                result['degraded_context']=copy.deepcopy(degraded)
            return result
        except BaseException:
            self.discard_resident()
            raise

    def discard_resident(self):
        process=getattr(self,'_resident_candidate',None)
        if process is not None:
            self._resident_candidate=None
            self.process=None
            from .resident_process import drop
            drop(self.binding['binding_id'],process)
        candidate=getattr(self,'_codex_resident_candidate',None)
        if candidate is not None:
            self._codex_resident_candidate=None
            from codex_appserver import drop_session_connection
            drop_session_connection(*candidate)

    def confirm_resident(self):
        """Forget rollback ownership after the Manifest commit succeeds."""
        self._resident_candidate=None
        self._codex_resident_candidate=None

    def _capture(self, kind, entries):
        # Durable entries also gate replay if a native stream omits progress events.
        for entry in entries if self._context_dispatched else []:
            if kind=='pi_entry' and entry.get('type')=='message':
                self.context_tracker.observe({'type':'message_end','message':entry.get('message') or {}})
                if (entry.get('message') or {}).get('role') in ('toolResult','bashExecution'):
                    self.context_tracker.progress=True
            elif kind=='claude_entry':
                self.context_tracker.observe(entry)
            elif kind=='codex_item':
                self.context_tracker.observe('rawResponseItem/completed',{'item':entry})
        self.connector.archive.append_many(self.binding,self.round_id,
            ({'type':kind,'value':entry} for entry in entries))

    def _payload(self, value):
        path=self.directory/'bootstrap.json'
        with open(path,'x',encoding='utf-8') as stream:
            os.chmod(path,0o600); json.dump(value,stream,ensure_ascii=False)
            stream.flush(); os.fsync(stream.fileno())
        return str(path)

    def _native_process(self, command, cancel_frame, env=None):
        budget=runner_timeouts(self.options,idle_timeout=self.timeout)
        self.execution.spawning()
        review = self._review_enabled()
        self.process=NativeProcess(command,cwd=self.connector.native_cwd,env=env or self.env,
            timeout=budget['idle_timeout'],hard_timeout=budget['hard_timeout'],
            operation=self.operation,
            cancel_frame=cancel_frame,on_process=self._registered_process,
            on_stall=self.on_stall, on_review=self._review_due if review else None,
            review_enabled=review)
        if self.cancelled.is_set():self.process.cancel()
        return self.process

    def _registered_process(self, process):
        self.execution.register(process)
        if self.on_process: self.on_process(process)

    def _claude_native_anchor(self,row,expected_protocol=None):
        if (not isinstance(row.get('startSeq'),int) or isinstance(row.get('startSeq'),bool)
                or row.get('startSeq') < 0 or row.get('storeSize') != row['startSeq']):
            raise Conflict('Unified Claude native store position mismatch')
        revision=row.get('nativeRevision');version=row.get('claudeVersion')
        protocol=row.get('bridgeProtocolVersion');parent=row.get('parentUuid') or ''
        try:
            if parent and str(uuid.UUID(parent)) != parent.lower():raise ValueError
        except (AttributeError,TypeError,ValueError):
            raise Conflict('Unified Claude native parent UUID mismatch')
        if (not isinstance(revision,str) or not re.fullmatch(r'[0-9a-f]{64}',revision)
                or not isinstance(protocol,int) or isinstance(protocol,bool)
                or (expected_protocol is not None and protocol!=expected_protocol)
                or not isinstance(version,str)
                or not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?',version)):
            raise Conflict('Unified Claude native anchor mismatch')
        anchor={'type':'native_anchor','attempt_id':self.attempt_id,
            'parent_uuid':parent,'store_revision':revision,'store_size':row['storeSize'],
            'bridge_protocol':protocol,'claude_version':version}
        if row.get('groundingUuid'):
            anchor['grounding_uuid']=str(row['groundingUuid'])
        return anchor

    @staticmethod
    def _claude_archive_batch(row):
        entries=row.get('entries');payloads=row.get('entryPayloads')
        if (not isinstance(entries,list) or not isinstance(payloads,list)
                or len(entries)!=len(payloads) or any(not isinstance(raw,str) for raw in payloads)):
            raise Conflict('Unified Claude archive payload receipt mismatch')
        try:decoded=[json.loads(raw) for raw in payloads]
        except (TypeError,ValueError):
            raise Conflict('Unified Claude archive payload receipt mismatch')
        if decoded!=entries:raise Conflict('Unified Claude archive payload receipt mismatch')
        return entries,payloads

    @staticmethod
    def _claude_transition_revision(base_revision,payloads):
        digest=hashlib.sha256()
        digest.update(b'claude-store-transition-v1\0')
        digest.update(base_revision.encode('ascii'));digest.update(b'\0[')
        for index,payload in enumerate(payloads):
            if index:digest.update(b',')
            digest.update(payload.encode('utf-8'))
        digest.update(b']')
        return digest.hexdigest()

    @staticmethod
    def _claude_terminal_receipt(row,anchor,grounding_uuid,captured_payloads):
        if (anchor is None or row.get('startSeq')!=anchor['store_size']
                or not isinstance(row.get('endSeq'),int) or isinstance(row.get('endSeq'),bool)
                or row['endSeq'] != anchor['store_size'] + len(captured_payloads)
                or str(row.get('groundingUuid') or '')!=grounding_uuid
                or row.get('terminalState') not in
                    ('completed','failed_recoverable','interrupted_confirmed')
                or not isinstance(row.get('nativeRevision'),str)
                or not re.fullmatch(r'[0-9a-f]{64}',row['nativeRevision'])
                or row.get('transitionRevision') != UnifiedRunner._claude_transition_revision(
                    anchor['store_revision'],captured_payloads)):
            raise Conflict('Unified Claude terminal receipt mismatch')
        return row['terminalState']

    def _claude_dispatch_observe(self, row):
        expected = getattr(self, '_claude_dispatch_id', None)
        if not expected or row.get('dispatch_id') != expected:
            return
        stage = row.get('stage')
        if stage not in ('start_received', 'store_begin', 'store_ready', 'input_queued',
                         'input_consumed', 'first_native_event', 'stderr_auth',
                         'stderr_network', 'stderr_rate_limit', 'stderr_other',
                         'provider_response_started', 'provider_retry'):
            return
        from .phase_timing import PhaseTiming
        timing = getattr(self, 'phase_timing', None)
        if isinstance(timing, PhaseTiming):
            timing.mark('claude.' + stage)

    def _claude_retry_expired(self, row):
        expected = getattr(self, '_claude_dispatch_id', None)
        if not expected or row.get('dispatch_id') != expected:
            return
        budget = row.get('retryBudgetMs')
        if type(budget) is not int or not 0 < budget <= 3600000:
            return
        if getattr(self, '_claude_retry_reported', None) == self._claude_dispatch_id:
            return
        self._claude_retry_reported = self._claude_dispatch_id
        if self.on_event:
            self.on_event({'type':'system', 'subtype':'retry_budget_expired',
                           'retry_budget_ms':budget})

    def _claude_start(self, process):
        process.begin_claude_response(self._claude_dispatch_id)
        self._timed('claude.start_send', process.send, {'type':'start'})

    def _claude(self,prompt,rounds,checkpoint,dispatch,locate):
        self._prepare_claude_input()
        from .resident_process import ResidentProcess, acquire, drop, enabled
        if prompt=='/compact' or not enabled() or not hasattr(self,'expected_offset'):
            drop(self.binding['binding_id'])
            return self._claude_oneshot(prompt,rounds,checkpoint,dispatch,locate)
        from .claude_connector import ClaudeConnector, managed_claude_binary
        from .claude_codec import CODEC_VERSION
        self._claude_dispatch_id = str(uuid.uuid4())
        binary=self.env.get('DESK_SHEPHERD_CLAUDE_BINARY') or cli_path.which('claude') or managed_claude_binary()
        binary=os.path.abspath(binary) if binary else ''
        sdk=self.env.get('DESK_SHEPHERD_CLAUDE_SDK_ROOT') or ClaudeConnector.default_sdk_root()
        available=ClaudeConnector.availability(sdk_root=sdk,claude_binary=binary)
        if not available.get('available'):raise Conflict(available['message'])
        _refresh_claude_subscription(self.env,binary)
        grounding=('' if self.options.get('_context_carrier') == 'system'
                   else str(self.options.get('_grounding') or ''))
        grounding_uuid=str(uuid.uuid4()) if grounding else ''
        native_env=clean_claude_parent_env(os.environ)
        runtime_env=clean_claude_parent_env(self.env)
        system_prompt=static_system_prompt(self.system_text)
        permission_mode=self.options.get('mode') or ('default' if self.on_permission else 'dontAsk')
        home=_resident_claude_home(self.connector)
        host_key=digest({'context':self._resident_context_key,'sdk':sdk,'binary':binary,
            'cwd':self.connector.native_cwd,'system_prompt':system_prompt,
            'permission_mode':permission_mode,'interactive_permissions':bool(self.on_permission),
            'model':self.options.get('model',''),'effort':self.options.get('effort',''),
            'thinking_display':self.options.get('thinking_display','summarized'),
            'autocompact':self.options.get('autocompact',''),
            'env':digest(runtime_env),'codec':CODEC_VERSION,'bridge_protocol':4,
            'dispatch_wait':3,'filesystem_guard':3,'bash_mode':'native',
            'setting_sources':'project',
            'project_resources':_claude_project_resources_digest(self.connector.native_cwd),
            'allowed_paths':digest(sorted(str(path) for path in
                (getattr(self,'runtime_roots',None) or []))),
            'image_transport':self.image_transport.receipt()['policy']})
        budget=runner_timeouts(self.options,idle_timeout=self.timeout)

        def native_entries():
            from .context_fallback import native_items
            entries=copy.deepcopy(native_items(checkpoint))
            entries=[entry for entry in entries if not (entry.get('type')=='attachment'
                and (entry.get('attachment') or {}).get('type')=='prompt_snapshot'
                and entry['attachment'].get('systemPrompt') != [system_prompt])]
            for entry in entries:
                if 'sessionId' in entry:entry['sessionId']=self.connector.native_session_id
            for row in rounds:entries.extend(self.connector.encode(row))
            parent=None
            for entry in entries:
                if entry.get('type')!='system' or entry.get('subtype')!='compact_boundary':
                    entry['parentUuid']=parent
                parent=entry.get('uuid',parent)
            return self._claude_execution_entries(entries, prompt)

        # Validate the complete execution view even when a resident could be
        # reused. No host acquisition or native request precedes this check.
        seeded_entries = native_entries()
        history_sources = set(self.image_transport.entry_sources)

        def create():
            _clear_private_tree(home,self.connector.store_root)
            home.mkdir(parents=True,mode=0o700)
            temp=home/'tmp';temp.mkdir(mode=0o700)
            bootstrap={'unified':True,'bridgeProtocolVersion':4,'entries':seeded_entries,
                'fallbackReceipt':getattr(self,'fallback_receipt',None),
                'sdkRoot':sdk,'claudeBinary':binary,'dbPath':str(home/'memory'),
                'cwd':self.connector.native_cwd,'sessionId':self.connector.native_session_id,
                'systemPrompt':system_prompt,
                'bashCwdPolicy':{'codec':CODEC_VERSION,'mode':'reset_each_call'},
                'protectedPaths':list(MACOS_AGENT_PROTECTED_DIRS),
                'allowedPaths':list(getattr(self,'runtime_roots',None) or []),
                'permissionMode':permission_mode,'interactivePermissions':bool(self.on_permission),
                'model':self.options.get('model',''),'effort':self.options.get('effort',''),
                'thinkingDisplay':self.options.get('thinking_display','summarized'),
                'autocompact':self.options.get('autocompact',''),
                'env':runtime_env}
            child_env={**native_env,'TMPDIR':str(temp)}
            command=[shutil.which('node'),bridge_path('claude_resident_runner.mjs'),
                     self._payload(bootstrap)]
            self.execution.spawning()
            process=ResidentProcess(command,cwd=self.connector.native_cwd,env=child_env,
                                    cancel_frame={'type':'cancel'},close_frame={'type':'shutdown'},
                                    lock_path=_resident_writer_lock(self.connector))
            process._claude_revision=None
            process._claude_image_sources=history_sources
            return process

        process,created=acquire(self.binding['binding_id'],host_key,self.expected_offset-1,create)
        if not created and not history_sources.issubset(getattr(process, '_claude_image_sources', set())):
            # A tool may have added an oversized native image after the last
            # seed. Rebuild the disposable host, never rewrite its source.
            drop(self.binding['binding_id'], process)
            process,created=acquire(self.binding['binding_id'],host_key,self.expected_offset-1,create)
        self._resident_candidate=process
        self.execution.register(process.proc);self.process=process
        if self.on_process:self.on_process(process.proc)
        process.begin(timeout=budget['idle_timeout'],hard_timeout=budget['hard_timeout'],
                      on_stall=self.on_stall)
        process.begin_claude_startup()
        if self.cancelled.is_set():
            process.cancel();raise Conflict('Unified turn cancelled before dispatch')
        process.send({'type':'turn','payload':{'prompt':prompt,'userFrame':self.user_frame,
            'dispatchId':self._claude_dispatch_id,
            'groundingText':grounding,'groundingUuid':grounding_uuid}})
        terminal=None;native_anchor=None;captured_payloads=[]
        try:
            for row in process.events():
                kind=row.get('type')
                if kind=='archive':
                    entries,payloads=self._claude_archive_batch(row)
                    self._capture('claude_entry',entries)
                    captured_payloads.extend(payloads)
                    process.send({'type':'archive_ack','id':row['id']})
                elif kind=='prepared':
                    if row.get('bridgeProtocolVersion')!=4:
                        raise Conflict('Unified resident Claude bridge protocol mismatch')
                    # A reused host already owns the exact committed prefix. The
                    # cold-start fallback receipt belongs only to a newly seeded
                    # host and is intentionally absent from the live one.
                    if created:self._verify_fallback_receipt(row.get('fallbackReceipt'))
                    if native_anchor is not None:
                        raise Conflict('Unified resident Claude duplicate prepared receipt')
                    if str(row.get('groundingUuid') or '') != grounding_uuid:
                        raise Conflict('Unified resident Claude grounding identity mismatch')
                    native_anchor=self._claude_native_anchor(row,4)
                    if (process._claude_revision is not None
                            and row['nativeRevision'] != process._claude_revision):
                        raise Conflict('Unified resident Claude store revision continuity mismatch')
                    if self.cancelled.is_set():
                        process.cancel();raise Conflict('Unified turn cancelled before dispatch')
                    dispatch(process=process.proc,native_anchor=native_anchor)
                    if locate:locate({'turn_id':self.round_id})
                    if self.cancelled.is_set():
                        process.cancel();raise Conflict('Unified turn cancelled before start')
                    self._claude_start(process)
                elif kind=='claude_dispatch':
                    self._claude_dispatch_observe(row)
                elif kind=='retry_budget_expired':
                    self._claude_retry_expired(row)
                elif kind=='permission':
                    decision=self.on_permission(row) if self.on_permission else {'allow':False}
                    if not isinstance(decision,dict):decision={'allow':bool(decision)}
                    process.send({'type':'permission_result','id':row['id'],**decision})
                elif kind=='event':
                    if self.on_event:self.on_event(row['event'])
                elif kind=='checkpoint':self.checkpoint=row['payload']
                elif kind=='result':
                    if terminal is not None:
                        raise Conflict('Unified resident Claude duplicate terminal receipt')
                    terminal=self._claude_terminal_receipt(
                        row,native_anchor,grounding_uuid,captured_payloads)
                    process._claude_revision=row['nativeRevision']
                    break
        finally:
            process.finish(raise_timeout=True)
        if terminal is None:raise Conflict('Unified resident Claude turn has no terminal result')
        if terminal!='completed':
            drop(self.binding['binding_id'],process)
            self._resident_candidate=None;self.process=None
        return terminal

    def _claude_oneshot(self,prompt,rounds,checkpoint,dispatch,locate):
        from .claude_connector import ClaudeConnector, managed_claude_binary
        from .claude_codec import CODEC_VERSION
        self._claude_dispatch_id = str(uuid.uuid4())
        binary=self.env.get('DESK_SHEPHERD_CLAUDE_BINARY') or cli_path.which('claude') or managed_claude_binary()
        binary=os.path.abspath(binary) if binary else ''
        sdk=self.env.get('DESK_SHEPHERD_CLAUDE_SDK_ROOT') or ClaudeConnector.default_sdk_root()
        available=ClaudeConnector.availability(sdk_root=sdk,claude_binary=binary)
        if not available.get('available'): raise Conflict(available['message'])
        _refresh_claude_subscription(self.env,binary)
        from .context_fallback import native_items
        entries=copy.deepcopy(native_items(checkpoint))
        entries=[entry for entry in entries if not (entry.get('type')=='attachment'
                 and (entry.get('attachment') or {}).get('type')=='prompt_snapshot'
                 and entry['attachment'].get('systemPrompt') != [static_system_prompt(self.system_text)])]
        for entry in entries:
            if 'sessionId' in entry: entry['sessionId']=self.connector.native_session_id
        for row in rounds: entries.extend(self.connector.encode(row))
        # Link independently encoded rounds into one native import chain.
        parent=None
        for entry in entries:
            if entry.get('type')!='system' or entry.get('subtype')!='compact_boundary':
                entry['parentUuid']=parent
            parent=entry.get('uuid',parent)
        entries = self._claude_execution_entries(entries, prompt)
        grounding=('' if prompt=='/compact' or self.options.get('_context_carrier') == 'system'
                   else str(self.options.get('_grounding') or ''))
        grounding_uuid=str(uuid.uuid4()) if grounding else ''
        payload={'unified':True,'bridgeProtocolVersion':3,'entries':entries,
            'dispatchId':self._claude_dispatch_id,
            'protectedPaths':list(MACOS_AGENT_PROTECTED_DIRS),
            'allowedPaths':list(getattr(self,'runtime_roots',None) or []),
            'fallbackReceipt':getattr(self,'fallback_receipt',None),'sdkRoot':sdk,'claudeBinary':binary,
            'dbPath':str(self.directory/'memory'),'cwd':self.connector.native_cwd,
            'sessionId':self.connector.native_session_id,'prompt':prompt,'userFrame':self.user_frame,
            'groundingText':grounding,'groundingUuid':grounding_uuid,
            'systemPrompt':static_system_prompt(self.system_text),
            'bashCwdPolicy':{'codec':CODEC_VERSION,'mode':'reset_each_call'},
            'permissionMode':self.options.get('mode') or ('default' if self.on_permission else 'dontAsk'),
            'interactivePermissions':bool(self.on_permission),'model':self.options.get('model',''),
            'effort':self.options.get('effort',''),
            'autocompact':self.options.get('autocompact',''),'env':clean_claude_parent_env(self.env)}
        native_env=clean_claude_parent_env(os.environ)
        native_env['TMPDIR']=str(self.directory)
        process=self._native_process([shutil.which('node'),bridge_path('claude_runner.mjs'),
                                     self._payload(payload)],{'type':'cancel'},native_env)
        process.begin_claude_startup()
        if self.cancelled.is_set():
            process.cancel();raise Conflict('Unified turn cancelled before dispatch')
        terminal=None;native_anchor=None;captured_payloads=[]
        try:
            for row in process.events():
                kind=row.get('type')
                if kind=='archive':
                    entries,payloads=self._claude_archive_batch(row)
                    self._capture('claude_entry',entries)
                    captured_payloads.extend(payloads)
                    process.send({'type':'archive_ack','id':row['id']})
                elif kind=='prepared':
                    if row.get('bridgeProtocolVersion')!=3:
                        raise Conflict('Unified Claude bridge protocol mismatch')
                    self._verify_fallback_receipt(row.get('fallbackReceipt'))
                    if native_anchor is not None:
                        raise Conflict('Unified Claude duplicate prepared receipt')
                    if str(row.get('groundingUuid') or '') != grounding_uuid:
                        raise Conflict('Unified Claude grounding identity mismatch')
                    native_anchor=self._claude_native_anchor(row,3)
                    if self.cancelled.is_set():
                        process.cancel();raise Conflict('Unified turn cancelled before dispatch')
                    dispatch(process=process.proc,native_anchor=native_anchor)
                    if locate: locate({'turn_id':self.round_id})
                    if self.cancelled.is_set():
                        process.cancel();raise Conflict('Unified turn cancelled before start')
                    self._claude_start(process)
                elif kind=='claude_dispatch':
                    self._claude_dispatch_observe(row)
                elif kind=='retry_budget_expired':
                    self._claude_retry_expired(row)
                elif kind=='permission':
                    decision=self.on_permission(row) if self.on_permission else {'allow':False}
                    if not isinstance(decision,dict): decision={'allow':bool(decision)}
                    process.send({'type':'permission_result','id':row['id'],**decision})
                elif kind=='event':
                    if self.on_event: self.on_event(row['event'])
                elif kind=='checkpoint': self.checkpoint=row['payload']
                elif kind=='result':
                    if terminal is not None:
                        raise Conflict('Unified Claude duplicate terminal receipt')
                    terminal=self._claude_terminal_receipt(
                        row,native_anchor,grounding_uuid,captured_payloads)
        finally: process.close()
        if terminal is None: raise Conflict('Unified Claude turn has no terminal result')
        return terminal

    def _pi_ready(self, process):
        ready=next(process.events(),{})
        if (ready.get('type')!='protocol_ready'
                or ready.get('contract')!='desk-pi-execute-v1'
                or ready.get('sessionId')!=self.connector.native_session_id):
            raise Conflict('Pi 执行能力初始化未确认；消息未派发。')
        timing=getattr(self,'phase_timing',None)
        if timing is not None:timing.host(ready.get('startup_timing'))

    def _pi_fallback_ready(self, process):
        prepared=next(process.events(),{})
        if prepared.get('type')!='fallback_prepared':
            raise Conflict('Pi 降级上下文缺少原生回读凭据')
        self._verify_fallback_receipt(prepared.get('fallbackReceipt'))

    def _pi(self,prompt,rounds,checkpoint,dispatch,locate):
        from .pi_connector import pi_sdk_root, PiConnector
        from pi_extensions import (
            append_system_prompt_if_missing, enforce_prompt_isolation,
            without_legacy_web_secrets,
        )
        runtime_env=without_legacy_web_secrets(self.env)
        sdk=self._timed('pi.sdk_discovery', pi_sdk_root)
        available=self._timed('pi.sdk_probe', PiConnector.availability, sdk_root=sdk)
        if not available['available']:
            raise Conflict(available['message'])
        messages=self._timed('pi.history_encode', lambda: [
            item for row in rounds for item in self.connector.encode(row)])
        from .context_fallback import native_items
        fallback=getattr(self,'fallback_receipt',None)
        if fallback:messages=native_items(checkpoint)+messages
        payload={'id':self.connector.native_session_id,'messages':messages,'compact':prompt=='/compact',
                 'entries':None if fallback else checkpoint['payload']['native'] if checkpoint else None}
        if fallback:payload['fallbackReceipt']=fallback
        args=enforce_prompt_isolation(
            list(self.command[1:]) if self.command else ['--mode','rpc','--offline','--no-extensions'])
        if self.command:
            args=append_system_prompt_if_missing(args,self.static_system_text)
            args=append_system_prompt_if_missing(args,self.history_hint)
        if not self.command:
            from pi_extensions import provider_extension_args, web_extension_args
            args+=provider_extension_args()
            args+=web_extension_args(runtime_env)
            base=Path(__file__).parents[1]/'engine_adapters'
            for name in ('pi_gate.ts','pi_harness_loop.ts','pi_autocompact.ts'):
                args+=['--extension',str(base/name)]
            if self.options.get('model'):args+=['--model',self.options['model']]
            if self.options.get('effort'):args+=['--thinking',self.options['effort']]
            if self.system_text:args+=['--append-system-prompt',self.system_text]
        def native_command():
            # Encoding above still validates every projected round, even on a
            # resident hit. Only cold hosts consume the serialized bootstrap.
            return [shutil.which('node'),str(Path(__file__).with_name('pi_memory_rpc.mjs')),
                    sdk,self._timed('pi.bootstrap_write', self._payload, payload),*args]
        resident=False
        resident_created=False
        if prompt!='/compact':
            from .resident_process import ResidentProcess, acquire, enabled
            if enabled() and hasattr(self,'execution') and hasattr(self,'expected_offset'):
                budget=runner_timeouts(self.options,idle_timeout=self.timeout)
                host_key=digest({'sdk':sdk,'args':args,'cwd':self.connector.native_cwd,
                                 'permission':runtime_env.get('DESKTRON_PI_PERMISSION_MODE','')})
                def create():
                    command=native_command()
                    self.execution.spawning()
                    return self._timed('pi.spawn', ResidentProcess, command,cwd=self.connector.native_cwd,
                                           env=runtime_env,cancel_frame={'type':'abort'},
                                           lock_path=_resident_writer_lock(self.connector))
                process,resident_created=self._timed('pi.pool_acquire', acquire,
                    self.binding['binding_id'],host_key,self.expected_offset-1,create,
                    **({'timing':self.phase_timing} if getattr(self,'phase_timing',None) else {}))
                self._resident_candidate=process
                self._timed('pi.process_register', self.execution.register, process.proc)
                self.process=process
                if self.on_process:self.on_process(process.proc)
                process.begin(timeout=budget['idle_timeout'],hard_timeout=budget['hard_timeout'],
                              on_stall=self.on_stall,
                              on_review=self._review_due if self._review_enabled() else None,
                              review_enabled=self._review_enabled())
                resident=True
            else:
                from .resident_process import drop
                drop(self.binding['binding_id'])
        else:
            from .resident_process import drop
            drop(self.binding['binding_id'])
        if not resident:
            process=self._timed('pi.spawn', self._native_process,
                native_command(),{'type':'abort'},env=runtime_env)
        timing=getattr(self,'phase_timing',None)
        if timing is not None:timing.resident(resident, resident and not resident_created)
        if self.on_runtime:
            try:
                self.on_runtime({'resident': resident,
                                 'resident_reused': bool(resident and not resident_created)})
            except Exception:
                pass  # Diagnostic metadata cannot alter native dispatch.
        terminal=''
        settled=False
        try:
            if not resident or resident_created:
                self._timed('pi.ready_wait', self._pi_ready, process)
            if fallback and (not resident or resident_created):
                self._timed('pi.fallback_verify', self._pi_fallback_ready, process)
            dispatch(process=process.proc)
            if locate:self._timed('runner.locator', locate, {'turn_id':self.round_id})
            if prompt!='/compact':
                # Every turn restates the session threshold (0 = Pi native), so a reused
                # resident host follows the latest choice and a fresh host starts from it.
                from .pi_compaction import pi_compaction_frame
                process.send(pi_compaction_frame(self.options))
            self._timed('pi.prompt_send', process.send,
                self.user_frame or {'type':'prompt','message':prompt})
            for event in process.events():
                kind=event.get('type')
                if self._review_response(event):continue
                if kind=='archive': self._capture('pi_entry',event['entries']); continue
                if kind=='desk_compaction': continue
                if kind=='checkpoint': self.checkpoint=event['payload']; continue
                if kind=='memory_compact_done':
                    status=event.get('status','completed')
                    if prompt != '/compact' or status not in ('completed','aborted','failed'):
                        raise Conflict('Unified Pi compaction has an invalid terminal')
                    if status=='completed' and self.checkpoint is None:
                        raise Conflict('Unified Pi compaction has no committed checkpoint')
                    settled=True
                    terminal=({'aborted':'interrupted_confirmed','failed':'failed_recoverable'}
                              .get(status,'completed'))
                    process.end_input();continue
                if self.on_event:self.on_event(event)
                if kind=='message_end' and (event.get('message') or {}).get('role')=='assistant':
                    from .pi_terminal import assistant_terminal
                    terminal = assistant_terminal(event['message'])
                if kind=='agent_start' and (process.review_snapshot() or {}).get('status') == 'injected':
                    self._review_run_started = True
                if (kind=='agent_end' and getattr(self, '_review_run_started', False)
                        and (process.review_snapshot() or {}).get('status') == 'injected'):
                    self._review_run_started = False
                    self._review_snapshot(process.complete_review())
                if kind=='agent_settled':
                    settled=True
                    terminal='interrupted_confirmed' if self.cancelled.is_set() else terminal or 'completed'
                    if self.context_tracker.rejected and terminal=='completed' and not self.checkpoint:
                        terminal='failed_recoverable'
                    if resident:break
                    process.end_input()
        finally:
            if resident:process.finish()
            else:process.close()
        if not settled:raise Conflict('Unified Pi turn has no settled boundary')
        review = process.review_snapshot()
        if review is not None and review.get('status') in ('review_due', 'dispatching', 'injected', 'rejected'):
            raise Conflict('Shepherd 长任务复核未安全完成，回合保持封锁')
        if resident and terminal!='completed':
            from .resident_process import drop
            drop(self.binding['binding_id'],process)
            self._resident_candidate=None
        return terminal

    def _codex(self,prompt,rounds,checkpoint,dispatch,locate):
        from .codex_connector import CodexFamilyConnector
        native=((checkpoint or {}).get('payload') or {}).get('native')
        portable_id=native.get('portable_checkpoint_id') if isinstance(native,dict) else ''
        from codex_appserver import drop_session, drop_session_connection, run_turn
        available=CodexFamilyConnector.availability(self.connector.engine,self.connector.binary)
        if not available.get('available'):
            raise Conflict(available.get('message') or str(available))
        manual_compact = prompt == '/compact'
        session_id='shepherd:'+self.binding['binding_id']
        if manual_compact:
            drop_session(session_id)
            home=self.directory/'home';home.mkdir(mode=0o700)
            self.execution.retain_context()
            CodexFamilyConnector._seed_private_config(self.connector.engine,str(home))
        else:
            home=_resident_codex_home(self.connector)
        def clean_home(preserve_existing=False):
            from .physical_delete import deletion_guard, is_deleted
            archive_children = getattr(self, 'on_native_context_drop', None)
            if archive_children:
                archive_children()  # Takes the same fence for its own writes.
            # Do not nest the flock held by the child callback. Reacquire and
            # recheck before ALL remaining work, including has_unsealed_turn:
            # its db() can create the archive directory after a completed purge.
            # A delete between the two phases wins; no subsequent write occurs.
            root = self.connector.store_root
            sid = self.binding['conversation_id']
            with deletion_guard(root, sid):
                if is_deleted(root, sid):
                    return
                if home.exists() and (preserve_existing or
                        self.connector.archive.has_unsealed_turn(self.binding)):
                    self.connector.archive.retain_native_home(self.binding, home)
                _clear_private_tree(home,root)
        def prepare_home():
            # A crash can leave the only copy of raw rollout/blob evidence here.
            # Never let the next host erase it, even after the old host was killed.
            clean_home(preserve_existing=True)
            CodexFamilyConnector._make_private(str(home),self.connector.store_root)
            CodexFamilyConnector._seed_private_config(self.connector.engine,str(home))
        CodexFamilyConnector._make_private(
            str(Path(self.connector.store_root)/'adapter'/self.connector.engine),
            self.connector.store_root)
        env={**self.env,
             'CODEX_HOME':str(home)}
        self.controller=CodexFamilyRunner(self.connector,options=self.options)
        if self.cancelled.is_set():self.controller.cancel()
        ids={};terminal={};resident_connection=[None];raw_output=[]
        context_rebuilt = False
        from .codex_working_context import read_working_context, WorkingContextUnavailable
        def snapshot(thread_id, path):
            nonlocal context_rebuilt
            if manual_compact:self.execution.retain_context()
            if os.path.commonpath((os.path.realpath(path), os.path.realpath(home))) != os.path.realpath(home):
                raise Conflict('Unified native working context escaped private HOME')
            try:
                self.checkpoint=read_working_context(path,thread_id=thread_id)
            except WorkingContextUnavailable as exc:
                # A cache is not authority. Only a matched successful ordinary
                # turn with a fully projectable journal can continue without it.
                # Manual /compact still requires its promised native checkpoint.
                if (manual_compact or terminal.get('status') != 'completed'
                        or thread_id != ids.get('thread_id') or exc.raw is None):
                    raise
                row = self.connector.archive.read(self.binding, self.round_id)
                self.connector.terminal_locator(self.round_id, 'completed', row['revision'])
                evidence = self.connector.archive.retain_context_evidence(
                    self.binding, self.round_id, exc.raw)
                if exc.expanded_raw is not None:
                    evidence['expanded'] = self.connector.archive.retain_context_evidence(
                        self.binding, self.round_id, exc.expanded_raw)
                self.connector.archive.append(self.binding, self.round_id, {
                    'type': 'runtime', 'context_recovery': {
                        'reason': exc.reason, 'action': 'rebuild_from_verified_history',
                        'evidence': evidence}})
                self.checkpoint = None
                self.history_recovery = {'reason': exc.reason, 'evidence': evidence}
                context_rebuilt = True
            if manual_compact and not self.checkpoint.get('compacted'):
                raise Conflict('Native manual compaction has no verified replacement context')
        def compacted(evidence):
            self.connector.archive.append(self.binding,self.round_id,
                {'type':'runtime','operation':'compact','terminal_evidence':evidence})
            terminal.update(status=evidence['status'])
            self.controller._disconnected()
        def started(thread_id,turn_id):
            ids.update(thread_id=thread_id,turn_id=turn_id)
            self.controller._turn_started(thread_id,turn_id)
            if locate:locate({'turn_id':self.round_id})
        def connected(conn):
            resident_connection[0]=conn
            self.controller._connected(conn)
        def submitting(_thread):
            if manual_compact:
                ids['thread_id'] = _thread
            conn=self.controller.connection
            dispatch(process=conn.proc if conn else None)
        def event(method,params):
            if method=='turn/started' and self.cancelled.is_set():self.controller.cancel()
            if method=='turn/retry' and params.get('active') and params.get('status')=='workspace_routing':
                self._archive_workspace_routing_retry(params,ids)
            if method=='rawResponseItem/completed' and ids.get('turn_id') and params.get('turnId')==ids['turn_id']:
                item=params.get('item')
                if not isinstance(item,dict) or not isinstance(item.get('type'),str) or not item['type']:
                    raise Conflict('原生归档事件格式无效；不提交完成回合。')
                self._capture('codex_item',[item])
                if not (item['type']=='message' and item.get('role') in ('system','developer','user')):
                    raw_output.append(True)
            if method=='turn/completed' and ids.get('turn_id') and (params.get('turn') or {}).get('id')==ids['turn_id']:
                # Raw events are absent from upstream JSON Schema even with
                # --experimental. Require live evidence, never infer it from a
                # release number or silently commit a completed empty source.
                if params['turn'].get('status')=='completed' and not manual_compact and not raw_output:
                    raise Conflict('原生回合缺少输出归档事件；不提交完成回合。')
                terminal.update(params['turn'])
                self.controller._disconnected()
            if self.on_event:self.on_event(method,params)
        self.execution.spawning()
        from .codex_codec import normalize_call_ids
        history_items=normalize_call_ids(
            (checkpoint['payload']['native']['items'] if checkpoint else [])
            + [item for row in rounds for item in self.connector.encode(row)])
        def verify_history(thread_id,path):
            if os.path.commonpath((os.path.realpath(path),os.path.realpath(home)))!=os.path.realpath(home):
                raise Conflict('Unified injected history escaped private HOME')
            if not portable_id:
                if getattr(self,'fallback_receipt',None):
                    actual=read_working_context(path,thread_id=thread_id)['items']
                    texts=[''.join(block.get('text','') for block in item.get('content',[])
                           if block.get('type') in ('input_text','text'))
                           for item in actual if item.get('type')=='message' and item.get('role')=='user']
                    expected=self.fallback_receipt['text_sha256']
                    if sum(hashlib.sha256(text.encode()).hexdigest()==expected for text in texts)!=1:
                        raise Conflict('降级上下文原生回读校验失败，未派发')
                return
            expected=[item for item in history_items if str(((item.get(
                'internal_chat_message_metadata_passthrough') or {}).get('turn_id') or '')).startswith(
                    'shepherd-checkpoint:v2:'+portable_id+':')]
            marker=str(((expected[0].get('internal_chat_message_metadata_passthrough') or {})
                       .get('turn_id') or '')) if expected else ''
            actual=CodexFamilyConnector._checkpoint_batch(path,marker)
            normalized=[CodexFamilyConnector._normalized_checkpoint_item(item) for item in expected]
            if not marker or actual!=normalized:
                mismatch=next((index for index,(left,right) in enumerate(zip(normalized,actual))
                               if left!=right),min(len(normalized),len(actual)))
                def shape(items,index):
                    row=items[index] if index<len(items) else {}
                    return {'count':len(items),'index':index,'type':row.get('type'),'id':row.get('id'),
                            'metadata':row.get('internal_chat_message_metadata_passthrough')}
                raise Conflict('Unified portable checkpoint injection verification failed: '+
                    json.dumps({'expected':shape(normalized,mismatch),'actual':shape(actual,mismatch)},
                               ensure_ascii=False))
            markers=CodexFamilyConnector._markers(path)
            for row in rounds:
                encoded=self.connector.encode(row)
                if CodexFamilyConnector._round_status(markers,str(row['offset']),
                        row['entry_hash'],encoded)!='complete':
                    raise Conflict('Unified portable checkpoint tail injection verification failed')
        run_system=(self.system_text if manual_compact else
            '\n\n'.join(x for x in (self.static_system_text,self.history_hint) if x))
        try:
            returned=run_turn(self.connector.native_cwd,env,None,prompt,self.options,
                system_text=run_system,runtime_roots=self.runtime_roots,
                on_notification=event,timeout=self.timeout,
                hard_timeout=runner_timeouts(self.options,idle_timeout=self.timeout)['hard_timeout'],
                binary=self.connector.binary,label=self.connector.engine,on_approval=self.on_approval,
                image_paths=self.image_paths,on_process=self._registered_process,on_connection=connected,
                on_turn_started=started,on_turn_submitting=submitting,cancelled=self.cancelled.is_set,
                on_model_selected=lambda model:event('model/selected', {'model':model}),
                history_items=history_items,
                strict_boundary=True,on_working_context=snapshot,on_stall=self.on_stall,
                on_history_injected=verify_history if portable_id or getattr(self,'fallback_receipt',None) else None,
                **({} if manual_compact else {
                    'session_id':session_id,
                    'resident_context':self._resident_context_key,
                    'resident_through_seq':self.expected_offset-1,
                    'on_session_create':prepare_home,'on_session_drop':clean_home,
                    'session_recap':False,'writer_lock_root':self.connector.store_root}),
                **({'operation':'compact','on_compact_completed':compacted} if manual_compact else {}))
        except Exception:
            if manual_compact or terminal.get('status') not in ('failed','interrupted'):raise
        else:
            if returned!=ids.get('thread_id'):raise Conflict('Unified native thread identity mismatch')
        finally:
            self.controller._disconnected()
        state={'completed':'completed','failed':'failed_recoverable','interrupted':'interrupted_confirmed'}.get(terminal.get('status'))
        if not state:raise Conflict('Unified native turn has no matching terminal')
        connection=resident_connection[0]
        if not manual_compact and connection is not None:
            process=getattr(connection,'proc',None)
            if state=='completed' and not context_rebuilt and process is not None and process.poll() is None:
                self._codex_resident_candidate=(session_id,connection)
            else:
                drop_session_connection(session_id,connection)
        return state
