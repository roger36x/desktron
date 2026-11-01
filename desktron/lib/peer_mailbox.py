"""Persistent request/reply mailbox for cross-session collaboration.

The sender's current turn owns its session lock, so a reply cannot be delivered
as another prompt to that same session until the turn ends.  Requests therefore
store their result outside both conversations; ``desk-msg ask`` polls this
mailbox while its tool call remains active.
"""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import json
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
import fcntl


_ID = re.compile(r"^pm_[0-9a-f]{32}$")
_LOCK = threading.Lock()
_KEEP_SECONDS = 7 * 24 * 60 * 60
_TRANSITIONS = {
    # running 是观测状态，不是结果正确性的前置条件；即使进程在写 running 前
    # 短暂失败，收尾仍应能从 queued 直接落到结果。
    "queued": {"queued", "running", "replied", "failed", "timeout"},
    "running": {"running", "replied", "failed", "timeout"},
    # 超时只代表发送方不再前台等待，不能阻止目标稍后开跑和写回结果。
    "timeout": {"timeout", "running", "replied", "failed"},
    "replied": {"replied", "consumed", "delivering"},
    "failed": {"failed", "consumed", "delivering"},
    "delivering": {"delivering", "delivered"},
    "consumed": {"consumed"},
    "delivered": {"delivered"},
}


def _root(data_home=None):
    base = Path(data_home or os.environ.get(
        "DESKTRON_DATA_HOME",
        os.path.expanduser("~/.local/share/desktron/data")))
    return base / "peer-mailbox"


def _path(request_id, data_home=None):
    if not _ID.fullmatch(str(request_id or "")):
        raise ValueError("invalid peer request id")
    return _root(data_home) / (request_id + ".json")


def _write(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".peer-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read(path):
    try:
        with path.open(encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


@contextmanager
def _locked(root):
    """Serialize updates across the Desk server and desk-msg processes."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = root / ".lock"
    with _LOCK:
        with lock_path.open("a+") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _prune(root, now):
    try:
        rows = list(root.glob("pm_*.json"))
    except OSError:
        return
    for path in rows:
        try:
            if now - path.stat().st_mtime > _KEEP_SECONDS:
                path.unlink()
        except OSError:
            pass


def create(source_sid, target_sid, data_home=None):
    now = int(time.time() * 1000)
    request_id = "pm_" + uuid.uuid4().hex
    row = {
        "id": request_id, "status": "queued",
        "source": "peer",
        "source_sid": str(source_sid or ""),
        "target_sid": str(target_sid or ""),
        "created_at": now, "updated_at": now,
        "reply": "", "error": "",
    }
    root = _root(data_home)
    with _locked(root):
        _prune(root, time.time())
        _write(root / (request_id + ".json"), row)
    return row


def get(request_id, data_home=None):
    try:
        path = _path(request_id, data_home)
    except ValueError:
        return None
    with _locked(path.parent):
        return _read(path)


def recover_abandoned(data_home=None):
    """Close requests that belonged to a previous Desk server process.

    The mailbox intentionally does not persist peer question text, so an
    accepted sidecar cannot be replayed after a crash without inventing input.
    Startup therefore fails accepted nonterminal rows and releases legacy
    detached-watcher delivery claims for explicit ``desk-msg wait`` pickup.
    Call this once before the HTTP server starts accepting requests.
    """
    root = _root(data_home)
    now = int(time.time() * 1000)
    changed = 0
    with _locked(root):
        for path in root.glob("pm_*.json"):
            row = _read(path)
            if row is None:
                continue
            status = row.get("status")
            if status == "delivering":
                row.update({"status": row.get("result_status") or "replied",
                            "updated_at": now,
                            "delivery_error": "Desk 已升级为显式 wait，已撤销旧后台转交"})
            elif row.get("accepted_at") and status in ("queued", "running", "timeout"):
                row.update({"status": "failed", "updated_at": now,
                            "error": "Desk 在隔离问询完成前退出，请重新发起"})
            else:
                continue
            _write(path, row)
            changed += 1
        _prune(root, time.time())
    return changed


def accept(request_id, target_sid, data_home=None):
    """Claim one request for dispatch to its declared target exactly once."""
    path = _path(request_id, data_home)
    with _locked(path.parent):
        row = _read(path)
        if (row is None or row.get("status") != "queued"
                or row.get("target_sid") != str(target_sid or "")
                or row.get("accepted_at")):
            return row, False
        row["accepted_at"] = int(time.time() * 1000)
        row["updated_at"] = row["accepted_at"]
        _write(path, row)
        return row, True


def update(request_id, status, reply="", error="", data_home=None, *, error_code=""):
    if status not in ("queued", "running", "replied", "failed", "timeout",
                      "consumed", "delivering", "delivered"):
        raise ValueError("invalid peer request status")
    path = _path(request_id, data_home)
    with _locked(path.parent):
        row = _read(path)
        if row is None:
            return None
        current = row.get("status")
        if status not in _TRANSITIONS.get(current, set()):
            return row
        # The first terminal failure is the boundary that the caller observed.
        # A cancelled provider worker may unwind later with a generic exception;
        # never let that duplicate erase the actionable original reason.
        if current == "failed" and status == "failed":
            return row
        row.update({"status": status, "updated_at": int(time.time() * 1000)})
        if reply:
            row["reply"] = str(reply)
        if error:
            row["error"] = str(error)[:1000]
        if error_code and re.fullmatch(r"[a-z_]{1,64}", str(error_code)):
            row["error_code"] = str(error_code)
        _write(path, row)
        return row


def claim_result(request_id, status, data_home=None):
    """Atomically claim an unread terminal result for a waiter or notifier."""
    if status not in ("consumed", "delivering"):
        raise ValueError("invalid peer reply claim")
    path = _path(request_id, data_home)
    with _locked(path.parent):
        row = _read(path)
        if row is None or row.get("status") not in ("replied", "failed"):
            return row, False
        row.update({"result_status": row["status"], "status": status,
                    "updated_at": int(time.time() * 1000)})
        _write(path, row)
        return row, True


def release_delivery(request_id, error="", data_home=None):
    """Put a claimed late reply back when notification delivery failed."""
    path = _path(request_id, data_home)
    with _locked(path.parent):
        row = _read(path)
        if row is None or row.get("status") != "delivering":
            return row
        row.update({"status": row.get("result_status") or "replied",
                    "updated_at": int(time.time() * 1000),
                    "delivery_error": str(error or "")[:1000]})
        _write(path, row)
        return row
