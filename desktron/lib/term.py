"""终端 / 会话层:三引擎会话枚举、工作态(busy/unread)观测、PTY 会话开 / 关 / 列表 / 续接。

PTY 进程在 ui_server 进程内 pty.fork(见 termpty),本层经 termpty 开/关/列;本层维护会话元数据
(_TERMS)与工作态观测(_OC/_CODEX/_TERM_* 状态)。依赖 deskcfg + termpty + engine(_adapter)
+ domains(域目录 / cwd 归属),单向向下。ui_server 重启会断会话(可 --resume 找回)。
"""
import os, json, re, time, subprocess, threading, atexit

from deskcfg import REPO_ROOT, DATA_HOME
import termpty
from engine import _adapter, _shepherd, _shepherd_trial, GUI_ENGINES
from domains import (_domain_for_cwd, _ensure_domain_dir, _workspace_dirs, _domain_dir,
                     _repo_items)


# ---- 分册装载(2026-08-27 分拆) --------------------------------------------------
# 三个分册按原文件段序 exec 进本模块命名空间:与拆前同一个 globals,同一个执行序。
# 为什么不是 import:term 的猴补面很宽(测试补 term.<名> 约 17 个,mock.patch.object
# 与直接赋值都有),调用方遍布各段——import 胶水是加载期绑定,补在门面上传不到分册;
# 单命名空间保住「改 term.X 全体生效」这条从单文件时代就成立的契约。
# 分册文件头有同样的说明:它们不是独立模块,别单独 import。
import os as _os
for _part in ("term_titles.py", "term_records.py", "term_sessions.py"):
    _p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), _part)
    with open(_p, encoding="utf-8") as _f:
        exec(compile(_f.read(), _p, "exec"), globals())
del _os, _part, _p, _f


def _term_list():
    """活着的工具终端(shell / 登录 / 自更新)。

    TUI 退场前这里还要给活着的引擎 PTY 补标题与归域:claude 取 OSC 会话名、codex 家族按
    rollout 的 cwd 反查工作区、cursor 从 transcript 重取——那些都随 TUI 一起没了。
    引擎会话现在只有一种形态,列在 _term_persisted 那一侧(它读适配器索引与引擎落盘记录)。
    """
    live = _live_map()
    with _TERMS_LOCK:
        ss = sorted(_TERMS.values(), key=lambda x: x["created"])
    now = time.time()
    out, dead = [], []
    for s in ss:
        lr = live.get(s["id"])
        if lr is None:
            dead.append(s["id"])   # 进程内没有了=已退出
            continue
        title = _display_title(s["id"], s["engine"], s)
        busy, unread = _term_busy_unread(s, lr, now)
        out.append({"id": s["id"], "engine": s["engine"], "title": title,
                    "domain": s.get("domain", ""), "alive": True,
                    "task": bool(s.get("task")), "time": s["created"] * 1000,
                    "busy": busy, "unread": unread})
    if dead:
        with _TERMS_LOCK:
            for d in dead:
                _TERMS.pop(d, None)
    return out


# 子代理在跑:侧栏那颗点原来只看 busy(会话主线在不在跑)。异步派出的子代理活过派它出来的
# 那个回合——主线已经答完、它还在后台干活,那段时间点是不闪的,而那时确实有活在跑。
# 判据不另造一套,直接问适配器的 running_agents(内部就是 subagents() 那一份)。
#
# 这一趟不摆在请求里算。会话列表是侧栏每几秒拉一次的接口,当前一趟约 20 毫秒(2026-08-14
# 实测,32 条会话);逐会话算一遍子代理,冷起要 4.7 秒,之后每趟 70~130 毫秒,而且一段会话
# 每新派出一个子代理,主记录就要整份重扫一次(实测 0.8 秒,最大那份 4 秒)。所以请求只贴
# 上一次算出的数,重算交给后台一条线程,过期了才起——晚一拍是一颗点晚一拍开始呼吸,
# 而摆进请求里是整条侧栏跟着卡。
#
# 算谁也收着:子代理活在派它出来的那个引擎进程里,进程没了它必然跟着没(同 _agent_status
# 里进程实例那一维)。所以只问进程还在的会话——活终端、回合正在跑的、以及握着常驻进程的
# GUI 会话。本机 32 条里符合的 7 条。
_AGENTS_RUN = {}          # 引擎会话 uuid -> (在跑的子代理个数, 算出的时刻)
_AGENTS_LOCK = threading.Lock()
_AGENTS_TTL = 4.0         # 缓存新鲜期(秒),与侧栏的轮询节奏同量级
_AGENTS_WORKING = False   # 后台同一时刻只跑一趟


