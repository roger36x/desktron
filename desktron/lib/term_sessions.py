# term.py 的分册(2026-08-27 分拆,specs/2026-08-27-repo-partition-2.md)。
# 不是独立模块:由 term.py 按序 exec 进它自己的命名空间——term 的猴补面很宽
# (测试补 term.<名> 约 17 个,调用方遍布各段),必须保持单命名空间的调用期查找;
# 文件切开只为可读性。名字都解析在 term 模块的 globals 里,别单独 import 这个文件。

def _claude_sessions():
    """claude 原生会话:各工作区目录的 cwd 各编码出一个 transcript 目录,取并集。
    标题取 summary,无则首条 user 文本(去 system-reminder,见 _claude_jsonl_title)。"""
    out = []
    for wd in _workspace_dirs():
        d = os.path.expanduser(f"~/.claude/projects/{_claude_proj_enc(wd)}")
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(d, fn)
            sid = fn[:-6]
            title, mt = _claude_jsonl_title(path)
            name = _claude_pick_name(path, sid, wd, title)
            out.append({"id": sid, "title": (name or sid[:8])[:80], "time": int(mt * 1000)})
    out.sort(key=lambda s: -s["time"])
    return out


def _term_sessions(engine):
    """终端引擎的原生会话清单。分发到描述符 store.list_all(各引擎落盘形态见 engines/stores.py)。"""
    from engines import get as _engine_get
    sp = _engine_get(engine)
    return sp.store.list_all() if sp and sp.store else []


def _dir_sessions(engine, wd):
    """引擎在指定目录(cwd=wd)下的原生会话清单 [{id,title,time}]。供按工作区 cwd 列 resume 候选——
    coding 项目指向真实仓,引擎在那目录的 transcript/rollout 不在 _workspace_dirs 里,要按 cwd 直扫。
    分发到描述符 store.list_by_cwd(各引擎落盘形态见 engines/stores.py)。"""
    if not wd:
        return []
    from engines import get as _engine_get
    sp = _engine_get(engine)
    if not (sp and sp.store):
        return []
    out = sp.store.list_by_cwd(wd)
    task_ids = {sid for sid, m in _term_meta_read().items() if m.get("task")}
    if task_ids:
        out = [item for item in out if item.get("id") not in task_ids]
    if engine == "cursor-agent":
        # cursor 的 transcript 按 git 仓库根归并:内建工作区(REPO_ROOT 子目录)的会话都折叠进仓库根的
        # cursor project,按子目录 cwd 直扫(list_by_cwd)拿不到 → resume picker 在这些工作区里空。
        # 补一遍 meta 里归属本工作区(wd 反查的 domain)的 cursor 会话。外部仓项目自带独立 cursor
        # project,list_by_cwd 已覆盖,这里只补缺、按 id 去重。
        # dom 为空(外部仓 coding 项目,wd 不属任何已知域)时不补扫:此时按空 domain 匹配会把无归属会话
        # 串进来,而外部仓本就由 list_by_cwd 全覆盖,补扫只为折叠进 REPO_ROOT 的内建工作区服务。
        dom = _domain_for_cwd(wd)
        if dom:
            seen = {x["id"] for x in out}
            for sid, m in _term_meta_read().items():
                if m.get("engine") != "cursor-agent" or m.get("task") or sid in seen:
                    continue
                if (m.get("domain") or "") != dom:
                    continue
                ci = _cursor_session_by_id(sid)
                if ci:
                    out.append({"id": sid, "title": ci["title"], "time": ci["time"]})
            out.sort(key=lambda s: -s["time"])
    for item in out:
        item["title"] = _display_title(item.get("id"), "cursor-agent", item) or item.get("title")
    return out


