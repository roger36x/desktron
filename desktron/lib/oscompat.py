"""平台兼容层：让 POSIX 写法的后端在 Windows 上保持同样语义。

macOS / Linux 上本模块只提供薄包装，行为与直接调用 os / fcntl 相同；`install()` 不做任何事。

Windows 上 `install()` 在进程启动时补齐以下差异（只补缺失或语义危险的部分，不改变 POSIX 平台）：

- `fcntl.flock`：以 `LockFileEx` 实现，锁定文件末尾之外的固定字节，不妨碍正常读写；
- `os.open`：自动加 `O_BINARY`，避免 CRT 文本模式改写换行；`O_NOFOLLOW` 拒绝末段为符号链接或 junction 的路径；
- `os.replace` / `os.rename`：目标被其他句柄短暂占用时有限重试；
- `os.kill(pid, 0)`：Windows 上 0 是 CTRL_C_EVENT，这里改为只检查存活；
- `os.killpg` / `os.getpgid` / `os.getpgrp` / `os.getsid`：以 Job Object 或进程树代替进程组；
- 缺失常量 `O_NOFOLLOW`、`O_NONBLOCK`、`signal.SIGKILL` 与 `os.fchmod`、`os.getuid`。

Windows 的 `os.stat` 不提供 POSIX 权限位，属主恒为 0；调用方用 `loose_mode()` 判断权限位，
不要直接写 `st_mode & 0o077`。
"""
import os
import signal
import subprocess
import sys
import threading
import time

IS_WINDOWS = sys.platform == "win32"
SIGKILL = getattr(signal, "SIGKILL", 9)


def loose_mode(mode):
    """返回组与其他用户可访问的权限位。Windows 用 ACL 管理权限，用户目录默认私有，按 0 处理。"""
    return 0 if IS_WINDOWS else (int(mode) & 0o077)


def getuid():
    return 0 if IS_WINDOWS else os.getuid()


