# term.py 的分册(2026-08-27 分拆,specs/2026-08-27-repo-partition-2.md)。
# 不是独立模块:由 term.py 按序 exec 进它自己的命名空间——term 的猴补面很宽
# (测试补 term.<名> 约 17 个,调用方遍布各段),必须保持单命名空间的调用期查找;
# 文件切开只为可读性。名字都解析在 term 模块的 globals 里,别单独 import 这个文件。

_ROLLOUT_DIR = {"codex": "~/.codex/sessions"}
_DESK_HOME = os.path.dirname(DATA_HOME)
# Desk 启动 codex 时注入隔离 CODEX_HOME。sessions 通常软链回全局,
# 但 state_5.sqlite / session_index.jsonl 可能分别留在隔离 home 或全局 home。只查隔离库会把
# 全局 CLI 起过的大 rollout 误判成“无标题”,再进入 rollout 回退扫描。读取时隔离优先、全局兜底。
_STATE_DB = {
    "codex": os.path.join(_DESK_HOME, "codex-home", "state_5.sqlite"),
}
_STATE_DB_GLOBAL = {
    "codex": os.path.expanduser("~/.codex/state_5.sqlite"),
}
_SESSION_INDEX = {
    "codex": os.path.join(_DESK_HOME, "codex-home", "session_index.jsonl"),
}
_SESSION_INDEX_GLOBAL = {
    "codex": os.path.expanduser("~/.codex/session_index.jsonl"),
}
_CODEX_CACHE = {"codex": {"t": 0.0, "v": []}}
_BRANCH_CACHE = {}   # {real_cwd: (time, branch)}; sidebar polling should not shell out for every row.
_TITLE_CACHE = {}   # {path: (mtime, title)}
_CT_CACHE = {}      # {path: (size, title)} —— /rename 的增量扫描游标
_RT_CACHE = {"codex": {}}  # {engine: {path: 增量 rollout 标题状态}}
_RT_CACHE_LOCK = threading.RLock()
_RT_WORK = []          # [(engine, path)]；后台一次只续扫一个 rollout，避免列表请求背 IO。
_RT_WORK_SET = set()
_RT_WORKER = None
_RT_WORK_LOCK = threading.Lock()
# 上面几份缓存(会话标题、/rename 改名、rollout 游标)原本只活在进程内,一重启就得把三百来个 transcript
# 重扫一遍——实测冷跑 2.0s,正好落在开机盯着空侧栏的那几秒里。落盘一份,启动时读回来,
# 内容没变(mtime / size 没动)就不必再开文件。写入节流,不为每条标题都刷一次盘。
_TCACHE_PATH = os.path.join(REPO_ROOT, ".workbench", "title-cache.json")
_TCACHE_LOCK = threading.Lock()
_tcache_dirty = 0
_tcache_last = 0.0
_tcache_timer = None
_TCACHE_FLUSH_DELAY = 20.0   # 侧栏 1.5s 一轮；写盘不能跟着轮询频率跑