def _agents_targets(rows):
    """要算子代理的那些会话行 → [(引擎, 引擎会话 uuid)]。"""
    out = []
    for r in rows:
        if r.get("engine") not in GUI_ENGINES:
            continue   # 别的引擎没有子代理这一维,_adapter 也不认它
        # Shepherd rows expose neither native process residency nor child
        # lifetime. A completed parent can still own active native/Pi children.
        if not (r.get("shepherd_mode") or r.get("alive") or r.get("busy") or r.get("resident")):
            continue   # Ordinary adapters expose their own process residency.
        engine = ("shepherd_trial" if r.get("shepherd_trial") else "shepherd") if r.get("shepherd_mode") else r["engine"]
        out.append((engine, r.get("resume_id") or r["id"]))
    return out


def _agents_adapter(engine):
    if engine == "shepherd_trial":
        return _shepherd_trial()
    if engine == "shepherd":
        return _shepherd()
    return _adapter(engine)


def _agents_worker(items):
    global _AGENTS_WORKING
    try:
        for eng, esid in items:
            try:
                n = int(_agents_adapter(eng).running_agents(esid) or 0)
            except Exception:
                n = 0
            with _AGENTS_LOCK:
                _AGENTS_RUN[esid] = (n, time.time())
    finally:
        with _AGENTS_LOCK:
            _AGENTS_WORKING = False


def _term_agents_annotate(rows):
    """给会话行补上 agents:此刻有几个子代理在跑,没有就不带这个字段。

    读的是后台算好的那份缓存,请求本身不扫任何记录。缓存过期时顺带起一趟后台重算,
    所以刚开机第一次拉到的是空的,下一拍才有数。"""
    global _AGENTS_WORKING
    targets = _agents_targets(rows)
    live = {esid for _, esid in targets}
    now = time.time()
    stale = False
    with _AGENTS_LOCK:
        for esid in list(_AGENTS_RUN):
            if esid not in live:
                del _AGENTS_RUN[esid]   # 进程没了的那些不留数,免得下次又活过来时先闪一下旧值
        for _, esid in targets:
            hit = _AGENTS_RUN.get(esid)
            if not hit or now - hit[1] > _AGENTS_TTL:
                stale = True
        counts = {esid: n for esid, (n, _t) in _AGENTS_RUN.items()}
        start = stale and not _AGENTS_WORKING
        if start:
            _AGENTS_WORKING = True
    for r in rows:
        n = counts.get(r.get("resume_id") or r.get("id"))
        if n:
            r["agents"] = n
    if start:
        threading.Thread(target=_agents_worker, args=(targets,), daemon=True).start()
    return rows


def _gui_resident_sids():
    """握着常驻进程的 GUI 会话。与 _gui_live_sids 的区别:那个是「此刻在跑」,
    这个是「进程还在,下一回合不必冷起」。侧栏据它决定要不要给「关闭进程」这一项。"""
    out = set()
    for name in GUI_ENGINES:
        try:
            out |= _adapter(name).resident_engine_sids()
        except Exception:
            pass
    return out


def release_gui_resident(esid):
    """摘掉某段 GUI 会话的常驻进程。哪个引擎握着就由哪个放,记录一律不动。"""
    for name in GUI_ENGINES:
        try:
            if _adapter(name).release_resident(esid):
                return True
        except Exception:
            pass
    return False


