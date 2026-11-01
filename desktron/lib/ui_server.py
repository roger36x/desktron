#!/usr/bin/env python3
"""desktron 自建前端的本地服务:服务 frontend/ 静态页 + 会话契约端点。
同源,免 CORS。会话 API 由各引擎的适配器实现(engine_adapters),不再有常驻引擎 server。
进程链:壳 → 本服务(python)→ 引擎 CLI(PTY 子进程)。本服务退出时一并收掉,不留孤儿。
用法: ui_server.py <frontend_dir> <ui_port> <reserved_port> [repo_root]
(reserved_port 只为兼容既有壳与测试的启动契约,本服务不读取。)
"""
import sys, os, signal, atexit, time, socket, subprocess, json, re, queue, gzip, io, stat
import datetime, email.utils, threading, hashlib
import http.server, socketserver, urllib.request, urllib.error, urllib.parse

PROCESS_STARTED = time.perf_counter()
STARTUP_TRACE_S = 60.0   # 只观察开机头一分钟:冷启诊断,不是常驻的请求日志
STARTUP_SLOW_S = 1.0
# 只记超过一秒的,看不出「三十个请求各几百毫秒」这种累加,而冷启的实际形状正是后者。
# 这里同时按路径归并,到点报一次总量:请求数、服务端总秒数、最贵的几条。
_STARTUP_STATS = {}
_STARTUP_STATS_LOCK = threading.Lock()


def _startup_stats_report(mark):
    with _STARTUP_STATS_LOCK:
        rows = sorted(_STARTUP_STATS.items(), key=lambda kv: -kv[1][1])
    if not rows:
        return
    total = sum(v[1] for _, v in rows)
    count = sum(v[0] for _, v in rows)
    top = "、".join("%s %d 次 %.1fs" % (k, v[0], v[1]) for k, v in rows[:5])
    deskcfg.log("startup", "开机 %ds 内共 %d 个请求,服务端合计 %.1fs;最贵:%s" % (
        mark, count, total, top))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import logbuf
logbuf.install()   # 尽早装:tee stdout/stderr 进环形缓冲,供前端日志悬浮窗(/workbench/logs)轮询
import deskcfg     # 模块形式引入:后台线程日志统一走 deskcfg.log("<模块>", msg),悬浮窗按 [模块] 过滤
# argv/env 派生常量与无状态共享工具集中在 deskcfg,供本文件与各拆分模块统一 import,免循环依赖。
# (_augment_path() 对 PATH 的补全在 deskcfg import 时执行一次。)
from deskcfg import FRONTEND, UI_PORT, DATA_HOME
import termpty   # 进程内 PTY 层:pty.fork 引擎、mux 监管、attach 中继。ui_server 重启会断会话(可 --resume)

# 引擎生命周期(GUI 适配器实例化 + 退出收尾)实现在 engine.py;
# 下列名字 import 回本命名空间,调用点不变。side-effect(注册)留在本文件。
from engine import _bus, _adapter, GUI_ENGINES, _cleanup, _parent_death_watchdog
from engine_adapters import SESSION_WINDOW   # 铺几个回合的单一口径,搬迁与取消息共用
atexit.register(_cleanup)
signal.signal(signal.SIGTERM, lambda *_: (_cleanup(), sys.exit(0)))
signal.signal(signal.SIGINT, lambda *_: (_cleanup(), sys.exit(0)))
_parent_death_watchdog()

# 工作台状态/引擎偏好(workbench.py):本文件仅契约路由与 WS 附着还需要这两个名字。
from workbench import _wb_state_read, _engine_pref

# 终端/会话层(term.py):WS 附着与懒预热用;状态 dict 同一对象,import 共享。
from term import (_TERMS, _TERMS_LOCK, _live_map, _term_attach_mark, _term_seen, _term_meta_read,
                  _term_persisted, _term_quiet_set, _term_context_domain, SIDEBAR_SERVED)

