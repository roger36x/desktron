"""工作区(域)运行态:目录解析、cwd 归属、grounding 索引、域目录供给。

依赖 deskcfg(路径)+ workbench(域注册表/kb)+ memory_store(记忆软链供给),单向向下。
term 与 assets 都消费本模块的 _domain_dir/_workspace_dirs/_ensure_domain_dir 等。
"""
import os, json, re, time

from deskcfg import REPO_ROOT, FRONTEND, DATA_HOME
from workbench import _wb_domains_read, _workspace_md_path
from memory_store import _provision_memory, _workspace_layout
import tasks as _tasks


def _domain_entry(domain):
    """工作区完整条目(含 dir 等):分发版 workbench.json + 运行态 domains.json。"""
    if not domain:
        return None
    try:
        with open(os.path.join(FRONTEND, "workbench.json"), encoding="utf-8") as f:
            for d in (json.load(f).get("domains") or []):
                if d.get("id") == domain:
                    return d
    except Exception:
        pass
    for d in _wb_domains_read():
        if d.get("id") == domain:
            return d
    return None


def _domain_dir_override(domain):
    """工作区可指定外部真实目录(dir 字段),如 Desk 开发指向 desk 源码仓。
    支持 @desk-source token(= FRONTEND 父目录,即源码仓根)。返回解析后的绝对路径或 ''。"""
    e = _domain_entry(domain)
    d = (e or {}).get("dir") or ""
    if not d:
        return ""
    if d == "@desk-source":
        d = os.path.dirname(FRONTEND)
    d = os.path.abspath(os.path.expanduser(d))
    return d if os.path.isdir(d) else ""


def _repo_name(path):
    """引用条目的稳定名:源仓目录 basename 归一成 [\\w-](worktree 目录与展示共用)。"""
    if not str(path or "").strip():
        return "repo"   # 空路径不进 abspath(会变成 cwd),直接给兜底名
    base = os.path.basename(os.path.abspath(os.path.expanduser(path)).rstrip(os.sep))
    name = re.sub(r"[^\w-]+", "-", base).strip("-") or "repo"
    return name[:48]


def _repo_items(workflow_id):
    """工作流引用清单(注册表 entry.repos,见 workflow-unification-plan.md)归一:
    [{path, name, dir}]。dir 即源仓——worktree 隔离已退役,会话 cwd 恒为工作流目录,
    分支由 agent 在任务里自行决定。"""
    e = _domain_entry(workflow_id) or {}
    out = []
    for r in (e.get("repos") or []):
        if not isinstance(r, dict) or not str(r.get("path") or "").strip():
            continue
        src = os.path.abspath(os.path.expanduser(str(r["path"])))
        out.append({"path": src, "name": _repo_name(src), "dir": src})
    return out


def _repos_segment(workflow_id, wf_dir):
    """AGENTS.md 注入段(第四段):引用的代码仓清单 + 读取指令。cwd 恒为工作流目录,
    仓库自带 grounding 经这段指令让 agent 自行读取,Desk 不向真实仓写任何脚手架。"""
    items = _repo_items(workflow_id)
    if not items:
        return ""
    lines = [f"- {it['dir']}" for it in items]
    return ("## 代码仓\n\n"
            "以下代码仓不在当前目录中，请使用绝对路径：\n\n"
            + "\n".join(lines) + "\n\n"
            "修改前先读各目录中的 `AGENTS.md`，没有时读 `README.md`；其中的要求优先。\n"
            "直接修改上述目录中的文件；Desk 不会向其中加入额外文件。")