def _gui_live_sids():
    """GUI 那侧在跑的引擎会话。每个引擎各有一份适配器实例,取并集——侧栏不分引擎,
    只问「这段会话此刻在不在动」;单问 claude 会让别的引擎的 GUI 回合永远显示空闲。"""
    out = set()
    for name in GUI_ENGINES:
        try:
            out |= _adapter(name).live_engine_sids()
        except Exception:
            pass
    return out


def _gui_index_row(eng, engine_sid, reverse=None):
    """没有可扫的引擎侧记录时,侧栏那一行的来源是适配器自己的索引。

    给没有离线 store 的 GUI 适配器保留通用兜底。侧栏要的标题与时刻都在适配器索引里；
    首轮答完 engine_sid 一落盘,_gui_unstarted 就不再列它,这里负责接上。
    """
    if reverse is None:
        try:
            reverse = {rec.get("engine_sid"): (sid, rec)
                       for sid, rec in (_adapter(eng).index or {}).items()
                       if isinstance(rec, dict) and rec.get("engine_sid")}
        except Exception:
            return None
    hit = reverse.get(engine_sid)
    if not hit:
        return None
    _sid, rec = hit
    t = rec.get("time") if isinstance(rec.get("time"), dict) else {}
    return {"id": engine_sid, "title": rec.get("title") or "新会话",
            "title_user": bool(rec.get("title_user")),
            "desk_name_fallback": rec.get("desk_name_fallback"),
            "time": int(t.get("updated") or t.get("created") or 0)}


def _gui_session_id(eng, engine_sid, reverse=None):
    """引擎锚点对应的 Desk 会话号(adp_)。

    会话页用适配器索引的 id，侧栏落盘行历史上只带引擎 id。两者不一起返回时，
    页面明明开着 A，侧栏却没有任何一行能与它判等。映射本来就稳定保存在索引里，
    在列表契约中显式带回，前端无需按标题或时间猜。
    """
    if reverse is None:
        try:
            reverse = {rec.get("engine_sid"): (sid, rec)
                       for sid, rec in (_adapter(eng).index or {}).items()
                       if isinstance(rec, dict) and rec.get("engine_sid")}
        except Exception:
            return ""
    hit = reverse.get(engine_sid)
    return str(hit[0] or "") if hit else ""


_TERM_PERSISTED_LOCK = threading.RLock()   # 后台预热与首轮轮询可能撞在一起；整树只扫一遍
_TERM_CATALOG_REVISION_LOCK = threading.Lock()  # 写路径只碰代次，绝不反向等待目录构建锁
_TERM_DROPPED = set()   # 已记过一次的落空归属;扫描频繁,同一条不重复写日志
_TERM_CATALOG = {"revision": 0, "built_revision": -1, "built_at": 0.0,
                 "stamp": None, "rows": [], "issues": set()}
_TERM_CATALOG_PROBE_SECONDS = 5.0
_TERM_SCAN_CONTEXT = threading.local()
_TERM_DYNAMIC_FIELDS = ("busy", "unread", "resident", "agents",
                        "completion_generation", "completed_generation")


def _term_catalog_invalidate():
    """Invalidate without waiting on a scan that may itself read metadata/titles."""
    with _TERM_CATALOG_REVISION_LOCK:
        _TERM_CATALOG["revision"] += 1


def _term_catalog_stamp():
    # Function identities make monkey-patched tests and embedders independent.
    # Production changes are driven by explicit invalidation; the bounded probe
    # also discovers native transcript/rollout writes made outside Desk.
    return (DATA_HOME, id(_adapter), id(_term_meta_read), id(_claude_session_by_id),
            id(_codex_sessions), id(_cursor_sessions), id(_display_title))


def _term_note_dropped(sid, eng):
    """meta 里有归属、底层记录取不到,这一条就从聊天区消失了。以前是一句 continue,
    人看到的是「会话不见了」而没有任何线索。留一行,下次至少能对上是哪一条。"""
    if sid in _TERM_DROPPED:
        return
    _TERM_DROPPED.add(sid)
    try:
        import deskcfg
        deskcfg.log("term", "侧栏丢弃会话 %s(engine=%s):meta 有归属但引擎侧记录取不到" % (sid, eng or "?"))
    except Exception:
        pass