# ---- 工作台本地 API:按领域拆分 ----
# 每域一个 api 模块:owns() 按路径认领,handle(h) 保持原 if 链行为,h 即 Handler 实例。
from core import api as core_api
from workflow import api as workflow_api
from task import api as task_api
from session import api as session_api
from feed import api as feed_api
from remote import api as remote_api
from app import api as app_api
from skill import api as skill_api
from memory import api as memory_api
from provision import api as provision_api
from system import api as system_api
from larkbot import api as larkbot_api
from arrangement import api as arrangement_api
from app_control.capability import peer_pid as app_control_peer_pid
# 五个 mixin 是 ui_server 的职责分册,只被这里 import;加载顺序契约:必须晚于上面的 sys.path 与 wiring。
from ui_static import UiStaticMixin
from ui_boot import UiBootMixin
from ui_contract import UiContractMixin
from ui_remote_gate import UiRemoteGateMixin
from ui_term_ws import UiTermWsMixin



# 请求正文超过这个大小还没被处理函数读完时，不再代读，直接关连接。
BODY_DRAIN_MAX = 1 << 20
# 正常处理与请求后代读都必须有界；客户端少发字节不能永久占住工作线程。
BODY_READ_TIMEOUT = 15.0
BODY_DRAIN_TIMEOUT = 0.25


class _RequestBody:
    """把 rfile 限定在本请求正文之内，并记下还剩多少没读。

    HTTP/1.1 长连接上，处理函数若在读正文之前就返回(404、校验失败、提前拒绝)，
    剩下的正文会被当成下一条请求行的开头，下一条请求随即得到 http.server 的
    501 Unsupported method。浏览器把这个 501 记在复用该连接的无关请求上
    (2026-10-01 实测：会话列表刷新间歇 501)。请求结束后由 handle_one_request 收尾。
    """

    def __init__(self, raw, length):
        self._raw, self.remaining = raw, length

    def read(self, n=-1):
        if self.remaining <= 0:
            return b""
        n = self.remaining if n is None or n < 0 else min(n, self.remaining)
        data = self._raw.read(n)
        self.remaining -= len(data)
        return data

    def readline(self, limit=-1):
        if self.remaining <= 0:
            return b""
        limit = self.remaining if limit is None or limit < 0 else min(limit, self.remaining)
        data = self._raw.readline(limit)
        self.remaining -= len(data)
        return data

    def __getattr__(self, name):
        return getattr(self._raw, name)