# ---- 常驻终端会话管理(会话=终端):每个终端是保活的 PTY 进程,断开不杀,重连重放缓冲 ----
# claude 终端 id = claude sessionId(uuid):新开 `claude --session-id <uuid>`,
# 重连已落盘会话 `claude --resume <uuid>`。这样 app 内会话与 ~/.claude transcript 同 id,
# claude --resume 在终端里也能看到。codex 无 --session-id,只能 live(进程在才在)。
_TERMS = {}          # 元数据存:id → {engine,domain,title,codex_uuid,task,created}。活进程状态(alive/输出/缓冲)在 termpty 进程内。
_TERMS_LOCK = __import__("threading").Lock()
# 侧栏已经出过一次:预热据此让路。预热要 fork 十几个引擎 CLI,每个都要把一份大记录读进内存,
# 那段时间整机的 CPU 与 IO 都被占着,侧栏的首次扫描夹在里面就慢。先让人看见列表,再去预热。
SIDEBAR_SERVED = __import__("threading").Event()
# 工作态观测:每引擎用各自的语义信号(被观测方主动声明),回退活动信号。
#   claude   —— OSC 标题前缀:盲文 throbber(U+2800–U+28FF)= 生成中,✳ 等稳定字形 = 空闲。对重绘/打字免疫。
#   codex    —— rollout 流式写:event_msg 的 task_started=生成中 / task_complete=完成。增量 tail(下)。
#   回退     —— 活动信号(2s 内有 PTY 输出)+ 静默窗(attach/resize/detach 的重绘不计)。
# 未读 = 用户没在看该会话时,它从"工作"翻回"空闲"(完成一轮);状态在此侧跟,不用 termpty 的活动型 unread(误报源)。
_TERM_QUIET = {}                       # sid -> busy 静默截止(仅回退路径用)
_TERM_ATTACHED = set()                 # 当前 ui_server 有 WS 附着的 sid(在看=已读)
_TERM_STATE = {}                       # sid -> {"was_busy": bool, "unread": bool}
_TERM_OBS_LOCK = __import__("threading").Lock()
_CODEX_STATUS = {}                     # codex uuid -> {"path","off","busy","ts","try"}
_CODEX_LOCK = __import__("threading").Lock()
# rollout 多久没长过就不再相信里面那个 task_started。判据本身随 TUI 的工作态一起撤了,
# 常量留给还在读 rollout 的交接蒸馏做同口径的陈旧判断。
# 取值宽松:正常回合里两条记录之间只有几秒,两分钟不写基本只有「回合已经不在了」一种解释。
CODEX_MARK_STALE = 120
def _term_quiet_set(sid, secs=3.0):
    with _TERM_OBS_LOCK:
        _TERM_QUIET[sid] = time.time() + secs
def _term_attach_mark(sid, on):
    with _TERM_OBS_LOCK:
        if on:
            _TERM_ATTACHED.add(sid)
            _TERM_STATE.setdefault(sid, {"was_busy": False, "unread": False})["unread"] = False
        else:
            _TERM_ATTACHED.discard(sid)
def _term_seen(sid, generation=None):
    """一次性已读:不进「正在看」集合,只把未读清掉。GUI 视图没有终端那样的常连附着通道,
    看见了报一声,比长期占着 attached 更贴合实际。"""
    with _TERM_OBS_LOCK:
        st = _TERM_STATE.setdefault(sid, {"was_busy": False, "unread": False})
        if generation is None:
            st["unread"] = False
        else:
            st["seen_generation"] = max(st.get("seen_generation", 0), generation)
            st["unread"] = st.get("completed_generation", 0) > st["seen_generation"]


def _gui_busy_unread(sid, live, generation=None, completed_generation=None):
    """已读属于具体回合，先到的已读回执不会被后到的完成轮询推翻。"""
    busy = sid in live
    with _TERM_OBS_LOCK:
        st = _TERM_STATE.setdefault(sid, {"was_busy": busy, "unread": False})
        if generation is not None:
            if generation < st.get("generation", 0):
                return st["was_busy"], st["unread"]
            st["generation"] = generation
            completed = completed_generation
            if completed is None:
                completed = generation if not busy and st["was_busy"] else 0
            st["completed_generation"] = max(st.get("completed_generation", 0), completed)
            # 同一回合已经完成，迟到的 busy 快照也不能让它重新闪烁。
            busy = busy and generation > st.get("completed_generation", 0)
            st["was_busy"] = busy
            st["unread"] = st.get("completed_generation", 0) > st.get("seen_generation", 0)
            return busy, st["unread"]
        if st["was_busy"] and not busy:
            st["unread"] = True
        st["was_busy"] = busy
        return busy, st["unread"]