def _term_persisted_scan(issues=None, *, static=False):
    """已落盘但当前无活进程的终端会话(dormant):点开即续接拉活。各引擎一律 meta 驱动——
    只列工作台自己开过的会话(claude transcript / codex rollout 各按 id 取标题);
    工作台外手敲开的会话不进 Vibing,走 resume 入口(_dir_sessions 按 cwd 列)找回。task 归执行记录区。"""
    live = set() if static else set(_live_map().keys())
    with _TERMS_LOCK:
        # codex live 的 uuid:resume 的 id 即 uuid,新建的捕获在 codex_uuid。用于从休眠列排除当前活的。
        _live_engs = ("codex", "cursor-agent")
        live_codex = {s.get("codex_uuid") for s in _TERMS.values() if s.get("engine") in _live_engs and s["id"] in live}
        live_codex |= {s["id"] for s in _TERMS.values() if s.get("engine") in _live_engs and s["id"] in live}
        live_codex.discard(None)
    meta = dict(_term_meta_read())
    adapter_indexes, adapter_reverse = {}, {}
    # 适配器索引先于侧栏 meta 落盘。即使登记中断，刷新仍保留已启动的会话。
    # 显式关闭/任务标记以 meta 为准，不能因索引仍在而重新展示。
    for engine in GUI_ENGINES:
        try:
            index = _adapter(engine).index
            if not isinstance(index, dict) or any(not isinstance(row, dict) for row in index.values()):
                raise ValueError("invalid session index")
            records = list(index.items())
            adapter_indexes[engine] = dict(records)
            adapter_reverse[engine] = {rec.get("engine_sid"): (sid, rec)
                                       for sid, rec in records
                                       if isinstance(rec, dict) and rec.get("engine_sid")}
        except Exception:
            if issues is not None:
                issues.add((engine, "index_unavailable"))
            continue
        for adapter_sid, rec in records:
            if not isinstance(rec, dict) or not rec.get("engine_sid"):
                continue
            if not rec.get("engine_started", True):
                continue
            engine_sid = rec["engine_sid"]
            alias = meta.get(adapter_sid) if adapter_sid != engine_sid else None
            if alias and alias.get("engine", engine) == engine:
                # 登记只做了一半时，adp_ 与引擎 UUID 仍可能同时在 meta 中。
                meta.pop(adapter_sid)
                meta[engine_sid] = {**rec, "engine": engine, **alias, **meta.get(engine_sid, {})}
            else:
                meta.setdefault(engine_sid, {**rec, "engine": engine})
    _TERM_SCAN_CONTEXT.reverse = adapter_reverse
    _gui_live = set() if static else _gui_live_sids()
    _gui_resident = set() if static else _gui_resident_sids()
    cur_ids = None   # cursor uuid 集合(惰性建一次;_cursor_sessions 自带 5s 缓存)
    cx_idx_cache = {}
    def _cx_idx(eng):
        if eng not in cx_idx_cache:
            cx_idx_cache[eng] = {s2["id"]: s2 for s2 in _codex_sessions(eng)}
        return cx_idx_cache[eng]
    out = []
    for sid, m in meta.items():
        if sid in live or m.get("task"):
            continue
        if m.get("closed"):
            continue   # cursor 关闭态:留归属供「找回」补扫,但不进内联休眠列表(见 _term_kill)
        eng = m.get("engine")
        if re.fullmatch(r"[0-9a-fA-F-]{36}", sid or ""):
            # uuid 归属以落盘记录为准纠偏 meta(曾把 cursor/codex uuid 误标 claude;迁移残留也可能错标)。
            # 走循环里本就要建的各引擎缓存索引判,不对每个 uuid 跑递归 glob——否则切引擎刷列表时
            # 会对每条 uuid 各跑两次整树 glob(几十条即明显卡顿)。
            if sid in _cx_idx("codex"):
                eng = "codex"
            else:
                if cur_ids is None:
                    cur_ids = {x["id"] for x in _cursor_sessions()}
                if sid in cur_ids:
                    eng = "cursor-agent"
                elif not eng:
                    eng = "claude"
        if eng == "claude":
            s = _claude_session_by_id(sid)
        elif eng == "codex":
            if sid in live_codex:
                continue   # 此刻活着(以内部序号在列),不重复
            s = _cx_idx(eng).get(sid)
        elif eng == "cursor-agent":
            if sid in live_codex:
                continue
            s = _cursor_session_by_id(sid)
        elif eng in GUI_ENGINES:
            # 上面几条按引擎侧落盘记录取行都没接住,但它确实有 GUI 适配器；
            # 行改从适配器索引取,给未来没有离线 store 的引擎兜底。
            s = _gui_index_row(eng, sid, adapter_reverse.get(eng))
        else:
            continue
        record_status = ""
        if not s and eng in set(GUI_ENGINES) | {"claude", "codex", "cursor-agent"}:
            s = (_gui_index_row(eng, sid, adapter_reverse.get(eng))
                 or {"id": sid, "title": m.get("title") or sid[:8], "time": 0})
            record_status = "unavailable"
            key = (eng, sid, record_status)
            if key not in _TERM_DROPPED:
                _TERM_DROPPED.add(key)
                import deskcfg
                deskcfg.log("term", "会话 %s(engine=%s):原生记录暂不可读，保留侧栏入口" % (sid, eng))
        if not s:
            _term_note_dropped(sid, eng)
            continue   # meta 有记录但底层记录已删,跳过
        dom = m.get("domain", "")
        if eng == "codex":
            dom = _codex_list_domain(dom, s.get("cwd", ""), dom)   # 普通会话按 cwd;隔离子区/dw 显式归属按 meta
        # cursor-agent:transcript 按 git 仓库根归并(子工作区折叠进 REPO_ROOT 的 project),落盘 cwd ≠ 启动 cwd,
        # 不能据它归域(会把子工作区会话误判成 _desktop → 从本工作区列表消失);信 meta domain(_cursor_capture 按启动域写)。
        # id 是引擎侧锚点，HTML 会话页则以适配器自己的 adp_ id 为地址。两者描述的是
        # 同一段会话；把后者作为别名带给前端，刷新/轮询重画后仍能恢复正确选中态。
        # 自动命名那一层按两个 id 各写了一条,取标题时一并把别名传进去(见 _display_title)。
        # 这一句每行扫一遍适配器索引,算过就存着,不要在取标题时再算一次。
        session_id = (_gui_session_id(eng, sid, adapter_reverse.get(eng))
                      if eng in GUI_ENGINES else "")
        item = {"id": sid, "engine": eng,
                "title": _display_title(sid, eng, s, session_id),
                "domain": dom, "alive": False, "task": False, "time": s["time"]}
        if record_status:
            item["record_status"] = record_status
        if session_id and session_id != sid:
            item["session_id"] = session_id
        # GUI 会话在这张表里是「休眠」的——它没有 PTY,进程是适配器按回合起的。
        # 但它照样在干活,侧栏不该一直显示成静止的灰点。工作态由适配器直报。
        if eng in GUI_ENGINES and not static:
            status = _gui_read_status(_adapter(eng), session_id) if session_id else None
            if status is not None:
                item.update(status)
            else:
                b_, u_ = _gui_busy_unread(sid, _gui_live)
                if b_ or u_:
                    item["busy"], item["unread"] = b_, u_
            if sid in _gui_resident:
                item["resident"] = True   # 握着常驻进程:菜单据此给「关闭进程」
        refs = [r for r in (m.get("refs") or []) if isinstance(r, str) and r]
        if refs:
            item["refs"] = refs   # 引用不是归属:会话的家仍是 domain,工作流只是引用它(同任务↔会话)
        cwd = s.get("cwd") or _transcript_cwd(eng, sid)   # 会话所在目录:界面在标题后标来源
        if cwd:
            item["cwd"] = cwd
        branch = _session_branch_label({"domain": dom}, dom, s)
        if branch:
            item["branch"] = branch
        out.append(item)
    out += _gui_unstarted(out, live, issues, adapter_indexes=adapter_indexes, static=static)
    out += _shepherd_sessions(out, live, issues, static=static)
    out.sort(key=lambda x: -x["time"])
    _TERM_SCAN_CONTEXT.reverse = None
    return out