class Handler(UiStaticMixin, UiBootMixin, UiContractMixin, UiRemoteGateMixin, UiTermWsMixin, http.server.SimpleHTTPRequestHandler):
    # HTTP/1.1 = 连接可复用。默认的 1.0 每个资源都要重建一次 TCP,而经隧道/VPN 那一路
    # 建一次连接实测 241ms,首屏 22 个资源光握手就 5.3 秒(2026-08-21 用户实见「初次加载很卡」)。
    # 代价是 1.1 要求每个响应都声明长度,否则客户端不知道什么时候算完、一直等下去。
    # 下面 end_headers 里有一道兜底:没声明长度的(SSE 流、少数手写响应)就关连接,
    # 让 EOF 充当结束标志——那正是 1.0 时代的行为,不比现在差。
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **k):
        super().__init__(*a, directory=FRONTEND, **k)

    def setup(self):
        super().setup()
        self.app_control_peer_pid = app_control_peer_pid(self.request)

    # 找不到的路径给一张能走回去的页面,不给 http.server 那张裸的 Error response。
    # 那张页面没有任何返回入口:一旦主 webview 被带到它上面,整个应用就没了,只能退出重开
    # (2026-08-10 实见,起因是备忘里一个相对路径链接)。前端与原生层都已拦住导航,这里是
    # 第三道:真漏过来了,至少还能点一下回到 Desk。
    error_message_format = (
        '<!doctype html><meta charset="utf-8">'
        '<style>html,body{height:100%%;margin:0;background:#1c1c1c;color:#b4b4b4;'   # %% :这串要过 % 格式化
        'font:14.5px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;'
        'display:flex;align-items:center;justify-content:center}'
        'div{text-align:center}h1{font-size:15px;font-weight:500;color:#ededed;margin:0 0 6px}'
        'p{margin:0 0 14px;font-size:13px;color:#8f8f8f}'
        'a{display:inline-block;padding:6px 14px;border-radius:8px;color:#d8d8d8;'
        'background:rgba(255,255,255,.07);text-decoration:none}a:hover{background:rgba(255,255,255,.12)}'
        '</style><div><h1>这个地址没有内容</h1>'
        '<p>%(code)d &middot; %(message)s</p><a href="/">回到 Desk</a></div>'
    )
    error_content_type = "text/html; charset=utf-8"

    # 冷启那几秒的慢请求要留痕。2026-09-05 实测:后端 718ms 就监听、接口本身只要几十毫秒,
    # 而第一个页面从 init 完成到 load 事件隔了 9.5 秒,随后整页重载,前 10 秒的日志一条也
    # 发不出去(缓冲攒到重载那一刻才落)。从外部只看得到「慢」,看不出是哪一类请求被挡住。
    # 预热线程(codex 目录、侧栏、飞书 bot、隧道、几个调度器)全在监听之后同时起,GIL 与
    # 磁盘在那一刻最紧,而这正是页面取首屏数据的同一段时间。
    #
    # 只记开机头一分钟、只记超过一秒的,长连接(事件流、终端)按定义就长,排除掉。
    def parse_request(self):
        # 计时起点必须在这里,不能放在 handle_one_request 开头:那里第一件事是阻塞读请求行,
        # 而 keep-alive 连接上这段等待就是两次轮询之间的空闲。按开头计时,量到的是轮询间隔
        # 而不是处理耗时——2026-09-05 第一版就栽在这:/dev/token 稳定 1.5s、/workbench/terminals
        # 稳定 3.0s、/workbench/domains 36.7s,全是整数,而且首尾相接,一看就不是真在干活。
        ok = super().parse_request()
        self._served_at = time.perf_counter()
        if ok:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > 0:
                self._body_previous_timeout = self.connection.gettimeout()
                self.connection.settimeout(BODY_READ_TIMEOUT)
                self._raw_rfile, self.rfile = self.rfile, _RequestBody(self.rfile, length)
            elif self.headers.get("Transfer-Encoding"):
                self.close_connection = True   # 不解析分块正文，也就不能再复用这条连接
        return ok

    def _finish_request_body(self):
        body, raw = self.rfile, getattr(self, "_raw_rfile", None)
        if raw is None:
            return
        self.rfile, self._raw_rfile = raw, None
        left = getattr(body, "remaining", 0)
        previous_timeout = getattr(self, "_body_previous_timeout", None)
        try:
            if left <= 0:
                return
            if self.close_connection or left > BODY_DRAIN_MAX:
                self.close_connection = True
                return
            self.connection.settimeout(BODY_DRAIN_TIMEOUT)
            while left > 0:
                chunk = raw.read(min(left, 65536))
                if not chunk:
                    self.close_connection = True
                    return
                left -= len(chunk)
        except (OSError, TimeoutError):
            self.close_connection = True
        finally:
            try:
                self.connection.settimeout(previous_timeout)
            except OSError:
                self.close_connection = True

    def handle_one_request(self):
        self._served_at = None
        self._raw_rfile = None
        self._body_previous_timeout = None
        try:
            super().handle_one_request()
        finally:
            self._finish_request_body()
            began = getattr(self, "_served_at", None)
            path = str(getattr(self, "path", "") or "")
            head = path.split("?", 1)[0]
            if began is not None and head not in ("/event", "/term"):
                cost = time.perf_counter() - began
                since_boot = began - PROCESS_STARTED
                if since_boot < STARTUP_TRACE_S:
                    key = re.sub(r"/adp_[0-9a-f]+", "/<sid>", head)[:60]
                    with _STARTUP_STATS_LOCK:
                        row = _STARTUP_STATS.setdefault(key, [0, 0.0])
                        row[0] += 1
                        row[1] += cost
                if since_boot < STARTUP_TRACE_S and cost > STARTUP_SLOW_S:
                    deskcfg.log("startup", "慢请求 %s %s %.0fms(开机后 %.1fs)" % (
                        getattr(self, "command", "?") or "?", head[:70], cost * 1000, since_boot))

    def do_GET(self):
        import remote_auth
        path = self.path.split("?", 1)[0]
        if path == "/workbench/app-control/host/next":
            if not self._gate(): return
            return self._local_mutation("GET")
        if path == remote_auth.PROBE_PATH:
            if not self._gate(): return
            return self._probe()
        if not self._gate():
            return
        if path == "/desk-lan-access/status":
            return self._lan_access_status()
        if path == "/desk-lan-access":
            return self._lan_access()
        if self.path.split("?", 1)[0] == "/term":
            return self._term_ws()
        if self._is_workbench():
            self._workbench("GET")
        elif self._is_contract():
            self._adapter_route("GET")
        elif self.path.split("?", 1)[0] == "/manifest.json":
            self._serve_manifest()
        elif self.path.split("?", 1)[0] in ("/", "/index.html"):
            self._serve_index()
        else:
            super().do_GET()

    def _local_mutation(self, method):
        import lifecycle
        path = self.path.split("?", 1)[0]
        # Restart establishes the gate; a previously dispatched App Control
        # effect must still be allowed to persist its terminal receipt.
        exempt = path in ("/workbench/restart", "/workbench/update/apply",
                          "/workbench/update/dev-app-apply",
                          "/workbench/app-control/host/result")
        admitted = exempt or lifecycle.begin(critical="http_write")
        if not admitted:
            self.close_connection = True
            return self._send_json({"error": "Desk 正在重启，写请求未执行",
                                    "code": "lifecycle_stopping"}, 409)
        try:
            if self._is_workbench():
                return self._workbench(method)
            if self._is_contract():
                return self._adapter_route(method)
            return self.send_error(404, "not found")
        finally:
            if not exempt:
                lifecycle.finish(critical="http_write")

    def do_POST(self):
        if not self._gate():
            return
        return self._local_mutation("POST")

    def do_PUT(self):
        # 每个写方法都先过令牌闸:缺了它,新开一条连接直接发 PUT 就绕过了令牌。
        if not self._gate():
            return
        return self._local_mutation("PUT")

    def do_HEAD(self):
        if not self._gate():
            return
        return super().do_HEAD()

    def do_DELETE(self):
        if not self._gate():
            return
        return self._local_mutation("DELETE")

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class AppControlUnixHandler(Handler):
    """只承载 Agent App Control；不复用主服务的静态页、转发或其他 API。"""

    def _app_control_request(self, method):
        path = self.path.split("?", 1)[0]
        if path != "/workbench/app-control" and not path.startswith(
                "/workbench/app-control/"):
            return self._send_json({"error": "unknown"}, 404)
        return self._workbench(method)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/workbench/app-control/host/next":
            return self._local_mutation("GET")
        return self._app_control_request("GET")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/workbench/app-control" and not path.startswith(
                "/workbench/app-control/"):
            return self._send_json({"error": "unknown"}, 404)
        return self._local_mutation("POST")

    def do_PUT(self):
        return self._send_json({"error": "method_not_allowed"}, 405)

    def do_DELETE(self):
        return self._send_json({"error": "method_not_allowed"}, 405)

    def do_HEAD(self):
        return self._send_json({"error": "method_not_allowed"}, 405)


