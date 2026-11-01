"""进程内 PTY 层:pty.fork 起引擎、一个 mux 线程监管所有 master fd(缓冲 + OSC 标题嗅探 +
工作态信号),把输出经 sink 回调推给附着的客户端,把客户端输入写回 PTY。

定位=瘦 PTY 监管,不含业务逻辑(域解析/codex 捕获/meta/标题策略留在 term.py/ui_server)。
原 sessiond daemon 的逻辑搬进 ui_server 进程内:不再有独立进程、unix socket、double-fork、
跨进程僵尸回收——PTY 是 ui_server 的直接子进程,ui_server 退出经 kill_all 一并带走,无孤儿。
代价:ui_server 重启会断会话(会话可 --resume 找回)。

attach 模型:pty_attach(sid, sink, on_close)。sink(bytes) 由 ui_server 提供(WS 封帧后发浏览器);
on_close() 在 PTY 退出时由 mux 调用(ui_server 据此关浏览器 WS)。串行回放:回放期间 mux 只往
buf 追加不调 sink,回放完补增量再设 sink——避免回放与实时输出并发调 sink 造成字节交错→转义序列破损。
"""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import codecs
import fcntl
import json
import os
import re
import selectors
import signal
import struct
import subprocess
import sys
import threading
import time

if oscompat.IS_WINDOWS:
    # Windows 用 ConPTY(经 pywinpty)。每个会话一个阻塞读线程，缓冲与回放逻辑与 POSIX 共用。
    pty = termios = None
else:
    import pty
    import termios

_TITLE_RE = re.compile(rb"\x1b\][012];([^\x07\x1b]*)(?:\x07|\x1b\\)")   # OSC 0/1/2 标题
# 影响 attach 还原度的私有模式(DECSET/DECRST):缓冲超 _BUF_CAP 从头裁后,app 启动时一次性发的这些
# 建屏序列会被裁掉,故单独记 sess["modes"] 粘性状态,pty_attach 回放前补回——否则重连进新 xterm 缺
# alt-screen/鼠标上报 → 残帧(光标卡 home、输入框不画)、滚轮滚不动历史。
_MODE_RE = re.compile(rb"\x1b\[\?([0-9;]+)([hl])")
_STICKY_MODES = frozenset((47, 1047, 1049,             # alt-screen
                           1000, 1001, 1002, 1003, 1004,  # 鼠标上报 / focus 上报
                           1005, 1006, 1015,            # 鼠标编码
                           2004, 25))                   # bracketed paste / 光标可见
# 每会话回放缓冲上限。裸字节缓冲,attach 时整段回放进新 xterm 重建 scrollback。
# 取 2MB(原 256KB):cursor 等普通屏 TUI 流式输出时每帧「上移清行+整块重画」会喷海量重绘
# churn(实测 262KB 里净进 scrollback 仅 ~20 行,其余是同一 UI 块刷 1700+ 次),256KB 会被 churn
# 撑爆、把前段真历史从头裁掉 → 重连后滚不动历史。放大到 2MB 让真历史在多轮对话内不被挤掉。
# (治本是服务端维护真 scrollback / 前端会话级常驻 xterm,见讨论;此处先救急。)
_BUF_CAP = 2 * 1024 * 1024


def _mode_prelude(modes):
    """据 sess 记录的当前私有模式,合成 attach 回放前的「建屏前缀」:先进 alt-screen(三者择一,1049 优先),
    再开鼠标上报/编码/bracketed paste;光标被显式藏起时补 ?25l。让重连进的新 xterm 回到正确屏幕态。"""
    if not modes:
        return b""
    out = bytearray()
    for alt in (1049, 1047, 47):      # alt-screen 择一:先进备用屏,后续回放内容才画在对的屏上
        if modes.get(alt):
            out += b"\x1b[?%dh" % alt
            break
    for n in (1000, 1001, 1002, 1003, 1004, 1005, 1006, 1015, 2004):
        if modes.get(n):
            out += b"\x1b[?%dh" % n
    if modes.get(25) is False:        # 默认光标可见,仅当 app 显式藏过才补隐藏
        out += b"\x1b[?25l"
    return bytes(out)