def _term_dynamic_overlay(rows, issues=None):
    """Copy static catalog rows and overlay current process/read state."""
    live, resident = _gui_live_sids(), _gui_resident_sids()
    terminal_live = set(_live_map())
    out = []
    for source in rows:
        item = {key: value for key, value in source.items() if key not in _TERM_DYNAMIC_FIELDS}
        if item.get("id") in terminal_live:
            continue
        if item.get("shepherd_mode"):
            try:
                adapter = _shepherd_trial() if item.get("shepherd_trial") else _shepherd()
                status = _gui_read_status(adapter, item["id"])
            except Exception:
                status = None
                if issues is not None:
                    issues.add(("activity", "status_unavailable"))
        elif item.get("engine") in GUI_ENGINES:
            try:
                adapter = _adapter(item["engine"])
                session_id = item.get("session_id")
                if session_id or str(item.get("id") or "").startswith("adp_"):
                    status = _gui_read_status(adapter, session_id or item["id"])
                else:
                    status = None
            except Exception:
                status = None
                if issues is not None:
                    issues.add(("activity", "status_unavailable"))
            if status is None:
                busy, unread = _gui_busy_unread(item["id"], live)
                status = {"busy": busy, "unread": unread}
            if item["id"] in resident:
                item["resident"] = True
        else:
            status = None
        if status is not None:
            item.update(status)
        out.append(item)
    return out