def _gui_read_status(ad, sid):
    """列表和 /seen 共用适配器的回合契约，身份固定为引擎名 + Desk 会话号。

    不依赖引擎锚点何时生成，也不靠轮询恰好撞见 busy 才能发现短回合完成。
    无回合契约的旧适配器仍使用原来的 busy 跳变路径。
    """
    read = getattr(ad, "turn_state", None)
    state = read(sid) if callable(read) else None
    if not isinstance(state, dict):
        return None
    generation = max(0, int(state.get("generation") or 0))
    key = (ad.name, sid)
    busy = state.get("state") != "ready"
    busy, unread = _gui_busy_unread(key, {key} if busy else set(), generation,
                                  state.get("completed_generation"))
    out = {"busy": busy, "unread": unread, "completion_generation": generation}
    if "completed_generation" in state:
        out["completed_generation"] = state["completed_generation"]
    return out


def _codex_rollout_path(uuid, engine="codex"):
    """uuid → rollout 文件路径(文件名含 uuid)。解析结果缓存;未命中 30s 内不重试,免每次 GET 递归 glob。"""
    import glob as _glob
    now = time.time()
    with _CODEX_LOCK:
        st = _CODEX_STATUS.get(uuid)
        if st:
            if st.get("path") and os.path.exists(st["path"]):
                return st["path"]
            if now - st.get("try", 0) < 30:
                return None
    base = _rollout_base(engine)
    hits = _glob.glob(base + f"/**/rollout-*-{uuid}.jsonl", recursive=True)
    path = hits[0] if hits else None
    with _CODEX_LOCK:
        st = _CODEX_STATUS.setdefault(uuid, {"path": None, "off": 0, "busy": None, "ts": 0.0, "try": 0.0})
        st["try"] = now
        if path:
            st["path"] = path
    return path
def _term_busy_unread(s, lr, now):
    """返回 (busy, unread)。工具终端没有语义信号,判据是「近 2 秒有输出且不在重绘静默窗里」;
    未读按 工作→空闲 的跳变跟踪,正在看的会话恒为已读。

    引擎那套语义信号(claude 的 OSC 盲文帧、codex 家族 rollout 的 task_started/task_complete)
    随 TUI 一起撤了——GUI 会话的工作态由适配器直报(见 _gui_busy_unread)。
    """
    sid = s["id"]
    with _TERM_OBS_LOCK:
        qu = _TERM_QUIET.get(sid, 0)
        busy = (now - lr.get("last_out", 0)) < 2.0 and now >= qu
        st = _TERM_STATE.setdefault(sid, {"was_busy": busy, "unread": False})
        if sid in _TERM_ATTACHED:
            st["unread"] = False
        elif st["was_busy"] and not busy:
            st["unread"] = True
        st["was_busy"] = busy
        return busy, st["unread"]


def _live_map():
    """进程内活会话 {id: rec};rec 含 alive/osc_title/unread/last_out/created。"""
    try:
        return {s["id"]: s for s in termpty.pty_list()}
    except Exception:
        return {}

# 域(工作区)归属在 transcript 里没有,落一个 sidecar:{uuid: {domain, engine}},跨重启保留。
_TERM_META = os.path.join(DATA_HOME, "term-meta.json")
_TERM_META_LOCK = threading.RLock()


def _term_meta_read():
    from durable_json import read
    with _TERM_META_LOCK:
        return read(_TERM_META)


def _term_meta_mutate(change):
    """Read-modify-write term metadata atomically within this server process."""
    with _TERM_META_LOCK:
        meta = _term_meta_read()
        result = change(meta)
        if result:
            _term_meta_write(meta)
        return result


def term_meta_register(sid, engine="claude", domain="", task=False):
    """给一段会话登记归属,让它出现在侧栏。会话没有 PTY,归属只能从这里登记——
    TUI 退场前另有一条 _term_open 顺手写 meta 的路,GUI 建的会话不走它,于是曾经只存在于
    对话视图自己的菜单里(实测 24 段里有 17 段是这样)。
    已登记过的不动:归属可能已被用户改过,这里只补缺。"""
    if not sid:
        return
    def register(meta):
        cur = meta.get(sid)
        if cur and cur.get("engine"):
            return False
        meta[sid] = {**(cur or {}), "engine": engine, "domain": domain, "closed": False}
        if task:
            meta[sid]["task"] = True   # 执行记录:不进 Vibing 列(_term_persisted 据它跳过)
        return True
    _term_meta_mutate(register)