_PTY = {}           # id → sess(进程内活会话)
_LOCK = threading.Lock()
_SEQ = [0]
_EXIT = {}          # pid → waitpid status:reaper 暂存、_finalize_exit pop 读


def _reap_children(*_):
    """只回收本模块登记的 PTY 子进程，并暂存 status 供 _finalize_exit 读。

    ui_server 里还有 subprocess 管理的 Git、隧道与工具进程；waitpid(-1) 会抢走它们的
    退出状态，使 subprocess.run/poll 把失败误报为成功或永远存活。信号处理期间先取一份
    PID 快照，不拿线程锁；刚 fork 尚未登记的进程由 mux 巡检或 _finalize_exit 兜底。
    """
    pids = [s.get("pid") for s in list(_PTY.values()) if s.get("pid")]
    for wanted_pid in pids:
        try:
            pid, status = os.waitpid(wanted_pid, os.WNOHANG)
            if pid == 0:
                continue
            _EXIT[pid] = status
        except (ChildProcessError, OSError):
            continue
    if len(_EXIT) > 1024:
        _EXIT.clear()   # 兜底防膨胀:正常由 _finalize_exit pop 清掉,丢历史无妨


def _exit_info(status):
    """把 waitpid status 解析成给前端的退出说明。status=None 表示没取到,退回通用文案。"""
    if status is None:
        return {"sig": None, "code": None, "text": "终端已退出"}
    if os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        try: name = signal.Signals(sig).name
        except Exception: name = ""
        return {"sig": sig, "code": None,
                "text": f"终端被终止 · 信号 {sig}{(' ' + name) if name else ''}"}
    if os.WIFEXITED(status):
        code = os.WEXITSTATUS(status)
        if code == 0:
            return {"sig": None, "code": 0, "text": "终端已退出"}
        hint = " · 命令未找到或不可执行" if code == 127 else ""
        return {"sig": None, "code": code, "text": f"终端已退出 · 退出码 {code}{hint}"}
    return {"sig": None, "code": None, "text": "终端已退出"}


def _finalize_exit(sess):
    """PTY 退出收尾(独立线程跑):取退出 status → 回调 on_close(info)。
    在独立线程里取码,避免阻塞 mux(单线程,卡一个会卡所有会话)。与 SIGCHLD reaper
    (_reap_children)抢回收时谁先拿到都行:reaper 把 status 暂存 _EXIT,这里先 pop;
    pop 不到再自己阻塞 waitpid;被 reaper 抢先(ChildProcessError)则轮询 _EXIT 兜底。"""
    pid = sess["pid"]
    status = _EXIT.pop(pid, None)
    if sess.get("proc") is not None:
        info = _exit_info_windows(sess["proc"])
        _deliver_exit(sess, info)
        return
    if status is None:
        try:
            _, status = os.waitpid(pid, 0)   # 阻塞到子进程真正退出
        except ChildProcessError:
            deadline = time.time() + 1.0     # reaper 抢先回收:轮询它写入的 _EXIT,至多约 1s
            while time.time() < deadline:
                status = _EXIT.pop(pid, None)
                if status is not None:
                    break
                time.sleep(0.02)
        except OSError:
            status = None
    _deliver_exit(sess, _exit_info(status))


def _exit_info_windows(proc):
    code = None
    for _ in range(50):   # ConPTY 关闭输出与进程退出码可见之间有极短间隔
        code = getattr(proc, "exitstatus", None)
        if code is not None or not proc.isalive():
            break
        time.sleep(0.02)
    code = getattr(proc, "exitstatus", None)
    if code is None:
        return {"sig": None, "code": None, "text": "终端已退出"}
    if code == 0:
        return {"sig": None, "code": 0, "text": "终端已退出"}
    return {"sig": None, "code": code, "text": f"终端已退出 · 退出码 {code}"}


def _deliver_exit(sess, info):
    try:
        from provision import setup_tasks
        setup_tasks.finish_terminal(sess['id'], info, sess.get('buf', b''))
    except Exception as error:
        print('[setup-task] 保存终端退出状态失败: ' + type(error).__name__, flush=True)
    if sess.get("close_text"):
        info["text"] = sess["close_text"]
    cb = sess.get("on_close")
    if cb is not None:
        try: cb(info)
        except Exception: pass


