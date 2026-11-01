#!/usr/bin/env python3
"""脱离 Desk 进程树执行版本切换、启动验证与回滚。

入口只接受 self_update 写出的 state.json。所有目标路径由该文件所在的规范更新目录反推，
不接受任意安装路径参数，避免更新接口变成通用文件替换器。
"""
import os
import sys

# The native host launches a fixed copy from the updates directory, outside the
# payload package. Bootstrap only the canonical product library before importing
# product modules; neither the host nor --describe-contract may rely on an
# inherited PYTHONPATH.
_CANONICAL_HOME = os.environ.get("DESKTRON_CANONICAL_HOME") or os.path.join(
    os.path.expanduser("~"), ".local", "share", "desktron", "home")
_PRODUCT_LIB = os.path.join(_CANONICAL_HOME, "desktron", "lib")
if _PRODUCT_LIB not in sys.path:
    sys.path.insert(0, _PRODUCT_LIB)

import oscompat  # noqa: E402,F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import argparse
import ctypes
import fcntl
import hashlib
import json
import posixpath
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.request

from macos_codesign import (installed_requirement_or_legacy, stable_requirement,
                            verify_stable_app)


PORT = 7765
BOOT_WAIT_S = 90
LOG_PREFIX = "[self-update]"


class NativeParentError(RuntimeError):
    pass


class HandoffPersistenceError(RuntimeError):
    pass


def active_time():
    """Monotonic time that excludes system sleep where the platform supports it."""
    clock = getattr(time, "CLOCK_UPTIME_RAW", None)
    return time.clock_gettime(clock) if clock is not None else time.monotonic()


def say(message):
    print(LOG_PREFIX, message, flush=True)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError("更新状态不是 JSON 对象")
    return value


def atomic_json(path, value):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    oscompat.fsync_directory(os.path.dirname(path) or ".")


def state_patch(path, value, **patch):
    # A failed write must not erase pending identity in the caller's recovery snapshot.
    updated = {**value, **patch, "updated_at": int(time.time())}
    atomic_json(path, updated)
    value.clear()
    value.update(updated)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                return h.hexdigest()
            h.update(chunk)


def safe_extract(archive, target):
    os.makedirs(target, mode=0o700, exist_ok=False)
    root = os.path.realpath(target)
    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getmembers()
        if not members:
            raise ValueError("更新包为空")
        names = []
        for member in members:
            name = str(member.name or "").replace("\\", "/")
            while name.startswith("./"):
                name = name[2:]
            normalized = posixpath.normpath(name)
            if (not normalized or normalized == "." or name.startswith("/") or normalized == ".." or
                    normalized.startswith("../")):
                raise ValueError("更新包包含越界路径")
            names.append(normalized)
        tops = {name.split("/", 1)[0] for name in names}
        if len(tops) != 1:
            raise ValueError("更新包必须只有一个顶层目录")
        package_root = os.path.join(root, next(iter(tops)))
        for member, normalized in zip(members, names):
            dest = os.path.realpath(os.path.join(target, normalized))
            if os.path.commonpath((root, dest)) != root:
                raise ValueError("更新包包含越界路径")
            if not (member.isdir() or member.isreg() or member.issym() or member.islnk()):
                raise ValueError("更新包包含不支持的特殊成员：%s" % member.name)
            if member.issym():
                linkname = str(member.linkname or "").replace("\\", "/")
                if linkname.startswith("/"):
                    raise ValueError("更新包包含越界链接")
                link = os.path.realpath(os.path.join(os.path.dirname(dest), linkname))
                if os.path.commonpath((package_root, link)) != package_root:
                    raise ValueError("更新包包含越界链接")
            elif member.islnk():
                linkname = str(member.linkname or "").replace("\\", "/")
                if linkname.startswith("/"):
                    raise ValueError("更新包包含越界链接")
                link = os.path.realpath(os.path.join(target, posixpath.normpath(linkname)))
                if os.path.commonpath((package_root, link)) != package_root:
                    raise ValueError("更新包包含越界链接")
            # Python 3.9 尚无 tarfile 的 data filter；逐项解包前用 realpath 再查一次，
            # 可挡住归档前项先造软链接、后项再借它写出目标目录的组合路径。
            parent = os.path.realpath(os.path.dirname(os.path.join(target, normalized)))
            if os.path.commonpath((root, parent)) != root:
                raise ValueError("更新包成员路径经过越界链接")
            try:
                tf.extract(member, target)
            except (tarfile.TarError, OSError) as exc:
                raise ValueError("更新包无法安全解包：%s" % str(exc)[:200]) from exc
    return os.path.join(target, next(iter(tops)))


def ps_rows():
    out = subprocess.run(["ps", "-axo", "pid,ppid,command"], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) == 3:
            try:
                rows.append((int(parts[0]), int(parts[1]), parts[2]))
            except ValueError:
                pass
    return rows


def port_owner():
    result = subprocess.run(["lsof", "-nP", "-t", "-iTCP:%d" % PORT, "-sTCP:LISTEN"],
                            capture_output=True, text=True)
    owners = set()
    for item in result.stdout.split():
        try:
            owners.add(int(item))
        except ValueError:
            return -1
    if result.returncode == 1 and not owners:
        return 0
    if result.returncode == 0 and len(owners) == 1:
        return next(iter(owners))
    return -1


def running_desk():
    rows = ps_rows(); backend = port_owner()
    if backend < 0:
        raise RuntimeError("无法唯一确认 Desk 端口监听进程")
    shell = next((pp for pid, pp, _ in rows if pid == backend), 0) if backend else 0
    app = next((cmd.split("/Contents/MacOS/")[0] for pid, _, cmd in rows
                if pid == shell and "/Contents/MacOS/" in cmd), "")
    return backend, shell, app


def alive(pid):
    try:
        os.kill(pid, 0); return True
    except OSError:
        return False


def process_start_token(pid):
    if not isinstance(pid, int) or pid <= 1:
        return ""
    if sys.platform == "darwin":
        try:
            class ProcBSDInfo(ctypes.Structure):
                _fields_ = [("head", ctypes.c_uint32 * 12), ("comm", ctypes.c_char * 16),
                            ("name", ctypes.c_char * 32), ("tail", ctypes.c_uint32 * 6),
                            ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            info = ProcBSDInfo()
            size = libproc.proc_pidinfo(int(pid), 3, 0, ctypes.byref(info), ctypes.sizeof(info))
            if size == ctypes.sizeof(info):
                return "darwin:%d:%d" % (info.start_sec, info.start_usec)
        except (OSError, AttributeError, ValueError):
            pass
        return ""  # Darwin must not downgrade exact birth identity to second precision.
    result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def process_matches_token(pid, token):
    if not token:
        return False
    actual = process_start_token(pid)
    if actual:
        return actual == token
    if alive(pid) and any(row_pid == pid for row_pid, _ in _process_parent_rows()):
        raise RuntimeError("无法复核仍存活的已登记后代身份：pid=%d" % pid)
    return False


def stop(pids):
    pids = [p for p in pids if isinstance(p, int) and p > 1]
    identities = {}
    for pid in pids:
        token = process_start_token(pid)
        if token:
            identities[pid] = token
        elif alive(pid):
            raise RuntimeError("无法确认旧 Desk 进程身份，未发送信号")
    for pid, token in identities.items():
        if process_matches_token(pid, token):
            try: os.kill(pid, signal.SIGTERM)
            except OSError: pass
    end = active_time() + 20
    while active_time() < end and any(process_matches_token(pid, token)
                                      for pid, token in identities.items()):
        time.sleep(.2)
    for pid, token in identities.items():
        if process_matches_token(pid, token):
            try: os.kill(pid, signal.SIGKILL)
            except OSError: pass
    end = active_time() + 5
    while active_time() < end and (port_owner() or any(process_matches_token(pid, token)
                                                        for pid, token in identities.items())):
        time.sleep(.2)
    if port_owner() or any(process_matches_token(pid, token)
                           for pid, token in identities.items()):
        raise RuntimeError("旧 Desk 未确认退出或未释放端口，未切换版本")


def open_app(path):
    subprocess.run(["open", "-a", path], check=False)


def _append_host_log(path, message):
    if not path:
        return
    with open(path, "a", encoding="utf-8") as log:
        log.write("[%s] %s\n" %
                  (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), message))
        log.flush()


