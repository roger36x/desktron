"""工作台本地状态与资产存储(不经引擎):domains / apps / hidden / state + git 知识库。

纯文件 I/O 与 git 操作,依赖 deskcfg(路径常量)与 credstore(_secrets_read,_author_id 用)。
ui_server 的 workbench 路由 import 回这些名字调用,调用点不变。
"""
import os, json, re, subprocess, threading

from deskcfg import REPO_ROOT, DATA_HOME, FRONTEND, ENGINES
from credstore import _secrets_read

# ---- 工作台本地 API(不经引擎):状态存储 / 资产扫描 / 文件读取 ----
WB_STATE = os.path.join(REPO_ROOT, ".workbench", "state.json")
WB_APPS = os.path.join(REPO_ROOT, ".workbench", "apps.json")
WB_DOMAINS = os.path.join(REPO_ROOT, ".workbench", "domains.json")
WB_HIDDEN = os.path.join(REPO_ROOT, ".workbench", "hidden.json")
_WB_STATE_LOCK = threading.RLock()
# 用户主动移除过的 app(墓碑):开机兜底(bootstrap_default_workspace_apps)与 reconcile
# 会按发行版声明与安装记录重新注册,不记这一笔的话,用户删掉的应用下次启动又回来。
# 记 id 与 uid 两个键:发行版换作者命名空间时 uid 会变,id 仍能拦住。
WB_APPS_REMOVED = os.path.join(REPO_ROOT, ".workbench", "apps-removed.json")
WB_WS_DIR = os.path.join(REPO_ROOT, ".workbench", "workspaces")
WB_EXT = (".md", ".sh", ".yaml", ".yml", ".json", ".txt")


def _wb_apps_read():
    """本地应用注册表(扁平):[{...,uid,targets:[...]}, ...]。一份定义,targets 列出生效的工作区:
    取 _desktop / 域 id / "*"(全部)。兼容旧结构 {domainId:[app,...]}:读时迁移成扁平,每条 targets=[该 key]。"""
    try:
        with open(WB_APPS, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return []
    if isinstance(d, list):
        return [a for a in d if not isinstance(a, dict) or a.get("page") != "lark-events"]
    if isinstance(d, dict):
        out = []
        for k, lst in d.items():
            for a in (lst or []):
                if isinstance(a, dict) and a.get("page") != "lark-events":
                    a = dict(a)
                    a.setdefault("targets", [k])
                    out.append(a)
        return out
    return []


def _wb_apps_removed_read():
    try:
        with open(WB_APPS_REMOVED, encoding="utf-8") as f:
            d = json.load(f)
        return {"ids": list(d.get("ids") or []), "uids": list(d.get("uids") or [])}
    except Exception:
        return {"ids": [], "uids": []}


def _wb_apps_removed_write(d):
    os.makedirs(os.path.dirname(WB_APPS_REMOVED), exist_ok=True)
    tmp = WB_APPS_REMOVED + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"ids": sorted(set(d.get("ids") or [])), "uids": sorted(set(d.get("uids") or []))},
                  f, ensure_ascii=False, indent=1)
    os.replace(tmp, WB_APPS_REMOVED)


def wb_app_tombstone(app_id="", uid=""):
    """记一笔「用户删过它」。删除是用户的意图,开机逻辑不该把它覆盖回来。"""
    d = _wb_apps_removed_read()
    if app_id:
        d["ids"].append(app_id)
    if uid:
        d["uids"].append(uid)
    _wb_apps_removed_write(d)


def wb_app_tombstone_clear(app_id="", uid=""):
    """用户显式重新安装时清墓碑——那同样是意图,而且更近。"""
    d = _wb_apps_removed_read()
    d["ids"] = [x for x in d["ids"] if x != app_id]
    d["uids"] = [x for x in d["uids"] if x != uid]
    _wb_apps_removed_write(d)


def wb_app_removed(app_id="", uid=""):
    d = _wb_apps_removed_read()
    return (app_id and app_id in d["ids"]) or (uid and uid in d["uids"])


def _wb_apps_write(data):
    os.makedirs(os.path.dirname(WB_APPS), exist_ok=True)
    tmp = WB_APPS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, WB_APPS)