try:
    if hasattr(signal, "SIGCHLD"):
        signal.signal(signal.SIGCHLD, _reap_children)
except (ValueError, OSError):
    pass   # 非主线程 import 时设不了信号,mux 巡检兜底


# ---- mux:一个线程 + selectors watch 所有 PTY master fd,增删经队列 + wake 管道 ----
_MUX = {"sel": None, "wake_w": None, "add": [], "rm": [], "lock": threading.Lock(), "on": False}


def _mux_ensure():
    with _MUX["lock"]:
        if _MUX["on"]:
            return
        _MUX["on"] = True
    sel = selectors.DefaultSelector()
    r, w = os.pipe()
    _MUX["sel"], _MUX["wake_w"] = sel, w
    sel.register(r, selectors.EVENT_READ, data="wake")
    threading.Thread(target=_mux_loop, args=(r,), daemon=True).start()


def _mux_add(sess):
    with _MUX["lock"]:
        _MUX["add"].append(sess)
    try: os.write(_MUX["wake_w"], b"\x00")
    except Exception: pass


def _mux_loop(wake_r):
    sel = _MUX["sel"]
    while True:
        for key, _ in sel.select(timeout=30):
            if key.data == "wake":
                try: os.read(wake_r, 4096)
                except Exception: pass
                continue
            sess = key.data
            try: data = os.read(key.fd, 65536)
            except OSError: data = b""
            if not data:   # 子进程退出
                sess["alive"] = False
                try: sel.unregister(key.fd)
                except Exception: pass
                try: os.close(key.fd)
                except Exception: pass
                _session_ended(sess)
                continue
            _ingest(sess, data)
        with _MUX["lock"]:
            adds, rms = _MUX["add"], _MUX["rm"]; _MUX["add"], _MUX["rm"] = [], []
        for s in adds:
            try: sel.register(s["master"], selectors.EVENT_READ, data=s)
            except Exception: pass
        for fd in rms:
            try: sel.unregister(fd)
            except Exception: pass
            try: os.close(fd)
            except Exception: pass
        _reap_children()   # 被动巡检:兜底回收漏网僵尸(SIGCHLD 可能丢失或合并)


def _session_ended(sess):
    """子进程输出结束：注销会话，在独立线程取退出状态(可能阻塞)，取到后回调 on_close。"""
    with _LOCK:
        if _PTY.get(sess["id"]) is sess:
            _PTY.pop(sess["id"], None)
    threading.Thread(target=_finalize_exit, args=(sess,), daemon=True).start()


def _ingest(sess, data):
    """一段 PTY 输出：追加回放缓冲(超限从完整转义序列处裁剪)、嗅探标题与粘性模式、转发给附着端。"""
    b = sess["buf"]; b += data
    if len(b) > _BUF_CAP:
        cut = len(b) - _BUF_CAP
        esc = b.find(b"\x1b", cut)     # 向前找下一个 ESC:回放从完整转义序列开头起,不留碎片
        if 0 <= esc < cut + 16384:
            cut = esc
        else:
            prev = b.rfind(b"\x1b", max(0, cut - 256), cut)   # cut 可能落在序列体内
            if prev >= 0:
                next_esc = b.find(b"\x1b", cut)
                cut = next_esc if next_esc >= 0 else -1   # 后无 ESC 则本轮不裁,等积累
        if cut >= 0:
            del b[:cut]
    sess["last_out"] = time.time()
    if sess.get("sink") is None:
        sess["unread"] = True
    tm = _TITLE_RE.findall(data)
    if tm:
        t = tm[-1].decode("utf-8", "ignore").strip()
        if t:
            sess["osc_title"] = t[:80]
    # 粘性模式跟踪:拼上一 chunk 残尾再扫,防 DECSET 序列骑在读取边界上被漏记
    scan = sess.get("_modetail", b"") + data
    if b"\x1b[?" in scan:
        md = sess.setdefault("modes", {})
        for params, hl in _MODE_RE.findall(scan):
            on = hl == b"h"
            for p in params.split(b";"):
                if p.isdigit():
                    n = int(p)
                    if n in _STICKY_MODES:
                        md[n] = on
    sess["_modetail"] = scan[-64:]   # 留残尾:覆盖被切断的半个序列
    sink = sess.get("sink")
    if sink is not None and not sess.get("_replaying"):
        try: sink(data)
        except Exception:
            sess["sink"] = None   # 客户端断了,detach 保活