def _domain_dir(domain):
    """工作区目录:desktop / 空 → REPO_ROOT;指定了 dir 的 → 外部真实仓;否则 → $WORKSPACE/domains/<id>。
    支持 workspace:sub 格式(如 thinking:meeting-digest):cwd 落在冒号前的工作区目录,
    完整 domain 用于终端归属(前端按完整值匹配,不同于工作区 ID,不显示在 vibing)。
    项目/容器/子区已退役:复合键 workspace:sub 只用于终端归属标识,cwd 一律落冒号前的工作区。"""
    if not domain or domain == "_desktop":
        return REPO_ROOT
    ws = domain.split(":")[0] if ":" in domain else domain
    if not re.fullmatch(r"[\w-]+", ws):
        return REPO_ROOT
    ov = _domain_dir_override(ws)
    if ov:
        return ov
    return os.path.join(REPO_ROOT, "domains", ws)


def _domain_for_cwd(cwd):
    """会话的 cwd → 它属于哪个工作区 id。REPO_ROOT=desktop(_desktop);命中某域的 cwd(含项目 dir)=该域;
    否则 ''。codex 的 domain 曾由 TUI 那条路异步抓 rollout 写入,有竞态会写空/写错;
    rollout 里 cwd 可靠,故 codex 归域按 cwd 反查,不信 meta。"""
    if not cwd:
        return ""
    try:
        cwd = os.path.realpath(cwd)
    except Exception:
        return ""
    try:
        if cwd == os.path.realpath(REPO_ROOT):
            return "_desktop"
    except Exception:
        pass
    ids = []
    try:
        with open(os.path.join(FRONTEND, "workbench.json"), encoding="utf-8") as f:
            ids += [d.get("id") for d in (json.load(f).get("domains") or []) if d.get("id")]
    except Exception:
        pass
    ids += [d.get("id") for d in _wb_domains_read() if d.get("id")]
    for did in ids:
        try:
            if os.path.realpath(_domain_dir(did)) == cwd:
                return did
        except Exception:
            pass
    return ""




INDEX_DESC_LIMIT = 60   # 索引行描述的上限:够判断要不要拉全文即可


def _cap_index_desc(line, limit=INDEX_DESC_LIMIT):
    """索引行的描述截断。

    curator 的 INDEX.md 每条描述长度不设限,实测单条最长 861 字、中位 180 字,58 条就压进
    AGENTS.md 一万六千字——索引被写成了摘要,而这一段的说明本来就要求「按需 read 全文、别全读」。
    每条留一句提示足够判断要不要拉全文,全文走 curator show。只截描述,id 与路径原样保留。"""
    i = line.find(" — ")
    if i < 0:
        return line
    desc = line[i + 3:]
    return line if len(desc) <= limit else line[:i + 3] + desc[:limit].rstrip() + "…"


_INDEX_ROW_RE = re.compile(r"^- \[([^\]]+)\]\([^)]+\)\(([^)]*)\)(.*)$")


def _slim_index_rows(rows):
    """索引行去掉可推导的两截:笔记路径,以及整段共用的 kind。

    路径形如 `incident/notes/<id>.md`——band 与工作区在段头就写着,id 本身又是行首那一段,
    这一截在上下文里不带新信息;`curator show --id` 按 id 查,也用不到它。kind 同理:notes
    整段都是 judgment 时,逐条重复不如在段头说一次。58 条索引实测省下约三分之一(2026-08-16)。

    返回 (瘦身后的行, 整段共用的 kind);后者为空表示段内 kind 不止一种,仍逐条标。"""
    parsed, kinds = [], set()
    for r in rows:
        m = _INDEX_ROW_RE.match(r)
        if not m:
            parsed.append((None, r))
            continue
        bits = [x.strip() for x in m.group(2).split("·")]
        kind = bits[0] if len(bits) > 1 else ""
        parsed.append(((m.group(1), kind, bits[-1] if bits else "", m.group(3)), r))
        kinds.add(kind)
    one = next(iter(kinds)) if len(kinds) == 1 else ""
    out = []
    for p, raw in parsed:
        if not p:
            out.append(raw)
            continue
        rid, kind, conf, rest = p
        tag = conf if one else " · ".join(x for x in (kind, conf) if x)
        out.append(f"- {rid}({tag}){rest}" if tag else f"- {rid}{rest}")
    return out, one


