"""Process-local admission and durable host-controlled restart transaction."""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import contextlib
import fcntl
import json
import os
import re
import threading
import time
import subprocess
import shutil
import sys

import runtime_identity

_LOCK = threading.RLock()
_STATE = "running"
_ACTIVE = 0
_CRITICAL = {}
_BLOCKER_PROVIDERS = {}
_OPERATION = ""
_ERROR = ""
_SHORT_CRITICAL = frozenset({'http_write','admission','app_control_enqueue','app_control_dispatch'})
HOST_CONTROL_TOKEN = runtime_identity.HOST_CONTROL_TOKEN


def _control_root():
    return os.path.expanduser("~/.local/state/desktron/host-control")


def lifecycle_lock_path():
    return os.path.join(_control_root(), "lifecycle.lock")


def _atomic(path, value):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def admitted():
    with _LOCK:
        return _STATE == "running"


def seal_for_exit():
    global _STATE
    with _LOCK:
        _STATE = "stopping"


def reopen_legacy_gate():
    global _STATE
    with _LOCK:
        if _STATE == "stopping" and not _OPERATION:
            _STATE = "running"
            return True
        return False


def begin(*, critical=""):
    global _ACTIVE
    with _LOCK:
        if _STATE != "running":
            return False
        _ACTIVE += 1
        if critical:
            _CRITICAL[critical] = _CRITICAL.get(critical, 0) + 1
        return True


def finish(*, critical=""):
    global _ACTIVE
    with _LOCK:
        _ACTIVE = max(0, _ACTIVE - 1)
        if critical:
            value = _CRITICAL.get(critical, 0) - 1
            if value > 0:
                _CRITICAL[critical] = value
            else:
                _CRITICAL.pop(critical, None)


@contextlib.contextmanager
def work(*, critical=""):
    ok = begin(critical=critical)
    try:
        yield ok
    finally:
        if ok:
            finish(critical=critical)


def register_blocker(name, provider):
    with _LOCK:
        _BLOCKER_PROVIDERS[str(name)] = provider


def blockers():
    with _LOCK:
        rows = [{"kind": key, "count": value}
                for key, value in sorted(_CRITICAL.items()) if value]
        providers = list(_BLOCKER_PROVIDERS.items())
    for name, provider in providers:
        try:
            value = int(provider() or 0)
        except Exception:
            value = 1
        if value:
            rows.append({"kind": name, "count": value})
    return rows


def restarting():
    with _LOCK:
        return bool(_STATE == "stopping" and _OPERATION)


def status():
    with _LOCK:
        return {"state": _STATE, "operation_id": _OPERATION, "error": _ERROR,
                "active_work": _ACTIVE,
                "blockers": [{"kind": key, "count": value}
                             for key, value in sorted(_CRITICAL.items()) if value]}


def reopen(operation_id):
    global _STATE, _OPERATION, _ERROR
    with _LOCK:
        if _STATE in ("checking", "stopping", "restart_error") and _OPERATION == operation_id:
            _STATE = "running"; _OPERATION = ""; _ERROR = ""
            return True
        return False


def mark_error(operation_id, message):
    global _STATE, _ERROR
    with _LOCK:
        if _OPERATION == operation_id and _STATE in ("checking", "stopping"):
            _STATE = "restart_error"; _ERROR = str(message or "重启状态未知")[:300]
            return True
        return False


def _system_blockers():
    rows = []
    probes = (
        ("arrangement", lambda: __import__("arrangement.scheduler", fromlist=["has_running"]).has_running()),
        ("larkbot", lambda: __import__("larkbot.runner", fromlist=["active_count"]).active_count()),
        ("app_control", lambda: __import__("app_control.runtime", fromlist=["runtime"]).runtime().lifecycle_critical_count()),
        ("tool_terminal", lambda: len(__import__("term")._term_list())),
    )
    for name, probe in probes:
        try:
            value = int(probe() or 0)
        except Exception:
            value = 1
        if value:
            rows.append({"kind": name, "count": value})
    try:
        import engine
        with engine._adapters_lock:
            adapters = list(engine._adapters.values())
        value = sum(sum(len(items) for items in (getattr(adapter, "_bg_tasks", {}) or {}).values())
                    for adapter in adapters)
    except Exception:
        value = 1
    if value:
        rows.append({"kind": "background_command", "count": value})
    return rows


