"""Bounded stdio lifecycle for a single Shepherd-owned native runner."""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import json
import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque

from .store import Conflict
from .execution_budget import ExecutionDeadline


class NativeProcess:
    def __init__(self, command, *, cwd, env, timeout, cancel_frame, on_process=None,
                 on_stall=None, on_review=None, hard_timeout=None, operation='turn',
                 on_timeout=None, review_enabled=False):
        self._on_timeout = on_timeout
        from owned_process import command as owned_command
        # Native JSON-lines protocols are UTF-8, including non-ASCII stdin.
        self.proc = subprocess.Popen(owned_command(command, cwd=cwd), env=env,
            text=True, encoding='utf-8', stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True)
        try:
            if on_process:
                on_process(self.proc)
            self._initialize(timeout, hard_timeout, cancel_frame, operation, on_stall,
                             on_review, review_enabled)
        except BaseException:
            # Thread/selector/resource failures after spawn still own a writer.
            # Stop it here, even if the caller never received this instance.
            self._kill()
            self.proc.wait(timeout=5)
            if hasattr(self, '_watch_condition'):
                with self._watch_condition:
                    self._watch_stopped = True
                    self._watch_condition.notify_all()
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try: stream.close()
                except (OSError, ValueError): pass
            raise

    def _initialize(self, timeout, hard_timeout, cancel_frame, operation='turn', on_stall=None,
                    on_review=None, review_enabled=False):
        self.cancel_frame = cancel_frame
        self.on_stall = on_stall
        self.on_review = on_review
        self._stall_events = queue.SimpleQueue()
        self._stall_sequence = 0
        self._stall_observer = threading.Thread(target=self._observe_stalls, daemon=True)
        self._stall_observer.start()
        self._write_lock = threading.Lock()
        self._cancel_lock = threading.Lock()
        self.cancelled = False
        self.timed_out = False
        self.timeout_kind = ''
        self._cancel_timer = None
        self.errors = deque(maxlen=16)
        self.budget = ExecutionDeadline(timeout, hard_timeout if hard_timeout is not None else timeout * 8,
                                        operation=operation, review_enabled=review_enabled)
        self._watch_condition = threading.Condition()
        self._watch_stopped = False
        self._stderr = threading.Thread(target=self._drain_errors, daemon=True)
        self._stderr.start()
        self._watchdog = threading.Thread(target=self._watch, daemon=True)
        self._watchdog.start()

    def _observe_stalls(self):
        while True:
            payload = self._stall_events.get()
            if payload is None:
                return
            if not callable(self.on_stall):
                continue
            try:
                self.on_stall(payload)
            except Exception:
                pass  # Observation must never change native execution.

    def _queue_stall(self, payload):
        self._stall_sequence += 1
        self._stall_events.put({**payload, 'sequence':self._stall_sequence})

    def _touch(self, event=None):
        recovered = False
        with self._watch_condition:
            if self._watch_stopped:
                return
            recovered = bool(self.budget.touch(event))
            expired = self.budget.claude_timeout_kind()
            if expired:
                self.timed_out = True
                self.timeout_kind = expired
            if recovered:
                self._queue_stall({'active': False, 'seconds': 0,
                    'activity': {'kind': 'working', **self.budget.observation()}})
            self._watch_condition.notify_all()

    def _watch(self):
        while True:
            observation = None
            review = None
            with self._watch_condition:
                while not self._watch_stopped:
                    now = time.monotonic()
                    deadline, kind = self.budget.next_deadline()
                    remaining = deadline - now
                    if remaining > 0:
                        self._watch_condition.wait(remaining)
                        continue
                    if self.proc.poll() is not None:
                        self._watch_stopped = True
                        return
                    if kind == 'idle':
                        observation = self.budget.mark_soft_stall()
                        if observation is not None:
                            self._queue_stall(observation)
                        break
                    if kind == 'review':
                        review = self.budget.mark_review_due()
                        break
                    if kind == 'review_ack':
                        review = self.budget.reject_review(
                            'execution review acknowledgement timed out')
                    elif kind == 'absolute_review_limit':
                        review = self.budget.mark_review_due()
                    # A sidecar can atomically record its failure cause here,
                    # before process termination or a delayed reader sees it.
                    # This hook must only mark state; no blocking I/O.
                    if callable(getattr(self, '_on_timeout', None)):
                        try:
                            self._on_timeout()
                        except Exception:
                            pass  # A diagnostic hook must not prevent termination.
                    self.timed_out = True
                    self.timeout_kind = kind
                    break
                if self._watch_stopped:
                    return
            if observation is not None:
                continue
            if review is not None:
                try:
                    if callable(self.on_review):
                        self.on_review(review)
                    elif not self.timed_out:
                        self.budget.reject_review('execution review callback unavailable')
                except Exception as exc:
                    if not self.timed_out:self.budget.reject_review(str(exc))
                with self._watch_condition:
                    self._watch_condition.notify_all()
                if not self.timed_out:
                    continue
            if self.timed_out:
                self._kill()
                return

    def _stop_watchdog(self):
        with self._watch_condition:
            self._watch_stopped = True
            self._watch_condition.notify_all()
        if threading.current_thread() is not self._watchdog:
            self._watchdog.join(timeout=1)
        self._stall_events.put(None)

    def _drain_errors(self):
        # Drain even when no UI callback exists; a full stderr pipe blocks stdout.
        # Read the fd directly: TextIOWrapper.read(size) may wait for the whole size and
        # would hide small incremental diagnostics from the inactivity watchdog.
        while True:
            try:
                chunk = os.read(self.proc.stderr.fileno(), 2048)
            except OSError:
                return
            if not chunk:
                return
            self._touch()
            self.errors.append(chunk.decode('utf-8', errors='replace'))

    def begin_claude_startup(self):
        with self._watch_condition:
            self.budget.begin_claude_startup()
            self._watch_condition.notify_all()

    def begin_claude_response(self, dispatch_id):
        with self._watch_condition:
            self.budget.begin_claude_response(dispatch_id)
            self._watch_condition.notify_all()

    def send(self, value):
        with self._write_lock:
            self.proc.stdin.write(json.dumps(value, ensure_ascii=False) + '\n')
            self.proc.stdin.flush()

    def review_prompt(self):
        with self._watch_condition:
            return self.budget.review_prompt()

    def accept_review(self):
        with self._watch_condition:
            result = self.budget.accept_review()
            self._watch_condition.notify_all()
            return result

    def complete_review(self):
        with self._watch_condition:
            result = self.budget.complete_review()
            self._watch_condition.notify_all()
            return result

    def reject_review(self, reason):
        with self._watch_condition:
            result = self.budget.reject_review(reason)
            self._watch_condition.notify_all()
            return result

    def review_snapshot(self):
        with self._watch_condition:
            return self.budget.review_snapshot()

    def end_input(self):
        with self._write_lock:
            if not self.proc.stdin.closed:
                self.proc.stdin.close()

    def _kill(self):
        # Only this helper's new process group, never Desk's group.
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # macOS may retain a sandboxed descendant after the group leader
            # exits. Do not replace the original execution error with cleanup.
            if self.proc.poll() is None:
                self.proc.kill()

    def cancel(self):
        with self._cancel_lock:
            if self.cancelled:
                return True
            self.cancelled = True
            # Arm the watchdog before touching a possibly full stdin pipe.
            self._cancel_timer = threading.Timer(3, self._kill)
            self._cancel_timer.daemon = True
            self._cancel_timer.start()
            def notify():
                try:
                    self.send(self.cancel_frame)
                except (OSError, ValueError):
                    self._kill()
            threading.Thread(target=notify, daemon=True).start()
        return True

    def events(self):
        for line in self.proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                self._touch()
                continue
            self._touch(event)
            if self.timed_out:
                raise Conflict(self.budget.timeout_message(self.timeout_kind))
            if isinstance(event, dict):
                yield event
        # stdout EOF does not prove the child exited: it may close fd 1 and keep
        # running. Keep the watchdog armed until wait() observes real process death,
        # otherwise both inactivity and the absolute ceiling can be bypassed.
        self.proc.wait()
        self._stop_watchdog()
        if self.timed_out:
            raise Conflict(self.budget.timeout_message(self.timeout_kind))
        if self.proc.returncode:
            raise Conflict('Shepherd 原生进程异常退出，未确认的回合保持封锁')

    def close(self):
        self._stop_watchdog()
        with self._cancel_lock:
            if self._cancel_timer:
                self._cancel_timer.cancel()
        self._kill()
        self.proc.wait(timeout=5)
        for stream in (self.proc.stdin, self.proc.stdout):
            if not stream.closed:
                try: stream.close()
                except (OSError, ValueError): pass  # A killed reader can leave buffered stdin unwritten.
        self._stderr.join(timeout=2)
        self.proc.stderr.close()