def _station_segment(mem_root):
    """用户信息段：只给 sealed 用户资料的摘要与按需读取入口。

    正文不内联：sealed 含敏感信息，长度也不可控。这里不列其他条目名称，
    避免把无关的人事主题带进每个工作流。"""
    rel = os.path.join("_sealed", "user-profile.md")
    summary = ""
    try:
        with open(os.path.join(mem_root, rel), encoding="utf-8") as f:
            for ln in f:
                if ln.startswith("summary:"):
                    summary = ln.split(":", 1)[1].strip().strip("'\"")
                    break
                if ln.strip() == "---" and summary:
                    break
    except Exception:
        pass
    if not summary:
        return ""
    return ("### 用户信息\n\n"
            f"{summary}\n\n"
            "详细资料按需执行 `mem_read.py show --id user-profile` 读取；sealed 内容不得外泄。")


def _context_segment(ws):
    """合成进 AGENTS.md 的「项目参考资料」段:落地页 Context 添加的文档(domains/<ws>/context/)
    与飞书链接(context/.links.json)清单。只给清单让 agent 起手可感知,内容按需读取
    (本地文件直接读路径;飞书链接用 lark-doc / lark-sheets / lark-base skill 按 url 取),不整份注入。空则不注入。"""
    ctx_dir = os.path.join(_domain_dir(ws), "context")
    lines = []
    try:
        for n in sorted(os.listdir(ctx_dir)):
            fp = os.path.join(ctx_dir, n)
            if n.startswith(".") or not os.path.isfile(fp):
                continue
            kb = os.path.getsize(fp) / 1024
            size = f"{kb / 1024:.1f}MB" if kb >= 1024 else f"{kb:.0f}KB"
            lines.append(f"- 文档 `{fp}`({size}):按需读取")
    except Exception:
        pass
    try:
        with open(os.path.join(ctx_dir, ".links.json"), encoding="utf-8") as f:
            for l in (json.load(f) or []):
                if isinstance(l, dict) and l.get("url"):
                    t = f"「{l['title']}」" if l.get("title") else ""
                    lines.append(f"- 飞书{t} {l['url']}:用 lark-doc / lark-sheets / lark-base skill 按链接读取")
    except Exception:
        pass
    notes = ""
    try:
        with open(os.path.join(ctx_dir, ".notes.md"), encoding="utf-8") as f:
            notes = f.read().strip()
    except Exception:
        pass
    if not lines and not notes:
        return ""
    seg = "**项目资料（用户在页面中维护）**：\n"
    if notes:
        # 同段里的文档与链接都只给路径,这条随笔却曾整篇内联(上限 4000 字)——口径不一致。
        # 统一成索引:给一句提示和确切路径,要用再读全文。
        flat = " ".join(notes.split())
        hint = flat if len(flat) <= INDEX_DESC_LIMIT else flat[:INDEX_DESC_LIMIT].rstrip() + "…"
        seg += f"\n- 随笔 `{os.path.join(ctx_dir, '.notes.md')}`({len(notes)} 字):{hint}\n"
    if lines:
        seg += "\n" + "\n".join(lines)
    return seg