def prepare_restart(operation_id, expected_boot_id=""):
    """Atomically stop new admission, then inspect external blocker stores."""
    global _STATE, _OPERATION, _ERROR
    if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        return {"ok": False, "error": "重启操作编号无效"}
    if expected_boot_id != runtime_identity.BOOT_ID:
        return {"ok": False, "error": "重启请求对应的后端已经换代",
                "boot_id": runtime_identity.BOOT_ID}
    with _LOCK:
        if _STATE == "stopping" and _OPERATION == operation_id:
            return {"ok": True, "state": _STATE, "operation_id": _OPERATION}
        if _STATE != "running":
            return {"ok": False, "error": "Desk 生命周期操作正在执行", **status()}
        _STATE = "checking"; _OPERATION = operation_id; _ERROR = ""
    # External side effects reject immediately. Only already-admitted local
    # critical sections get a bounded grace period.
    initial = blockers() + _system_blockers()
    non_short = [row for row in initial if row.get('kind') not in _SHORT_CRITICAL]
    if non_short:
        with _LOCK:
            if _STATE == 'checking' and _OPERATION == operation_id:
                _STATE = 'running'; _OPERATION = ''; _ERROR = ''
        return {"ok": False, "error": "仍有不可中断操作", "blockers": non_short}
    deadline = time.monotonic() + 2
    while True:
        with _LOCK:
            short = {key:value for key,value in _CRITICAL.items()
                     if key in _SHORT_CRITICAL and value}
        if not short or time.monotonic() >= deadline:
            break
        time.sleep(.02)
    # Providers own their own locks. Read them after admission is closed and
    # without holding _LOCK, so their dispatch paths cannot deadlock us.
    blocked = blockers() + _system_blockers()
    with _LOCK:
        if _STATE != "checking" or _OPERATION != operation_id:
            return {"ok": False, "error": "Desk 生命周期状态已变化", **status()}
        if blocked:
            _STATE = "running"; _OPERATION = ""; _ERROR = ""
            return {"ok": False, "error": "仍有不可中断操作", "blockers": blocked}
        _STATE = "stopping"
        return {"ok": True, "state": _STATE, "operation_id": _OPERATION}


def _response_path(operation_id):
    return os.path.join(_control_root(), "response-%s.json" % operation_id)


def _update_response_path(operation_id):
    return os.path.join(_control_root(), "update-response-%s.json" % operation_id)


def _read_response(operation_id, facts):
    try:
        with open(_response_path(operation_id), encoding="utf-8") as handle:
            value = json.load(handle)
        return value if _response_matches(value, operation_id, facts) else None
    except Exception:
        return None