def _term_meta_write(meta):
    from durable_json import write
    with _TERM_META_LOCK:
        result = write(_TERM_META, meta)
        _term_catalog_invalidate()
        return result


def _term_context_domain(sid, requested):
    """校验一次会话打开请求携带的工作流上下文。

    context 只决定本次 CLI 从哪里启动,不改会话的 domain/refs。只接受会话自己的工作流或
    已写入 refs 的工作流,避免 WS query 任意创建 domains/<id> 目录。
    """
    requested = str(requested or "").strip()
    if not requested or (requested != "_desktop" and not re.fullmatch(r"[\w-]+", requested)):
        return ""
    with _TERMS_LOCK:
        live = _TERMS.get(sid) or {}
    stable_id = live.get("codex_uuid") or sid
    meta = _term_meta_read()
    m = meta.get(stable_id) or meta.get(sid) or {}
    home = m.get("domain")
    if home is None:
        home = live.get("domain", "")
    refs = [r for r in (m.get("refs") or []) if isinstance(r, str)]
    norm = lambda d: d or "_desktop"
    return requested if norm(requested) == norm(home) or requested in refs else ""


# 引擎的 TUI 形态 2026-08-20 退场。这里原来有一大段:_term_open(按引擎描述符拼 argv、
# 起 PTY、归域、捕获 uuid)、_auth_term_open(引擎原生 login TUI)、可写根的合并
# (_merge_access_dirs / _codex_access_dirs,拼成 codex 的 --add-dir)。
# 会话现在一律由适配器按回合起进程(engine_adapters),工作目录与可写根在那一侧按会话算
# (CliAdapter.session_cwd / CodexAdapter._session_roots);登录改为开一个普通 shell
# 跑引擎自己的登录命令(前端 openLoginTUI → /workbench/shell)。
# 留下来的只有工具终端这一种 PTY:shell(下面这个)、登录、自更新。


def _shell_term_open(cwd=None, cols=None, rows=None, init=None, command=None, setup_task_id=None):
    """临时 shell 终端:在 cwd(默认 REPO_ROOT)起登录 shell($SHELL),嵌进 app 的 xterm 执行命令。
    task=True 不进 Vibing 列;每次新开(shell 无 resume);退出由 termpty on_close 关 WS,前端据此收起。
    init:普通终端的预填输入。command:按钮已确认的固定命令，直接作为进程参数执行，
    不加载用户交互 shell 的启动插件；PTY 仍支持命令自身的密码与授权输入。"""
    import oscompat
    env = dict(os.environ)
    env.pop('DESKTRON_SETUP_TASK_ID', None)
    if setup_task_id is not None:
        import re
        if command is None or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', setup_task_id):
            return None
        env['DESKTRON_SETUP_TASK_ID'] = setup_task_id
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    if command is not None:
        if not isinstance(command, str) or not command.strip() or init:
            return None
        # 继承 Desk 的工具 PATH，但不执行 BASH_ENV 或登录 shell 的交互启动逻辑。
        env.pop("BASH_ENV", None)
        env.pop("ENV", None)
        # 先等终端附着，再启动任务。否则快速失败的命令会在 WebSocket 连上前退出，
        # 连同报错输出一起消失。命令通过 argv 传递，启动信号只是一行空输入。
        if oscompat.IS_WINDOWS:
            # PowerShell:先读一行启动信号，再执行命令。命令经环境变量传入，不拼进脚本文本。
            policy_script = ""
            if env.get("DESKTRON_NETWORK_MODE") in ("system", "direct", "manual"):
                import network_proxy
                env["DESKTRON_NETWORK_SCRIPT"] = os.path.join(os.path.dirname(network_proxy.__file__), "network-proxy.ps1")
                policy_script = ("try { & $env:DESKTRON_NETWORK_SCRIPT } "
                                 "catch { Write-Error 'Desk 网络代理初始化失败；未执行安装命令。'; exit 1 }; ")
                # winget has a separate proxy option; npm/curl follow environment variables.
                if command.startswith("winget "):
                    if env.get("HTTPS_PROXY"):
                        command += ' --proxy "$env:HTTPS_PROXY"'
                    else:
                        command += " --no-proxy"
            env["DESKTRON_SETUP_COMMAND"] = command
            argv = oscompat.command_shell_argv(
                "[void][Console]::In.ReadLine(); " + policy_script
                + "Invoke-Expression $env:DESKTRON_SETUP_COMMAND; exit $LASTEXITCODE")
        else:
            argv = ["/bin/bash", "--noprofile", "--norc", "-c",
                    'IFS= read -r _desk_start && exec /bin/bash --noprofile --norc -c "$1"',
                    "desk-command", command]
    else:
        argv = oscompat.interactive_shell_argv()
    base = cwd if (cwd and os.path.isdir(cwd)) else REPO_ROOT
    cols = int(cols) if cols else 80
    rows = int(rows) if rows else 24
    if cols < 20 or rows < 5:
        cols, rows = 80, 24
    try:
        sid, _pid, _created = termpty.pty_open(argv, env, base, None, cols=cols, rows=rows)
    except Exception:
        return None
    sess = {"id": sid, "engine": "shell", "domain": "", "title": "终端",
            "created": int(time.time()), "task": True, "codex_uuid": None}
    if command is not None:
        sess["command_pending"] = True
    with _TERMS_LOCK:
        _TERMS[sid] = sess
    if init and isinstance(init, str):
        # 等登录 shell 把 rc 输出与提示符画完再写,否则初始输入会混在 rc 输出里。
        import threading
        def _prefill():
            time.sleep(0.8)
            try: termpty.pty_input(sid, init.encode("utf-8", "ignore"))
            except Exception: pass
        threading.Thread(target=_prefill, daemon=True).start()
    return sess


