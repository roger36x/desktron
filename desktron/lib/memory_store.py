"""本机记忆工作层的目录与读取(运行态 .memory/,不进仓库)。

布局(2026-07-07 修订 v3,语义正交分层;单一出口 _workspace_layout,memory skill 写入闸共用):
- <ws>/journal 流水:发生过什么。隔离 recap 自动或按请求生成,后端免闸只追加(旧名 s0)
- <ws>/notes  项目模型:记忆引擎整篇重写的当前态,免闸(旧名 s1)
- <ws>/sot    SOT:现在是什么。每工作区固定几篇主题文档,有界、合入不追加,过闸人审编辑(旧 shared 改造)
- _sealed     红线封存:什么不能出本机。用户全局、与分层正交(旧名 private)
promote=把成熟笔记的结论合入 SOT 文档(人审编辑动作),不再是挪文件。

单入口模型:agent 不持有工作层文件路径(cwd 不再软链 memory/),读写全部经 curator 命令。
布局契约、懒迁移(v2→v3 链)、band 名单一源都在 memory-engine(memory_engine.layout);
本模块是 Desk 侧薄层:deskcfg 根解析 + imports 挂载点 + gitignore/软链退役 + UI 服务端直读。
"""
import os
import re

from deskcfg import REPO_ROOT


def _memory_root():
    """记忆工作层共享根(运行态,不进仓库)。布局契约与懒迁移在 memory-engine(layout.migrate 链)。"""
    root = os.path.join(REPO_ROOT, ".memory")
    _engine_layout().migrate(root)
    return root


def _engine_layout():
    from memory_engine import layout as _l
    return _l


# 记忆 band 名与别名:单一源在 memory_engine.layout,此处 re-export(Desk 内旧引用兼容)。
def __getattr__(name):
    if name == "_MEM_BUCKETS":
        return _engine_layout().BUCKETS
    if name == "_BUCKET_ALIAS":
        return _engine_layout().ALIAS
    raise AttributeError(name)


# 工作层 gitignore pattern:运行态 WORKSPACE 是本地 git(无远端),仍显式挡住工作层入 git。
# knowledge 是运行时 KB 实例(种子 copy 出来的真实目录,非软链),同 .memory 一样不进 workspace git。
# domains/*/memory 是旧软链挂点(已退役),pattern 保留兜住存量;domains/*/temp 是纯临时产出物。
_WORKLAYER_IGNORES = (".memory/", "knowledge/", "domains/*/memory", "domains/*/imports", "domains/*/temp")


def _workspace_layout(domain):
    """工作区 id → 工作层绝对路径(引擎契约 + Desk 特有的 imports 挂载点)。唯一出口。"""
    lay = _engine_layout().workspace_layout(_memory_root(), domain)
    lay["imports"] = os.path.join(REPO_ROOT, "domains", domain, "imports")
    return lay