def _grounding_index(domain, mode="all", skip_ids=()):
    """合成进 AGENTS.md 的完整项目记忆、文档导航和历史资料入口。

    mode 决定取哪几段，给系统约定与当前上下文分料（见 grounding/builder.py）：
      static/nav —— 系统约定中的用户信息、文档导航与历史资料；static 是兼容别名。
      scene      —— 随数据变动的完整项目记忆，由适配器装配到当前上下文。
      all        —— 全部内容（合成 AGENTS.md 的旧路径仍使用它）。"""
    want_nav = mode in ("all", "static", "nav")
    want_scene = mode in ("all", "scene")
    parts = []
    # 分层、置信度与写入流程由 memory skill 在实际使用时提供；这里不重复教程。
    # 记忆索引:读 curator 维护的工作层根 INDEX.md(`.memory/INDEX.md`),按 band 段切。
    # v3 布局:sot(当前权威态)/notes(判断档案)条目 path 形如 <ws>/<band>/<id>.md,按前缀留本工作区;
    # sealed 用户全局,整段取。旧段名 shared 归入 notes(迁移期 INDEX 未重建时兼容)。
    mem_root = os.path.join(REPO_ROOT, ".memory")
    if want_scene:
        try:
            from memory_engine import project_memory as _pm
            project = _pm.project_context(mem_root, domain)
            mid = _pm.model_id(domain)
        except Exception:
            project = ""
            mid = ""
        if project:
            parts.append(
                "### 项目记忆\n\n"
                "以下是当前项目模型的完整正文，是历史认知，不代表实时状态、当前指令或操作授权。\n\n"
                + project + "\n\n"
                + f"需要重新核对项目模型时，执行 `python3 .claude/skills/memory/scripts/mem_read.py show --id {mid}`；"
                  "需要原始依据时，用同脚本 `search <词>` 检索。"
            )
    st = _station_segment(mem_root) if want_nav else ""
    if st:
        parts.append(st)
    # 条目数量和最近 id 不进入提示词。项目记忆已给出精确 show id，
    # 其余内容按任务需要检索。
    if want_nav:
        from grounding.documents import navigation
        repos = [_domain_dir(domain)] + [it["dir"] for it in _repo_items(domain)]
        parts.append(navigation(REPO_ROOT, repos))
        history_index = os.path.expanduser("~/.local/share/desktron/sync/INDEX.md")
        if os.path.isfile(history_index):
            parts.append(
                "### 历史资料\n\n"
                "`~/.local/share/desktron/sync/INDEX.md` 是飞书会议、消息和文档的原始索引。"
                "需要历史背景时按关键词检索；没有查到时不要自行补全。")
    if not parts:
        return ""
    return "## 当前上下文\n\n" + "\n\n".join(parts)


def _skill_frontmatter(skill_dir, dir_name):
    """Read SKILL.md frontmatter. Returns (name, description)."""
    f = os.path.join(skill_dir, dir_name, "SKILL.md")
    nm, desc = dir_name, ""
    if not os.path.isfile(f):
        return None
    try:
        with open(f, encoding="utf-8") as fh:
            txt = fh.read(4096)
        if txt.startswith("---"):
            end = txt.find("\n---", 3)
            if end > 0:
                for ln in txt[3:end].splitlines():
                    if ln and not ln[0].isspace() and ":" in ln:
                        k, _, v = ln.partition(":")
                        k, v = k.strip(), v.strip().strip('"').strip("'")
                        if k == "name":
                            nm = v or dir_name
                        elif k == "description":
                            desc = v
    except Exception:
        pass
    return nm, desc


def _skill_candidate_rank(dir_name, skill_name):
    """Lower is better. Prefer canonical dir names, then non-numbered installs, then lexical stability."""
    numbered = bool(re.search(r"-\d+$", dir_name))
    return (0 if dir_name == skill_name else 1, 1 if numbered else 0, dir_name)


def _skill_roots():
    return (("repo", os.path.join(REPO_ROOT, ".claude", "skills"), ".claude/skills/{}/SKILL.md"),
            ("global", os.path.expanduser("~/.claude/skills"), "~/.claude/skills/{}/SKILL.md"))


