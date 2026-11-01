# 由 engine_adapters.py 分拆(2026-08-27,纯搬移;见 specs/2026-08-27-repo-partition-2.md)
import atexit
import collections
import os
import queue
import threading
import time


class _PersistUnusable(Exception):
    """常驻进程这一回合没能起作用,且一个字都还没吐出来——可以安全地退回一回合一进程那条路。"""


class _EngineProc:
    """一条会话的常驻引擎进程。

    stdin 收 stream-json 用户帧,stdout 一回合吐到 result 帧为止,进程不退,等下一帧。
    省掉的是每回合重启进程 + 按记录重放整条会话的那一段:实测 67 万 token 的会话里
    这段要 19~38 秒,用户看到的就是发完消息之后长时间没有动静。"""

    def __init__(self, proc, key, owner=None):
        self.proc = proc
        self.key = key                              # 选项指纹,变了就得重开
        self.owner = owner                          # 终局 recap 必须回到创建此进程的适配器
        self.at = time.time()                       # 上次用到的时刻,闲置回收看它
        self.busy = False                           # 管道上正有一轮在产出(用户轮或自续轮)
        self.err = collections.deque(maxlen=40)     # stderr 尾部
        # stdout 不由回合直接读,而是由一条常驻的泵线程逐行灌进这个队列(EOF 放一个 None)。
        # 回合之间引擎并不闲着:子代理交回时它自己续一轮,帧就写在这段间隙里。此前间隙里
        # 没人读 stdout,这些帧一直躺到用户下一次发话才被读循环消费——界面上是「刷新才出来」,
        # 更糟的是它们与用户那一轮的帧连成一串,归属判错就是正文重复、回复错位
        # (2026-09-08 用户实见)。泵线程一直读,谁消费由 reader_lock 决定:
        # 回合读与空闲读同一时刻只有一个拿着它。
        self.lines = queue.Queue()
        self.pump_thread = None
        self.reader_lock = threading.Lock()
        self.wanted = False                         # 有用户回合在等着接管管道
        self.idle_thread = None                     # 间隙里读管道的那条线程

    def pump(self):
        """把 stdout 一行一行搬进队列。只用 readline,不用 for-in:后者预读一大块,
        进程被杀时读到一半的行会丢。"""
        try:
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    break
                self.lines.put(line)
        except Exception:
            pass
        self.lines.put(None)

    def pump_start(self):
        """泵线程幂等启动。测试里直接构造 _EngineProc 塞进池子的那几条路也会走到这里。"""
        if self.pump_thread is None:
            self.pump_thread = threading.Thread(target=self.pump, daemon=True)
            self.pump_thread.start()

    def drain_err(self):
        """stderr 得一直抽干。老写法整个生命期只在收尾读一次,常驻下管道会写满,
        子进程卡在写 stderr 上,表现成整条会话不动。"""
        try:
            for ln in self.proc.stderr:
                self.err.append(ln.rstrip("\n"))
        except Exception:
            pass

    def tail_err(self):
        return "\n".join(self.err)[-300:]


_PERSIST = {}                    # sid -> _EngineProc
_PERSIST_LOCK = threading.Lock()
# 建过的适配器实例。_persist_drop 是模块级的,拿不到实例,而常驻进程一没,挂在它下面的
# 后台命令台账就该跟着清——按 sid 遍历这几个实例即可(一个引擎一个,统共四个)。
_ADAPTERS = __import__("weakref").WeakSet()
_PERSIST_IDLE = 1800             # 闲置多久回收(秒)
# 同时留几个常驻进程。每个都握着一份完整上下文,开十几条会话就是十几份,
# 内存与订阅额度都吃不消。超了先回收最久没用过的那个——它下次再问时重开,
# 代价只是一次冷启动,而不封顶的代价是整机变慢。
_PERSIST_MAX = max(1, int(os.environ.get("DESKTRON_PERSIST_MAX", "6") or 6))


def _persist_drop(sid):
    """摘掉并杀死一条会话的常驻进程。回收、中止、选项变更都走这里。"""
    with _PERSIST_LOCK:
        ep = _PERSIST.pop(sid, None)
    if ep is None:
        return
    try:
        ep.proc.kill()
    except Exception:
        pass
    _bg_forget(sid)


