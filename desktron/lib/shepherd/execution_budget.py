"""Derive native execution deadlines from verified session capabilities."""
import hashlib
import json
import math
import re
import time
import uuid
from collections import OrderedDict

THINKING_FACTORS = {
    'off': 0.75,
    'minimal': 0.8,
    'low': 0.9,
    'medium': 1.0,
    'high': 1.25,
    'xhigh': 1.6,
    'max': 2.0,
}
_BASE_CONTEXT = 200_000
_MIN_HARD_SECONDS = 60 * 60
_MAX_HARD_SECONDS = 12 * 60 * 60
_MAX_TOOL_OUTPUT_FINGERPRINTS = 128
_REVIEW_INTERVAL_SECONDS = 60 * 60
_REVIEW_ABSOLUTE_SECONDS = 12 * 60 * 60
_REVIEW_ACK_SECONDS = 30
_CLAUDE_STARTUP_SECONDS = 30
_CLAUDE_HANDOFF_SECONDS = 30
_CLAUDE_NOTICE_SECONDS = 60
_CLAUDE_NO_PROGRESS_SECONDS = 10 * 60


def execution_review_prompt(review_id, sequence):
    """Internal Pi steering message; the native queue preserves the turn boundary."""
    return f'''[Desktron 长任务复核，不是新的用户请求；review_id={review_id}；sequence={sequence}]
当前任务已持续一小时。请先判断继续执行是否合理：核对近期可验证进展、有限剩余步骤、预计耗时、阻碍、重复失败、范围变化和副作用风险。
- 合理：继续完成已明确的剩余步骤，不扩展到新发现的相邻问题。
- 不合理或高风险：停止新增工具，汇报当前结果。
- 需要授权或范围选择：停止执行并向用户提出一个明确问题。
不要只回答“仍在工作”。这是同一公开回合内的内部提醒，不要把它当作用户新增需求。'''


class ExecutionReviewClock:
    """Hourly Pi review schedule with a non-renewable twelve-hour ceiling."""

    def __init__(self, *, clock=time.monotonic, wall_clock=time.time,
                 interval=_REVIEW_INTERVAL_SECONDS,
                 absolute_seconds=_REVIEW_ABSOLUTE_SECONDS,
                 ack_seconds=_REVIEW_ACK_SECONDS, id_factory=None):
        self.clock = clock
        self.wall_clock = wall_clock
        self.interval = max(.001, float(interval))
        self.absolute_seconds = max(self.interval, float(absolute_seconds))
        self.ack_seconds = max(.001, float(ack_seconds))
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self.started = clock()
        self.started_wall = wall_clock()
        self.absolute_deadline = self.started + self.absolute_seconds
        self.next_review_at = min(self.absolute_deadline, self.started + self.interval)
        self.ack_deadline = None
        self.sequence = 0
        self.review_id = ''
        self.status = 'running'
        self.error = ''

    def mark_due(self, now=None):
        now = self.clock() if now is None else float(now)
        if now >= self.absolute_deadline:
            self.status = 'absolute_limit'
            return self.snapshot(now)
        if self.status != 'running' or now < self.next_review_at:
            return None
        self.sequence += 1
        self.review_id = str(self.id_factory())
        if not self.review_id:
            raise ValueError('execution review id is empty')
        self.status = 'review_due'
        return self.snapshot(now)

    def prompt(self):
        if self.status != 'review_due':
            return None
        self.status = 'dispatching'
        self.ack_deadline = self.clock() + self.ack_seconds
        return execution_review_prompt(self.review_id, self.sequence)

    def accepted(self, now=None):
        now = self.clock() if now is None else float(now)
        if self.status != 'dispatching':
            raise ValueError('execution review is not awaiting native acknowledgement')
        if now >= self.absolute_deadline:
            self.status = 'absolute_limit'
            raise ValueError('execution review absolute limit reached')
        self.status = 'injected'
        self.ack_deadline = None
        return self.snapshot(now)

    def completed(self, now=None):
        now = self.clock() if now is None else float(now)
        if self.status != 'injected':
            raise ValueError('execution review continuation is not active')
        if now >= self.absolute_deadline:
            self.status = 'absolute_limit'
            raise ValueError('execution review absolute limit reached')
        self.status = 'running'
        # Keep reviews on task-start hour boundaries. If a review itself crossed
        # a boundary, skip stale ticks instead of immediately dispatching again.
        elapsed_ticks = math.floor(max(0., now - self.started) / self.interval) + 1
        self.next_review_at = min(self.absolute_deadline,
                                  self.started + elapsed_ticks * self.interval)
        return self.snapshot(now)

    def reject(self, reason):
        self.status = 'rejected'
        self.error = str(reason or 'execution review steering rejected')
        self.ack_deadline = None
        return self.snapshot()

    def next_deadline(self):
        if self.status == 'absolute_limit':
            return self.clock(), 'absolute_review_limit'
        if self.status == 'rejected':
            return self.clock(), 'review_rejected'
        if self.status == 'dispatching':
            return min((self.ack_deadline, 'review_ack'),
                       (self.absolute_deadline, 'absolute_review_limit'), key=lambda row:row[0])
        if self.status in ('review_due', 'injected'):
            return self.absolute_deadline, 'absolute_review_limit'
        return min((self.next_review_at, 'review'),
                   (self.absolute_deadline, 'absolute_review_limit'), key=lambda row:row[0])

    def snapshot(self, now=None):
        now = self.clock() if now is None else float(now)
        wall = self.wall_clock()
        as_wall = lambda deadline: wall + max(0, deadline - now)
        result = {
            'status':self.status,
            'started_at':self.started_wall,
            'next_review_at':as_wall(self.next_review_at),
            'absolute_deadline':as_wall(self.absolute_deadline),
            'review_sequence':self.sequence,
            'review_id':self.review_id if self.status in ('review_due', 'dispatching', 'injected') else '',
        }
        if self.ack_deadline is not None:
            result['ack_deadline'] = as_wall(self.ack_deadline)
        if self.error:
            result['error'] = self.error
        return result


