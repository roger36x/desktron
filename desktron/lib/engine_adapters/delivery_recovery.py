"""意外退出后的回执自动收口。

结果未知(uncertain)只说明上一轮没有收到实时终态，并不意味着需要用户决定任何事：
系统本来就不重发上一轮，用户点「继续」也不提供新信息。真正需要封锁新回合的风险只有一个——
旧的原生写入进程可能还活着，会与新回合同时写同一份记录。所以：

1. 旧写入进程的锁能非阻塞取得，即证明它已退出(owned_process 把锁 fd 交给原生进程，
   原生进程或其监督进程任一存活，锁都取不到)。
2. 退出已证明后，按引擎原生记录判断上一轮是否正常结束，结案为 completed 或 interrupted；
   没有原生证据时记 interrupted。结案只写回执，不重发请求、不改写历史。
3. 写入进程仍存活或无法判定时保持封锁，并在后台退避重探；只有长时间无法证明才留给用户。
"""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import fcntl
import os

# 后台重探的间隔(秒)。累计约 10 分钟，覆盖进程组慢速退出与系统高负载。
RETRY_DELAYS = (0.5, 1, 2, 4, 8, 15, 30, 60, 120, 180, 180)


def writer_released(path):
    """True=无人持有写入锁(含锁文件不存在)；False=仍被持有或无法判定。"""
    if not path:
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError):
            return False
        except OSError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)