# _codex_capture / _cursor_capture 随 TUI 一起撤:那是「新建的 TUI 会话自生成 id,
# 事后从落盘记录里捞真 uuid」的补丁。GUI 这条路由引擎在首帧直接报 id,适配器当场登记
# (engine_adapters._set_engine_sid),不需要事后捕获。


def _codex_del_rollout(u, engine="codex"):
    """删某个 codex 会话的 rollout 文件(休眠列表是 rollout 驱动的,不删会重新冒出来)。"""
    if not u:
        return
    import glob as _glob
    for p in (p for base in _rollout_bases(engine)
              for p in _glob.glob(base + f"/**/rollout-*-{u}.jsonl", recursive=True)):
        try: os.remove(p)
        except Exception: pass
    _CODEX_CACHE.setdefault(engine, {"t": 0.0, "v": []})["t"] = 0.0   # 让列表立刻重扫,不等 5s 缓存


def _cursor_del_transcript(u):
    """删某个 cursor-agent 会话的 transcript 目录(休眠列表 transcript 驱动,不删会重新冒出来)。
    只删 agent-transcripts/<u>/ 这一层;~/.cursor/chats 下的 store.db 留给 cursor 自己管。"""
    if not u:
        return
    import shutil as _shutil
    for wd in _workspace_dirs():
        d = os.path.join(_cursor_transcripts_dir(wd), u)
        if os.path.isdir(d):
            try: _shutil.rmtree(d)
            except Exception: pass
    _CURSOR_CACHE["t"] = 0.0   # 让列表立刻重扫,不等 5s 缓存


# _codex_live_uuid / _codex_restart_target / _codex_root_restart_worker /
# _term_refresh_codex_dirs 随 TUI 一起撤。前者是「捕获窗口错过时按工作区+时间猜 uuid」,
# 后三者是「工作流 repos[] 变了就等回合结束、原位 resume 一次」——因为 codex 的
# --add-dir 只在启动时读一次。GUI 这条路每回合现算 runtimeWorkspaceRoots,下一轮即生效,
# 不必重启,也就没有猜 uuid 的场合。


