"""User-visible Shepherd Conversation facade (not an execution engine)."""
import copy
import os
import threading
import time
import uuid

from .cli import CliAdapter
from .attachment_context import request_context
from .command_manifest import command_token
from .shepherd_recap import ShepherdRecapMixin
from .shepherd_subagents import ShepherdSubagentsMixin
from shepherd import (ClaudeConnector, ClaudeRunner, CodexFamilyConnector,
                      CodexFamilyRunner, ExecutionHarness, PiConnector, PiRunner, Service)
from shepherd.store import Conflict
from shepherd.turn_inputs import TurnInputs
from shepherd.image_content import (image_block, model_image_info, model_patch_limited,
                                    MODEL_IMAGE_PATCH_LIMIT, ImageInputError)
from shepherd.compact_operations import CompactOperations, CompactOperationConflict
from pi_reasoning import is_openai_reasoning_provider, is_openai_summary_titles_only


def image_transport_notice(transport):
    """Turn notice for execution-only image changes; None when nothing changed."""
    if transport is None:
        return None
    compressed = getattr(transport, 'history_compressed', 0)
    omitted = getattr(transport, 'omitted', 0)
    if not transport.derivations and not omitted:
        return None
    details = ['仅模型执行副本经过压缩；原始附件、Turn Input 和既有历史未改写。']
    if compressed:
        details.append(f'整请求超过传输预算，本轮压缩了 {compressed} 张较早的历史图片。')
    if omitted:
        details.append(f'另有 {omitted} 张更早的历史图片压缩后仍放不下，本轮未发送；原图仍在存档中。')
    return {'state':'ready','stage':'image_transport','scope':'turn',
            'label':'图片已按 Claude 传输预算优化，原图保留','details':details}


