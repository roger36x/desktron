"""One admission/rejection policy for all unified native runners.

A rejection is evidence, not permission to replay. Only a settled, committed
failure with no semantic progress may get one live retry; recovery never runs it.
"""
from __future__ import annotations
import re
import json
from pathlib import Path
from dataclasses import dataclass

from .store import Conflict

POLICY = json.loads(Path(__file__).with_name('context_policy.json').read_text(encoding='utf-8'))
FALLBACK_HISTORY_TOKENS = POLICY['fallbackHistoryTokens']  # not a guessed model window
MAX_CONTEXT_RETRIES = POLICY['maxContextRetries']
_CONTEXT_ERROR = re.compile(POLICY['contextErrorPattern'], re.I)
_NON_CONTEXT_ERROR = re.compile(POLICY['nonContextErrorPattern'], re.I)
_COMPACTION_THRASHING = re.compile(POLICY['compactionThrashingPattern'], re.I)


def _explicit_refusal(error):
    """Structured service refusal wins across the whole native error envelope."""
    if isinstance(error, list):
        return any(_explicit_refusal(value) for value in error)
    if not isinstance(error, dict):
        return False
    for key in ('code', 'type', 'subtype'):
        code = error.get(key)
        if ((isinstance(code, str) and _NON_CONTEXT_ERROR.search(code))
                or str(code) in ('401', '403', '429')):
            return True
    return any(_explicit_refusal(error.get(key)) for key in (
        'error', 'errors', 'codexErrorInfo', 'codex_error_info', 'result'))


def context_error_reason(error):
    """Classify native error fields only; normal model/tool/user prose is not evidence."""
    if _explicit_refusal(error):
        return ''
    if isinstance(error, dict):
        reasons = [context_error_reason(error.get(key)) for key in (
            'code', 'type', 'subtype', 'message', 'error', 'errorMessage', 'codexErrorInfo',
            'codex_error_info', 'errors', 'result')]
        if 'contextWindowExceeded' in error:
            reasons.append('context_window_exceeded')
    elif isinstance(error, list):
        reasons = [context_error_reason(value) for value in error]
    else:
        if not isinstance(error, str) or _NON_CONTEXT_ERROR.search(error):
            return ''
        if _COMPACTION_THRASHING.search(error):
            return 'compaction_thrashing'
        return ('context_window_exceeded' if error.upper() == 'CONTEXT'
                or _CONTEXT_ERROR.search(error) else '')
    # Preserve the specific failure even when the error also mentions a window.
    return ('compaction_thrashing' if 'compaction_thrashing' in reasons
            else next((reason for reason in reasons if reason), ''))


def context_error(error):
    return bool(context_error_reason(error))


@dataclass(frozen=True)
class Admission:
    history_budget: int | None
    reason: str


def admit(*, window, current, history, has_history, has_checkpoint, required,
          learned=None, rejected=False):
    """Decide before native serialization or resident acquisition."""
    if required and window <= 0:
        raise Conflict('目标模型上下文窗口未知，历史未派发')
    # Unknown capacity is not unlimited permission to restore a native cache.
    # This is a HISTORY transport cap, never a guessed model window.
    budget = window - current - max(8192, window // 10) if window > 0 else FALLBACK_HISTORY_TOKENS
    if budget < 0:
        raise Conflict('当前输入、系统提示和预留空间超过上下文窗口；没有可用的降级上下文空间，未派发')
    if learned is not None:
        budget = min(budget, learned)
    reason = ('context_rejected' if rejected or (learned is not None and history > learned) else
              'no_compatible_checkpoint' if has_history and not has_checkpoint else
              ('context_over_budget' if window > 0 else 'context_window_unknown')
              if history > budget else '')
    return Admission(min(budget, FALLBACK_HISTORY_TOKENS), reason)


def _object(value):
    return value if isinstance(value, dict) else {}


class RejectionTracker:
    """Protocol normalization only; one policy above owns the transitions."""
    def __init__(self, engine):
        self.engine = engine
        self.rejection_reason = ''
        self.progress = False
        self._compact_error = None

    @property
    def rejected(self):
        return bool(self.rejection_reason)

    def _reject(self, error, *, retain=False):
        reason = context_error_reason(error)
        if reason or not retain:
            self.rejection_reason = reason

    def observe(self, event, params=None):
        if self.engine == 'codex':
            from codex_appserver import _turn_semantic_event
            body = _object(params)
            if not isinstance(params, dict):self.progress=True
            if _turn_semantic_event(event, body):
                self.progress = True
            if event in ('error', 'turn/failed', 'turn/completed'):
                turn = _object(body.get('turn'))
                error=turn.get('error') or body.get('error')
                if error:self._reject(error)
                elif event == 'turn/completed' and turn.get('status') == 'completed':
                    self.rejection_reason = ''  # Native recovery actually succeeded.
            return
        if not isinstance(event, dict):
            return
        kind = str(event.get('type') or '')
        if self.engine == 'pi':
            message = _object(event.get('message'))
            if kind == 'message_end' and message.get('role') == 'assistant':
                if message.get('stopReason') not in ('error', 'aborted') and not message.get('errorMessage'):
                    self.rejection_reason = ''  # native recovery actually succeeded
                else:
                    self._reject(message.get('errorMessage'))
                self.progress |= bool(message.get('content'))
            if kind == 'compaction_end' and not event.get('aborted'):
                self._reject(event.get('errorMessage'), retain=True)
            if kind.startswith('tool_execution_'):
                self.progress = True
            if kind == 'message_update':
                stream=_object(event.get('assistantMessageEvent'))
                if stream.get('type') not in ('start','error','done'):
                    self.progress = True
        else:
            if kind == 'system' and event.get('subtype') == 'status' and event.get('compact_result'):
                self._compact_error = (event.get('compact_error') or ''
                    if event.get('compact_result') == 'failed' else None)
            if kind == 'result':
                if event.get('local_command') == 'compact' and self._compact_error is not None:
                    # A successful command dispatch is not a successful compact.
                    self._reject(self._compact_error)
                elif event.get('is_error') or event.get('subtype') != 'success':
                    self._reject(event)
                else:
                    self.rejection_reason = ''
            if kind == 'assistant':
                message=_object(event.get('message'))
                # Pinned Claude emits this zero-output placeholder when resuming
                # a historical user summary, before the real prompt is processed.
                placeholder=(message.get('model')=='<synthetic>' and
                    message.get('stop_reason')=='stop_sequence' and
                    message.get('content')==[{'type':'text','text':'No response requested.'}])
                if placeholder:return
                if event.get('error') or event.get('isApiErrorMessage') or event.get('is_api_error_message'):
                    self._reject([event.get('error'),
                        [b.get('text') for b in (message.get('content') or [])
                         if isinstance(b, dict)]], retain=True)
                else:
                    self.progress = True
            if kind == 'stream_event':
                stream=_object(event.get('event'))
                block=_object(stream.get('content_block'))
                if (stream.get('type')=='content_block_delta' or
                        (stream.get('type')=='content_block_start' and
                         (block.get('type')=='tool_use' or block.get('text') or block.get('thinking')))):
                    self.progress = True
            if kind in ('tool_progress', 'tool_use_summary') or (
                    kind=='system' and str(event.get('subtype') or '').startswith('hook_')):
                self.progress = True
            if kind=='attachment' and str(_object(event.get('attachment')).get('type') or '').startswith('hook_'):
                self.progress = True  # Native hooks can execute arbitrary commands too.

    def retryable(self, terminal, cancelled=False):
        return terminal == 'failed_recoverable' and self.rejected and not self.progress and not cancelled