def _term_kill(sid, keep=False):
    """keep=True:关闭(停进程,保留 transcript/rollout/serve 会话,可 resume 找回);keep=False:删除
    (连记录一并删,不可恢复)。codex/claude 关闭删 sidecar meta(靠落盘 cwd 仍能从工作区
    resume 列找回);cursor-agent 关闭保留 meta(归域只能靠 meta,见下),以休眠态留在原工作区。"""
    with _TERMS_LOCK:
        sess = _TERMS.pop(sid, None)
    meta = _term_meta_read()
    eng = (meta.get(sid) or {}).get("engine")
    # 停 PTY 进程:不以 _TERMS 命中为前提——记录漂移(重启补登记失败等)时 _PTY 里可能仍有活进程,
    # 只删记录不杀进程会留永久孤儿(UI 再无入口管到它)。pty_kill 对不存在的 sid 安全返回 False。
    termpty.pty_kill(sid)
    _is_uuid = bool(re.fullmatch(r"[0-9a-fA-F-]{36}", sid or ""))
    # codex rollout:live 的 id 是内部序号 t{seq},真身在 sess["codex_uuid"];resume 的 live id 即 uuid;休眠 sid 即 uuid。
    cu = None
    route_eng = (sess.get("engine") if sess else None) or eng
    if route_eng in ("codex", "cursor-agent"):
        cu = (sess.get("codex_uuid") if sess else None) or (sid if _is_uuid else None)
    elif route_eng is None and _is_uuid:
        # 重启后 meta 丢、无 sess:codex(v7)/cursor(v4)uuid 同形,靠落盘位置归属判定引擎。
        import glob as _glob2
        for _e in ("codex",):
            if _glob2.glob(_rollout_base(_e) + f"/**/rollout-*-{sid}.jsonl", recursive=True):
                route_eng = _e; cu = sid; break
        if cu is None and _cursor_session_by_id(sid):
            route_eng = "cursor-agent"; cu = sid
        if cu is None and sid[14] == "7":
            route_eng = "codex"; cu = sid
    # 关闭(keep)一律只打 closed 标记,不删 meta —— _term_persisted 跳过 closed(会话从内联列表消失,
    # 与原先删 meta 的观感一致),_dir_sessions 仍按 meta 域补扫到它(「找回」可原地续接);删除(非 keep)
    # 才连 meta + transcript 清。
    # 原来只有 cursor-agent 这么做(它的归域只能靠 meta),其余引擎「落盘 cwd 可靠」所以直接删 meta。
    # 引用模型落地后这条前提不成立了:分组(domain)与工作流引用(refs)只存在于 meta,cwd 推不出来。
    # 删了 meta 再被扫回来,会话就成了无归属的散会话——实测从任务里关一段,它带着任务绑定跑进了聊天组。
    _mark_closed = keep
    if cu and not keep:
        if route_eng == "cursor-agent":
            _cursor_del_transcript(cu)   # 关闭(keep)保留 transcript,可 resume;删除才清
        else:
            _codex_del_rollout(cu, route_eng or "codex")   # 关闭(keep)保留 rollout,可 resume;删除才清
    if not keep:   # 彻底删除:连用户手改的标题一并清,免留孤儿条目(关闭 keep 保留,可 resume 找回)
        _term_set_title(sid, "")
        if cu:
            _term_set_title(cu, "")
        try:   # 自动命名那一层同样清掉;它按两个 id 各写了一条,两个都传进去
            from session_autotitle import drop_auto_title
            drop_auto_title(sid, cu)
        except Exception:
            pass
    def update_closed(current):
        changed_now = False
        if _mark_closed:
            for key in (sid, cu):
                if key and key in current and not current[key].get("closed"):
                    current[key] = {**current[key], "closed": True}
                    changed_now = True
        else:
            for key in (sid, cu):
                if key and current.pop(key, None) is not None:
                    changed_now = True
        return changed_now
    _term_meta_mutate(update_closed)
    # 删除态(非 keep)的其余引擎:claude 删 transcript(codex 已在上面处理)。
    # 不看 sess:live 会话上面已 pty_kill,keep=0 同样要清记录(否则进程杀了、transcript 还在,刷新又冒出来)。
    if not cu and not keep:
        if _is_uuid and route_eng not in ("cursor-agent", "pi"):
            # claude transcript:先按各工作目录的编码删。记录的目录名由会话创建时的 cwd 定死、
            # --resume 之后不迁移,所以这一圈可能一个也删不到;再按 id 精确命中兜底
            # (uuid 全等 + 直属 ~/.claude/projects 子目录,见 _claude_transcript_hits),
            # 否则进程杀了、记录还在,刷新一次会话又冒出来。
            for wd in _workspace_dirs():
                try: os.remove(_claude_path(sid, wd))
                except Exception: pass
            for _p in _claude_transcript_hits(sid):
                try: os.remove(_p)
                except OSError: pass
