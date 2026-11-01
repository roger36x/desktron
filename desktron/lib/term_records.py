# term.py 的分册(2026-08-27 分拆,specs/2026-08-27-repo-partition-2.md)。
# 不是独立模块:由 term.py 按序 exec 进它自己的命名空间——term 的猴补面很宽
# (测试补 term.<名> 约 17 个,调用方遍布各段),必须保持单命名空间的调用期查找;
# 文件切开只为可读性。名字都解析在 term 模块的 globals 里,别单独 import 这个文件。

def _claude_jsonl_title(path):
    """从 claude jsonl 文件提取标题,带 mtime 缓存。"""
    try:
        mt = os.path.getmtime(path)
    except Exception:
        return "", 0
    cached = _TITLE_CACHE.get(path)
    if cached and cached[0] == mt:
        return cached[1], mt
    title, fu = "", ""
    try:
        with open(path, encoding="utf-8") as f:
            for i, ln in enumerate(f):
                try: o = json.loads(ln)
                except Exception: continue
                if o.get("type") == "summary" and o.get("summary"):
                    title = o["summary"]; break
                if o.get("type") == "user" and not fu:
                    c = (o.get("message") or {}).get("content")
                    t = ""
                    if isinstance(c, str): t = c
                    elif isinstance(c, list):
                        for x in c:
                            if isinstance(x, dict) and x.get("type") == "text":
                                t = x.get("text", ""); break
                    t = re.sub(r"<(system-reminder|local-command-[^>]*|command-[^>]*)>.*?</\1>", "", t, flags=re.S)
                    t = re.sub(r"<[^>]+>", "", t).strip()
                    if t: fu = t
                if i > 80: break
        if not title:
            title = fu.strip()
    except Exception:
        pass
    _TITLE_CACHE[path] = (mt, title)
    _tcache_touch()
    return title, mt