def _ensure_worklayer_gitignore(repo_root):
    """幂等把工作层 pattern 补进 <repo_root>/.gitignore。
    运行态 REPO_ROOT=WORKSPACE 那个本地 git 没有 .gitignore,源码仓的也不软链进来,
    故运行时补一次;源码仓已带这些 pattern 时(开发态 REPO_ROOT=源码仓)此处为 no-op。"""
    gi = os.path.join(repo_root, ".gitignore")
    try:
        existing = ""
        if os.path.exists(gi):
            with open(gi, encoding="utf-8") as f:
                existing = f.read()
        have = {l.strip() for l in existing.splitlines()}
        miss = [p for p in _WORKLAYER_IGNORES if p not in have]
        if miss:
            with open(gi, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write("# desktron 工作层(运行态,不入 git)\n" + "\n".join(miss) + "\n")
    except Exception:
        pass


def _provision_memory(d, domain):
    """单入口模型:cwd 不再软链 memory/(agent 读写全部经 curator 命令,不持有工作层路径)。
    本函数只剩两件事:清掉旧软链挂点(存量升级),补工作层 gitignore pattern。"""
    mem = os.path.join(d, "memory")
    try:
        if os.path.isdir(mem) and not os.path.islink(mem):
            # 目录里只有旧 band 软链(temp/private/shared)时整个撤走;有真实文件则不动(不删用户数据)
            entries = os.listdir(mem)
            if all(os.path.islink(os.path.join(mem, n)) for n in entries):
                for n in entries:
                    os.unlink(os.path.join(mem, n))
                os.rmdir(mem)
        _ensure_worklayer_gitignore(REPO_ROOT)
    except Exception:
        pass


def _memory_tree():
    """.memory/ 的目录树,原样返回(像文件浏览器 / git,不加工不翻译)。
    嵌套 {name, type:dir|file, path, children}。只含目录与 .md 文件;兜底建 _sealed
    (按区目录 <ws>/{s0,s1,shared} 由写入时创建,不预铺)。"""
    root = _memory_root()
    try: os.makedirs(os.path.join(root, "_sealed"), exist_ok=True)
    except Exception: pass
    def walk(d, rel):
        items = []
        try: names = sorted(os.listdir(d))
        except Exception: return items
        for n in names:
            if n.startswith("."):
                continue
            full = os.path.join(d, n)
            r = (rel + "/" + n) if rel else n
            if os.path.isdir(full):
                items.append({"name": n, "type": "dir", "path": r, "children": walk(full, r)})
            elif n.endswith(".md"):
                items.append({"name": n, "type": "file", "path": r})
        # 目录在前,文件在后
        items.sort(key=lambda x: (x["type"] != "dir", x["name"]))
        return items
    return walk(root, "")


def _record_access(full):
    """人在界面点开单条正文时记一次访问。

    口径与 agent 侧的 curator show 一致——取用了正文才算,目录树浏览与列表渲染不计。
    两条通道落同一份 `.curator/access.json`,让生命周期与 GC 的可达性判定拿到全量,
    不再只看得见 agent 那一半。打点失败不影响读取。"""
    try:
        from curator.config import Instance
        from curator.note import read_note
        from curator.state import record_access
        fm, _body = read_note(full)
        nid = fm.get("id")
        if nid:
            record_access(Instance(root=_memory_root()), nid)
    except Exception:
        pass


def _memory_read(rel, count=False):
    """rel = .memory/ 下的相对路径(可多级,如 area/incident/foo.md)。限定在根内、限 .md。
    count=True 时记一次访问(只有「界面点开单条」这一个入口传)。"""
    root = _memory_root()
    if not rel or not rel.endswith(".md") or ".." in rel.split("/"):
        return None
    full = os.path.realpath(os.path.join(root, rel))
    rootr = os.path.realpath(root)
    if not (full == rootr or full.startswith(rootr + os.sep)):
        return None
    try:
        with open(full, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return None
    if count:
        _record_access(full)
    return text


def _memory_edit(rel, body, by="user"):
    """用户在 UI 编辑记忆:正文整体替换。路径纪律:限 .memory 内 .md;_sealed(红线封存)不可编辑。
    走 curator 闸(update --body-stdin),confidence 由内核按 source 的独立来源数重算。
    2026-08-21 起不再「编辑即核实」——#75 当时把用户编辑等同 verified,而顶档已改为
    corroborated,判据是引用了几个独立来源;编辑正文不产生新来源,不该动档位。
    id 不在索引时(机器写的 memo-* 等,curator 索引未收编)仅对免闸层(notes/journal/staging)
    直改文件并刷 updated;过闸层(sot/_sealed)绝不绕闸,直接报错。"""
    import subprocess as _sp
    # 用 Desk 自带的入口,不赖 PATH:2026-08-13 由 kbkit 更名为 curator 后,PATH 上的
    # ~/.local/bin/curator 要等装机(desktron doctor)才出现,期间按名字找会落空。
    exe = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "curator")
    if not os.path.isfile(exe):
        exe = "curator"
    root = _memory_root()
    if not rel or not rel.endswith(".md") or ".." in rel.split("/"):
        return {"ok": False, "error": "路径非法"}
    if rel.split("/")[0] == "_sealed" or "/_sealed/" in rel:
        return {"ok": False, "error": "_sealed 为红线封存区,不可在 UI 编辑"}
    full = os.path.realpath(os.path.join(root, rel))
    rootr = os.path.realpath(root)
    if not full.startswith(rootr + os.sep) or not os.path.isfile(full):
        return {"ok": False, "error": "记忆不存在"}
    try:
        with open(full, encoding="utf-8") as f:
            txt = f.read()
    except Exception as e:
        return {"ok": False, "error": f"读取失败:{str(e)[:80]}"}
    m = re.match(r"\A(---\n.*?\n---\n)(.*)\Z", txt, re.S)
    if not m:
        return {"ok": False, "error": "缺 frontmatter,不像记忆笔记"}
    fm_text = m.group(1)
    idm = re.search(r"^id:\s*(\S+)\s*$", fm_text, re.M)
    nid = idm.group(1) if idm else ""
    body = str(body or "").rstrip() + "\n"
    if nid:   # 走闸:update 改正文,confidence 由内核按 source 重算
        try:
            r1 = _sp.run([exe, "--root", root, "update", "--id", nid, "--body-stdin"],
                         input=body, capture_output=True, text=True, timeout=30)
            if r1.returncode == 0:
                # Desk 有全局 SIGCHLD 回收器；它先于 subprocess.waitpid 收走状态时，
                # Python 可能把实际退出 2 的 curator 误记成 0。不能只信 returncode：
                # 复读目标文件确认正文真的落下，避免 sot 闸失败被误报成成功、notes
                # 也因此跳过直改回退（全量测试与真实常驻进程都会遇到）。
                try:
                    with open(full, encoding="utf-8") as f:
                        after = f.read()
                    am = re.match(r"\A---\n.*?\n---\n(.*)\Z", after, re.S)
                    if am and am.group(1).rstrip() == body.rstrip():
                        return {"ok": True, "method": "curator"}
                except Exception:
                    pass
        except Exception:
            pass
    parts = rel.split("/")
    free_tier = any(seg in ("notes", "journal", "s0", "staging", "temp") for seg in parts[:-1])
    if not free_tier:
        return {"ok": False, "error": "该层过闸(sot),curator 不可用时不绕闸;先修索引或用命令行处理"}
    import datetime as _d
    today = _d.date.today().isoformat()
    # 不动 confidence:它由 source 的独立来源数决定,改正文不产生新来源。
    fm2, n_upd = re.subn(r"^updated:.*$", f"updated: {today}", fm_text, count=1, flags=re.M)
    if not n_upd:
        fm2 = fm2.rstrip()[:-3].rstrip() + f"\nupdated: {today}\n---\n"
    tmp = full + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(fm2 + body)
    os.replace(tmp, full)
    return {"ok": True, "method": "direct"}


def _fm(path):
    """读取 Markdown frontmatter；项目摘要可能被 YAML 折成多行，不能按单行截断。"""
    try:
        from curator.note import read_note
        fm, _body = read_note(path)
        return fm
    except Exception:
        return {}


def _mem_src_type(s):
    """从 source 文本推断来源类型:群聊(lark) / 代码仓(repo) / 文档(doc) / 其它。"""
    low = (s or "").lower()
    if "chat_id" in low or "lark" in low or "群" in (s or ""):
        return "lark"
    if "repo" in low or "git" in low or "仓" in (s or ""):
        return "repo"
    if "wiki" in low or "http" in low or "doc" in low or "文档" in (s or ""):
        return "doc"
    return "other"


def _mem_src_label(s):
    """来源明细:取 source 首行,截断展示。"""
    s = (s or "").split("\n")[0].strip()
    return (s[:48] + "…") if len(s) > 48 else s


def _mem_title(path, fm, mid):
    """记忆可读标题:优先 frontmatter summary;缺失则回落正文首个非空行(去 # / 引用符),截断;再兜底 id。
    老记忆常没写 summary,直接暴露 id 不可读——回落正文首行,保证总览/面板可读(不因缺字段变回 id)。"""
    t = (fm.get("summary") or "").strip()
    if t:
        return t
    try:
        with open(path, encoding="utf-8") as f:
            body = f.read(4096)
    except Exception:
        return mid
    if body.startswith("---"):   # 跳过 frontmatter
        e = body.find("\n---", 3)
        if e >= 0:
            nl = body.find("\n", e + 4)
            body = body[nl + 1:] if nl >= 0 else ""
    for ln in body.splitlines():
        s = ln.strip().lstrip("#").lstrip(">").strip()
        if s:
            return (s[:60] + "…") if len(s) > 60 else s
    return mid


def _collect_ws_memory(ws, origin="", origin_name=""):
    """单个工作区键的记忆条目(sot+notes 两层)。origin/origin_name 标注来源域(聚合视图区分哪个子区来的)。"""
    lay = _workspace_layout(ws)
    out = []
    for band in ("sot", "notes"):
        d = lay[band]
        try:
            names = sorted(os.listdir(d))
        except Exception:
            continue
        for n in names:
            if not n.endswith(".md") or n.startswith("."):
                continue
            fm = _fm(os.path.join(d, n))
            mid = fm.get("id") or n[:-3]
            src = fm.get("source") or ""
            try:
                mt = os.path.getmtime(os.path.join(d, n))
            except Exception:
                mt = 0
            is_rollup = mid.startswith("rollup-") or fm.get("role") == "rollup"
            links = fm.get("links") or []
            if isinstance(links, str):
                links = [x.strip() for x in links.strip("[]").split(",") if x.strip()]
            elif not isinstance(links, list):
                links = []
            updated = fm.get("updated") or fm.get("created") or ""
            # YAML 会把未加引号的 2026-08-31 解析成 datetime.date。接口直接 json.dumps
            # 会因此中断连接，前端只剩「加载中…」。传输边界统一转成 ISO 字符串。
            if hasattr(updated, "isoformat"):
                updated = updated.isoformat()
            elif not isinstance(updated, str):
                updated = str(updated)
            out.append({
                "id": mid, "path": "%s/%s/%s" % (ws, band, n),
                "summary": _mem_title(os.path.join(d, n), fm, mid),
                "srcType": _mem_src_type(src), "srcLabel": _mem_src_label(src),
                "type": fm.get("type") or "", "confidence": fm.get("confidence") or "",
                "updated": updated,
                "links": [str(x).strip() for x in links if str(x).strip()],
                "rollup": is_rollup,
                "sot": band == "sot",
                "mtime": int(mt * 1000),
                "origin": origin, "originName": origin_name,
            })
    return out


def _project_memory(ws):
    """工作流页面只列记忆引擎生成的项目模型，summary 仅作显示名称。

    旧 notes/SOT 是生成项目模型的原料，不再作为产品界面的替代列表；没有 rollup 时
    明确返回空态，避免尚未迁移的工作流突然重新暴露几十条事件笔记。
    """
    if not ws or not re.fullmatch(r"[\w:-]+", ws):
        return []
    out = _collect_ws_memory(ws)
    rollups = [item for item in out if item["rollup"]]
    return sorted(rollups, key=lambda x: -x["mtime"])
