"""Reusable native hosts for unified Shepherd execution.

The host may outlive a turn, but a turn remains the ownership boundary: only
an idle, context-aligned host is reusable and every failed boundary drops it.
Durable history continues to live in the unified archive.
"""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import atexit
import collections
import json
import os
import queue
import subprocess
import threading
import time

from .store import Conflict
from .execution_budget import ExecutionDeadline


class ResidentProcess:
    """Line-oriented native host with turn-scoped timeout and cancellation."""

    def __init__(self, command, *, cwd, env, cancel_frame, close_frame=None, lock_path=''):
        from owned_process import command as owned_command, protect
        self.lock_path = str(lock_path or '')
        if self.lock_path:
            os.makedirs(os.path.dirname(self.lock_path), mode=0o700, exist_ok=True)
        # Native JSON-lines protocols are UTF-8, not the Windows ANSI locale.
        self.proc = protect(subprocess.Popen(owned_command(
            command, lock_path=self.lock_path, cwd=cwd), env=env, text=True, encoding='utf-8',
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, bufsize=1))
        self.cancel_frame = dict(cancel_frame)
        self.close_frame = dict(close_frame) if close_frame else None
        self.errors = collections.deque(maxlen=16)
        self.lines = queue.Queue()
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._kill_lock = threading.Lock()
        self._cancel_timer = None
        self._active = False
        self._timed_out = ''
        self._cancelled = False
        self._on_stall = None
        self._on_review = None
        self._stall_epoch = 0
        self._stall_sequence = 0
        self._stall_events = queue.SimpleQueue()
        self._provider_fence = {}
        self.budget = None
        self._stall_observer = threading.Thread(target=self._observe_stalls, daemon=True)
        self._stall_observer.start()
        self._stdout = threading.Thread(target=self._pump_stdout, daemon=True)
        self._stderr = threading.Thread(target=self._pump_stderr, daemon=True)
        self._stdout.start(); self._stderr.start()

    def _observe_stalls(self):
        while True:
            row = self._stall_events.get()
            if row is None:
                return
            epoch, callback, payload = row
            with self._state_lock:
                if epoch != self._stall_epoch:
                    continue
            if not callable(callback):
                continue
            try:
                callback(payload)
            except Exception:
                pass  # Observation must never change native execution.

    def _queue_stall(self, payload):
        self._stall_sequence += 1
        self._stall_events.put((self._stall_epoch, self._on_stall,
                                {**payload, 'sequence':self._stall_sequence}))

    def _touch(self, event=None):
        with self._state_lock:
            if self._active and self.budget is not None:
                recovered = bool(self.budget.touch(event))
                expired = self.budget.claude_timeout_kind()
                if expired:
                    self._timed_out = self.budget.timeout_message(expired)
                if recovered:
                    self._queue_stall({'active':False, 'seconds':0,
                        'activity':{'kind':'working', **self.budget.observation()}})

    def _pump_stdout(self):
        try:
            for line in self.proc.stdout:
                try: event = json.loads(line)
                except ValueError:
                    self._touch()
                    continue
                self._touch(event)
                if isinstance(event, dict): self.lines.put(event)
        except Exception:
            pass
        self.lines.put(None)

    def _pump_stderr(self):
        try:
            while True:
                chunk = os.read(self.proc.stderr.fileno(), 2048)
                if not chunk:
                    return
                self._touch(); self.errors.append(chunk.decode('utf-8', errors='replace'))
        except OSError:
            return

    def begin(self, *, timeout, hard_timeout, operation='turn', on_stall=None,
              on_review=None, review_enabled=False):
        with self._state_lock:
            if self._timed_out:
                raise Conflict(self._timed_out)
            if self._active:
                raise Conflict('Shepherd 常驻原生进程仍在执行上一回合')
            if self.proc.poll() is not None:
                raise Conflict('Shepherd 常驻原生进程已经退出')
            self._active = True; self._cancelled = False
            self._stall_epoch = getattr(self, '_stall_epoch', 0) + 1
            self._stall_sequence = 0
            self._on_stall = on_stall
            self._on_review = on_review
            fence = getattr(self, '_provider_fence', None)
            if fence is None:
                fence = self._provider_fence = {}
            self.budget = ExecutionDeadline(timeout, hard_timeout, operation=operation,
                                            provider_fence=fence,
                                            review_enabled=review_enabled)

    def begin_claude_startup(self):
        with self._state_lock:
            if not self._active or self.budget is None:
                raise Conflict('Claude startup wait has no active turn')
            self.budget.begin_claude_startup()
        self._arm_claude_watch()

    def begin_claude_response(self, dispatch_id):
        with self._state_lock:
            if not self._active or self.budget is None:
                raise Conflict('Claude dispatch wait has no active turn')
            self.budget.begin_claude_response(dispatch_id)
        self._arm_claude_watch()

    def _arm_claude_watch(self):
        with self._state_lock:
            budget = self.budget
            if getattr(self, '_claude_watched_budget', None) is budget:
                return
            self._claude_watched_budget = budget
        threading.Thread(target=self._watch_claude_response, args=(budget,),
                         daemon=True).start()

    def _watch_claude_response(self, budget):
        # Keep the entire turn bounded independently of callbacks/blocked stdin,
        # even after first progress, during approval or automatic compaction.
        while True:
            with self._state_lock:
                if (not self._active or self.budget is not budget or self._cancelled):
                    return
                deadline, kind = budget.claude_termination_deadline()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._timed_out = budget.timeout_message(kind)
                    break
            time.sleep(min(.25, max(.001, remaining)))
        # The poison flag prevents committing/reusing this host if a late
        # terminal races with the exact owned-process shutdown.
        self._kill()

    def send(self, value):
        with self._write_lock:
            self.proc.stdin.write(json.dumps(value, ensure_ascii=False) + '\n')
            self.proc.stdin.flush()
        self._touch()

    def events(self):
        while True:
            with self._state_lock:
                if self._timed_out:
                    raise Conflict(self._timed_out)
                deadline, kind = self.budget.next_deadline()
            now = time.monotonic()
            if now >= deadline:
                if kind == 'idle':
                    with self._state_lock:
                        observation = self.budget.mark_soft_stall()
                        if observation is not None:
                            self._queue_stall(observation)
                    continue
                if kind in ('review', 'review_ack'):
                    with self._state_lock:
                        review = (self.budget.mark_review_due() if kind == 'review'
                                  else self.budget.reject_review(
                                      'execution review acknowledgement timed out'))
                        callback = self._on_review
                    if review is not None:
                        try:
                            if callable(callback): callback(review)
                            elif kind == 'review':
                                with self._state_lock:
                                    self.budget.reject_review('execution review callback unavailable')
                        except Exception as exc:
                            if kind == 'review':
                                with self._state_lock:
                                    self.budget.reject_review(str(exc))
                    continue
                if kind == 'absolute_review_limit':
                    with self._state_lock:
                        review = self.budget.mark_review_due()
                        callback = self._on_review
                    if callable(callback) and review is not None:
                        try:callback(review)
                        except Exception:pass
                self._kill()
                raise Conflict(self.budget.timeout_message(kind))
            try:
                event = self.lines.get(timeout=min(0.25, deadline - now))
            except queue.Empty:
                continue
            if event is None:
                # EOF is not process death. Keep the same budget armed while a
                # writer which closed stdout is still alive; never wait forever.
                try:
                    self.proc.wait(timeout=min(.25, max(.001, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    self.lines.put(None)
                    continue
                # The watchdog can mark a timeout and kill the host while this
                # consumer is waiting for EOF. Preserve that authoritative cause.
                with self._state_lock:
                    if self._timed_out:
                        raise Conflict(self._timed_out)
                self._stderr.join(timeout=.1)
                detail=''.join(self.errors)[-1000:].strip()
                suffix=f'（exit={self.proc.returncode}' + (f'；{detail}' if detail else '') + '）'
                raise Conflict('Shepherd 常驻原生进程异常退出，未确认的回合保持封锁' + suffix)
            with self._state_lock:
                if self._timed_out:
                    raise Conflict(self._timed_out)
            yield event

    def review_prompt(self):
        with self._state_lock:
            return self.budget.review_prompt() if self.budget is not None else None

    def accept_review(self):
        with self._state_lock:
            return self.budget.accept_review() if self.budget is not None else None

    def complete_review(self):
        with self._state_lock:
            return self.budget.complete_review() if self.budget is not None else None

    def reject_review(self, reason):
        with self._state_lock:
            return self.budget.reject_review(reason) if self.budget is not None else None

    def review_snapshot(self):
        with self._state_lock:
            return self.budget.review_snapshot() if self.budget is not None else None

    def finish(self, *, raise_timeout=False):
        with self._state_lock:
            budget = self.budget
            if self._active and budget is not None:
                expired = budget.claude_timeout_kind()
                if expired:
                    self._timed_out = budget.timeout_message(expired)
            self._active = False
            self._stall_epoch = getattr(self, '_stall_epoch', 0) + 1
            self._on_stall = None
            self._on_review = None
            timer, self._cancel_timer = self._cancel_timer, None
        if timer:
            timer.cancel()
        if raise_timeout and self._timed_out:
            self._kill()
            raise Conflict(self._timed_out)

    def cancel(self):
        with self._state_lock:
            if not self._active or self._cancelled:
                return True
            self._cancelled = True
            timer = threading.Timer(3, self._kill); timer.daemon = True
            self._cancel_timer = timer; timer.start()
        def notify():
            try: self.send(self.cancel_frame)
            except (OSError, ValueError): self._kill()
        threading.Thread(target=notify, daemon=True).start()
        return True

    def _kill(self):
        # protected kill asks the owned_process supervisor to enumerate and stop
        # detached descendants before it exits; direct SIGKILL would bypass that.
        with self._kill_lock:
            if self.proc.poll() is None:
                try: self.proc.kill()
                except (ProcessLookupError, PermissionError): pass

    def close(self):
        self.finish()
        if self.proc.poll() is None and self.close_frame is not None:
            try: self.send(self.close_frame)
            except (OSError, ValueError): pass
            try: self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired: pass
        if self.proc.poll() is None: self._kill()
        try: self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired: self.proc.kill(); self.proc.wait(timeout=5)
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                if not stream.closed: stream.close()
            except (OSError, ValueError): pass
        self._stdout.join(timeout=2); self._stderr.join(timeout=2)
        self._stall_events.put(None)
        try: self.proc.stderr.close()
        except (OSError, ValueError): pass


class _Entry:
    def __init__(self, process, key, through_seq):
        self.process = process; self.key = key; self.through_seq = through_seq
        self.busy = True; self.retiring = False; self.close_lock = threading.Lock()
        self.at = time.time()


_HOSTS = {}
_LOCK = threading.Lock()
_IDLE = 1800
_MAX = max(1, int(os.environ.get('DESKTRON_SHEPHERD_PERSIST_MAX', '6') or 6))
def _soft_rss_mb():
    try:
        value = os.environ.get('DESKTRON_SHEPHERD_HOST_RSS_MB')
        return max(0, int(value)) if value not in (None, '') else 384
    except ValueError:
        return 384
_SOFT_RSS_MB = _soft_rss_mb()


def enabled():
    return os.environ.get('DESKTRON_SHEPHERD_PERSIST', '1') != '0'


def _writer_lock_released(path):
    if not path:
        return True
    import fcntl
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def _tree_released(entry):
    return (entry.process.proc.poll() is not None
            and _writer_lock_released(getattr(entry.process, 'lock_path', '')))


def drop(binding_id, expected=None):
    """Close first and remove only after exact process death is observable.

    A failed close leaves a retiring fence in the pool.  That may temporarily
    block this Binding, but it can never admit a second Writer beside an orphan.
    Later reaper/drop attempts retry the same exact Popen identity.
    """
    with _LOCK:
        entry = _HOSTS.get(binding_id)
        if entry is None or (expected is not None and entry.process is not expected):
            return False
        entry.retiring = True
    if not entry.close_lock.acquire(blocking=False):
        return False
    try:
        try: entry.process.close()
        except Exception:
            try: entry.process._kill()
            except Exception: pass
        dead = _tree_released(entry)
        if dead:
            with _LOCK:
                if _HOSTS.get(binding_id) is entry:
                    _HOSTS.pop(binding_id, None)
        return dead
    finally:
        entry.close_lock.release()


def evict(binding_id, expected=None):
    with _LOCK:
        entry = _HOSTS.get(binding_id)
        if entry is None or entry.busy or (expected is not None and entry is not expected):
            return False
    return drop(binding_id, entry.process)


def _trim(keep=''):
    try:
        import resident
        resident.make_room('shepherd-native', keep)
    except Exception:
        pass
    while True:
        with _LOCK:
            if len(_HOSTS) < _MAX:
                return
            idle = [(entry.at, binding_id, entry) for binding_id, entry in _HOSTS.items()
                    if not entry.busy and binding_id != keep]
        if not idle:
            return
        _, binding_id, entry = min(idle)
        if not evict(binding_id, entry):
            return


def acquire(binding_id, key, through_seq, create, *, timing=None):
    """Return an exclusive host whose in-memory context covers *through_seq*."""
    from contextlib import nullcontext
    from .phase_timing import PhaseTiming
    span = timing.span if isinstance(timing, PhaseTiming) else lambda _stage: nullcontext()
    stale = None
    with span('pi.pool_lock'), _LOCK:
        entry = _HOSTS.get(binding_id)
        if entry is not None and entry.retiring:
            if not _tree_released(entry):
                raise Conflict('Shepherd 旧常驻进程仍在退出，禁止并发 Writer')
            _HOSTS.pop(binding_id, None); entry = None
        if entry is not None and entry.process.proc.poll() is not None:
            if not _tree_released(entry):
                entry.retiring = True
                raise Conflict('Shepherd 旧常驻进程仍在退出，禁止并发 Writer')
            _HOSTS.pop(binding_id, None); entry = None
        elif entry is not None and (entry.key != key or entry.through_seq != through_seq):
            entry.retiring = True; stale = entry
        if entry is not None and stale is None:
            if entry.busy:
                raise Conflict('Shepherd 常驻原生进程正在执行')
            entry.busy = True; entry.at = time.time()
            return entry.process, False
    if stale is not None:
        with span('pi.pool_retire'):
            if not drop(binding_id, stale.process):
                raise Conflict('Shepherd 旧常驻进程仍在退出，禁止并发 Writer')
    with span('pi.pool_trim'):
        _trim(binding_id)
    process = create()
    entry = _Entry(process, key, through_seq)
    with _LOCK:
        if _HOSTS.get(binding_id) is None:
            _HOSTS[binding_id] = entry
            return process, True
    process.close()
    raise Conflict('Shepherd 常驻原生进程准入发生并发冲突')


def release(binding_id, process, through_seq):
    with _LOCK:
        entry = _HOSTS.get(binding_id)
        if entry is None or entry.process is not process:
            return False
        entry.through_seq = through_seq; entry.busy = False; entry.at = time.time()
        return True


def over_soft_limit(binding_id, process):
    """Check only an idle, still-owned host. Zero/disabled measurements fail open."""
    if not _SOFT_RSS_MB:
        return False
    with _LOCK:
        entry = _HOSTS.get(binding_id)
        if entry is None or entry.process is not process or entry.busy:
            return False
    try:
        import resident
        rss = resident.tree_rss_bytes(process.proc.pid, fresh=True)
    except Exception:
        return False
    return bool(rss and rss > _SOFT_RSS_MB * (1 << 20))


def rotate_after_commit(binding_id, process, *, engine='', checkpoint=False):
    """Drop a committed idle cache at a compact boundary or Claude RSS ceiling.

    Rotation is an optimization after durable context commit. Cleanup failure
    must not relabel the user turn; the parent-death supervisor remains the
    final process cleanup boundary.
    """
    rotate = bool(checkpoint)
    if not rotate and engine == 'claude':
        rotate = over_soft_limit(binding_id, process)
    if not rotate:
        return False
    try:
        return drop(binding_id, process)
    except Exception:
        return False


def roster():
    with _LOCK:
        rows = list(_HOSTS.items())
    return [(binding_id, entry.process.proc.pid, entry.busy or entry.retiring, entry.at)
            for binding_id, entry in rows if entry.process.proc.poll() is None]


def policy():
    return {'max_hosts':_MAX, 'idle_seconds':_IDLE,
            'claude_soft_rss_mb':_SOFT_RSS_MB, 'enabled':enabled()}


def _reap():
    while True:
        time.sleep(60); now = time.time()
        with _LOCK:
            rows = list(_HOSTS.items())
        for binding_id, entry in rows:
            if not entry.busy and (entry.process.proc.poll() is not None or now - entry.at > _IDLE):
                evict(binding_id, entry)


threading.Thread(target=_reap, daemon=True).start()
atexit.register(lambda: [drop(binding_id) for binding_id in list(_HOSTS)])
try:
    import resident as _resident
    _resident.add_pool('shepherd-native', roster, evict)
except Exception:
    pass
