"""Codex app-server 的最小 JSON-RPC 客户端。

Desk 只在这里依赖 app-server 协议：读取模型/协作模式目录，以及运行 GUI 回合。
GUI 按 Desk 会话复用一个 stdio 连接；同一连接上的后续回合只发 turn/start，不再反复
启动 app-server 与 thread/resume。不同会话仍使用不同连接，通知不会交叉。
"""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import atexit, collections, hashlib, json, os, queue, re, selectors, signal, subprocess, tempfile, threading, time, uuid


class AppServerError(RuntimeError):
    pass


class AppServerTimeout(AppServerError):
    """这一次读没等到东西。单独成一档:等一条响应时它是故障,等回合事件时它只是安静。"""


class _CapacityUnavailable(AppServerError):
    """One native turn ended before execution because the selected model was full."""
    def __init__(self, thread_id, error):
        self.thread_id = str(thread_id or '')
        self.error = dict(error or {})
        super().__init__(str(self.error.get('message') or 'Selected model is at capacity'))


class _WorkspaceRoutingUnavailable(AppServerError):
    """Codex could not discover its remote account/backend workspace route."""
    def __init__(self, thread_id, error):
        self.thread_id = str(thread_id or '')
        self.error = dict(error or {})
        super().__init__(str(self.error.get('message') or 'workspace routing discovery failed'))


# Codex Desktop 2026-09-16 uses these four serverOverloaded delays. Keep both
# policies bounded and local to one admitted Desk turn; a process restart never
# reconstructs or replays either loop. Routing gets one retry because the native
# client already exhausted its own reconnects before reporting this terminal.
CAPACITY_RETRY_DELAYS = (10, 30, 120, 300)
WORKSPACE_ROUTING_RETRY_DELAYS = (2,)
NODE_REPL_HARD_TIMEOUT_MS = 10000


def _codex_error_info(error):
    if not isinstance(error, dict):
        return None
    return error.get('codexErrorInfo', error.get('codex_error_info'))


def _server_overloaded_error(error):
    """Use Codex's typed reason, never English message matching, as authority."""
    info = _codex_error_info(error)
    return info == 'serverOverloaded' or (isinstance(info, dict) and (
            'serverOverloaded' in info or 'server_overloaded' in info
            or info.get('type') == 'serverOverloaded'))


def _workspace_routing_error(error):
    """Recognize only Codex's exact remote routing terminal, not local path errors."""
    if not isinstance(error, dict):
        return False
    message = str(error.get('message') or '').strip().rstrip('.').casefold()
    info = _codex_error_info(error)
    return info == 'other' and message == 'workspace routing discovery failed'


def _retryable_terminal_event(method, params):
    if method not in ('error', 'turn/failed', 'turn/completed'):
        return False
    params = params if isinstance(params, dict) else {}
    turn = params.get('turn') if isinstance(params.get('turn'), dict) else {}
    error = turn.get('error') if isinstance(turn.get('error'), dict) else params.get('error')
    return _server_overloaded_error(error) or _workspace_routing_error(error)


def _server_overloaded_event(method, params):
    if not _retryable_terminal_event(method, params):
        return False
    params = params if isinstance(params, dict) else {}
    turn = params.get('turn') if isinstance(params.get('turn'), dict) else {}
    error = turn.get('error') if isinstance(turn.get('error'), dict) else params.get('error')
    return _server_overloaded_error(error)


def _retry_error_diagnostic(error):
    """Keep bounded, credential-redacted native fields for durable diagnostics."""
    from error_notice import safe_text
    error = error if isinstance(error, dict) else {}
    out = {'message': safe_text(error.get('message'), 1000)}
    info = _codex_error_info(error)
    if isinstance(info, str):
        out['codexErrorInfo'] = safe_text(info, 500)
    elif isinstance(info, (int, float, bool)) or info is None:
        out['codexErrorInfo'] = info
    elif isinstance(info, dict):
        cleaned = {}
        for key, value in info.items():
            if not (isinstance(value, (str, int, float, bool)) or value is None):
                continue
            safe_key = safe_text(key, 100)
            sensitive = re.search(
                r'(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)',
                safe_key)
            # Sensitive keys are redacted regardless of value type. For other
            # strings include the key so a bare secret cannot evade key=value patterns.
            if sensitive:
                value = '[redacted]'
            elif isinstance(value, str):
                value = safe_text(f'{safe_key}={value}', 600).partition('=')[2]
            cleaned[safe_key] = value
        out['codexErrorInfo'] = cleaned
    detail = error.get('additionalDetails', error.get('additional_details'))
    if detail:
        out['additionalDetails'] = safe_text(detail, 2000)
    return out


def _capacity_semantic_event(method, params=None):
    """Assistant output or executable work makes an automatic replay unsafe."""
    params = params if isinstance(params, dict) else {}
    # Raw response items are archival duplicates and may replay injected
    # system/developer/user/tool history before this native turn starts. They
    # cannot prove current-turn model output or side effects.
    if method.startswith('rawResponseItem/'):
        return False
    if method.startswith('item/'):
        item = params.get('item') if isinstance(params.get('item'), dict) else {}
        kind = str(item.get('type') or '')
        # Codex may project the already-admitted user input as a turn item. It
        # is evidence of dispatch, not model progress or a replayable side effect.
        if kind in ('userMessage', 'user_message') or (
                kind == 'message' and item.get('role') == 'user'):
            return False
        return True
    return (method in ('applyPatchApproval', 'execCommandApproval', 'model/rerouted')
            or (method.startswith('turn/') and method not in (
                'turn/started', 'turn/completed', 'turn/failed')))


def _turn_semantic_event(method, params=None):
    """Return whether one current-turn frame advances model/tool semantics."""
    params = params if isinstance(params, dict) else {}
    if method in (
            'item/agentMessage/delta', 'item/reasoning/summaryTextDelta',
            'item/reasoning/textDelta', 'item/commandExecution/outputDelta',
            'item/fileChange/outputDelta'):
        return bool(str(params.get('delta') or ''))
    # rawResponseItem/completed is an archival duplicate stream. It can replay
    # injected developer/user/assistant/tool history, so it cannot prove that
    # the current turn advanced; current-turn item/* lifecycle events do.
    if method not in ('item/started', 'item/completed'):
        return False
    item = params.get('item') if isinstance(params.get('item'), dict) else {}
    kind = str(item.get('type') or '')
    def populated(value):
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, list):
            return any(populated(entry) for entry in value)
        if isinstance(value, dict):
            return any(populated(value.get(key)) for key in (
                'text', 'content', 'summary', 'items', 'output_text', 'input_text',
                'encryptedContent', 'encrypted_content'))
        return False
    item_id = item.get('id') or item.get('itemId') or item.get('item_id')
    tools = (
        'commandExecution', 'fileChange', 'mcpToolCall', 'dynamicToolCall',
        'webSearch', 'collabAgentToolCall', 'subAgentActivity')
    if kind in tools:
        # A known, identified tool item's start/completion is an execution boundary.
        return bool(item_id)
    if method == 'item/started':
        return False
    if kind == 'contextCompaction':
        return bool(item_id)
    if kind == 'message' and item.get('role') != 'assistant':
        return False
    if kind not in ('agentMessage', 'reasoning', 'message'):
        return False
    return any(populated(item.get(key)) for key in (
        'text', 'content', 'summary', 'items', 'encryptedContent', 'encrypted_content'))


def _capacity_wait(delay, cancelled, deadline):
    deadline = float(deadline)
    end = min(deadline, time.monotonic() + max(0.0, float(delay)))
    while time.monotonic() < end:
        if cancelled and cancelled():
            return False
        time.sleep(min(.1, max(0.0, end - time.monotonic())))
    return time.monotonic() < deadline and not (cancelled and cancelled())


def _log(msg):
    """写一行进程日志。deskcfg 在这里只按需引入,避免与它的引擎配置形成回环。"""
    try:
        import deskcfg
        deskcfg.log("engine", msg)
    except Exception:
        pass


def _node_repl_deadline(item, now=None):
    """返回 node_repl/js 的端到端截止时刻；其他工具不设额外限制。

    node_repl 自带的 timeout_ms 只覆盖 JS kernel 执行。插件发现、原生管道连接与
    elicitation 授权发生在它之前，过去会绕过该计时器并占住整个回合。Desk 在收到
    item/started 时从最外层再计一次，既尊重调用方更短的 timeout_ms，也把无显式超时
    的调用限制在十秒内。
    """
    if not isinstance(item, dict) or item.get("type") != "mcpToolCall":
        return None
    server = str(item.get("server") or "").lower().replace("-", "_")
    tool = str(item.get("tool") or "").lower()
    if server != "node_repl" or tool != "js":
        return None
    args = item.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    args = args if isinstance(args, dict) else {}
    try:
        requested = int(args.get("timeout_ms") or NODE_REPL_HARD_TIMEOUT_MS)
    except (TypeError, ValueError):
        requested = NODE_REPL_HARD_TIMEOUT_MS
    timeout_ms = max(100, min(requested, NODE_REPL_HARD_TIMEOUT_MS))
    return (time.monotonic() if now is None else now) + timeout_ms / 1000.0


def _node_repl_process_groups(root_pid):
    """找出当前 app-server 后代里另起 session 的 node_repl/kernel 进程组。

    Codex 启 MCP 时 node_repl 会 start_new_session；只 kill app-server 的 pgid 会漏掉它，
    随后 kernel.js 被 launchd 收养成 PPID 1。这里只沿 root_pid 的后代链取带明确命令标记
    的进程组，不碰别的 Desk 会话或用户进程。
    """
    try:
        raw = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid=,command="], capture_output=True,
            text=True, timeout=2).stdout.splitlines()
    except Exception:
        return set()
    rows = []
    for line in raw:
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
        except ValueError:
            continue
    descendants = {int(root_pid)}
    changed = True
    while changed:
        changed = False
        for pid, ppid, _pgid, _command in rows:
            if ppid in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return {pgid for pid, _ppid, pgid, command in rows
            if pid in descendants and pgid > 1
            and ("/node_repl" in command or "/kernel.js" in command)}


# app-server 的启动参数按二进制分。
_APPSERVER_ARGV = {
    "codex": ("app-server", "--stdio"),
}


def _compact_limit(opts, binary="codex"):
    """把会话档位收窄成可安全拼进 -c 的整数；只给已验证的同协议引擎。"""
    binary = os.path.basename(str(binary or ''))
    if binary != "codex":
        return 0
    try:
        value = int(str((opts or {}).get("autocompact") or "").strip())
    except ValueError:
        return 0
    return value if value in (200000, 400000, 600000) else 0


def _appserver_argv(binary, opts=None):
    engine = os.path.basename(str(binary or ""))
    no_tools = bool((opts or {}).get("_no_tools"))
    # --search is a global Codex flag and must precede the app-server
    # subcommand. Toolless recap/peer branches stay fail-closed below.
    native_search = not no_tools and engine == "codex"
    out = [binary, *(["--search"] if native_search else []),
           *_APPSERVER_ARGV.get(engine, _APPSERVER_ARGV["codex"])]
    if no_tools:
        # Peer prompts are untrusted context, not user authorization. Remove
        # every execution surface before initialize; interrupting item/started
        # would already be too late for commands or MCP calls.
        disabled = ["shell_tool", "apps", "browser_use", "computer_use",
                    "multi_agent", "hooks", "plugins", "image_generation",
                    "tool_suggest", "web_search_cached", "web_search_request",
                    "artifact", "code_mode", "code_mode_host", "enable_mcp_apps",
                    "js_repl", "multi_agent_v2", "request_permissions_tool",
                    "search_tool", "tool_search", "unified_exec", "memories"]
        if engine == "codex":
            disabled += ["view_image", "skill_search", "sleep_tool",
                         "current_time_reminder", "deferred_executor",
                         "in_app_local_automation", "psp", "standalone_web_search",
                         "unified_exec_tty", "external_agent_memory_import"]
        for feature in disabled:
            out += ["--disable", feature]
        # Codex 0.154 does not advertise this feature yet and rejects an
        # unknown `--disable`, while its typed feature map accepts an explicit
        # false entry. Future versions therefore inherit the same closed value.
        out += ["-c", "features.memory_self_evolution=false"]
        out += ["-c", 'web_search="disabled"',
                "-c", "orchestrator.mcp.enabled=false"]
    limit = _compact_limit(opts, engine)
    if limit:
        # 这是官方 config.toml 键的进程级覆盖。Desk 每段会话独占一条 app-server 连接,
        # 因而能做到会话级窗口,不改用户的全局 CODEX_HOME/config.toml。
        out += ["-c", f"model_auto_compact_token_limit={limit}"]
    if (opts or {}).get('_working_context_export'):
        out += ['-c','features.memories=false','-c','features.memory_self_evolution=false']
    return out