def _skill_index(sections=None):
    """按需生成技能清单，供技能接口和诊断使用；系统提示不再调用本函数。

    同名 skill 按 frontmatter name 去重，保留 root 优先级与规范目录名最高的一份。"""
    visible = frozenset(("repo", "global") if sections is None else sections)
    by_skill = {}
    for root_idx, (source, sk, path_tpl) in enumerate(_skill_roots()):
        try:
            names = sorted(os.listdir(sk))
        except Exception:
            continue
        for name in names:
            meta = _skill_frontmatter(sk, name)
            if not meta:
                continue
            nm, desc = meta
            rank = (root_idx, _skill_candidate_rank(name, nm))
            cur = by_skill.get(nm)
            if cur is None or rank < cur[0]:
                by_skill[nm] = (rank, source, name, path_tpl.format(name), desc)
    repo_rows, global_names = [], []
    for nm in sorted(by_skill):
        _rank, source, dir_name, skill_path, desc = by_skill[nm]
        if source not in visible:
            continue
        if source == "global":
            global_names.append(f"`{nm}`" if dir_name == nm else f"`{nm}`(目录 `{dir_name}`)")
            continue
        short = re.split(r"[。\n(（]", desc, maxsplit=1)[0][:55] if desc else ""
        hint = f" ({skill_path})" if dir_name != nm else ""
        repo_rows.append(f"- `{nm}`{hint}:{short}" if short else f"- `{nm}`{hint}")
    if not repo_rows and not global_names:
        return ""
    parts = []
    if repo_rows:
        parts.append("\n".join(repo_rows))
    if global_names:
        parts.append("全局已安装技能(只列名称;按需读 `~/.claude/skills/<name>/SKILL.md`):"
                     + "、".join(global_names))
    locations = []
    if "repo" in visible:
        locations.append("仓内技能读 `.claude/skills/<name>/SKILL.md`")
    if "global" in visible:
        locations.append("全局技能读 `~/.claude/skills/<name>/SKILL.md`")
    return ("## 可用技能(按需调用,详情读对应 SKILL.md)\n\n> "
            + ";".join(locations)
            + "。用户点名 skill 或任务明显匹配时先读正文。\n\n"
            + "\n".join(parts))

LEGACY_MARK = "grounding-injected"     # 注入成功的标记文件名(适配器写,见 engine_adapters.grounding)
LEGACY_MARK_TTL = 7 * 86400            # 标记多久算过期:换了旧版本或注入坏掉时自动退回写文件


def _legacy_agents_md_off():
    """合成 AGENTS.md 这条老路是否已退休。

    grounding 现在由 Desk 在起进程时显式注入(grounding/builder.py),合成文件那条路
    ——写进域目录、靠引擎起手自己读盘——就多余了,而且它只在 cwd 落域目录时生效,
    覆盖不到归属为 _desktop 的会话。

    退休不设人工开关,由注入自己证明:适配器每次成功构造出系统约定就刷新
    `.workbench/grounding-injected` 的时间戳,这里见到新鲜的标记才停写。
    注入坏掉或回退到旧版本时标记会过期,老路自动恢复,不会两条路同时断掉。
    DESKTRON_GROUNDING_INJECTED=0 可强制保留老路(排查用)。
    """
    if os.environ.get("DESKTRON_GROUNDING_INJECTED") == "0":
        return False
    mark = os.path.join(REPO_ROOT, ".workbench", LEGACY_MARK)
    try:
        return time.time() - os.path.getmtime(mark) < LEGACY_MARK_TTL
    except OSError:
        return False