def fsync_directory(path):
    """Sync rename metadata on POSIX; Windows CRT cannot open directories.

    Windows callers still fsync the file before atomically replacing it. This
    does not claim POSIX directory-fsync/power-loss guarantees on Windows.
    File flush/replace errors and POSIX directory-sync errors remain fatal.
    """
    if IS_WINDOWS:
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------------------------
# Windows 原生接口
# --------------------------------------------------------------------------------------------
if IS_WINDOWS:
    import ctypes
    import ctypes.wintypes as wt
    import msvcrt

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _iphlp = None

    PROCESS_TERMINATE = 0x0001
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    STILL_ACTIVE = 259
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 0x102
    ERROR_ACCESS_DENIED = 5
    ERROR_INVALID_PARAMETER = 87
    ERROR_LOCK_VIOLATION = 33
    ERROR_IO_PENDING = 997
    TH32CS_SNAPPROCESS = 0x2
    LOCKFILE_FAIL_IMMEDIATELY = 0x1
    LOCKFILE_EXCLUSIVE_LOCK = 0x2
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0800
    JobObjectExtendedLimitInformation = 9
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                    ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD), ("hEvent", wt.HANDLE)]

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD),
                    ("SchedulingClass", wt.DWORD)]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", _IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    def _sig(name, res, *args):
        fn = getattr(_k32, name)
        fn.restype = res
        fn.argtypes = list(args)
        return fn

    _OpenProcess = _sig("OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
    _CloseHandle = _sig("CloseHandle", wt.BOOL, wt.HANDLE)
    _GetExitCodeProcess = _sig("GetExitCodeProcess", wt.BOOL, wt.HANDLE, ctypes.POINTER(wt.DWORD))
    _TerminateProcess = _sig("TerminateProcess", wt.BOOL, wt.HANDLE, wt.UINT)
    _GetProcessTimes = _sig("GetProcessTimes", wt.BOOL, wt.HANDLE, ctypes.POINTER(wt.FILETIME),
                            ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME),
                            ctypes.POINTER(wt.FILETIME))
    _WaitForSingleObject = _sig("WaitForSingleObject", wt.DWORD, wt.HANDLE, wt.DWORD)
    _CreateToolhelp32Snapshot = _sig("CreateToolhelp32Snapshot", wt.HANDLE, wt.DWORD, wt.DWORD)
    _Process32FirstW = _sig("Process32FirstW", wt.BOOL, wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
    _Process32NextW = _sig("Process32NextW", wt.BOOL, wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
    _LockFileEx = _sig("LockFileEx", wt.BOOL, wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD,
                       ctypes.POINTER(_OVERLAPPED))
    _UnlockFileEx = _sig("UnlockFileEx", wt.BOOL, wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD,
                         ctypes.POINTER(_OVERLAPPED))
    _CreateJobObjectW = _sig("CreateJobObjectW", wt.HANDLE, ctypes.c_void_p, wt.LPCWSTR)
    _SetInformationJobObject = _sig("SetInformationJobObject", wt.BOOL, wt.HANDLE, ctypes.c_int,
                                    ctypes.c_void_p, wt.DWORD)
    _AssignProcessToJobObject = _sig("AssignProcessToJobObject", wt.BOOL, wt.HANDLE, wt.HANDLE)
    _TerminateJobObject = _sig("TerminateJobObject", wt.BOOL, wt.HANDLE, wt.UINT)
    _OpenEventW = _sig("OpenEventW", wt.HANDLE, wt.DWORD, wt.BOOL, wt.LPCWSTR)
    _SetThreadExecutionState = _sig("SetThreadExecutionState", wt.DWORD, wt.DWORD)
    _GetCurrentProcess = _sig("GetCurrentProcess", wt.HANDLE)

    def _open(pid, access):
        if not isinstance(pid, int) or pid <= 0:
            return None
        handle = _OpenProcess(access, False, pid)
        return handle or None

    def _exit_code(handle):
        code = wt.DWORD()
        if not _GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value


def process_alive(pid):
    """进程是否仍在运行。不会向目标发送任何信号。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
    handle = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if handle is None:
        # 拒绝访问说明进程存在但属于其他会话或更高权限。
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        return _exit_code(handle) == STILL_ACTIVE
    finally:
        _CloseHandle(handle)


def process_start_token(pid):
    """返回进程启动身份；PID 被复用时令牌不同。取不到时返回空串。"""
    if not IS_WINDOWS:
        return ""
    handle = _open(int(pid), PROCESS_QUERY_LIMITED_INFORMATION)
    if handle is None:
        return ""
    try:
        if _exit_code(handle) != STILL_ACTIVE:
            return ""
        created, exited, kernel, user = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
        if not _GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                ctypes.byref(kernel), ctypes.byref(user)):
            return ""
        return "win-ct:%d" % ((created.dwHighDateTime << 32) | created.dwLowDateTime)
    finally:
        _CloseHandle(handle)


def process_table():
    """返回 [(pid, ppid, 可执行文件名)]。POSIX 用 ps，Windows 用 Toolhelp 快照。"""
    rows = []
    if not IS_WINDOWS:
        try:
            out = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="], capture_output=True,
                                 text=True, timeout=2, check=False).stdout
        except Exception:
            return rows
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                rows.append((int(parts[0]), int(parts[1]), parts[2] if len(parts) > 2 else ""))
        return rows
    snap = _CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return rows
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            rows.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile))
            ok = _Process32NextW(snap, ctypes.byref(entry))
    finally:
        _CloseHandle(snap)
    return rows


def descendants(root_pid, table=None):
    """按父子关系列出 root 的后代 PID（不含 root）。

    Windows 的 PPID 不随父进程退出而更新，PID 也会被复用；只有启动时间不早于父进程的
    子项才算后代，避免把复用了旧 PID 的无关进程算进来。
    """
    rows = process_table() if table is None else table
    children = {}
    for pid, ppid, _name in rows:
        if pid != ppid:
            children.setdefault(ppid, []).append(pid)
    started = {}

    def start(pid):
        if pid not in started:
            token = process_start_token(pid) if IS_WINDOWS else ""
            started[pid] = int(token.split(":", 1)[1]) if token else 0
        return started[pid]

    found, pending = [], [int(root_pid)]
    seen = {int(root_pid)}
    while pending:
        parent = pending.pop()
        for child in children.get(parent, ()):
            if child in seen:
                continue
            if IS_WINDOWS and start(parent) and start(child) and start(child) < start(parent):
                continue
            seen.add(child)
            found.append(child)
            pending.append(child)
    return found


def terminate_pid(pid, code=1):
    """立即结束单个进程。POSIX 发送 SIGKILL。"""
    if not IS_WINDOWS:
        os.kill(int(pid), SIGKILL)
        return
    handle = _open(int(pid), PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION)
    if handle is None:
        if ctypes.get_last_error() == ERROR_ACCESS_DENIED:
            raise PermissionError("无权结束进程 %s" % pid)
        raise ProcessLookupError("进程 %s 不存在" % pid)
    try:
        if _exit_code(handle) != STILL_ACTIVE:
            raise ProcessLookupError("进程 %s 已退出" % pid)
        if not _TerminateProcess(handle, int(code) & 0xFFFFFFFF):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _CloseHandle(handle)


# --------------------------------------------------------------------------------------------
# Job Object：Windows 上代替“新会话 + 进程组”
# --------------------------------------------------------------------------------------------
_JOBS = {}
_JOBS_LOCK = threading.Lock()


def _new_job(kill_on_close=True):
    job = _CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    flags = JOB_OBJECT_LIMIT_BREAKAWAY_OK
    if kill_on_close:
        flags |= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    info.BasicLimitInformation.LimitFlags = flags
    if not _SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                    ctypes.byref(info), ctypes.sizeof(info)):
        error = ctypes.get_last_error()
        _CloseHandle(job)
        raise ctypes.WinError(error)
    return job


def adopt(proc):
    """把以 start_new_session=True 启动的子进程放进独立 Job，供 killpg 整组结束。

    POSIX 上 start_new_session 已经建立进程组，这里不做事。Windows 上 Job 设置
    KILL_ON_JOB_CLOSE：Desk 后端退出（包括被强杀）时，作业内的全部进程随之结束。
    组长退出满 60 秒后，作业句柄在下一次登记时释放，残留的后代随之结束。
    返回 proc，便于链式调用。
    """
    if not IS_WINDOWS:
        return proc
    pid = getattr(proc, "pid", None)
    handle = getattr(proc, "_handle", None)
    if not isinstance(pid, int) or pid <= 0 or handle is None:
        return proc
    try:
        job = _new_job()
    except OSError:
        return proc
    if not _AssignProcessToJobObject(job, int(handle)):
        _CloseHandle(job)
        return proc
    import weakref
    stale = []
    with _JOBS_LOCK:
        old = _JOBS.pop(pid, None)
        if old:
            stale.append(old[0])
        _JOBS[pid] = [job, weakref.ref(proc), 0.0]
        if len(_JOBS) > 32:
            now = time.monotonic()
            for key, entry in list(_JOBS.items()):
                leader = entry[1]()
                exited = leader is None or leader.poll() is not None
                if not exited:
                    continue
                if not entry[2]:
                    entry[2] = now
                elif now - entry[2] >= 60:
                    stale.append(entry[0])
                    del _JOBS[key]
    for item in stale:
        _CloseHandle(item)
    return proc


def _job_for(pid):
    with _JOBS_LOCK:
        entry = _JOBS.get(int(pid))
        return entry[0] if entry else None


class Job:
    """Windows Job Object 的最小封装；关闭句柄即结束作业内全部进程。"""

    def __init__(self, kill_on_close=True):
        self.handle = _new_job(kill_on_close)

    def assign(self, proc):
        handle = getattr(proc, "_handle", None)
        if handle is None or not _AssignProcessToJobObject(self.handle, int(handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self, code=1):
        if self.handle:
            _TerminateJobObject(self.handle, int(code))

    def close(self):
        if self.handle:
            _CloseHandle(self.handle)
            self.handle = None


def release(pid):
    """进程确认退出后释放 Job 句柄。"""
    if not IS_WINDOWS:
        return
    with _JOBS_LOCK:
        entry = _JOBS.pop(int(pid), None)
    if entry:
        _CloseHandle(entry[0])


def session_kwargs():
    """新进程组的 Popen 参数。POSIX 为 start_new_session；Windows 为新进程组且不弹控制台。"""
    if not IS_WINDOWS:
        return {"start_new_session": True}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}


def no_window_kwargs():
    """后台子进程不弹出控制台窗口。POSIX 为空。"""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}


def getpgid(pid):
    if not IS_WINDOWS:
        return os.getpgid(pid)
    if pid == 0:
        return os.getpid()
    if not process_alive(pid):
        raise ProcessLookupError("进程 %s 不存在" % pid)
    return int(pid)


def getpgrp():
    return os.getpid() if IS_WINDOWS else os.getpgrp()


def killpg(pgid, sig):
    """结束整组进程。Windows 上优先结束 adopt 登记的 Job，否则按进程树结束。"""
    if not IS_WINDOWS:
        os.killpg(pgid, sig)
        return
    pgid = int(pgid)
    if sig == 0:
        if not process_alive(pgid):
            raise ProcessLookupError("进程组 %s 不存在" % pgid)
        return
    job = _job_for(pgid)
    if job:
        if _TerminateJobObject(job, 1):
            return
    tree = descendants(pgid)
    alive_root = process_alive(pgid)
    if not alive_root and not tree:
        raise ProcessLookupError("进程组 %s 不存在" % pgid)
    for pid in reversed(tree):
        try:
            terminate_pid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if alive_root:
        try:
            terminate_pid(pgid)
        except ProcessLookupError:
            pass


def kill(pid, sig):
    """os.kill 的跨平台版本；sig 为 0 时只检查存活。"""
    if not IS_WINDOWS:
        os.kill(pid, sig)
        return
    if sig == 0:
        if not process_alive(pid):
            raise ProcessLookupError("进程 %s 不存在" % pid)
        return
    terminate_pid(pid, code=sig if isinstance(sig, int) and sig > 0 else 1)


def request_self_shutdown():
    """让本进程走与收到 SIGTERM 相同的主线程收尾路径。"""
    if IS_WINDOWS:
        signal.raise_signal(signal.SIGTERM)
    else:
        os.kill(os.getpid(), signal.SIGTERM)


# --------------------------------------------------------------------------------------------
# 文件锁
# --------------------------------------------------------------------------------------------
# Windows 字节范围锁是强制锁；锁在 2^63 附近的一个字节上，不影响文件内容的读写。
_LOCK_OFFSET_LOW = 0
_LOCK_OFFSET_HIGH = 0x7FFFFFFF


def lock_file(fd, exclusive=True, blocking=True):
    """对 fd 加锁；非阻塞且已被占用时抛 BlockingIOError，与 fcntl.flock 一致。"""
    if not IS_WINDOWS:
        import fcntl
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            flags |= fcntl.LOCK_NB
        fcntl.flock(fd, flags)
        return
    handle = msvcrt.get_osfhandle(fd)
    flags = (LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0)
    if not blocking:
        flags |= LOCKFILE_FAIL_IMMEDIATELY
    overlapped = _OVERLAPPED()
    overlapped.Offset = _LOCK_OFFSET_LOW
    overlapped.OffsetHigh = _LOCK_OFFSET_HIGH
    if _LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
        return
    error = ctypes.get_last_error()
    if error in (ERROR_LOCK_VIOLATION, ERROR_IO_PENDING):
        raise BlockingIOError(11, "文件已被其他进程锁定")
    raise ctypes.WinError(error)


def unlock_file(fd):
    if not IS_WINDOWS:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    handle = msvcrt.get_osfhandle(fd)
    overlapped = _OVERLAPPED()
    overlapped.Offset = _LOCK_OFFSET_LOW
    overlapped.OffsetHigh = _LOCK_OFFSET_HIGH
    _UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))


class _FcntlShim:
    """Windows 上的 fcntl 替身，只实现本仓库用到的 flock。"""
    LOCK_SH = 1
    LOCK_EX = 2
    LOCK_NB = 4
    LOCK_UN = 8
    __name__ = "fcntl"

    @staticmethod
    def flock(fd, operation):
        if hasattr(fd, "fileno"):
            fd = fd.fileno()
        if operation & _FcntlShim.LOCK_UN:
            unlock_file(fd)
            return
        lock_file(fd, exclusive=bool(operation & _FcntlShim.LOCK_EX),
                  blocking=not (operation & _FcntlShim.LOCK_NB))

    @staticmethod
    def ioctl(*_args, **_kwargs):
        raise OSError("Windows 不支持 ioctl")

    @staticmethod
    def fcntl(*_args, **_kwargs):
        return 0


# --------------------------------------------------------------------------------------------
# 网络：端口占用者与回环对端 PID
# --------------------------------------------------------------------------------------------
def _tcp_rows():
    """Windows：返回 [(本地地址, 本地端口, 远端地址, 远端端口, 状态, pid)]，仅 IPv4。"""
    global _iphlp
    if _iphlp is None:
        _iphlp = ctypes.WinDLL("iphlpapi")
        _iphlp.GetExtendedTcpTable.restype = wt.DWORD
        _iphlp.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD), wt.BOOL,
                                               wt.ULONG, ctypes.c_int, wt.ULONG]
    TCP_TABLE_OWNER_PID_ALL = 5
    AF_INET = 2
    size = wt.DWORD(0)
    _iphlp.GetExtendedTcpTable(None, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
    for _ in range(4):
        buf = ctypes.create_string_buffer(size.value + 4096)
        size = wt.DWORD(len(buf))
        rc = _iphlp.GetExtendedTcpTable(buf, ctypes.byref(size), False, AF_INET,
                                        TCP_TABLE_OWNER_PID_ALL, 0)
        if rc == 0:
            break
    else:
        return []
    count = ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0]
    rows = []
    stride = 6 * 4
    base = ctypes.addressof(buf) + 4
    for index in range(count):
        state, laddr, lport, raddr, rport, pid = (ctypes.c_uint32 * 6).from_address(base + index * stride)
        rows.append((laddr, _port(lport), raddr, _port(rport), state, pid))
    return rows


def _port(raw):
    return ((raw & 0xFF) << 8) | ((raw >> 8) & 0xFF)


def listening_pid(port):
    """返回在 127.0.0.1/0.0.0.0 上监听 port 的进程 PID；没有时返回 None。"""
    if not IS_WINDOWS:
        try:
            out = subprocess.run(["lsof", "-nP", "-t", "-iTCP:%d" % int(port), "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=3).stdout
        except Exception:
            return None
        for token in out.split():
            if token.isdigit():
                return int(token)
        return None
    MIB_TCP_STATE_LISTEN = 2
    for _laddr, lport, _raddr, _rport, state, pid in _tcp_rows():
        if lport == int(port) and state == MIB_TCP_STATE_LISTEN:
            return int(pid)
    return None


def loopback_peer_pid(peer_port, server_port):
    """Windows：回环 TCP 连接中，本地端口为 peer_port、远端端口为 server_port 的连接属于哪个进程。

    结果来自内核连接表，客户端无法伪造。用于代替 Unix Socket 的对端凭据。
    """
    if not IS_WINDOWS:
        return 0
    MIB_TCP_STATE_ESTAB = 5
    for _laddr, lport, _raddr, rport, state, pid in _tcp_rows():
        if lport == int(peer_port) and rport == int(server_port) and state == MIB_TCP_STATE_ESTAB:
            return int(pid)
    return 0


# --------------------------------------------------------------------------------------------
# 父进程看护与停止事件
# --------------------------------------------------------------------------------------------
def watch_parent(parent_pid, on_exit, interval=1.0):
    """父进程退出时调用 on_exit。Windows 的 getppid 不随父进程退出变化，必须用进程句柄等待。"""
    def posix_watch():
        while True:
            time.sleep(interval)
            if os.getppid() != parent_pid:
                on_exit()
                return

    def windows_watch():
        handle = _open(int(parent_pid), SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION)
        if handle is None:
            on_exit()
            return
        try:
            _WaitForSingleObject(handle, 0xFFFFFFFF)
        finally:
            _CloseHandle(handle)
        on_exit()

    thread = threading.Thread(target=windows_watch if IS_WINDOWS else posix_watch, daemon=True)
    thread.start()
    return thread


def watch_stop_event(name, on_set):
    """Windows：等待宿主创建的命名事件；宿主以 SetEvent 请求后端正常退出。"""
    if not IS_WINDOWS or not name:
        return None

    def run():
        handle = _OpenEventW(SYNCHRONIZE, False, name)
        if not handle:
            return
        try:
            _WaitForSingleObject(handle, 0xFFFFFFFF)
        finally:
            _CloseHandle(handle)
        on_set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def inhibit_sleep():
    """Windows：阻止系统因空闲进入睡眠，直到进程退出。返回是否生效。"""
    if not IS_WINDOWS:
        return False
    return bool(_SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))


# --------------------------------------------------------------------------------------------
# 桌面集成：打开网址、浏览器路径、系统终端
# --------------------------------------------------------------------------------------------
def open_url(url):
    """用系统默认浏览器打开 http(s) 网址。"""
    if IS_WINDOWS:
        os.startfile(url)  # noqa: S606 — 只接收调用方已校验的 http(s) 网址
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, url])


def chrome_candidates():
    """Chromium 系浏览器可执行文件候选(用于 Desk 专属 profile 开页)。"""
    if IS_WINDOWS:
        roots = [os.environ.get(k, "") for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        out = []
        for root in filter(None, roots):
            out.append(os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"))
        for root in filter(None, roots[:2]):
            out.append(os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"))
        return out
    return ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium", "/usr/bin/chromium-browser"]


def powershell():
    """优先 PowerShell 7(pwsh)，否则系统自带 Windows PowerShell。"""
    import shutil
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"


def interactive_shell_argv():
    """内嵌终端的交互 shell。POSIX 为登录 shell；Windows 为 PowerShell。"""
    if IS_WINDOWS:
        return [powershell(), "-NoLogo"]
    return [os.environ.get("SHELL") or "/bin/bash", "-l"]


def command_shell_argv(command):
    """执行一条已确认的固定命令。POSIX 用 bash -c；Windows 用 PowerShell -Command。"""
    if IS_WINDOWS:
        return [powershell(), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-Command", command]
    return ["/bin/bash", "--noprofile", "--norc", "-c", command]


def login_shell_argv(command):
    """以登录 shell 执行命令(带用户 PATH)。POSIX 为 bash -lc；Windows 为 PowerShell。"""
    if IS_WINDOWS:
        return command_shell_argv(command)
    return ["bash", "-lc", command]


def run_with_success_marker(argv, marker_env, marker_path, **kwargs):
    """运行 argv；仅在命令成功退出时创建 marker_path。

    POSIX 沿用 `sh -c '"$@" && : > "$MARKER"'`：只有命令本身成功，标记才会出现。Windows 没有
    /bin/sh，直接运行并按退出码写标记。kwargs 原样交给 subprocess.run，返回其结果。
    """
    env = dict(kwargs.pop("env", None) or os.environ)
    env[marker_env] = marker_path
    if not IS_WINDOWS:
        wrapper = '"$@" && : > "$%s"' % marker_env
        return subprocess.run(["/bin/sh", "-c", wrapper, "sh"] + list(argv), env=env, **kwargs)
    result = subprocess.run(list(argv), env=env, **kwargs)
    if result.returncode == 0:
        with open(marker_path, "w", encoding="utf-8"):
            pass
    return result


def open_system_terminal(command):
    """在系统终端窗口中执行命令(用于引擎 OAuth 登录)。"""
    if IS_WINDOWS:
        subprocess.Popen([powershell(), "-NoExit", "-Command", command],
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
        return
    if sys.platform == "darwin":
        subprocess.Popen(["osascript", "-e", 'tell application "Terminal" to activate',
                          "-e", f'tell application "Terminal" to do script "{command}"'])
        return
    raise OSError("当前系统没有可用的终端启动方式")


def process_started_at(pid):
    """进程启动时间(Unix 秒)；取不到返回 None。"""
    if IS_WINDOWS:
        token = process_start_token(pid)
        if not token:
            return None
        return int(token.split(":", 1)[1]) / 1e7 - 11644473600
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))],
                             capture_output=True, timeout=10)
        return time.mktime(time.strptime(out.stdout.decode("utf-8", "replace").strip()))
    except Exception:
        return None


# --------------------------------------------------------------------------------------------
# 链接：Windows 普通用户无法创建符号链接时，目录退回 junction
# --------------------------------------------------------------------------------------------
def _create_junction(target, link):
    from windows_fs import create_junction
    return create_junction(target, link)


def link_path(target, link, target_is_directory=None):
    if not IS_WINDOWS:
        os.symlink(target, link)
        return "symlink"
    if target_is_directory is None:
        target_is_directory = os.path.isdir(target)
    try:
        os.symlink(target, link, target_is_directory=target_is_directory)
        return "symlink"
    except OSError:
        if not target_is_directory:
            raise
    _create_junction(target, link)
    return "junction"


def read_link(link):
    """读取符号链接或 junction 的目标；不是链接时返回 None。"""
    try:
        if os.path.islink(link) or (IS_WINDOWS and _is_junction(link)):
            return os.readlink(link)
    except OSError:
        return None
    return None


# Windows CRT has no O_NOFOLLOW. A bit the CRT never uses carries the request
# into os.open, which strips it and enforces the final-component check itself.
WINDOWS_O_NOFOLLOW = 0x10000000
_NAME_SURROGATE = 0x20000000   # symlinks, junctions and other name-surrogate reparse points


def _is_name_surrogate(info):
    return bool(getattr(info, "st_reparse_tag", 0) & _NAME_SURROGATE)


def _nofollow_error(path):
    import errno
    code = getattr(errno, "ELOOP", errno.EACCES)
    return OSError(code, "refusing to follow a link (O_NOFOLLOW)", path)


def _open_nofollow(original_open, path, flags, mode, fstat=None):
    """POSIX O_NOFOLLOW for the final path component on Windows.

    Reject existing links, open without truncation, then validate the handle.
    Missing-file creation is exclusive so a link swapped in cannot create its
    target; an existing-file open cannot create anything. Truncation occurs only
    through the validated descriptor, never before the final-component check.
    `fstat` is the unpatched os.fstat captured at install, so callers that
    substitute os.fstat (tests simulating ownership or growth) see their own
    calls unchanged.
    """
    fstat = fstat or os.fstat
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        before = None
    if before is not None and _is_name_surrogate(before):
        raise _nofollow_error(path)
    open_flags = flags & ~os.O_TRUNC
    if flags & os.O_CREAT:
        if before is None:
            open_flags |= os.O_EXCL
        elif not flags & os.O_EXCL:
            open_flags &= ~os.O_CREAT
    fd = original_open(path, open_flags, mode)
    try:
        after = os.lstat(path)
        opened = fstat(fd)
        if (_is_name_surrogate(after)
                or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
                or (before is not None and (before.st_dev, before.st_ino)
                    != (opened.st_dev, opened.st_ino))):
            raise _nofollow_error(path)
        if flags & os.O_TRUNC:
            os.ftruncate(fd, 0)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _is_junction(path):
    checker = getattr(os.path, "isjunction", None)
    if checker is not None:
        return checker(path)
    try:
        import stat as _stat
        st = os.lstat(path)
        return getattr(st, "st_reparse_tag", 0) == _stat.IO_REPARSE_TAG_MOUNT_POINT
    except (OSError, AttributeError):
        return False


def is_link(path):
    return os.path.islink(path) or (IS_WINDOWS and _is_junction(path))


def remove_link(path):
    """删除符号链接或 junction，不触碰目标内容。"""
    if IS_WINDOWS and _is_junction(path):
        os.rmdir(path)
    else:
        os.unlink(path)


# --------------------------------------------------------------------------------------------
# 命令解析：POSIX execvp 按 PATH 查找裸命令；Windows 的 CreateProcess 只补 .exe，
# 找不到 npm 安装的 .cmd 入口(codex.cmd、pi.cmd、npm.cmd)。这里按 PATHEXT 预先解析。
# --------------------------------------------------------------------------------------------
def _resolve_command(args, kwargs):
    if kwargs.get("shell") or kwargs.get("executable"):
        return args
    argv = args[0] if args else kwargs.get("args")
    if not isinstance(argv, (list, tuple)) or not argv or not isinstance(argv[0], str):
        return args
    head = argv[0]
    env = kwargs.get("env")
    search = (env or {}).get("PATH") or (env or {}).get("Path") if env is not None else None
    if head and head.lower().endswith(".cmd") and os.path.isfile(head):
        # 已解析出的 npm 入口(例如 shutil.which("codex") 的结果)同样改为直接调用 node。
        shim = npm_shim_argv(head, search)
        if not shim:
            return args
        resolved = [*shim, *argv[1:]]
    else:
        if not head or os.path.dirname(head) or os.path.splitext(head)[1]:
            return args
        import cli_path
        found = cli_path.which(head, path=search) if search else cli_path.which(head)
        if not found:
            return args
        resolved = [*(npm_shim_argv(found, search) or [found]), *argv[1:]]
    if args:
        return (resolved, *args[1:])
    kwargs["args"] = resolved
    return args


def npm_shim_argv(path, search=None):
    """npm 在 Windows 上生成的 .cmd 入口只是 `node <包内脚本> %*` 的包装。

    同时支持官方 Pi 安装器的 `%~dp0pi-launcher.js` 入口。
    直接返回 [node, 脚本]，不经 cmd.exe：参数不再经过批处理的二次解析(引号与 % 等字符会被改写)，
    结束进程时也不会留下脱离的 node 子进程。不是 npm 入口时返回 None。
    """
    if not str(path).lower().endswith(".cmd"):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            text = stream.read(8192)
    except OSError:
        return None
    import re
    match = re.search(r'"(?:%~dp0|%dp0%)[\\/]*([^"\r\n]+?\.(?:js|cjs|mjs))"', text, re.IGNORECASE)
    if not match:
        return None
    folder = os.path.dirname(os.path.abspath(path))
    script = os.path.join(folder, *re.split(r"[\\\\/]", match.group(1)))
    if not os.path.isfile(script):
        return None
    node = os.path.join(folder, "node.exe")
    if not os.path.isfile(node):
        import cli_path
        node = cli_path.which("node", path=search) if search else cli_path.which("node")
    if not node or not node.lower().endswith(".exe"):
        return None
    return [node, script]


# --------------------------------------------------------------------------------------------
# 安装
# --------------------------------------------------------------------------------------------
_INSTALLED = False


def install():
    """Windows 上补齐 POSIX 语义；其他平台不做任何事。可重复调用。"""
    global _INSTALLED
    if not IS_WINDOWS or _INSTALLED:
        return
    _INSTALLED = True
    sys.modules.setdefault("fcntl", _FcntlShim)
    if not hasattr(os, "O_NOFOLLOW"):
        os.O_NOFOLLOW = WINDOWS_O_NOFOLLOW
    for name in ("O_NONBLOCK", "O_DIRECTORY", "O_CLOEXEC"):
        if not hasattr(os, name):
            setattr(os, name, 0)
    if not hasattr(signal, "SIGKILL"):
        signal.SIGKILL = SIGKILL

    original_open = os.open
    original_fstat = os.fstat

    def open_binary(path, flags, mode=0o777, *, dir_fd=None):
        # Long-path conversion is shared by all filesystem calls in windows_fs.
        nofollow = flags & WINDOWS_O_NOFOLLOW
        flags = (flags & ~WINDOWS_O_NOFOLLOW) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
        if dir_fd is not None:
            return original_open(path, flags, mode, dir_fd=dir_fd)
        if nofollow:
            return _open_nofollow(original_open, path, flags, mode, original_fstat)
        return original_open(path, flags, mode)

    os.open = open_binary

    def retrying(fn):
        def wrapper(src, dst, *args, **kwargs):
            deadline = None
            delay = 0.01
            while True:
                try:
                    return fn(src, dst, *args, **kwargs)
                except PermissionError:
                    # Only consult the retry clock after contention; successful
                    # operations must not consume callers' mocked/lifecycle clocks.
                    now = time.monotonic()
                    if deadline is None:
                        deadline = now + 2.0
                    if now >= deadline:
                        raise
                    time.sleep(delay)
                    delay = min(delay * 2, 0.2)
        wrapper.__wrapped__ = fn
        return wrapper

    os.replace = retrying(os.replace)
    os.rename = retrying(os.rename)

    original_kill = os.kill

    def safe_kill(pid, sig):
        if sig == 0:
            if not process_alive(pid):
                raise ProcessLookupError("进程 %s 不存在" % pid)
            return
        if sig in (getattr(signal, "CTRL_C_EVENT", -1), getattr(signal, "CTRL_BREAK_EVENT", -1)):
            return original_kill(pid, sig)
        terminate_pid(pid, code=sig if isinstance(sig, int) and sig > 0 else 1)

    os.kill = safe_kill
    if not hasattr(os, "fchmod"):
        os.fchmod = lambda fd, mode: None
    if not hasattr(os, "getuid"):
        os.getuid = getuid
    os.getpgid = getpgid
    os.getpgrp = getpgrp
    if not hasattr(os, "getsid"):
        os.getsid = getpgid
    os.killpg = killpg

    # 符号链接：普通用户在未开启“开发者模式”时没有创建权限。退路保持“链接”语义：
    # 目录用 junction(无需特权)，文件用硬链接(同卷)或复制；islink/unlink 同样认 junction，
    # 让“已是链接则比对目标、替换时先删链接”的既有逻辑在 Windows 上照常成立。
    original_symlink = os.symlink
    global native_symlink
    native_symlink = original_symlink
    original_islink = os.path.islink
    original_unlink = os.unlink

    def symlink_with_fallback(src, dst, target_is_directory=False, *, dir_fd=None):
        try:
            return original_symlink(src, dst, target_is_directory, dir_fd=dir_fd)
        except OSError as error:
            if getattr(error, "winerror", None) != 1314 or dir_fd is not None:   # ERROR_PRIVILEGE_NOT_HELD
                raise
        target = src if os.path.isabs(src) else os.path.join(os.path.dirname(os.path.abspath(dst)), src)
        # Long destinations may carry an extended prefix. Resolve the relative
        # target's dot components before a junction serializes its absolute path.
        target = os.path.normpath(target)
        if target_is_directory or os.path.isdir(target):
            _create_junction(target, dst)
            return None
        try:
            os.link(target, dst)
        except OSError:
            import shutil
            shutil.copy2(target, dst)
        return None

    def islink_or_junction(path):
        return original_islink(path) or _is_junction(path)

    def unlink_link(path, *, dir_fd=None):
        if dir_fd is None and _is_junction(path):
            return os.rmdir(path)
        return original_unlink(path, dir_fd=dir_fd)

    os.symlink = symlink_with_fallback
    os.path.islink = islink_or_junction
    # Path.is_symlink uses lstat directly, bypassing os.path.islink. Directory
    # junctions must receive the same rejection as symlinks at safety boundaries.
    from pathlib import Path
    Path.is_symlink = lambda self: islink_or_junction(self)
    os.unlink = unlink_link
    os.remove = unlink_link

    # start_new_session 在 Windows 上被 subprocess 忽略。这里把它解释为“自成一组”：
    # 子进程进入独立 Job，killpg(pid) 结束整个作业；Desk 后端退出时作业随之结束。
    original_init = subprocess.Popen.__init__

    def popen_init(self, *args, **kwargs):
        args = _resolve_command(args, kwargs)
        new_session = bool(kwargs.get("start_new_session"))
        if new_session:
            kwargs["creationflags"] = int(kwargs.get("creationflags") or 0) | \
                subprocess.CREATE_NEW_PROCESS_GROUP
        original_init(self, *args, **kwargs)
        if new_session:
            adopt(self)

    subprocess.Popen.__init__ = popen_init

    # Install after the existing policies so long paths retain binary opens,
    # rename retries and junction handling. No registry or global OS change.
    from windows_fs import install as install_filesystem_paths
    install_filesystem_paths()


install()