def _claude_custom_title(path):
    """claude 的 /rename 落在 transcript 的 custom-title 条目里,取最后一条。
    终端那侧显示的就是它,而 ~/.claude/sessions 注册表里的 name 会被引擎改成
    workspace-8b 这类自动名,两边于是显示成两个名字。

    整份扫一遍在大文件上要 180ms(实测 134MB),而活会话每次轮询 mtime 都变。
    所以按文件大小做增量:第一次全扫,之后只扫新追加的那一段,没有新的改名就沿用上一次。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    prev = _CT_CACHE.get(path)
    start, title = (prev[0], prev[1]) if prev and prev[0] <= size else (0, "")
    if prev and prev[0] == size:
        return title
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            f.seek(start)
            if start:
                f.readline()   # 断点可能落在半行上,丢掉它
            for ln in f:
                if '"custom-title"' not in ln:
                    continue   # 廉价预筛:整行解析 json 太贵,先按子串过一遍
                try:
                    o = json.loads(ln)
                except ValueError:
                    continue
                if o.get("type") == "custom-title" and o.get("customTitle"):
                    title = o["customTitle"]
    except OSError:
        return title
    _CT_CACHE[path] = (size, title)
    _tcache_touch()
    return title


def _claude_path(sid, wd):
    return os.path.expanduser(f"~/.claude/projects/{_claude_proj_enc(wd)}/{sid}.jsonl")


_CLAUDE_SID_RE = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def _claude_transcript_hits(sid):
    """按会话 id 在 ~/.claude/projects/*/ 下精确命中的记录文件(可能不止一处,按目录名排序)。

    记录的目录名在会话创建时由当时的 cwd 定死,之后 --resume 一直追加同一个文件、不随进程
    cwd 迁移,所以一段会话的记录可能落在与它当前工作目录不对应的目录里,按编码算不出来。
    这里只按 id 找:id 是 uuid,跨目录不会撞。

    id 先过 uuid 形状校验,再要求命中的文件直属 ~/.claude/projects 的某个子目录——通配符、
    路径分隔符都进不了模式,调用方(含删除)拿到的一定是同名同层的那一个文件,没有模糊匹配。
    """
    import glob as _glob
    if not (sid and _CLAUDE_SID_RE.fullmatch(str(sid))):
        return []
    base = os.path.realpath(os.path.expanduser("~/.claude/projects"))
    out = []
    for path in sorted(_glob.glob(os.path.join(base, "*", f"{sid}.jsonl"))):
        real = os.path.realpath(path)
        if os.path.basename(real) == f"{sid}.jsonl" and os.path.dirname(os.path.dirname(real)) == base:
            out.append(path)
    return out


def _claude_pick_name(path, sid, wd, title):
    """claude 会话显示什么名字。三档:用户自己 /rename 的 > 注册表里的名 > transcript 摘要。

    注册表那一档要挑一下:引擎会自动给会话起「工作目录名 + 短哈希」这样的名字
    (incident-b2 之类),它不带任何内容,一列排下来彼此只差最后两位,认不出哪条是哪条。
    认出这个形状就让位给 transcript 的摘要或首条提问——那才是这段会话说了什么。
    用户手动改过的名字不受影响。"""
    reg = _claude_named().get(sid) or ""
    if _claude_auto_named(reg, wd):   # wd 未知时判据自己退到全部工作区目录名(见其 docstring)
        reg = ""
    return _claude_custom_title(path) or reg or title


def _claude_session_by_id(sid):
    """按 id 直接定位 claude jsonl 文件,不扫全量目录。

    工作目录编码都对不上时按 id 兜底一次(_claude_transcript_hits):记录目录名由会话创建时的
    cwd 定死、--resume 之后不迁移,出生目录一旦不在 _workspace_dirs 里(如把那个仓从工作流的
    repos 里摘掉),这里返回 None,休眠列表就把这段会话整条丢掉。兜底是单层 glob,且只在前面
    那圈 stat 全落空时才走,不构成侧栏刷新的负担。"""
    for wd in _workspace_dirs():
        path = _claude_path(sid, wd)
        if os.path.isfile(path):
            title, mt = _claude_jsonl_title(path)
            name = _claude_pick_name(path, sid, wd, title)
            return {"id": sid, "title": (name or sid[:8])[:80], "time": int(mt * 1000)}
    hits = _claude_transcript_hits(sid)
    if not hits:
        return None
    title, mt = _claude_jsonl_title(hits[0])
    name = _claude_pick_name(hits[0], sid, "", title)
    return {"id": sid, "title": (name or sid[:8])[:80], "time": int(mt * 1000)}


def _transcript_cwd(engine, sid):
    """claude / cursor 的 transcript 目录名是 cwd 的编码,--resume 只在当前 cwd 对应的目录里找。
    会话被导入工作流后归属变了,但落盘位置没动——resume 必须回到落盘所在目录,否则报
    「No conversation found with session ID」。返回该目录;找不到给 ''(调用方回落域目录)。"""
    if engine == "claude":
        pather = _claude_path
    elif engine == "cursor-agent":
        pather = _cursor_path
    else:
        return ""   # codex 的 rollout 是全局 glob,与 cwd 无关
    for wd in _workspace_dirs():
        try:
            if os.path.isfile(pather(sid, wd)):
                return wd
        except Exception:
            continue
    return ""


def _cursor_proj_enc(wd):
    """cursor transcript 目录名编码:连续非字母数字→单个 -,去首尾连字符。
    实测对拍 ~/.cursor/projects 真实目录名:`/.local`(/ + .)折叠成单个 -
    (/Users/alice/.local/share/desktron/workspace → Users-alice-local-share-desktron-workspace)。
    注意:必须用 `+` 折叠连续分隔符;每字符一个 - 会在含 `/.`(隐藏目录)等路径上错位、扫不到 transcript。"""
    return re.sub(r"[^A-Za-z0-9]+", "-", wd).strip("-")


_CURSOR_CACHE = {"t": 0.0, "v": []}


def _cursor_transcripts_dir(wd):
    return os.path.expanduser(f"~/.cursor/projects/{_cursor_proj_enc(wd)}/agent-transcripts")


def _cursor_jsonl_title(path):
    """cursor transcript 标题:优先取 <user_query> 内层,避免把 cursor 注入上下文当标题。带 mtime 缓存。"""
    try:
        mt = os.path.getmtime(path)
    except Exception:
        return "", 0
    cached = _TITLE_CACHE.get(path)
    if cached and cached[0] == mt:
        return cached[1], mt
    title = ""
    try:
        with open(path, encoding="utf-8") as f:
            for i, ln in enumerate(f):
                if i > 80:
                    break
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                if o.get("role") != "user":
                    continue
                c = (o.get("message") or {}).get("content")
                t = ""
                if isinstance(c, str):
                    t = c
                elif isinstance(c, list):
                    for x in c:
                        if isinstance(x, dict) and x.get("type") == "text":
                            t = x.get("text", ""); break
                m = re.search(r"<user_query>(.*?)</user_query>", t, re.S)
                t = m.group(1) if m else re.sub(r"</?user_query>", "", t)
                t = re.sub(r"<[^>]+>", "", t).strip()
                if t:
                    title = t; break
    except Exception:
        pass
    _TITLE_CACHE[path] = (mt, title)
    return title, mt


def _cursor_path(sid, wd):
    return os.path.join(_cursor_transcripts_dir(wd), sid, f"{sid}.jsonl")


def _cursor_sessions():
    """cursor-agent 会话:扫各工作区目录的 agent-transcripts/<sid>/<sid>.jsonl,归本工作台。带 5s 缓存(同 _codex_sessions)。"""
    now = time.time()
    if now - _CURSOR_CACHE["t"] < 5:
        return _CURSOR_CACHE["v"]
    out = []
    for wd in _workspace_dirs():
        base = _cursor_transcripts_dir(wd)
        if not os.path.isdir(base):
            continue
        for sid in os.listdir(base):
            path = _cursor_path(sid, wd)
            if not os.path.isfile(path):
                continue
            title, mt = _cursor_jsonl_title(path)
            out.append({"id": sid, "title": (title or sid[:8])[:80], "time": int(mt * 1000), "cwd": wd})
    out.sort(key=lambda s: -s["time"])
    _CURSOR_CACHE["t"] = now; _CURSOR_CACHE["v"] = out
    return out


def _cursor_session_by_id(sid):
    for wd in _workspace_dirs():
        path = _cursor_path(sid, wd)
        if os.path.isfile(path):
            title, mt = _cursor_jsonl_title(path)
            return {"id": sid, "title": (title or sid[:8])[:80], "time": int(mt * 1000), "cwd": wd}
    return None


def _handoff_content_text(content):
    """Normalize transcript content payloads into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                val = item.get("text") or item.get("content") or item.get("input") or item.get("output")
                if isinstance(val, str):
                    parts.append(val)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    if isinstance(content, dict):
        val = content.get("text") or content.get("content")
        return val if isinstance(val, str) else ""
    return ""


def _handoff_clean_text(text, role):
    """Drop engine prompt injection noise and keep the user-visible conversation text."""
    text = (text or "").strip()
    if not text:
        return ""
    m = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, flags=re.S)
    if m:
        text = m.group(1).strip()
    elif role == "user" and (text.startswith("# AGENTS.md") or text.lstrip().startswith("<system-reminder")):
        return ""
    text = re.sub(r"<(system-reminder|local-command-[^>]*|command-[^>]*)>.*?</\1>", "", text, flags=re.S)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _claude_handoff_messages(sid):
    path = ""
    for wd in _workspace_dirs():
        p = _claude_path(sid, wd)
        if os.path.isfile(p):
            path = p
            break
    if not path:
        return []
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                role = o.get("type")
                if role not in ("user", "assistant"):
                    continue
                msg = o.get("message") or {}
                text = _handoff_clean_text(_handoff_content_text(msg.get("content")), role)
                if text:
                    out.append((role, text))
    except Exception:
        return []
    return out


def _cursor_handoff_messages(sid):
    path = ""
    for wd in _workspace_dirs():
        p = _cursor_path(sid, wd)
        if os.path.isfile(p):
            path = p
            break
    if not path:
        # Tests and recovery utilities may patch _cursor_path without a real workspace list.
        try:
            p = _cursor_path(sid, "")
            if os.path.isfile(p):
                path = p
        except Exception:
            path = ""
    if not path:
        return []
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                role = o.get("role")
                if role not in ("user", "assistant"):
                    continue
                msg = o.get("message") or {}
                text = _handoff_clean_text(_handoff_content_text(msg.get("content")), role)
                if text:
                    out.append((role, text))
    except Exception:
        return []
    return out


def _codex_handoff_messages(sid, engine="codex"):
    path = _codex_rollout_path(sid, engine)
    if not path:
        try:
            import glob as _glob
            hits = _glob.glob(_rollout_base(engine) + f"/**/rollout-*-{sid}.jsonl", recursive=True)
            path = hits[0] if hits else ""
        except Exception:
            path = ""
    if not path:
        return []
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                p = o.get("payload") or {}
                role, content = "", None
                if o.get("type") == "response_item" and p.get("type") == "message":
                    role, content = p.get("role"), p.get("content")
                elif p.get("type") in ("user_message", "assistant_message"):
                    role = "user" if p.get("type") == "user_message" else "assistant"
                    content = p.get("message") or p.get("text") or p.get("content")
                if role not in ("user", "assistant"):
                    continue
                text = _handoff_clean_text(_handoff_content_text(content), role)
                if text:
                    out.append((role, text))
    except Exception:
        return []
    return out