class ShepherdAdapter(ShepherdSubagentsMixin, ShepherdRecapMixin, CliAdapter):
    name = 'shepherd'
    trial = False
    SESSION_ID_PREFIX = 'adp_'
    display = 'Shepherd'
    binary = None
    streams_text = True
    asks_permission = True
    persist_supported = False
    search_supported = True
    rewind_supported = False
    fork_supported = True
    compact_supported = True
    durable_delivery = False   # Shepherd uses its ownership-bound request ledger.
    # Each Runner owns its cancellation protocol and bounded process cleanup.
    interrupt_kill = False

    def transcript_stat(self, sid):
        """Fingerprint the public projection so missed SSE can be reconciled."""
        path = self._msg_path(sid)
        try:
            st = os.stat(path)
        except OSError:
            return {}
        return {"size": st.st_size, "mtime": int(st.st_mtime * 1000)}

    def __init__(self, data_dir, workspace, bus):
        self.policy_home = os.path.abspath(data_dir)
        if self.trial:
            from shepherd.trial import data_root
            data_dir = data_root(data_dir)
        super().__init__(data_dir, workspace, bus)
        self.data_home = os.path.abspath(data_dir)
        self.shepherd_root = os.path.join(self.data_home, 'shepherd')
        self.compact_operations = CompactOperations(self.shepherd_root)
        self.service = Service(self.shepherd_root, storage="unified",
                               claude_system_lookup=self._claude_static_system)
        from shepherd.history_retrieval import HistoryRetrieval
        self._history_retrieval = HistoryRetrieval(self.shepherd_root)
        self._history_search_lock = threading.Lock()
        self._history_search_building = False
        self._history_search_done = 0
        self._history_search_total = 0
        self._history_search_active = ''
        self._history_search_errors = []
        self._history_search_retry_at = 0
        from shepherd.execution_scratch import sweep
        sweep(self.shepherd_root)
        # A delete is complete only when all conversation/binding-scoped content
        # is gone. The body-free marker survives to reject delayed requests and
        # lets startup finish an interrupted purge without restoring the row.
        from shepherd.physical_delete import deleted_ids, mark_deleted, purge
        deleted = set(deleted_ids(self.shepherd_root))
        for sid in self.service.store.conversation_ids():
            try:
                state = self.service.store.load_raw(sid, allow_deleted=True)
                if state and state.get('tombstone'):
                    # Upgrade deletion tombstones written by an older build.
                    mark_deleted(self.shepherd_root, sid)
                    deleted.add(sid)
            except Exception:
                continue
        self._history_cleanup_warnings = []
        purged = set()
        for sid in sorted(deleted):
            try:
                state = self.service.store.load_raw(sid, allow_deleted=True)
                if state is not None:
                    purge(self.shepherd_root, sid, state)
                purged.add(sid)
            except (Conflict, ValueError, OSError) as exc:
                self._history_cleanup_warnings.append({
                    'conversation':sid, 'code':'physical_cleanup_pending',
                    'message':str(exc)[:200]})
        removed = [sid for sid in list(self.index) if sid in purged]
        for sid in removed:
            super().delete_session(sid)
        self._pending_physical_deletes = frozenset(deleted - purged)
        self._tombstoned_at_startup = tuple(sorted(purged))
        self._workers = {}
        self._worker_lock = threading.Lock()
        self._shepherd_approvals = {}
        self._shepherd_approval_lock = threading.Lock()
        self._requested_switch_locks = {}
        self._selection_epochs = {}
        self._prepare_timers = {}
        self._promotion_timers = {}
        self._promotion_lock = threading.Lock()
        from shepherd.requests import Requests
        self.requests = Requests(self.shepherd_root)
        self._request_local = threading.local()
        self._compact_local = threading.local()
        self._compact_threads = {}
        self._shutting_down = False
        # Compatibility diagnostics are durable metadata-only audit evidence,
        # not an actionable engine condition. Remove the warning persisted by
        # older builds before restoring any genuine recovery notice.
        active_ids = [sid for sid in self.index if sid not in self._pending_physical_deletes]
        for sid in active_ids:
            self._publish_recovery_compatibility({'conversation_id':sid})
        from shepherd.recovery import CrashRecovery
        # Startup scans only classify durable state. Inflight recovery must run
        # through RecoveryController so its persisted retry budget and turn/switch
        # guards cannot be bypassed by a process restart.
        self._recovery_reports = CrashRecovery(self.service).scan(
            active_ids, recover_inflight=False)
        for report in self._recovery_reports:
            self._publish_recovery_compatibility(report)
        from shepherd.recovery_controller import RecoveryController
        self._recovery = RecoveryController(
            self.service, self.requests, retry_callback=self._retry_recovery,
            report_callback=self._record_recovery)
        for sid in active_ids:
            self._check_namespace(sid)
            state = self.service.coordinator.state(sid)
            self.requests.recover(sid, state)
            self._sync_active_binding(sid)
            # A successful durable compact receipt may predate the build that
            # emits compaction-ready fences. Reconcile that one legacy notice
            # from committed evidence; this is idempotent after the issue is gone.
            self._compact_operation_state(sid)
            # Older live checkpoints could leave an unknown-but-available
            # Shepherd turn displayed as a red local transport failure.  The
            # durable ledger is authoritative; heal that projection on startup.
            for turn in (state or {}).get('turns') or []:
                if (turn.get('outcome') == 'unknown'
                        and turn.get('history_state') == 'available'):
                    self._downgrade_unknown_projection(sid, int(turn.get('seq') or 0))
        # Native switch recovery and inflight evidence recovery are scheduled
        # only after construction, through the same guarded paths as live work.
        for report in list(self._recovery_reports):
            sid = report.get('conversation_id')
            if report.get('state') in ('pending_switch','recovery_rebind_pending',
                                       'recovery_binding_required','legacy_recovery_required'):
                self._schedule_engine_prepare(sid)
            elif report.get('reason') == 'startup_recovery_pending':
                self.recover_pending(sid, trigger='startup', blocking=False)
        # Formal sessions created before unified storage retain their immutable
        # native evidence, then move the active Producer through the ordinary
        # verified switch path.  Timers keep adapter construction cheap and let
        # busy/recovering sessions finish their existing boundary first.
        if not self.trial:
            for sid in active_ids:
                self._schedule_storage_promotion(sid)

    def _check_namespace(self, sid):
        trial_sid = str(sid).startswith('adp_trial_')
        if trial_sid != self.trial:
            raise Conflict('Shepherd 会话不属于当前存储空间')

    def trial_policy(self):
        from shepherd.trial import policy
        return policy(self.policy_home) if self.trial else {'create': True, 'execute': True}

    def _require_execution(self, sid):
        self._check_namespace(sid)
        if self.trial and not self.trial_policy()['execute']:
            raise Conflict('统一存储试用执行已暂停；历史仍可查看')

    def normalize_options(self, opts):
        out = super().normalize_options(opts)
        out['shepherd_mode'] = True
        engine = str(out.get('active_engine') or '').strip()
        if engine:
            out['active_engine'] = engine
        return out

    def create_session(self, domain='', task=False, opts=None, *, _turn_guard=None):
        if self.trial and not self.trial_policy()['create']:
            raise Conflict('统一存储试用新建已关闭')
        opts = dict(opts or {})
        engine = str(opts.pop('active_engine', '') or '').strip()
        if engine not in ('claude', 'codex', 'pi'):
            raise ValueError('Shepherd 初始引擎无效')
        opts.update(shepherd_mode=True, active_engine=engine)
        row = super().create_session(domain=domain, task=task, opts=opts,
                                     _turn_guard=_turn_guard)
        sid = row['id']
        try:
            # A new Conversation must snapshot the same engine defaults that
            # its first turn would inherit through /config and dispatch.  Saving
            # only active_engine/shepherd_mode left Pi without a session model:
            # the collapsed label used the global GPT route while the menu fell
            # back to the catalog's DeepSeek default until turn one reported its
            # actual model.
            from workbench import _wb_state_read
            raw_defaults = ((_wb_state_read().get('engine_opts') or {}).get(engine) or {})
            defaults = self._engine_input_options(raw_defaults, inherited=True)
            # Explicit role belongs to this Conversation; global role never does.
            engine_opts = {**defaults, **self._engine_input_options(opts)}
            worker = self._worker(engine)
            normalized = self._engine_options(engine, worker, engine_opts)
            # New Claude Bindings snapshot their static prompt before any
            # portable history import. Publish creation options first so the
            # Service callback reads the same role/style source as turn dispatch.
            with self._lock:
                self.index[sid]['engine_opts'] = {engine: normalized}
                self._save_index()
            created = self.service.create(sid, engine, self.session_cwd(sid))
            with self._lock:
                current = self.index[sid]
                current['shepherd_mode'] = True
                current['shepherd_trial'] = self.trial
                current['engine'] = engine
                current['binding_id'] = created['binding_id']
                current['producer_generation'] = created['lease']['generation']
                self._save_index()
            return copy.deepcopy(self.index[sid])
        except Exception:
            if _turn_guard is not None:
                super()._delete_session_peer_fenced_base(sid)
            else:
                super().delete_session(sid)
            raise

    def fork_session(self, sid, msg_id=''):
        """Branch at a settled reply, retaining Shepherd and the selected engine."""
        from shepherd.fork import copy_history
        self._require_execution(sid)
        with self._lock:
            guard = self._turn_locks.setdefault(sid, threading.Lock())
            switch_guard = self._requested_switch_locks.setdefault(sid, threading.Lock())
        if not guard.acquire(blocking=False):
            raise Conflict('当前回合尚未结束，请稍后分支')
        switched = False
        child = None
        child_guard = threading.Lock()
        child_guard.acquire()
        try:
            switched = switch_guard.acquire(blocking=False)
            if not switched:
                raise Conflict('正在切换引擎，请稍后分支')
            with self._lock:
                source = copy.deepcopy(self.index.get(sid))
            if not source:
                raise Conflict('Shepherd 会话不存在')
            state = self.service.store.load_raw(sid)
            if (not state or state.get('active_turn') or state.get('pending_switch')
                    or state.get('legacy_recoveries')):
                raise Conflict('会话仍在执行、切换或恢复，请稍后分支')
            messages = self.messages(sid)
            index = next((i for i, m in enumerate(messages)
                if (m.get('info') or {}).get('id') == msg_id
                and (m.get('info') or {}).get('role') == 'assistant'), None)
            if index is None:
                raise Conflict('该回复没有可校验的分支锚点')
            generation = str((messages[index].get('info') or {}).get('seq') or '')
            boundary = next((r for r in state['turns']
                if r.get('attempt_id', '').split(':', 1)[0] == 'attempt_' + generation), None)
            if not boundary or boundary['outcome'] != 'completed':
                raise Conflict('只能从已完成并校验的回复创建分支')
            engine = self.selected_engine(sid)
            opts = {**copy.deepcopy(source.get('opts') or {}),
                    **self.session_options(sid, engine=engine),
                    'shepherd_mode': True, 'active_engine': engine}
            child = self.create_session(domain=source.get('domain') or '',
                task=bool(source.get('task')), opts=opts, _turn_guard=child_guard)['id']
            # Settings belong to the facade; no native grants, resident hosts,
            # request receipts or pending engine selections are shared.
            with self._lock:
                row = self.index[child]
                row['engine_opts'] = {**copy.deepcopy(source.get('engine_opts') or {}),
                                     engine: row['engine_opts'][engine]}
                row['forked_from'] = sid
                row['forked_at_message'] = msg_id
                row['title'] = ('分支 · ' + (source.get('title') or 'Shepherd'))[:60]
                self._save_index()
            copy_history(self.service, sid, child, boundary['seq'])
            self._copy_fork_messages(sid, child, messages[:index + 1])
            self._sync_active_binding(child)
            self.bus.publish({'type': 'session.updated', 'properties': {'sessionID': child}})
            with self._lock:
                return copy.deepcopy(self.index[child])
        except Exception:
            if child:
                self._tombstone_and_purge(child)
                super()._delete_session_peer_fenced_base(child)
            raise
        finally:
            child_guard.release()
            if switched:
                switch_guard.release()
            guard.release()

    def _copy_fork_messages(self, source_id, target_id, messages):
        """Keep display attachments independent of the source's lifetime too."""
        import shutil
        from pathlib import Path
        old_prefix = f'/session/{source_id}/asset/'
        new_prefix = f'/session/{target_id}/asset/'
        def clone(value):
            if isinstance(value, dict):
                return {key: target_id if key == 'sessionID' else clone(item)
                        for key, item in value.items()}
            if isinstance(value, list):
                return [clone(item) for item in value]
            if isinstance(value, str) and value.startswith(old_prefix):
                from urllib.parse import unquote
                name = unquote(value[len(old_prefix):])
                path = self.asset_path(source_id, name)
                if not path:
                    raise Conflict('分支引用的附件已缺失')
                target = Path(self.dir) / 'uploads' / target_id
                target.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target / os.path.basename(path))
                return new_prefix + value[len(old_prefix):]
            return value
        self._rewrite_msgs(target_id, msgs=clone(messages))

    def list_sessions(self):
        rows = super().list_sessions()
        for row in rows:
            row['engine'] = self.selected_engine(row['id'])
            row['shepherd_mode'] = True
            row['shepherd_trial'] = self.trial
        return rows

    def _record_recovery(self, report):
        with self._lock:
            self._recovery_reports.append(copy.deepcopy(report))
            del self._recovery_reports[:-100]
        sid = str((report or {}).get('conversation_id') or '')
        if sid and (report or {}).get('outcome') in ('recovered_incomplete','committed'):
            self._sync_active_binding(sid)
        if sid and (report or {}).get('state') in ('recovered_incomplete', 'recovery_rebind_pending'):
            self._downgrade_unknown_projection(sid, int((report or {}).get('offset') or 0))
        if sid and ((report or {}).get('state') == 'ready'
                    or (report or {}).get('outcome') == 'committed'):
            self._clear_recovery_notice_if_resolved(sid)
        self._publish_recovery_compatibility(report)

    def _downgrade_unknown_projection(self, sid, seq):
        """Make GUI projection match durable unknown-but-available recovery.

        Generic turn finalization sees EOF before RecoveryController verifies the
        native archive, so it may persist a red local transport failure.  When
        recovery later proves the content is safe history but terminal is unknown,
        the public projection must not keep advertising the answer itself as a
        failed assistant message.
        """
        if not seq:
            return False
        try:
            state = self.service.coordinator.state(sid) or {}
            turn = next((row for row in state.get('turns') or [] if row.get('seq') == seq), None)
        except Exception:
            turn = None
        if not turn or turn.get('outcome') != 'unknown' or turn.get('history_state') != 'available':
            return False
        started = int(float(turn.get('started_at') or 0) * 1000)
        reason = str(turn.get('recovery_reason') or 'native_terminal_unconfirmed')
        changed = False
        def rewrite(messages):
            nonlocal changed
            out = []
            for msg in messages:
                info = msg.get('info') or {}
                if info.get('role') == 'assistant':
                    turn_t0 = int(info.get('turn_t0') or info.get('seq') or 0)
                    if (abs(turn_t0 - started) < 10000 and info.get('local_only')
                            and ((info.get('error') or {}).get('message')
                                 or info.get('terminal', {}).get('status') == 'failed')):
                        info = dict(info)
                        info.pop('failed', None); info.pop('error', None)
                        info.pop('local_only', None)
                        info['terminal'] = {'status':'unknown', 'source':'recovery',
                                            'native':{'source':'recovery', 'status':'unknown'}}
                        info['recovery'] = {'state':'blocked', 'reason':reason,
                                            'label':'原生终态未确认，内容已保留'}
                        msg = {**msg, 'info':info}
                        changed = True
                out.append(msg)
            return out
        try:
            self._rewrite_msgs(sid, transform=rewrite)
            if changed:
                self._trim_store(sid)
            return changed
        except OSError:
            return False

    def _clear_recovery_notice_if_resolved(self, sid, state=None):
        """Clear a persisted recovery issue once the execution fence is gone."""
        coordinator = getattr(self.service, 'coordinator', None)
        if coordinator is None:
            return False
        state = state if state is not None else (coordinator.state(sid) or {})
        if self._active_turn(state).get('recovery_state') == 'blocked':
            return False
        with self._lock:
            row = self.index.get(sid) or {}
            issues = list((row.get('environment_issues') or {}).values())
            legacy = row.get('environment') or {}
            present = any(issue.get('stage') == 'recovery' for issue in issues)
            present = present or (not issues and legacy.get('stage') == 'recovery')
        if not present:
            return False
        self._env_publish(sid, {'state':'ready', 'stage':'recovery',
                               'label':'历史已核验，可继续'})
        return True

    def _publish_recovery_compatibility(self, report):
        """Keep compatibility diagnostics in the Coordinator audit only.

        Internal metadata omissions are already validated before commit and
        require no user action. Older builds persisted them as an input-bar
        warning; clear that legacy projection without discarding the audit.
        """
        sid = str((report or {}).get('conversation_id') or '')
        if not sid:
            return
        with self._lock:
            row = self.index.get(sid) or {}
            legacy = ((row.get('environment') or {}).get('stage') == 'shepherd_compatibility'
                      or 'shepherd_compatibility' in (row.get('environment_issues') or {}))
        if legacy:
            self._env_publish(sid, {'state':'ready', 'stage':'shepherd_compatibility'})

    def _sync_active_binding(self, sid):
        """Mirror the durable Producer into the public index after recovery."""
        try:
            active = self.service.active(sid)
            binding, lease = active['binding'], active['lease']
        except Exception:
            return False
        if not binding or not lease:
            return False
        with self._lock:
            row = self.index.get(sid)
            if row is None:
                return False
            changed = (row.get('engine') != binding['engine']
                       or row.get('binding_id') != binding['binding_id']
                       or row.get('producer_generation') != lease['generation'])
            row['engine'] = binding['engine']
            row['binding_id'] = binding['binding_id']
            row['producer_generation'] = lease['generation']
            row.setdefault('opts', {})['active_engine'] = binding['engine']
            if changed:
                self._save_index()
        return True

    def _schedule_storage_promotion(self, sid, delay=.5):
        if self.trial or self._shutting_down:
            return
        with self._lock:
            if sid not in self.index or sid in self._promotion_timers:
                return
            timer = threading.Timer(delay, self._promote_storage, args=(sid,))
            timer.daemon = True
            self._promotion_timers[sid] = timer
        timer.start()

    def _promote_storage(self, sid):
        with self._lock:
            self._promotion_timers.pop(sid, None)
            if self._shutting_down or sid not in self.index:
                return
            turn_guard = self._turn_locks.setdefault(sid, threading.Lock())
            switch_guard = self._requested_switch_locks.setdefault(sid, threading.Lock())
        # Promotion verifies the whole existing history twice around the fence.
        # Serialize the one-time work so several old sessions cannot create a
        # startup I/O and memory spike together.
        if not self._promotion_lock.acquire(blocking=False):
            self._schedule_storage_promotion(sid, 1)
            return
        if not turn_guard.acquire(blocking=False):
            self._promotion_lock.release()
            self._schedule_storage_promotion(sid, 2)
            return
        switch_acquired = False
        try:
            switch_acquired = switch_guard.acquire(blocking=False)
            if not switch_acquired:
                self._schedule_storage_promotion(sid, 2)
                return
            state = self.service.coordinator.state(sid) or {}
            active = self.service.active(sid)
            if (active.get('binding') or {}).get('connector_version','').startswith('unified-v1:'):
                return
            pending = state.get('pending_switch') or {}
            if state.get('active_turn') or (pending and not pending.get('storage_upgrade')):
                self._schedule_storage_promotion(sid, 2)
                return
            self.service.promote_to_unified(sid)
            self._sync_active_binding(sid)
        except Exception as exc:
            # A semantic preflight failure is actionable and must stay visible;
            # repeatedly rereading a long incompatible history would add load
            # without changing the evidence.
            self._env_publish(sid, {'state':'warning', 'stage':'storage-upgrade',
                'label':'Shepherd 统一存储升级待处理', 'details':[str(exc)[:300]]})
        finally:
            if switch_acquired:
                switch_guard.release()
            turn_guard.release()
            self._promotion_lock.release()

    def promote_sessions(self):
        """Return deterministic progress and trigger every formal session."""
        rows = []
        for sid in sorted(self.index):
            state = self.service.coordinator.state(sid) or {}
            active = self.service.active(sid)
            binding = active.get('binding') or {}
            unified = binding.get('connector_version','').startswith('unified-v1:')
            rows.append({'session_id':sid, 'state':('unified' if unified else
                'busy' if state.get('active_turn') else 'pending'),
                'turns':len(state.get('turns') or [])})
            if not unified:
                self._schedule_storage_promotion(sid, .01)
        return rows

    def _retry_recovery(self, sid, retry_count):
        if self._shutting_down or sid not in self.index:
            return
        self.recover_pending(sid, trigger='background_retry', blocking=False,
                             retry_count=retry_count)

    def recover_pending(self, sid, *, trigger='operation', blocking=False,
                        retry_count=0):
        """Run evidence-only recovery under the existing conversation guards."""
        self._check_namespace(sid)
        if self.trial and not self.trial_policy()['execute']:
            return {'conversation_id':sid, 'state':'trial_paused', 'action':'none'}
        with self._lock:
            if sid not in self.index:
                return {'conversation_id':sid, 'state':'deleted', 'action':'none'}
            turn_guard = self._turn_locks.setdefault(sid, threading.Lock())
        if not turn_guard.acquire(blocking=blocking):
            return {'conversation_id':sid, 'state':'busy', 'action':'deferred'}
        try:
            return self._recover_with_turn_guard(
                sid, trigger=trigger, retry_count=retry_count, blocking=blocking)
        finally:
            turn_guard.release()

    def _recover_with_turn_guard(self, sid, *, trigger, retry_count=0,
                                 blocking=True):
        import self_update
        coordinator = getattr(getattr(self, 'service', None), 'coordinator', None)
        if coordinator is None:
            return {'conversation_id':sid, 'state':'unavailable', 'action':'none'}
        # Tests and embedders may replace Service after adapter construction.
        # Recovery must always operate on the same Service as production paths.
        self._recovery.service = self.service
        switch_guard = self._switch_lock(sid)
        if not switch_guard.acquire(blocking=blocking):
            return {'conversation_id':sid, 'state':'switching', 'action':'deferred'}
        try:
            if self_update.external_draining():
                return {'conversation_id':sid, 'state':'draining', 'action':'deferred'}
            report = self._recovery.recover_guarded(
                sid, trigger=trigger, retry_count=retry_count)
            if report.get('outcome') in ('recovered_incomplete','committed'):
                self._sync_active_binding(sid)
            return report
        finally:
            switch_guard.release()

    def grounding_engine(self, sid):
        # 技能可见性取决于本回合实际执行的引擎及其恢复方式，而不是门面名 shepherd。
        # grounding 在 _before_turn_start 之后构造，此时 Producer 已是本回合的执行引擎。
        engine = self.session_engine(sid)
        return 'shepherd:claude' if engine == 'claude' else engine

    def grounding(self, sid, first_turn):
        grounding = super().grounding(sid, first_turn)
        if self.session_engine(sid) == 'pi' and not str(
                (grounding or {}).get('constant') or '').strip():
            raise Conflict('Pi 系统约定构造失败，已停止启动')
        return grounding

    def session_engine(self, sid):
        try:
            active = self.service.active(sid)
            if active['binding']:
                return active['binding']['engine']
        except Exception:
            pass
        return str((self.index.get(sid) or {}).get('engine') or
                   ((self.index.get(sid) or {}).get('opts') or {}).get('active_engine') or '')

    def selected_engine(self, sid):
        row = self.index.get(sid) or {}
        selected = str(row.get('selected_engine') or row.get('next_engine') or '')
        return selected if selected in ('claude', 'codex', 'pi') else self.session_engine(sid)

    @staticmethod
    def _history_seq(state):
        turns = (state or {}).get('turns')
        return len(turns) if isinstance(turns, list) else int((state or {}).get('committed_offset') or 0)

    @staticmethod
    def _active_turn(state):
        turn = ((state or {}).get('active_turn')
                or (state or {}).get('inflight') or {})
        if turn and 'recovery_state' not in turn and turn.get('state') == 'needs_reconciliation':
            turn = {**turn, 'recovery_state':'blocked',
                    'recovery_reason':turn.get('reconciliation_reason') or ''}
        return turn

    def engine_has_history(self, sid, _engine_sid):
        try: return self._history_seq(self.service.active(sid)['state']) > 0
        except Exception: return False

    def _trim_store(self, sid):
        """Keep the complete verified public projection for indexed paging.

        Other adapters can reconstruct old pages from their native transcript.
        Shepherd's mixed-engine archive is authoritative, but reconstructing it
        may require private native evidence; discarding this already verified
        projection would therefore make ordinary UI paging depend on Producers.
        """
        return None

    @staticmethod
    def _presentation_message(message, fallback_engine='', fallback_model=''):
        info = message.get('info') or {}
        engine = str(info.get('engine') or fallback_engine)
        model = str(info.get('model') or fallback_model)
        provider = model.split('/', 1)[0] if '/' in model else model
        if engine != 'pi' or not is_openai_reasoning_provider(provider):
            return message
        parts = [part for part in message.get('parts') or []
                 if not (part.get('type') == 'reasoning'
                         and is_openai_summary_titles_only(provider, part.get('text')))]
        return message if len(parts) == len(message.get('parts') or []) else {
            **message, 'parts': parts}

    def _presentation_messages(self, messages):
        """Hide encrypted OpenAI reasoning titles from every UI history entry point."""
        return [self._presentation_message(message) for message in messages]

    def _presentation_live_message(self, _sid, message):
        if not message:
            return message
        # New checkpoints carry immutable turn attribution. Older snapshots
        # without it stay visible rather than being guessed from mutable session state.
        return self._presentation_message(message)

    def history(self, sid, have=0, limit=30):
        """Page the immutable public projection without reopening native stores.

        Mixed-engine source verification belongs to commit and Producer catch-up.
        A user scrolling older UI history must never touch Claude/Pi/Codex
        bindings or acquire native writers.
        """
        return self.projected_history(sid, have, limit)

    def _refresh_history_search(self, conversations):
        errors = []
        try:
            for conversation in conversations:
                with self._history_search_lock:
                    self._history_search_active = conversation
                try:
                    self._history_retrieval.sync(conversation)
                except Exception as exc:
                    errors.append(str(exc)[:200])
                finally:
                    with self._history_search_lock:
                        self._history_search_active = ''
                        self._history_search_done += 1
        finally:
            with self._history_search_lock:
                self._history_search_errors = errors
                self._history_search_retry_at = time.time() + 30 if errors else 0
                self._history_search_building = False

    def search_sessions(self, query, sort=''):
        """Search verified public Conversation history through its dedicated cache."""
        catalog = self._history_retrieval.catalog()
        stale = [row['conversation'] for row in catalog['conversations']
                 if (row.get('index') or {}).get('state') != 'ready']
        with self._history_search_lock:
            building = self._history_search_building
            active = self._history_search_active
            progress = {'done': self._history_search_done,
                        'total': self._history_search_total}
            errors = list(self._history_search_errors)
        ordering = 'time' if str(sort or '') == 'time' else 'relevance'
        try:
            found = self._history_retrieval.search_cached(
                query, limit=20, sort=ordering, exclude=(active,))
        except Exception as exc:
            return {'results': [], 'indexing': building, 'progress': progress,
                    'error': str(exc)[:200]}
        with self._history_search_lock:
            if (stale and not self._history_search_building
                    and time.time() >= self._history_search_retry_at):
                self._history_search_building = True
                self._history_search_done = 0
                self._history_search_total = len(stale)
                self._history_search_errors = []
                threading.Thread(target=self._refresh_history_search, args=(stale,),
                                 name='shepherd-history-search', daemon=True).start()
            building = self._history_search_building
            progress = {'done': self._history_search_done,
                        'total': self._history_search_total}
        metadata = {row['conversation']: row for row in catalog['conversations']}
        groups = {}
        for hit in found.get('hits') or []:
            sid = hit['conversation']
            meta = metadata.get(sid) or {}
            group = groups.get(sid)
            if group is None:
                group = groups[sid] = {
                    'session_id': sid,
                    'engine': meta.get('engine') or self.selected_engine(sid),
                    'title': hit.get('title') or meta.get('title') or '对话',
                    'domain': hit.get('domain') or meta.get('domain') or '',
                    'updated': int(meta.get('updated_at') or 0),
                    'count': 0,
                    'hits': [],
                }
            group['count'] += 1
            if len(group['hits']) < 10:
                group['hits'].append({
                    'message_id': '',
                    'turn_from_end': max(0, int(meta.get('total') or 0) - int(hit['seq'])),
                    'role': hit.get('role') or '',
                    'at': int(float(hit.get('settled_at') or 0) * 1000),
                    'snippet': hit.get('snippet') or '',
                })
        warnings = list(catalog.get('warnings') or []) + list(found.get('warnings') or [])
        return {'results': list(groups.values())[:30], 'indexing': building,
                'progress': progress, 'error': errors[0] if errors else '',
                'warnings': warnings}

    def switch_engine(self, sid, engine):
        """Persist UI selection immediately; Producer catch-up happens at send."""
        self._peer_visible_operation_admit(
            sid, "目标会话切换引擎前等待隔离问询冻结上下文")
        try:
            return self._switch_engine_fenced(sid, engine)
        finally:
            self._peer_user_release(sid)

    def _switch_engine_fenced(self, sid, engine):
        self._check_namespace(sid)
        if self._shutting_down:
            raise Conflict('Desk 正在退出，未切换引擎')
        if engine not in ('claude', 'codex', 'pi'):
            raise Conflict('Shepherd 目标引擎无效')
        producer = self.session_engine(sid)
        coordinator = getattr(self.service, 'coordinator', None)
        pending = bool((coordinator.state(sid) or {}).get('pending_switch')) if coordinator else False
        preparing = engine != producer or pending
        with self._lock:
            row = self.index.get(sid)
            if row is None:
                raise Conflict('Shepherd Conversation 不存在')
            if not (row.get('engine_opts') or {}).get(engine):
                # First use of an engine inherits its own current defaults,
                # just as creating a Conversation directly on that engine does.
                from workbench import _wb_state_read
                defaults = ((_wb_state_read().get('engine_opts') or {}).get(engine) or {})
                row.setdefault('engine_opts', {})[engine] = self._engine_options(
                    engine, self._worker(engine), self._engine_input_options(defaults, inherited=True))
            row['selected_engine'] = engine
            row['selection_epoch'] = int(row.get('selection_epoch') or 0) + 1
            if not preparing:
                row.pop('next_engine', None)
            else:
                row['next_engine'] = engine
            self._save_index()
        self._publish_requested_engine(sid, producer, engine, preparing=preparing)
        if preparing:
            self._schedule_engine_prepare(sid)
        else:
            self._cancel_engine_prepare(sid)
        self._switch_notice(sid)
        self.refresh_environment(sid)
        return {'ok':True, 'session_id':sid, 'engine':engine,
                'producer_engine':producer, 'preparing':preparing,
                'opts':self.saved_engine_options(sid, engine),
                'model':self.config_label(sid, engine=engine)}

    def _cancel_engine_prepare(self, sid):
        """Invalidate a queued/in-flight pre-fence optimization."""
        with self._lock:
            self._selection_epochs[sid] = self._selection_epochs.get(sid, 0) + 1
            timer = self._prepare_timers.pop(sid, None)
        if timer:
            timer.cancel()

    def _schedule_engine_prepare(self, sid):
        if self.trial and not self.trial_policy()['execute']: return
        """Debounce best-effort catch-up; clicks and turns never wait for it."""
        with self._lock:
            if self._shutting_down or sid not in self.index:
                return
            epoch = self._selection_epochs.get(sid, 0) + 1
            self._selection_epochs[sid] = epoch
            old = self._prepare_timers.pop(sid, None)
            if old:
                old.cancel()
            timer = threading.Timer(.25, self._prepare_selected_engine, args=(sid, epoch))
            timer.daemon = True
            self._prepare_timers[sid] = timer
        timer.start()

    def _prepare_selected_engine(self, sid, epoch):
        with self._lock:
            if self._shutting_down or self._selection_epochs.get(sid) != epoch:
                return
            self._prepare_timers.pop(sid, None)
            guard = self._turn_locks.setdefault(sid, threading.Lock())
        if not guard.acquire(blocking=False):
            return
        try:
            with self._lock:
                if self._selection_epochs.get(sid) != epoch:
                    return
            self._apply_requested_engine(sid)
        except Exception as exc:
            # Keep the selection, but do not hide failed preparation until send.
            # An obsolete attempt cannot overwrite a newer selection's notice.
            self._switch_notice(sid, error=str(exc), epoch=epoch)
            return
        finally:
            guard.release()
        with self._lock:
            pending = bool((self.index.get(sid) or {}).get('next_engine'))
        if pending:
            self._schedule_engine_prepare(sid)

    def _switch_notice(self, sid, *, error=None, epoch=None):
        with self._lock:
            row = self.index.get(sid)
            if row is None or (epoch is not None and self._selection_epochs.get(sid) != epoch):
                return
            if error is None:
                if ('shepherd-switch' not in (row.get('environment_issues') or {})
                        and (row.get('environment') or {}).get('stage') != 'shepherd-switch'):
                    return
                payload = {'state':'ready', 'stage':'shepherd-switch'}
            else:
                payload = {'state':'warning', 'stage':'shepherd-switch',
                           'label':'引擎准备失败；可重试或改选引擎', 'details':[error]}
            payload = self._env_project_locked(sid, payload)
            self.bus.publish({'type':'session.environment',
                              'properties':{'sessionID':sid, **payload}})

    def _publish_requested_engine(self, sid, producer, selected, *, engine_changed=False, preparing=None):
        props = {'sessionID':sid, 'engine':selected, 'selected_engine':selected,
                 'producer_engine':producer, 'preparing':(selected != producer if preparing is None else preparing),
                 'engine_changed':bool(engine_changed), 'shepherd_mode':True}
        self.bus.publish({'type':'session.updated','properties':props})
        # Keep selection events cheap: dynamic model catalogs are fetched by the
        # ordinary /config refresh and must never block the click response.
        self.bus.publish({'type':'session.settings','properties':{
            **props, 'opts':self.saved_engine_options(sid, selected),
            'model':self.config_label(sid, engine=selected)}})

    def _switch_lock(self, sid):
        with self._lock:
            return self._requested_switch_locks.setdefault(sid, threading.Lock())

    def _apply_requested_engine(self, sid):
        self._require_execution(sid)
        import self_update
        if self._shutting_down:
            raise Conflict('Desk 正在退出，引擎选择已保留')
        self._recap_cancel(sid)
        with self._switch_lock(sid):
            if self_update.external_draining():
                raise Conflict('Desk 正在重启，引擎选择已保留，启动后继续')
            if getattr(self.service, 'coordinator', None) is not None:
                self._recovery.service = self.service
                self._recovery.recover_guarded(sid, trigger='switch_before')
                recovered = self.service.active(sid)
                if not recovered['binding']:
                    state = recovered['state']
                    legacy = (state.get('legacy_recoveries') or [])
                    if legacy:
                        self.service.recover_legacy_incomplete(sid, legacy[0]['recovery_id'])
                    else:
                        pending_recovery = state.get('pending_switch') or {}
                        recovery_engine = str(((state.get('bindings') or {}).get(
                            pending_recovery.get('target_binding_id')) or {}).get('engine') or '')
                        with self._lock:
                            selected = str((self.index.get(sid) or {}).get('next_engine') or '')
                        if pending_recovery.get('recovery_turn_id') and selected \
                                and recovery_engine and selected != recovery_engine:
                            # The user chose another engine after the incomplete
                            # turn was fenced. Finishing the same-engine rebind
                            # first would make that choice unreachable whenever
                            # the rebind fails; service.switch retargets the
                            # fenced switch to the selected engine instead.
                            pass
                        elif pending_recovery.get('recovery_turn_id'):
                            self.service.recover_incomplete(
                                sid, pending_recovery.get('source_binding_id') or '')
                        else:
                            old = next((row for row in state.get('bindings', {}).values()
                                        if row.get('status') == 'recovery_required'), None)
                            if old:
                                self.service.recover_incomplete(sid, old['binding_id'])
            with self._lock:
                row = self.index.get(sid)
                if row is None:
                    raise Conflict('Shepherd Conversation 不存在')
                target = str(row.get('next_engine') or '')
                selection_epoch = int(row.get('selection_epoch') or 0)
            previous_engine = self.session_engine(sid)
            coordinator = getattr(self.service, 'coordinator', None)
            state = coordinator.state(sid) if coordinator is not None else {}
            pending = state.get('pending_switch') or {}
            pending_binding = (state.get('bindings') or {}).get(pending.get('target_binding_id')) or {}
            required_target = str(pending_binding.get('engine') or '')
            # A newer explicit selection may retarget a fenced switch. When the
            # selection is unchanged, resume the persisted original target.
            apply_target = target or required_target
            if not apply_target:
                active = self.service.active(sid)
            elif apply_target == previous_engine and not pending:
                active = self.service.active(sid)
            else:
                try:
                    active = self.service.switch(sid, apply_target, self.session_cwd(sid),
                        cancelled=(None if pending else
                                   lambda: self._shutting_down or self.selected_engine(sid) != apply_target),
                        selection_epoch=selection_epoch)
                except Conflict as exc:
                    if '更新的选择取消' in str(exc):
                        return {'ok':True, 'preparing':True, 'superseded':True,
                                'session_id':sid, 'engine':self.selected_engine(sid),
                                'producer_engine':self.session_engine(sid)}
                    raise
            binding, lease = active['binding'], active['lease']
            if binding is None or lease is None:
                raise Conflict('Shepherd 切换后没有 Active Producer')
            with self._lock:
                row = self.index.get(sid)
                if row is None:
                    raise Conflict('Shepherd Conversation 不存在')
                if row.get('model'):
                    row.setdefault('engine_models', {})[previous_engine] = row['model']
                row['engine'] = binding['engine']
                model = (row.get('engine_models') or {}).get(binding['engine'])
                if model:
                    row['model'] = model
                else:
                    row.pop('model', None)
                row['binding_id'] = binding['binding_id']
                row['producer_generation'] = lease['generation']
                row.setdefault('opts', {})['active_engine'] = binding['engine']
                if row.get('next_engine') == binding['engine']:
                    row.pop('next_engine', None)
                selected = str(row.get('selected_engine') or binding['engine'])
                if selected != binding['engine']:
                    row['next_engine'] = selected
                remaining = str(row.get('next_engine') or '')
                self._save_index()
            selected = self.selected_engine(sid)
            self._publish_requested_engine(sid, binding['engine'], selected,
                                           engine_changed=not remaining)
            if not remaining:
                self._switch_notice(sid)
            return {'ok':True,'preparing':bool(remaining),'session_id':sid,
                    'engine':selected,'producer_engine':binding['engine'],
                    'binding_id':binding['binding_id'],'generation':lease['generation'],
                    'committed_offset':self._history_seq(active['state']),
                    'applied_offset':binding.get('imported_through_seq', binding.get('applied_offset', 0))}

    def _before_turn_start(self, sid):
        rid = getattr(self._request_local, 'rid', '')
        compact = self._compact_operation_state(sid)
        compact_id = getattr(getattr(self, '_compact_local', None), 'operation_id', '')
        if compact.get('running') and compact.get('operation_id') != compact_id:
            raise Conflict('Shepherd 正在压缩上下文，请等待完成后再发送')
        if self._shutting_down or (rid and self.requests.cancelled(sid, rid)):
            raise Conflict('请求已在进入引擎前取消')
        coordinator = getattr(self.service, 'coordinator', None)
        durable = coordinator.state(sid) if coordinator is not None else None
        # prompt_async may already have claimed a request while waiting for the
        # conversation guard. Recheck the durable tombstone inside that guard,
        # before public message persistence or native preparation.
        if isinstance(durable, dict) and durable.get('tombstone'):
            raise Conflict('Shepherd Conversation 已删除，消息未发送')
        # TurnRunner already holds turn_guard. Commit-only recovery takes the
        # switch guard as well, so a stale terminal source cannot race catch-up.
        self._recover_with_turn_guard(sid, trigger='send_before')
        coordinator = getattr(self.service, 'coordinator', None)
        durable = coordinator.state(sid) if coordinator is not None else None
        # Only a real durable v2 Conversation can require recovery rebind.
        # Lightweight embedders/tests may provide only the adapter facade.
        if rid.startswith('pi-child-'):
            # A delayed Pi result must not apply a user's pending engine switch.
            # This is inside the conversation turn guard, before any message store.
            if (self.session_engine(sid) != 'pi' or self.selected_engine(sid) != 'pi'
                    or (durable or {}).get('pending_switch')):
                raise Conflict('Pi 子代理结果等待原会话回到 Pi，未派发')
            return
        if isinstance(durable, dict) and durable.get('version') == 2:
            active_binding = durable.get('active_binding_id') or ''
            pending_recovery = durable.get('pending_switch') or {}
            old = next((row for row in durable.get('bindings', {}).values()
                        if row.get('status') == 'recovery_required'), None)
            if not active_binding and (pending_recovery.get('recovery_turn_id') or old):
                self._apply_requested_engine(sid)
        # 追平可能耗时，期间用户仍可更新目标；未收敛时不能用中间 Producer
        # 写入下一条消息。限制循环次数，持续改选时保留意图并明确拒绝本轮。
        for _ in range(8):
            coordinator = getattr(self.service, 'coordinator', None)
            pending = bool((coordinator.state(sid) or {}).get('pending_switch')) if coordinator else False
            if not (self.index.get(sid) or {}).get('next_engine') and not pending:
                return
            self._apply_requested_engine(sid)
        if (self.index.get(sid) or {}).get('next_engine') or pending:
            raise Conflict('Shepherd 执行引擎仍在切换，请等待完成后重新发送')

    def execution_status(self, sid):
        selected = self.selected_engine(sid)
        producer = self.session_engine(sid)
        target = str((self.index.get(sid) or {}).get('next_engine') or '')
        coordinator = getattr(self.service, 'coordinator', None)
        state = coordinator.state(sid) if coordinator is not None else {}
        active_turn = self._active_turn(state)
        pending = (state or {}).get('pending_switch') or {}
        status = {'selected_engine': selected, 'producer_engine': producer,
                  'preparing': bool(target or pending)}
        reconciliation = None
        if active_turn.get('recovery_state') == 'blocked':
            reconciliation = {
                'required': True,
                'attempt_id': active_turn.get('attempt_id') or '',
                'reason': active_turn.get('recovery_reason') or 'native_evidence_incomplete',
                'detail': active_turn.get('recovery_detail') or '',
                'action': 'omit_and_continue',
            }
        if reconciliation:
            status['reconciliation'] = reconciliation
        return status or None

    def reconcile_end(self, sid, attempt_id):
        self._require_execution(sid)
        """Omit only unverifiable content and continue from an explicit boundary."""
        from shepherd.recovery import process_state, terminate_recorded_process
        with self._lock:
            turn_lock = self._turn_locks.setdefault(sid, threading.Lock())
            switch_lock = self._requested_switch_locks.setdefault(sid, threading.Lock())
        if not turn_lock.acquire(blocking=False):
            raise Conflict('Shepherd 会话仍在执行，不能核验收口')
        switch_acquired = False
        try:
            switch_acquired = switch_lock.acquire(blocking=False)
            if not switch_acquired:
                raise Conflict('Shepherd 会话仍在切换，不能核验收口')
            state = self.service.coordinator.state(sid) or {}
            active_turn = self._active_turn(state)
            identity = (active_turn.get('recovery') or {}).get('process')
            # Missing/ambiguous identity is never signalled: the whole Binding
            # is quarantined instead. An exact live process must be stopped
            # before replacement so two Producers cannot act concurrently.
            process = process_state(identity) if identity else 'unknown'
            if active_turn.get('recovery_state') == 'blocked':
                if process == 'alive':
                    if not terminate_recorded_process(identity):
                        raise Conflict('Shepherd 无法停止仍存活的旧原生进程；证据保持封锁')
                    process = process_state(identity)
                if process != 'dead':
                    raise Conflict('Shepherd 无法确认旧原生写者已退出；证据保持封锁')
            active = self.service.reconcile_end(sid, attempt_id=attempt_id)
            binding, lease = active['binding'], active['lease']
            if not binding or not lease:
                raise Conflict('Shepherd 核验收口后没有 Active Producer')
            with self._lock:
                row = self.index.get(sid)
                if row is None:
                    raise Conflict('Shepherd Conversation 不存在')
                row['engine'] = binding['engine']
                row['binding_id'] = binding['binding_id']
                row['producer_generation'] = lease['generation']
                row.setdefault('opts', {})['active_engine'] = binding['engine']
                self._save_index()
            self._publish_requested_engine(sid, binding['engine'], '')
            self._env_publish(sid, {'state':'ready', 'stage':'recovery',
                'label':'Shepherd 已隔离无法验证的内容，并从未知边界继续'})
            self.requests.finish_reconciliation(sid, attempt_id)
            return {'ok': True, 'session_id': sid, 'engine': binding['engine'],
                    'binding_id': binding['binding_id'],
                    'committed_offset': self._history_seq(active['state'])}
        finally:
            if switch_acquired:
                switch_lock.release()
            turn_lock.release()

    @staticmethod
    def _engine_catalog(engine, catalog):
        """Shepherd-only capabilities layered on a worker catalog.

        Pi's compaction threshold is applied by Shepherd's own Pi launcher, so it is
        advertised here and not by the native Pi adapter.
        """
        if engine != 'pi' or not isinstance(catalog, dict):
            return catalog
        # Tiers are declared per model/route: only long-context routes get the menu.
        from shepherd.pi_compaction import annotate_pi_catalog
        return annotate_pi_catalog(catalog)

    @staticmethod
    def _engine_input_options(raw, *, inherited=False):
        """Separate engine settings from public Conversation metadata.

        Global engine defaults never carry Desk role.  A role explicitly saved
        on this Conversation remains valid.  Shepherd routing flags belong only
        to the public facade and are never native engine options.
        """
        value = dict(raw) if isinstance(raw, dict) else {}
        if inherited:
            value.pop('desk_role', None)
        for key in ('shepherd_mode','shepherd_trial','active_engine'):
            value.pop(key, None)
        return value

    @staticmethod
    def _engine_options(engine, worker, raw):
        value = worker.normalize_options(raw)
        if engine == 'pi':
            from shepherd.pi_compaction import normalize_pi_autocompact
            threshold = normalize_pi_autocompact((raw or {}).get('autocompact'))
            # Explicit "automatic" must override later global thresholds, just
            # like an explicit empty model overrides a global model selection.
            if threshold or (raw or {}).get('autocompact') == '':
                value['autocompact'] = threshold
        return value

    def options_catalog(self, sid=None, engine=None):
        selected = engine or (self.selected_engine(sid) if sid else '')
        worker = self._worker(selected) if selected else None
        return (self._engine_catalog(selected, worker.options_catalog())
                if worker else {'ui': self.ui_contract()})

    def options_for_session(self, sid, engine=None):
        selected = engine or self.selected_engine(sid)
        worker = self._worker(selected)
        return self._engine_catalog(selected, worker.options_for_session(sid)
                                    if hasattr(worker, 'options_for_session') else worker.options_catalog())

    def execution_name(self, sid=None):
        return self.selected_engine(sid) if sid else self.name

    def slash_commands_for_session(self, sid):
        """Do not advertise native commands that the facade cannot safely dispatch.

        A worker's cached list is engine-specific and may contain native session
        mutations. Shepherd accepts only product-owned commands with dedicated
        routes; those are declared by the backend command manifest.
        """
        self._check_namespace(sid)
        return []

    def _set_engine_model(self, engine, sid, model, since=None):
        model = str(model or '').strip()
        if not model:
            return
        with self._lock:
            row = self.index.get(sid)
            if row is None:
                return
            cut = ((row.get('engine_model_cuts') or {}).get(engine) or 0)
            if since is not None and since < cut:
                return
            row.setdefault('engine_models', {})[engine] = model
            if engine == self.session_engine(sid):
                row['model'] = model
            self._save_index()

    def config_label(self, sid=None, engine=None):
        if not sid:
            return 'Shepherd'
        selected = engine or self.selected_engine(sid)
        row = self.index.get(sid) or {}
        actual = str((row.get('engine_models') or {}).get(selected) or '')
        if not actual and selected == self.session_engine(sid):
            actual = str(row.get('model') or '')
        return selected + (' · ' + actual if actual else '')

    def saved_engine_options(self, sid, engine):
        saved = ((self.index.get(sid) or {}).get('engine_opts') or {}).get(engine) or {}
        return copy.deepcopy(saved)

    def session_options(self, sid, fallback=None, engine=None):
        selected = engine or self.selected_engine(sid)
        inherited = self._engine_input_options(fallback, inherited=True)
        saved = self._engine_input_options(self.saved_engine_options(sid, selected))
        worker = self._worker(selected)
        return self._engine_options(selected, worker, {**inherited, **saved})

    def execution_options(self, sid, fallback=None):
        """Options for the immutable Producer chosen at the turn boundary."""
        return self.session_options(sid, fallback, engine=self.session_engine(sid))

    def update_session_options(self, sid, patch, fallback=None, engine=None):
        selected = engine or self.selected_engine(sid)
        worker = self._worker(selected)
        value = self._engine_options(selected, worker, {
            **self._engine_input_options(fallback, inherited=True),
            **self._engine_input_options(self.saved_engine_options(sid, selected)),
            **self._engine_input_options(patch)})
        with self._lock:
            row = self.index.get(sid)
            if row is None: return None
            row.setdefault('engine_opts', {})[selected] = value
            if 'model' in (patch or {}):
                row.setdefault('engine_models', {}).pop(selected, None)
                row.setdefault('engine_model_cuts', {})[selected] = int(time.time() * 1000)
                if selected == self.session_engine(sid):
                    row.pop('model', None)
            self._save_index()
        self.bus.publish({'type':'session.settings','properties':{
            'sessionID':sid, 'engine':selected, 'selected_engine':self.selected_engine(sid),
            'opts':value,'model':self.config_label(sid, engine=selected)}})
        if 'model' in (patch or {}):
            self.refresh_environment(sid)
        return value

    def _claude_static_system(self, sid):
        worker = self._worker('claude')
        return worker.system_body(self.session_options(sid, engine='claude'))

    def _worker(self, engine):
        if engine not in ('claude', 'codex', 'pi'):
            raise Conflict('Shepherd Active Engine 无效')
        with self._worker_lock:
            worker = self._workers.get(engine)
            if worker is None:
                from engine_adapters import ClaudeAdapter, CodexAdapter, PiAdapter
                cls = {'claude':ClaudeAdapter,'codex':CodexAdapter,'pi':PiAdapter}[engine]
                worker = cls(self.data_home, self.workspace, self.bus)
                self._bind_worker(worker)
                self._workers[engine] = worker
            return worker

    def _bind_worker(self, worker):
        """Point a private protocol translator at the one public Conversation store."""
        worker.owner = self
        worker.dir = self.dir
        worker.index_path = self.index_path
        worker.index = self.index
        worker._lock = self._lock
        worker._live = self._live
        worker._turn_seq = self._turn_seq
        worker._turn_prev = self._turn_prev
        worker._msg_cache = self._msg_cache
        worker._msg_cache_lock = self._msg_cache_lock
        worker._compact_state = self._compact_state
        worker._bg_tasks = self._bg_tasks
        # Worker 自发的超时、连接失败和清理也必须发布 Shepherd 全局待答快照，
        # 不能只在外层主动响应路径聚合。
        worker._asks_provider = self._pending_ask
        # Native Session IDs remain hidden in Shepherd bindings. Protocol
        # translators may observe them, but must not publish them into the public
        # adapter index or the ordinary terminal/session registry.
        worker._set_engine_sid = lambda _sid, _native_sid: None
        worker._term_register = lambda _sid, _native_sid: None
        def record_model(sid, model, since=None, engine=worker.name):
            ctx = self._live.get(sid)
            if ctx is not None:
                ctx['actual_model'] = str(model or '').strip()
                self._checkpoint_live(ctx, force=True)
            self._set_engine_model(engine, sid, model, since)
        worker.set_session_model = record_model
        worker._checkpoint_live = self._checkpoint_live
        worker._record_compact = self._record_compact

    def _checkpoint_live(self, ctx, force=False):
        public = {key:value for key,value in ctx.items() if key != 'esid'}
        public['engine'] = str(ctx.get('producer_engine') or '')
        public['model'] = str(ctx.get('actual_model') or '')
        super()._checkpoint_live(public, force=force)
        if '_checkpoint_at' in public:
            ctx['_checkpoint_at'] = public['_checkpoint_at']

    def live_message(self, sid):
        ctx = self._live.get(sid)
        if not ctx:
            row = self._recovered_live_message(sid)
            if row: row.get('info', {}).pop('engine_sid', None)
            return self._presentation_live_message(sid, row)
        # Native process exit precedes source verification/commit. Only the
        # facade's _finish_turn may release the public running state.
        info = {'id':ctx['msg_id'], 'role':'assistant', 'sessionID':sid,
                'running':True, 'started':ctx.get('t0') or 0}
        if ctx.get('producer_engine'): info['engine'] = ctx['producer_engine']
        if ctx.get('actual_model'): info['model'] = ctx['actual_model']
        if ctx.get('compact_t0'): info['compacting'] = ctx['compact_t0']
        from .lifecycle import snapshot
        info.update(snapshot(self, ctx))
        if isinstance(ctx.get('environment'), dict):
            info['environment'] = copy.deepcopy(ctx['environment'])
        message = {'info':info, 'parts':copy.deepcopy([
            ctx['parts'][key] for key in ctx.get('order', []) if key in ctx['parts']])}
        return self._presentation_live_message(sid, message)

    def _connector(self, binding):
        if binding.get('connector_version','').startswith('unified-v1:'):
            return self.service.connector(binding)
        nonce = binding['ownership_nonce']
        if binding['engine'] == 'claude':
            return ClaudeConnector(binding, nonce, store_root=self.shepherd_root,
                                   ownership=self.service.coordinator.ownership)
        if binding['engine'] == 'pi':
            return PiConnector(binding, nonce, store_root=self.shepherd_root,
                               ownership=self.service.coordinator.ownership)
        return CodexFamilyConnector(binding, nonce, store_root=self.shepherd_root,
                                    ownership=self.service.coordinator.ownership)

    def build_cmd(self, _text, _engine_sid, _opts):
        return []

    def prompt_async(self, sid, text, opts=None, atts=None, **kwargs):
        self._require_execution(sid)
        peer_request_id = str(kwargs.get('peer_request_id') or '')
        if peer_request_id:
            # Compatibility callers must enter the sidecar before Shepherd's
            # ordinary request ledger claims or persists any user payload.
            if atts or kwargs.get('requested_engine'):
                raise Conflict('隔离问询只接受文本且不能切换目标引擎')
            return super().prompt_async(sid, text, opts=opts, atts=[], **kwargs)
        rid = str(kwargs.get('client_request_id') or 'request_' + uuid.uuid4().hex)
        target = kwargs.pop('requested_engine', '')
        if self._shutting_down:
            raise Conflict('Desk 正在退出，消息未发送')
        # Claim before the peer fence so a concurrent transport retry can return
        # the existing receipt instead of conflicting with its own first send.
        self.recover_pending(sid, trigger='admission_before', blocking=False)
        receipt, fresh = self.requests.claim(sid, rid, {
            'text':text, 'attachments':atts or [], 'engine':target})
        if not fresh:
            return receipt
        own_peer_fence = not bool(kwargs.get('_peer_admitted'))
        token = kwargs.get('_peer_fence_token')
        if not isinstance(token, dict):
            token = {'owned': False, 'transferred': False}
            kwargs['_peer_fence_token'] = token
        try:
            if own_peer_fence:
                kwargs['_peer_prepare_run'] = self._peer_user_admit(
                    sid, kwargs.get('confirmed_peer_cancel'))
                kwargs['_peer_admitted'] = True
                token['owned'] = True
            return self._accept_prompt(sid, text, opts, atts, rid, target, kwargs)
        except Exception as exc:
            self.requests.update(sid, rid, 'rejected', reason=str(exc))
            raise
        finally:
            if own_peer_fence and token.get('owned') and not token.get('transferred'):
                token['owned'] = False
                self._peer_user_release(sid)

    def _model_patch_limited(self, sid, target, opts):
        producer = self.session_engine(sid)
        engine = target or producer
        route = {}
        if engine == 'pi':
            if target and target != producer:
                saved = self.saved_engine_options(sid, engine)
                if saved:
                    route = self._engine_options(engine, self._worker(engine), saved)
                else:
                    from workbench import _wb_state_read
                    defaults = ((_wb_state_read().get('engine_opts') or {}).get(engine) or {})
                    route = self._engine_options(
                        engine, self._worker(engine),
                        self._engine_input_options(defaults, inherited=True))
            else:
                route = self.session_options(sid, opts, engine=engine)
        return model_patch_limited(engine, route.get('model'), route.get('provider'))

    def _accept_prompt(self, sid, text, opts, atts, rid, target, kwargs):
        # Native slash commands mutate one engine's private session metadata and
        # cannot be represented as a portable Shepherd Turn; '/compact' is also
        # the runners' internal compaction sentinel. Product-owned commands use
        # dedicated endpoints and never reach this prompt path. Only a whole
        # command token is refused: a lone '/', paths and other text that merely
        # begins with '/' are ordinary prompts.
        command = command_token(text)
        if command:
            raise Conflict(f'{command} 是斜杠命令，Shepherd 不把它作为消息转交原生引擎；'
                           '可用命令见输入框的“/”补全')
        # Validate before accepting a turn or persisting its user bubble. The
        # ordinary HTTP rejection path preserves the caller's text/attachments.
        service = getattr(self, 'service', None)
        coordinator = getattr(service, 'coordinator', None)
        active_turn = (self._active_turn(coordinator.state(sid))
                       if coordinator is not None else {})
        if active_turn.get('recovery_state') == 'blocked':
            raise Conflict('Shepherd 上轮来源无法安全验证；请先隔离无法验证内容并继续')
        if active_turn:
            raise Conflict('Shepherd 上轮仍在恢复，暂不能发送新消息')
        patch_limited = self._model_patch_limited(sid, target, opts)
        images = []
        for attachment in atts or []:
            if attachment.get('type') == 'image':
                try:
                    data = attachment.get('data_b64')
                    mime = attachment.get('media_type') or 'image/png'
                    images.append(image_block(data, mime))
                    if patch_limited:
                        info = model_image_info(data, mime)
                        if info['patches'] > MODEL_IMAGE_PATCH_LIMIT:
                            raise ImageInputError(
                                f'图片为 {info["width"]}×{info["height"]}，处理后需要 '
                                f'{info["patches"]} 个图像块，超过 {MODEL_IMAGE_PATCH_LIMIT} 上限；'
                                '请缩小或切成多张后重试')
                except ImageInputError:
                    raise
                except ValueError as exc:
                    raise ImageInputError(str(exc)) from exc
        # Reject the aggregate multimodal request before persisting its bubble
        # or switching engines. The durable writer rechecks the final projection
        # (including generated attachment context) before reserving/dispatching.
        context = request_context(atts)
        TurnInputs.validate_public(TurnInputs.public_input(text, images, context))
        if images and (target or self.session_engine(sid)) == 'claude':
            from shepherd.image_content import native_content
            from shepherd.image_transport import ClaudeImageTransport
            transport = ClaudeImageTransport()
            content = transport.content(native_content(images, 'claude'))
            transport.validate_request([], {'type':'user', 'message':{
                'role':'user', 'content':[{'type':'text', 'text':str(text or '') + context}, *content]}})
        if target:
            # The accepted worker waits for the captured Ask snapshot, then
            # switches under the same prompt fence before taking turn_guard.
            kwargs['_before_dispatch'] = lambda: self._switch_engine_fenced(sid, target)
        kwargs['client_request_id'] = rid
        super().prompt_async(sid, text, opts=opts, atts=atts, **kwargs)
        return self.requests.get(sid, rid)

    def request_status(self, sid, rid):
        receipt = self.requests.get(sid, rid)
        if receipt['status'] in ('preparing', 'started', 'uncertain'):
            # Never reconcile the gap between request linking and reservation
            # while its runner still owns turn_guard.
            try:
                self.recover_pending(sid, trigger='request_status', blocking=False)
            except Exception as exc:
                self._receipt_settle_failed(sid, exc)
        return self.requests.get(sid, rid)

    def cancel_unsent_delivery(self, sid, rid):
        """Cancel an accepted internal notification before it reaches a runner."""
        return self.requests.cancel(sid, rid, only_before_start=True)

    def _run_serial(self, sid, text, opts=None, atts=None, refresh_opts=None,
                    peer_request_id='', peer_head='', requested_at=None, client_request_id='',
                    _peer_fence_token=None, _before_dispatch=None):
        self._request_local.rid = client_request_id
        error = ''
        try:
            return super()._run_serial(sid, text, opts, atts, refresh_opts,
                peer_request_id, peer_head, requested_at, client_request_id,
                _peer_fence_token, _before_dispatch)
        except Exception as exc:
            error = str(exc)
            if client_request_id and getattr(exc, 'code', None) in (
                    'invalid_image', 'invalid_attachment', 'image_transport_too_large'):
                self.requests.update(sid, client_request_id, 'rejected', reason=error)
            raise
        finally:
            try:
                if client_request_id:
                    self.requests.settle(sid, client_request_id,
                        self.service.coordinator.state(sid) if hasattr(self.service,'coordinator') else {}, error=error)
            except Exception as exc:
                self._receipt_settle_failed(sid, exc)
            finally:
                self._request_local.rid = ''
            # super()._run_serial has released turn_guard at this point. A
            # terminal source that failed only during readback can now finish
            # without waiting for another user action.
            if not self._shutting_down:
                try:
                    self.recover_pending(sid, trigger='turn_finished', blocking=False)
                except Exception as exc:
                    self._receipt_settle_failed(sid, exc)
                self._schedule_storage_promotion(sid, .05)

    def _run(self, sid, text, opts=None, atts=None):
        rid = getattr(self._request_local, 'rid', '')
        if self._shutting_down:
            raise Conflict('Desk 正在退出，消息未发送')
        if rid:
            self.requests.update(sid, rid, 'preparing')
        return super()._run(sid, text, opts, atts)

    def abort(self, sid, recall=False, client_request_id=''):
        self._check_namespace(sid)
        receipt = self.requests.cancel(sid, client_request_id)
        # A user stop is a session-level child-work barrier. Apply it before the
        # pre-dispatch early return too: otherwise cancelling one queued child
        # notification merely admits the next late result one second later.
        if (not client_request_id or not receipt or receipt.get('status') in
                ('cancelled','preparing','started','uncertain')):
            from pi_children import cancel_session
            cancel_session(self.dir, sid, suppress_pending=True)
        if receipt and receipt['status'] == 'cancelled':
            return {'state':'ready', 'generation':self.turn_state(sid).get('generation',0),
                    'request':receipt}
        if client_request_id and receipt and receipt['status'] not in ('preparing','started','uncertain'):
            return {'request':receipt, **self.turn_state(sid)}
        result = super().abort(sid, recall=recall)
        if result is None and receipt:
            return {'request':receipt, 'state':'cancelling',
                    'generation':self.turn_state(sid).get('generation', 0)}
        return result

    def shutdown(self, timeout=4):
        """Stop admission and owned runners; recovery, never replay, finishes commits."""
        from pi_children import close_for
        if self._shutting_down:
            close_for(self.dir)
            return
        self._shutting_down = True
        self._peer_cancel_all()
        self._recovery.stop()
        for sid in list(self._prepare_timers):
            self._cancel_engine_prepare(sid)
        with self._lock:
            promotions = list(self._promotion_timers.values())
            self._promotion_timers.clear()
        for timer in promotions:
            timer.cancel()
        contexts = list(self._live.items())
        for sid, ctx in contexts:
            ctx['aborted'] = 'interrupt'
            self.abort(sid)
        deadline = time.monotonic() + timeout
        while any(self._live.get(sid) is ctx for sid, ctx in contexts) and time.monotonic() < deadline:
            time.sleep(.05)
        # Runner shutdown can race the async-started event. Keep observation alive
        # through that bounded wait, then perform one final scoped stop scan.
        close_for(self.dir)
        # Runner cancellation already has its own targeted hard timeout. Do not
        # fabricate a completed/interrupted terminal or mutate native evidence.

    def _stash(self, sid, atts):
        try:
            saved = super()._stash(sid, atts)
        except OSError as exc:
            raise ImageInputError('附件保存失败，消息未发送，请检查磁盘后重试') from exc
        if len(saved) != len(atts or []):
            raise ImageInputError('附件未完整保存，消息未发送，请检查附件后重试')
        return saved

    def _observe_runner_stall(self, ctx, payload):
        from .lifecycle import _current
        # Codex dispatches observation callbacks away from its deadline loop.
        # Keep sequence validation, state mutation, persistence and publication
        # atomic so a delayed older callback cannot overwrite recovery.
        lock = ctx.setdefault('_runner_stall_lock', threading.Lock())
        with lock:
            if not _current(self, ctx):
                return
            try:
                sequence = int((payload or {}).get('sequence') or 0)
            except (TypeError, ValueError):
                return
            if sequence <= int(ctx.get('_runner_stall_sequence') or 0):
                return
            ctx['_runner_stall_sequence'] = sequence
            active = bool((payload or {}).get('active'))
            if active:
                seconds = max(0, int((payload or {}).get('seconds') or 0))
                native = (payload or {}).get('activity') or {}
                phase = native.get('phase') if native.get('phase') == 'first_response' else ''
                ctx['_runner_stall'] = {
                    'since':time.time() - seconds,
                    'active_tools':max(0, int(native.get('active_tools') or 0)),
                    **({'phase':phase} if phase else {})}
            else:
                ctx.pop('_runner_stall', None)
            self._checkpoint_live(ctx, force=True)
            activity = self.turn_activity(ctx['sid'], ctx)
            self.bus.publish({'type':'session.stalled','properties':{
                'sessionID':ctx['sid'], 'generation':int(ctx.get('seq') or 0),
                'seconds':activity['seconds'] if activity['kind']=='no_progress' else 0,
                'activity':activity}})

    def _observe_execution_review(self, ctx, payload):
        from .lifecycle import _current
        if not _current(self, ctx) or not isinstance(payload, dict):
            return
        allowed = ('status', 'started_at', 'next_review_at', 'absolute_deadline',
                   'ack_deadline', 'review_sequence', 'review_id', 'error')
        review = {key:payload[key] for key in allowed if key in payload}
        ctx['_execution_review'] = review
        self._checkpoint_live(ctx, force=True)
        self.bus.publish({'type':'session.execution_review','properties':{
            'sessionID':ctx['sid'], 'generation':int(ctx.get('seq') or 0),
            'review':review}})

    def _turn_dispatch(self, ctx, _cmd, _env, _uframe):
        from shepherd.phase_timing import PhaseTiming
        timing = ctx.get('_response_timing')
        phases = PhaseTiming(lambda row: self._trace(ctx['sid'], {
            **row, 'seq': ctx.get('seq')}), started=getattr(timing, 'started', None))
        phases.mark('adapter.enter')
        phases.call('adapter.require_execution', self._require_execution, ctx['sid'])
        rid = getattr(self._request_local, 'rid', '')
        if self._shutting_down or (rid and self.requests.cancelled(ctx['sid'], rid)):
            ctx['aborted'] = 'interrupt'
            return 'Shepherd 回合在派发前已取消'
        active = phases.call('adapter.active', self.service.active, ctx['sid'])
        binding, lease = active['binding'], active['lease']
        if binding is None or lease is None:
            raise Conflict('Shepherd Conversation 没有 Active Producer')
        worker = phases.call('adapter.worker', self._worker, binding['engine'])
        ctx['producer_engine'] = binding['engine']
        if ctx.get('aborted'):
            return 'Shepherd 回合在派发前已取消'
        connector = phases.call('adapter.connector', self._connector, binding)
        opts = dict(ctx.get('opts') or {})
        # Derive one verified Working Context budget for both timeout sizing and
        # full-history admission. Prefer the model catalog; Codex model/list does
        # not always advertise a window, so an explicit autocompact threshold is
        # the conservative fallback. If neither exists, zero means unknown:
        # unified runners retain native admission, never infer model capacity.
        try:
            from shepherd.execution_budget import runner_timeouts
            opts['_context_window'] = runner_timeouts(
                opts, phases.call('adapter.catalog', worker.options_catalog))['context_window']
        except Exception:
            opts['_context_window'] = 0
        static_system = phases.call('adapter.system', worker.system_body, opts)
        dynamic_grounding = str(opts.get('_grounding') or '').strip()
        if binding['engine'] == 'pi' and not dynamic_grounding:
            raise Conflict('Pi 系统约定构造失败，已停止启动')
        # Unified hosts bind the current context to the native system/developer
        # carrier. Its hash participates in host identity.
        unified = getattr(connector, 'unified', False) is True
        system = worker._turn_system_prompt(opts)
        if unified:
            opts['_context_carrier'] = 'system'
        elif binding['engine'] == 'claude':
            # Legacy private stores have immutable prompt snapshots. Preserve
            # their audited hidden-context receipt until explicitly migrated.
            from context_delivery import system_context
            system = static_system
            opts['_grounding'] = system_context(opts)
        base_env = _env
        if binding['engine'] == 'claude':
            # `_env` begins with the Desktron backend environment. After a restart
            # initiated inside Claude, that can contain the parent turn's
            # temporary config/messaging identity. Do not forward it into the
            # new private Binding; explicit Desktron/provider settings remain.
            from shepherd.runners import clean_claude_parent_env
            base_env = clean_claude_parent_env(base_env)
        env = {**base_env, **phases.call('adapter.spawn_env', worker.spawn_env, opts),
               'DESKTRON_ENGINE': binding['engine'],
               'DESKTRON_PUBLIC_ENGINE': self.name}
        if binding['engine'] == 'claude':
            # 一年期令牌优先于钥匙串副本，避免常驻进程手里的复制令牌到期（见 claude_token.py）。
            import claude_token, deskcfg
            env = claude_token.apply(env, log=deskcfg.log)
        def on_process(process):
            ctx.update(proc=process)
            if self._shutting_down or ctx.get('aborted') or (rid and self.requests.cancelled(ctx['sid'], rid)):
                raise Conflict('Shepherd 请求在派发前已取消')
            # App Control authenticates both the per-conversation capability and
            # the kernel-observed caller ancestry.  Ordinary adapters register
            # their engine process in _note_proc; Shepherd owns the process via
            # its private runner, so it must register the public Conversation
            # here as well.  Keep native Binding IDs private.
            self._note_proc(ctx['sid'], process)
        from shepherd.unified_runners import UnifiedRunner
        if unified:
            from shepherd.coordinator import digest
            # Pi RPC has a verified same-turn `steer` queue. Other public engines keep
            # their existing deadlines until an equivalent contract is wired.
            opts['_execution_review'] = binding['engine'] == 'pi'
            # Compatibility lookup only. Current host identity hashes the full system.
            opts['_checkpoint_system_hash'] = digest(static_system)
            # Mixed debug traces retain their legacy short hashes. Privacy-safe performance
            # telemetry receives the raw values only in memory and HMACs them with a local key.
            ctx['_usage_binding_hash'] = digest(str(binding['binding_id']))[:12]
            ctx['_usage_binding_value'] = str(binding['binding_id'])
            ctx['_performance_telemetry'] = self.performance
            ctx['_usage_model'] = str(opts.get('model') or '')
            ctx['_usage_provider'] = (str(opts.get('provider') or '') or
                                      (ctx['_usage_model'].split('/', 1)[0]
                                       if '/' in ctx['_usage_model'] else ''))
        from .shepherd_events import EventProjection
        def projection_warning(detail):
            self._ctx_env_publish(ctx, {'state':'warning', 'stage':'event_projection',
                'scope':'turn', 'label':'部分过程信息显示不完整，可查阅会话归档',
                'details':[f"{detail['engine']} / {detail['event']}: {detail['exception']}"]})
        project = EventProjection(binding['engine'], worker, ctx, projection_warning)
        observe_stall = lambda payload: self._observe_runner_stall(ctx, payload)
        observe_review = lambda payload: self._observe_execution_review(ctx, payload)
        if binding['engine'] == 'claude':
            runner = phases.call('adapter.runner_create',
                UnifiedRunner if unified else ClaudeRunner, connector, on_event=project,
                on_permission=lambda request: self._ask_claude_permission(ctx['sid'], request),
                options=opts, system_text=system,
                runtime_roots=worker._session_roots(ctx['sid'], opts),
                env=env, on_process=on_process, on_stall=observe_stall,
                user_frame=phases.call('adapter.user_frame', worker.build_user_frame,
                    ctx.get('text') or '', ctx.get('saved') or []))
        elif binding['engine'] == 'codex':
            runner = phases.call('adapter.runner_create',
                UnifiedRunner if unified else CodexFamilyRunner, connector,
                on_event=project,
                on_approval=lambda message, conn: worker._approval_ask(ctx['sid'], message, conn),
                options=opts, system_text=system,
                runtime_roots=worker._session_roots(ctx['sid'], opts), env=env,
                on_process=on_process, on_stall=observe_stall,
                image_paths=[x[1] for x in ctx.get('saved') or [] if x[0] == 'image'])
        else:
            runner = phases.call('adapter.runner_create',
                UnifiedRunner if unified else PiRunner, connector,
                on_event=project,
                on_process=on_process, on_stall=observe_stall,
                on_review=observe_review if unified else None,
                options=opts, system_text=system, env=env,
                command=phases.call('adapter.command', worker.build_cmd, ctx.get('text') or '', None, opts),
                user_frame=phases.call('adapter.user_frame', worker.build_user_frame,
                    ctx.get('text') or '', ctx.get('saved') or []))
        if unified:
            ctx['_usage_system_hash'] = runner.system_prompt_hash()[:16]
            ctx['_usage_system_value'] = runner.system_text
            if binding['engine'] == 'pi':
                runner.on_runtime = lambda payload: ctx.update(
                    _usage_resident_reused=bool((payload or {}).get('resident_reused')))
            runner.on_native_context_drop = lambda: self._archive_native_children(
                ctx['sid'], binding['binding_id'])
            def context_retry():
                for key in ('engine_error','engine_failure','native_terminal',
                            '_engine_error_published','_close_stdin'):
                    ctx.pop(key,None)
                self._ctx_env_publish(ctx, {'state':'working','stage':'context_recovery',
                    'scope':'turn','label':'上下文已被原生引擎拒绝，已缩减历史并安全重派（1/1）'})
            runner.on_context_retry=context_retry
        runner.phase_timing = phases
        phases.mark('adapter.ready')
        ctx['_shepherd_runner'] = runner
        if ctx.get('aborted'):
            return 'Shepherd 回合在派发前已取消'
        from .response_timing import mark as timing_mark
        runner.on_timing_dispatched = lambda: timing_mark(ctx, 'submitted_ms')
        harness = ExecutionHarness(self.service.coordinator, connector, runner)
        if self._active_turn(active['state']):
            phases.call('harness.recover_prior', harness.recover,
                ctx['sid'], lease_id=lease['lease_id'], generation=lease['generation'])
        if rid:
            phases.call('harness.request_start', self.requests.update,
                ctx['sid'], rid, 'started', attempt_id='attempt_' + str(ctx.get('seq') or ''))
        input_public = TurnInputs.public_input(ctx.get('_user_text'), [
            image_block(saved[4], saved[3] or 'image/png')
            for saved in ctx.get('saved') or [] if saved[0] == 'image' and saved[4]],
            ctx.get('_attached_context'))
        row = harness.run(
            ctx['sid'], lease_id=lease['lease_id'], generation=lease['generation'],
            prompt=ctx.get('text') or '', attempt_id='attempt_' + str(ctx.get('seq') or ''),
            input_public=input_public)
        if row.get('compatibility'):
            self._publish_recovery_compatibility({
                'conversation_id':ctx['sid'], 'compatibility':row['compatibility']})
        image_transport = getattr(runner, 'image_transport', None)
        notice = image_transport_notice(image_transport)
        if notice is not None:
            self._ctx_env_publish(ctx, notice)
        history_recovery=getattr(runner,'history_recovery',None)
        if row['terminal_state']=='completed' and isinstance(history_recovery,dict):
            self._env_publish(ctx['sid'], {'state':'ready','stage':'history_recovery',
                'scope':'environment','label':'已自动恢复，可以继续',
                'details':['本轮历史及原始记录已保留；下轮将从已验证历史重建上下文，未重跑工具。']})
        elif row['terminal_state']=='completed':
            self._env_publish(ctx['sid'], {'state':'ready','stage':'history_recovery'})
        degraded=getattr(runner,'degraded_context',None)
        if row['terminal_state']=='completed' and isinstance(degraded,dict):
            omitted=int(degraded.get('omitted_rounds') or 0)
            detail='已使用有界历史摘要和近期回合继续'
            if omitted:detail+=f'；另有 {omitted} 个较早回合未带入目标模型'
            self._env_publish(ctx['sid'], {'state':'ready','stage':'context_degraded',
                'scope':'environment','label':'目标模型窗口较小，已使用降级上下文',
                'details':[detail]})
        elif row['terminal_state']=='completed':
            self._env_publish(ctx['sid'], {'state':'ready','stage':'context_degraded'})
        if unified and isinstance(runner.context_retries,int) and runner.context_retries>0:
            self._ctx_env_publish(ctx, {'state':'ready','stage':'context_recovery'})
        ctx.pop('esid', None)
        if row['terminal_state'] != 'completed':
            return (getattr(runner,'context_retry_error','') or ctx.get('engine_error')
                    or binding['engine'] + ' 回合未完成')
        return None

    def _soft_interrupt(self, sid, proc):
        ctx = self._live.get(sid) or {}
        runner = ctx.get('_shepherd_runner')
        with self._shepherd_approval_lock:
            for slot in self._shepherd_approvals.values():
                if slot['sid'] == sid:
                    slot['result'] = {'allow':False, 'message':'用户已中断'}
                    slot['done'].set()
        worker = self._workers.get(self.session_engine(sid))
        if worker and hasattr(worker, '_clear_session_approvals'):
            worker._clear_session_approvals(sid)
        if worker and worker.name == 'pi':
            for ask in worker._pending_ask(sid):
                worker.respond_permission(ask['id'], False, resolution='cancelled')
        return runner.cancel() if runner else True

    def _pending_ask(self, sid):
        """Best-effort global approval snapshot; never reverse an already-settled decision."""
        try:
            with self._shepherd_approval_lock:
                pending = [copy.deepcopy(slot['props']) for slot in self._shepherd_approvals.values()
                           if slot['sid'] == sid and not slot['done'].is_set()]
        except Exception:
            pending = []
        try:
            with self._worker_lock:
                workers = list(self._workers.values())
        except Exception:
            workers = []
        for worker in workers:
            try:
                pending.extend(worker._pending_ask(sid))
            except Exception:
                continue
        return pending

    def _turn_engine(self, sid, ctx):
        # 本回合实际派发到的 Producer；派发前就取消的回合退回当前绑定。选择器上的
        # selected/next_engine 只代表下一轮，不能拿来标注这一轮。
        engine = str(ctx.get('producer_engine') or self.session_engine(sid) or '')
        return engine if engine in ('claude', 'codex', 'pi') else ''

    def _clear_turn_error_environment(self, sid):
        with self._lock:
            row = self.index.get(sid) or {}
            issues = list((row.get('environment_issues') or {}).values())
            if not issues and row.get('environment'):
                issues = [row['environment']]
        # Older Codex-family failures used stage=engine. Resolve that exact
        # issue, including when a higher-priority authentication notice hides it.
        for issue in issues:
            legacy = (issue.get('stage') == 'engine'
                      and self.session_engine(sid) == 'codex')
            if legacy and not issue.get('action'):
                self._env_publish(sid, {'state':'ready', 'stage':'engine',
                    'generation':issue.get('generation', 0), 'label':'继续执行'})
        super()._clear_turn_error_environment(sid)

    def _receipt_settle_failed(self, sid, exc):
        # Diagnostics may hit the same full/unavailable disk as the receipt.
        # Neither logging nor index projection may interrupt fence cleanup.
        try:
            from deskcfg import log
            log('shepherd', f'回执收口失败 {sid}: {type(exc).__name__}')
        except Exception:
            pass
        try:
            self._env_publish(sid, {'state':'warning', 'stage':'recovery',
                'label':'Shepherd 请求回执尚未收口，正在核验；消息不会自动重跑'})
        except Exception:
            pass

    def _before_turn_ready(self, sid, ctx):
        """Persist the receipt under turn_guard before any ready is visible."""
        rid = str(getattr(self._request_local, 'rid', '') or ctx.get('client_request_id') or '')
        if not rid:
            return None  # Manual compaction has its own operation receipt.
        try:
            self.requests.settle(sid, rid, self.service.coordinator.state(sid))
            receipt = self.requests.get(sid, rid)
        except Exception as exc:
            self._receipt_settle_failed(sid, exc)
            return {'request':{'request_id':rid, 'status':'uncertain',
                               'reason':'receipt_settle_failed'}}
        if receipt['status'] in ('accepted', 'preparing', 'started', 'uncertain', 'not_found'):
            return {'request':receipt}
        return None

    def _finish_turn(self, sid, ctx, abnormal):
        import self_update
        # Native IDs belong to Bindings, never the public transcript/fork anchor.
        ctx.pop('esid', None)
        for ask in self._pending_ask(sid):
            self.respond_permission(ask['id'], False, resolution='cancelled')
        not_started = str(ctx.pop('_prompt_not_started', '') or '')
        if not_started:
            # The user bubble was persisted before the native dispatch fence.
            # Restore it instead of saving a fabricated failed assistant turn.
            self._pop_last_user_record(sid)
            ctx['aborted'] = 'recall'
            result = super()._finish_turn(sid, ctx, None)
            request_id = str(getattr(self._request_local, 'rid', '') or '')
            self._env_publish(sid, {'state':'warning', 'stage':'native_pre_dispatch',
                                   'label':'消息未发送：' + self._notice_head(not_started),
                                   'details':[not_started]})
            self.bus.publish({'type':'session.error','properties':{
                'sessionID':sid, 'requestID':request_id,
                'error':{'message':not_started,'code':'prompt_not_started',
                         'stage':'native_pre_dispatch'}}})
        else:
            result = super()._finish_turn(sid, ctx, abnormal)
        state = self.service.coordinator.state(sid)
        if state and self._active_turn(state).get('recovery_state') == 'blocked':
            self._env_publish(sid, {'state':'warning', 'stage':'recovery',
                'label':'Shepherd 上轮原生终态未确认，已阻止自动重跑；需要核验私有执行记录'})
        elif state:
            self._clear_recovery_notice_if_resolved(sid, state)
        # 当前回合释放串行锁后，防抖准备最新选择；UI 与配置选择早已即时生效。
        if (not self._shutting_down and not self_update.external_draining()
                and (self.index.get(sid) or {}).get('next_engine')):
            self._schedule_engine_prepare(sid)
        if (state and state.get('turns') and not self._active_turn(state)
                and not not_started and not self._shutting_down):
            from peer_context import refresh_after_settle
            refresh_after_settle(self.shepherd_root, sid)
        return result

    def _record_compact(self, ctx, props):
        """Keep the UI boundary in the durable output, never the native summary.

        Capture order at start, not at completion: output arriving during the
        operation must stay after the boundary on reload. The ordinary live
        session.compact event still owns rendering, so no second part event is
        emitted. Checkpoint and turn settlement persist this same parts list.
        """
        # Manual operations have their own verified checkpoint/receipt and row.
        if str(ctx.get('text') or '').strip() == '/compact':
            return
        if props.get('running'):
            if '_compact_anchor' not in ctx:
                ctx['_compact_anchor'] = len(ctx['order'])
            return
        anchor = ctx.pop('_compact_anchor', None)
        result = props.get('result')
        if (anchor is None or props.get('error')
                or isinstance(result, str) and result in (
                    'failed', 'cancelled', 'noop', 'retrying', 'unknown')
                or not (props.get('trigger') or props.get('pre')
                        or props.get('post') or result)):
            return
        ctx['_compact_seq'] = int(ctx.get('_compact_seq') or 0) + 1
        pid = ctx['msg_id'] + '_compact_' + str(ctx['_compact_seq'])
        props['trigger'] = props.get('trigger') or 'auto'
        meta = {key:copy.deepcopy(props[key]) for key in ('trigger', 'pre', 'post', 'ms')
                if props.get(key)}
        ctx['parts'][pid] = {'id':pid, 'type':'compact', 'text':'', 'compact':meta,
                             'messageID':ctx['msg_id'], 'sessionID':ctx['sid']}
        ctx['order'].insert(anchor, pid)

    def _persist_manual_compact_marker(self, sid, seq):
        """Persist the verified manual boundary after its /compact command row."""
        seq = int(seq or 0)
        mid = 'msg_compact_' + str(seq)
        def append_once(messages):
            if any((message.get('info') or {}).get('id') == mid for message in messages):
                return messages
            command_seq = seq
            for message in reversed(messages):
                info = message.get('info') or {}
                text = ''.join(str(part.get('text') or '') for part in message.get('parts') or []
                               if part.get('type') == 'text').strip()
                if info.get('role') == 'user' and not info.get('compact') and text == '/compact':
                    command_seq = int(info.get('seq') or seq)
                    break
            row = {'info': {'id':mid, 'role':'user', 'sessionID':sid, 'seq':command_seq,
                            'at':int(time.time() * 1000),
                            'compact':{'trigger':'manual'}},
                   'parts':[]}
            return [*messages, row]
        self._rewrite_msgs(sid, transform=append_once)
        self._trim_store(sid)

    def handle_event(self, _obj, _ctx):
        raise NotImplementedError

    def _ask_claude_permission(self, sid, request):
        request_id = str(request.get('id') or '')
        if not request_id:
            return {'allow':False, 'message':'权限请求缺少 ID'}
        done = threading.Event(); slot = {'done':done, 'result':None, 'sid':sid}
        props = {'id':request_id, 'sessionID':sid,
                 'tool':str(request.get('tool') or 'tool'),
                 'input':request.get('input') or {},
                 'reason':str(request.get('title') or request.get('description')
                              or '是否允许执行该操作？')}
        slot['props'] = props
        with self._shepherd_approval_lock:
            self._shepherd_approvals[request_id] = slot
        self.bus.publish({'type':'permission.asked','properties':props})
        ctx = self._live.get(sid) or {}
        deadline = time.monotonic() + self.PERMISSION_TIMEOUT
        while not done.wait(.1):
            proc = ctx.get('proc')
            if (ctx.get('aborted') or (proc is not None and proc.poll() is not None)
                    or time.monotonic() >= deadline):
                self.respond_permission(request_id, False, resolution='timeout')
                break
        with self._shepherd_approval_lock:
            self._shepherd_approvals.pop(request_id, None)
        return slot['result'] or {'allow':False, 'message':'权限请求已超时'}

    def respond_permission(self, request_id, allow, answers=None, response='', *, resolution=''):
        native_sid = ''
        with self._shepherd_approval_lock:
            pending = self._shepherd_approvals.get(str(request_id or ''))
            if pending:
                updated = None
                if isinstance(answers, dict) and isinstance(answers.get('updatedInput'), dict):
                    updated = answers['updatedInput']
                elif answers or response:
                    updated = dict(pending['props']['input'])
                    if answers: updated['answers'] = answers
                    if response: updated['response'] = response
                pending['result'] = {'allow':bool(allow),
                                     'updatedInput':updated,
                                     'message':str(response or ('用户拒绝了该操作' if not allow else ''))}
                pending['done'].set()
                native_sid = pending['sid']
        if native_sid:
            try:
                self.bus.publish({'type':'permission.resolved','properties':{
                    'sessionID':native_sid,'id':request_id,
                    'resolution':resolution or ('approved' if allow else 'denied'),
                    'asks':self._pending_ask(native_sid)}})
            except Exception:
                pass
            return True
        with self._worker_lock:
            workers = list(self._workers.values())
        for worker in workers:
            try:
                handled = worker.respond_permission(
                    request_id, allow, answers, response, resolution=resolution,
                    asks_provider=self._pending_ask)
            except Exception:
                continue
            if handled:
                return True
        return False

    def _tombstone_and_purge(self,sid):
        from shepherd.physical_delete import is_deleted, mark_deleted, purge
        if is_deleted(self.shepherd_root, sid):
            state = self.service.store.load_raw(sid, allow_deleted=True)
            tombstone = {'state':'deleted','reason':'user_delete'}
        else:
            tombstone = self.service.coordinator.tombstone_for_delete(sid)
            state = self.service.store.load_raw(sid, allow_deleted=True)
            mark_deleted(self.shepherd_root, sid)
        purge(self.shepherd_root, sid, state or {
            'conversation_id':sid, 'bindings':{}, 'pending_switch':None})
        return tombstone

    def delete_session(self, sid):
        # Fence peer registration before observing/cancelling the current owner;
        # otherwise a new ask could register in the wait→purge gap.
        sid = self.own_sid(sid)
        self._check_namespace(sid)
        with self._lock:
            if sid not in self.index:
                return
        self._peer_fence_enter(sid)
        try:
            return self._delete_session_peer_fenced(sid)
        finally:
            self._peer_fence_exit(sid)

    def _delete_session_peer_fenced(self, sid):
        # Locks come first so no new prompt or switch can enter between the
        # Writer check, durable delete marker and physical content purge.
        peer_run = self._peer_cancel(sid, reason="目标会话正在删除，隔离问询已取消")
        with self._lock:
            if sid not in self.index:
                return
            turn_guard = self._turn_locks.setdefault(sid, threading.Lock())
            switch_guard = self._requested_switch_locks.setdefault(sid, threading.Lock())
        # Peer projection/model work intentionally runs outside `turn_guard` so
        # real messages have priority.  Deletion must instead wait for the actual
        # tracked worker to exit; a stuck source read fails boundedly and cannot
        # race physical purge.
        if peer_run is not None and not peer_run.finished.wait(3):
            raise Conflict('Shepherd 会话仍在执行或切换，不能删除')
        acquired = turn_guard.acquire(timeout=0)
        if not acquired:
            raise Conflict('Shepherd 会话仍在执行或切换，不能删除')
        switch_acquired = False
        try:
            switch_acquired = switch_guard.acquire(blocking=False)
            if not switch_acquired:
                raise Conflict('Shepherd 会话仍在执行或切换，不能删除')
            try:
                self._tombstone_and_purge(sid)
            except Exception as exc:
                # The marker already fences execution and delayed delivery, but
                # deletion is not successful until every private byte is gone.
                # Keep the public row so the same user action can retry purge;
                # startup also resumes it from the retained state manifest.
                from shepherd.physical_delete import is_deleted
                if not is_deleted(self.shepherd_root, sid):
                    raise
                self._recovery.cancel_conversation(sid)
                raise Conflict('私有内容尚未完全清理，会话未删除；请重试') from exc
            with self._lock:
                timer = self._prepare_timers.pop(sid, None)
                promotion = self._promotion_timers.pop(sid, None)
                self._selection_epochs.pop(sid, None)
            if timer:
                timer.cancel()
            if promotion:
                promotion.cancel()
            self._recovery.cancel_conversation(sid)
            self.compact_operations.delete_public(sid)
            # Shepherd already owns peer, turn and switch fences here; calling
            # the public base delete wrapper would re-acquire turn_guard.
            super()._delete_session_peer_fenced_base(sid)
        finally:
            if switch_acquired:
                switch_guard.release()
            turn_guard.release()


    @staticmethod
    def _is_product_command(text):
        return str(text or '').strip() == '/compact'

    def compact_available(self, sid):
        """Only engines with an owned, restorable compact snapshot advertise it."""
        self._check_namespace(sid)
        try:
            binding = self.service.active(sid).get('binding') or {}
        except Exception:
            return False
        return (binding.get('connector_version','').startswith('unified-v1:')
                and self.selected_engine(sid) in ('claude', 'pi', 'codex'))

    def _recover_legacy_compact_notice(self, sid, operation):
        """Migrate the old Pi compact-as-permission projection from durable success."""
        if str((operation or {}).get('result') or '') != 'success':
            return False
        state = self.service.coordinator.state(sid) or {}
        expected = str(operation.get('expected_turn_id') or '')
        committed = next((turn for turn in (state.get('turns') or [])
                          if str((turn or {}).get('turn_id') or '') == expected
                          and turn.get('outcome') == 'completed'), None)
        source = (committed or {}).get('source_ref') or {}
        engine = str(source.get('engine') or '')
        completed_at = int(float(operation.get('updated_at') or 0) * 1000)
        incoming = {'state':'ready', 'stage':'compaction', 'engine':engine,
                    'resolvedThrough':completed_at}
        from error_notice import has_legacy_compaction_permission
        if (not engine or not completed_at
                or not has_legacy_compaction_permission(self.index.get(sid) or {}, incoming)):
            return False
        self._env_publish(sid, dict(incoming, label='上下文压缩已恢复'))
        return True

    def _compact_operation_state(self, sid):
        """Settle a durable receipt from existing Coordinator evidence only."""
        row = self.compact_operations.current(sid)
        if not row or not row.get('running'):
            self._recover_legacy_compact_notice(sid, row)
            return row
        # In the accepting process the worker owns settlement. Coordinator may
        # commit the turn just before marker verification; /state must not claim
        # success inside that window. Cross-boot recovery alone classifies evidence.
        if (row.get('boot_id') == self.compact_operations.boot_id
                and row.get('status') == 'running'):
            with self._lock:
                worker = self._compact_threads.get(row.get('operation_id'))
            if row.get('operation_id') in self._compact_threads and (
                    worker is None or worker.ident is None or worker.is_alive()):
                return row
        state = self.service.coordinator.state(sid) or {}
        before = int(row.get('before_seq') or 0)
        expected = str(row.get('expected_turn_id') or f'{sid}:{before + 1}')
        turns = state.get('turns') if isinstance(state.get('turns'), list) else []
        active = self._active_turn(state)
        if str(active.get('turn_id') or '') == expected:
            return row
        committed = next((turn for turn in turns
                          if str((turn or {}).get('turn_id') or '') == expected), None)
        if committed:
            outcome = str(committed.get('outcome') or '')
            result = 'success' if outcome == 'completed' else 'cancelled' if outcome == 'interrupted' else 'failed'
            error = '' if result != 'failed' else '压缩未产生可恢复的完整检查点'
            try:
                if result == 'success':
                    self._persist_manual_compact_marker(sid, int(row.get('started') or 0))
                done = self.compact_operations.finish(
                    sid, row['operation_id'], result=result, error=error)
                self._recover_legacy_compact_notice(sid, done)
                return done
            except Exception:
                return row
        if active:
            return row
        # The accepting backend died before a committed turn appeared. Never
        # replay; release the fence with an explicit failed terminal.
        return self.compact_operations.finish(
            sid, row['operation_id'], result='failed', error='Desk 重启前压缩未留下可恢复检查点')

    @staticmethod
    def _compact_receipt(row):
        result = str((row or {}).get('result') or 'running')
        error = str((row or {}).get('error') or '')
        running = bool((row or {}).get('running'))
        return {'ok':not error and result not in ('failed', 'not_started'),
                'accepted':running, 'running':running,
                'started':(row or {}).get('started'),
                'operation_id':(row or {}).get('operation_id'),
                'result':result, **({'error':error} if error else {})}

    def compact_session_async(self, sid, operation_id=''):
        """Admit compaction quickly; completion is projected by event/state."""
        self._require_execution(sid)
        operation_id = str(operation_id or ('compact_' + uuid.uuid4().hex))[:128]
        if self._shutting_down:
            return {'ok':False, 'error':'Desk 正在退出，不能开始压缩'}
        if not self.compact_available(sid):
            return {'ok':False, 'error':'当前执行引擎尚不支持统一存储主动压缩'}
        current = self._compact_operation_state(sid)
        if current.get('operation_id') == operation_id:
            return self._compact_receipt(current)
        with self._lock:
            turn_guard = self._turn_locks.setdefault(sid, threading.Lock())
        if not turn_guard.acquire(blocking=False):
            return {'ok':False, 'error':'当前回合仍在运行，不能同时压缩'}
        try:
            state = self.service.coordinator.state(sid) or {}
            active_turn = self._active_turn(state)
            if active_turn.get('recovery_state') == 'blocked':
                return {'ok':False, 'error':'Shepherd 上轮来源无法安全验证，请先完成隔离'}
            if active_turn:
                return {'ok':False, 'error':'Shepherd 上轮仍在恢复，暂不能压缩'}
            before = self._history_seq(state)
            try:
                compact, fresh = self.compact_operations.claim(
                    sid, operation_id, before_seq=before)
            except (CompactOperationConflict, ValueError) as exc:
                return {'ok':False, 'error':str(exc)[:300]}
            if not fresh:
                return self._compact_receipt(compact)
            started = int(compact.get('started') or time.time() * 1000)
            with self._lock:
                self._compact_threads[operation_id] = None
        finally:
            turn_guard.release()
        self.bus.publish({'type':'session.compact','properties':{'sessionID':sid, **compact}})
        def run():
            try:
                try:
                    result = self.compact_session(sid, started=started, admitted=True,
                                                  operation_id=operation_id)
                except Exception as exc:
                    result = {'ok':False, 'error':str(exc)[:300] or '压缩任务异常退出'}
                # Admission and execution are separated by a thread boundary. If a
                # competing turn appears in that window, compact_session rejects it
                # before its normal terminal projection; close the accepted operation.
                current = self.compact_operations.get(sid, operation_id)
                if current.get('status') == 'running' and not result.get('ok'):
                    error = str(result.get('error') or '压缩任务未能开始')[:300]
                    done = self.compact_operations.finish(
                        sid, operation_id, result='failed', error=error)
                else:
                    # verifying means the expected turn already committed. A
                    # receipt-write failure is retried by compact_status/turn_state;
                    # it must not relabel a successful native compaction as failed.
                    done = None
                if done:
                    self.bus.publish({'type':'session.compact','properties':{'sessionID':sid, **done}})
            finally:
                with self._lock:
                    self._compact_threads.pop(operation_id, None)
        try:
            thread = threading.Thread(target=run, daemon=True,
                                      name=f'shepherd-compact-{sid[-8:]}')
            with self._lock:
                self._compact_threads[operation_id] = thread
            thread.start()
        except Exception as exc:
            with self._lock:
                self._compact_threads.pop(operation_id, None)
            error = str(exc)[:300] or '压缩任务启动失败'
            done = self.compact_operations.finish(
                sid, operation_id, result='failed', error=error)
            self.bus.publish({'type':'session.compact','properties':{'sessionID':sid, **done}})
            return {'ok':False, 'error':error}
        return self._compact_receipt(compact)

    def cancel_unaccepted_compact(self, sid, operation_id):
        self._check_namespace(sid)
        try:
            row = self.compact_operations.cancel_unaccepted(sid, operation_id)
        except ValueError as exc:
            return {'ok':False, 'error':str(exc)[:300]}
        return self._compact_receipt(row)

    def compact_status(self, sid, operation_id):
        self._check_namespace(sid)
        try:
            row = self.compact_operations.get(sid, operation_id)
        except ValueError:
            return {'operation_id':str(operation_id or ''), 'status':'not_found',
                    'running':False, 'result':'not_found'}
        if not row.get('status'):
            return {'operation_id':str(operation_id or ''), 'status':'not_found',
                    'running':False, 'result':'not_found'}
        if row.get('running'):
            current = self._compact_operation_state(sid)
            if current.get('operation_id') == operation_id:
                row = current
        return self._compact_receipt(row)

    def turn_state(self, sid):
        out = super().turn_state(sid)
        ctx = self._live.get(sid) or {}
        if not ctx:
            receipt = self.requests.blocking(sid)
            if receipt and receipt['status'] in ('preparing', 'started', 'uncertain'):
                try:
                    self.recover_pending(sid, trigger='state_read', blocking=False)
                except Exception as exc:
                    self._receipt_settle_failed(sid, exc)
                receipt = self.requests.blocking(sid)
            if receipt:
                out.update(state='blocked', request=receipt)
        review = ctx.get('_execution_review')
        out['execution_review'] = copy.deepcopy(review) if isinstance(review, dict) else None
        operation = self._compact_operation_state(sid)
        native = out.get('compact') if isinstance(out.get('compact'), dict) else {}
        operation_at = (int(float(operation.get('updated_at') or 0) * 1000)
                        if operation and not operation.get('running')
                        else int((operation or {}).get('started') or 0))
        if operation and (operation.get('running')
                          or operation_at >= int(native.get('started') or 0)):
            out['compact'] = operation
            if operation.get('running'):
                out['compacting'] = int(operation.get('started') or 0)
                out['activity'] = {'kind':'compacting', 'seconds':max(
                    0, int(time.time() - int(operation.get('started') or 0) / 1000))}
            elif not (self._live.get(sid) or {}).get('compact_t0'):
                out.pop('compacting', None)
        return out

    def compact_session(self, sid, *, started=None, admitted=False, operation_id=''):
        """Run compaction as a serialized unified turn, never as a native slash prompt."""
        self._require_execution(sid)
        if not self.compact_available(sid):
            return {'ok':False, 'error':'当前执行引擎尚不支持统一存储主动压缩'}
        if sid in self._live:
            return {'ok':False, 'error':'当前回合仍在运行，不能同时压缩'}
        state = self.service.coordinator.state(sid) or {}
        before = self._history_seq(state)
        active_turn = self._active_turn(state)
        if active_turn.get('recovery_state') == 'blocked':
            return {'ok':False, 'error':'Shepherd 上轮来源无法安全验证，请先完成隔离'}
        if active_turn:
            return {'ok':False, 'error':'Shepherd 上轮仍在恢复，暂不能压缩'}
        started = int(started or time.time() * 1000)
        if not admitted:
            compact = {'running':True, 'started':started, 'trigger':'manual',
                       **({'operation_id':operation_id} if operation_id else {})}
            self._compact_state[sid] = compact
            self.bus.publish({'type':'session.compact','properties':{'sessionID':sid, **compact}})
        error = ''
        result = 'success'
        try:
            import self_update
            with self_update.tracked_work(user_activity=True, restart_blocking=False) as admitted:
                if not admitted:
                    error = 'Desk 正在准备更新，请稍后压缩'
                else:
                    local = getattr(self, '_compact_local', None)
                    if local is not None:
                        local.operation_id = operation_id
                    try:
                        self._run_serial(sid, '/compact', self.session_options(sid, {}), [])
                    finally:
                        if local is not None:
                            local.operation_id = ''
            state = self.service.coordinator.state(sid) or {}
            turns = state.get('turns') if isinstance(state.get('turns'), list) else []
            latest = turns[-1] if len(turns) > before else {}
            if not error:
                if len(turns) == before + 1 and latest.get('outcome') == 'interrupted':
                    result = 'cancelled'
                elif len(turns) != before + 1 or latest.get('outcome') != 'completed':
                    error = '压缩未产生可恢复的完整检查点'
        except Exception as exc:
            error = str(exc)[:300] or '压缩执行失败'
        result = 'failed' if error else result
        if operation_id and not error:
            # Coordinator already owns the native terminal. Projection writes
            # after this point are retryable bookkeeping, never a model failure.
            try:
                self.compact_operations.verifying(sid, operation_id)
                if result == 'success':
                    self._persist_manual_compact_marker(sid, started)
                done = self.compact_operations.finish(
                    sid, operation_id, result=result, error='')
            except Exception:
                return self._compact_receipt(
                    self.compact_operations.get(sid, operation_id))
        else:
            done = {'running':False, 'started':started, 'trigger':'manual',
                    **({'operation_id':operation_id} if operation_id else {}),
                    'result':result, 'error':error}
            if operation_id:
                done = self.compact_operations.finish(
                    sid, operation_id, result=result, error=error)
            else:
                if not error and result == 'success':
                    try:
                        self._persist_manual_compact_marker(sid, started)
                    except Exception as exc:
                        error = str(exc)[:300] or '压缩结果写入失败'
                        result = 'failed'
                        done.update(result=result, error=error)
                self._compact_state[sid] = done
        self.bus.publish({'type':'session.compact','properties':{'sessionID':sid, **done}})
        return {'ok':not error, 'result':result, **({'error':error} if error else {})}


class ShepherdTrialAdapter(ShepherdAdapter):
    """Same protocol implementation, separately owned index and execution state."""
    trial = True
    SESSION_ID_PREFIX = 'adp_trial_'
    display = 'Shepherd'