def model_context_window(catalog, model_id='', provider=''):
    """Return the selected model's advertised context window, or zero."""
    models = (catalog or {}).get('models') if isinstance(catalog, dict) else None
    if not isinstance(models, list):
        return 0
    flat = []
    for row in models:
        if not isinstance(row, dict):
            continue
        flat.append(row)
        flat.extend(item for item in (row.get('variants') or []) if isinstance(item, dict))
    selected = str(model_id or '').strip()
    if selected and '/' not in selected and str(provider or '').strip():
        selected = f'{str(provider).strip()}/{selected}'
    row = next((item for item in flat if str(item.get('id') or '') == selected), None)
    if row is None and not selected:
        row = next((item for item in flat if item.get('default')), None)
    try:
        value = int((row or {}).get('contextWindow') or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def hard_timeout_seconds(idle_timeout, *, effort='', context_window=0, ultracode=False):
    """Compute an absolute ceiling from the soft-stall observation window.

    Eight observation windows is the compatibility baseline. Deeper reasoning and a
    larger verified context window raise the ceiling monotonically. Unknown
    model windows use the baseline instead of a guessed model table.
    """
    idle = max(0.001, float(idle_timeout))
    level = 'max' if ultracode else str(effort or 'medium').strip().lower()
    thinking = THINKING_FACTORS.get(level, THINKING_FACTORS['medium'])
    try:
        window = int(context_window or 0)
    except (TypeError, ValueError):
        window = 0
    window_factor = 1.0 if window <= 0 else min(3.0, max(0.75, math.sqrt(window / _BASE_CONTEXT)))
    value = idle * 8 * thinking * window_factor
    # Production defaults receive a useful lower/upper bound. Tiny explicit
    # test timeouts remain tiny so lifecycle tests do not wait an hour.
    if idle >= 60:
        value = min(_MAX_HARD_SECONDS, max(_MIN_HARD_SECONDS, value))
    return max(idle, value)


def runner_timeouts(options, catalog=None, *, idle_timeout=900):
    opts = dict(options or {})
    window = opts.get('_context_window') or model_context_window(catalog, opts.get('model'), opts.get('provider'))
    if not window:
        try:
            compact_window = int(opts.get('autocompact') or 0)
        except (TypeError, ValueError):
            compact_window = 0
        if compact_window > 0:
            window = compact_window
    hard = hard_timeout_seconds(
        idle_timeout, effort=opts.get('effort'), context_window=window,
        ultracode=bool(opts.get('ultracode')))
    return {'idle_timeout':float(idle_timeout), 'hard_timeout':hard,
            'context_window':int(window or 0)}


class ToolOutputTracker:
    """Bounded hashes for cumulative tool snapshots; never retains output text."""

    def __init__(self, limit=_MAX_TOOL_OUTPUT_FINGERPRINTS):
        self.limit = max(1, int(limit))
        self.seen = OrderedDict()
        self.active = set()
        self.ended = OrderedDict()

    @staticmethod
    def fingerprint(result):
        if not ExecutionDeadline._tool_output_progress(result):
            return None
        try:
            payload = json.dumps(result.get('content'), ensure_ascii=False,
                                 sort_keys=True, separators=(',', ':')).encode('utf-8')
        except (TypeError, ValueError, UnicodeError):
            return None
        return hashlib.sha256(payload).hexdigest()

    def start(self, tool_id):
        if not tool_id or tool_id in self.active:
            return False
        self.active.add(tool_id)
        self.ended.pop(tool_id, None)
        self._clear(tool_id)
        return True

    def update(self, tool_id, result):
        if not tool_id or tool_id in self.ended:
            return False
        fingerprint = self.fingerprint(result)
        if not fingerprint:
            return False
        key = (tool_id, fingerprint)
        if key in self.seen:
            self.seen.move_to_end(key)
            return False
        self.seen[key] = None
        while len(self.seen) > self.limit:
            self.seen.popitem(last=False)
        return True

    def end(self, tool_id):
        if not tool_id or tool_id in self.ended:
            return False
        self.active.discard(tool_id)
        self._clear(tool_id)
        self.ended[tool_id] = None
        self.ended.move_to_end(tool_id)
        while len(self.ended) > self.limit:
            self.ended.popitem(last=False)
        return True

    def _clear(self, tool_id):
        for key in tuple(self.seen):
            if key[0] == tool_id:
                self.seen.pop(key, None)


class ExecutionDeadline:
    """One turn's monotonic budget, shared by disposable and resident hosts.

    Opaque compaction has no streaming-output contract and uses the absolute
    operation budget. Provider queue/retry/wait notices open a bounded waiting
    window; repeated local notices cannot renew it. Only native work progress
    ends that window. No clock transition establishes a successful terminal.
    Callers serialize access with their existing process lock.
    """

    def __init__(self, timeout, hard_timeout, *, operation='turn', clock=time.monotonic,
                 provider_fence=None, review_enabled=False, wall_clock=time.time,
                 review_id_factory=None):
        self.clock = clock
        self.idle = max(.001, float(timeout))
        self.hard = max(.001, float(hard_timeout))
        self.started = self.last_activity = clock()
        self.stall_baseline = self.last_activity
        self.hard_deadline = self.started + self.hard
        self.operation = operation
        self.review = (ExecutionReviewClock(clock=clock, wall_clock=wall_clock,
                       id_factory=review_id_factory)
                       if review_enabled and operation == 'turn' else None)
        if self.review is not None:
            self.hard_deadline = self.review.absolute_deadline
        self.compacting = operation == 'compact'
        # A verified automatic compaction may reserve at most one more hard window.
        # The reserve is one-shot and the whole turn stays bounded by 2 * hard.
        self.maximum_hard_deadline = self.started + self.hard * 2
        self.automatic_compaction_extended = False
        # Match the provider's one-hour wait bound, while keeping explicit tiny
        # test budgets proportional. This is a ceiling, never a progress pulse.
        self.wait_budget = min(self.hard, self.idle * 4, 3600)
        self.wait_deadline = None
        self.wait_kind = ''
        self.last_progress = None
        # Local owned-pipe movement and the Provider's last upstream byte are
        # independent diagnostics. Neither one is semantic progress.
        self.transport_at = None
        self.provider_transport_at = None
        self.active_tools = set()
        self.last_tool_result = None
        # Hashes only: never retain tool output in the execution budget. Pi
        # partialResult is a cumulative snapshot, so an identical replay is
        # transport activity rather than new semantic progress.
        self.tool_output_tracker = ToolOutputTracker()
        self.tool_output_fingerprints = self.tool_output_tracker.seen
        self.soft_stalled = False
        self.wait_epoch = None
        self.wait_request_seq = 0
        self.wait_notice_seq = 0
        self.provider_fence = provider_fence
        self.claude_startup_deadline = None
        self.claude_dispatch_id = ''
        self.claude_dispatch_stage = ''
        self.claude_stderr_category = ''
        self._claude_wait_open = False
        self._claude_response_started = None
        self._claude_first_response_started = None

    def begin_claude_startup(self):
        """Bound bootstrap/initialization before a durable user dispatch exists."""
        if self.claude_startup_deadline is not None or self.claude_dispatch_id:
            raise ValueError('Claude startup wait already armed')
        self.claude_startup_deadline = min(self.hard_deadline,
            self.clock() + min(_CLAUDE_STARTUP_SECONDS, self.wait_budget))

    def claude_termination_deadline(self):
        """Independent watchdog limit; soft observation never hides a hard limit."""
        limits = [(self.hard_deadline, 'hard')]
        if self.claude_startup_deadline is not None:
            limits.append((self.claude_startup_deadline, 'claude_startup'))
        if self.wait_deadline is not None:
            limits.append((self.wait_deadline, self.wait_kind))
        return min(limits, key=lambda item: item[0])

    def claude_timeout_kind(self):
        if self.claude_startup_deadline is None and not self.claude_dispatch_id:
            return ''
        deadline, kind = self.claude_termination_deadline()
        return kind if self.clock() >= deadline else ''

    def begin_claude_response(self, dispatch_id):
        """Bound local SDK handoff, then first work; never establish a terminal.

        Called by the owner after the durable dispatch fence, before writing start.
        SDK iterator consumption is not proof of CLI or Provider acceptance.
        """
        expired = self.claude_timeout_kind()
        if expired:
            raise ValueError(self.timeout_message(expired))
        self.claude_startup_deadline = None
        if self.compacting or self.operation == 'compact':
            return
        if not isinstance(dispatch_id, str) or not dispatch_id or self.claude_dispatch_id:
            raise ValueError('Claude dispatch wait identity invalid or already armed')
        self.claude_dispatch_id = dispatch_id
        self.claude_dispatch_stage = 'start_sent'
        self._claude_wait_open = True
        self._claude_response_started = self.clock()
        self.wait_deadline = min(self.hard_deadline,
            self._claude_response_started + min(_CLAUDE_HANDOFF_SECONDS, self.wait_budget))
        self.wait_kind = 'claude_handoff'

    def _observe_claude_dispatch(self, event):
        if not self.claude_dispatch_id or event.get('dispatch_id') != self.claude_dispatch_id:
            return False
        # Deadline arrival is irreversible even if the watchdog/consumer has
        # not had a CPU slice yet. A late ack must not resurrect an expired wait.
        if self._claude_wait_open and self.wait_deadline is not None and self.clock() >= self.wait_deadline:
            return False
        stage = event.get('stage')
        order = ('start_sent', 'start_received', 'store_begin', 'store_ready',
                 'input_queued', 'input_consumed', 'first_native_event')
        if stage in ('stderr_auth', 'stderr_network', 'stderr_rate_limit', 'stderr_other'):
            self.claude_stderr_category = stage
        elif stage in order and order.index(stage) > order.index(self.claude_dispatch_stage):
            self.claude_dispatch_stage = stage
        if stage == 'input_consumed' and self._claude_wait_open and self.wait_kind == 'claude_handoff':
            # SDK consumption proves neither CLI processing nor Provider contact.
            # Start both the visible notice and consecutive-silence clocks here,
            # not when the owner first wrote the dispatch frame.
            now = self.clock()
            self._claude_first_response_started = now
            self.stall_baseline = now
            self.wait_deadline = min(self.hard_deadline,
                now + min(_CLAUDE_NO_PROGRESS_SECONDS, self.wait_budget))
            self.wait_kind = 'claude_response'
        elif (stage in ('provider_response_started', 'provider_retry')
                and self._claude_wait_open
                and self.wait_kind in ('claude_response', 'provider_wait')):
            # Verified upstream response/retry is native progress. Each pulse
            # restarts the consecutive-silence window, but the bridge's one-hour
            # retry budget and the turn hard deadline remain absolute.
            recovered = self.soft_stalled
            self._renew_claude_progress_wait(self.clock())
            return recovered
        return False

    @staticmethod
    def _content_progress(content):
        if isinstance(content, str):
            return bool(content.strip())
        if not isinstance(content, list):
            return False
        for block in content:
            if isinstance(block, str) and block.strip():
                return True
            if not isinstance(block, dict):
                continue
            if block.get('type') in ('tool_use', 'toolCall', 'tool_result'):
                return True
            if any(isinstance(block.get(key), str) and block[key].strip()
                   for key in ('text', 'thinking', 'partial_json', 'data', 'url', 'path')):
                return True
            source = block.get('source')
            if isinstance(source, dict) and any(source.get(key) for key in ('data', 'url', 'path')):
                return True
        return False

    @classmethod
    def _tool_output_progress(cls, result):
        if not isinstance(result, dict):
            return False
        content = result.get('content')
        if isinstance(content, str):
            return bool(content.strip())
        if not isinstance(content, list):
            return False
        for block in content:
            if isinstance(block, str) and block.strip():
                return True
            if not isinstance(block, dict):
                continue
            if any(isinstance(block.get(key), str) and block[key].strip()
                   for key in ('text', 'thinking', 'partial_json', 'data', 'url', 'path')):
                return True
            source = block.get('source')
            if isinstance(source, dict) and any(source.get(key) for key in ('data', 'url', 'path')):
                return True
        return False

    @classmethod
    def _semantic_progress(cls, event):
        kind = event.get('type')
        if kind in ('progress', 'tool_execution_start', 'tool_execution_end',
                    'compaction_end', 'tool_progress', 'tool_use_summary'):
            return True
        if kind == 'tool_execution_update':
            return cls._tool_output_progress(event.get('partialResult'))
        if kind == 'message_update':
            delta = event.get('assistantMessageEvent')
            if not isinstance(delta, dict):
                return False
            delta_kind = delta.get('type')
            if delta_kind in ('toolcall_start', 'toolcall_end'):
                return True
            return delta_kind in ('text_delta', 'thinking_delta', 'toolcall_delta') and bool(
                str(delta.get('delta') or ''))
        if kind == 'message_end':
            message = event.get('message')
            return isinstance(message, dict) and message.get('role') == 'assistant' and \
                cls._content_progress(message.get('content'))
        if kind == 'assistant':
            message = event.get('message')
            return isinstance(message, dict) and not (
                event.get('error') or event.get('isApiErrorMessage')
                or event.get('is_api_error_message')) and cls._content_progress(message.get('content'))
        if kind == 'user':
            message = event.get('message')
            content = message.get('content') if isinstance(message, dict) else None
            return isinstance(content, list) and any(
                isinstance(block, dict) and block.get('type') == 'tool_result'
                for block in content)
        if kind == 'stream_event':
            stream = event.get('event')
            if not isinstance(stream, dict):
                return False
            if stream.get('type') == 'content_block_start':
                return cls._content_progress([stream.get('content_block')])
            if stream.get('type') == 'content_block_delta':
                delta = stream.get('delta')
                return isinstance(delta, dict) and any(
                    isinstance(delta.get(key), str) and bool(delta[key])
                    for key in ('text', 'thinking', 'partial_json'))
            return False
        if kind == 'system' and str(event.get('subtype') or '').startswith('hook_'):
            return True
        if kind == 'attachment':
            attachment = event.get('attachment')
            return isinstance(attachment, dict) and str(
                attachment.get('type') or '').startswith('hook_')
        return False

    def touch(self, event=None):
        """Record transport separately; renew soft-stall only for semantic progress."""
        now = self.clock()
        previous_activity = self.last_activity
        previous_soft_stalled = self.soft_stalled
        # A parsed frame, malformed stdout line, stderr chunk, or local write
        # proves only that this owned pipe moved. It is diagnostic and cannot
        # postpone or recover the semantic soft-stall clock.
        self.transport_at = now
        if not isinstance(event, dict):
            return False
        if event.get('type') == 'event' and isinstance(event.get('event'), dict):
            event = event['event']  # Claude bridge envelope
        kind = event.get('type')
        if self.claude_timeout_kind():
            return False  # Neither a late prepared/result nor progress revives expiry.
        if kind == 'claude_dispatch':
            return self._observe_claude_dispatch(event)
        if kind == 'result' and self._claude_wait_open:
            self._end_wait()  # A terminal result, not a guessed outcome.
        elif kind == 'permission' and self._claude_wait_open:
            # The native permission request is progress, not a terminal state.
            # A missing user/bridge response must still remain bounded.
            self._renew_claude_progress_wait(now)
        progress = self._semantic_progress(event)
        tool_id = event.get('toolCallId')
        tool_id = tool_id if isinstance(tool_id, str) and tool_id else None
        if kind == 'tool_execution_update':
            progress = self.tool_output_tracker.update(
                tool_id, event.get('partialResult'))
        if kind == 'tool_execution_start':
            tool_id = tool_id or 'unknown'
            progress = self.tool_output_tracker.start(tool_id)
            self.active_tools.add(tool_id)
        elif kind == 'tool_execution_end':
            tool_id = tool_id or 'unknown'
            progress = self.tool_output_tracker.end(tool_id)
            self.active_tools.discard(tool_id)
            if progress:
                self.last_tool_result = now
        if kind == 'compaction_start' or (kind == 'system' and
                event.get('subtype') == 'status' and event.get('status') == 'compacting'):
            if not self.compacting and self.operation != 'compact':
                self._extend_for_automatic_compaction(now)
            self.compacting = True
            progress = True
        elif kind == 'compaction_end' or (kind == 'system' and
                event.get('subtype') == 'compact_boundary'):
            self.compacting = self.operation == 'compact'
            progress = True
        if progress:
            self.last_activity = self.last_progress = now
            self._renew_claude_progress_wait(now)
        recovered = previous_soft_stalled and (
            not self.soft_stalled or self.last_activity > previous_activity)
        if recovered:
            self.soft_stalled = False
        return recovered

    def _extend_for_automatic_compaction(self, now):
        if self.automatic_compaction_extended:
            return
        self.automatic_compaction_extended = True
        self.hard_deadline = max(self.hard_deadline,
            min(self.maximum_hard_deadline, now + self.hard))

    def _renew_claude_progress_wait(self, now):
        """Restart only the consecutive-silence clock after input handoff."""
        if (not self._claude_wait_open
                or self.wait_kind not in ('claude_response', 'provider_wait')):
            return
        self.soft_stalled = False
        self.stall_baseline = now
        self.wait_deadline = min(self.hard_deadline,
            now + min(_CLAUDE_NO_PROGRESS_SECONDS, self.wait_budget))
        self.wait_kind = 'provider_wait'

    def _end_wait(self):
        self.wait_deadline = None
        self.wait_kind = ''
        self._claude_wait_open = False

    def _stall_deadline(self):
        if (self._claude_wait_open and self.wait_kind == 'claude_response'
                and self._claude_first_response_started is not None):
            # A truthful early observation instead of fifteen minutes of opaque
            # 'working'. This hint never renews either waiting deadline.
            return self._claude_first_response_started + min(_CLAUDE_NOTICE_SECONDS, self.idle)
        return max(self.last_activity, self.stall_baseline) + self.idle

    def mark_soft_stall(self):
        """Turn an expired inactivity window into observation, never a terminal."""
        if self.compacting or self.soft_stalled:
            return None
        now = self.clock()
        baseline = max(self.last_activity, self.stall_baseline)
        if now < self._stall_deadline():
            return None
        self.soft_stalled = True
        activity = {'kind': 'no_progress', **self.observation()}
        if self._claude_wait_open and self.wait_kind == 'claude_response':
            activity['phase'] = 'first_response'
        return {'active': True, 'seconds': max(0, int(now - baseline)),
                'activity': activity}

    def mark_review_due(self):
        return self.review.mark_due() if self.review is not None else None

    def review_prompt(self):
        return self.review.prompt() if self.review is not None else None

    def accept_review(self):
        return self.review.accepted() if self.review is not None else None

    def complete_review(self):
        return self.review.completed() if self.review is not None else None

    def reject_review(self, reason):
        return self.review.reject(reason) if self.review is not None else None

    def review_snapshot(self):
        return self.review.snapshot() if self.review is not None else None

    def next_deadline(self):
        limits = ([] if self.review is not None else [(self.hard_deadline, 'hard')])
        if self.review is not None:
            limits.append(self.review.next_deadline())
        if self.wait_deadline is not None:
            limits.append((self.wait_deadline, self.wait_kind))
        if self.claude_startup_deadline is not None:
            limits.append((self.claude_startup_deadline, 'claude_startup'))
        if not self.compacting and not self.soft_stalled:
            # Soft observation and Provider expiry are independent clocks. A
            # bounded Provider wait may still become visibly stalled at the
            # ordinary inactivity threshold without terminating the process.
            limits.append((self._stall_deadline(), 'idle'))
        return min(limits, key=lambda item: item[0])

    def timeout_message(self, kind):
        label = {'hard': '达到最长时限', 'claude_startup': 'Claude 初始化超时',
                 'claude_response': 'Claude 连续无原生进展等待超时', 'claude_handoff': '输入交付确认超时',
                 'queue': '排队等待超时', 'retry': '重试等待超时',
                 'provider_wait': 'Claude 连续无原生进展等待超时',
                 'review_rejected': '阶段复核注入失败',
                 'review_ack': '阶段复核原生确认超时',
                 'absolute_review_limit': '达到 12 小时执行上限'}[kind]
        operation = '压缩' if self.compacting else '执行'
        facts = self.observation()
        details = []
        if kind == 'claude_handoff':
            details.append('SDK 尚未确认取出输入，不能断言模型服务已收到请求')
        if kind == 'claude_startup':
            details.append('原生宿主尚未就绪，不自动派发用户输入')
        if kind == 'claude_response':
            details.append('SDK 已取出输入，但连续未观察到模型响应或原生重试；不能归因于模型服务')
        if kind in ('claude_startup', 'claude_response', 'provider_wait'):
            details.append('若 macOS 弹出目录授权窗，可拒绝或只授权所需目录，不必开放完全磁盘访问')
        if self.claude_dispatch_stage:
            details.append('Claude 最后阶段 ' + self.claude_dispatch_stage)
        if self.claude_stderr_category:
            details.append('CLI stderr 类别 ' + self.claude_stderr_category)
        if facts['active_tools']:
            details.append(f"仍有 {facts['active_tools']} 个工具未返回")
        elif kind == 'provider_wait':
            details.append('模型未返回新进展')
        if facts['progress_age_seconds'] is not None:
            details.append(f"最后进展距今 {facts['progress_age_seconds']} 秒")
        if facts['transport_age_seconds'] is not None:
            details.append(f"最后管道活动距今 {facts['transport_age_seconds']} 秒")
        if facts['provider_transport_age_seconds'] is not None:
            details.append(
                f"Provider 最后连接数据距今 {facts['provider_transport_age_seconds']} 秒")
        if (facts['transport_age_seconds'] is not None
                or facts['provider_transport_age_seconds'] is not None):
            details.append('连接活动不代表模型进展')
        if facts['tool_result_age_seconds'] is not None:
            details.append(f"最近工具回执距今 {facts['tool_result_age_seconds']} 秒")
        detail = '（' + '；'.join(details) + '）' if details else ''
        outcome = ('用户输入未派发，不会自动重试' if kind == 'claude_startup'
                   else '未确认的回合保持封锁')
        return f'Shepherd 原生{operation}{label}{detail}，{outcome}'

    def observation(self):
        """Bounded facts only; never infer queueing or retain tool arguments."""
        now = self.clock()
        age = lambda at: None if at is None else max(0, int(now - at))
        facts = {'progress_age_seconds': age(self.last_progress),
                 'transport_age_seconds': age(self.transport_at),
                 'provider_transport_age_seconds': age(self.provider_transport_at),
                 'tool_result_age_seconds': age(self.last_tool_result),
                 'active_tools': len(self.active_tools)}
        if self.claude_dispatch_stage:
            facts['claude_dispatch_stage'] = self.claude_dispatch_stage
        if self.claude_stderr_category:
            facts['claude_stderr_category'] = self.claude_stderr_category
        return facts
