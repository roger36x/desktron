"""Small stdio-transparent supervisor for Desk-owned native writers.

The supervisor is the process-group leader recorded by the caller. If Desk is
killed (including SIGKILL), it kills its own group; no PID search or model retry.
An optional flock is inherited by the native writer, so killing the supervisor
cannot release the session fence while that writer is still exiting.
"""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import os
import hashlib
import signal
import select
import subprocess
import sys
import threading
import time


def writer_lock_path(directory, session_id):
    """One filename contract for writer acquisition, liveness and deletion."""
    name = str(session_id)
    if oscompat.IS_WINDOWS and any(c in name for c in '<>:"/\\|?*'):
        name = 'writer-' + hashlib.sha256(name.encode('utf-8')).hexdigest()
    return os.path.join(os.fspath(directory), name + '.native-writer.lock')


def command(argv, *, lock_path='', cwd=''):
    """Build the safe supervisor command; the supervisor, not Desk, enters cwd.

    Keeping the outer Popen out of a task directory closes the pre-watchdog gap
    where Python could block in child chdir/exec before returning a process handle.
    """
    return [sys.executable, os.path.abspath(__file__), str(os.getpid()),
            str(lock_path), os.fspath(cwd) if cwd else '', '--', *argv]


def _descendant_process_groups(root_pid):
    """Return detached process groups below *root_pid* without touching peers."""
    try:
        out = subprocess.run(['ps', '-axo', 'pid=,ppid=,pgid='], capture_output=True,
                             text=True, timeout=2, check=False).stdout
    except Exception:
        return set()
    children = {}
    groups = {}
    for line in out.splitlines():
        try:
            pid, ppid, pgid = (int(value) for value in line.split())
        except (TypeError, ValueError):
            continue
        children.setdefault(ppid, []).append(pid)
        groups[pid] = pgid
    pending, descendants = [int(root_pid)], set()
    while pending:
        parent = pending.pop()
        for child in children.get(parent, ()):
            if child in descendants:
                continue
            descendants.add(child); pending.append(child)
    own = os.getpgrp()
    return {groups[pid] for pid in descendants if groups.get(pid) and groups[pid] != own}


def _kill_owned_descendants(root_pid, known_groups=(), known_pids=None):
    """Kill descendants without signalling the supervisor's own group."""
    detached = set(known_groups) | _descendant_process_groups(root_pid)
    for pgid in detached:
        try: os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError): pass
    # macOS NOTE_TRACK follows descendants through double-fork/reparent. Verify
    # the birth identity before signalling so a stale PID can never hit a peer.
    for pid, token in dict(known_pids or {}).items():
        if _same_process(pid, token):
            try: os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError): pass


def _kill_owned_tree(root_pid, known_groups=(), known_pids=None):
    _kill_owned_descendants(root_pid, known_groups, known_pids)
    try: os.killpg(os.getpgrp(), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError): pass


def _proc_tracker(root_pid):
    """Kernel fork tracking where available; returns (queue, tracked identities)."""
    tracked = {}
    if not all(hasattr(select, name) for name in
               ('kqueue', 'kevent', 'KQ_FILTER_PROC', 'KQ_NOTE_FORK', 'KQ_NOTE_TRACK')):
        return None, tracked
    try:
        queue = select.kqueue()
        flags = select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR
        notes = select.KQ_NOTE_FORK | select.KQ_NOTE_TRACK | select.KQ_NOTE_EXIT
        queue.control([select.kevent(root_pid, filter=select.KQ_FILTER_PROC,
                                    flags=flags, fflags=notes)], 0, 0)
        return queue, tracked
    except Exception:
        try: queue.close()
        except Exception: pass
        return None, tracked


def _track_events(queue, tracked, timeout=.05):
    if queue is None:
        time.sleep(timeout)
        return
    try:
        events = queue.control(None, 128, timeout)
    except Exception:
        time.sleep(timeout)
        return
    note_child = getattr(select, 'KQ_NOTE_CHILD', 0)
    note_exit = getattr(select, 'KQ_NOTE_EXIT', 0)
    for event in events:
        pid = int(event.ident)
        if event.fflags & note_exit:
            tracked.pop(pid, None)
        if event.fflags & note_child:
            token = _start_token(pid)
            if token:
                tracked[pid] = token


def _start_token(pid):
    try:
        from shepherd.recovery import _process_start_token
        return _process_start_token(int(pid))
    except Exception:
        return ''


def _same_process(pid, token):
    return bool(token and _start_token(pid) == token)