def process_group_members(pgid):
    if not isinstance(pgid, int) or pgid <= 1:
        return []
    result = subprocess.run(["ps", "-axo", "pid=,pgid=,state=,command="],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("无法枚举候选宿主进程组")
    members = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid, group = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if group != pgid or parts[2].startswith("Z"):
            continue
        try:
            session = os.getsid(pid)
        except ProcessLookupError as exc:
            # ESRCH for a process which exited after ps is not an escaped live process.
            # kill(0) alone misclassifies zombies; confirm via a fresh non-zombie snapshot.
            if not any(row_pid == pid for row_pid, _ in _process_parent_rows()):
                continue
            raise RuntimeError("无法确认候选宿主进程会话") from exc
        except OSError as exc:
            raise RuntimeError("无法确认候选宿主进程会话") from exc
        if session == pgid:
            members.append((pid, parts[2], parts[3] if len(parts) > 3 else ""))
    return members


def process_group_alive(pgid):
    return bool(process_group_members(pgid))


def _process_parent_rows():
    result = subprocess.run(["ps", "-axo", "pid=,ppid=,state="],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("无法枚举候选宿主事务后代")
    rows = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if not parts[2].startswith("Z"):
            rows.append((pid, ppid))
    return rows


def _descendants(rows, roots):
    descendants = set(roots)
    changed = True
    while changed:
        changed = False
        for pid, ppid in rows:
            if ppid in descendants and pid not in descendants:
                descendants.add(pid); changed = True
    return descendants


def observe_transaction_descendants(process):
    """Durably identify descendants, including children that created another session."""
    saved_receipts = getattr(process, "_desk_descendant_receipts", {})
    receipts = dict(saved_receipts) if isinstance(saved_receipts, dict) else {}
    roots = {int(process.pid)} | {pid for pid, token in receipts.items()
                                 if process_matches_token(pid, token)}
    first = _descendants(_process_parent_rows(), roots)
    for pid in first - roots:
        token = process_start_token(pid)
        if not token:
            # kill(pid, 0) can still succeed for an exited zombie. The wrapper's short-lived
            # sleep child may exit between enumeration and proc_pidinfo. Only a fresh live
            # (non-zombie) row makes missing birth identity uncertain; never bless that case.
            if alive(pid) and any(row_pid == pid for row_pid, _ in _process_parent_rows()):
                raise RuntimeError("无法确认候选宿主后代出生身份：pid=%d" % pid)
            continue
        # Re-read ancestry around token capture. A numeric PID from the first snapshot may
        # have exited and been reused; never convert such a race into authority to signal.
        current_roots = {int(process.pid)} | {saved_pid for saved_pid, saved in receipts.items()
                                              if process_matches_token(saved_pid, saved)}
        second = _descendants(_process_parent_rows(), current_roots)
        if pid not in second or process_start_token(pid) != token:
            if process_matches_token(pid, token):
                raise RuntimeError("候选宿主后代身份或父子关系在登记期间变化")
            continue
        receipts[pid] = token
    process._desk_descendant_receipts = receipts
    return receipts


def _signal_detached_receipts(receipts, pgid, signal_number):
    """Signal only exact observed identities that no longer belong to the owned PGID/SID."""
    for pid, token in list(receipts.items()):
        if not process_matches_token(pid, token):
            continue
        try:
            contained = os.getpgid(pid) == pgid and os.getsid(pid) == pgid
        except OSError as exc:
            if process_matches_token(pid, token):
                raise RuntimeError("无法复核候选宿主脱组后代身份") from exc
            continue
        if not contained:
            try:
                os.kill(pid, signal_number)
            except ProcessLookupError:
                pass


def process_group_summary(members):
    """Keep process identity useful without persisting arbitrary child argv."""
    return [(pid, state, os.path.basename(command.split(None, 1)[0]) if command else "")
            for pid, state, command in members]


def owned_group_leader(process, pgid):
    """Revalidate the transaction anchor immediately before every signal."""
    if process.poll() is not None:
        return False
    try:
        return (process.pid == pgid and os.getpgid(process.pid) == pgid
                and os.getsid(process.pid) == pgid)
    except OSError:
        return False


def tracked_launch_pids(process):
    host_pid = getattr(process, "_desk_host_pid", 0)
    backend_pid = getattr(process, "_desk_backend_pid", 0)
    return [("host", host_pid if isinstance(host_pid, int) else 0),
            ("backend", backend_pid if isinstance(backend_pid, int) else 0)]


# Native launch contract v1. A backend may own a separate PGID (Foundation.Process).
# Never weaken this ownership contract to make a candidate or rollback pass.
NATIVE_LAUNCH_CONTRACT = 1


def native_session_contains(label, pid, pgid):
    return (os.getsid(pid) == pgid
            and (label == "backend" or os.getpgid(pid) == pgid))


def native_process_checks(host_pid, backend_pid, *, pgid=None):
    checks = {"native_parent": host_pid > 1 and backend_pid > 1
              and process_parent(backend_pid) == host_pid}
    try:
        if pgid is None:
            contained = (host_pid > 1 and backend_pid > 1
                         and os.getsid(host_pid) == os.getsid(backend_pid))
        else:
            contained = (host_pid > 1 and backend_pid > 1
                         and native_session_contains("host", host_pid, pgid)
                         and native_session_contains("backend", backend_pid, pgid))
    except OSError:
        contained = False
    checks["process_group" if pgid is not None else "native_session"] = contained
    return checks


def require_tracked_pids_contained(process, pgid):
    """Fail closed if an exact process observed in this launch escaped its owned session."""
    for label, pid in tracked_launch_pids(process):
        if pid <= 1 or not alive(pid):
            continue
        try:
            contained = native_session_contains(label, pid, pgid)
        except OSError as exc:
            if not alive(pid):
                continue
            raise RuntimeError("无法确认候选%s进程身份" % label) from exc
        if not contained:
            raise RuntimeError("候选%s进程已脱离事务进程组" % label)


def process_parent(pid):
    if not isinstance(pid, int) or pid <= 1:
        return 0
    result = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                            capture_output=True, text=True)
    try:
        return int(result.stdout.strip()) if result.returncode == 0 else 0
    except ValueError:
        return 0


def launch_app_exact(path, log_path=None, purpose="new", *, contract_receipt=None):
    """Launch and record one exact host in its own transaction-owned process group."""
    binary = os.path.join(path, "Contents", "MacOS", "desktron")
    resources = os.path.join(path, "Contents", "Resources")
    log_path = log_path or "/tmp/desktron-update-host.log"
    digest = sha256(binary)
    try:
        host_rev = open(os.path.join(resources, "release-rev"), encoding="utf-8").read().strip()
        host_build = open(os.path.join(resources, "release-build"), encoding="utf-8").read().strip()
    except OSError:
        host_rev = host_build = ""
    inode = os.stat(binary).st_ino
    say("launch purpose=%s command=%s rev=%s build=%s sha256=%s inode=%s log=%s" %
        (purpose, json.dumps([binary]), host_rev, host_build, digest, inode, log_path))
    log = open(log_path, "a", encoding="utf-8")
    try:
        log.write("\n[%s] purpose=%s command=%s rev=%s build=%s sha256=%s inode=%s\n" %
                  (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), purpose,
                   json.dumps([binary]), host_rev, host_build, digest, inode))
        log.flush()
        # Keep a transaction-owned session leader alive for the helper's full lifetime. If the
        # native host exits first, the wrapper remains as an unambiguous PGID/SID anchor until
        # this helper either cleans the group or exits after accepting a healthy host.
        receipt_fd, receipt_path = tempfile.mkstemp(prefix="host-child.", dir=os.path.dirname(log_path))
        os.close(receipt_fd); os.chmod(receipt_path, 0o600)
        release_path = receipt_path + ".release"
        command = ('"$1" --update-launch-contract-check "$5"' if contract_receipt else '"$1"')
        wrapper = (command + ' & child=$!; trap "" TERM; printf "%s\\n" "$child" > "$2"; '
                   'while kill -0 "$child" 2>/dev/null && kill -0 "$3" 2>/dev/null; do '
                   '[ -e "$4" ] && exit 0; sleep 0.1; done; '
                   '[ -e "$4" ] && exit 0; kill "$child" 2>/dev/null; wait "$child"; code=$?; '
                   'while kill -0 "$3" 2>/dev/null; do sleep 0.1; done; exit "$code"')
        launch_env = dict(os.environ)
        launch_env["DESKTRON_UPDATE_TRANSACTION"] = "1"
        process = subprocess.Popen(["/bin/sh", "-c", wrapper, "desk-host-wrapper", binary,
                                    receipt_path, str(os.getpid()), release_path,
                                    contract_receipt or ""],
                                   cwd=os.path.dirname(path), stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                   close_fds=True, env=launch_env)
        # start_new_session must make the wrapper both session and process-group leader.
        # Verify immediately; a process that cannot prove this isolation is never signalled as a group.
        process._desk_pgid = process.pid
        try:
            isolated = os.getpgid(process.pid) == process.pid and os.getsid(process.pid) == process.pid
        except OSError:
            isolated = False
        if not isolated:
            try: process.terminate()
            except OSError: pass
            raise RuntimeError("候选宿主未建立独立进程会话")
        host_pid = 0
        deadline = active_time() + 3
        while active_time() < deadline and process.poll() is None:
            try:
                value = open(receipt_path, encoding="utf-8").read().strip()
                host_pid = int(value) if value else 0
            except (OSError, ValueError):
                host_pid = 0
            if host_pid > 1:
                break
            time.sleep(.02)
        try: os.remove(receipt_path)
        except OSError: pass
        try:
            host_isolated = (host_pid > 1 and os.getpgid(host_pid) == process.pid
                             and os.getsid(host_pid) == process.pid)
        except OSError:
            host_isolated = False
        if not host_isolated:
            try: os.killpg(process.pid, signal.SIGKILL)
            except OSError: pass
            raise RuntimeError("候选原生宿主未加入独立进程会话")
        process._desk_host_pid = host_pid
        process._desk_descendant_receipts = {}
        try:
            observe_transaction_descendants(process)
        except Exception:
            if owned_group_leader(process, process.pid):
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                deadline = active_time() + 3
                while process_group_alive(process.pid) and active_time() < deadline:
                    time.sleep(.05)
            if process_group_alive(process.pid):
                raise RuntimeError("候选宿主首次身份登记失败且事务组未清理")
            raise
        process._desk_release_path = release_path
        process._desk_log_path = log_path
        process._desk_purpose = purpose
        _append_host_log(log_path, "launched purpose=%s pid=%d host_pid=%d pgid=%d" %
                         (purpose, process.pid, host_pid, process._desk_pgid))
        return process
    finally:
        if 'receipt_path' in locals():
            try: os.remove(receipt_path)
            except OSError: pass
        log.close()


