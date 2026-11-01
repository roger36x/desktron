# 由 engine_adapters.py 分拆(2026-08-27,纯搬移;见 specs/2026-08-27-repo-partition-2.md)
import re
import queue
import threading
import time


class EventBus:
    """进程内 SSE 总线:适配器 publish,/event 处理器 subscribe。"""

    def __init__(self):
        self._clients = []
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def publish(self, ev):
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass


class _TurnWatch:
    """回合看门狗:安静超过 timeout 秒才动手。

    原来用的是一个 threading.Timer(timeout, proc.kill):从回合开始就计时,到点无条件杀。
    那等于给每一回合设了总时长上限,而目标驱动的长活一两个小时是常事——干得越久越先被杀。
    改成看动静:每收到一帧续一次命,真安静够久了才收口。
    实现上不为每帧重建定时器(一回合几百帧),而是自己按周期醒来比对最后一帧的时刻。"""

    def __init__(self, timeout, on_dead, should_defer=None):
        self.timeout = max(30, int(timeout or 0))
        self.on_dead = on_dead
        self.should_defer = should_defer
        self.at = time.time()
        self.done = threading.Event()
        self.th = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.th.start()
        return self

    def beat(self):
        self.at = time.time()

    def cancel(self):
        self.done.set()

    def _loop(self):
        step = max(5, min(60, self.timeout / 10))
        while not self.done.wait(step):
            if time.time() - self.at > self.timeout:
                defer = False
                if callable(self.should_defer):
                    try:
                        defer = bool(self.should_defer())
                    except Exception:
                        defer = False
                if defer:
                    self.at = time.time()
                    continue
                try:
                    self.on_dead()
                except Exception:
                    pass
                return


# 一段会话默认铺多少个回合。搬迁(import_session)按它重写存储,取消息(GET /session/<id>/message)
# 按它截断——两处必须同一个数:此前一个 25、一个 50,存储里回合多于 25 的会话打开时先铺出 50 个,
# 空闲时的对账把存储重写成 25,刻度那一列跟着缩回去(2026-08-16 用户实见:偶尔加载出超过默认
# 条数的历史)。更早的并没有丢:往上拉到顶按 30 个一批经 /history 现取。
SESSION_WINDOW = 25

# 当前上下文与用户原话之间的分隔。注入在 _run,摘除在各引擎重建历史那一侧——
# 生产与消费必须认同一个串,所以放一份在这里。旧串只用于读取历史记录,不再产出。
SCENE_SEP = "\n\n---\n\n以上是本次会话的工作区上下文,下面是用户的话。\n\n"
LEGACY_SCENE_SEPS = (
    "\n\n---\n\n以上是本次会话的起手情景,下面是用户的话。\n\n",
)
SCENE_SEPS = (SCENE_SEP,) + LEGACY_SCENE_SEPS
SCENE_HEAD = "## 当前上下文"   # 当前上下文的固定开头,见 domains.py 的组装
LEGACY_SCENE_HEADS = ("## 起手已知",)
SCENE_HEADS = (SCENE_HEAD,) + LEGACY_SCENE_HEADS
_DESK_CONTEXT_HEADS = SCENE_HEADS + (
    "〈Desk 会话信息〉",
    "〈任务收尾核对〉",
    "以下内容来自用户显式加入的飞书聊天窗口",
)


def strip_scene_envelope(text):
    """只摘掉有 Desk 固定开头和完整分隔符的上下文信封。"""
    value = str(text or "")
    if not value.lstrip().startswith(_DESK_CONTEXT_HEADS):
        return value
    for sep in SCENE_SEPS:
        if sep in value:
            return value.split(sep, 1)[1]
    return value


def strip_scene_injection(text):
    """标题兼容路径：剥离完整信封，也容忍旧版损坏的上下文记录。"""
    value = str(text or "")
    clean = strip_scene_envelope(value)
    if clean != value:
        return clean
    if value.lstrip().startswith(SCENE_HEADS + ("〈Desk 会话信息〉",)):
        return ""
    return value


# 会话名默认取用户的第一句话。直接拿原文切 60 字会出三类坏名字,2026-09-03 用户实见的
# 「起手已知### 项目记忆摘要thinking 工作区放不归属…」三类占全:
#
#   一、切到了 Desk 自己前置的当前上下文。它只加在送给引擎的那一份里,界面上的气泡仍是
#       用户原话;但 claude 的标题兜底读的正是引擎记录,于是把 Desk 的开场白当成了用户的
#       第一句。codex 那侧 2026-08-26 修过(term_titles._engine_title_clean),claude 落下了。
#   二、markdown 标记原样留着。`## `、`- `、`**`、反引号在一行标题里全是噪音。
#   三、换行被折没,前后两行的字直接粘成一个词——「摘要」接「thinking」就是这么来的。
#
# 一次处理干净,各引擎共用:live 那条路走 _touch,claude 重建历史那条路走 claude_transcript。
_TITLE_LIMIT = 60
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MD_EMPH = re.compile(r"(\*\*|__|\*|_|`)")
_MD_HEAD = re.compile(r"^#{1,6}\s*")
_MD_QUOTE = re.compile(r"^>\s*")
_MD_LIST = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
_TITLE_BREAK = "。！？；，、,.;!? "


def clean_session_title(text, limit=_TITLE_LIMIT):
    """把一段正文压成一行能读的会话名;压不出人话就返回 "",让调用方接着找下一条。

    返回空串是有意义的答案,不是失败:整条都是工作区上下文、或者只有一段代码围栏时,
    正确的做法是跳过它去看下一条消息,而不是硬凑一个名字出来。
    """
    s = strip_scene_injection(text)
    fence = ""
    for raw in s.splitlines():
        ln = raw.strip()
        if fence:                              # 围栏里的是代码,不是句子,整段跳到闭合为止
            if ln.startswith(fence):
                fence = ""
            continue
        if not ln:
            continue
        if ln.startswith("```") or ln.startswith("~~~"):
            fence = ln[:3]
            continue
        if ln.startswith("|") or set(ln) <= set("-=*_ "):   # 表格、分隔线:都不是句子
            continue
        ln = _MD_LIST.sub("", _MD_QUOTE.sub("", _MD_HEAD.sub("", ln)))
        ln = _MD_EMPH.sub("", _MD_LINK.sub(r"\1", ln))
        ln = re.sub(r"\s+", " ", ln).strip()
        if not ln:
            continue
        return _title_cut(ln, limit)
    return ""


def _title_cut(s, limit):
    """够短就原样;要截就尽量截在标点或空格上,截了才加省略号。

    只在末尾四分之一里找断点:找得太靠前的话,一句长话会被砍成半截意思。
    """
    if len(s) <= limit:
        return s
    head = s[:limit]
    at = max(head.rfind(ch) for ch in _TITLE_BREAK)
    if at >= int(limit * 0.75):
        head = head[:at]
    return head.rstrip(_TITLE_BREAK) + "…"