def _bg_forget(sid):
    """常驻进程没了,挂在它下面的后台命令也就没了:台账一并清掉。

    后台命令是那个引擎进程的子进程。进程被回收、被中止、或因选项变更换了实例之后,它既不会
    再跑,也不会再有 task_notification 回来销号——不清的话输入框上那句「N 个命令运行中」
    就永远挂着(2026-08-21 用户实见:后端台账已空,界面上还留着一个 1)。
    """
    for ad in list(_ADAPTERS):
        try:
            ad._bg_clear(sid)
        except Exception:
            pass


def _persist_evict(sid, expected=None):
    """仅用于资源回收：recap 同步完成后才摘掉常驻进程。"""
    with _PERSIST_LOCK:
        current = _PERSIST.get(sid)
        if current is None or current.busy or (expected is not None and current is not expected):
            return False
    try:
        from .recap import recap_before_evict
        if not recap_before_evict(sid, getattr(current, 'owner', None)):
            return False
    except Exception:
        return False
    with _PERSIST_LOCK:
        current = _PERSIST.get(sid)
        if current is None or current.busy or (expected is not None and current is not expected):
            return False
        _PERSIST.pop(sid, None)
    try:
        current.proc.kill()
    except Exception:
        pass
    _bg_forget(sid)
    return True


def _persist_trim(keep_sid=""):
    """腾位置。两道闸:先按内存跨引擎结一次账,再按个数收自己这一池。

    个数那一道单独用不住:一条常驻 claude 进程实测 450~510MB,一份 codex app-server
    连同子进程约 60MB,同样写 6,前者的上限是 3GB、后者是 0.36GB;而两池互不相识,
    claude 池满了只挤 claude,哪怕 codex 那边闲着五个更该收(见 resident 的模块注释)。

    正在跑回合的不动——杀一个在跑的等于把用户这一回合的活弄丢,而回收本身只是为了省资源。
    """
    try:
        import resident
        resident.make_room("claude", keep_sid)
    except Exception:
        pass
    while True:
        with _PERSIST_LOCK:
            if len(_PERSIST) < _PERSIST_MAX:
                return
            idle = [(ep.at, s) for s, ep in _PERSIST.items() if not ep.busy and s != keep_sid]
            if not idle:
                return          # 全都在跑,让它超一会儿,比打断谁都强
            victim = min(idle)[1]
        if not _persist_evict(victim):
            return              # recap 失败就保留；下一次结账再试，不越过可靠性闸


def _persist_reap():
    """闲置回收。不回收会留下一堆常驻的引擎进程,每条都占着一份完整上下文。"""
    while True:
        time.sleep(60)
        now = time.time()
        for sid, ep in list(_PERSIST.items()):
            if ep.busy:
                continue
            if ep.proc.poll() is not None or now - ep.at > _PERSIST_IDLE:
                _persist_evict(sid, ep)


threading.Thread(target=_persist_reap, daemon=True).start()
def _persist_shutdown():
    """Normal exit captures recap; supervised restart leaves it to durable recovery."""
    try:
        import lifecycle
        restarting = lifecycle.restarting()
    except Exception:
        restarting = False
    if not restarting:
        try:
            from .recap import recap_on_shutdown
            recap_on_shutdown()
        except Exception:
            pass
    for ad in list(_ADAPTERS):
        try:
            ad._peer_cancel_all()
        except Exception:
            pass
    for s in list(_PERSIST):
        _persist_drop(s)


atexit.register(_persist_shutdown)


def _persist_roster():
    """报给内存预算那边:这一池有哪些常驻项(见 resident)。"""
    with _PERSIST_LOCK:
        rows = list(_PERSIST.items())
    out = []
    for sid, ep in rows:
        try:
            out.append((sid, ep.proc.pid, ep.busy, ep.at))
        except Exception:
            continue
    return out


try:
    import resident as _resident
    _resident.add_pool("claude", _persist_roster, _persist_evict)
except Exception:
    pass