def reconcile_host_restart(operation_id, facts, *, timeout=20):
    """Resolve a lost ACK while this backend is still alive."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = _read_response(operation_id, facts)
        state = str((value or {}).get("state") or "")
        if state in ("rejected", "failed", "cancelled"):
            reopen(operation_id)
            return state
        if state in ("accepted", "completed", "stopping"):
            time.sleep(.1); continue
        if facts.get('supervisor') == 'systemd':
            try:
                unit_state = _systemd_unit_state('desktron-restart-' + operation_id + '.timer')
                service_state = _systemd_unit_state('desktron-restart-' + operation_id + '.service')
            except Exception:
                unit_state = service_state = 'unknown'
            if unit_state == service_state == 'not_found':
                reopen(operation_id); return 'not_found'
        time.sleep(.1)
    mark_error(operation_id, "原生宿主重启状态无法确认")
    return "restart_error"


def _response_matches(value, operation_id, facts):
    return bool(isinstance(value, dict)
        and value.get("operation_id") == operation_id
        and str(value.get("host_instance_id") or "") == str(facts.get("host_instance_id") or "")
        and str(value.get("request_launch_generation") or "") == str(facts.get("launch_generation") or "")
        and value.get("request_boot_id") == facts.get("boot_id")
        and int(value.get("request_pid") or 0) == int(facts.get("pid") or 0))


def _linux_start_token(pid):
    try:
        tail = open('/proc/%d/stat' % int(pid), encoding='utf-8').read().rsplit(')', 1)[1].split()
        return tail[19]
    except Exception:
        return ''


def _systemd_unit_state(unit):
    try:
        result = subprocess.run(['systemctl','--user','show',unit,
                                 '--property=LoadState','--value'],
                                capture_output=True,text=True,timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return 'unknown'
    if result.returncode == 0 and result.stdout.strip() == 'loaded':
        return 'loaded'
    text = (result.stderr or result.stdout or '').lower()
    return 'not_found' if 'not-found' in text or 'not found' in text else 'unknown'


def _systemd_unit_exists(unit):
    return _systemd_unit_state(unit) == 'loaded'


def submit_host_restart(operation_id, *, expires_s=8):
    """Transfer stop responsibility to the platform supervisor."""
    facts = runtime_identity.facts()
    if facts.get("supervisor") == "systemd":
        # Submit to the manager synchronously.  Once systemctl returns, stop,
        # timeout/KILL and relaunch no longer depend on this Python process.
        job = "desktron-restart-" + operation_id
        if _systemd_unit_exists(job + '.timer') or _systemd_unit_exists(job + '.service'):
            return {"ok": True, "state": "accepted", "operation_id": operation_id}
        token = _linux_start_token(facts.get('pid'))
        if not token:
            return {"ok": False, "state": "rejected", "error": "无法读取后端进程身份"}
        root = _control_root(); os.makedirs(root, mode=0o700, exist_ok=True)
        source = os.path.join(os.path.dirname(__file__), 'systemd_restart.py')
        helper = os.path.join(root, 'systemd-restart-' + operation_id + '.py')
        temporary = helper + '.tmp'
        shutil.copyfile(source, temporary); os.chmod(temporary, 0o700); os.replace(temporary, helper)
        result_path = _response_path(operation_id)
        from deskcfg import UI_PORT
        result = subprocess.run(["systemd-run", "--user", "--unit=" + job,
                                 "--on-active=1s", "--collect", sys.executable, helper,
                                 "--boot", str(facts.get('boot_id') or ''),
                                 "--pid", str(facts.get('pid') or 0),
                                 "--start-token", token, "--port", str(UI_PORT),
                                 "--result", result_path, "--operation-id", operation_id],
                                capture_output=True, text=True, timeout=5)
        if result.returncode:
            states = (_systemd_unit_state(job + '.timer'), _systemd_unit_state(job + '.service'))
            if 'loaded' in states:
                return {"ok": True, "state": "accepted", "operation_id": operation_id}
            if states == ('not_found', 'not_found'):
                return {"ok": False, "state": "rejected",
                        "error": (result.stderr or result.stdout or "systemd restart failed")[-300:]}
            return {"ok": False, "state": "unknown",
                    "error": "systemd 已接收状态无法确认"}
        return {"ok": True, "state": "accepted", "operation_id": operation_id}
    token = HOST_CONTROL_TOKEN
    if not facts.get("supervised") or not token:
        return {"ok": False, "error": "当前 Desk 不由新版原生宿主监督"}
    root = _control_root()
    os.makedirs(root, mode=0o700, exist_ok=True)
    response = _response_path(operation_id)
    request = os.path.join(root, "request-%s.json" % operation_id)
    try:
        with open(response, encoding="utf-8") as handle:
            previous = json.load(handle)
        if _response_matches(previous, operation_id, facts) and previous.get("state") in (
                "accepted", "completed", "rejected", "failed", "cancelled"):
            return {"ok": previous.get("state") in ("accepted", "completed"), **previous}
        os.remove(response)
    except FileNotFoundError:
        pass
    except Exception:
        try: os.remove(response)
        except OSError: pass
    _atomic(request, {"operation_id": operation_id,
                      "host_instance_id": facts["host_instance_id"],
                      "launch_generation": facts["launch_generation"],
                      "boot_id": facts["boot_id"], "pid": facts["pid"],
                      "token": token, "expires_at": time.time() + expires_s})
    deadline = time.monotonic() + expires_s
    last = None
    while time.monotonic() < deadline:
        try:
            with open(response, encoding="utf-8") as handle:
                value = json.load(handle)
            if not _response_matches(value, operation_id, facts):
                raise ValueError("response mismatch")
            last = value
            if value.get("state") in ("accepted", "completed"):
                return {"ok": True, **value}
            if value.get("state") in ("rejected", "failed", "cancelled"):
                return {"ok": False, **value}
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            pass
        time.sleep(.05)
    try:
        with open(response, encoding="utf-8") as handle:
            value = json.load(handle)
        if _response_matches(value, operation_id, facts) and value.get("state") in (
                "accepted", "completed", "rejected", "failed", "cancelled"):
            return {"ok": value.get("state") in ("accepted", "completed"), **value}
    except Exception:
        pass
    # Arbitrate timeout with the host through the same flock.  If we acquire it,
    # the host cannot concurrently claim this request; removing the exact file
    # makes not_found final.  If the lock is held or the request disappeared,
    # acceptance may already have happened and admission must stay closed.
    arbitration = try_update_lock()
    if arbitration is not None:
        try:
            value = _read_response(operation_id, facts)
            if value and value.get("state") in ("accepted", "completed", "stopping"):
                return {"ok": True, **value}
            if value and value.get("state") in ("rejected", "failed", "cancelled"):
                return {"ok": False, **value}
            try:
                with open(request, encoding="utf-8") as handle:
                    pending = json.load(handle)
                if (pending.get("operation_id") == operation_id
                        and pending.get("host_instance_id") == facts.get("host_instance_id")
                        and pending.get("launch_generation") == facts.get("launch_generation")
                        and pending.get("boot_id") == facts.get("boot_id")
                        and int(pending.get("pid") or 0) == int(facts.get("pid") or 0)):
                    os.remove(request)
                    return {"ok": False, "state": "not_found",
                            "error": "原生宿主未接收重启请求"}
            except (FileNotFoundError, json.JSONDecodeError, ValueError):
                pass
        finally:
            release_update_lock(arbitration)
    return {"ok": False, "state": "unknown", "error": "无法确认原生宿主是否接受重启"}


def submit_host_update(operation_id, *, expires_s=8):
    """Ask the exact native host to launch the staged updater and own shutdown."""
    facts = runtime_identity.facts()
    token = HOST_CONTROL_TOKEN
    if facts.get("supervisor") != "native" or not facts.get("supervised") or not token:
        return {"ok": False, "state": "rejected", "error": "当前 Desk 不由新版原生宿主监督"}
    if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        return {"ok": False, "state": "rejected", "error": "更新操作编号无效"}
    root = _control_root()
    os.makedirs(root, mode=0o700, exist_ok=True)
    response = _update_response_path(operation_id)
    request = os.path.join(root, "update-request-%s.json" % operation_id)
    try:
        with open(response, encoding="utf-8") as handle:
            previous = json.load(handle)
        if _response_matches(previous, operation_id, facts) and previous.get("state") in (
                "accepted", "rejected", "failed", "cancelled"):
            return {"ok": previous.get("state") == "accepted", **previous}
        os.remove(response)
    except FileNotFoundError:
        pass
    except Exception:
        try: os.remove(response)
        except OSError: pass
    _atomic(request, {"operation_id": operation_id,
                      "host_instance_id": facts["host_instance_id"],
                      "launch_generation": facts["launch_generation"],
                      "boot_id": facts["boot_id"], "pid": facts["pid"],
                      "token": token, "expires_at": time.time() + expires_s})
    deadline = time.monotonic() + expires_s
    while time.monotonic() < deadline:
        try:
            with open(response, encoding="utf-8") as handle:
                value = json.load(handle)
            if not _response_matches(value, operation_id, facts):
                raise ValueError("response mismatch")
            if value.get("state") == "accepted":
                return {"ok": True, **value}
            if value.get("state") in ("rejected", "failed", "cancelled"):
                return {"ok": False, **value}
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            pass
        time.sleep(.05)
    # The host claims requests while holding this same lock.  Acquiring it proves
    # that a still-present exact request was never accepted and can be cancelled.
    arbitration = try_update_lock()
    if arbitration is not None:
        try:
            try:
                with open(response, encoding="utf-8") as handle:
                    value = json.load(handle)
                if _response_matches(value, operation_id, facts):
                    if value.get("state") == "accepted":
                        return {"ok": True, **value}
                    if value.get("state") in ("rejected", "failed", "cancelled"):
                        return {"ok": False, **value}
            except Exception:
                pass
            try:
                with open(request, encoding="utf-8") as handle:
                    pending = json.load(handle)
                if (pending.get("operation_id") == operation_id
                        and pending.get("host_instance_id") == facts.get("host_instance_id")
                        and pending.get("launch_generation") == facts.get("launch_generation")
                        and pending.get("boot_id") == facts.get("boot_id")
                        and int(pending.get("pid") or 0) == int(facts.get("pid") or 0)):
                    os.remove(request)
                    return {"ok": False, "state": "not_found",
                            "error": "原生宿主未接收更新请求"}
            except (FileNotFoundError, json.JSONDecodeError, ValueError):
                pass
        finally:
            release_update_lock(arbitration)
    return {"ok": False, "state": "unknown", "error": "无法确认原生宿主是否接受更新"}


def cancel_unclaimed(operation_id, facts=None):
    """Cancel only when the platform supervisor proves it never accepted."""
    facts = facts or runtime_identity.facts()
    if facts.get('supervisor') == 'systemd':
        try:
            states = (_systemd_unit_state('desktron-restart-' + operation_id + '.timer'),
                      _systemd_unit_state('desktron-restart-' + operation_id + '.service'))
        except Exception:
            states = ('unknown','unknown')
        return reopen(operation_id) if states == ('not_found','not_found') else False
    fd = try_update_lock()
    if fd is None:
        return False
    try:
        response = _read_response(operation_id, facts)
        if response and response.get("state") in ("accepted", "completed", "stopping"):
            return False
        if response and response.get("state") in ("rejected", "failed", "cancelled"):
            return reopen(operation_id)
        path = os.path.join(_control_root(), "request-%s.json" % operation_id)
        try:
            with open(path, encoding="utf-8") as handle:
                value = json.load(handle)
            if value.get("operation_id") != operation_id:
                return False
            os.remove(path)
            return reopen(operation_id)
        except FileNotFoundError:
            # Host claims under this same flock and persists accepted before
            # removing the request. With neither record present, no claim exists.
            return reopen(operation_id)
        except (json.JSONDecodeError, ValueError):
            return False
    finally:
        release_update_lock(fd)


def control_root():
    return _control_root()


def try_update_lock():
    """Cross-process exclusion shared by restart supervisor and self-update."""
    os.makedirs(_control_root(), mode=0o700, exist_ok=True)
    fd = os.open(lifecycle_lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def release_update_lock(fd):
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