if hasattr(socketserver, "ThreadingUnixStreamServer"):
    class AppControlUnixServer(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
else:
    AppControlUnixServer = None


class AppControlLoopbackServer(socketserver.ThreadingTCPServer):
    """Windows：只绑定 127.0.0.1 的专用 App Control 通道，对端 PID 取自内核连接表。"""
    daemon_threads = True
    allow_reuse_address = False


# 先把 HTTP 服务真正跑起来，再做目录预热、隧道与 bot 看护。
# 这些工作本来虽各自“后台”，但逐个创建/导入仍发生在 serve_forever 之前；线程刚起时还会
# 同时抢 GIL、磁盘与网络。页面不该为它们付冷启动成本。
import remote_auth
import lan_settings
try:
    lan_settings.load_runtime()
except (OSError, ValueError):
    deskcfg.log('ui', '局域网配置读取失败；受限局域网入口未开放，请检查本机配置。')
import tailscale_settings
try:
    tailscale_settings.load_runtime()
except (OSError, ValueError):
    deskcfg.log('ui', 'Tailscale 配置读取失败；专用入口未开放，请检查本机配置。')
# Accepted peer sidecars are process-owned and their question text is purposely
# absent from the mailbox.  Close crash leftovers before admitting new HTTP
# work; legacy detached delivery claims are returned for explicit wait pickup.
try:
    __import__("peer_mailbox").recover_abandoned(DATA_HOME)
except Exception as _e:
    deskcfg.log("peer-mailbox", f"启动恢复失败:{str(_e)[:120]}")
remote_auth.load_runtime_token()
_BIND = remote_auth.bind_host()   # 主监听：局域网模式保留环回，远端默认与令牌同步。
_SERVER = Server((_BIND, UI_PORT), Handler)
_SERVER_THREAD = threading.Thread(
    target=_SERVER.serve_forever, daemon=True, name="desk-http")
_SERVER_THREAD.start()
_LAN_SERVER = None
_LAN_INTERFACE = remote_auth.lan_interface()
if _LAN_INTERFACE is not None:
    try:
        _LAN_SERVER = Server((str(_LAN_INTERFACE.ip), UI_PORT), Handler)
        threading.Thread(target=_LAN_SERVER.serve_forever, daemon=True,
                         name="desk-lan-http").start()
        deskcfg.log("ui", f"局域网 http://{_LAN_INTERFACE.ip}:{UI_PORT}/ [访问令牌已启用]")
    except OSError as _e:
        deskcfg.log("ui", f"局域网监听失败(errno={_e.errno})；本机入口可用，局域网未开放")
        if _LAN_SERVER is not None:
            _LAN_SERVER.server_close()
        _LAN_SERVER = None
_SERVER.lan_address = str(_LAN_INTERFACE.ip) if _LAN_SERVER is not None else ""
if _LAN_SERVER is not None:
    _LAN_SERVER.lan_address = _SERVER.lan_address
# Tailscale Serve 专用入口：只绑环回，且该端口上的环回来源一律验票(见 remote_auth.serve_port)。
_SERVE_SERVER = None
try:
    _SERVE_PORT = remote_auth.serve_port(UI_PORT)
except ValueError as _e:
    _SERVE_PORT = None
    deskcfg.log("ui", f"Tailscale 入口未开放:{_e}")
if _SERVE_PORT:
    try:
        _SERVE_SERVER = Server(("127.0.0.1", _SERVE_PORT), Handler)
        _SERVE_SERVER.serve_proxy = True
        _SERVE_SERVER.lan_address = ""
        threading.Thread(target=_SERVE_SERVER.serve_forever, daemon=True,
                         name="desk-serve-http").start()
        deskcfg.log("ui", f"Tailscale 入口 http://127.0.0.1:{_SERVE_PORT}/ [每个请求验票]")
    except OSError as _e:
        deskcfg.log("ui", f"Tailscale 入口监听失败(errno={_e.errno})；本机入口可用")
        if _SERVE_SERVER is not None:
            _SERVE_SERVER.server_close()
        _SERVE_SERVER = None
for _srv in (_SERVER, _LAN_SERVER, _SERVE_SERVER):
    if _srv is not None:
        _srv.serve_port = _SERVE_PORT if _SERVE_SERVER is not None else 0
        _srv.lan_interface = str(_LAN_INTERFACE) if _LAN_SERVER is not None else ''
_APP_CONTROL_SOCKET = os.path.join(DATA_HOME, "app-control.sock")
os.makedirs(os.path.dirname(_APP_CONTROL_SOCKET), mode=0o700, exist_ok=True)
if AppControlUnixServer is not None:
    try:
        os.unlink(_APP_CONTROL_SOCKET)
    except FileNotFoundError:
        pass
    _APP_CONTROL_SERVER = AppControlUnixServer(_APP_CONTROL_SOCKET, AppControlUnixHandler)
    os.chmod(_APP_CONTROL_SOCKET, 0o600)
else:
    from app_control.capability import set_loopback_port as _set_app_control_port
    _APP_CONTROL_SOCKET = ""
    _APP_CONTROL_SERVER = AppControlLoopbackServer(("127.0.0.1", 0), AppControlUnixHandler)
    _set_app_control_port(_APP_CONTROL_SERVER.server_address[1])
_APP_CONTROL_THREAD = threading.Thread(
    target=_APP_CONTROL_SERVER.serve_forever, daemon=True, name="desk-app-control-unix")
_APP_CONTROL_THREAD.start()
deskcfg.log("ui", f"http://{_BIND}:{UI_PORT}/"
            + ("  [远端令牌已启用]" if _BIND != "127.0.0.1" else ""))
deskcfg.log("startup", f"后端已监听({(time.perf_counter() - PROCESS_STARTED) * 1000:.0f}ms)")
def _serve_until_shutdown():
    try:
        if sys.platform == "win32":
            # Windows 上无超时的 join() 不会被信号打断；分段等待，让宿主经停止事件触发的
            # SIGTERM 收尾能在主线程执行。
            while _SERVER_THREAD.is_alive():
                _SERVER_THREAD.join(0.5)
        else:
            _SERVER_THREAD.join()
    finally:
        if _LAN_SERVER is not None:
            _LAN_SERVER.shutdown()
            _LAN_SERVER.server_close()
        if _SERVE_SERVER is not None:
            _SERVE_SERVER.shutdown()
            _SERVE_SERVER.server_close()
        _APP_CONTROL_SERVER.shutdown()
        _APP_CONTROL_SERVER.server_close()
        try:
            if _APP_CONTROL_SOCKET:
                os.unlink(_APP_CONTROL_SOCKET)
        except FileNotFoundError:
            pass
        _SERVER.shutdown()
        _SERVER.server_close()
        _cleanup()


# 安装/门禁仍加载真实路由与适配器，但不启动预热、Bot、安排或更新任务。
# install_check 只在空白临时 HOME 与数据目录使用此模式，不代表正式宿主验收。
if os.environ.get("DESKTRON_INSTALL_CHECK") == "1":
    deskcfg.log("install-check", "隔离验收：后台任务未启动")
    _serve_until_shutdown()
    sys.exit(0)

for _mark in (20, 60):
    threading.Timer(_mark, _startup_stats_report, args=(_mark,)).start()


# 常驻引擎进程的内存预算:周期结账,超了跨引擎收最闲的那个(见 lib/resident.py)。
# 准入那一道单独用不住——2026-08-23 实见的情形是三个会话就地长大,期间一次新准入都没有。
try:
    __import__("resident").start_sweep()
except Exception:
    pass


def _claude_theme_boot():
    """Desk 阅读主题随安装落位:主题文件幂等写入 ~/.claude/themes/;仅当用户从未设置过 theme 键时
    默认选中一次(落标记防重复,用户之后改任何主题都尊重)。可退:设置页开关 / CC 内 /theme。"""
    try:
        thdir = os.path.expanduser("~/.claude/themes")
        thfile = os.path.join(thdir, "desk-reading.json")
        os.makedirs(thdir, exist_ok=True)
        if not os.path.exists(thfile):
            with open(thfile, "w", encoding="utf-8") as f:
                json.dump({"name": "Desk 阅读", "base": "dark", "overrides": {
                    "text": "#d8d8d4", "inactive": "#9a9a96", "subtle": "#5c6066",
                    "claude": "#d97757", "success": "#8cc265", "error": "#e05561",
                    "warning": "#e6c07b", "suggestion": "#4aa5f0", "permission": "#4aa5f0",
                    "planMode": "#42b3c2", "promptBorder": "#565b65",
                    "diffAdded": "#243524", "diffRemoved": "#3a2426",
                    "diffAddedDimmed": "#20291f", "diffRemovedDimmed": "#2b2022",
                    "diffAddedWord": "#3a5a36", "diffRemovedWord": "#6b3a3e",
                }}, f, ensure_ascii=False, indent=2)
        mark = os.path.join(DATA_HOME, "claude-theme-defaulted")
        spath = os.path.expanduser("~/.claude/settings.json")
        if os.path.exists(mark):
            return
        cc = {}
        if os.path.exists(spath):
            with open(spath, encoding="utf-8") as f:
                cc = json.load(f)
        if "theme" not in cc:   # 只在用户从未设置过时默认一次;设置过(含 auto/dark)不碰
            cc["theme"] = "custom:desk-reading"
            with open(spath, "w", encoding="utf-8") as f:
                json.dump(cc, f, ensure_ascii=False, indent=2)
            deskcfg.log("claude-theme", "首次默认启用 Desk 阅读主题")
        with open(mark, "w") as f:
            f.write("1")
    except Exception as _e:
        deskcfg.log("claude-theme", f"落位失败:{str(_e)[:120]}")
_claude_theme_boot()
# 记忆调度器(dreaming 位):会话间定期跑整理动词的候选分析,产出人审报告(见 memory_scheduler)。
# 机械分析自动、落成品/删除人审;DESKTRON_DREAM=off 关。daemon 线程,软失败不阻塞启动。
try:
    __import__("memory_scheduler").start_background()
except Exception as _e:
    deskcfg.log("dreaming", f"调度器未启动:{str(_e)[:120]}")
# PTY 只剩 shell / 登录 / 更新这几种工具终端(引擎的 TUI 形态 2026-08-20 退场)。
# 原来这里还有一段「会话预热」:开机把最近的休眠会话逐个 resume 成 PTY,点开秒进。
# GUI 会话没有可预热的对象——进程按回合起、跑完就退,预热等于什么都没做,整段随 TUI 撤掉。
# Codex 的模型目录也预热一次。它是切会话必经的 /config 的一部分,拉一遍要起 app-server
# 再问两轮 RPC(实测 5.2 秒),压在切换上就是「切 codex 比切 cc 慢」。缓存本身落盘、TTL 一天,
# 这里只管把首次那一下挪到开机时。拉不到不影响任何事:界面会用兜底档位表。
def _prewarm_codex_catalog():
    try:
        from codex_appserver import catalog_warm
    except Exception:
        return
    began = time.perf_counter()
    try:
        catalog_warm(cwd=deskcfg.REPO_ROOT)
    except Exception as e:
        deskcfg.log("prewarm", f"codex 目录预热失败:{str(e)[:80]}")
        return
    # 各预热各报自己的完成时刻:它们与页面取首屏数据挤在同一段时间,不留痕就只能猜谁挡了谁。
    deskcfg.log("startup", "codex 目录预热完成(%.0fms,开机后 %.1fs)" % (
        (time.perf_counter() - began) * 1000, began - PROCESS_STARTED))


__import__("threading").Thread(target=_prewarm_codex_catalog, daemon=True).start()


# 侧栏那份列表要扫各引擎的记录目录取标题,冷跑一次实测 1.17s,之后落缓存只要 0.04s。
# 那一次原来由第一个请求来付——正好是开机后人盯着空侧栏的那几秒。启动就在后台跑一遍,
# 界面连上来时缓存已经是热的。失败无所谓:请求那侧照样会自己扫。
def _warm_sidebar():
    began = time.perf_counter()
    try:
        _term_persisted()
    except Exception as e:
        deskcfg.log("ui", f"侧栏预热失败:{str(e)[:120]}")
        return
    deskcfg.log("startup", "侧栏预热完成(%.0fms,开机后 %.1fs)" % (
        (time.perf_counter() - began) * 1000, began - PROCESS_STARTED))
__import__("threading").Thread(target=_warm_sidebar, daemon=True).start()
# 飞书 bot 的长连接:enabled 且配了凭证的 agent 各拉一条，并持续看护。短断线
# 由官方 SDK 恢复；sidecar 退出或长时间未连回时由 Desk 退避重启。整个过程都在
# 后台线程里，网络慢的时候不挡页面启动。
try:
    larkbot_api.start_background()
except Exception as _e:
    deskcfg.log("larkbot", f"看护未启动:{str(_e)[:120]}")
# 用户安排与 Bot Agent 在同一台机器执行。调度线程只领取本机声明；多端页面通过受控
# 管理面聚合，不把运行责任搬来搬去。
try:
    __import__("arrangement.scheduler", fromlist=["start_background"]).start_background(deskcfg.REPO_ROOT)
except Exception as _e:
    deskcfg.log("arrangements", f"调度器未启动:{str(_e)[:120]}")
# 自更新只在后台检查与暂存。普通更新一定等用户确认；必须下线的版本也只有连续满足
# 静默条件后，才由脱离进程树的独立更新器切换并重启。开发 checkout 永不自动安装。
try:
    if __import__("self_update").start_background():
        deskcfg.log("self-update", "后台检查已启动")
except Exception as _e:
    deskcfg.log("self-update", f"后台检查未启动:{str(_e)[:120]}")
# TASKS.md 与 task-ledger.json 的变化看护:前者可由 agent 直接编辑,后者由 desk-task
# 独立进程更新,两条都不经过当前后端。每秒 stat 一遍,变了推 task.updated SSE；详情里的
# 轻量进展因此不必等客户端轮询。见 task_watch 的模块说明。
try:
    import task_watch
    task_watch.start(deskcfg.REPO_ROOT, _bus, deskcfg.DATA_HOME)
except Exception as _e:
    deskcfg.log("task", f"清单看护未启动:{str(_e)[:120]}")
_serve_until_shutdown()