def _session_handoff_prompt(source_engine, sid, target_engine):
    """为新线程构造历史接续 seed；源与目标引擎可相同,不会 resume/fork 原线程。"""
    source_engine = source_engine or ""
    target_engine = target_engine or ""
    if source_engine == "claude":
        msgs = _claude_handoff_messages(sid)
    elif source_engine == "cursor-agent":
        msgs = _cursor_handoff_messages(sid)
    elif source_engine == "codex":
        msgs = _codex_handoff_messages(sid, source_engine)
    else:
        raise ValueError("暂不支持从 %s 交接到当前引擎" % (source_engine or "未知引擎"))
    if not msgs:
        raise ValueError("没有找到可交接的会话内容")
    budget = 18000
    lines = [
        "# Session handoff",
        f"source_engine: {source_engine}",
        f"source_session: {sid}",
        f"target_engine: {target_engine}",
        "",
        "这是一个不恢复原线程的新会话。请先用几句话概括当前目标、已完成事项和未完成事项，然后继续推进。",
        "不要沿用源会话的 Desk 业务角色；只依据本会话当前注入的约束工作。",
        "不要假设源引擎的隐含上下文仍然存在；需要时重新检查本地文件。",
        "",
        "## 历史对话",
    ]
    used = sum(len(x) + 1 for x in lines)
    role_name = {"user": "用户", "assistant": "助手"}
    selected = []
    for role, text in reversed(msgs):
        item = f"{role_name.get(role, role)}: {text}"
        cost = len(item) + 2
        if selected and used + cost > budget:
            break
        selected.append(item)
        used += cost
    selected.reverse()
    lines.extend(selected)
    return "\n".join(lines).strip() + "\n"
