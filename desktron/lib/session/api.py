"""会话领域路由:会话列举/关闭、按 cwd 枚举引擎会话、跨引擎交接、移动/改题、shell 终端。
拆分自 ui_server._workbench;owns() 按路径认领,
handle(h) 内 h 为 Handler 实例,行为与原 if 链一致。"""
import os, re, json, time, urllib.parse
from deskcfg import REPO_ROOT
import termpty
from workbench import _engine_pref, _wb_domains_read, _builtin_domain_ids
from domains import _domain_dir
from term import (_dir_sessions, _live_map, _term_list, _term_persisted, _term_snapshot,
                  _term_agents_annotate,
                  _term_kill, _term_meta_read, _term_meta_write, _term_meta_mutate,
                  _term_move_domain, _resolve_uuid_engine,
                  _session_handoff_prompt, _shell_term_open, SIDEBAR_SERVED)
from session.title_service import rename_session, session_identity as _session_identity


def _delete_session_permanently(h, sid):
    """统一永久删除适配器索引、终端 meta 与引擎原生记录。

    返回 ``(payload, status)`` 表示失败；成功返回 ``None``。先删适配器，确保运行中
    会话的 409 不会破坏本地入口，用户可在原位置重试。
    """
    owner = h._adapter_owning(sid)
    own_sid, identities = _session_identity(owner, sid)
    # 原生存储由引擎描述符负责，不能只依赖旧 PTY 清理器（后者没有 Pi 分支）。
    from engines import get as engine_get
    native_id, store = sid, None
    name = getattr(owner, "name", None)
    if owner is None:
        name = (_term_meta_read().get(sid) or {}).get("engine")
    elif isinstance(getattr(owner, "index", None), dict):
        native_id = (owner.index.get(own_sid) or {}).get("engine_sid")
    if isinstance(name, str):
        spec = engine_get(name)
        store = spec.store if spec else None
    if owner is not None:
        try:
            state = owner.turn_state(own_sid)
            if isinstance(state, dict) and (state.get("state") in ("running", "cancelling")
                                           or state.get("compacting")):
                return {"error": "请等待当前回合结束后再删除", "code": "session_busy"}, 409
            # Codex 释放常驻进程也需要 adp_ ID，不能传原生 thread ID。
            owner.delete_session(own_sid)
        except RuntimeError as exc:
            status = getattr(exc, "status", None)
            if status == 409:
                error = {"error": str(exc)}
                if getattr(exc, "code", None):
                    error["code"] = exc.code
                return error, 409
            return {"error": str(exc) or "删除会话失败"}, 500
        except Exception:
            return {"error": "删除会话失败，请稍后重试"}, 500
    try:
        if store and native_id:
            store.delete(native_id)
        for identity in identities:
            _term_kill(identity, keep=False)
    except (OSError, ValueError):
        return {"error": "清理会话记录失败，请稍后重试"}, 500
    return None


def owns(p, method):
    # /workbench/auth-login(引擎原生 login TUI)2026-08-20 随 TUI 撤:登录改为开一个普通
    # shell 跑引擎自己的命令(前端 openLoginTUI → /workbench/shell),不必每接一个引擎
    # 就在后端补一条它的 login 分支。
    return (p in ("/workbench/sessions", "/workbench/session-handoff", "/workbench/terminals",
                  "/workbench/term/cwd", "/workbench/shell", "/workbench/setup-tasks") or
            p.startswith("/workbench/terminals/") or p.startswith("/workbench/sessions/"))