def _wb_apps_expand(flat=None):
    """扁平注册表 → {_desktop:[...], <domain>:[...]}:按每条 targets 展开,"*" 展到所有已知域 + _desktop。
    供前端按工作区取应用(渲染心智不变);一份定义可出现在多个工作区(引用,非拷贝)。"""
    flat = _wb_apps_read() if flat is None else flat
    ids = set()
    try:
        with open(os.path.join(FRONTEND, "workbench.json"), encoding="utf-8") as f:
            ids |= {d.get("id") for d in (json.load(f).get("domains") or []) if d.get("id")}
    except Exception:
        pass
    ids |= {d.get("id") for d in _wb_domains_read() if d.get("id")}
    out = {}
    for a in flat:
        if a.get("page") == "lark-events":
            continue
        tg = a.get("targets") or ["_desktop"]
        keys = (ids | {"_desktop"}) if "*" in tg else set(tg)
        for k in keys:
            out.setdefault(k, []).append(a)
    return out


def _domain_record_normalize(row):
    """运行态工作流记录升级到 desk_role schema,不把旧 agent 隐式迁成业务角色。"""
    out = dict(row or {})
    out.pop("agent", None)
    role = str(out.get("desk_role") or "").strip()
    out["desk_role"] = (role if role != "diagnose" and re.fullmatch(r"[a-z0-9_-]+", role)
                        else "")
    return out


def _wb_domains_read():
    """运行态工作区注册表(用户/引擎新建的工作区落此,与 workbench.json 分发域并集渲染)。
    结构:[{id,name,desc,desk_role?,icon,apps:[],subareas?}, ...]。desk_role 缺省为空；
    旧 agent 字段只丢弃、不隐式迁移。分发物 frontend/workbench.json 不被运行时改写。
    容器条目(kind:container)的子区清单是机器数据,直接存在这份 JSON 里;曾短暂把 goal/subareas/tasks
    迁进 domains/<id>/PROJECT.md(结构化 markdown),按「精简、说明自由体化」的方向已退役——
    此处对存量 PROJECT.md 做一次性回迁(子区表 → domains.json,任务/进度弃用),迁完删文件。
    容器的说明与工作区同机制(.workbench/workspaces/<id>.md,经 /workbench/workspace 读写)。"""
    try:
        with open(WB_DOMAINS, encoding="utf-8") as f:
            d = json.load(f)
            doms = [_domain_record_normalize(x) for x in d if isinstance(x, dict)] \
                if isinstance(d, list) else []
    except Exception:
        doms = []
    return doms


def _wb_domains_write(data):
    os.makedirs(os.path.dirname(WB_DOMAINS), exist_ok=True)
    tmp = WB_DOMAINS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump([_domain_record_normalize(x) for x in (data or []) if isinstance(x, dict)],
                  f, ensure_ascii=False, indent=2)
    os.replace(tmp, WB_DOMAINS)


def _wb_hidden_read():
    """本机隐藏的工作区 id 列表(从 desktop/rail 移除,可恢复)。隐藏是本机安装状态,不动官方定义。"""
    try:
        with open(WB_HIDDEN, encoding="utf-8") as f:
            d = json.load(f)
            return [x for x in d if isinstance(x, str)] if isinstance(d, list) else []
    except Exception:
        return []


def _wb_hidden_write(ids):
    os.makedirs(os.path.dirname(WB_HIDDEN), exist_ok=True)
    tmp = WB_HIDDEN + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(set(ids)), f, ensure_ascii=False, indent=2)
    os.replace(tmp, WB_HIDDEN)


# ---- 已装包记账:pkgstore 的卸载/更新依据(每装一个包一条) ----
WB_INSTALLED = os.path.join(REPO_ROOT, ".workbench", "installed.json")


def _wb_installed_read():
    """已装包列表:[{name,address,url,ref,subpath,repo_slug,resolved_commit,version,provides,dep_apps,installed_at}, ...]。
    pkgstore 据此做卸载逆操作、更新差量、引用计数。缺文件/坏文件 → 空列表。"""
    try:
        with open(WB_INSTALLED, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, list) else []
    except Exception:
        return []


def _wb_installed_write(data):
    os.makedirs(os.path.dirname(WB_INSTALLED), exist_ok=True)
    tmp = WB_INSTALLED + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, WB_INSTALLED)


# ---- git 知识库:注册表 + 独立目录 clone + workspace 软链(供 _wb_file 读取) ----
WB_KB_GIT = os.path.join(REPO_ROOT, ".workbench", "kb-git.json")
KB_GIT_HOME = os.path.join(os.path.dirname(DATA_HOME), "kb")   # clone 落点:~/.local/share/desktron/kb/<name>


def _kb_git_read():
    """git 知识库注册表:[{name, url}, ...]。本地路径桥接库不进此表。"""
    try:
        with open(WB_KB_GIT, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, list) else []
    except Exception:
        return []


def _kb_git_write(data):
    os.makedirs(os.path.dirname(WB_KB_GIT), exist_ok=True)
    tmp = WB_KB_GIT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, WB_KB_GIT)