def _win_reader(sess):
    """Windows:阻塞读 ConPTY 输出。PtyProcess.read 返回已解码文本，这里统一回到 UTF-8 字节。"""
    proc = sess["proc"]
    while True:
        try:
            text = proc.read(65536)
        except EOFError:
            break
        except Exception:
            if not proc.isalive():
                break
            time.sleep(0.02)
            continue
        if text:
            _ingest(sess, text.encode("utf-8", "surrogateescape") if isinstance(text, str) else bytes(text))
        elif not proc.isalive():
            break
    sess["alive"] = False
    _session_ended(sess)


def _win_spawn(cmd, env, cwd, cols, rows):
    from winpty import PtyProcess   # pywinpty;只在 Windows 运行依赖中安装
    return PtyProcess.spawn(list(cmd), cwd=cwd, env=dict(env or {}), dimensions=(rows, cols))


def _winsize(master, cols, rows, sess=None):
    try:
        if sess is not None and sess.get("proc") is not None:   # Windows ConPTY 会话
            sess["proc"].setwinsize(rows, cols)
            return
        if master is None:
            return
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except Exception:
        pass


# ---- 会话生命周期 ----
def pty_open(cmd, env, cwd, sid=None, cols=80, rows=24):
    """pty.fork 起 cmd;sid 由调用方给(claude uuid / codex resume id),
    缺省发内部序号 t{N}。返回 (sid, pid, created):created=False 表示该 sid 已有活会话,直接复用、
    未 spawn。env/cwd 全由调用方算好传入。

    幂等保证:check 已存活 + fork + 登记 全在 _LOCK 内,使并发的同 sid resume(前端 POST 与 WS、
    重复点击)收敛到单进程。否则各 spawn 一个引擎进程,抢 codex rollout 的单 attach
    槽 → 输家空白或退出(终端已退出)。fork 在锁内:子进程立即 execvpe,不碰锁,无死锁。"""
    with _LOCK:
        if sid:
            ex = _PTY.get(sid)
            if ex and ex.get("alive"):
                return sid, ex["pid"], False   # 已有活会话:复用,不重复 spawn
        if not sid:
            _SEQ[0] += 1
            sid = f"t{_SEQ[0]}"
        if oscompat.IS_WINDOWS:
            try:
                proc = _win_spawn(cmd, env, cwd, cols, rows)
            except Exception as error:
                raise OSError("终端启动失败:%s" % error) from error
            sess = {"id": sid, "pid": proc.pid, "master": None, "proc": proc,
                    "decoder": codecs.getincrementaldecoder("utf-8")("replace"),
                    "buf": bytearray(), "sink": None, "on_close": None, "alive": True,
                    "osc_title": "", "created": int(time.time()), "last_out": 0.0,
                    "unread": False, "cwd": cwd, "cols": cols, "rows": rows,
                    "_replaying": False, "modes": {}}
            _PTY[sid] = sess
            threading.Thread(target=_win_reader, args=(sess,), daemon=True).start()
            return sid, proc.pid, True
        pid, master = pty.fork()
        if pid == 0:
            try: os.chdir(cwd)
            except Exception: pass
            try:
                os.execvpe(cmd[0], cmd, env)   # 成功则不返回
            except Exception:
                os._exit(127)                  # exec 失败(命令不存在/不可执行):定为 127,别让异常打出 traceback 进 PTY
            os._exit(127)
        sess = {"id": sid, "pid": pid, "master": master, "buf": bytearray(),
                "sink": None, "on_close": None, "alive": True, "osc_title": "",
                "created": int(time.time()), "last_out": 0.0, "unread": False,
                "cwd": cwd,   # spawn 时的工作目录:查看器用它做允许根(见 assets._view_roots)
                "cols": cols, "rows": rows, "_replaying": False, "modes": {}}
        _PTY[sid] = sess   # 锁内登记:并发的同 sid 调用从此看到 alive→复用
    _winsize(master, cols, rows)
    _mux_ensure()
    _mux_add(sess)
    return sid, pid, True