def _term_persisted(issues=None):
    """Return the versioned static catalog with per-request dynamic state."""
    now, stamp = time.monotonic(), _term_catalog_stamp()
    with _TERM_PERSISTED_LOCK:
        with _TERM_CATALOG_REVISION_LOCK:
            build_revision = _TERM_CATALOG["revision"]
        stale = (_TERM_CATALOG["built_revision"] != build_revision
                 or _TERM_CATALOG["stamp"] != stamp or _TERM_CATALOG["issues"]
                 or now - _TERM_CATALOG["built_at"] >= _TERM_CATALOG_PROBE_SECONDS)
        if stale:
            build_issues = set()
            try:
                rows = _term_persisted_scan(build_issues, static=True)
            finally:
                _TERM_SCAN_CONTEXT.reverse = None
            # Preserve the revision captured before the scan: a concurrent write
            # increments it and therefore forces the next request to rebuild.
            _TERM_CATALOG.update(rows=rows, issues=build_issues, stamp=stamp,
                                 built_at=now, built_revision=build_revision)
        rows = list(_TERM_CATALOG["rows"])
        cached_issues = set(_TERM_CATALOG["issues"])
    if issues is not None:
        issues.update(cached_issues)
    return _term_dynamic_overlay(rows, issues)


def _term_snapshot():
    """列表完整性与数据来源。异常不变成完整空表；不输出异常正文或真实路径。"""
    import hashlib
    import socket
    source = hashlib.sha256((socket.gethostname() + "\0" + os.path.realpath(DATA_HOME)).encode()).hexdigest()
    issues, rows = set(), []
    try:
        rows.extend(_term_list())
    except Exception:
        issues.add(("terminals", "list_unavailable"))
    try:
        rows.extend(_term_persisted(issues))
    except Exception:
        issues.add(("sessions", "index_unavailable"))
    try:
        rows = _term_agents_annotate(rows)
    except Exception:
        issues.add(("activity", "status_unavailable"))
    return {"sessions": rows, "complete": not issues, "source": source,
            "issues": [{"source": source, "code": code} for source, code in sorted(issues)]}


def _shepherd_sessions(listed, live, issues=None, static=False):
    return (_shepherd_space_sessions(listed, live, issues, static=static)
            + _shepherd_space_sessions(listed, live, issues, trial=True, static=static))