#: 审批请求的 decision 取值以请求自带的 availableDecisions 为准,不硬编码——
#: 版本之间枚举会变。实测 codex 0.5x 给的是 ["accept", {...}, "cancel"];列表里
#: 还有对象形态的选项(如 acceptWithExecpolicyAmendment,附带把命令加进白名单),那属于
#: 「批准并放宽策略」,不是这里要替人做的决定,只取纯字符串那几个。
_YES = ("accept", "approve", "allow", "approved")
_NO = ("cancel", "decline", "deny", "denied", "reject", "abort")


def pick_decision(params, allow):
    opts = [x for x in (params or {}).get("availableDecisions") or [] if isinstance(x, str)]
    want = _YES if allow else _NO
    for x in opts:
        if x.lower() in want:
            return x
    return "accept" if allow else "cancel"


class _Connection:
    def __init__(self, cwd=None, env=None, binary="codex", opts=None, on_process=None,
                 writer_lock_root=None):
        # 自成进程组。`codex` 是一层 node shim,真正干活的是它 spawn 的平台二进制;
        # terminate 只发给 shim,那个二进制会被 launchd 收养继续跑,并且一直攥着 rollout——
        # 于是同一段会话再也发不出消息,报错还写着「正在别处使用(终端那侧)」,把人引向别处。
        # 2026-08-10 实见一个 ppid=1 的孤儿占着 Desk Opt 的记录,而 ui_server 一直活着。
        # 独立成组之后,close 可以按组收,一个都不落。
        argv = _appserver_argv(binary, opts)
        # Every Desk-owned app-server is supervised. If the backend is SIGKILLed,
        # the supervisor kills the whole writer group instead of leaving a native
        # thread lock behind. Shepherd additionally persists exact PID evidence.
        from owned_process import command as owned_command, writer_lock_path
        session_id = str((env or {}).get('DESKTRON_NATIVE_WRITER_SESSION_ID') or '')
        lock_path = ''
        if session_id:
            data_home = str(writer_lock_root or (env or {}).get('DESKTRON_DATA_HOME') or '')
            if not data_home:
                try:
                    from deskcfg import DATA_HOME
                    data_home = DATA_HOME
                except Exception:
                    data_home = os.path.expanduser('~/.local/share/desktron/data')
            lock_path = writer_lock_path(
                os.path.join(data_home, 'adapter', os.path.basename(str(binary))), session_id)
        argv = owned_command(argv, lock_path=lock_path)
        self.proc = subprocess.Popen(
            argv, cwd=cwd, env=env, text=True, encoding='utf-8', bufsize=1,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        self._selector = None
        self._stdout_queue = None
        self._stdout_thread = None
        self._stdout_stopped = threading.Event()
        self._stdout_eof = False
        self._stdout_failed = False
        try:
            if callable(on_process):
                on_process(self.proc, 'active')
            self._next_id = 1
            self._stderr = collections.deque(maxlen=80)
            self._read_buffer = bytearray()
            if oscompat.IS_WINDOWS:
                # Windows select() only accepts sockets, not subprocess pipes.
                # Bound buffering and keep timeout/framing in the calling thread.
                self._stdout_queue = queue.Queue(maxsize=8)
                self._stdout_thread = threading.Thread(target=self._drain_stdout, daemon=True)
                self._stdout_thread.start()
            else:
                self._selector = selectors.DefaultSelector()
                self._selector.register(self.proc.stdout, selectors.EVENT_READ)
            # 审批接管:适配器注入 hook(msg, conn) -> bool。返回 True 表示这条服务端请求由它
            # 接手(发去界面等人答),连接层就不再当场拒绝。_awaiting 是还挂着几条——挂着的时候
            # 服务端不会再发任何帧,静默上限与总时限都得放宽,否则等人的那几分钟会被当成卡死。
            threading.Thread(target=self._drain_stderr, daemon=True).start()
        except Exception:
            try:
                self.close()
            finally:
                if callable(on_process):
                    try: on_process(self.proc, 'stopped')
                    except Exception: pass
            raise

    def _drain_stdout(self):
        def publish(chunk):
            while not self._stdout_stopped.is_set():
                try:
                    self._stdout_queue.put(chunk, timeout=.1)
                    return True
                except queue.Full:
                    pass
            return False

        try:
            while not self._stdout_stopped.is_set():
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not publish(chunk) or not chunk:
                    return
        except (OSError, ValueError) as error:
            publish(error)

    def _read_chunk(self, timeout):
        if self._stdout_queue is not None:
            if self._stdout_stopped.is_set():
                return b''
            if self._stdout_failed:
                raise AppServerError('app-server 输出通道读取失败，未收到完整终态')
            if self._stdout_eof:
                return b''
            try:
                chunk = self._stdout_queue.get(timeout=timeout)
            except queue.Empty:
                return None
            if isinstance(chunk, Exception):
                self._stdout_failed = True
                raise AppServerError('app-server 输出通道读取失败，未收到完整终态') from None
            if not chunk:
                self._stdout_eof = True
            return chunk
        if not self._selector.select(timeout):
            return None
        return os.read(self.proc.stdout.fileno(), 65536)

    def _drain_stderr(self):
        try:
            for line in self.proc.stderr:
                self._stderr.append(line.rstrip("\n"))
        except Exception:
            pass

    _write_lock = None
    # 类级默认:这两样也要在没走 __init__ 的替身上成立(测试里的伪连接就是这么用的)。
    approval_hook = None   # 适配器注入 hook(msg, conn) -> bool;接手了就返回 True
    _awaiting = 0          # 还挂着几条待人拍板的审批

    def _send(self, obj):
        if self._write_lock is None:
            self._write_lock = threading.Lock()
        try:
            with self._write_lock:   # 中断从另一个线程发,与跑回合的那个线程共用这条 stdin
                self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
        except Exception as exc:
            raise AppServerError(f"app-server 写入失败：{exc}") from exc

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request_nowait(self, method, params=None):
        """发一条请求就走,不等应答。

        中断要从另一个线程发,而这条连接此刻正被跑回合的那个线程读着——在这里等应答会跟它抢读。
        它那侧的读循环对「有 id、没有 method、且 id 不是它等的那个」一律丢弃,
        所以这条应答被安全地忽略掉。回合停没停,由后续帧说了算。"""
        rid = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        return rid

    def read(self, timeout=30):
        # POSIX selector / Windows 字节队列都不混用 TextIOWrapper 预读。
        # 同批到达的通知和响应曾使第二帧在文本缓冲区里等到超时；readline 遇半行
        # 还会越过 select 的时限。统一用 fd 字节读取和显式行缓冲，不混用两套缓冲。
        deadline = time.monotonic() + timeout
        searched = 0
        while True:
            end = self._read_buffer.find(b"\n", searched)
            if end >= 0:
                break
            searched = len(self._read_buffer)
            left = deadline - time.monotonic()
            chunk = self._read_chunk(left) if left > 0 else None
            if chunk is None:
                raise AppServerTimeout(f"app-server {timeout:g} 秒内没有响应")
            if not chunk:
                # stderr is process-scoped, not proof of why this turn ended.
                # Never attach old tool logs (or raw credentials) as the cause.
                error = AppServerError("app-server 输出通道已关闭，未收到完整终态")
                error.code = 'NATIVE_EOF'
                error.returncode = self.proc.poll()
                raise error
            self._read_buffer.extend(chunk)
        line = bytes(self._read_buffer[:end])
        del self._read_buffer[:end + 1]
        try:
            return json.loads(line)
        except (ValueError, UnicodeDecodeError) as exc:
            raise AppServerError("app-server 返回了无效 JSON 帧") from exc

    # 等人审批时能等多久。适配器那侧有自己的兜底定时器(到点自动拒绝),这里只要比它宽,
    # 免得连接层先一步把「在等人」当成「服务端卡死」。
    APPROVAL_WAIT = 600

    def take_approval(self, msg):
        """服务端请求先给适配器过一手。它接手就返回 True,连接层不再应答。"""
        hook = self.approval_hook
        if not hook:
            return False
        try:
            if not hook(msg, self):
                return False
        except Exception:
            return False
        self._awaiting += 1
        return True

    def answer_approval(self, msg_id, result):
        """人答完了,把结果回给服务端。从另一个线程调,_send 自带写锁。"""
        self._send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        self._awaiting = max(0, self._awaiting - 1)

    def handle_server_request(self, msg):
        """服务端请求的统一处置:先问接管方,没人接就按协议拒绝。

        返回 True 表示已接管——这条请求挂着等人答,调用方要把静默判据放宽。
        连接上有两条读循环(request() 等应答那条、run_turn 跑回合那条),审批请求两边都会
        落,所以处置收在这一个方法里。2026-08-16 只改了前者,回合里的审批照旧被当场拒绝,
        表现是服务端停在 waitingOnApproval 而 Desk 这侧静默到超时。"""
        if self.take_approval(msg):
            return True
        self._deny_server_request(msg)
        return False

    def _deny_server_request(self, msg):
        """没有接管方时的兜底:按协议拒绝或返回无权限,不能让回合静默挂住。

        拒绝的字面量以请求自带的 availableDecisions 为准(实测 2026-08-16:
        item/commandExecution/requestApproval 给的是 ["accept", {...}, "cancel"]),
        此前硬写的 decline 不在其中,服务端不认——命令直接判失败,而不是被拒。"""
        method = msg.get("method") or ""
        if method == "item/tool/requestUserInput":
            result = {"answers": {}}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline"}
        elif method == "item/tool/call":
            result = {"success": False, "contentItems": [{
                "type": "inputText", "text": "Desktron GUI 未注册这个动态工具。"}]}
        elif method == "item/permissions/requestApproval":
            # PermissionsRequestApprovalResponse 没有 decision 字段。空 profile 表示不授予
            # 额外权限；旧的 {decision: denied} 不符合 schema，app-server 会继续等待。
            result = {"permissions": {}}
        elif method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            result = {"decision": pick_decision(msg.get("params") or {}, False)}
        elif method in ("applyPatchApproval", "execCommandApproval"):
            # 旧协议的 ReviewDecision 与 v2 的 decline 不是同一个枚举。
            result = {"decision": {"denied": {
                "rejection": "Desktron GUI 当前没有审批交互。"}}}
        elif method == "currentTime/read":
            result = {"currentTimeAt": int(time.time())}
        else:
            self._send({"jsonrpc": "2.0", "id": msg.get("id"), "error": {
                "code": -32601, "message": f"Desktron GUI 不支持服务端请求：{method}"}})
            return
        self._send({"jsonrpc": "2.0", "id": msg.get("id"), "result": result})

    # 一条请求最多等多久(秒)。timeout 是「两帧之间的静默上限」,只要期间有任何通知进来
    # 就重新计时;缺了这个总时限,一个永远不被应答的请求可以无限等下去——2026-08-10 实见:
    # app-server 挂在一条到模型 API 的连接上八分半没有响应,而 Desk 这侧不报错、不收尾,
    # 回合的 _live 一直不出栈,侧栏就一直闪,日志里一个字都没有。
    OVERALL = 300

    def request(self, method, params=None, on_notification=None, timeout=30, overall=None):
        rid = self._next_id
        self._next_id += 1
        end = time.time() + (overall or self.OVERALL)
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        while True:
            left = end - time.time()
            if left <= 0:
                raise AppServerError(
                    f"app-server 的 {method} 请求 {int(overall or self.OVERALL)} 秒未被应答")
            # 挂着待人审批时,服务端在等我们、不会再发帧:静默上限放到审批那档,
            # 否则「人还没点」会被读成「服务端没响应」,回合当场报错收尾。
            quiet = max(timeout, self.APPROVAL_WAIT) if self._awaiting else timeout
            msg = self.read(min(quiet, left))
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                if msg.get("error"):
                    err = msg["error"]
                    raise AppServerError(str(err.get("message") if isinstance(err, dict) else err))
                return msg.get("result") or {}
            if "id" in msg and msg.get("method"):
                if self.handle_server_request(msg):
                    end += self.APPROVAL_WAIT   # 等人的这段不算进总时限
                    continue
            elif msg.get("method") and on_notification:
                on_notification(msg.get("method"), msg.get("params") or {})

    def initialize(self, on_notification=None, overall=None):
        self.request("initialize", {
            "clientInfo": {"name": "desktron", "title": "Desktron", "version": "1"},
            "capabilities": {"experimentalApi": True},
        }, on_notification, overall=overall)
        self.notify("initialized")

    def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        stopped = getattr(self, '_stdout_stopped', None)
        if stopped is not None:
            stopped.set()
        output = getattr(self, '_stdout_queue', None)
        if output is not None:
            # Wake a concurrent read() immediately, even if its protocol wait
            # is long (e.g. pending approval). A full queue has no blocked getter.
            try:
                output.put_nowait(b'')
            except queue.Full:
                pass
        try:
            self._close_process_tree()
        finally:
            reader = getattr(self, '_stdout_thread', None)
            if reader is not None and reader.is_alive() and reader is not threading.current_thread():
                reader.join(timeout=2)
            selector = getattr(self, '_selector', None)
            if selector is not None:
                selector.close()
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass

    def _close_process_tree(self):
        """收掉整棵进程树。

        只 terminate 顶上那个 node shim 是不够的:真正干活的平台二进制是它的子进程,
        shim 一死它就成孤儿,还攥着 rollout 不放。按进程组发信号才收得干净。
        另外不能拿 poll() 当「还活着吗」的判据——宿主装了 SIGCHLD 收割器
        (termpty 的 os.waitpid(-1)),状态被抢走之后 poll() 的返回值两个方向都不可信。
        直接发,发不动(进程真没了)就当已经收完。"""
        pid = self.proc.pid
        detached_node_groups = _node_repl_process_groups(pid)
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.proc.terminate()
            except Exception:
                pass
        # node_repl 自己另起了进程组，不属于 app-server pgid。快照后立即发信号，避免它先
        # 变成孤儿，也避免等待期间旧 pgid 被系统复用；目标只来自当前连接的后代。
        for pgid in detached_node_groups:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            self.proc.wait(timeout=2)
        except Exception:
            pass
        try:                      # 还有活口就补一刀,别留孤儿
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        if detached_node_groups:
            time.sleep(0.1)
        for pgid in detached_node_groups:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        # 等它真的没了再走。thread 的写入权是文件锁(~/.codex/thread-writer-locks/),
        # 进程活着锁就不放;而换连接时(改模型、改沙箱都会换 key)紧接着就要 resume 同一条
        # thread,旧进程还没咽气的话,新连接撞上「already has an active writer」。
        # 补一刀之后不等,等于把这个竞态留在那里。
        for _ in range(30):        # 至多 3 秒,按 100ms 探
            try:
                os.kill(pid, 0)
            except OSError:
                return             # 真没了
            time.sleep(0.1)


class _SessionConnection:
    def __init__(self, conn, key, *, context_key=None, through_seq=None,
                 recap_on_evict=True, on_drop=None, owner=None):
        self.conn = conn
        self.key = key
        self.context_key = context_key
        self.through_seq = through_seq
        self.recap_on_evict = bool(recap_on_evict)
        self.on_drop = on_drop
        self.owner = owner
        self.thread_id = ""
        self.at = time.time()
        self.busy = False
        self.turn_id = ""      # 正在跑的那一回合。中断要 threadId + turnId 两个值,
        self.interrupt_requested = False
                               # 它们原来只活在 run_turn 的局部变量里,外面取不到。


_SESSIONS = {}
# 在飞的回合与挂起的中断,都按 Desk 会话 id 记在这里。挂在连接对象上不够:
# 按 Esc 时连接可能还没建出来(冷起、initialize、thread/resume 都在回合之内),
# 那一段时间里 _SESSIONS 里根本没有这一条,中断就无处可记。
_INFLIGHT = set()
_PENDING_INTERRUPT = set()
_SESSION_LOCKS = {}
_SESSIONS_LOCK = threading.Lock()
_SESSION_IDLE = 1800
_SESSION_MAX = max(1, int(os.environ.get("DESKTRON_CODEX_APP_SERVERS", "6") or 6))


def _session_key(cwd, env, opts, system_text, runtime_roots, binary="codex"):
    """会影响 app-server 或 thread 绑定的配置变化时，重建连接并 resume 一次。"""
    env_blob = json.dumps(sorted((str(k), str(v)) for k, v in (env or {}).items()),
                          ensure_ascii=False, separators=(",", ":"))
    env_digest = hashlib.sha256(env_blob.encode("utf-8")).hexdigest()
    # Model / effort / collaboration mode are turn/start settings. Including them
    # here would rebuild app-server and resume the same thread with a different
    # model, which makes Codex emit the native "recorded with model ..." warning.
    # Sandbox still belongs to the thread binding, so keep it in the cache key.
    bind_opts = {"sandbox": (opts or {}).get("sandbox"),
                 "autocompact": _compact_limit(opts, binary)}
    return (
        binary, os.path.realpath(cwd or ""), env_digest,
        json.dumps(bind_opts, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        system_text or "", tuple(str(x) for x in (runtime_roots or []) if str(x)),
    )


def _session_lock(sid):
    with _SESSIONS_LOCK:
        return _SESSION_LOCKS.setdefault(sid, threading.Lock())


def live_session_ids():
    """池子里还活着的 Desk 会话 id。侧栏据此判断「这段会话握着一条常驻连接」。"""
    with _SESSIONS_LOCK:
        return {sid for sid, e in _SESSIONS.items() if e.conn.proc.poll() is None}


def drop_session(sid, expected=None):
    """释放一条 Desk 会话的 app-server；expected 避免误关刚替换的新连接。"""
    with _SESSIONS_LOCK:
        current = _SESSIONS.get(sid)
        if current is None or (expected is not None and current is not expected):
            return
        _SESSIONS.pop(sid, None)
    try:
        current.conn.close()
    finally:
        if current.on_drop:
            try:
                current.on_drop()
            except Exception:
                pass


def drop_session_connection(sid, conn):
    """Drop only the resident entry still owning *conn*."""
    with _SESSIONS_LOCK:
        current = _SESSIONS.get(sid)
    if current is None or current.conn is not conn:
        return False
    drop_session(sid, current)
    return True


def commit_session(sid, conn, through_seq):
    """Advance a resident projected thread only after its archive commit."""
    with _SESSIONS_LOCK:
        current = _SESSIONS.get(sid)
        if (current is None or current.conn is not conn or current.busy
                or current.conn.proc.poll() is not None):
            return False
        current.through_seq = int(through_seq)
        current.at = time.time()
        return True


def evict_session(sid, expected=None):
    """仅用于资源回收：同步完成 recap 后才释放 app-server。"""
    guard = _session_lock(sid)
    if not guard.acquire(blocking=False):
        return False
    try:
        with _SESSIONS_LOCK:
            current = _SESSIONS.get(sid)
            if current is None or current.busy or (expected is not None and current is not expected):
                return False
        if current.recap_on_evict:
            try:
                from engine_adapters.recap import recap_before_evict
                if not recap_before_evict(sid, getattr(current, 'owner', None)):
                    return False
            except Exception:
                return False
        with _SESSIONS_LOCK:
            current = _SESSIONS.get(sid)
            if current is None or current.busy or (expected is not None and current is not expected):
                return False
            _SESSIONS.pop(sid, None)
        try:
            current.conn.close()
        finally:
            if current.on_drop:
                try:
                    current.on_drop()
                except Exception:
                    pass
        return True
    finally:
        guard.release()


def writer_probe(thread_id="", home=""):
    """撞上「active writer」时的现场取证。

    这个报错到目前为止没能只凭推断收敛:锁目录里查不到对应的锁文件,进程列表里也没有
    别的 TUI,而报错照样发生。与其继续猜,不如在撞上的那一刻把三样东西记下来——
    我们自己池子里的连接、锁目录的持有者、命令行里带着这条 thread 的进程。
    只在出错路径上跑,平时不产生任何开销。"""
    import subprocess
    lines = []
    with _SESSIONS_LOCK:
        rows = [(sid, e.thread_id, e.conn.proc.pid, e.busy, e.conn.proc.poll())
                for sid, e in _SESSIONS.items()]
    for sid, tid, pid, busy, dead in rows:
        lines.append(f"  池 {sid[:20]} thread={tid[:13]} pid={pid} busy={busy} 退出码={dead}")
    home = os.path.expanduser(home or os.environ.get("CODEX_HOME") or "~/.codex")
    lockdir = os.path.join(home, "thread-writer-locks")
    try:
        out = subprocess.run(["lsof", "+D", lockdir], capture_output=True,
                             text=True, timeout=5).stdout.strip().splitlines()[1:]
        for ln in out[:12]:
            lines.append("  锁 " + " ".join(ln.split()[:2]) + " " + ln.split()[-1].split("/")[-1])
    except Exception as exc:
        lines.append(f"  锁 查询失败:{str(exc)[:60]}")
    if thread_id:
        try:
            out = subprocess.run(["pgrep", "-af", thread_id], capture_output=True,
                                 text=True, timeout=5).stdout.strip().splitlines()
            for ln in out[:6]:
                lines.append("  进程 " + ln[:120])
        except Exception:
            pass
    return "\n".join(lines) or "  (无)"


def interrupt(session_id):
    """请 app-server 停掉这条会话正在跑的那一回合。

    协议里有 turn/interrupt(参数 threadId + turnId,取自 codex app-server 的 schema)。
    原来中止走的是杀进程,而 ctx["proc"] 指的是 app-server 本身——杀它既不精确
    (整条连接一起没了、下一回合要冷起),实测也停不下那一回合:活在 server 那侧继续跑。
    停不下来时返回 False,由调用方退回杀进程,打断不该有「按了没反应」这一档。
    """
    with _SESSIONS_LOCK:
        entry = _SESSIONS.get(session_id)
        inflight = session_id in _INFLIGHT
        if inflight and (entry is None or not entry.thread_id or not entry.turn_id):
            # 回合在飞,但还拿不到 turnId:可能连接刚冷起、可能 thread/resume 还没回、
            # 也可能 turn/start 刚发出去。这三种都不能返回 False——调用方会退回杀进程,
            # 而进程是整条常驻 app-server,不是这一回合。杀了它,回合仍活在别处,
            # 而 thread 的写入权(文件锁)要等进程真的死透才放,下一条消息就撞上
            # 「already has an active writer」,整段会话跟着退出。记账,等 turnId 一到补发。
            _PENDING_INTERRUPT.add(session_id)
            return True
    if entry is None or not entry.thread_id or not entry.turn_id:
        return False
    if entry.conn.proc.poll() is not None:
        return False
    try:
        # 不等回应:这条请求要和正在读回合帧的那个线程共用同一条连接,
        # 在这里等响应会跟它抢读。发出去即可,回合停没停由后续帧说了算。
        entry.interrupt_requested = True
        entry.conn.request_nowait("turn/interrupt",
                                  {"threadId": entry.thread_id, "turnId": entry.turn_id})
        return True
    except Exception:
        return False


def _session_roster():
    """报给内存预算那边:这一池有哪些常驻项(见 resident)。

    pid 取 app-server 那条 stdio 进程,它下面还挂着原生二进制、code-mode helper 与
    node_repl 三个子进程——真正占地方的是它们,所以那边按进程树算。
    """
    with _SESSIONS_LOCK:
        rows = list(_SESSIONS.items())
    out = []
    for sid, e in rows:
        try:
            out.append((sid, e.conn.proc.pid, e.busy, e.at))
        except Exception:
            continue
    return out


try:
    import resident as _resident
    _resident.add_pool("codex", _session_roster, evict_session)
except Exception:
    pass


def _trim_sessions(keep_sid=""):
    """腾位置。先按内存跨引擎结一次账,再按个数收自己这一池(理由见 resident 的模块注释)。"""
    try:
        import resident
        resident.make_room("codex", keep_sid)
    except Exception:
        pass
    while True:
        with _SESSIONS_LOCK:
            if len(_SESSIONS) < _SESSION_MAX:
                return
            idle = [(entry.at, sid) for sid, entry in _SESSIONS.items()
                    if not entry.busy and sid != keep_sid]
        if not idle:
            return
        if not evict_session(min(idle)[1]):
            return


def _get_session(sid, key, cwd, env, requested_thread="", on_create=None, binary="codex",
                 opts=None, context_key=None, through_seq=None, recap_on_evict=True,
                 on_drop=None, writer_lock_root=None, owner=None):
    with _SESSIONS_LOCK:
        entry = _SESSIONS.get(sid)
        invalid = bool(entry and (
            entry.conn.proc.poll() is not None or entry.key != key
            or (context_key is not None and entry.context_key != context_key)
            or (through_seq is not None and entry.through_seq != through_seq)
            or (requested_thread and entry.thread_id and requested_thread != entry.thread_id)))
    if invalid:
        drop_session(sid, entry)
        entry = None
    if entry is None:
        _trim_sessions(sid)
        if on_create:
            on_create()
        writer_env = dict(env or {})
        writer_env['DESKTRON_NATIVE_WRITER_SESSION_ID'] = sid
        conn_args = {"cwd": cwd, "env": writer_env, "binary": binary}
        if writer_lock_root:
            conn_args['writer_lock_root'] = writer_lock_root
        if _compact_limit(opts, binary) or (opts or {}).get('_working_context_export'):
            conn_args["opts"] = opts
        entry = _SessionConnection(_Connection(**conn_args), key,
            context_key=context_key, through_seq=through_seq,
            recap_on_evict=recap_on_evict, on_drop=on_drop, owner=owner)
        with _SESSIONS_LOCK:
            _SESSIONS[sid] = entry
        return entry, True
    return entry, False


def _reap_sessions():
    while True:
        time.sleep(60)
        now = time.time()
        with _SESSIONS_LOCK:
            stale = [(sid, entry) for sid, entry in _SESSIONS.items()
                     if not entry.busy and (
                         entry.conn.proc.poll() is not None or now - entry.at > _SESSION_IDLE)]
        for sid, entry in stale:
            evict_session(sid, entry)


def _drop_all_sessions():
    with _SESSIONS_LOCK:
        rows = list(_SESSIONS.items())
    for sid, entry in rows:
        drop_session(sid, entry)


threading.Thread(target=_reap_sessions, daemon=True).start()
atexit.register(_drop_all_sessions)


# 模型目录:内存缓存 + 落盘。拉一次要起一个 app-server 进程再问两轮 RPC,实测 5.2 秒
# (2026-08-11,对比 claude 的 config_label 恒定 20ms)。而 /config 是切会话必经的一站,
# 只要缓存过期,那 5 秒就整个压在切换上——「切 codex 比切 cc 慢」就是这么来的。
# 目录的内容(有哪些模型、各自支持哪些 effort、有哪些协作模式)按版本变,不按分钟变,
# 所以 TTL 放到一天,并落盘让重启后也能直接用。
# 按二进制分开存,不同二进制的模型清单互不覆盖。
_CATALOGS = {}          # binary → {"at": float, "value": dict}
_CATALOG_LOCK = threading.Lock()
_CATALOG_TTL = 86400.0
_CATALOG_FETCHING = set()   # 正在后台拉的二进制


def _catalog_slot(binary):
    return _CATALOGS.setdefault(binary, {"at": 0.0, "value": None})


def _catalog_cache_path(binary="codex"):
    try:
        import deskcfg
        base = os.path.join(deskcfg.REPO_ROOT, ".workbench")
    except Exception:
        base = os.path.expanduser("~/.cache/desktron")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        return ""
    return os.path.join(base, f"{binary}-catalog.json")


def _catalog_load_disk(binary="codex"):
    path = _catalog_cache_path(binary)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(snap, dict) or not isinstance(snap.get("value"), dict):
        return None
    return {**snap, "value": snap["value"]}


def _catalog_save_disk(at, value, binary="codex"):
    path = _catalog_cache_path(binary)
    if not path:
        return
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"at": at, "value": value}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def fork_thread(thread_id, cwd=None, env=None, binary="codex"):
    """从一条 thread 另开一支,返回新 thread 的 id。拿不到返回空串。

    协议里的 thread/fork 只要 threadId,返回整个新 thread。用一条短连接发:
    源会话那条池化连接可能正忙着跑回合,借它发等于跟读回合的线程抢读。
    """
    thread_id = str(thread_id or "").strip()
    if not thread_id:
        return ""
    conn = None
    try:
        conn = _Connection(cwd=cwd, env=env, binary=binary)
        conn.initialize()
        out = conn.request("thread/fork", {"threadId": thread_id}, overall=60)
        return str(((out or {}).get("thread") or {}).get("id") or "")
    except Exception:
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


class HistoryProjectionError(AppServerError):
    """分页索引可能只覆盖 rollout 前缀，禁止用它确认撤回成功。"""


def _validate_recall_history(thread):
    if thread.get("historyMode") != "paginated":
        return
    path = thread.get("path")
    if not path or not os.path.isfile(path):
        raise HistoryProjectionError("无法核验分页历史的原始日志；消息撤回未完成，后续发送已暂停")
    expected = 0
    with open(path, encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            row = json.loads(line)
            if line_number == 1:
                meta = row.get("payload") or {}
                if row.get("type") != "session_meta" or meta.get("id") != thread.get("id"):
                    raise HistoryProjectionError("原始日志与会话不匹配；停止自动撤回")
                # 原生 revert 会另起一段日志，序号从保留前缀的末端继续，而非从零开始。
                base = meta.get("history_base") or {}
                expected = base.get("end_ordinal_exclusive", 0)
                if not isinstance(expected, int) or expected < 0:
                    raise HistoryProjectionError("原始日志分段边界异常；停止自动撤回")
            if row.get("ordinal") != expected:
                raise HistoryProjectionError(
                    f"Codex 历史日志序号异常（第 {line_number} 行），历史索引可能缺失后续回合；"
                    "消息撤回未完成，后续发送已暂停。需要从保留原记录的修复副本恢复会话")
            expected += 1


def revert_thread(session_id, thread_id, before_turn_id, cwd=None, env=None, binary="codex",
                  method="revert", expected=None, checkpoint=None, timeout=30):
    """从 app-server 持久上下文中删除撤回回合，不触碰工作区文件。

    优先借 Desk 会话的常驻连接执行：它持有这条 thread 的写入权，另起 app-server
    容易撞上 active writer。连接已被中断兜底摘掉时才起短连接。Codex 新版使用
    thread/revert(beforeTurnId)。未知旧版仍可从 revert
    的 method-not-found 或旧会话不支持分页撤回时安全回退。
    """
    thread_id = str(thread_id or "").strip()
    before_turn_id = str(before_turn_id or "").strip()
    if not thread_id or not before_turn_id:
        return {"ok": False, "conflict": True,
                "error": "发送结果不确定且缺少 threadId 或 turnId；停止自动撤回，需核对原生历史"}
    guard = _session_lock(session_id) if session_id else None
    entry = None
    conn = None
    owned = False
    prior_approval = None
    changed_approval = False
    deadline = time.monotonic() + timeout
    def left():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppServerTimeout("撤回核验超时")
        return remaining
    if guard and not guard.acquire(timeout=max(0, timeout)):
        return {"ok": False, "error": "撤回等待会话锁超时"}
    try:
        if session_id:
            with _SESSIONS_LOCK:
                hit = _SESSIONS.get(session_id)
                if (hit is not None and hit.thread_id == thread_id and not hit.busy
                        and hit.conn.proc.poll() is None):
                    entry = hit
                    entry.busy = True
                    conn = entry.conn
        if conn is None:
            conn = _Connection(cwd=cwd, env=env, binary=binary)
            owned = True
            conn.initialize(overall=left())
            # thread/revert / thread/rollback 只认识当前 app-server 已加载的 thread。
            # Esc 的中断兜底可能已经摘掉原常驻连接；短连接若初始化后直接撤回，
            # 即使 rollout 仍在磁盘也会报 thread not found。先恢复，再执行撤回。
            conn.request("thread/resume", {"threadId": thread_id, "cwd": cwd}, overall=left())
        prior_approval = conn.approval_hook
        conn.approval_hook = None  # 撤回不运行工具，也不继承上一轮可能无限等待的审批回调。
        changed_approval = True
        from recall_protocol import verified_revert
        return verified_revert(lambda name, params: conn.request(name, params, overall=left()),
                               thread_id, before_turn_id, method, expected, checkpoint,
                               validate_history=_validate_recall_history
                               if os.path.basename(str(binary)) == "codex" else None)
    except HistoryProjectionError as exc:
        return {"ok": False, "conflict": True, "history_corrupt": True, "error": str(exc)}
    except Exception as exc:
        # 超时/读错后的响应不能留给下一轮读；只有本操作占用的连接可以释放。
        if entry is not None:
            drop_session(session_id, entry)
        return {"ok": False, "error": str(exc)[:300]}
    finally:
        if changed_approval:
            conn.approval_hook = prior_approval
        if entry is not None:
            entry.busy = False
            entry.at = time.time()
        if owned and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if guard:
            guard.release()


def _ordinal_projection_error(exc):
    """Codex 0.152 的分页 fork 会拒绝旧版本留下的重复 rollout ordinal。"""
    text = str(exc or "").lower()
    return ("thread history projection" in text
            and "expected ordinal" in text
            and "got" in text)


def _normalized_rollout(source, target, thread_id):
    """复制 rollout 并重排顶层 ordinal；源会话文件始终只读。"""
    count = 0
    with open(source, encoding="utf-8") as src, open(target, "w", encoding="utf-8") as dst:
        for count, line in enumerate(src, 1):
            row = json.loads(line)
            row["ordinal"] = count - 1
            # path fork 会用 session_meta 的 id 判断是否在拿旧路径恢复同一条 thread。
            # 临时快照必须有自己的身份，否则即使 path 不同也会被判为 stale path。
            if count == 1 and row.get("type") == "session_meta":
                payload = row.get("payload") or {}
                payload["id"] = thread_id
                payload["session_id"] = thread_id
                row["payload"] = payload
            dst.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    if not count:
        raise AppServerError("Codex rollout 为空，无法修复分页 fork")


def _fork_from_normalized_rollout(conn, thread_id, variant, overall):
    """用临时规范化快照绕过旧 rollout 的 ordinal 污染，不修改历史会话。"""
    meta = conn.request("thread/read", {
        "threadId": thread_id,
        "includeTurns": False,
    }, overall=overall())
    source = str((((meta or {}).get("thread") or {}).get("path") or "")).strip()
    if not source or not os.path.isfile(source):
        raise AppServerError("找不到 Codex rollout，无法修复分页 fork")
    source = os.path.realpath(source)
    marker = os.sep + "sessions" + os.sep
    if marker not in source:
        raise AppServerError("Codex rollout 不在 sessions 目录，无法安全建立临时 fork")
    sessions_dir = source.split(marker, 1)[0] + os.sep + "sessions"
    snapshot_id = str(uuid.uuid4())
    # 0.152 的 path loader 只接受 sessions 树内文件，而且分页 fork 仍要求该 thread 已
    # 进入 store。先把快照 resume 成短命 thread，再从它 fork；finally 立即 delete，
    # 不让临时快照留在历史列表或磁盘。
    with tempfile.TemporaryDirectory(prefix=".desktron-recap-", dir=sessions_dir) as tmp:
        stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime())
        # path loader 还会校验官方 rollout-<时间>-<uuid>.jsonl 文件名。
        target = os.path.join(tmp, f"rollout-{stamp}-{snapshot_id}.jsonl")
        _normalized_rollout(source, target, snapshot_id)
        resumed_id = ""
        try:
            resumed = conn.request("thread/resume", {
                "threadId": "",
                "path": target,
                "excludeTurns": True,
            }, overall=overall())
            resumed_id = str((((resumed or {}).get("thread") or {}).get("id") or ""))
            if not resumed_id:
                raise AppServerError("临时 Codex rollout 恢复后没有 thread id")
            repaired = dict(variant)
            repaired["threadId"] = resumed_id
            repaired.pop("path", None)
            try:
                return conn.request("thread/fork", repaired, overall=overall())
            except AppServerError as exc:
                # 与正常兼容链一致：当前 0.152 不允许 deferGoalContinuation + ephemeral。
                if ("deferGoalContinuation" in repaired
                        and any(x in str(exc).lower() for x in
                                ("cannot be combined", "conflict", "invalid params"))):
                    repaired.pop("deferGoalContinuation", None)
                    return conn.request("thread/fork", repaired, overall=overall())
                raise
        finally:
            if resumed_id:
                try:
                    conn.request("thread/delete", {"threadId": resumed_id}, overall=10)
                except Exception:
                    pass


def recap_fork(thread_id, prompt, cwd=None, env=None, binary="codex", timeout=120,
               cancel_run=None, read_only=False, require_ephemeral=False, no_tools=False,
               on_forked=None):
    """在源 thread 的临时分支上跑一轮纯文本 recap,返回最后一条助手正文。

    `ephemeral` 让 Codex 不把支线物化到会话记录；普通 recap 在旧版协议不认识
    该字段时退到普通 fork 并立刻 thread/delete。peer ask 传 `require_ephemeral=True`，不允许
    产生连清理都可能失败的持久支线，并以 `no_tools=True` 在 app-server 初始化前关闭原生、
    MCP 和应用工具。历史仍继承源 thread，使新增问话位于可复用缓存前缀之后。
    """
    thread_id = str(thread_id or "").strip()
    if not thread_id:
        return ""
    conn = None
    fork_id = ""
    ephemeral = True
    end = time.time() + max(1, timeout)
    completed = False
    failed = False
    tool_attempted = False
    final_text = ""
    last_text = ""
    chunks = collections.defaultdict(list)
    final_at = 0.0
    turn_id = ""
    # app-server 会把用户输入与上下文压缩也作为 item 上报；它们没有外部副作用，不能
    # 与 commandExecution/fileChange/mcpToolCall 等工具项混为一谈。
    passive_items = {"agentMessage", "reasoning", "plan", "userMessage",
                     "contextCompaction"}

    def left():
        remain = end - time.time()
        if remain <= 0:
            raise AppServerTimeout(f"recap 支线超过 {timeout} 秒")
        return max(1, int(remain))

    def belongs(params):
        event_thread = str((params or {}).get("threadId") or "")
        event_turn = str((params or {}).get("turnId") or
                         ((params or {}).get("turn") or {}).get("id") or "")
        return ((not event_thread or not fork_id or event_thread == fork_id)
                and (not event_turn or not turn_id or event_turn == turn_id))

    def note(method, params):
        nonlocal completed, failed, tool_attempted, final_text, last_text, final_at
        params = params or {}
        if not belongs(params):
            return
        if method == "item/agentMessage/delta":
            item_id = str(params.get("itemId") or "")
            delta = str(params.get("delta") or "")
            if delta:
                chunks[item_id].append(delta)
            return
        if method == "item/started":
            item = params.get("item") or {}
            if item.get("type") not in passive_items:
                tool_attempted = True
            return
        if method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage":
                text = str(item.get("text") or "")
                if not text:
                    text = "".join(chunks.get(str(item.get("id") or ""), []))
                if text:
                    last_text = text
                    if item.get("phase") == "final_answer":
                        final_text = text
                        final_at = time.time()
            elif item.get("type") not in passive_items:
                tool_attempted = True
            return
        if method == "turn/completed":
            turn = params.get("turn") or {}
            completed = True
            failed = failed or turn.get("status") == "failed"
            return
        # 响应流断开的自动重连也包成 error 通知,只是 willRetry=true。主回合早已把它当
        # 「还在跑」(engine_adapters/codex.py),这里此前一律按终局失败收场:一条长会话的
        # recap 每次都在第一帧断流,支线就永远结不了,回收闸跟着每分钟重开一条 app-server
        # 再失败一次(2026-09-07 实证:放过重连后 76 秒正常写出 recap)。
        if method == "error" and (params.get("willRetry") or params.get("will_retry")):
            return
        if method in ("turn/failed", "error"):
            completed = True
            failed = True
            return

    try:
        if no_tools:
            conn = _Connection(cwd=cwd, env=env, binary=binary,
                               opts={"_no_tools": True})
        else:
            conn = _Connection(cwd=cwd, env=env, binary=binary)
        if cancel_run is not None:
            cancel_run.bind(conn.proc)
        conn.initialize(overall=left())
        if no_tools:
            # Verify process-level gates before any source thread is resumed or
            # forked.  User/project config can add MCP and tool features; this
            # check fails closed if a binary ignores one of our overrides.
            config = (conn.request("config/read", {
                "cwd": cwd, "includeLayers": False}, overall=left()).get("config") or {})
            features = config.get("features") or {}
            mcp = ((config.get("orchestrator") or {}).get("mcp") or {})
            # A configured server without a verifiable global off switch fails closed.
            mcp_safe = mcp.get("enabled") is False
            required_off = ("shell_tool", "apps", "browser_use", "computer_use",
                            "multi_agent", "hooks", "plugins", "image_generation",
                            "tool_suggest", "web_search_cached", "web_search_request",
                            "artifact", "code_mode", "code_mode_host", "enable_mcp_apps",
                            "js_repl", "multi_agent_v2", "request_permissions_tool",
                            "search_tool", "tool_search", "unified_exec", "memories",
                            "memory_self_evolution")
            if os.path.basename(str(binary)) == "codex":
                required_off += ("view_image", "skill_search", "sleep_tool",
                                 "current_time_reminder", "deferred_executor",
                                 "in_app_local_automation", "psp", "standalone_web_search",
                                 "unified_exec_tty", "external_agent_memory_import")
            if (config.get("web_search") != "disabled" or not mcp_safe
                    or any(features.get(name) is not False for name in required_off)):
                raise AppServerError("peer 隔离支线无法关闭全部工具")
        params = {"threadId": thread_id, "ephemeral": True, "excludeTurns": True}
        if read_only:
            params.update(approvalPolicy="untrusted", sandbox="read-only")
        # 有持续目标的 Codex thread 分叉后默认可能自己续跑。recap 必须等显式 turn/start。
        if os.path.basename(str(binary)) == "codex":
            params["deferGoalContinuation"] = True
        # 兼容窗口按「只去掉不认识的可选字段」逐级退,避免某个旧 Codex 只不认识
        # deferGoalContinuation 时连仍可用的 ephemeral 也一起丢掉、平白写出持久支线。
        variants = [params]
        if "deferGoalContinuation" in params:
            variants.append({k: v for k, v in params.items()
                             if k != "deferGoalContinuation"})
        variants.append({"threadId": thread_id, "ephemeral": True})
        if not require_ephemeral:
            variants.append({"threadId": thread_id})
        if read_only:
            # Compatibility must not silently drop the isolation policy.
            variants = [{**v, "approvalPolicy":"untrusted", "sandbox":"read-only"}
                        for v in variants]
        forked = None
        for i, variant in enumerate(variants):
            if i and variant == variants[i - 1]:
                continue
            try:
                forked = conn.request("thread/fork", variant, overall=left())
                ephemeral = bool(variant.get("ephemeral"))
                break
            except AppServerError as exc:
                # 旧 Codex 曾在恢复会话时重复写一个 ordinal。主会话对此宽容，0.152 的
                # 分页 fork 却会拒绝整条历史。只对这一种结构错误走临时只读快照；认证、
                # 参数或其它存储错误仍原样向上暴露。
                if (os.path.basename(str(binary)) == "codex"
                        and not require_ephemeral and _ordinal_projection_error(exc)):
                    forked = _fork_from_normalized_rollout(
                        conn, thread_id, variant, left)
                    ephemeral = bool(variant.get("ephemeral"))
                    break
                msg = str(exc).lower()
                if (i == len(variants) - 1
                        or not any(x in msg for x in
                                   ("unknown field", "invalid params", "unexpected",
                                    "cannot be combined", "conflict"))):
                    raise
        if forked is None:
            raise AppServerError("thread/fork 没有返回结果")
        fork_id = str(((forked or {}).get("thread") or {}).get("id") or "")
        if not fork_id:
            raise AppServerError("thread/fork 没有返回临时 thread id")
        if callable(on_forked):
            on_forked(fork_id)
        started = conn.request("turn/start", {
            "threadId": fork_id,
            "input": [{"type": "text", "text": str(prompt or "")}],
            **({"approvalPolicy":"untrusted", "sandboxPolicy":{"type":"readOnly"}}
               if read_only else {}),
        }, note, timeout=min(30, left()), overall=left())
        turn_id = str(((started or {}).get("turn") or {}).get("id") or "")
        if not turn_id:
            raise AppServerError("turn/start 没有返回 recap turn id")
        if tool_attempted:
            conn.request_nowait("turn/interrupt", {"threadId": fork_id, "turnId": turn_id})
        while not completed:
            if cancel_run is not None and cancel_run.cancelled.is_set():
                return ""
            if tool_attempted:
                # prompt 已明确禁止工具；一旦仍尝试,立即中止且不把错误解释落进 journal。
                conn.request_nowait("turn/interrupt", {"threadId": fork_id, "turnId": turn_id})
                return ""
            try:
                msg = conn.read(min(1, max(0.1, end - time.time())))
            except AppServerTimeout:
                if final_at and time.time() - final_at >= 2:
                    break       # Codex 偶发漏 turn/completed,与主回合同一兜底
                left()
                continue
            if "id" in msg and msg.get("method"):
                conn.handle_server_request(msg)   # recap 无人审批,统一即时拒绝
                continue
            method = msg.get("method") or ""
            if method:
                note(method, msg.get("params") or {})
        if failed:
            raise AppServerError("recap 支线回合失败")
        if tool_attempted:
            return ""
        if final_text:
            return final_text.strip()
        if last_text:
            return last_text.strip()
        return "".join("".join(v) for v in chunks.values()).strip()
    except Exception:
        if time.time() >= end and cancel_run is not None:
            cancel_run.cancel()
        # 上层 RecapMixin 统一记录失败状态与日志。此前这里吞掉所有协议错误，表现为
        # recap 永远是 null、journal 永远不写，同时日志也完全为空。
        raise
    finally:
        if conn is not None and fork_id and not ephemeral:
            try:
                conn.request("thread/delete", {"threadId": fork_id}, overall=10)
            except Exception:
                # 用户回到主会话时 cancel_run 会先收掉旧连接；持久兼容支线仍要用一条
                # 新短连接补删，不能因取消而在历史列表里积一条半成品。
                cleanup = None
                try:
                    cleanup = _Connection(cwd=cwd, env=env, binary=binary)
                    cleanup.initialize(overall=10)
                    cleanup.request("thread/delete", {"threadId": fork_id}, overall=10)
                except Exception:
                    pass
                finally:
                    if cleanup is not None:
                        try:
                            cleanup.close()
                        except Exception:
                            pass
        if cancel_run is not None and conn is not None:
            cancel_run.unbind(conn.proc)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def compact_thread(session_id, thread_id, cwd=None, env=None, on_notification=None,
                   binary="codex", label="Codex"):
    """对一条 Codex thread 发起上下文压缩。

    app-server 暴露的是 thread/compact/start，不是 TUI 文本命令。优先复用这段 GUI
    会话的常驻连接；没有连接时用短连接 resume 后再 compact。
    """
    thread_id = str(thread_id or "").strip()
    if not thread_id:
        return {"ok": False, "error": f"这段 {label} 会话还没有 thread，无法压缩"}
    entry = None
    with _SESSIONS_LOCK:
        entry = _SESSIONS.get(session_id or "")
        if entry is not None and entry.busy:
            return {"ok": False, "error": "当前回合还在进行，结束后再压缩"}
    own = entry is None
    conn = None
    try:
        if own:
            conn = _Connection(cwd=cwd, env=env, binary=binary)
            conn.initialize(on_notification=on_notification, overall=60)
            conn.request("thread/resume", {"threadId": thread_id, "cwd": cwd},
                         on_notification, overall=120)
        else:
            conn = entry.conn
            entry.at = time.time()
        conn.request("thread/compact/start", {"threadId": thread_id},
                     on_notification, timeout=30, overall=240)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _catalog_fetch(cwd=None, env=None, conn=None, on_notification=None, binary="codex"):
    """真去问一遍。起进程 + 两轮 RPC,实测 5 秒上下,别放在请求线程里。"""
    own_connection = conn is None
    if own_connection:
        conn = _Connection(cwd=cwd, env=env, binary=binary)
    try:
        if own_connection:
            conn.initialize(on_notification=on_notification)
        models = conn.request("model/list", {"limit": 100, "includeHidden": False},
                              on_notification).get("data") or []
        modes = conn.request("collaborationMode/list", {}, on_notification).get("data") or []
        value = {"models": models, "modes": modes}
        now = time.time()
        with _CATALOG_LOCK:
            _catalog_slot(binary).update(at=now, value=value)
        _catalog_save_disk(now, value, binary)
        return value
    finally:
        if own_connection:
            conn.close()


def catalog(cwd=None, env=None, max_age=None, conn=None, on_notification=None, block=False,
            binary="codex"):
    """返回 Codex 自己公布的模型、模型 Effort 和 collaboration modes。

    默认不阻塞:手上有值(内存或盘上)就直接给,过期了在后台刷新;一个都没有就先返回空目录,
    让调用方按自己的兜底档位表显示,拉到之后下一次取就是全的。
    block=True 用于预热与需要确定值的场合(比如 /agent 那类必须报出真实模型名的地方)。"""
    now = time.time()
    max_age = _CATALOG_TTL if max_age is None else max_age
    with _CATALOG_LOCK:
        slot = _catalog_slot(binary)
        if slot["value"] is None:
            snap = _catalog_load_disk(binary)
            if snap:
                slot.update(at=snap.get("at") or 0.0, value=snap["value"])
        value, at = slot["value"], slot["at"]
        if value is not slot["value"]:
            slot["value"] = value
    if value is not None and now - at < max_age:
        return value
    if block or conn is not None:
        return _catalog_fetch(cwd, env, conn, on_notification, binary)
    # 有旧值就先用旧值,后台去换新的;一个都没有就返回空,调用方走兜底
    _catalog_refresh_async(cwd, env, binary)
    return value if value is not None else {"models": [], "modes": []}


def _catalog_refresh_async(cwd=None, env=None, binary="codex"):
    """后台拉一次,同一个二进制同一时刻只跑一个。拉失败就算了,下次再说——
    目录拿不到不影响跑回合(跑回合那条路自己带连接同步拉)。"""
    with _CATALOG_LOCK:
        if binary in _CATALOG_FETCHING:
            return
        _CATALOG_FETCHING.add(binary)

    def run():
        try:
            _catalog_fetch(cwd, env, binary=binary)
        except Exception as error:
            # Report only the failure class, never native stderr/config/credentials.
            try:
                import deskcfg
                deskcfg.log('codex-models', f'目录刷新失败：{type(error).__name__}')
            except Exception:
                pass
        finally:
            with _CATALOG_LOCK:
                _CATALOG_FETCHING.discard(binary)

    threading.Thread(target=run, daemon=True).start()


def catalog_warm(cwd=None, env=None, binary="codex"):
    """预热:Desk 起来之后拉一次,把那 5 秒挪到用户看不见的地方。"""
    _catalog_refresh_async(cwd, env, binary)


def _selected(catalog_data, opts):
    models = catalog_data.get("models") or []
    modes = catalog_data.get("modes") or []
    model = str(opts.get("model") or "")
    model_row = next((x for x in models if x.get("id") == model or x.get("model") == model), None)
    if model_row is None:
        model_row = next((x for x in models if x.get("isDefault")), None) or (models[0] if models else {})
    model = model_row.get("model") or model_row.get("id") or model
    mode_id = str(opts.get("collaboration_mode") or "default")
    mode_row = next((x for x in modes if x.get("mode") == mode_id), None) or {}
    effort = (opts.get("effort") or mode_row.get("reasoning_effort")
              or model_row.get("defaultReasoningEffort") or None)
    return model, effort, mode_id


def _desk_model_resume_warning(message, selected_model):
    """旧 thread 记录的模型和 Desk 当前模型不同：以 Desk 选择为准时不算异常。"""
    msg = str(message or "")
    model = str(selected_model or "")
    if not model or "recorded with model" not in msg or "resuming with" not in msg:
        return False
    m = re.search(r"resuming with `([^`]+)`", msg)
    return bool(m and m.group(1).strip().casefold() == model.strip().casefold())


def run_turn(cwd, env, thread_id, text, opts, system_text="", image_paths=None,
             runtime_roots=None, on_notification=None, on_process=None, on_environment=None,
             timeout=900, hard_timeout=None, session_id="", binary="codex", label="Codex", on_approval=None,
             on_turn_started=None, on_turn_submitting=None, on_model_selected=None, cancelled=None,
             history_items=None, strict_boundary=False, session_name="", on_name_sync=None,
             on_connection=None, turn_system_text=None, on_working_context=None,
             operation='turn', on_compact_completed=None, resident_context=None,
             resident_through_seq=None, on_session_create=None, on_session_drop=None,
             on_history_injected=None, on_stall=None,
             session_recap=True, writer_lock_root=None, owner=None):
    """运行一个 app-server 回合；仅在无副作用的容量拒绝后有界续跑。"""
    base_timeout = max(.001, float(timeout))
    total_timeout = (max(.001, float(hard_timeout)) if hard_timeout is not None
                     else base_timeout * 8)
    deadline = time.monotonic() + total_timeout
    codex_turn = operation == 'turn' and os.path.basename(str(binary)) == 'codex'
    capacity_delays = CAPACITY_RETRY_DELAYS if codex_turn else ()
    routing_delays = WORKSPACE_ROUTING_RETRY_DELAYS if codex_turn else ()
    current_thread, current_text = thread_id, text
    current_images = list(image_paths or [])
    current_turn_system = turn_system_text
    current_submit = on_turn_submitting
    current_session_name, current_name_sync = session_name, on_name_sync
    retry_counts = {'capacity': 0, 'workspace_routing': 0}
    retry_count = 0
    retry_maximum = 0
    retry_active = False
    stall_sequence = 0

    def emit(method, params):
        if on_notification:
            on_notification(method, params)

    def emit_stall(payload):
        nonlocal stall_sequence
        if not callable(on_stall):
            return
        stall_sequence += 1
        event = {**dict(payload or {}), 'sequence':stall_sequence}
        def notify():
            try:
                on_stall(event)
            except Exception:
                pass  # Observation must never change native execution.
        threading.Thread(target=notify, daemon=True).start()

    def retry_clear(reason):
        nonlocal retry_active
        if not retry_active:
            return
        emit('turn/retry', {'active':False, 'attempt':retry_count,
             'max':retry_maximum, 'reason':reason})
        retry_active = False

    while True:
        buffered = []
        semantic = [False]

        def relay_turn(method, params):
            if _capacity_semantic_event(str(method or ''), params):
                semantic[0] = True
                if retry_active:
                    retry_clear('progress')
                for saved_method, saved_params in buffered:
                    emit(saved_method, saved_params)
                buffered.clear()
            if _retryable_terminal_event(method, params) and not semantic[0]:
                buffered.append((method, dict(params or {})))
                return
            if retry_active and method in ('error', 'turn/failed', 'turn/completed'):
                retry_clear('settled')
            emit(method, params)

        remaining = max(.001, deadline - time.monotonic())
        try:
            return _run_turn_once(
                cwd, env, current_thread, current_text, opts, system_text=system_text,
                image_paths=current_images, runtime_roots=runtime_roots,
                on_notification=relay_turn, on_process=on_process,
                on_environment=on_environment, timeout=min(base_timeout, remaining),
                hard_timeout=remaining, session_id=session_id, binary=binary, label=label,
                on_approval=on_approval, on_turn_started=on_turn_started,
                on_turn_submitting=current_submit, on_model_selected=on_model_selected,
                cancelled=cancelled, history_items=history_items,
                strict_boundary=strict_boundary, session_name=current_session_name,
                on_name_sync=current_name_sync, on_connection=on_connection,
                turn_system_text=current_turn_system, on_working_context=on_working_context,
                operation=operation, on_compact_completed=on_compact_completed,
                resident_context=resident_context, resident_through_seq=resident_through_seq,
                on_session_create=on_session_create, on_session_drop=on_session_drop,
                on_history_injected=on_history_injected, on_stall=emit_stall,
                session_recap=session_recap, writer_lock_root=writer_lock_root, owner=owner)
        except (_CapacityUnavailable, _WorkspaceRoutingUnavailable) as exc:
            current_thread = exc.thread_id or current_thread
            routing = isinstance(exc, _WorkspaceRoutingUnavailable)
            status = 'workspace_routing' if routing else 'capacity'
            delays = routing_delays if routing else capacity_delays
            used = retry_counts[status]
            delay = delays[used] if used < len(delays) else None
            enough_time = delay is not None and time.monotonic() + delay < deadline
            stopped = bool(cancelled and cancelled())
            if semantic[0] or delay is None or not enough_time or stopped:
                try:
                    retry_clear('cancelled' if stopped else 'exhausted')
                    for saved_method, saved_params in buffered:
                        emit(saved_method, saved_params)
                finally:
                    if session_id:
                        drop_session(session_id)
                raise AppServerError(str(exc)) from exc
            retry_counts[status] += 1
            retry_count = retry_counts[status]
            retry_maximum = len(delays)
            retry_active = True
            wait_ms = int(delay * 1000)
            signal_name = ('turn.workspaceRoutingDiscovery'
                           if routing else 'turn.serverOverloaded')
            try:
                emit('turn/retry', {'active':True, 'attempt':retry_count,
                     'max':retry_maximum, 'delay_ms':wait_ms,
                     'deadline_at':int(time.time() * 1000) + wait_ms,
                     'status':status, 'error':str(exc)[:300],
                     'native_error':_retry_error_diagnostic(exc.error),
                     'source':'desk', 'signal':signal_name})
                description = ('工作区后端路由不可用' if routing else '模型容量不足')
                _log(f'{label} {description}；{delay:g} 秒后进行第 {retry_count}/{retry_maximum} 次重试')
                waited = _capacity_wait(delay, cancelled, deadline)
            except BaseException:
                try:
                    retry_clear('failed')
                except Exception:
                    pass
                if session_id:
                    drop_session(session_id)
                raise
            if not waited:
                stopped = bool(cancelled and cancelled())
                try:
                    retry_clear('cancelled' if stopped else 'exhausted')
                    for saved_method, saved_params in buffered:
                        emit(saved_method, saved_params)
                finally:
                    if session_id:
                        drop_session(session_id)
                raise AppServerError(str(exc)) from exc
            # Codex records the original user input in the failed native turn.
            # Continue that same thread with an empty input, matching Codex
            # Desktop; resending text/images could duplicate future side effects.
            current_text = ''
            current_images = []
            current_turn_system = None
            current_submit = None       # the Desk request crossed its fence once
            current_session_name = ''   # naming is not part of a retry
            current_name_sync = None
            continue
        except Exception:
            retry_clear('failed')
            for saved_method, saved_params in buffered:
                emit(saved_method, saved_params)
            raise


def _run_turn_once(cwd, env, thread_id, text, opts, system_text="", image_paths=None,
             runtime_roots=None, on_notification=None, on_process=None, on_environment=None,
             timeout=900, hard_timeout=None, session_id="", binary="codex", label="Codex", on_approval=None,
             on_turn_started=None, on_turn_submitting=None, on_model_selected=None, cancelled=None,
             history_items=None, strict_boundary=False, session_name="", on_name_sync=None,
             on_connection=None, turn_system_text=None, on_working_context=None,
             operation='turn', on_compact_completed=None, resident_context=None,
             resident_through_seq=None, on_session_create=None, on_session_drop=None,
             on_history_injected=None, on_stall=None,
             session_recap=True, writer_lock_root=None, owner=None):
    """执行一次原生 turn；容量策略由外层 run_turn 独占。"""
    # timeout 是软停滞观察窗口；hard_timeout 是包含启动、工具和审批等待的绝对上限。
    # 两者都从这里开始，避免 initialize/resume 落在计时之外。
    timeout = max(0.001, float(timeout))
    hard_timeout = max(timeout, float(hard_timeout) if hard_timeout is not None else timeout * 8)
    # Absolute execution budgets use a monotonic clock. Wall-clock corrections
    # must never lengthen a native turn or its one-shot compaction reserve.
    turn_budget_started = time.monotonic()
    turn_end = turn_budget_started + hard_timeout
    maximum_turn_end = turn_budget_started + hard_timeout * 2
    automatic_compaction_extended = [False]
    guard = _session_lock(session_id) if session_id else None
    entry = None
    conn = None
    keep = False
    created = True
    ready_sent = False
    mcp_states = {}
    mcp_auth = {}
    config_warnings = []
    startup_warnings = []
    selected_model = ""
    retry_app_control = False
    capacity_safe = [True]
    hold_for_capacity = False
    if on_working_context and history_items is None:
        raise AppServerError('Working context export requires an exclusive projected-history thread')
    if on_working_context and session_id and resident_through_seq is None:
        raise AppServerError('Resident projected history requires a durable archive cursor')
    if operation not in ('turn', 'compact'):
        raise AppServerError('Unknown app-server operation')
    if operation == 'compact' and (not on_working_context or not on_compact_completed):
        raise AppServerError('Manual projected compaction requires checkpoint export and terminal capture')

    def environment(state, label, stage="", details=None):
        if not on_environment or (not created and state in ("working", "ready")):
            return
        failed = sum(1 for x in mcp_states.values() if x == "failed")
        ready = sum(1 for x in mcp_states.values() if x == "ready")
        on_environment({
            "state": state, "stage": stage, "label": label,
            "details": list(details or []),
            "mcp": {"ready": ready, "failed": failed, "total": len(mcp_states)},
        })

    def warning_details():
        rows = list(config_warnings) + list(startup_warnings)
        rows += [f"MCP {name}：{detail}" for name, detail in mcp_auth.items()]
        rows += [f"MCP {name}：{state}" for name, state in mcp_states.items()
                 if state == "failed" and name not in mcp_auth]
        return list(dict.fromkeys(x[:300] for x in rows if x))[:12]

    def warning_label(rows):
        if rows and all("Skill descriptions were shortened to fit the" in row for row in rows):
            return f"{label} · 技能描述已缩短"
        if not ready_sent:
            return f"{label} 启动 · {len(rows)} 项"
        return f"{label} 已就绪，{len(rows)} 项异常"

    def relay(method, params):
        nonlocal ready_sent, retry_app_control
        if method == "mcpServer/startupStatus/updated":
            name = str(params.get("name") or "MCP")
            state = str(params.get("status") or "")
            if state:
                mcp_states[name] = state
            if state == "ready":
                mcp_auth.pop(name, None)
                if name == "desk_app_control":
                    retry_app_control = False
            elif state == "failed":
                detail = str(params.get("error") or params.get("failureReason") or state)
                mcp_auth[name] = detail[:240]
                # MCP 的启动状态属于 app-server 进程。失败后继续缓存这条连接，后续回合
                # 只会复用同一个失败实例，即使路径已经同步好也不会再尝试。当前回合仍可
                # 正常完成，但结束时丢弃连接，让下一回合冷起并重新加载 App Control。
                if name == "desk_app_control":
                    retry_app_control = True
            elif state == "cancelled":
                # app-server 会对不再需要的线程级 MCP 启动任务发 cancelled；没有错误字段时
                # 这是生命周期收尾，不是 MCP 故障。旧逻辑把它持久化成黄色警告。
                mcp_auth.pop(name, None)
            rows = warning_details()
            if rows:
                environment("warning", warning_label(rows) if ready_sent
                            else f"MCP {name} 加载异常", "mcp", rows)
            elif not ready_sent:
                environment("working", f"正在加载 MCP · {name}", "mcp")
            # ready_sent 之后还可能收到迟到的 MCP 状态帧。没有异常时它只会把
            # 同一条「Codex 已就绪」再弹一次，属于噪声；有异常时上面的 warning 仍会透出。
        elif method == "configWarning":
            summary = str(params.get("summary") or f"{label} 配置警告")
            detail = str(params.get("details") or "")
            row = summary + ("：" + detail if detail else "")
            if _desk_model_resume_warning(row, selected_model):
                return
            config_warnings.append(row[:300])
            environment("warning", f"{label} 配置 · {len(warning_details())} 项",
                        "config", warning_details())
        elif method == "warning":
            msg = str(params.get("message") or f"{label} 启动警告")[:300]
            if _desk_model_resume_warning(msg, selected_model):
                return
            if msg.startswith("Skill descriptions were shortened to fit the"):
                msg = "技能仍全部可见；为满足 2% 的技能上下文预算，部分描述已缩短。\n" + msg
            startup_warnings.append(msg)
            rows = warning_details()
            environment("warning", warning_label(rows),
                        "runtime" if ready_sent else "startup", rows)
        if on_notification:
            on_notification(method, params)

    if guard:
        guard.acquire()
    try:
        if session_id:
            with _SESSIONS_LOCK:
                _INFLIGHT.add(session_id)
        if session_id:
            key = _session_key(cwd, env, opts, system_text, runtime_roots, binary)
            session_opts = ({**(opts or {}), '_working_context_export':True}
                            if on_working_context else opts)
            def create_session():
                environment("working", f"正在启动 {label}", "process")
                if on_session_create:
                    on_session_create()
            entry, created = _get_session(
                session_id, key, cwd, env, thread_id, binary=binary,
                opts=session_opts, on_create=create_session,
                context_key=resident_context, through_seq=resident_through_seq,
                recap_on_evict=session_recap, on_drop=on_session_drop,
                writer_lock_root=writer_lock_root, owner=owner)
            entry.busy = True
            conn = entry.conn
        else:
            environment("working", f"正在启动 {label}", "process")
            conn_args = {"cwd": cwd, "env": env, "binary": binary}
            if _compact_limit(opts, binary):
                conn_args["opts"] = opts
            if on_working_context:
                conn_args['opts'] = {**(opts or {}),'_working_context_export':True}
            conn = _Connection(**conn_args)
        # 审批接管挂在连接上,每回合按当前调用方重挂:同一条连接跨回合复用,上一轮的
        # 闭包握着上一轮的 ctx,不换会把这一轮的问句发到上一轮的会话里去。
        conn.approval_hook = on_approval
        if on_connection:
            on_connection(conn)
        if on_process:
            on_process(conn.proc)
        if cancelled and cancelled():
            return thread_id
        if created:
            environment("working", f"正在初始化 {label}", "initialize")
            conn.initialize(on_notification=relay,
                            overall=max(1, int(turn_end - time.monotonic())))
            if cancelled and cancelled():
                return thread_id
        # Codex 的线程启动会推送 MCP startupStatus；清单查询不是 turn/start 的前置条件。
        # 它会额外等待所有 MCP 的工具/鉴权清单，慢服务可把用户问题堵在这里 10–30s。
        # 启动失败与需重登录仍由 relay 显示，不屏蔽 MCP，也不另开线程争读连接。
        if created and os.path.basename(str(binary)) != "codex":
            environment("working", "正在加载 MCP", "mcp")
            try:
                status = conn.request(
                    "mcpServerStatus/list", {"limit": 100, "detail": "toolsAndAuthOnly"},
                    relay, timeout=10, overall=min(30, max(1, int(turn_end - time.monotonic()))))
                for row in status.get("data") or []:
                    name = str(row.get("name") or "MCP")
                    mcp_states[name] = "ready"
                    if row.get("authStatus") == "notLoggedIn":
                        mcp_auth[name] = "需要重新登录"
                environment("working", f"已加载 {len(status.get('data') or [])} 个 MCP", "mcp")
            except AppServerError as exc:
                startup_warnings.append(f"无法读取 MCP 状态：{str(exc)[:220]}")
                environment("warning", "无法确认 MCP 状态", "mcp", warning_details())
        # 缓存过期时也在当前会话连接上刷新目录，避免为 model/list 另起一个 app-server。
        if cancelled and cancelled():
            return thread_id
        environment("working", f"正在读取 {label} 配置", "config")
        caps = catalog(cwd=cwd, env=env, conn=conn, on_notification=relay, binary=binary)
        if cancelled and cancelled():
            return thread_id
        model, effort, mode_id = _selected(caps, opts or {})
        selected_model = model
        # 目录解析后的值才是本回合真正提交给 app-server 的模型。普通命中不会像
        # model/rerouted 那样另发通知，因此必须在派发边界显式交给调用方，供消息尾注
        # 按回合持久化；后续若发生重路由，原生通知会再覆盖它。
        if on_model_selected and model:
            on_model_selected(model)
        sandbox = str((opts or {}).get("sandbox") or "workspace-write")
        if sandbox not in ("read-only", "workspace-write", "danger-full-access"):
            sandbox = "workspace-write"
        # 有人接管审批才让引擎开口问。没有接管方时仍旧 never——那时越权动作直接失败,
        # 失败看得见;留 on-request 会让回合停在一个没人看得见的问句上,静默到超时。
        policy = "on-request" if on_approval else "never"
        roots = [str(x) for x in (runtime_roots or []) if str(x)]
        base = {
            "cwd": cwd,
            "approvalPolicy": policy,
            "sandbox": sandbox,
            "developerInstructions": system_text or None,
        }
        # Callers that supply projected history own it. A replaced process must
        # rebuild from that projection, never touch ordinary native transcripts.
        if history_items is not None:
            # Unified execution exports native compaction from its private,
            # disposable rollout. Ordinary callers retain their existing mode.
            base.update(ephemeral=not bool(on_working_context), experimentalRawEvents=True)
            if on_working_context:
                base['config'] = {'features.memories':False, 'features.memory_self_evolution':False}
            if created:
                thread_id = ""
                if model:
                    base["model"] = model
        if model and not thread_id:
            base["model"] = model
        if roots:
            base["runtimeWorkspaceRoots"] = roots
        # 续接一条很长的 thread 要读不少东西,给得比缺省宽,但仍然有上限。
        _left = lambda: max(1, int(turn_end - time.monotonic()))
        if entry is not None and entry.thread_id:
            thread_id = entry.thread_id
            started = {}
        elif thread_id:
            environment("working", "正在恢复会话", "thread")
            started = conn.request("thread/resume", dict(base, threadId=thread_id),
                                   relay, overall=_left())
        else:
            environment("working", "正在创建会话", "thread")
            started = conn.request("thread/start", base, relay, overall=_left())
        thread = started.get("thread") or {}
        thread_id = thread.get("id") or thread_id
        if not thread_id:
            raise AppServerError("app-server 没有返回 thread id")
        if entry is not None:
            entry.thread_id = thread_id
        if session_name:
            name_state = "synced"
            try:
                conn.request("thread/name/set", {"threadId": thread_id, "name": session_name},
                             relay, timeout=8, overall=min(8, _left()))
            except AppServerError as exc:
                message = str(exc).lower()
                unsupported = any(x in message for x in (
                    "method not found", "unknown method", "unknown variant", "unsupported", "not supported", "does not support"))
                name_state = "unsupported" if unsupported else "failed"
                if not unsupported:
                    if on_name_sync:
                        on_name_sync(name_state)
                    raise
            if on_name_sync:
                on_name_sync(name_state)
        if history_items is not None and created and history_items:
            conn.request("thread/inject_items", {"threadId": thread_id, "items": history_items},
                         relay, overall=_left())
            if on_history_injected:
                snapshot = conn.request('thread/read', {'threadId':thread_id,
                    'includeTurns':False}, relay, overall=min(10,_left())).get('thread') or {}
                if snapshot.get('id') != thread_id or not snapshot.get('path'):
                    raise AppServerError('Projected history injection has no matching thread path')
                on_history_injected(thread_id,snapshot['path'])
        if created:
            ready_sent = True
            details = warning_details()
            if details:
                environment("warning", warning_label(details), "ready", details)
            else:
                environment("ready", f"{label} 已就绪", "ready")

        if operation == 'compact':
            from codex_compaction import compact_projected
            return compact_projected(conn, thread_id, on_notification=on_notification,
                on_submitting=on_turn_submitting, on_started=on_turn_started,
                on_completed=on_compact_completed, on_working_context=on_working_context,
                cancelled=cancelled, timeout=timeout, deadline=turn_end)

        inputs = [{"type": "text", "text": text}]
        inputs += [{"type": "localImage", "path": str(p)} for p in (image_paths or []) if p]
        settings = {"model": model or started.get("model") or ""}
        if effort:
            settings["reasoning_effort"] = effort
        turn_params = {
            "threadId": thread_id,
            "input": inputs,
            "approvalPolicy": policy,
            "collaborationMode": {
                "mode": mode_id if mode_id in ("default", "plan") else "default",
                "settings": settings,
            },
        }
        if turn_system_text is not None:
            # Codex 0.154 accepts application context on an existing thread.
            # Its native history retains previous snapshots; callers must label
            # their temporal scope. Thread resume does not replace stored rules;
            # mode settings are not an application-system-prompt override.
            # Opt-in only: ordinary engines and their Plan prompts stay intact.
            turn_params['additionalContext'] = {
                'desktron': {'kind':'application', 'value':turn_system_text}}
        turn_started_at = time.time()
        completed = [None]
        final_answer = [None]
        expected_turn = [None]
        early = []
        readback_after = [0.0]
        running_tools = set()
        tool_deadlines = {}

        def event_turn_id(method, params):
            event_turn = params.get("turnId") or params.get("turn_id") or ""
            if method.startswith("turn/"):
                event_turn = ((params.get("turn") or {}).get("id") or event_turn)
            return event_turn

        def belongs_to_turn(method, params):
            if expected_turn[0] is None:
                return False
            event_thread = params.get("threadId") or params.get("thread_id")
            if event_thread and event_thread != thread_id:
                return False
            event_turn = event_turn_id(method, params)
            return not event_turn or not expected_turn[0] or event_turn == expected_turn[0]

        def proves_current_turn(method, params):
            """Identity-less compatibility frames may render, but cannot renew liveness."""
            event_thread = params.get("threadId") or params.get("thread_id")
            return bool(event_thread == thread_id and expected_turn[0]
                        and event_turn_id(method, params) == expected_turn[0])

        def confirm_terminal():
            # Silence is only a reason to inspect, never evidence of success.
            # Readback must describe this exact native turn, without an active
            # successor. Queries neither replay input nor extend the idle budget.
            if (getattr(conn, "_awaiting", 0) or running_tools
                    or not expected_turn[0] or time.monotonic() < readback_after[0]):
                return
            readback_after[0] = time.monotonic() + 5
            try:
                result = conn.request("thread/read", {
                    "threadId": thread_id, "includeTurns": True}, receive,
                    timeout=5, overall=min(5, _left()))
            except AppServerError as exc:
                if completed[0] is not None:
                    return  # A real terminal notification arrived during readback.
                raise AppServerError("PROTOCOL: 原生终态缺失且回读失败；未重发请求") from exc
            if completed[0] is not None:
                return
            thread = result.get("thread") or {}
            turns = thread.get("turns")
            if thread.get("id") != thread_id or not isinstance(turns, list):
                raise AppServerError("PROTOCOL: 原生终态回读身份或结构不完整；未重发请求")
            matches = [t for t in turns if t.get("id") == expected_turn[0]]
            if len(matches) != 1:
                raise AppServerError("PROTOCOL: 原生终态回读缺少当前回合；未重发请求")
            turn = matches[0]
            if (getattr(conn, "_awaiting", 0) or running_tools
                    or any(t.get("status") == "inProgress" for t in turns)):
                return
            if turn.get("status") not in ("completed", "failed", "interrupted"):
                raise AppServerError("PROTOCOL: 原生回读返回未知终态；未重发请求")
            _log("%s 回合按原生回读确认终态，turn=%s" % (label, expected_turn[0]))
            dispatch("turn/completed", {"threadId": thread_id, "turn": turn,
                                        "terminalSource": "thread/read"})

        def dispatch(method, params):
            nonlocal turn_end
            if _capacity_semantic_event(method, params):
                capacity_safe[0] = False
            if method == "turn/started" and entry is not None and entry.interrupt_requested:
                # 原生 turn/start 的回执可能早于任务激活，紧跟其后的 interrupt 会报
                # no active turn。启动事件才确认活动回合存在；按相同 ID 补发一次。
                interrupt(session_id)
            if method == "turn/completed":
                turn = params.get("turn") or {}
                if turn.get("status") not in ("completed", "failed", "interrupted"):
                    raise AppServerError("PROTOCOL: 原生 turn/completed 缺少有效终态")
                completed[0] = turn
            elif method == "item/started":
                item = params.get("item") or {}
                exact_compaction_owner = (
                    params.get("threadId") == thread_id
                    and event_turn_id(method, params) == expected_turn[0]
                    and bool(item.get("id"))
                    and item.get("status") == "inProgress")
                if (item.get("type") == "contextCompaction"
                        and exact_compaction_owner
                        and not automatic_compaction_extended[0]):
                    # Automatic compaction earns one reserve only with explicit
                    # current thread/turn identity and a concrete in-progress item.
                    automatic_compaction_extended[0] = True
                    turn_end = max(turn_end, min(maximum_turn_end,
                                                time.monotonic() + hard_timeout))
                if item.get("status") == "inProgress" and item.get("id"):
                    running_tools.add(item["id"])
                    tool_deadline = _node_repl_deadline(item)
                    if tool_deadline is not None:
                        tool_deadlines[item["id"]] = tool_deadline
            elif method == "item/completed":
                item = params.get("item") or {}
                running_tools.discard(item.get("id"))
                tool_deadlines.pop(item.get("id"), None)
                # 严格认 final_answer,不能放宽成「不是 commentary」。
                #
                # 这个变量是给下面那条兜底用的:填上之后读超时就从 30 秒缩到 2 秒,静默即按它收口。
                # 前提是「它真的是最终答复」——codex 用 phase 区分,所以安全。放宽判据等于把
                # 过程里的每一句话都当成最终答复:agent 说完一句、接着思考或跑工具静默超过
                # 2 秒,回合就被判完了。界面显示「已完成」、输入框解锁,而引擎还在跑,这时发
                # 新消息就卡在「处理中」(2026-08-11 实见)。
                if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                    final_answer[0] = {
                        "id": params.get("turnId") or "",
                        "status": "completed",
                        "durationMs": int((time.time() - turn_started_at) * 1000),
                    }
            if on_notification:
                on_notification(method, params)

        def receive(method, params):
            if method in ("mcpServer/startupStatus/updated", "configWarning", "warning"):
                relay(method, params)
                return
            # 常驻连接可能迟到一条上一回合的正式收尾；先等 turn/start 返回当前 turn id，
            # 再决定通知归属，避免上一轮误结束这一轮。
            if expected_turn[0] is None:
                early.append((method, params))
            elif belongs_to_turn(method, params):
                dispatch(method, params)

        if on_turn_submitting and on_turn_submitting(thread_id) is False:
            keep = True
            return thread_id
        turn_started = conn.request("turn/start", turn_params, receive,
                                    timeout=60, overall=_left())
        expected_turn[0] = ((turn_started.get("turn") or {}).get("id") or "")
        if entry is not None:
            entry.turn_id = expected_turn[0]
        if on_turn_started and expected_turn[0]:
            on_turn_started(thread_id, expected_turn[0])
        with _SESSIONS_LOCK:
            asked = session_id in _PENDING_INTERRUPT if session_id else False
            _PENDING_INTERRUPT.discard(session_id)
        if asked and expected_turn[0]:
            # 这一回合还没跑起来就被按了 Esc。turnId 到手,立刻把中断补上。
            try:
                if entry is not None:
                    entry.interrupt_requested = True
                conn.request_nowait("turn/interrupt", {
                    "threadId": thread_id, "turnId": expected_turn[0]})
            except Exception:
                pass
        for method, params in early:
            if belongs_to_turn(method, params):
                dispatch(method, params)
        # 静默不是终态：长推理、同步脚本和子代理等待都可能没有原生帧。timeout 只触发
        # 可恢复的 soft-stall 观察；动态 hard_timeout 才是 Desk 的最终执行上限。
        # Provider/工具声明的明确期限仍在各自路径单独生效。
        last_seen = [time.monotonic()]
        soft_stalled = [False]
        def stall(active):
            if not callable(on_stall):
                return
            now = time.monotonic()
            payload = {'active':bool(active),
                       'seconds':max(0, int(now - last_seen[0])) if active else 0,
                       'activity':{'kind':'no_progress' if active else 'working',
                                   'active_tools':len(running_tools)}}
            try:
                on_stall(payload)
            except Exception:
                pass  # Observation must never change native execution.
        def observed_activity():
            last_seen[0] = time.monotonic()
            if soft_stalled[0]:
                soft_stalled[0] = False
                stall(False)
        while completed[0] is None:
            now = time.monotonic()
            left = turn_end - now
            if left <= 0:
                effective_limit = max(0, turn_end - turn_budget_started)
                raise AppServerError(f"{label} 回合达到最长时限 {effective_limit:g} 秒")
            if not soft_stalled[0] and now - last_seen[0] >= timeout:
                soft_stalled[0] = True
                stall(True)
            tool_left = min(tool_deadlines.values(), default=turn_end) - now
            if tool_left <= 0:
                try:
                    conn.request_nowait("turn/interrupt", {
                        "threadId": thread_id, "turnId": expected_turn[0]})
                except Exception:
                    pass
                raise AppServerError(
                    "node_repl_timeout: node_repl/js 完整调用超过外层时限；"
                    "已中断回合并释放连接")
            try:
                # Final text may precede the terminal event. After two quiet
                # seconds inspect native status; text itself is not a terminal.
                # 挂着待人审批时不适用上面那条收口:服务端在等人点头,本来就不会再发帧。
                # 按 final_answer 只等两秒的话,「等你批准」会被读成「协议终点已到」,
                # 回合当场收尾——界面上出现时间戳与复制/分支那一排,看着像已经结束,
                # 而人一批准它又继续吐字(2026-09-02 手机端实见)。request() 那条路
                # 早就按 _awaiting 放宽过,跑回合这条漏了。
                # getattr 取值:连接对象由调用方传入(测试里是替身),不该假设它一定
                # 带这两个字段;取不到就退回原来的两档判据。
                waiting = getattr(conn, "_awaiting", 0)
                quiet = (getattr(conn, "APPROVAL_WAIT", 600) if waiting
                         else (2 if final_answer[0] else 30))
                msg = conn.read(min(quiet, left, max(0.001, tool_left)))
            except AppServerTimeout:
                if tool_deadlines and min(tool_deadlines.values()) <= time.monotonic():
                    try:
                        conn.request_nowait("turn/interrupt", {
                            "threadId": thread_id, "turnId": expected_turn[0]})
                    except Exception:
                        pass
                    raise AppServerError(
                        "node_repl_timeout: node_repl/js 完整调用超过外层时限；"
                        "已中断回合并释放连接")
                if final_answer[0] is not None and not strict_boundary:
                    confirm_terminal()
                    if completed[0] is not None:
                        break
                # 安静不等于出事:一条长命令(sleep、大范围检索、等网络)跑着的时候
                # app-server 一个通知都不发。此前这里把 30 秒无声当故障抛出去,回合就在
                # 半路断掉,界面上表现为「还没做完就已处理」,而且助手消息以工具块收尾、
                # 没有 turn/completed(2026-08-10 实测:sleep 120 于 177 秒处中断)。
                # 进程还活着就接着等；静默只投影 soft-stall，最终仍受 hard_timeout 约束。
                if conn.proc.poll() is None:
                    continue
                raise
            if "id" in msg and msg.get("method"):
                request_params = msg.get('params') or {}
                request_thread = request_params.get('threadId') or request_params.get('thread_id')
                request_turn = request_params.get('turnId') or request_params.get('turn_id')
                # A request without exact current-turn identity is still answered,
                # but cannot prove this turn recovered from soft stall. Even an
                # exact request must represent a real approval/input boundary.
                if (request_thread == thread_id and request_turn == expected_turn[0]
                        and _turn_semantic_event(msg.get('method') or '', request_params)):
                    observed_activity()
                # A server request can authorize or execute work. Even if a
                # later terminal says capacity, replay is no longer provably safe.
                capacity_safe[0] = False
                # 审批请求就落在这里——回合跑起来之后的帧由本循环读,不再经 request()。
                # 接管之后这条挂着等人答,静默判据要跟着放宽:等人的那几分钟没有任何帧,
                # 按原来的线会把回合掐断,而服务端那侧还停在 waitingOnApproval。
                if conn.handle_server_request(msg):
                    continue
                continue
            method = msg.get("method") or ""
            params = msg.get("params") or {}
            if method in ("mcpServer/startupStatus/updated", "configWarning", "warning"):
                relay(method, params)
                continue
            if method and belongs_to_turn(method, params):
                if proves_current_turn(method, params) and _turn_semantic_event(method, params):
                    observed_activity()
                dispatch(method, params)
        turn = completed[0] or {}
        if on_working_context and turn.get('status') == 'completed':
            snapshot_thread = conn.request('thread/read', {
                'threadId': thread_id, 'includeTurns': False}, overall=min(10, _left())).get('thread') or {}
            if snapshot_thread.get('id') != thread_id or not snapshot_thread.get('path'):
                raise AppServerError('Native working context export has no matching rollout')
            # A terminal followed by thread/read is the live-session write barrier.
            # Disposable callers still close first; resident projected sessions
            # keep the writer and rely on the callback's stable-file verification.
            if not session_id:
                conn.close()
            on_working_context(thread_id, snapshot_thread['path'])
        if turn.get("status") == "failed":
            err = turn.get("error") or {}
            retryable_failure = None
            if (operation == 'turn' and os.path.basename(str(binary)) == 'codex'
                    and capacity_safe[0]):
                if _server_overloaded_error(err):
                    retryable_failure = _CapacityUnavailable(thread_id, err)
                elif _workspace_routing_error(err):
                    retryable_failure = _WorkspaceRoutingUnavailable(thread_id, err)
            if retryable_failure is not None:
                # Preserve this exact resident writer only until the bounded
                # wrapper continues the same native thread. Pool eviction and
                # a second producer remain fenced while it waits.
                if session_id:
                    keep = True
                    hold_for_capacity = True
                raise retryable_failure
            raise AppServerError(str(err.get("message") or f"{label} 回合失败"))
        keep = not retry_app_control
        return thread_id
    except Exception as exc:
        if created and not ready_sent:
            environment("error", f"{label} 启动失败", "error", [str(exc)[:300]])
        raise
    finally:
        if session_id:
            with _SESSIONS_LOCK:
                _INFLIGHT.discard(session_id)
                _PENDING_INTERRUPT.discard(session_id)   # 别把这一回合的中断留给下一回合
        if entry is not None:
            entry.busy = bool(hold_for_capacity)
            entry.turn_id = ""
            entry.interrupt_requested = False
            entry.at = time.time()
            if not keep or entry.conn.proc.poll() is not None:
                drop_session(session_id, entry)
        elif conn is not None:
            conn.close()
        if guard:
            guard.release()