def release_exact(process, timeout=3):
    """Release the group leader only after the launched native host was accepted."""
    path = str(getattr(process, "_desk_release_path", "") or "")
    if not path or process.poll() is not None:
        raise RuntimeError("候选宿主事务 leader 无法安全释放")
    with open(path, "x", encoding="utf-8") as marker:
        marker.write("accepted\n")
    deadline = active_time() + timeout
    while process.poll() is None and active_time() < deadline:
        time.sleep(.05)
    try: os.remove(path)
    except OSError: pass
    if process.poll() is None:
        raise RuntimeError("候选宿主事务 leader 未退出")


def stop_exact(process, timeout=8):
    """Stop only a group whose transaction-owned leader remains an exact live anchor."""
    if not process:
        return
    pgid = int(getattr(process, "_desk_pgid", 0) or 0)
    log_path = str(getattr(process, "_desk_log_path", "") or "")
    purpose = str(getattr(process, "_desk_purpose", "host") or "host")
    if pgid <= 1 or pgid == os.getpgrp():
        raise RuntimeError("拒绝清理未验证的宿主进程组")
    if not owned_group_leader(process, pgid):
        raise RuntimeError("事务进程组 leader 身份已丢失，拒绝按裸 PGID 发信号")
    require_tracked_pids_contained(process, pgid)
    receipts = observe_transaction_descendants(process)
    members = process_group_members(pgid)
    if not any(pid == process.pid for pid, _, _ in members):
        raise RuntimeError("无法在进程表中确认事务进程组 leader")
    _append_host_log(log_path, "stop purpose=%s pid=%s pgid=%d members=%s port_owner=%d" %
                     (purpose, getattr(process, "pid", 0), pgid,
                      json.dumps(process_group_summary(members), ensure_ascii=False), port_owner()))
    if members:
        if not owned_group_leader(process, pgid):
            raise RuntimeError("事务进程组 leader 在 TERM 前已变化，拒绝发信号")
        require_tracked_pids_contained(process, pgid)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    _signal_detached_receipts(receipts, pgid, signal.SIGTERM)
    deadline = active_time() + timeout
    remaining = process_group_members(pgid)
    while (any(pid != process.pid for pid, _, _ in remaining)
           or any(process_matches_token(pid, token) for pid, token in receipts.items())) \
            and active_time() < deadline:
        if owned_group_leader(process, pgid):
            receipts = observe_transaction_descendants(process)
            _signal_detached_receipts(receipts, pgid, signal.SIGTERM)
        time.sleep(.1)
        remaining = process_group_members(pgid)
    if any(pid != process.pid for pid, _, _ in remaining):
        if not owned_group_leader(process, pgid):
            raise RuntimeError("事务进程组 leader 在 KILL 前已变化，拒绝发信号")
        require_tracked_pids_contained(process, pgid)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _signal_detached_receipts(receipts, pgid, signal.SIGKILL)
    elif any(pid == process.pid for pid, _, _ in remaining):
        if not owned_group_leader(process, pgid):
            raise RuntimeError("事务进程组 leader 在结束前已变化，拒绝发信号")
        require_tracked_pids_contained(process, pgid)
        try:
            os.kill(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _signal_detached_receipts(receipts, pgid, signal.SIGKILL)
    _signal_detached_receipts(receipts, pgid, signal.SIGKILL)
    deadline = active_time() + 3
    while (process_group_alive(pgid)
           or any(process_matches_token(pid, token) for pid, token in receipts.items())) \
            and active_time() < deadline:
        time.sleep(.1)
    remaining = process_group_members(pgid)
    code = process.poll()
    _append_host_log(log_path, "stopped purpose=%s pid=%s pgid=%d exit=%s remaining=%s port_owner=%d" %
                     (purpose, getattr(process, "pid", 0), pgid, code,
                      json.dumps(process_group_summary(remaining), ensure_ascii=False), port_owner()))
    if remaining:
        raise RuntimeError("宿主进程组未退出：pgid=%d members=%s" %
                           (pgid, process_group_summary(remaining)))
    escaped = [(label, pid) for label, pid in tracked_launch_pids(process)
               if pid > 1 and alive(pid)]
    escaped += [("descendant", pid) for pid, token in receipts.items()
                if process_matches_token(pid, token)]
    if escaped:
        raise RuntimeError("候选精确进程未退出：%s" % escaped)


def wait_backend_down(timeout=8, stable_for=.5):
    deadline = active_time() + timeout
    empty_since = None
    while active_time() < deadline:
        owner = port_owner()
        now = active_time()
        if not owner:
            empty_since = now if empty_since is None else empty_since
            if now - empty_since >= stable_for:
                return True
        else:
            empty_since = None
        time.sleep(.1)
    return False


def require_native_parent(host_pid):
    if host_pid <= 1 or os.getppid() != host_pid:
        raise NativeParentError("更新器不是已授权原生宿主的直接子进程")


def wait_native_host_exit(host_pid, timeout=30):
    """Observe direct-parent loss after the caller verified it before signalling ready."""
    deadline = active_time() + timeout
    while active_time() < deadline:
        if os.getppid() != host_pid:
            return
        time.sleep(.05)
    raise RuntimeError("原生宿主未在期限内退出，未切换版本")


def acquire_lifecycle_lock(timeout=10):
    root = os.path.expanduser("~/.local/state/desktron/host-control")
    os.makedirs(root, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(root, "lifecycle.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = active_time() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if active_time() >= deadline:
                os.close(fd)
                raise RuntimeError("宿主生命周期锁未释放，未切换版本")
            time.sleep(.05)


def sleep_inhibitor_assertions_present(process):
    if process is None or process.poll() is not None:
        return False
    try:
        result = subprocess.run(["/usr/bin/pmset", "-g", "assertions"], capture_output=True,
                                text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if isinstance(getattr(result, "returncode", 0), int) and result.returncode != 0:
        return False
    owned = "pid %d(caffeinate)" % process.pid
    owned_lines = [line for line in result.stdout.splitlines() if owned in line]
    return (any("PreventSystemSleep" in line for line in owned_lines)
            and any("PreventUserIdleSystemSleep" in line for line in owned_lines))


def acquire_sleep_inhibitor(log_path=""):
    """Keep the Mac awake for the whole host switch, before the old host exits."""
    if sys.platform != "darwin":
        return None
    command = ["/usr/bin/caffeinate", "-is", "-w", str(os.getpid())]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, close_fds=True)
    deadline = active_time() + 3
    while active_time() < deadline and process.poll() is None:
        if sleep_inhibitor_assertions_present(process):
            _append_host_log(log_path, "sleep inhibitor ready pid=%d command=%s" %
                             (process.pid, json.dumps(command)))
            return process
        time.sleep(.05)
    try:
        process.terminate(); process.wait(timeout=1)
    except Exception:
        try: process.kill()
        except Exception: pass
    raise RuntimeError("无法建立宿主切换防睡眠断言")


def require_sleep_inhibitor(process):
    if sys.platform == "darwin" and not sleep_inhibitor_assertions_present(process):
        raise RuntimeError("宿主切换防睡眠断言已失效")


def release_sleep_inhibitor(process, log_path=""):
    """Best-effort cleanup must never overwrite an already persisted transaction outcome."""
    if process is None:
        return
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        message = "sleep inhibitor stopped pid=%d exit=%s" % (process.pid, process.returncode)
    except Exception as exc:
        message = "sleep inhibitor cleanup failed pid=%s error=%s" % (
            getattr(process, "pid", 0), type(exc).__name__)
    try:
        _append_host_log(log_path, message)
    except Exception:
        pass


def signal_native_ready(operation_id):
    root = os.path.expanduser("~/.local/state/desktron/host-control")
    os.makedirs(root, mode=0o700, exist_ok=True)
    path = os.path.join(root, "update-ready-%s.json" % operation_id)
    atomic_json(path, {"operation_id": operation_id, "helper_pid": os.getpid(),
                       "state": "ready", "ready_at": int(time.time())})
    return path


def wait_native_ready_consumed(path, host_pid, timeout=5):
    """File removal is the native host's acknowledgement that it owns the shutdown."""
    deadline = active_time() + timeout
    while active_time() < deadline:
        if not os.path.exists(path):
            return
        require_native_parent(host_pid)
        time.sleep(.05)
    raise RuntimeError("原生宿主未确认更新器就绪，未切换版本")


def prepare_managed_pi_extensions(home):
    """Required runtime dependencies must be ready before the old Desk is stopped."""
    launcher = os.path.join(home, "desktron", "bin", "desktron")
    result = subprocess.run(["bash", launcher, "pi-subagents-setup"],
                            capture_output=True, text=True, timeout=900)
    if result.returncode:
        raise RuntimeError("Pi 子代理扩展检查失败：%s" %
                           (result.stderr or result.stdout or "")[-300:])
    say("Pi 子代理固定依赖已准备")


def prepare_installed_providers(home):
    """Best effort: provider setup uses ambient auth and must never block rollback."""
    launcher = os.path.join(home, "desktron", "bin", "desktron")
    try:
        result = subprocess.run(["bash", launcher, "provider-setup"],
                                capture_output=True, text=True, timeout=60)
        say("Provider 配置已检查" if result.returncode == 0 else "Provider 待用户登录后配置")
    except Exception as exc:
        say("Provider 配置检查跳过：%s" % str(exc)[:160])


def endpoint(path, timeout=2):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (PORT, path), timeout=timeout) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("endpoint response is not an object")
    return value


def remote_wss_expected(remote):
    """Whether the current local status requires Tailscale HTTPS after update."""
    if (not isinstance(remote, dict) or not isinstance(remote.get("local"), bool)
            or not isinstance(remote.get("enabled"), bool)):
        raise ValueError("手机连接状态回执不完整")
    tailscale = remote.get("tailscale")
    if tailscale is None:
        return False
    if (not isinstance(tailscale, dict)
            or not isinstance(tailscale.get("enabled"), bool)
            or not isinstance(tailscale.get("state"), str)):
        raise ValueError("Tailscale 状态回执不完整")
    if tailscale.get("state") != "ready":
        return False
    address = tailscale.get("address")
    if not isinstance(address, str) or not address.startswith("https://"):
        raise ValueError("Tailscale HTTPS 地址回执不完整")
    return tailscale.get("enabled") is True


def probe_predicates(desk, update, remote, *, expect_rev=None, expect_host_rev=None,
                     expect_host_build=None, expect_pgid=None, expect_native_pid=None,
                     port_owner_pid=None, process_running=None, previous_identity=None,
                     expected_identity=None, expect_remote_wss=False):
    checks = {"desk": desk.get("desk") is True,
              "payload_rev": not expect_rev or str(desk.get("rev") or "") == expect_rev,
              "host_rev": not expect_host_rev or str(update.get("host_rev") or "") == expect_host_rev,
              "host_build": expect_host_build is None or update.get("host_build") == expect_host_build}
    if expect_native_pid is not None or expect_pgid is not None:
        try:
            probe_pid = int(desk.get("pid") or 0)
            host_pid = int(desk.get("host_pid") or 0)
        except (TypeError, ValueError):
            probe_pid = host_pid = 0
        if process_running is not None:
            checks["host_process_running"] = process_running is True
        checks["listener_owner"] = probe_pid > 1 and port_owner_pid == probe_pid
        checks["native_pid"] = host_pid > 1 and host_pid == expect_native_pid
        checks.update(native_process_checks(host_pid, probe_pid, pgid=expect_pgid))
    if previous_identity is not None:
        for field in ("boot_id", "host_instance_id", "launch_generation"):
            old = str(previous_identity.get(field) or "")
            new = str(desk.get(field) or "")
            checks["new_" + field] = bool(old) and bool(new) and new != old
    if expected_identity is not None:
        for field in ("boot_id", "host_instance_id", "launch_generation"):
            expected = str(expected_identity.get(field) or "")
            actual = str(desk.get(field) or "")
            checks["same_" + field] = bool(expected) and actual == expected
    if expect_remote_wss:
        tailscale = remote.get("tailscale") if isinstance(remote, dict) else None
        address = tailscale.get("address") if isinstance(tailscale, dict) else None
        checks["remote_local"] = remote.get("local") is True
        checks["remote_wss"] = (isinstance(tailscale, dict)
            and tailscale.get("enabled") is True and tailscale.get("state") == "ready"
            and isinstance(address, str) and address.startswith("https://"))
    return all(checks.values()), checks


def probe_snapshot(raw):
    """Return only identity and health fields needed to diagnose acceptance."""
    allowed = {
        "desk": ("desk", "rev", "boot_id", "host_instance_id", "launch_generation",
                 "pid", "host_pid"),
        "update": ("host_rev", "host_build", "state"),
    }
    snapshot = {}
    for section, fields in allowed.items():
        value = raw.get(section)
        if isinstance(value, dict):
            snapshot[section] = {field: value.get(field) for field in fields if field in value}
    remote = raw.get("remote")
    if isinstance(remote, dict):
        tailscale = remote.get("tailscale")
        snapshot["remote"] = {"local": remote.get("local") is True}
        if isinstance(tailscale, dict):
            address = tailscale.get("address")
            snapshot["remote"].update(
                tailscale_enabled=tailscale.get("enabled") is True,
                tailscale_state=tailscale.get("state") if isinstance(tailscale.get("state"), str) else "",
                tailscale_https=isinstance(address, str) and address.startswith("https://"))
    for field in ("desk_error", "update_error", "remote_error"):
        if field in raw:
            snapshot[field] = True
    return snapshot


def probe(expect_rev=None, timeout=BOOT_WAIT_S, *, expect_host_rev=None,
          expect_host_build=None, expected_process=None, expected_native_pid=None,
          previous_identity=None, expected_identity=None, expect_remote_wss=False,
          stable_for=0, guard=None):
    end = active_time() + timeout; attempt = 0; last = {}; healthy_since = None
    healthy_identity = None
    expect_pgid = (int(getattr(expected_process, "_desk_pgid", 0) or 0)
                   if expected_process is not None else None)
    expect_native_pid = (int(getattr(expected_process, "_desk_host_pid", 0) or 0)
                         if expected_process is not None else expected_native_pid)
    while active_time() < end:
        if guard is not None:
            guard()
        if expected_process is not None:
            observe_transaction_descendants(expected_process)
        attempt += 1; raw = {}
        process_running = (None if expected_process is None else
            (expected_process.poll() is None and expect_native_pid > 1 and alive(expect_native_pid)))
        owner = port_owner()
        try: raw["desk"] = endpoint("/desk-probe")
        except Exception as exc: raw["desk_error"] = str(exc)[:160]
        if expect_host_rev:
            try: raw["update"] = endpoint("/workbench/update-check")
            except Exception as exc: raw["update_error"] = str(exc)[:160]
        if expect_remote_wss:
            try: raw["remote"] = endpoint("/desk-lan-access/status")
            except Exception as exc: raw["remote_error"] = str(exc)[:160]
        ok, checks = probe_predicates(raw.get("desk", {}), raw.get("update", {}), raw.get("remote", {}),
            expect_rev=expect_rev, expect_host_rev=expect_host_rev,
            expect_host_build=expect_host_build, expect_pgid=expect_pgid,
            expect_native_pid=expect_native_pid, port_owner_pid=owner,
            process_running=process_running,
            previous_identity=previous_identity, expected_identity=expected_identity,
            expect_remote_wss=expect_remote_wss)
        if expected_process is not None:
            identity_checks = ("listener_owner", "native_pid", "native_parent", "process_group")
            if all(checks.get(field) is True for field in identity_checks):
                expected_process._desk_backend_pid = int(raw.get("desk", {}).get("pid") or 0)
        process_state = {}
        if expected_process is not None:
            process_state = {"pid":getattr(expected_process, "pid", 0), "pgid":expect_pgid,
                             "exit":expected_process.poll(),
                             "members":process_group_summary(process_group_members(expect_pgid))}
        last = {"attempt":attempt, "expected":{"payload_rev":expect_rev or "",
                "host_rev":expect_host_rev or "", "host_build":expect_host_build,
                "pgid":expect_pgid, "native_pid":expect_native_pid,
                "previous_identity":previous_identity or {},
                "expected_identity":expected_identity or {},
                "remote_wss":bool(expect_remote_wss)}, "actual":probe_snapshot(raw), "checks":checks,
                "process":process_state, "port_owner":owner}
        say("probe " + json.dumps(last, ensure_ascii=False, sort_keys=True))
        if expected_process is not None and not process_running:
            _append_host_log(getattr(expected_process, "_desk_log_path", ""),
                "probe host exited pid=%s pgid=%s exit=%s members=%s port_owner=%d" %
                (getattr(expected_process, "pid", 0), expect_pgid, expected_process.poll(),
                 json.dumps(process_group_summary(process_group_members(expect_pgid)), ensure_ascii=False), owner))
            return False, json.dumps(last, ensure_ascii=False, sort_keys=True)
        if ok:
            if stable_for:
                now = active_time()
                desk_identity = tuple(str(raw.get("desk", {}).get(field) or "")
                                      for field in ("boot_id", "host_instance_id", "launch_generation"))
                if healthy_since is None or desk_identity != healthy_identity:
                    healthy_since = now
                    healthy_identity = desk_identity
                if now - healthy_since < stable_for:
                    time.sleep(.1)
                    continue
            if guard is not None:
                guard()
            if expected_process is not None:
                expected_process._desk_accepted_identity = {
                    key: raw["desk"].get(key) for key in
                    ("boot_id", "host_instance_id", "launch_generation", "pid", "host_pid")}
                _append_host_log(getattr(expected_process, "_desk_log_path", ""),
                    "probe accepted pid=%s pgid=%s exit=%s port_owner=%d" %
                    (getattr(expected_process, "pid", 0), expect_pgid,
                     expected_process.poll(), port_owner()))
            return True, json.dumps(last, ensure_ascii=False, sort_keys=True)
        healthy_since = None
        healthy_identity = None
        time.sleep(.5)
    if expected_process is not None:
        _append_host_log(getattr(expected_process, "_desk_log_path", ""),
            "probe rejected pid=%s pgid=%s exit=%s members=%s port_owner=%d" %
            (getattr(expected_process, "pid", 0), expect_pgid, expected_process.poll(),
             json.dumps(process_group_summary(process_group_members(expect_pgid)), ensure_ascii=False), port_owner()))
    return False, json.dumps(last, ensure_ascii=False, sort_keys=True)


def verify_staged_launch_contract(app, host_log, guard=None):
    """Exercise candidate's production Swift launch primitive BEFORE authorizing old host exit."""
    marker = os.path.join(app, "Contents", "Resources", "update-launch-contract")
    try:
        with open(marker, encoding="utf-8") as handle:
            supported = handle.read().strip() == str(NATIVE_LAUNCH_CONTRACT)
    except OSError:
        supported = False
    if not supported:
        raise RuntimeError("候选壳缺少受支持的启动契约自检，未授权退出旧宿主")
    # Private per-operation location; never reuse stale receipts or execute an unsupported flag.
    with tempfile.TemporaryDirectory(prefix="launch-contract.", dir=os.path.dirname(host_log)) as root:
        receipt = os.path.join(root, "receipt.json")
        process = None
        try:
            if guard: guard()
            process = launch_app_exact(app, host_log, "contract-check", contract_receipt=receipt)
            deadline = active_time() + 8
            while active_time() < deadline:
                if guard: guard()
                observe_transaction_descendants(process)
                if os.path.isfile(receipt):
                    value = read_json(receipt)
                    pid = int(value.get("pid") or 0)
                    host_pid = int(value.get("host_pid") or 0)
                    # Swift can publish its receipt after the preceding process snapshot.
                    # Re-observe after publication, preserving ancestry and birth-token
                    # verification; the receipt alone never authorizes the reported PID.
                    observe_transaction_descendants(process)
                    checks = native_process_checks(host_pid, pid, pgid=process._desk_pgid)
                    checks.update(contract=value.get("contract") == NATIVE_LAUNCH_CONTRACT,
                                  native_pid=host_pid == process._desk_host_pid,
                                  leader=owned_group_leader(process, process._desk_pgid))
                    # Observation must already establish birth identity before trusting a receipt.
                    token = process._desk_descendant_receipts.get(pid)
                    checks["child_identity"] = bool(token and process_matches_token(pid, token))
                    say("launch-contract " + json.dumps(checks, sort_keys=True))
                    if not all(checks.values()):
                        raise RuntimeError("候选壳启动契约自检失败")
                    process._desk_backend_pid = pid
                    return
                if process.poll() is not None or not alive(process._desk_host_pid):
                    break
                time.sleep(.05)
            raise RuntimeError("候选壳启动契约自检未返回有效回执")
        finally:
            # Cleanup must succeed before ready too; never proceed with a leaked test child.
            if process is not None:
                stop_exact(process, timeout=2)


def verify_current_native_contract(state, native_host_pid, expect_rev, expect_remote_wss, guard):
    identity = {key: str(state.get("previous_" + key) or "") for key in
                ("boot_id", "host_instance_id", "launch_generation")}
    ok, detail = probe(expect_rev, timeout=5,
        expect_host_rev=state.get("previous_host_rev"),
        expect_host_build=state.get("previous_host_build"), expected_native_pid=native_host_pid,
        expected_identity=identity, expect_remote_wss=expect_remote_wss, stable_for=.5, guard=guard)
    if not ok:
        raise RuntimeError("当前宿主未通过启动契约预检，未授权切换：" + detail)


def accepted_launch_identity(process):
    identity = getattr(process, "_desk_accepted_identity", None)
    if not isinstance(identity, dict) or not all(identity.get(key) for key in
            ("boot_id", "host_instance_id", "launch_generation", "pid", "host_pid")):
        raise RuntimeError("缺少精确已验收运行身份，拒绝移交事务")
    return dict(identity)


def commit_handoff(state_path, state):
    """Durable proof written ONLY after release_exact, never inferred from version strings.

    A crash before this write stays unknown; a crash after it can reconcile the exact boot.
    """
    keys = ("operation_id", "pending_protocol", "pending_outcome", "pending_rev", "pending_build",
            "pending_host_rev", "pending_host_build", "pending_identity")
    try:
        atomic_json(state_path + ".commit", {key: state.get(key) for key in keys})
    except Exception as exc:
        raise HandoffPersistenceError("宿主已移交，提交回执未持久化") from exc


def recover_native_host(app, host_log, *, expect_rev, expect_host_rev,
                        previous_identity, expect_remote_wss, expect_host_build=None,
                        attempts=8, guard=None, before_release=None, after_release=None):
    """Bounded rollback: never accept a launch without proving the old host is healthy."""
    failures = []
    for attempt in range(1, attempts + 1):
        if not wait_backend_down(12):
            detail = "attempt %d: 7765 被未知进程 %d 占用" % (attempt, port_owner())
            failures.append(detail); say("rollback " + detail)
            return False, "; ".join(failures)[-2000:]
        process = None
        try:
            process = launch_app_exact(app, host_log, "rollback-%d" % attempt)
            ok, detail = probe(expect_rev, timeout=30, expect_host_rev=expect_host_rev,
                               expect_host_build=expect_host_build, expected_process=process,
                               previous_identity=previous_identity,
                               expect_remote_wss=expect_remote_wss, stable_for=.5, guard=guard)
            if ok:
                try:
                    if before_release is not None:
                        before_release(process)
                    release_exact(process)
                except Exception as exc:
                    failures.append("attempt %d release: %s" % (attempt, str(exc)[:160]))
                    try: stop_exact(process)
                    except Exception as stop_exc:
                        failures.append("attempt %d stop: %s" % (attempt, str(stop_exc)[:160]))
                    return False, "; ".join(failures)[-2000:]
                # Handoff is irreversible; persistence failure cannot authorize stopping this host.
                if after_release is not None:
                    after_release(process)
                return True, detail
            failures.append("attempt %d: %s" % (attempt, detail))
        except HandoffPersistenceError:
            raise
        except Exception as exc:
            failures.append("attempt %d launch/probe: %s" % (attempt, str(exc)[:300]))
        if process is not None:
            try:
                stop_exact(process)
            except Exception as exc:
                failures.append("attempt %d stop: %s" % (attempt, str(exc)[:160]))
                return False, "; ".join(failures)[-2000:]
        if not wait_backend_down(12):
            failures.append("attempt %d cleanup: 7765 未释放" % attempt)
            return False, "; ".join(failures)[-2000:]
        if attempt < attempts:
            delay = min(2 ** (attempt - 1), 10)
            say("rollback retry in %ds" % delay)
            time.sleep(delay)
    return False, "; ".join(failures)[-2000:]


def replace_dir(source, target, backup):
    """Perform only the two renames; the outer transaction owns all recovery."""
    if os.path.exists(backup):
        shutil.rmtree(backup)
    if os.path.exists(target):
        os.replace(target, backup)
    os.replace(source, target)


def restore_dir(target, backup):
    if not os.path.exists(backup):
        if os.path.exists(target):
            return
        raise RuntimeError("规范安装目录与备份均不存在")
    failed = target + ".failed"
    if os.path.exists(failed):
        shutil.rmtree(failed)
    if os.path.exists(target):
        os.replace(target, failed)
    os.replace(backup, target)
    if os.path.exists(failed):
        shutil.rmtree(failed, ignore_errors=True)


def read_requirement_marker(path):
    """Read the durable identity without requiring a newer helper module at bootstrap."""
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as stream:
            value = stream.read(8193).strip()
    except OSError as error:
        raise RuntimeError("无法读取已记录的稳定签名身份") from error
    if len(value) > 8192 or not stable_requirement(value):
        raise RuntimeError("已记录的稳定签名身份无效")
    return value


def sync_parent_directory(path):
    if os.name == "nt":
        return
    directory = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_marker(path, value):
    if value:
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(str(value) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            sync_parent_directory(path)
        except Exception:
            try: os.remove(tmp)
            except OSError: pass
            raise
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            return
        sync_parent_directory(path)


def apply(state_path, *, native_host_pid=0, operation_id=""):
    state_path = os.path.realpath(state_path)
    updates = os.path.dirname(state_path)
    base = os.path.dirname(updates)
    if os.path.basename(updates) != "updates" or os.path.basename(state_path) != "state.json":
        raise ValueError("状态文件必须位于规范 updates 目录")
    state = read_json(state_path)
    if state.get("state") != "applying":
        raise ValueError("更新状态不是 applying")
    persisted_operation_id = str(state.get("operation_id") or "")
    if native_host_pid:
        require_native_parent(native_host_pid)
        if not operation_id or persisted_operation_id != operation_id:
            raise ValueError("更新操作身份不匹配")
        if state.get("helper_contract") != 2 or state.get("helper_sha256") != sha256(__file__):
            raise ValueError("更新器契约或执行摘要不匹配，未授权退出旧宿主")
    elif operation_id and operation_id != persisted_operation_id:
        raise ValueError("更新操作身份不匹配")
    operation_id = operation_id or persisted_operation_id
    if not native_host_pid and not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ValueError("更新操作身份无效")
    state_patch(state_path, state, apply_pid=os.getpid(), pending_protocol=0)
    mode = str(state.get("mode") or "release")
    rev = str(state.get("staged_rev") or ""); build = int(state.get("staged_build") or 0)
    work = ""; payload = None
    if mode == "host-only":
        stage = os.path.realpath(str(state.get("staged_app") or ""))
        if os.path.dirname(stage) != updates or not os.path.basename(stage).startswith("dev-host."):
            raise ValueError("开发壳暂存路径无效")
        app = os.path.join(stage, "Desktron.app")
        expected_host_rev = str(state.get("staged_host_rev") or "")
        expected_host_build = int(state.get("staged_host_build") or 0)
    elif mode == "release":
        archive = os.path.realpath(str(state.get("archive") or ""))
        if os.path.commonpath((updates, archive)) != updates or not os.path.isfile(archive):
            raise ValueError("暂存更新包不存在")
        work = os.path.join(os.path.dirname(archive), "unpacked.%d" % os.getpid())
        if os.path.exists(work): shutil.rmtree(work)
        package = safe_extract(archive, work)
        payload = os.path.join(package, "payload"); app = os.path.join(package, "Desktron.app")
        expected_host_rev = rev
        expected_host_build = build
        if not os.path.isfile(os.path.join(payload, "desktron", "bin", "desktron")):
            raise ValueError("更新包缺少 Desk payload")
        if open(os.path.join(payload, ".release-rev"), encoding="utf-8").read().strip() != rev:
            raise ValueError("更新包版本与清单不一致")
        if open(os.path.join(payload, ".release-build"), encoding="utf-8").read().strip() != str(build):
            raise ValueError("更新包构建序号与清单不一致")
        if state.get("sha256") and sha256(archive) != state["sha256"]:
            raise ValueError("应用前复核校验失败")
    else:
        raise ValueError("未知更新模式")
    app_binary = os.path.join(app, "Contents", "MacOS", "desktron")
    if not os.path.isfile(app_binary): raise ValueError("更新包缺少桌面应用")
    app_host_rev = open(os.path.join(app, "Contents", "Resources", "release-rev"), encoding="utf-8").read().strip()
    try:
        app_host_build = int(open(os.path.join(app, "Contents", "Resources", "release-build"),
                                  encoding="utf-8").read().strip())
    except (OSError, ValueError):
        app_host_build = 0
    if (not expected_host_rev or app_host_rev != expected_host_rev
            or not expected_host_build or app_host_build != expected_host_build):
        raise ValueError("桌面应用版本或构建号与清单不一致")
    if mode == "host-only" and state.get("staged_host_sha256") and sha256(app_binary) != state["staged_host_sha256"]:
        raise ValueError("开发壳暂存校验失败")

    app_target = os.path.expanduser("~/Applications/Desktron.app")
    requirement_path = os.path.join(base, ".codesign-requirement")
    try:
        candidate_requirement = verify_stable_app(app)
        expected_requirement = read_requirement_marker(requirement_path)
        current_requirement = (installed_requirement_or_legacy(app_target)
                               if os.path.isdir(app_target) else "")
        if expected_requirement:
            if current_requirement != expected_requirement:
                raise ValueError("当前桌面应用签名与已记录的稳定身份不一致")
            if candidate_requirement != expected_requirement:
                raise ValueError("候选桌面应用签名与已记录的稳定身份不一致")
        elif current_requirement and current_requirement != candidate_requirement:
            raise ValueError("候选桌面应用的签名身份与当前稳定身份不一致")
    except Exception as exc:
        state_patch(state_path, state, state="error", error=str(exc)[:500],
                    fix="桌面应用签名预检失败，安装文件未切换",
                    completion_id=operation_id, completion_outcome="failed",
                    completed_at=int(time.time()), apply_pid=0,
                    acceptance_unknown=False)
        return 1

    home = os.path.join(base, "home")
    home_next, home_prev = os.path.join(base, "home.update-next"), os.path.join(base, "home.prev-update")
    app_next, app_prev = app_target + ".update-next", app_target + ".prev-update"
    shutil.rmtree(app_next, ignore_errors=True)
    if mode == "release":
        shutil.rmtree(home_next, ignore_errors=True); shutil.copytree(payload, home_next, symlinks=True)
    shutil.copytree(app, app_next, symlinks=True)
    if verify_stable_app(app_next) != candidate_requirement:
        raise ValueError("复制后的桌面应用签名身份发生变化")
    if mode == "release":
        prepare_managed_pi_extensions(home_next)

    old_rev = ""
    old_build = ""
    requirement_existed = os.path.isfile(requirement_path)
    old_requirement = expected_requirement
    rev_path, build_path = os.path.join(base, ".update-rev"), os.path.join(base, ".update-build")
    try: old_rev = open(rev_path).read().strip()
    except OSError: pass
    try: old_build = open(build_path).read().strip()
    except OSError: pass
    lifecycle_lock = None; target_app = app_target
    attempted_home = attempted_app = False
    launched_host = None; candidate_released = False
    previous_identity = {"boot_id":str(state.get("previous_boot_id") or ""),
        "host_instance_id":str(state.get("previous_host_instance_id") or ""),
        "launch_generation":str(state.get("previous_launch_generation") or "")}
    previous_host_rev = str(state.get("previous_host_rev") or "")
    previous_host_build = int(state.get("previous_host_build") or 0)
    # Installed markers can lag a development payload. Accept captured runtime identity;
    # marker restoration still restores the original file contents.
    previous_payload_rev = str(state.get("previous_payload_rev") or old_rev or rev)
    previous_payload_build = int(state.get("previous_payload_build", old_build or build or 0))
    host_log = os.path.join(updates, "host-launch-%s.log" % (operation_id or os.getpid()))
    expect_remote_wss = False
    sleep_inhibitor = None
    native_host_exited = False
    ready_published = False
    try:
        if native_host_pid:
            if not all(previous_identity.values()) or not previous_host_rev or not previous_host_build:
                raise RuntimeError("原生宿主缺少可验证的版本、构建号或生命周期身份，未切换版本")
            try:
                remote_before = endpoint("/desk-lan-access/status")
                expect_remote_wss = remote_wss_expected(remote_before)
            except Exception as exc:
                raise RuntimeError("无法确认切换前手机连接状态：%s" % str(exc)[:160]) from exc
            require_native_parent(native_host_pid)
            sleep_inhibitor = acquire_sleep_inhibitor(host_log)
            require_native_parent(native_host_pid)
            guard = lambda: (require_native_parent(native_host_pid),
                             require_sleep_inhibitor(sleep_inhibitor))
            verify_current_native_contract(state, native_host_pid,
                previous_payload_rev, expect_remote_wss, guard)
            verify_staged_launch_contract(app_next, host_log, guard)
            guard()
            state_patch(state_path, state, state="switching", expect_rev=rev,
                        expect_host_rev=expected_host_rev, expect_host_build=expected_host_build,
                        expect_remote_wss=expect_remote_wss, host_log=host_log,
                        acceptance_unknown=True)
            # Mark before writing: an I/O exception can occur AFTER the ready receipt is visible.
            ready_published = True
            ready_path = signal_native_ready(operation_id)
            wait_native_ready_consumed(ready_path, native_host_pid)
            require_sleep_inhibitor(sleep_inhibitor)
            wait_native_host_exit(native_host_pid)
            native_host_exited = True
            require_sleep_inhibitor(sleep_inhibitor)
            lifecycle_lock = acquire_lifecycle_lock()
        else:
            backend, shell, running_app = running_desk()
            if not backend: raise RuntimeError("找不到正在运行的 Desk，未切换版本")
            target_app = running_app or app_target
            state_patch(state_path, state, state="switching", expect_rev=rev,
                        expect_host_rev=expected_host_rev, host_log=host_log,
                        acceptance_unknown=True)
            stop((backend, shell))
        if mode == "release":
            attempted_home = True
            replace_dir(home_next, home, home_prev)
        attempted_app = True
        replace_dir(app_next, app_target, app_prev)
        write_marker(requirement_path, candidate_requirement)
        if native_host_pid:
            require_sleep_inhibitor(sleep_inhibitor)
        if mode == "release":
            write_marker(rev_path, rev); write_marker(build_path, build); prepare_installed_providers(home)
        state_patch(state_path, state, state="verifying")
        if native_host_pid: launched_host = launch_app_exact(app_target, host_log, "candidate")
        else: open_app(app_target)
        ok, detail = probe(rev, expect_host_rev=expected_host_rev,
                           expect_host_build=expected_host_build, expected_process=launched_host,
                           previous_identity=previous_identity, expect_remote_wss=expect_remote_wss,
                           stable_for=.5, guard=((lambda: require_sleep_inhibitor(sleep_inhibitor))
                                  if native_host_pid else None))
        if native_host_pid:
            require_sleep_inhibitor(sleep_inhibitor)
        if not ok: raise RuntimeError("新版本启动自检失败：%s" % detail)
        if launched_host is not None:
            # Use an old-reader protected state, NOT accepted_pending_commit (v1 readers
            # would reconcile that state by version alone when rolling back old payload).
            state_patch(state_path, state, state="verifying", pending_protocol=2,
                        pending_outcome="idle", pending_rev=rev, pending_build=build,
                        pending_host_rev=expected_host_rev,
                        pending_host_build=expected_host_build,
                        pending_identity=accepted_launch_identity(launched_host),
                        acceptance_unknown=True)
            release_exact(launched_host)
            candidate_released = True
            launched_host = None
            commit_handoff(state_path, state)
        completed_at = int(time.time())
        state_patch(state_path, state, state="idle", current_rev=rev, current_build=build,
                    current_host_rev=expected_host_rev, current_host_build=expected_host_build,
                    hasUpdate=False, staged_rev="", staged_build=0,
                    staged_host_rev="", staged_host_build=0, error="", applied_at=completed_at,
                    completion_id=operation_id, completion_outcome="succeeded", completed_at=completed_at,
                    apply_pid=0, operation_id="", acceptance_unknown=False,
                    pending_outcome="", pending_rev="", pending_build=0,
                    pending_host_rev="", pending_host_build=0)
        if work and os.path.basename(work).startswith("unpacked.") and os.path.commonpath((updates, work)) == updates:
            try: shutil.rmtree(work, ignore_errors=True)
            except Exception: pass
        try:
            say(("已激活开发壳 %s" if mode == "host-only" else "已更新到 %s") % expected_host_rev[:12])
        except Exception:
            pass
        return 0
    except Exception as exc:
        if native_host_pid and not ready_published:
            # No shutdown authorization and no install mutation occurred. Do not enter rollback
            # or test a still-healthy old host with the very predicate which just failed.
            state_patch(state_path, state, state="error", error=str(exc)[:500],
                        fix="更新预检失败，未授权退出旧宿主；当前版本未切换",
                        completion_id=operation_id, completion_outcome="failed",
                        completed_at=int(time.time()), apply_pid=0, acceptance_unknown=False)
            return 1
        try: say("更新失败，开始恢复旧版：%s" % exc)
        except Exception: pass
        if candidate_released:
            try:
                state_patch(state_path, state, state="verifying", pending_protocol=2,
                            error=str(exc)[:500],
                            fix="候选宿主已验收并释放；等待运行身份对账完成提交",
                            apply_pid=0, operation_id=operation_id, acceptance_unknown=True)
            except Exception:
                pass
            return 1
        rollback_errors = []
        if native_host_pid and launched_host is not None:
            try:
                stop_exact(launched_host)
                if not wait_backend_down(20): rollback_errors.append("失败候选未释放 7765")
            except Exception as stop_exc:
                rollback_errors.append("停止失败候选壳：%s" % str(stop_exc)[:160])
        elif not native_host_pid:
            try:
                new_backend, new_shell, _ = running_desk()
                if new_backend: stop((new_backend, new_shell))
            except Exception as stop_exc:
                rollback_errors.append("停止失败版本：%s" % str(stop_exc)[:160])
        # An uncertain candidate tree must never coexist with or be mistaken for a rollback host.
        # Preserve the staged files and operation identity for manual recovery instead of replaying.
        if rollback_errors:
            state_patch(state_path, state, state="rollback_failed", error=str(exc)[:500],
                        fix="；".join(rollback_errors)[-2000:], apply_pid=0,
                        completion_id=operation_id, completion_outcome="failed",
                        completed_at=int(time.time()), acceptance_unknown=True)
            return 1
        if attempted_app:
            try: restore_dir(app_target, app_prev)
            except Exception as restore_exc: rollback_errors.append("恢复旧壳文件：%s" % str(restore_exc)[:160])
        if attempted_home:
            try: restore_dir(home, home_prev)
            except Exception as restore_exc: rollback_errors.append("恢复旧 payload：%s" % str(restore_exc)[:160])
        if attempted_app:
            try: write_marker(requirement_path, old_requirement if requirement_existed else "")
            except Exception as marker_exc:
                rollback_errors.append("恢复签名身份标记：%s" % str(marker_exc)[:160])
        if mode == "release":
            try: write_marker(rev_path, old_rev); write_marker(build_path, old_build)
            except Exception as marker_exc: rollback_errors.append("恢复版本标记：%s" % str(marker_exc)[:160])
        if rollback_errors:
            state_patch(state_path, state, state="rollback_failed", error=str(exc)[:500],
                        fix="；".join(rollback_errors)[-2000:], apply_pid=0,
                        operation_id=operation_id, completion_id=operation_id,
                        completion_outcome="failed", completed_at=int(time.time()),
                        acceptance_unknown=True)
            return 1
        if not os.path.exists(app_target) or (mode == "release" and not os.path.exists(home)):
            rollback_errors.append("规范安装目录未完整恢复")
            state_patch(state_path, state, state="rollback_failed", error=str(exc)[:500],
                        fix="；".join(rollback_errors)[-2000:], apply_pid=0,
                        operation_id=operation_id, completion_id=operation_id,
                        completion_outcome="failed", completed_at=int(time.time()),
                        acceptance_unknown=True)
            return 1
        restored_app = app_target
        old_ok = False; old_detail = ""
        if native_host_pid:
            try:
                require_sleep_inhibitor(sleep_inhibitor)
            except Exception as inhibitor_exc:
                rollback_errors.append("回滚前防睡眠断言失效：%s" % str(inhibitor_exc)[:160])
                state_patch(state_path, state, state="rollback_failed", error=str(exc)[:500],
                            fix="；".join(rollback_errors)[-2000:], apply_pid=0,
                            operation_id=operation_id, completion_id=operation_id,
                            completion_outcome="failed", completed_at=int(time.time()),
                            acceptance_unknown=True)
                return 1
        if native_host_pid:
            try:
                if not native_host_exited and os.getppid() == native_host_pid:
                    # Ready may already be consumed with host shutdown queued on the main
                    # thread. Health does not revoke that authorization. Never report rollback
                    # success for a host which can still execute a late authorized exit.
                    old_ok = False
                    old_detail = "ready 已发布但旧宿主尚未退出，退出授权未证明撤销；禁止自动重试"
                else:
                    old_ok, old_detail = recover_native_host(restored_app, host_log,
                        expect_rev=previous_payload_rev or None,
                        expect_host_rev=previous_host_rev or None,
                        expect_host_build=previous_host_build or None,
                        previous_identity=previous_identity,
                        expect_remote_wss=expect_remote_wss,
                        guard=lambda: require_sleep_inhibitor(sleep_inhibitor),
                        after_release=lambda _process: commit_handoff(state_path, state),
                        before_release=lambda process: state_patch(state_path, state,
                            state="verifying", pending_protocol=2, pending_outcome="rolled_back",
                            pending_rev=previous_payload_rev, pending_build=previous_payload_build,
                            pending_host_rev=previous_host_rev,
                            pending_host_build=previous_host_build,
                            pending_identity=accepted_launch_identity(process),
                            acceptance_unknown=True))
            except HandoffPersistenceError:
                # Pending remains sealed; the accepted rollback host has already been released.
                return 1
            except Exception as rollback_exc:
                old_ok = False
                old_detail = "旧版验收异常：%s" % str(rollback_exc)[:300]
        else:
            try:
                open_app(restored_app)
                old_ok, old_detail = probe(previous_payload_rev or None,
                    expect_host_rev=previous_host_rev or None,
                    expect_remote_wss=expect_remote_wss, stable_for=.5)
            except Exception as rollback_exc:
                old_ok = False
                old_detail = "旧版验收异常：%s" % str(rollback_exc)[:300]
        if not old_ok: rollback_errors.append("旧版健康检查失败：%s" % old_detail)
        rollback_ok = old_ok and not rollback_errors
        fix = "旧版已恢复" if rollback_ok else "；".join(rollback_errors)[-2000:]
        state_patch(state_path, state, state=("rolled_back" if rollback_ok else "rollback_failed"),
                    current_rev=previous_payload_rev, current_build=previous_payload_build,
                    current_host_rev=previous_host_rev, current_host_build=previous_host_build,
                    error=str(exc)[:500], fix=fix, apply_pid=0,
                    operation_id=("" if rollback_ok else operation_id),
                    completion_id=operation_id, completion_outcome="failed",
                    completed_at=int(time.time()), acceptance_unknown=not rollback_ok,
                    pending_outcome="", pending_rev="", pending_build=0,
                    pending_host_rev="", pending_host_build=0)
        return 1
    finally:
        try:
            release_sleep_inhibitor(sleep_inhibitor, host_log)
        finally:
            if lifecycle_lock is not None:
                try: fcntl.flock(lifecycle_lock, fcntl.LOCK_UN)
                except Exception: pass
                try: os.close(lifecycle_lock)
                except Exception: pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state")
    parser.add_argument("--describe-contract", action="store_true")
    parser.add_argument("--native-host-pid", type=int, default=0)
    parser.add_argument("--operation-id", default="")
    args = parser.parse_args()
    if args.describe_contract:
        print(json.dumps({"helper_contract": 2, "sha256": sha256(__file__)}))
        return 0
    if not args.state:
        parser.error("--state is required")
    try:
        return apply(args.state, native_host_pid=args.native_host_pid,
                     operation_id=args.operation_id)
    except Exception as exc:
        say("未切换版本：%s" % exc)
        if not isinstance(exc, NativeParentError):
            try:
                state = read_json(args.state)
                uncertain = bool(state.get("acceptance_unknown")) or state.get("state") in (
                    "switching", "verifying", "accepted_pending_commit", "rollback_failed")
                state_patch(args.state, state,
                            state=(state.get("state") if uncertain else "error"),
                            error=str(exc)[:500],
                            fix=("需要人工核对当前宿主与安装文件，禁止自动重试"
                                 if uncertain else "重新检查并下载更新"),
                            completion_id=(str(state.get("operation_id") or "") if not uncertain else ""),
                            completion_outcome=("failed" if not uncertain else ""),
                            completed_at=(int(time.time()) if not uncertain else 0),
                            apply_pid=0, acceptance_unknown=uncertain)
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