def pty_set_on_close(sid, on_close):
    """提前设置 on_close 回调，消除 pty_open 与 pty_attach 之间的窗口——
    此窗口内进程若退出，mux 线程因 on_close 为 None 而静默移除会话，
    导致 pty_attach 找不到会话返回 False → 前端显示「终端已退出」。"""
    with _LOCK:
        sess = _PTY.get(sid)
    if sess:
        sess["on_close"] = on_close


def pty_kill(sid, sig=15, close_text=""):
    """杀会话进程。SIGTERM 可能被忽略(交互 shell 默认无视 TERM),故 TERM 路径起后台线程
    校验退出、超时升级 SIGKILL——否则调用方(如 _term_kill)先删了记录、进程没死,成永久孤儿。"""
    with _LOCK:
        sess = _PTY.get(sid)
    if not sess:
        return False
    if close_text:
        sess["close_text"] = str(close_text)[:80]
    pid = sess["pid"]
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return True
    except Exception as e:
        print(f"[termpty] kill {sid}(pid {pid}) 信号 {sig} 发送失败:{e}", flush=True)
    if sig == signal.SIGTERM:
        threading.Thread(target=_kill_escalate, args=(sid, pid), daemon=True).start()
    return True


def _kill_escalate(sid, pid, wait=1.5):
    """SIGTERM 后台兜底:等 wait 秒,会话仍活(mux 未见 EOF)则 SIGKILL。pid 比对防会话重开误杀。"""
    deadline = time.time() + wait
    while time.time() < deadline:
        with _LOCK:
            sess = _PTY.get(sid)
        if not sess or not sess.get("alive") or sess["pid"] != pid:
            return   # 已退出(或同 sid 新会话),不越界补刀
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
        print(f"[termpty] {sid}(pid {pid}) SIGTERM 未退,已升级 SIGKILL", flush=True)
    except ProcessLookupError:
        pass
    except Exception as e:
        print(f"[termpty] {sid}(pid {pid}) SIGKILL 失败:{e}", flush=True)


def kill_all(sig=15):
    """退出收尾:杀掉所有 PTY 子进程(ui_server atexit/SIGTERM 调,确保不留孤儿)。"""
    with _LOCK:
        pids = [s["pid"] for s in _PTY.values()]
    for pid in pids:
        try: os.kill(pid, sig)
        except Exception: pass


def pty_list():
    """活会话快照 [{id,pid,alive,osc_title,created,unread,last_out}]。供 term.py 列表/工作态读。"""
    with _LOCK:
        ss = sorted(_PTY.values(), key=lambda x: x["created"])
    return [{"id": s["id"], "pid": s["pid"], "alive": s["alive"], "osc_title": s.get("osc_title", ""),
             "created": s["created"], "unread": s.get("unread", False),
             "last_out": s.get("last_out", 0.0)} for s in ss]


def pty_info(sid):
    """返回重启一段 PTY 所需的稳定快照；不存在或已退出时返回 None。"""
    with _LOCK:
        s = _PTY.get(sid)
        if not s or not s.get("alive"):
            return None
        return {"id": s["id"], "pid": s["pid"], "cwd": s.get("cwd"),
                "cols": s.get("cols", 80), "rows": s.get("rows", 24)}


def pty_cwds():
    """活会话 spawn 时的工作目录集合。查看器拿它当允许根:项目退役后,外部仓(如 ~/work/desktron)
    不再是注册域,终端里点出来的路径会全部越界 404;有会话跑在那儿就说明它是当前在用的目录。
    只取 spawn 值不走 lsof:这里在每次 view 请求里调,要够快。"""
    with _LOCK:
        return {s.get("cwd") for s in _PTY.values() if s.get("alive") and s.get("cwd")}