def mark_grounding_injected():
    """记一次成功注入。适配器每回合调用,失败静默——这只是个开关信号,不该挡住回合。

    标记由无到有(或从过期转新鲜)时顺带扫一遍域目录清旧合成文件。只在 _ensure_domain_dir
    里清不够:那里要该域被会话触碰才会跑,没有会话进去的域会一直留着一份不再更新的快照,
    而 cwd 落进去的引擎照读不误(2026-08-21 重启后实测,7 个域留着旧文件)。
    """
    try:
        d = os.path.join(REPO_ROOT, ".workbench")
        os.makedirs(d, exist_ok=True)
        first = not _legacy_agents_md_off()
        with open(os.path.join(d, LEGACY_MARK), "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
        if first:
            sweep_legacy_agents_md()
    except Exception:
        pass


def sweep_legacy_agents_md():
    """扫全部域目录,清掉旧合成文件。幂等,可单独调用。返回清掉的域数。"""
    n = 0
    base = os.path.join(REPO_ROOT, "domains")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return 0
    for name in names:
        d = os.path.join(base, name)
        if not os.path.isdir(d):
            continue
        if os.path.isfile(os.path.join(d, "AGENTS.md")) or os.path.isfile(os.path.join(d, "CLAUDE.md")):
            _drop_legacy_agents_md(d)
            n += 1
    return n


def _drop_legacy_agents_md(d):
    """退休后清掉域目录里的旧合成文件:留着会被 cwd 落域目录的引擎读到,那是一份不再更新的快照。"""
    for name in ("AGENTS.md", "CLAUDE.md"):
        try:
            os.remove(os.path.join(d, name))
        except OSError:
            pass


def _ensure_domain_dir(domain):
    """确保域目录存在:软链共享资产(skills/配置/知识/基座)+ 合成 AGENTS.md(起手地图 + 项目宪法 + 该工作区说明)。
    cwd 切到这里后,codex 家族原生读 AGENTS.md;claude 不读 AGENTS.md,经同目录 CLAUDE.md(`@AGENTS.md` import)拿同一份。
    合成顺序即分层:起手地图(orientation) → 项目宪法 → 工作区说明。详细记忆/知识约定不整份注入,orientation 给要点。
    指定了 dir 的工作区(如 Desk 开发,cwd=真实源码仓):不做脚手架,用仓库自带 grounding,也不写 memory 软链(不污染仓库)。
    支持 workspace:sub 格式:cwd/资产均落在冒号前的工作区,sub 仅用于终端归属标识。
    容器子区(容器:子区)分三类:带职能引用(导入)→ 按被引用工作区自身域脚手架(cwd/记忆都在原工作区);
    编码子区(引用代码仓)→ 真实仓不碰;自定义(无引用)→ cwd 落项目内独立子目录,记忆/grounding/说明
    按容器键同源,并把本子区的规范片段拼进子区自己的 AGENTS.md。"""
    ws = domain.split(":")[0] if domain and ":" in domain else domain
    d = _domain_dir(domain)
    if d == REPO_ROOT:
        return d
    if _domain_dir_override(ws):
        return d   # 外部真实仓:仓库自带 AGENTS/.claude/grounding,工作台不碰
    try:
        os.makedirs(d, exist_ok=True)
        # 共享资产软链(单一源在 REPO_ROOT);第三方 Git KB 在 REPO_ROOT/kb/ 下显式注册。
        for link in (".claude", "prompts", "knowledge", "common", ".workbench"):
            src = os.path.join(REPO_ROOT, link)
            dst = os.path.join(d, link)
            if os.path.exists(src) and not os.path.lexists(dst):
                try: os.symlink(src, dst)
                except Exception: pass
        # 显式注入已经接管时，不再先读取任务、记忆、技能并拼出一份马上丢弃的旧正文。
        # 保留资产供给与旧文件清理；标记过期/强制开关仍走下面的完整合成路径。
        if _legacy_agents_md_off():
            _drop_legacy_agents_md(d)
            _provision_memory(d, ws)
            return d
        # 合成段落:起手地图(orientation) + 项目宪法(AGENTS) + 工作区说明,以 --- 分隔。
        # 详细记忆/知识约定不整份注入(memory-convention ~41% 体量):orientation 给要点,详细按需读 common/*.md 与 SKILL.md。
        segs = []
        # 表达约束(prompts/style.md)不在这里注:三引擎都已经从 system prompt 拿到同一份
        # ——claude 走 --append-system-prompt,codex 家族走首回合正文的〈系统约定〉
        # (见 engine_adapters 的 style_body / SEED_HEAD)。此前两条路各注一遍,同一份
        # 720 字逐字重复(2026-08-16 核对)。
        for path in (os.path.join(REPO_ROOT, "common", "orientation.md"),   # 起手地图:你在哪/有什么/怎么连(引擎无关常驻总图,放最前)
                     os.path.join(REPO_ROOT, "AGENTS.md")):
            try:
                with open(path, encoding="utf-8") as f:
                    t = f.read().strip()
                    if t: segs.append(t)
            except Exception: pass
        try:
            with open(_workspace_md_path(ws), encoding="utf-8") as f:
                t = f.read().strip()
                if t: segs.append(t)
        except Exception: pass
        rs = _repos_segment(ws, d)   # 引用的代码仓清单:告知 agent 去哪里工作,不向真实仓写脚手架
        if rs:
            segs.append(rs)
        # 当前待办快照(TASKS.md 在本目录=会话 cwd,人在面板勾选与 agent 编辑同一份):
        # 未完成+进行中列标题,已完成只报数量;清单空则整段不注入。跨会话/跨引擎 resume 起手即带,
        # 不靠用户或 agent 额外动作去翻文件。
        ti = _tasks.agents_tasks_segment(os.path.join(d, _tasks.TASKS_MD_NAME), REPO_ROOT)   # 备忘只给索引(一句提示 + note id),全文按需拉
        if ti:
            segs.append(ti)
        ci = _context_segment(ws)   # 项目参考资料(Context):落地页上传的文档与飞书链接清单,按需读取不整份注入
        if ci:
            segs.append(ci)
        gi = _grounding_index(ws)   # 项目记忆、文档导航与历史资料入口
        if gi:
            segs.append(gi)
        if _legacy_agents_md_off():
            _drop_legacy_agents_md(d)
            body = ""
        else:
            body = "\n\n---\n\n".join(segs)
        if body:
            try:
                with open(os.path.join(d, "AGENTS.md"), "w", encoding="utf-8") as f: f.write(body + "\n")
                # claude 不原生读 cwd AGENTS.md(实测只读 CLAUDE.md);用 @import 让它拿同一份合成 body,
                # codex 家族读 AGENTS.md、这一行无害不重复加载;PTY resume 也覆盖(claude 每次重读 CLAUDE.md)。
                with open(os.path.join(d, "CLAUDE.md"), "w", encoding="utf-8") as f: f.write("@AGENTS.md\n")
            except Exception: pass
        _provision_memory(d, ws)   # 单入口:清旧 memory 软链挂点 + 补工作层 gitignore(数据只经命令访问)
    except Exception:
        return REPO_ROOT
    return d


def _workspace_dirs():
    """所有工作区目录(REPO_ROOT + 各 domain 目录 + coding 项目的真实仓目录),会话扫描取并集用。
    项目目录必须在列:否则项目里的 claude transcript / codex rollout 扫不到,休眠列表丢失(重启后会话消失)。"""
    dirs = [REPO_ROOT]
    try:
        base = os.path.join(REPO_ROOT, "domains")
        for n in sorted(os.listdir(base)):
            p = os.path.join(base, n)
            if os.path.isdir(p):
                dirs.append(p)
    except Exception:
        pass
    try:
        for d in _wb_domains_read():
            if d.get("dir"):
                ov = _domain_dir_override(d.get("id"))
                if ov and ov not in dirs:
                    dirs.append(ov)
            for r in (d.get("repos") or []):   # 工作流引用的代码仓:导入其中的会话后要解析得到,
                if isinstance(r, dict) and str(r.get("path") or "").strip():   # 否则 meta 有记录却进不了列表
                    rp = os.path.abspath(os.path.expanduser(str(r["path"])))
                    if os.path.isdir(rp) and rp not in dirs:
                        dirs.append(rp)
    except Exception:
        pass
    return dirs
