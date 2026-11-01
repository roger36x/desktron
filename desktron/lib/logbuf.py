"""ui_server 进程内日志捕获:tee stdout/stderr 进环形缓冲,供前端日志悬浮窗轮询。

范围:只捕获本进程 Python 层的写入(print/traceback,含 dreaming 调度器、watchdog、pkgstore
等 daemon 线程)。子进程(引擎/PTY/钩子)继承的是真实 fd,不经 sys.stdout,不在捕获范围;
它们各有自己的落点(termpty 缓冲、rollout、hook 输出),这里不重复收。

契约:install() 尽早调用(晚装会漏启动期日志);tail(since) 按序号增量取,
前端记住 next 轮询,断点续读;缓冲 2000 行封顶,溢出丢最老(seq 不回卷,客户端可感知跳号)。
"""
import sys
import threading
import time
from collections import deque

_BUF = deque(maxlen=2000)
_LOCK = threading.Lock()
_SEQ = [0]


def _push(stream_name, text):
    with _LOCK:
        _SEQ[0] += 1
        _BUF.append({"seq": _SEQ[0], "t": time.strftime("%H:%M:%S"),
                     "s": stream_name, "ln": text})


def push_client(text, stream="ui"):
    """前端往同一个缓冲里写一行。

    这个面板原来只显示后端进程的 stdout/stderr,浏览器里 console.log 的东西进不来——
    于是每次排查前端状态都得开 Safari 的 Web 检查器,门槛高到很多问题就这么放着了
    (2026-08-12:切引擎后过程区展不开,卡在拿不到那三个数)。前后端进同一条流、
    同一个面板,时间线也对得上:哪一帧到了后端、界面那一刻画成什么样,并排就能读。

    stream 借用 _Tee 那套名字,前端来的一律标 ui,面板里能与 out/err 分开看。"""
    text = str(text or "")
    for ln in text.splitlines() or [""]:
        _push(stream, ln[:2000])   # 单行截断:界面那边是等宽窄面板,过长的一行反而读不动


class _Tee:
    """包一层原始流:写入原样透传,整行进缓冲。残段(无换行)攒到下一次冲齐。"""

    def __init__(self, raw, name):
        self._raw = raw
        self._name = name
        self._part = ""
        self._bol = True      # 下一次写入落在行首吗(决定要不要补时间戳)

    def _stamp(self, s):
        """给透传出去的每一行开头补上时刻。

        缓冲那一份一直带 t(日志悬浮窗按它排),而落盘的 stdout 从来没有——事后翻
        /tmp/desktron-web.log 只看得见「发生过什么」,看不见「什么时候」,
        时间线就还原不了(2026-08-25 排查手机连不上时正卡在这里:那 63 条异常
        没法跟用户说的故障窗口对齐,只能靠进程年龄反推)。

        按行首补而不是按 write() 补:一次 write 可能只是半行(print 分多次写),
        整段前面加一个时刻会把时间戳插进句子中间。
        """
        if not s:
            return s
        ts = None
        parts = s.split("\n")
        out = []
        for i, seg in enumerate(parts):
            if i:
                out.append("\n")
                self._bol = True
            if self._bol and seg:
                if ts is None:
                    ts = time.strftime("[%m-%d %H:%M:%S] ")
                out.append(ts)
                self._bol = False
            out.append(seg)
        return "".join(out)

    def write(self, s):
        try:
            self._raw.write(self._stamp(s))
        except Exception:
            pass
        try:
            self._part += s
            while "\n" in self._part:
                ln, self._part = self._part.split("\n", 1)
                if ln.strip():
                    _push(self._name, ln)
        except Exception:
            pass
        return len(s)

    def flush(self):
        try:
            self._raw.flush()
        except Exception:
            pass

    def __getattr__(self, k):   # fileno/isatty/encoding 等透传:subprocess 继承 fd 的行为不变
        return getattr(self._raw, k)


def install():
    """替换 sys.stdout/sys.stderr(幂等)。print(flush=True) 取的是调用时的 sys.stdout,装一次全线程生效。"""
    if not isinstance(sys.stdout, _Tee):
        sys.stdout = _Tee(sys.stdout, "out")
    if not isinstance(sys.stderr, _Tee):
        sys.stderr = _Tee(sys.stderr, "err")


def tail(since=0, limit=500):
    """增量取日志:seq > since 的行(最多 limit),附下次轮询用的游标。"""
    with _LOCK:
        lines = [x for x in _BUF if x["seq"] > since][-limit:]
        return {"next": _SEQ[0], "lines": lines}