def protect(proc):
    """Adapter kill/drop stops only the exact registered supervisor."""
    pid = getattr(proc, 'pid', None)
    if isinstance(pid, int) and pid > 0:
        token = _start_token(pid)
        def kill():
            if proc.poll() is not None or not _same_process(pid, token):
                return
            try:
                os.kill(pid, signal.SIGTERM)  # supervisor enumerates detached child groups
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=3)
            except Exception:
                if not _same_process(pid, token):
                    return
                try: os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError: pass
        proc.kill = kill
        proc._desk_start_token = token
    return proc


def _acquire_writer_lock(lock_path):
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + 2
    while True:
        try:
            oscompat.lock_file(fd, exclusive=True, blocking=False)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise RuntimeError('Pi previous writer is still stopping; retry after it exits')
            time.sleep(.05)


def _windows_main(parent, lock_path, child_cwd, argv):
    """Windows：writer 放进 KILL_ON_JOB_CLOSE 作业。

    本进程被结束（包括 TerminateProcess）时作业句柄随之关闭，writer 及其后代一并结束；
    父进程（Desk 后端）退出时主动结束作业。Windows 没有可继承的 flock，锁由本进程持有，
    直到 writer 退出或作业被结束。
    """
    job = oscompat.Job()
    fd = None
    try:
        if lock_path:
            fd = _acquire_writer_lock(lock_path)
        oscompat.watch_parent(parent, job.terminate)
        if not oscompat.process_alive(parent):
            return 1
        child = subprocess.Popen(argv, cwd=child_cwd or None)
        try:
            job.assign(child)
        except OSError:
            child.kill()
            raise
        result = child.wait()
        return result if result >= 0 else 128 - result
    except Exception as exc:
        print(f'Desk native writer: {exc}', file=sys.stderr, flush=True)
        return 1
    finally:
        if fd is not None:
            os.close(fd)
        job.close()


def main():
    parent = int(sys.argv[1])
    lock_path = sys.argv[2]
    # Accept the old argv shape for an in-flight installed bridge, while every
    # new caller passes the target cwd explicitly to the supervisor.
    if len(sys.argv) > 3 and sys.argv[3] == '--':
        child_cwd = ''
        argv = sys.argv[4:]
    else:
        child_cwd = sys.argv[3] if len(sys.argv) > 3 else ''
        if len(sys.argv) < 5 or sys.argv[4] != '--':
            raise RuntimeError('owned writer requires an argv')
        argv = sys.argv[5:]
    if oscompat.IS_WINDOWS:
        if not argv:
            raise RuntimeError('owned writer requires an argv')
        return _windows_main(parent, lock_path, child_cwd, argv)
    import fcntl
    if not argv or os.getpgrp() != os.getpid():
        raise RuntimeError('owned writer requires a dedicated process group')
    # Native RPC cancellation is the graceful path. OS termination is the
    # bounded fallback, installed before spawn so no orphan window exists.
    known_groups = set()
    queue, known_pids = _proc_tracker(os.getpid())
    event_lock = threading.Lock()
    def drain_events(timeout=0):
        with event_lock:
            _track_events(queue, known_pids, timeout)
    def stop_tree(*_):
        # Include forks queued since the watcher's last sample before signalling.
        drain_events(0)
        _kill_owned_tree(os.getpid(), known_groups, known_pids)
    signal.signal(signal.SIGTERM, stop_tree)

    def parent_watch():
        while True:
            # Drain fork events before checking parent death: a child can
            # double-fork and reparent between two user-space process scans.
            drain_events(.05)
            known_groups.update(_descendant_process_groups(os.getpid()))
            if os.getppid() != parent:
                stop_tree()
                return

    # Start before opening the session or spawning a writer. Death in the tiny
    # Popen-to-registration interval is covered without a durable PID claim.
    threading.Thread(target=parent_watch, daemon=True).start()
    if os.getppid() != parent:
        return 1
    fd = None
    try:
        if lock_path:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Pi previous writer is still stopping; retry after it exits')
                    time.sleep(.05)
        child = subprocess.Popen(argv, cwd=child_cwd or None,
                                 pass_fds=(fd,) if fd is not None else ())
        result = child.wait()
        # Strict short-lived callers must not leave a shell `&` descendant
        # behind after the foreground command returns.
        if os.environ.get('DESK_OWNED_STRICT_EXIT') == '1':
            drain_events(.1)
            known_groups.update(_descendant_process_groups(os.getpid()))
            _kill_owned_descendants(os.getpid(), known_groups, known_pids)
        return result if result >= 0 else 128 - result
    except Exception as exc:
        print(f'Desk native writer: {exc}', file=sys.stderr, flush=True)
        return 1
    finally:
        if fd is not None:
            os.close(fd)


if __name__ == '__main__':
    sys.exit(main())
