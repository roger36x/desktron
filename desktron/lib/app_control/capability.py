"""App Control Agent 会话能力。

会话号会出现在普通 UI 接口里，不能作为调用方身份。能力只保存在 Desk 后端进程内，
仅注入对应 Agent 进程；不落数据库、日志或前端。
"""
import hmac
import os
import oscompat
import secrets
import socket
import struct
import subprocess
import sys
import threading


_LOCK = threading.RLock()
_CAPABILITIES = {}
_ROOT_PROCESSES = {}
_GENERATIONS = {}


_LOOPBACK_PORT = 0


def set_loopback_port(port):
    """Windows：登记专用 App Control 回环端口。只有连到该端口的请求才取对端 PID。"""
    global _LOOPBACK_PORT
    _LOOPBACK_PORT = int(port or 0)


def channel_address(data_home):
    """返回注入 Agent 进程的本地控制通道地址。

    POSIX 为 Unix Socket 路径；Windows 为 `tcp:127.0.0.1:<端口>`，端口由后端启动时登记。
    """
    if oscompat.IS_WINDOWS:
        return "tcp:127.0.0.1:%d" % _LOOPBACK_PORT if _LOOPBACK_PORT else ""
    return os.path.join(data_home, "app-control.sock")


def peer_pid(connection):
    """从内核凭据读取客户端 PID；不能由请求内容覆盖。

    POSIX 读 Unix Socket 对端凭据。Windows 没有可用的 Unix Socket 凭据，改用专用回环
    端口，并从内核 TCP 连接表查出发起连接的进程。
    """
    if oscompat.IS_WINDOWS:
        try:
            if not _LOOPBACK_PORT or connection.getsockname()[1] != _LOOPBACK_PORT:
                return 0
            host, port = connection.getpeername()[:2]
            if host != "127.0.0.1":
                return 0
            return oscompat.loopback_peer_pid(port, _LOOPBACK_PORT)
        except (OSError, ValueError, TypeError):
            return 0
    if getattr(connection, "family", None) != socket.AF_UNIX:
        return 0
    try:
        if sys.platform == "darwin":
            return struct.unpack("i", connection.getsockopt(0, 2, 4))[0]
        if hasattr(socket, "SO_PEERCRED"):
            return struct.unpack(
                "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
    except (OSError, ValueError, struct.error):
        pass
    return 0


def issue(session_id):
    """返回该 Desk 会话稳定的随机能力。"""
    sid = str(session_id or "")
    if not sid:
        raise ValueError("session_id required")
    with _LOCK:
        token = _CAPABILITIES.get(sid)
        if not token:
            token = secrets.token_urlsafe(32)
            _CAPABILITIES[sid] = token
        return token


def register_process(session_id, proc):
    """把会话绑定到当前引擎进程实例；新实例会立即替换旧实例。"""
    sid = str(session_id or "")
    pid = int(getattr(proc, "pid", 0) or 0)
    if not sid or pid <= 0 or not callable(getattr(proc, "poll", None)):
        return
    with _LOCK:
        _ROOT_PROCESSES[sid] = proc
        _GENERATIONS[sid] = _GENERATIONS.get(sid, 0) + 1


def _parents():
    if oscompat.IS_WINDOWS:
        return {pid: ppid for pid, ppid, _name in oscompat.process_table()}
    try:
        output = subprocess.check_output(
            ["/bin/ps", "-axo", "pid=,ppid="], text=True,
            stderr=subprocess.DEVNULL, timeout=1)
        table = {}
        for line in output.splitlines():
            fields = line.split()
            if len(fields) == 2:
                table[int(fields[0])] = int(fields[1])
    except Exception:
        table = {}
    return table


def _descends_from(peer_pid, roots):
    pid, seen, parents = int(peer_pid or 0), set(), _parents()
    while pid > 0 and pid not in seen:
        if pid in roots:
            return True
        seen.add(pid)
        pid = parents.get(pid, 0)
    return False


def verify(session_id, capability, peer_pid=0):
    sid = str(session_id or "")
    actual = str(capability or "")
    if not sid or not actual:
        return False
    with _LOCK:
        expected = _CAPABILITIES.get(sid, "")
        proc = _ROOT_PROCESSES.get(sid)
        generation = _GENERATIONS.get(sid, 0)
    if not expected or not hmac.compare_digest(expected, actual) or proc is None:
        return False
    try:
        if proc.poll() is not None:
            return False
        root_pid = int(proc.pid)
    except Exception:
        return False
    if not _descends_from(peer_pid, {root_pid}):
        return False
    # ps 查询发生在锁外。期间会话可能撤销或换了新引擎进程；返回前必须确认刚才
    # 验证的仍是当前实例，否则旧进程会在竞态窗口里多通过一次。
    with _LOCK:
        if (_CAPABILITIES.get(sid, "") != expected
                or _ROOT_PROCESSES.get(sid) is not proc
                or _GENERATIONS.get(sid, 0) != generation):
            return False
        try:
            return proc.poll() is None
        except Exception:
            return False


def revoke(session_id):
    """删除会话时立即废止旧进程持有的能力。"""
    with _LOCK:
        sid = str(session_id or "")
        _CAPABILITIES.pop(sid, None)
        _ROOT_PROCESSES.pop(sid, None)
        _GENERATIONS[sid] = _GENERATIONS.get(sid, 0) + 1