def pty_cwd(sid):
    """会话当前工作目录(经 lsof 查 PTY 进程的 cwd fd,仅 macOS/Linux 有 lsof 时可用)。
    查不到(无 lsof、已退出、权限不足)返回 None,调用方按"未知"处理,不报错。"""
    with _LOCK:
        sess = _PTY.get(sid)
    if not sess or not sess.get("alive"):
        return None
    try:
        out = subprocess.run(["lsof", "-a", "-p", str(sess["pid"]), "-d", "cwd", "-Fn"],
                             capture_output=True, text=True, timeout=1).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


# ---- attach:sink 回调中继,串行回放 ----
def pty_attach(sid, sink, on_close=None):
    """附着:sink(bytes) 收 PTY 输出(ui_server 负责 WS 封帧),on_close() 在 PTY 退出时回调。
    串行回放:回放期间 _replaying=True,mux 只追加 buf 不调 sink;回放完补增量再设 sink。
    返回 True=成功(此后由 ui_server 跑输入循环 pty_input/pty_resize),False=会话不存在。"""
    with _LOCK:
        sess = _PTY.get(sid)
    if not sess:
        return False
    sess["sink"] = None          # 单活附着:旧 sink 失效→旧 WS 转哑,由前端 attachTerminal 关掉(不在后端强关,避免误关前端仍活跃的连接)
    sess["on_close"] = on_close
    sess["_replaying"] = True
    sent = 0
    try:
        prelude = _mode_prelude(sess.get("modes") or {})   # 先补回被裁掉的建屏序列(alt-screen/鼠标),再回放可见缓冲
        if prelude:
            try: sink(prelude)
            except Exception:
                sess["_replaying"] = False
                return False
        snap = bytes(sess["buf"]); sent = len(snap)
        try: sink(snap)
        except Exception:
            sess["_replaying"] = False
            return False
        after = len(sess["buf"])
        if after > sent:
            try: sink(bytes(sess["buf"][sent:]))
            except Exception: pass
            sent = after
    finally:
        sess["sink"] = sink
        sess["unread"] = False
        sess["_replaying"] = False
        # 清标志前 mux 可能又追加了数据(未被上面 sink 覆盖),补发一次:
        # 重复 ESC 序列无损,但丢前缀会让终端收到残片→可见乱码。
        after = len(sess["buf"])
        if after > sent:
            try: sink(bytes(sess["buf"][sent:]))
            except Exception: pass
    return True


def pty_buf_info(sid):
    """诊断:回放缓冲现状。len=字节数;alt=当前是否处于 alt-screen(最后一个进入序列在离开之后)。
    若 buf 被 _BUF_CAP 从头裁掉、把进入 alt-screen 的序列裁没了,alt 会是 False——回放进新 xterm
    时缺了屏幕态建立序列,绝对定位错位/残帧。用于判定切会话空白是几何重绘问题还是 buf 裁剪问题。"""
    with _LOCK:
        sess = _PTY.get(sid)
        if not sess:
            return None
        b = sess["buf"]
        return {"len": len(b), "alt": b.rfind(b"\x1b[?1049h") > b.rfind(b"\x1b[?1049l")}


def pty_detach(sid, sink):
    with _LOCK:
        sess = _PTY.get(sid)
    if sess and sess.get("sink") is sink:
        sess["sink"] = None
        sess["on_close"] = None


def pty_input(sid, data):
    with _LOCK:
        sess = _PTY.get(sid)
    if not sess:
        return False
    if sess.get("proc") is not None:
        try:
            text = sess["decoder"].decode(bytes(data))
            if text:
                sess["proc"].write(text)
            return True
        except Exception:
            return False
    try:
        view = memoryview(data)
        while view:
            written = os.write(sess["master"], view)
            if written <= 0:
                return False
            view = view[written:]
        return True
    except Exception:
        return False


def pty_resize(sid, cols, rows):
    with _LOCK:
        sess = _PTY.get(sid)
    if sess:
        sess["cols"], sess["rows"] = cols, rows
        _winsize(sess["master"], cols, rows, sess)


def pty_redraw(sid, cols, rows, settle=0.06):
    """抖动一行触发全屏/内联 TUI 重画当前帧，最终恢复请求的几何。"""
    pty_resize(sid, cols, max(1, rows - 1))
    time.sleep(settle)
    pty_resize(sid, cols, rows)