def _kb_name_from_url(url):
    base = url.rstrip("/").split("/")[-1]
    if base.endswith(".git"):
        base = base[:-4]
    return re.sub(r"[^a-z0-9_-]", "-", base.lower()).strip("-")


def _kb_repo_scan(abs_dir, path_prefix):
    """扫一个知识库仓:git head + 顶层 .md(root_files)+ 子目录分组(.md/.yaml)。
    path_prefix 是 workspace 相对前缀(末尾带 /),给前端读取用。"""
    _nat = lambda s: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]
    info = {"head": "", "root_files": [], "groups": []}
    try:
        info["head"] = subprocess.run(
            ["git", "-C", abs_dir, "log", "-1", "--format=%h %ad", "--date=short"],
            capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        pass
    for f in sorted(os.listdir(abs_dir)):
        if f.endswith(".md"):
            info["root_files"].append({"name": f, "path": path_prefix + f})
    for d in sorted(os.listdir(abs_dir)):
        sub = os.path.join(abs_dir, d)
        if d.startswith(".") or not os.path.isdir(sub):
            continue
        files = []
        for dirpath, dirs, fs in os.walk(sub, followlinks=True):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            for f in fs:
                if f.endswith((".md", ".yaml", ".yml")):
                    full = os.path.join(dirpath, f)
                    files.append({"name": os.path.relpath(full, sub),
                                  "path": path_prefix + os.path.relpath(full, abs_dir)})
        if files:
            files.sort(key=lambda x: _nat(x["name"]))
            info["groups"].append({"dir": d, "count": len(files), "files": files})
    return info


def _kb_git_link(name):
    """clone 目录软链进 workspace/kb/<name>(幂等),让 _wb_file 能按相对路径读到。"""
    link = os.path.join(REPO_ROOT, "kb", name)
    target = os.path.join(KB_GIT_HOME, name)
    try:
        os.makedirs(os.path.join(REPO_ROOT, "kb"), exist_ok=True)
        if os.path.islink(link):
            if os.readlink(link) != target:
                os.unlink(link); os.symlink(target, link)
        elif not os.path.exists(link):
            os.symlink(target, link)
    except Exception:
        pass


def _kb_git_clone(url, name):
    """clone 到独立目录并建软链。已存在则 pull 更新。返回 (ok, err)。"""
    dest = os.path.join(KB_GIT_HOME, name)
    try:
        os.makedirs(KB_GIT_HOME, exist_ok=True)
        if os.path.isdir(os.path.join(dest, ".git")):
            subprocess.run(["git", "-C", dest, "pull", "--ff-only"],
                           capture_output=True, text=True, timeout=120)
        else:
            r = subprocess.run(["git", "clone", "--depth", "1", url, dest],
                               capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                return False, (r.stderr or "clone 失败").strip()[:400]
    except subprocess.TimeoutExpired:
        return False, "git 操作超时"
    except Exception as e:
        return False, str(e)[:400]
    _kb_git_link(name)
    return True, ""


def _kb_git_remove(name):
    import shutil as _sh
    link = os.path.join(REPO_ROOT, "kb", name)
    dest = os.path.join(KB_GIT_HOME, name)
    try:
        if os.path.islink(link): os.unlink(link)
    except Exception:
        pass
    try:
        if os.path.isdir(dest): _sh.rmtree(dest, ignore_errors=True)
    except Exception:
        pass


def _builtin_domain_ids():
    """分发版 workbench.json 里的域 id(运行态新建不得与之撞)。"""
    try:
        with open(os.path.join(FRONTEND, "workbench.json"), encoding="utf-8") as f:
            return {d.get("id") for d in (json.load(f).get("domains") or [])}
    except Exception:
        return set()


def _workspace_md_path(domain):
    """工作区说明 markdown 的解析路径:运行态(.workbench/workspaces/<id>.md)优先,
    回落分发版(frontend/workspaces/<id>.md)。运行态新建的工作区其描述不进仓库分发物。"""
    rt = os.path.join(WB_WS_DIR, domain + ".md")
    if os.path.isfile(rt):
        return rt
    return os.path.join(FRONTEND, "workspaces", domain + ".md")


def _author_id():
    """本地作者身份(应用 uid 的作者段,跨用户不撞)。来源优先级:
    identity 文件 > SECRETS 的 DESKTRON_AUTHOR > 机器用户名。多用户/市场化后接公司 SSO。"""
    import getpass
    val = ""
    try:
        with open(os.path.expanduser("~/.config/desktron/identity"), encoding="utf-8") as f:
            val = f.read().strip().splitlines()[0].strip()
    except Exception:
        val = _secrets_read().get("DESKTRON_AUTHOR", "")
    if not val:
        try:
            val = getpass.getuser()
        except Exception:
            val = "local"
    val = re.sub(r"[^a-z0-9_-]", "", val.lower()) or "local"
    return val[:24]


def _app_uid(short, author=None):
    """应用全局唯一稳定 id:<作者>.<短名>.<6位随机>。注册时生成,生成后不变。
    author 缺省=本机用户(make-app 的本机应用);装包时传包作者,使官方/第三方包的 app
    与用户自建 app 落在不同命名空间,避免同 id 互相覆盖/误删。"""
    import binascii
    a = re.sub(r"[^a-z0-9_-]", "", (author or "").lower())[:24] or _author_id()
    suffix = binascii.hexlify(os.urandom(3)).decode()
    return f"{a}.{short}.{suffix}"


def _app_validate(app, require_operability=False):
    """应用结构校验；新写入传 require_operability=True，存量读取保持兼容。"""
    errs = []
    if not isinstance(app, dict):
        return ["应用必须是对象"]
    if not re.fullmatch(r"[a-z0-9_-]+", app.get("id") or ""):
        errs.append("id 缺失或非法(仅 a-z0-9_-)")
    if not (app.get("label") or "").strip():
        errs.append("label 缺失")
    try:
        from app_control.manifest import validate_app_operability
        errs.extend(validate_app_operability(app, required=require_operability))
    except Exception as e:
        errs.append("agent_operability 校验失败:%s" % str(e))
    if app.get("agentic"):
        for k in ("who", "what", "how"):
            if not (app.get(k) or "").strip():
                errs.append(f"agentic 应用缺 {k}")
        # params 占位必须在 who/how/where 文本里出现
        decl = " ".join(str(app.get(k) or "") for k in ("who", "what", "how", "where"))
        for p in app.get("params") or []:
            nm = p.get("name")
            if not nm:
                errs.append("params 条目缺 name")
            elif "${" + nm + "}" not in decl:
                errs.append(f"参数 ${{{nm}}} 在 who/what/how/where 里未被引用")
        skill = app.get("skill")
        if skill and not os.path.isdir(os.path.join(REPO_ROOT, ".claude", "skills", skill)):
            errs.append(f"引用的 skill 不存在:{skill}")
    else:
        pid = app.get("page") or app.get("id")
        local = os.path.join(REPO_ROOT, ".workbench", "pages", str(pid) + ".html")
        official = os.path.join(FRONTEND, "pages", str(pid) + ".html")
        if not (os.path.isfile(local) or os.path.isfile(official)):
            errs.append(f"非 agentic 应用页面不存在:.workbench/pages/{pid}.html")
    return errs


def _wb_state_read():
    with _WB_STATE_LOCK:
        try:
            with open(WB_STATE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


def _wb_state_write(data):
    with _WB_STATE_LOCK:
        os.makedirs(os.path.dirname(WB_STATE), exist_ok=True)
        tmp = WB_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, WB_STATE)


def _wb_state_patch(data):
    """合并客户端的局部更新，避免另一端的旧快照覆盖用户排序。"""
    if not isinstance(data, dict):
        return _wb_state_read()
    with _WB_STATE_LOCK:
        state = _wb_state_read()
        # 自动轮询发现新会话时，客户端手里可能还是另一端改序前的旧数组。
        # 这里只把真正的新 id 放到最前，已有 id 继续沿用服务端顺序；手工拖拽仍通过
        # sessionOrder 精确覆盖对应分组。
        pins = data.get("sessionOrderPins")
        if isinstance(pins, dict):
            orders = state.get("sessionOrder") if isinstance(state.get("sessionOrder"), dict) else {}
            orders = dict(orders)
            for domain, incoming in pins.items():
                if not isinstance(incoming, list):
                    continue
                current = orders.get(domain) if isinstance(orders.get(domain), list) else []
                seen = set(current)
                fresh = [item for item in incoming if item not in seen]
                orders[domain] = fresh + current
            state["sessionOrder"] = orders
        for key, value in data.items():
            if key == "sessionOrderPins":
                continue
            if key in ("sessionOrder", "ui") and isinstance(value, dict):
                current = state.get(key) if isinstance(state.get(key), dict) else {}
                state[key] = {**current, **value}
            else:
                state[key] = value
        _wb_state_write(state)
        return state


# 引擎偏好(运行态,存 workbench state):放此处让 engine 与 settings 都从下游 import,破 engine↔settings 环。
def _engine_pref():
    # 存量 state 里可能还写着已下线的引擎,一律回落到缺省的 claude。
    e = _wb_state_read().get("engine", "claude")
    return e if e in ENGINES else "claude"


def _engine_pref_set(name):
    _wb_state_patch({"engine": name})