def handle(h, p, q, method):
    if p == "/workbench/setup-tasks":
        if method != "GET":
            return h._send_json({"error": "method"}, 405)
        from provision import setup_tasks
        try:
            return h._send_json({"tasks": setup_tasks.recent()})
        except (OSError, ValueError):
            return h._send_json({"error": "安装任务记录暂不可读；原文件已保留，请勿重复部署"}, 503)
    if p == "/workbench/sessions":
        # 按工作区 cwd 列引擎原生会话(供 resume / coding 项目展开会话用)
        params = urllib.parse.parse_qs(q)
        did = (params.get("domain") or [""])[0]
        eng = (params.get("engine") or [_engine_pref()])[0]
        wd = _domain_dir(did) if did else REPO_ROOT
        return h._send_json({"sessions": _dir_sessions(eng, wd)})
    if p == "/workbench/session-handoff" and method == "POST":
        # 历史无角色接续:把源记录整理成新线程 seed；源与目标可同引擎,这里不 resume/fork。
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
            sid = (body.get("session_id") or body.get("id") or "").strip()
            src = (body.get("source_engine") or body.get("engine") or "").strip()
            dst = (body.get("target_engine") or _engine_pref() or "").strip()
            assert sid and src and dst
            prompt = _session_handoff_prompt(src, sid, dst)
        except Exception as e:
            return h._send_json({"ok": False, "error": str(e) or "handoff failed"}, 422)
        return h._send_json({"ok": True, "prompt": prompt})
    if p == "/workbench/terminals":
        # 会话清单:活的工具终端(shell / 登录 / 更新)+ 已落盘的会话。前端 Vibing 列即这些。
        # POST 这一档 2026-08-20 随 TUI 一起撤了:建会话走 POST /session(适配器那条路),
        # 引擎不再以嵌入式终端的形态跑。shell 终端仍从 /workbench/shell 开。
        if method == "GET":
            snapshot = _term_snapshot()   # 仍在统一列表层补 agents，不另建一份扫描。
            if snapshot["complete"]:
                SIDEBAR_SERVED.set()
            if urllib.parse.parse_qs(q).get("integrity") == ["1"]:
                return h._send_json(snapshot)
            if not snapshot["complete"]:
                # 旧前端只识别数组：失败应进入保留缓存分支，不能发缺项的成功列表。
                return h._send_json({"error": "会话列表暂未读取完整，请稍后重试",
                                     "code": "session_list_incomplete"}, 503)
            return h._send_json(snapshot["sessions"])
        return h._send_json({"error": "建会话走 POST /session"}, 405)
    if p == "/workbench/term/cwd" and method == "GET":
        # 终端标签实时目录:经 lsof 查 PTY 进程 cwd,给悬浮终端页头部小徽标轮询用。
        qp = urllib.parse.parse_qs(q)
        sid = (qp.get("id") or [""])[0]
        cwd = termpty.pty_cwd(sid) if sid else None
        if cwd:
            home = os.path.expanduser("~")
            if cwd == home or cwd.startswith(home + os.sep):
                cwd = "~" + cwd[len(home):]
        return h._send_json({"cwd": cwd})
    if p == "/workbench/shell" and method == "POST":
        # 临时 shell 终端:悬浮框"+"里的"打开终端",起 $SHELL 嵌进 xterm 执行命令。
        # body.init:可选初始输入。是否执行由调用方决定:不带换行是预填,带换行则自动执行。
        qp = urllib.parse.parse_qs(q)
        jb = {}
        ln = int(h.headers.get("Content-Length", 0))
        if ln:
            try: jb = json.loads(h.rfile.read(ln) or b"{}")
            except Exception: jb = {}
        def _qpi2(name):
            try:
                v = jb.get(name) or (qp.get(name) or [""])[0]
                v = int(v); return v if v > 0 else None
            except Exception:
                return None
        cwd = jb.get("cwd") or (qp.get("cwd") or [""])[0] or None
        init = jb.get("init") if isinstance(jb.get("init"), str) else None
        command = jb.get("command")
        if command is not None and (not isinstance(command, str) or not command.strip() or init):
            return h._send_json({"error": "command 需要非空命令，且不能同时提供 init"}, 400)
        import self_update
        with self_update.admit_work(user_activity=True) as admitted:
            if not admitted:
                return h._send_json({"error": "Desk 正在准备强制更新，请稍后再打开终端"}, 409)
            if jb.get("setup_request_id"):
                if command is None:
                    return h._send_json({"error": "安装任务需要固定命令"}, 400)
                from provision import setup_tasks
                try:
                    task = setup_tasks.start(jb["setup_request_id"], command,
                        lambda: _shell_term_open(cwd, _qpi2("cols"), _qpi2("rows"), command=command,
                                                 setup_task_id=jb["setup_request_id"]))
                    return h._send_json({"id": task.get("terminal_id"), "task": task})
                except (OSError, ValueError, RuntimeError):
                    return h._send_json({"error": "无法确认安装任务状态；请查看安装进度，不要重复执行"}, 409)
            s = _shell_term_open(cwd, _qpi2("cols"), _qpi2("rows"), init=init, command=command)
        return h._send_json({"id": s["id"]}) if s else h._send_json({"error": "open failed"}, 400)
    if p.startswith("/workbench/terminals/") and p.endswith("/domain") and method == "POST":
        # 普通引擎会话按终端 meta 换域并置 pinned；Shepherd 是公开会话门面，只存在于
        # 自己的适配器索引，不能拿公开 adp_ 号去终端 meta 里查。
        sid = p[len("/workbench/terminals/"):-len("/domain")]
        ln = int(h.headers.get("Content-Length", 0))
        try:
            jb = json.loads(h.rfile.read(ln) or b"{}")
        except Exception:
            jb = {}
        dom = str(jb.get("domain") or "").strip()
        reg = {d.get("id") for d in _wb_domains_read()} | set(_builtin_domain_ids()) | {"_desktop"}
        if dom not in reg:
            return h._send_json({"ok": False, "error": f"目标工作区不存在:{dom}"}, 422)
        owner = h._adapter_owning(sid)
        own_sid, _identities = _session_identity(owner, sid)
        indexed = isinstance(getattr(owner, "index", None), dict) and own_sid in owner.index
        if getattr(owner, "name", None) == "shepherd":
            try:
                ok, err = owner.move_session_domain(own_sid, dom)
            except (OSError, ValueError):
                return h._send_json({"ok": False, "error": "保存会话归属失败，请稍后重试"}, 500)
            cd = False
        elif indexed and hasattr(owner, "move_session_domain"):
            old_domain = owner.session_domain(own_sid)
            try:
                ok, err = owner.move_session_domain(own_sid, dom)
            except (OSError, ValueError):
                return h._send_json({"ok": False, "error": "保存会话归属失败，请稍后重试"}, 500)
            if ok:
                try:
                    ok, err, cd = _term_move_domain(sid, dom)
                except (OSError, ValueError):
                    try:
                        rolled_back, _rollback_error = owner.move_session_domain(own_sid, old_domain)
                        if not rolled_back:
                            raise OSError("adapter rollback rejected")
                    except (OSError, ValueError):
                        return h._send_json({"ok": False, "error": "会话归属回滚失败，请刷新后重试"}, 500)
                    return h._send_json({"ok": False, "error": "保存终端归属失败，请稍后重试"}, 500)
                if not ok:
                    try:
                        rolled_back, _rollback_error = owner.move_session_domain(own_sid, old_domain)
                        if not rolled_back:
                            raise OSError("adapter rollback rejected")
                    except (OSError, ValueError):
                        return h._send_json({"ok": False, "error": "会话归属回滚失败，请刷新后重试"}, 500)
            else:
                cd = False
        else:
            ok, err, cd = _term_move_domain(sid, dom)
        if not ok:
            return h._send_json({"ok": False, "error": err}, 422)
        return h._send_json({"ok": True, "cd": cd})
    if p.startswith("/workbench/terminals/") and p.endswith("/release") and method == "POST":
        # 摘掉一段 GUI 会话的常驻进程,记录不动。GUI 会话没有 PTY,「关闭」对它不是删记录,
        # 而是放掉那条跨回合活着的引擎进程——下一回合冷起一次即可。
        sid = p[len("/workbench/terminals/"):-len("/release")]
        from term import release_gui_resident
        return h._send_json({"ok": bool(release_gui_resident(sid))})
    title_prefix = next((prefix for prefix in ("/workbench/sessions/", "/workbench/terminals/")
                         if p.startswith(prefix) and p.endswith("/title")), None)
    if title_prefix and method == "POST":
        sid = urllib.parse.unquote(p[len(title_prefix):-len("/title")])
        try:
            ln = int(h.headers.get("Content-Length", 0))
            jb = json.loads(h.rfile.read(ln) or b"{}")
            if not isinstance(jb, dict) or not isinstance(jb.get("title"), str):
                raise ValueError("title 必须是字符串")
        except (ValueError, TypeError):
            return h._send_json({"error": "会话名称格式错误"}, 400)
        try:
            result = rename_session(h._adapter_owning(sid), sid, jb["title"])
        except (OSError, ValueError):
            return h._send_json({"error": "会话名称保存失败，请重试"}, 500)
        return h._send_json(result)
    if p.startswith("/workbench/terminals/") and p.endswith("/reopen") and method == "POST":
        sid = p[len("/workbench/terminals/"):-len("/reopen")]
        _, identities = _session_identity(h._adapter_owning(sid), sid)
        def reopen(meta):
            changed = False
            for identity in identities:
                if (meta.get(identity) or {}).get("closed"):
                    meta[identity] = {**meta[identity], "closed": False}
                    changed = True
            return changed
        _term_meta_mutate(reopen)
        return h._send_json({"ok": True})
    if p.startswith("/workbench/terminals/") and method == "DELETE":
        _keep = urllib.parse.parse_qs(q).get("keep", ["0"])[0] == "1"   # keep=1:关闭(停进程,留 transcript/rollout 可 resume);否则删除
        sid = p.rsplit("/", 1)[-1]
        if _keep:
            owner = h._adapter_owning(sid)
            own_sid, identities = _session_identity(owner, sid)
            if owner is not None:
                # GUI 回合不在 PTY 表里，关闭必须通知归属适配器，保留历史供恢复。
                try:
                    owner.abort(own_sid, recall=False)
                except Exception:
                    return h._send_json({"error": "关闭会话失败，请稍后重试"}, 500)
            for identity in identities:
                _term_kill(identity, keep=True)
        else:
            error = _delete_session_permanently(h, sid)
            if error is not None:
                return h._send_json(*error)
        return h._send_json({"ok": True})
    return h._send_json({"error": "unknown"}, 404)