def _shepherd_space_sessions(listed, live, issues=None, trial=False, static=False):
    """把 Shepherd 的公开 Conversation 加入统一侧栏快照。

    Shepherd 故意没有公开 engine_sid；隐藏 Binding 只存在于协调器。侧栏行以公开
    adp_ SID 为唯一身份，并以 shepherd_mode 标记独立书桌归属。
    """
    out = []
    try:
        adapter = _shepherd_trial() if trial else _shepherd()
        records = adapter.list_sessions() or []
    except Exception:
        if issues is not None:
            issues.add(("shepherd", "index_unavailable"))
        return out
    known = {str(x.get("session_id") or x.get("id") or "") for x in listed}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        sid = str(rec.get("id") or "")
        if not sid or sid in known or rec.get("task"):
            continue
        t = rec.get("time") if isinstance(rec.get("time"), dict) else {}
        item = {"id": sid, "session_id": sid,
                "engine": str(rec.get("engine") or ""), "shepherd_mode": True,
                "shepherd_trial": trial,
                "title": _display_title(sid, "shepherd", rec) or "新会话",
                "domain": rec.get("domain") or "", "alive": sid in live,
                "task": False, "time": int(t.get("updated") or t.get("created") or 0)}
        if not static:
            status = _gui_read_status(adapter, sid)
            if status is not None:
                item.update(status)
        refs = [r for r in (rec.get("refs") or []) if isinstance(r, str) and r]
        if refs:
            item["refs"] = refs
        out.append(item)
    return out


def _gui_unstarted(listed, live, issues=None, adapter_indexes=None, static=False):
    """刚建、还没跑过第一回合的 GUI 会话。

    这类会话在两处都还不存在:引擎要到首帧才报 session_id,适配器据此才登记 meta
    (engine_adapters._set_engine_sid);claude 也要到那时才落 transcript,而上面那圈
    正是靠「meta 有登记 + 底层记录读得到」取会话的。两个条件都不满足,于是在工作流里
    新建一段会话后,侧栏要等到聊过一轮、且手动刷新之后才看得见它(2026-08-17 用户实见)。

    这里绕开那两个前提,直接读适配器自己的索引:create_session 当场就往里写了
    id/title/domain/created。只补两处都没有的那些,已列出的不重复。
    """
    out = []
    listed_ids = {str(x.get("session_id") or x.get("id") or "") for x in listed}
    adapter_indexes = adapter_indexes or {}
    for eng in GUI_ENGINES:
        try:
            idx = adapter_indexes.get(eng)
            if idx is None:
                idx = _adapter(eng).index
        except Exception:
            if issues is not None:
                issues.add((eng, "index_unavailable"))
            continue   # 某个引擎不可用不连累其余(同本模块其它按引擎取数处的写法)
        for sid, rec in (idx or {}).items():
            if not isinstance(rec, dict):
                continue
            # 通常拿到 engine_sid 就代表首轮已启动；Pi 能预分配稳定 UUID，另以
            # engine_started=False 表明它仍是一段刚创建、尚未发出首轮的新会话。
            if rec.get("engine_sid") and rec.get("engine_started", True):
                continue
            if rec.get("task"):
                continue   # 执行记录归任务行,不进 Vibing 列(同上面那圈对 meta.task 的处置)
            if sid in live or sid in listed_ids:
                continue
            t = rec.get("time") if isinstance(rec.get("time"), dict) else {}
            # 标题退到「新会话」而不是 id 前缀:这一档会话本来就还没有可取的名字,
            # 摆一串 adp_ 开头的十六进制,读的人认不出那是刚点出来的哪一段。
            # 跑过一轮就有真标题了,那时它走上面那圈,这条不再命中。
            item = {"id": sid, "engine": eng,
                        "title": _display_title(sid, eng, rec) or "新会话",
                        "domain": rec.get("domain") or "", "alive": False, "task": False,
                        "time": int(t.get("created") or 0)}
            if not static:
                status = _gui_read_status(_adapter(eng), sid)
                if status is not None:
                    item.update(status)
            out.append(item)
    return out