def _tcache_load():
    try:
        with open(_TCACHE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        for k, v in (d.get("jsonl") or {}).items():
            if isinstance(v, list) and len(v) == 2:
                _TITLE_CACHE[k] = (v[0], v[1])
        for k, v in (d.get("custom") or {}).items():
            if isinstance(v, list) and len(v) == 2:
                _CT_CACHE[k] = (v[0], v[1])
        for engine, rows in (d.get("rollout") or {}).items():
            if engine not in _RT_CACHE or not isinstance(rows, dict):
                continue
            for path, state in rows.items():
                if isinstance(path, str) and isinstance(state, dict):
                    _RT_CACHE[engine][path] = state
    except Exception:
        pass


def _tcache_flush():
    """把当前整批游标一次落盘；失败保留 dirty，下一次继续尝试。"""
    global _tcache_dirty, _tcache_last, _tcache_timer
    with _TCACHE_LOCK:
        timer, _tcache_timer = _tcache_timer, None
        if timer and timer is not threading.current_thread():
            timer.cancel()
        if not _tcache_dirty:
            return False
        dirty = _tcache_dirty
        now = time.time()
        try:
            # 顺手丢掉已经不存在的文件,免得缓存无限长
            with _RT_CACHE_LOCK:
                rollouts = {engine: {path: dict(state) for path, state in rows.items()
                                     if isinstance(path, str) and isinstance(state, dict)
                                     and os.path.exists(path)}
                            for engine, rows in _RT_CACHE.items()}
            d = {"jsonl": {k: list(v) for k, v in _TITLE_CACHE.items()
                            if isinstance(k, str) and isinstance(v, (tuple, list))
                            and len(v) == 2 and os.path.exists(k)},
                 "custom": {k: list(v) for k, v in _CT_CACHE.items()
                             if isinstance(k, str) and isinstance(v, (tuple, list))
                             and len(v) == 2 and os.path.exists(k)},
                 "rollout": rollouts}
            os.makedirs(os.path.dirname(_TCACHE_PATH), exist_ok=True)
            tmp = "%s.tmp.%d" % (_TCACHE_PATH, os.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, _TCACHE_PATH)
        except Exception:
            _tcache_dirty = max(_tcache_dirty, dirty)
            return False
        _tcache_dirty = 0
        _tcache_last = now
        return True


def _tcache_touch(force=False):
    """标脏并安排一次延迟 flush；一批扫描只起一个 timer。"""
    global _tcache_dirty, _tcache_timer
    flush_now = False
    with _TCACHE_LOCK:
        _tcache_dirty += 1
        flush_now = force or _tcache_dirty >= 40
        if not flush_now and _tcache_timer is None:
            _tcache_timer = threading.Timer(_TCACHE_FLUSH_DELAY, _tcache_flush)
            _tcache_timer.daemon = True
            _tcache_timer.start()
    if flush_now:
        return _tcache_flush()
    return False


atexit.register(_tcache_flush)
_THREAD_TITLE_CACHE = {}   # {engine: (mtime, {sid: title})}
_SESSION_INDEX_CACHE = {}   # {engine: (mtime, {sid: title})}
_ROLLOUT_UUID_RE = re.compile(r"rollout-[^/\\]*-([0-9a-fA-F-]{36})\.jsonl$")
# 工作流代码仓变化后需要重启的 Codex PTY。每个 sid 只允许一个 worker，连续变更由同一
# worker 读取最新注册表后收敛，避免 add/remove 连点时反复杀进程。
_CODEX_ROOT_WORKERS = set()


def _rollout_base(engine="codex"):
    # 会话枚举必须与引擎实际使用的 home 一致。Codex 的隔离 sessions 在正常
    # 情况下是全局目录的软链，但旧安装可能已形成实体目录；交给 store 统一判定。
    try:
        from engines import get as _engine_get
        spec = _engine_get(engine)
        if spec and spec.store and hasattr(spec.store, "base_dir"):
            return spec.store.base_dir()
    except Exception:
        pass
    return os.path.expanduser(_ROLLOUT_DIR.get(engine, "~/.codex/sessions"))


def _rollout_bases(engine="codex"):
    from engines import get
    return get(engine).store.session_dirs()


def _git_branch_for_cwd(cwd):
    """Best-effort current git branch for a terminal cwd. Empty means non-git, detached, or unreadable."""
    if not cwd:
        return ""
    try:
        rcwd = os.path.realpath(cwd)
    except Exception:
        return ""
    now = time.time()
    cached = _BRANCH_CACHE.get(rcwd)
    if cached and now - cached[0] < 2.0:
        return cached[1]
    branch = ""
    try:
        r = subprocess.run(["git", "-C", rcwd, "symbolic-ref", "--quiet", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=1.0)
        if r.returncode == 0:
            branch = (r.stdout or "").strip()[:120]
    except Exception:
        branch = ""
    _BRANCH_CACHE[rcwd] = (now, branch)
    return branch


def _branch_cwd_for_session(sess, dom, stored=None):
    """Resolve the cwd whose git branch should label a session row."""
    cwd = (stored or {}).get("cwd") if isinstance(stored, dict) else ""
    if cwd:
        return cwd
    try:
        return _domain_dir(dom or sess.get("domain", ""))
    except Exception:
        return ""


def _session_branch_label(sess, dom, stored=None):
    """会话行的分支徽标:只标「隔离会话」(cwd 不在该域主目录,即挂在独立 worktree/别处)。
    主 checkout 上的普通会话不标——全体同一分支的徽标是噪音,分支归属看项目本身即可。"""
    cwd = _branch_cwd_for_session(sess, dom, stored)
    if not cwd:
        return ""
    try:
        base = _domain_dir(dom or sess.get("domain", ""))
        if base and os.path.realpath(cwd) == os.path.realpath(base):
            return ""
    except Exception:
        pass
    return _git_branch_for_cwd(cwd)


def _codex_list_domain(meta_domain, stored_cwd, fallback_domain=""):
    """Domain shown in session lists for codex-like engines.

    Rollout cwd is generally the most reliable source, but explicit isolated
    targets are represented only in Desk metadata/session state. Keep those
    stable so recovered subarea sessions do not disappear into the source repo.
    """
    if meta_domain and (":" in meta_domain or meta_domain.startswith("dw-")):
        return meta_domain
    by_cwd = _domain_for_cwd(stored_cwd) if stored_cwd else ""
    # cwd 只在它真的指向某个工作流时才盖过 meta。指不出来(落在工作台根,_domain_for_cwd
    # 给的是 _desktop)时要回落 meta——GUI 建的会话 cwd 恒为工作台根,归属只存在于 meta 里,
    # 照旧写法会被一律判成「聊天」:侧栏里那段会话从它的工作流下消失,会话视图的任务栏
    # 也因此报「查不到归属」(2026-08-11 实见)。
    if by_cwd and by_cwd != "_desktop":
        return by_cwd
    return meta_domain or fallback_domain or by_cwd


def _desk_seed_strip(t):
    """剥掉 Desk 自己前置的系统约定 seed 与当前上下文,返回用户真正说的那句话。

    codex 家族没有 --append-system-prompt,系统约定只能拼进首轮正文,所以首条 user 消息
    是「seed + 分隔符 + 真话」。取标题时不剥的话,新建的会话在被 codex 自动命名之前,
    侧栏上显示的就是那段约定的开头(2026-08-24 实见:
    「〈系统约定,本次会话持续有效〉 ## …」)。

    分隔符从 engine_adapters 现取,不在这儿另抄一份常量——抄两份迟早只改一边,
    而这里正是「改了那边、这边照旧显示旧形状」最难被发现的地方。
    """
    try:
        from engine_adapters import CodexAdapter, SCENE_SEPS
        seps = (CodexAdapter.SEED_SEP,) + SCENE_SEPS
    except Exception:
        return t
    for sep in seps:
        if sep in t:
            t = t.split(sep, 1)[1]
    return t


def _engine_title_clean(nm):
    """引擎自己存的标题(state db 的 threads.title / session_index 的 thread_name)拿来用之前
    过一道:codex 会拿首条用户消息自动填 title,而首条消息现在以 Desk 的 seed 开头——
    于是「新会话的名字」成了「〈系统约定,本次会话持续有效〉 ## 起手…」(2026-08-26 用户实见,
    切引擎重载后侧栏与页头都显示这段)。

    先剥分隔符;db 里存的是截断过的(seed 约 3.3k,截断点远在分隔符之前),剥不出用户原话的
    就整条判废返回 ""——调用方落回 rollout 首条用户消息那条兜底,那一侧剥得干净。
    """
    nm = _desk_seed_strip(nm or "")
    try:
        from engine_adapters import CodexAdapter
        head = CodexAdapter.SEED_HEAD.strip()
    except Exception:
        head = "〈系统约定"
    if nm.lstrip().startswith("〈Desk 会话信息〉") or (head and nm.lstrip().startswith(head[:6])):
        return ""
    return nm


def _rollout_user_title_candidate(t):
    """rollout 首条真实用户消息可作为默认标题;系统注入上下文不算。

    注意 `<` 那一条挡不住 Desk 的 seed:它开头是书名号 〈(U+3008),不是尖括号(U+003C)。
    seed 由 _desk_seed_strip 在取用前剥掉,不靠这里挡——剥掉之后后面还跟着用户的真话,
    整条丢弃会让标题落空。"""
    return (t and not t.startswith("# AGENTS.md") and not t.lstrip().startswith("<")
            and "instructions" not in t[:40] and not t.startswith("Today's date is"))


def _state_db_stamps(engine):
    """返回标题库及其 WAL/SHM 的联合指纹，隔离库排在全局库前。"""
    out = []
    for db in (_STATE_DB.get(engine, ""), _STATE_DB_GLOBAL.get(engine, "")):
        db = os.path.expanduser(db)
        if not db or any(path == db for path, _stamp in out):
            continue
        try:
            stamp = tuple((p, os.path.getmtime(p)) for p in (db, db + "-wal", db + "-shm")
                          if os.path.exists(p))
        except OSError:
            stamp = ()
        if stamp:
            out.append((db, stamp))
    return tuple(out)


def _state_db_stamp(engine):
    """兼容旧调用者；第二项现为所有候选库的联合指纹。"""
    rows = _state_db_stamps(engine)
    return (rows[0][0] if rows else "", tuple(stamp for _path, stamp in rows))


def _session_index_paths(engine):
    return tuple(dict.fromkeys(os.path.expanduser(path) for path in (
        _SESSION_INDEX.get(engine, ""), _SESSION_INDEX_GLOBAL.get(engine, "")) if path))


def _session_index_path(engine):
    """兼容旧调用者，返回最高优先级的 session_index。"""
    paths = _session_index_paths(engine)
    return paths[0] if paths else ""


def _session_index_stamps(engine):
    out = []
    for path in _session_index_paths(engine):
        try:
            out.append((path, os.path.getmtime(path)))
        except OSError:
            continue
    return tuple(out)


def _session_index_stamp(engine):
    """兼容旧调用者；第二项现为所有候选索引的联合指纹。"""
    rows = _session_index_stamps(engine)
    return (rows[0][0] if rows else "", rows)


def _session_index_titles(engine):
    """合并全局与隔离 session_index；隔离记录优先，任一来源变化即刷新。"""
    sources = _session_index_stamps(engine)
    c = _SESSION_INDEX_CACHE.get(engine)
    if c and c[0] == sources:
        return c[1]
    titles = {}
    # 低优先级先写，高优先级后覆盖；每个 jsonl 内仍是后出现的 rename 覆盖前值。
    for path, _mt in reversed(sources):
        try:
            with open(path, encoding="utf-8") as f:
                for ln in f:
                    try:
                        o = json.loads(ln)
                    except Exception:
                        continue
                    sid = str(o.get("id") or "").strip()
                    title = _engine_title_clean(str(o.get("thread_name") or "").strip())
                    if sid and title:
                        titles[sid] = title[:80]
        except OSError:
            continue
    _SESSION_INDEX_CACHE[engine] = (sources, titles)
    return titles


def _state_db_titles(engine):
    """合并全局与隔离 state_5.sqlite 标题；隔离库优先，按联合指纹缓存。"""
    sources = _state_db_stamps(engine)
    c = _THREAD_TITLE_CACHE.get(engine)
    if c and c[0] == sources:
        return c[1]
    titles = {}
    try:
        import sqlite3
        for db, _stamp in reversed(sources):
            conn = None
            try:
                conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=0.5,
                                       isolation_level=None, check_same_thread=False)
                cur = conn.execute("SELECT id, title FROM threads")
                for sid, t in cur:
                    nm = _engine_title_clean(str(t or "").strip())
                    if sid and nm:
                        titles[str(sid)] = nm[:80]
            except Exception:
                continue
            finally:
                if conn is not None:
                    conn.close()
    except Exception:
        pass
    _THREAD_TITLE_CACHE[engine] = (sources, titles)
    return titles


def _rollout_uuid(path):
    m = _ROLLOUT_UUID_RE.search(path)
    return m.group(1) if m else ""


def _iso_ms(ts):
    if not ts:
        return 0
    try:
        from datetime import datetime
        s = str(ts)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return 0


def _codex_rollout_title(path, engine="codex"):
    """codex rollout 标题:新版 /rename 把新标题写进 state_5.sqlite,优先读它;
    旧版 codex 写 event_msg.thread_name_updated 到 rollout jsonl,次优先;
    都没有时回落到首条真实 user 文本。请求线程只读固定大小的头尾窗口,
    之后按持久化字节游标只读新增内容,不遍历历史正文。"""
    return _codex_rollout_title_bounded(path, engine)


_ROLLOUT_TITLE_SYNC_BYTES = 256 * 1024
_ROLLOUT_TITLE_HEAD_BYTES = 192 * 1024
_ROLLOUT_TITLE_TAIL_BYTES = _ROLLOUT_TITLE_SYNC_BYTES - _ROLLOUT_TITLE_HEAD_BYTES
_ROLLOUT_TITLE_BACKGROUND_BYTES = 512 * 1024


def _rollout_title_state(path, engine):
    """Return a cursor that survives polling and process restarts.

    Rollouts are append-only in normal operation. Replacement, truncation or an
    older cache shape resets the cursor instead of trusting stale offsets.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None, None
    with _RT_CACHE_LOCK:
        rows = _RT_CACHE.setdefault(engine, {})
        state = rows.get(path)
        identity = [int(st.st_dev), int(st.st_ino)]
        try:
            offset = int((state or {}).get("offset") or 0) if isinstance(state, dict) else 0
        except (TypeError, ValueError):
            offset = -1
        if (not isinstance(state, dict) or state.get("identity") != identity
                or offset < 0 or offset > st.st_size):
            state = {"identity": identity, "offset": 0, "first_user": "",
                     "thread_name": "", "complete": False, "skip_line": False,
                     "foreground_head_done": False, "complete_once": False}
            rows[path] = state
            offset = 0
        else:
            state.setdefault("first_user", "")
            state.setdefault("thread_name", "")
            state.setdefault("skip_line", False)
            state.setdefault("foreground_head_done", False)
            state.setdefault("complete_once", bool(state.get("complete")))
        if offset < st.st_size:
            state["complete"] = False
    return state, st


def _rollout_title_consume(state, obj):
    p = obj.get("payload") or {}
    if obj.get("type") == "event_msg" and p.get("type") == "thread_name_updated":
        title = str(p.get("thread_name") or "").strip()
        if title:
            state["thread_name"] = title
        return
    if state.get("first_user"):
        return
    items = [p] if obj.get("type") == "response_item" else []
    if obj.get("type") == "history_mutation" and p.get("operation") == "append":
        items = [x for x in (p.get("items") or []) if isinstance(x, dict)]
    for item in items:
        if item.get("type") != "message" or item.get("role") != "user":
            continue
        for content in (item.get("content") or []):
            text = content.get("text", "") if isinstance(content, dict) else ""
            text = _desk_seed_strip(text)
            if _rollout_user_title_candidate(text):
                from engine_adapters.bus import clean_session_title
                from session_names import FALLBACK_TITLE_MAX_CHARS
                title = (clean_session_title(text, limit=FALLBACK_TITLE_MAX_CHARS)
                         or text.strip()[:FALLBACK_TITLE_MAX_CHARS])
                if len(title) > FALLBACK_TITLE_MAX_CHARS:
                    title = title[:FALLBACK_TITLE_MAX_CHARS - 1].rstrip() + "…"
                state["first_user"] = title
                return


def _rollout_title_scan(path, engine, budget):
    """Scan at most budget bytes; oversized JSONL records are skipped in chunks."""
    with _RT_CACHE_LOCK:
        state, stat = _rollout_title_state(path, engine)
        if state is None:
            return None
        start = int(state.get("offset") or 0)
        remaining = max(1, int(budget or 0))
        try:
            with open(path, "rb") as stream:
                stream.seek(start)
                while remaining > 0 and stream.tell() < stat.st_size:
                    before = stream.tell()
                    line = stream.readline(remaining)
                    used = stream.tell() - before
                    if not used:
                        break
                    remaining -= used
                    complete_line = line.endswith(b"\n") or stream.tell() >= stat.st_size
                    if state.get("skip_line"):
                        if complete_line:
                            state["skip_line"] = False
                        continue
                    if not complete_line:
                        state["skip_line"] = True
                        continue
                    try:
                        _rollout_title_consume(state, json.loads(line))
                    except (ValueError, UnicodeDecodeError):
                        continue
                state["offset"] = stream.tell()
                state["complete"] = stream.tell() >= stat.st_size
                if state["complete"]:
                    state["complete_once"] = True
        except OSError:
            return dict(state)
        result = dict(state)
    _tcache_touch()
    return result


def _rollout_title_tail(path, engine, budget):
    """Inspect only the newest complete records without marking middle history scanned."""
    with _RT_CACHE_LOCK:
        state, stat = _rollout_title_state(path, engine)
        if state is None:
            return None
        stamp = [int(stat.st_size), int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)))]
        if state.get("tail_stamp") == stamp:
            return dict(state)
        limit = max(1, int(budget or 0))
        start = max(0, stat.st_size - limit)
        try:
            with open(path, "rb") as stream:
                aligned = start == 0
                if start:
                    stream.seek(start - 1)
                    aligned = stream.read(1) == b"\n"
                stream.seek(start)
                remaining = stat.st_size - start
                if not aligned and remaining:
                    skipped = stream.readline(remaining)
                    remaining -= len(skipped)
                while remaining > 0:
                    line = stream.readline(remaining)
                    if not line:
                        break
                    remaining -= len(line)
                    if not line.endswith(b"\n") and stream.tell() < stat.st_size:
                        continue
                    try:
                        _rollout_title_consume(state, json.loads(line))
                    except (ValueError, UnicodeDecodeError):
                        continue
                state["tail_stamp"] = stamp
        except OSError:
            return dict(state)
        result = dict(state)
    _tcache_touch()
    return result


def _rollout_title_background_worker():
    global _RT_WORKER
    while True:
        with _RT_WORK_LOCK:
            if not _RT_WORK:
                _RT_WORKER = None
                return
            engine, path = _RT_WORK.pop(0)
        try:
            while True:
                state = _rollout_title_scan(path, engine, _ROLLOUT_TITLE_BACKGROUND_BYTES)
                if not state or state.get("complete"):
                    break
                try:
                    time.sleep(0.05)
                except Exception:
                    pass
            try:
                _CODEX_CACHE.setdefault(engine, {"t": 0.0, "v": []})["t"] = 0.0
            except Exception:
                pass
        finally:
            with _RT_WORK_LOCK:
                _RT_WORK_SET.discard((engine, path))


def _rollout_title_schedule(path, engine):
    global _RT_WORKER
    key = (engine, path)
    with _RT_WORK_LOCK:
        if key not in _RT_WORK_SET:
            _RT_WORK_SET.add(key)
            _RT_WORK.append(key)
        if _RT_WORKER is None or not _RT_WORKER.is_alive():
            _RT_WORKER = threading.Thread(target=_rollout_title_background_worker,
                                          name="rollout-title-index", daemon=True)
            _RT_WORKER.start()


def _codex_rollout_title_bounded(path, engine="codex", fallback="", trusted_fallback=False):
    """Resolve a title without making a list request parse an entire rollout.

    Modern manual names live in session_index/state DB. Desk-created sessions
    persist fallback_title at first-turn storage. Older/external sessions scan
    bounded head and tail windows synchronously, then only newly appended bytes.
    """
    sid = _rollout_uuid(path)
    index_title = _session_index_titles(engine).get(sid, "") if sid else ""
    db_title = "" if index_title else (_state_db_titles(engine).get(sid, "") if sid else "")
    if index_title or db_title:
        return (index_title or db_title)[:80]
    if trusted_fallback:
        return str(fallback or "")[:80]
    state, stat = _rollout_title_state(path, engine)
    if state is None:
        return str(fallback or "")[:80]
    if state.get("complete_once") and int(state.get("offset") or 0) < stat.st_size:
        state = _rollout_title_scan(path, engine, _ROLLOUT_TITLE_HEAD_BYTES) or state
    elif not state.get("complete") and not state.get("foreground_head_done"):
        state = _rollout_title_scan(path, engine, _ROLLOUT_TITLE_HEAD_BYTES) or state
        with _RT_CACHE_LOCK:
            live = _RT_CACHE.get(engine, {}).get(path)
            if isinstance(live, dict):
                live["foreground_head_done"] = True
                state = dict(live)
    if not state.get("complete"):
        state = _rollout_title_tail(path, engine, _ROLLOUT_TITLE_TAIL_BYTES) or state
    if not state.get("complete"):
        _rollout_title_schedule(path, engine)
    return str(state.get("thread_name") or fallback or state.get("first_user") or "")[:80]


def _resolve_uuid_engine(sid):
    """uuid 休眠会话归属引擎:落盘位置权威(rollout 在 ~/.codex;cursor 在 ~/.cursor),
    其次看 meta,再否则 claude。codex/cursor uuid 同形无法靠版本号区分,故落盘存在性
    (各引擎描述符的 store.owns)优先于 meta(claude 无 rollout/transcript,不会被误判)。"""
    from engines import get as _engine_get
    for e in ("codex", "cursor-agent"):   # 落盘位置权威,顺序保持(rollout 先于 cursor transcript)
        sp = _engine_get(e)
        if sp and sp.store and sp.store.owns(sid):
            return e
    eng = (_term_meta_read().get(sid) or {}).get("engine")
    return eng or "claude"


def _codex_sessions(engine="codex"):
    """codex 原生会话:读 <rollout 目录>/**/rollout-*-<uuid>.jsonl。首行 session_meta 带 id/cwd/时间,
    按 cwd==工作区 过滤;标题优先取 /rename 写入的 thread_name_updated,否则取首条真实 user 文本。
    带 5s 缓存:Vibing 列每 3s 轮询,不该每次都 stat 上千个 rollout 文件。"""
    import glob as _glob
    now = time.time()
    cache = _CODEX_CACHE.setdefault(engine, {"t": 0.0, "v": []})
    if now - cache["t"] < 5:
        return cache["v"]
    wsdirs = {os.path.realpath(p) for p in _workspace_dirs()}
    registered = {sid for sid, rec in _term_meta_read().items() if rec.get("engine") == engine}
    fallbacks = {}
    try:
        for rec in list((_adapter(engine).index or {}).values()):
            if not isinstance(rec, dict) or not rec.get("engine_sid"):
                continue
            native = rec["engine_sid"]
            registered.add(native)
            fallback = str(rec.get("fallback_title") or rec.get("title") or "").strip()
            if fallback:
                fallbacks[native] = (fallback, bool(rec.get("fallback_title")))
    except Exception:
        pass
    files = [p for base in _rollout_bases(engine)
             for p in _glob.glob(base + "/**/rollout-*.jsonl", recursive=True)]
    # 全量枚举，首行按文件指纹缓存，避免每轮重读无变化的文件。
    headers = cache.setdefault("headers", {})
    fresh_headers = {}
    out = []
    seen = set()
    for path in files:
        try:
            st = os.stat(path)
            stamp = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            old = headers.get(path)
            if old and old[0] == stamp:
                meta = old[1]
            else:
                with open(path, encoding="utf-8") as f:
                    meta = json.loads(f.readline())
            if not isinstance(meta, dict) or meta.get("type") != "session_meta":
                continue
            fresh_headers[path] = (stamp, meta)
            pl = meta.get("payload") or {}
            sid, cwd = pl.get("id"), pl.get("cwd") or ""
            if not sid or sid in seen:
                continue
            if sid not in registered and (not cwd or os.path.realpath(cwd) not in wsdirs):
                continue
            start = _iso_ms(pl.get("timestamp") or meta.get("timestamp"))
            fallback, trusted = fallbacks.get(sid, ("", False))
            title = _codex_rollout_title_bounded(path, engine, fallback, trusted)
        except Exception:
            continue
        seen.add(sid)
        mt = st.st_mtime
        out.append({"id": sid, "title": (title or sid[:8])[:80], "time": int(mt * 1000), "start": start, "cwd": cwd})
    out.sort(key=lambda row: -row["time"])
    cache["headers"] = fresh_headers
    cache["t"] = now; cache["v"] = out
    return out


def _claude_proj_enc(wd):
    """claude transcript 目录名编码:cwd 里所有非字母数字字符都换成 -(/、.、_ 等;连字符保留,
    不合并连续分隔符,如 /.local→--local)。原来只换 /、. 漏了 _,含下划线的项目路径(如
    desktron_workbench)算错目录 → resume 列空。"""
    return re.sub(r"[^A-Za-z0-9]", "-", wd)


_CLAUDE_NAMES = {"t": 0.0, "v": {}}


WB_CLAUDE_NAMES = os.path.join(REPO_ROOT, ".workbench", "claude-names.json")


def _claude_named():
    """claude /rename 设的会话名:存在 ~/.claude/sessions/<pid>.json 的 name(键 sessionId)。
    transcript 里没有这个名(只有 summary),故 resume/vibing 的 claude 标题优先用它。5s 轻缓存。
    claude 退出即删该注册表 → name 丢失;故把活会话见过的 name 落持久缓存,exit 后回落,别名不丢。"""
    import glob as _glob
    now = time.time()
    if now - _CLAUDE_NAMES["t"] < 5.0:
        return _CLAUDE_NAMES["v"]
    live = {}
    for f in _glob.glob(os.path.expanduser("~/.claude/sessions/*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                o = json.load(fh)
            sid, nm = o.get("sessionId"), o.get("name")
            if sid and nm:
                live[sid] = nm
        except Exception:
            pass
    cache = {}
    try:
        with open(WB_CLAUDE_NAMES, encoding="utf-8") as fh:
            cache = json.load(fh) or {}
    except Exception:
        pass
    changed = False
    for sid, nm in live.items():
        if cache.get(sid) != nm:
            cache[sid] = nm; changed = True
    if changed:
        try:
            os.makedirs(os.path.dirname(WB_CLAUDE_NAMES), exist_ok=True)
            tmp = WB_CLAUDE_NAMES + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cache, fh, ensure_ascii=False)
            os.replace(tmp, WB_CLAUDE_NAMES)
        except Exception:
            pass
    out = {**cache, **live}   # 活会话名优先(最新);退出后回落缓存
    _CLAUDE_NAMES["t"], _CLAUDE_NAMES["v"] = now, out
    return out


# 串行化 custom-titles.json 的 read-modify-write:改名/迁移/新建清残留可能并发(多个后台 capture 线程 +
# 前端改名),无锁整文件读改写会丢更新、且同名 .tmp 互相覆盖。RLock 容许 _term_migrate_title 持锁时嵌套调 _term_set_title。
_TITLES_LOCK = __import__("threading").RLock()
_CUSTOM_TITLES = {"t": 0.0, "v": {}}


WB_CUSTOM_TITLES = os.path.join(REPO_ROOT, ".workbench", "custom-titles.json")


def _claude_auto_named(nm, wd=""):
    """这是不是 claude 引擎自动起的会话名(「目录名-短哈希」,如 workspace-9c)。

    引擎给每个进程注册一个这种名(~/.claude/sessions/<pid>.json),它不带任何内容,
    一列排下来彼此只差最后两位。它不是用户 /rename 的产物,不该顶掉按内容派生的标题
    (2026-08-27 用户实见:久睡的会话一开工,侧栏标题变成 workspace-9c)。
    锚定目录名再判,不用裸的「-两位十六进制结尾」:用户真给会话起名 release-2a 是合法的。

    锚的是全部工作区目录名的集合,不是这条记录落在哪个目录:名字里那截来自引擎进程
    自己的 cwd,与 transcript 的落盘目录未必是同一个——实见 domains/incident 下的会话
    叫 workspace-9c(引擎 cwd 是工作区根)。只锚落盘目录会漏掉这一类,而它恰是最常见的
    一类:第一版就是这么漏的。wd 给了也只并进候选,不缩小范围。"""
    nm = str(nm or "")
    if not nm:
        return False
    try:
        bases = {os.path.basename(str(x).rstrip("/")) for x in _workspace_dirs()}
    except Exception:
        bases = set()
    if wd:
        bases.add(os.path.basename(str(wd).rstrip("/")))
    return any(b and re.fullmatch(re.escape(b) + r"-[0-9a-f]{2,8}", nm) for b in bases)


def _custom_titles():
    """用户在会话列表右键手动改的标题:{sid: 标题}。持久化在 .workbench/custom-titles.json,5s 轻缓存。
    引擎无关——优先级高于一切自动派生标题(claude OSC / transcript summary / codex rollout)。
    key 取稳定的引擎 uuid(codex/cursor 的 codex_uuid、claude 的会话 uuid)或会话 id。"""
    now = time.time()
    if now - _CUSTOM_TITLES["t"] < 5.0:
        return _CUSTOM_TITLES["v"]
    with _TITLES_LOCK:
        if now - _CUSTOM_TITLES["t"] < 5.0:   # 双检:等锁期间别的线程可能已重载
            return _CUSTOM_TITLES["v"]
        out = {}
        try:
            with open(WB_CUSTOM_TITLES, encoding="utf-8") as fh:
                out = json.load(fh) or {}
        except Exception:
            out = {}
        _CUSTOM_TITLES["t"], _CUSTOM_TITLES["v"] = now, out
        return out


def _custom_title_for(*ids):
    """按候选 id 顺序取自定义标题(命中即返回);live codex/cursor 同时传 codex_uuid 与内部 id 兜底。"""
    ct = _custom_titles()
    for i in ids:
        if i and ct.get(i):
            return ct[i]
    return None


def _engine_renamed(sid, engine, row=None):
    """用户在引擎里自己 /rename 过。

    自动命名不能盖掉这个:名字是用户给的,哪怕给在引擎那一侧。各家的落点不同——
    claude 写 transcript 的 custom-title 条目与 ~/.claude/sessions 注册表,
    codex 写 state_5.sqlite 的 threads.title(旧版写 rollout 事件),
    pi 写会话文件的 session_info.name(由适配器在建行时标成 title_user)。
    这些读取本来就带缓存(按 mtime / 文件大小增量),不额外增加侧栏轮询的负担。

    判不准时一律返回 True(当作用户改过),宁可不显示自动名,也不要盖掉一个真名字。"""
    row = row or {}
    if row.get("title_user"):
        return True
    eng = str(engine or "")
    try:
        if eng == "codex":
            uuid = str(row.get("codex_uuid") or sid or "")
            if not uuid:
                return False
            return bool(_session_index_titles(eng).get(uuid) or _state_db_titles(eng).get(uuid))
        if eng == "claude":
            return _claude_user_renamed(sid)
    except Exception:
        return True
    return False


def _claude_user_renamed(sid):
    """claude 侧的用户改名:transcript 的 custom-title,或注册表里那个不是引擎自动生成的名。
    注册表的自动名形如「工作目录名-短哈希」,由 _claude_auto_named 认出来,不算改名。"""
    if not sid:
        return False
    nm = str(_claude_named().get(str(sid)) or "")
    for wd in _workspace_dirs():
        path = _claude_path(sid, wd)
        if not os.path.isfile(path):
            continue
        if _claude_custom_title(path):
            return True
        return bool(nm) and not _claude_auto_named(nm, wd)
    return bool(nm) and not _claude_auto_named(nm)


def _display_title(sid, engine, row, *ids):
    """侧栏与页头显示哪个名字。四层,由高到低:

      1. 用户在 Desk 里手改的(.workbench/custom-titles.json)
      2. 用户在引擎里 /rename 的(_engine_renamed,此时直接用引擎给的那个名)
      3. 支线自动命名的(.workbench/auto-titles.json)
      4. 引擎派生的(transcript 摘要 / rollout 首条用户消息 / 「新会话」)

    自动名夹在中间,是这个功能的全部约束所在:它压不过任何一种人给的名字,
    但盖得住「首条消息截断」这类派生标题——后者正是它要替换的东西。"""
    ids = tuple(i for i in ((sid,) + ids) if i)
    from session_names import resolve_title
    from session_autotitle import auto_titles
    projected = {**(row or {}), "title_user": _engine_renamed(sid, engine, row)}
    reverse = getattr(_TERM_SCAN_CONTEXT, "reverse", None) or {}
    indexed = _gui_index_row(engine, sid, reverse.get(engine))
    if indexed and isinstance(indexed.get("desk_name_fallback"), dict):
        projected["desk_name_fallback"] = indexed["desk_name_fallback"]
    return resolve_title(projected, ids, _custom_titles(), auto_titles())


def _term_move_domain(sid, domain):
    """把会话移动到另一个工作区:只改 meta domain 并置 pinned(此后 resume 以 meta 为权威,
    不被 codex rollout 的旧 cwd 反写回原域)。

    归属只是分组,不动任何进程的工作目录——会话的 cwd 跟它自己的落盘位置走
    (见 _transcript_cwd);工作流目录由适配器按会话取(CliAdapter.session_cwd)。
    早先这里给活着的 claude 注入 /cd 把进程切到新域目录,那是「搬家」语义:切走之后
    原目录的 transcript 不再增长,与引用模型冲突,已退役。
    task 执行记录与 shell 终端不可移。返回 (ok, err, cd_injected);cd_injected 恒为 False,
    保留在返回值里是为了不动调用方签名。"""
    sid = str(sid or "").strip()
    if not sid:
        return False, "缺 sid", False
    with _TERMS_LOCK:
        ex = _TERMS.get(sid) or next((x for x in _TERMS.values() if x.get("codex_uuid") == sid), None)
    outcome = {"ok": False, "error": "会话不存在", "keys": set()}
    def move(meta):
        keys = {sid} if sid in meta else set()
        if ex:
            keys.update(k for k in (ex["id"], ex.get("codex_uuid")) if k)
        if not keys:
            return False
        base = next((meta[k] for k in keys if k in meta), None) or (
            {"engine": ex.get("engine", ""), "domain": ex.get("domain", ""),
             "task": bool(ex.get("task"))} if ex else None)
        if base is None:
            return False
        if base.get("task"):
            outcome["error"] = "任务执行记录不可移动"
            return False
        if (base.get("engine") or "") == "shell":
            outcome["error"] = "临时终端不可移动"
            return False
        for key in keys:   # 活的 codex 内部 id 与 uuid 双键同步:休眠后 resume 按 uuid 查 meta,别漏
            row = dict(meta.get(key) or base)
            row["domain"] = domain
            row["pinned"] = True
            meta[key] = row
        outcome.update(ok=True, error="", keys=set(keys))
        return True
    _term_meta_mutate(move)
    if not outcome["ok"]:
        return False, outcome["error"], False
    with _TERMS_LOCK:   # 活会话:只换归组,进程 cwd 不动
        for x in _TERMS.values():
            if x["id"] in outcome["keys"] or x.get("codex_uuid") in outcome["keys"]:
                x["domain"] = domain
    cd = False   # 恒为 False:归属不再改工作目录(注入已退役,见 docstring)
    return True, "", cd


def _term_set_title(sid, title, aliases=()):
    """设/清会话自定义标题:title 去空白后为空=清除(回落自动派生)。原子落盘 + 失效缓存。"""
    if not sid:
        return
    with _TITLES_LOCK:   # 整段 read-modify-write 串行,防并发丢更新 / 同名 .tmp 互覆
        try:
            with open(WB_CUSTOM_TITLES, encoding="utf-8") as fh:
                cur = json.load(fh) or {}
        except FileNotFoundError:
            cur = {}
        if not isinstance(cur, dict):
            raise ValueError("会话名称文件格式错误")
        title = (title or "").strip()[:80]
        for identity in (sid, *aliases):
            cur.pop(str(identity), None)
        if title:
            cur[str(sid)] = title
        os.makedirs(os.path.dirname(WB_CUSTOM_TITLES), exist_ok=True)
        tmp = WB_CUSTOM_TITLES + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cur, fh, ensure_ascii=False)
        os.replace(tmp, WB_CUSTOM_TITLES)
        _CUSTOM_TITLES["t"] = 0.0
        _term_catalog_invalidate()


def _term_migrate_title(old_id, new_id):
    """把挂在 old_id 上的自定义标题迁到 new_id。用于 codex/cursor:用户可能在真 uuid 捕获前
    (会话还以内部序号 t{N} 列着)就右键改了名,标题被存到了 t{N} 上;捕获到 uuid 后迁过去,否则
    重启 t{N} 重排会丢标题(原会话拿不到)又泄漏(新会话误继承)。new_id 已有标题则不覆盖。"""
    if not old_id or not new_id or old_id == new_id:
        return
    with _TITLES_LOCK:   # 取旧→查新→写新→清旧 作为一个原子序列,防与并发改名/迁移交错
        t = _custom_title_for(old_id)
        if not t:
            return
        if not _custom_title_for(new_id):
            _term_set_title(new_id, t)
        _term_set_title(old_id, "")   # 迁走后清掉临时 id 的脏键


def _term_stable_id(sid):
    """活会话的前端/PTY 临时 id → 引擎稳定 id；尚未捕获时原样返回。"""
    sid = str(sid or "").strip()
    if not sid:
        return sid
    with _TERMS_LOCK:
        sess = _TERMS.get(sid) or next((x for x in _TERMS.values() if x.get("codex_uuid") == sid), None)
    return (sess or {}).get("codex_uuid") or sid


def term_session_refs_add(sids, domain):
    """把会话关联到工作流；任务绑定与目录导入共用 term meta 的 refs 语义。"""
    domain = str(domain or "").strip()
    if not domain or domain == "_desktop":
        return 0
    with _TERMS_LOCK:
        live = dict(_TERMS)
    prepared = []
    for raw in sids or ():
        raw = str(raw or "").strip()
        sid = _term_stable_id(raw)
        if not sid:
            continue
        sess = live.get(raw) or live.get(sid) or next(
            (x for x in live.values() if x.get("codex_uuid") == sid), None)
        guessed_engine = _resolve_uuid_engine(sid) if not sess and re.fullmatch(
            r"[0-9a-fA-F-]{36}", sid) else ""
        prepared.append((raw, sid, sess, guessed_engine))
    def add_refs(meta):
        changed = 0
        for raw, sid, sess, guessed_engine in prepared:
            row = dict(meta.get(sid) or meta.get(raw) or {})
            refs = [x for x in (row.get("refs") or []) if isinstance(x, str) and x]
            if domain in refs:
                continue
            refs.append(domain)
            row["refs"] = refs
            if sess:
                row.setdefault("engine", sess.get("engine") or "")
                row.setdefault("domain", sess.get("domain") or "_desktop")
            elif guessed_engine:
                row.setdefault("engine", guessed_engine)
                row.setdefault("domain", "_desktop")
            meta[sid] = row
            changed += 1
        return changed
    return _term_meta_mutate(add_refs)


def _term_migrate_identity(sess, new_id):
    """捕获稳定 id 后迁移所有以临时 PTY id 落盘的用户引用。"""
    old_id = (sess or {}).get("id")
    if not old_id or not new_id or old_id == new_id:
        return
    _term_migrate_title(old_id, new_id)
    # refs 必须迁,否则会话在捕获真实 UUID 之后恢复,仍被当作无归属聊天。
    def migrate(meta):
        old_meta = meta.pop(old_id, None)
        if not old_meta:
            return False
        current = dict(meta.get(new_id) or {})
        merged = {**old_meta, **current}
        merged["refs"] = list(dict.fromkeys(
            [x for x in (old_meta.get("refs") or []) + (current.get("refs") or [])
             if isinstance(x, str) and x]))
        meta[new_id] = merged
        return True
    _term_meta_mutate(migrate)


_tcache_load()   # 进程起来先把上一轮的扫描结果读回来
