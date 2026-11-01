"""包安装器:填 git url 即装 workspace / app / KB。

一个包 = 某目录下一份 `desktron-pkg.yaml` + 内容(在仓根或子路径);装地址 `git-url[@ref][#subpath]`。
仓是容器,可装 1..n 个包;目录仓另有 `desktron-catalog.yaml` 只列条目。安装器做:
clone → 读清单 → 校验 → 审查(plan,只读)→ 确认后注册(install,confirm)→ 渲染。卸载是逆。

落点:clone 到 PKG_HOME(仓外,与 kb/ 同级);注册写运行态 .workbench/
(domains/apps/pages/workspaces 各表 + installed.json);依赖 KB 走显式 Git KB 注册表。
纯文件 I/O + git subprocess,全程软失败,依赖 deskcfg/workbench 单向向下。
本会话只实现内核(段1):无路由、无前端、不打包 skill(只声明依赖、缺则标待补)。
"""
import os, re, time, shutil, subprocess, json

from deskcfg import REPO_ROOT, DATA_HOME, FRONTEND
from workbench import (
    _wb_domains_read, _wb_domains_write, _wb_apps_read, _wb_apps_write,
    wb_app_removed, wb_app_tombstone_clear,
    _app_validate, _app_uid, _author_id, _wb_installed_read, _wb_installed_write,
    WB_WS_DIR, KB_GIT_HOME,
    _kb_git_read, _kb_git_write, _kb_git_clone, _kb_name_from_url,
)
from app_control.manifest import validate_app_operability


def _kb_present(name):
    """用户是否已有某个显式安装的第三方 Git KB。"""
    if not name:
        return False
    try:
        if any(x.get("name") == name for x in _kb_git_read()):
            return True
    except Exception:
        pass
    return os.path.isdir(os.path.join(KB_GIT_HOME, name))


def _required_kbs(data):
    """读包清单 requires.kb,规整成 [{name,url,ref}]。name 缺省可从 url 推。"""
    out = []
    for kb in ((data.get("requires") or {}).get("kb") or []):
        if not isinstance(kb, dict):
            continue
        url = (kb.get("url") or "").strip()
        name = (kb.get("name") or (_kb_name_from_url(url) if url else "")).strip()
        if name:
            out.append({"name": name, "url": url, "ref": kb.get("ref")})
    return out


def _ensure_required_kbs(data):
    """装包时确保 requires.kb 都在;已有则跳过(幂等),缺的按 url clone + 注册。
    返回 {installed, skipped, failed:[{name,error}]}。不回滚主安装,失败只上报。"""
    res = {"installed": [], "skipped": [], "failed": []}
    for kb in _required_kbs(data):
        name, url = kb["name"], kb["url"]
        if _kb_present(name):
            res["skipped"].append(name)
            continue
        if not url:
            res["failed"].append({"name": name, "error": "requires.kb 缺 url,无法安装"})
            continue
        ok, err = _kb_git_clone(url, name)
        if not ok:
            res["failed"].append({"name": name, "error": err})
            continue
        try:
            reg = _kb_git_read()
            if not any(x.get("name") == name for x in reg):
                reg.append({"name": name, "url": url})
                _kb_git_write(reg)
        except Exception:
            pass
        res["installed"].append(name)
    return res


def _required_skills(data):
    """读包清单 requires.skills,规整成 [name,...](容忍字符串或 {name} 两种写法)。"""
    out = []
    for sk in ((data.get("requires") or {}).get("skills") or []):
        name = (sk.get("name") if isinstance(sk, dict) else sk) or ""
        name = name.strip()
        if name:
            out.append(name)
    return out


def _ensure_required_skills(data):
    """检查 requires.skills。缺项由用户通过技能市场安装，不再隐式拉官方内容仓。"""
    req = _required_skills(data)
    res = {"installed": [], "skipped": [s for s in req if _has_skill(s)], "failed": []}
    need = [s for s in req if not _has_skill(s)]
    for s in need:
        res["failed"].append({"name": s, "error": "本机未安装；请先通过技能市场安装"})
    return res

# 包 clone 落点:~/.local/share/desktron/packages/<repo-slug>(与 kb/ 同级,仓外)
PKG_HOME = os.path.join(os.path.dirname(DATA_HOME), "packages")
_PAGES_DIR = os.path.join(REPO_ROOT, ".workbench", "pages")
_APPS_DIR = os.path.join(REPO_ROOT, ".workbench", "apps")
_ID_RE = re.compile(r"[a-z0-9_-]+")

# ---- 寻址 / git ----

def _parse_address(addr):
    """`git-url[@ref][#subpath]` → (url, ref, subpath)。
    `#subpath` 先剥;`@ref` 只认末段路径里的 '@'(避开 ssh `git@host:` 的用户段)。"""
    addr = (addr or "").strip()
    base, _, subpath = addr.partition("#")
    sep = max(base.rfind("/"), base.rfind(":"))
    tail = base[sep + 1:]
    if "@" in tail:
        i = base.rfind("@")
        url, ref = base[:i], base[i + 1:]
    else:
        url, ref = base, ""
    return url.strip(), ref.strip(), subpath.strip().strip("/")


def _git(args, cwd=None, timeout=300):
    try:
        r = subprocess.run(["git"] + (["-C", cwd] if cwd else []) + args,
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 1, "", "git 超时"
    except Exception as e:
        return 1, "", str(e)[:300]


def _default_remote_ref(repo_dir):
    try:
        r = subprocess.run(["git", "-C", repo_dir, "symbolic-ref", "refs/remotes/origin/HEAD"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return "origin/HEAD"


def _clone(url, ref, dest, recurse=False):
    """clone(首次)/ fetch+reset(已存在)一个仓到 dest。ref 空则 reset --hard 追远端 HEAD。
    recurse=True 拉 submodule(超级仓)。全程软失败,返回 (ok, err)。"""
    try:
        os.makedirs(PKG_HOME, exist_ok=True)
        if os.path.isdir(os.path.join(dest, ".git")):
            rc, _, err = _git(["fetch", "--all", "--quiet"], cwd=dest, timeout=180)
            if rc != 0:
                return False, (err or "fetch 失败").strip()[:300]
            if ref:
                rc, _, err = _git(["checkout", "--quiet", ref], cwd=dest, timeout=60)
            else:
                target = _default_remote_ref(dest)
                rc, _, err = _git(["reset", "--hard", target], cwd=dest, timeout=60)
            if rc != 0:
                return False, (err or "更新失败").strip()[:300]
        else:
            cmd = ["clone", "--quiet"] + (["--recurse-submodules"] if recurse else []) + [url, dest]
            rc, _, err = _git(cmd, timeout=300)
            if rc != 0:
                return False, (err or "clone 失败").strip()[:300]
            if ref:
                rc, _, err = _git(["checkout", "--quiet", ref], cwd=dest, timeout=60)
                if rc != 0:
                    return False, (err or "checkout 失败").strip()[:300]
        if recurse:
            _git(["submodule", "update", "--init", "--recursive", "--quiet"], cwd=dest, timeout=300)
        return True, ""
    except Exception as e:
        return False, str(e)[:300]


def _head_commit(repo_dir):
    rc, out, _ = _git(["rev-parse", "HEAD"], cwd=repo_dir, timeout=10)
    return out.strip() if rc == 0 else ""


# ---- 清单 ----

def _read_manifest(pkg_dir):
    """读包目录的清单 → ("package", data) | ("catalog", data) | (None, None)。
    desktron-pkg.yaml 优先(package),否则 desktron-catalog.yaml(目录仓)。"""
    try:
        import yaml
    except Exception:
        return None, None
    for fn, kind in (("desktron-pkg.yaml", "package"), ("desktron-catalog.yaml", "catalog")):
        p = os.path.join(pkg_dir, fn)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return kind, (yaml.safe_load(f) or {})
            except Exception:
                return None, None
    return None, None


def _ok_id(x):
    return bool(_ID_RE.fullmatch(x or ""))


def _review_package(data, pkg_dir):
    """package 清单 → (errs, review)。只读;structural 错进 errs,缺依赖进 review 的待补字段。"""
    errs = []
    if not (data.get("name") or "").strip():
        errs.append("缺 name")
    p = data.get("provides") or {}
    ws_ids, app_ids, kb_names = [], [], []
    write_apps, html_sources, unmet_skills, unresolved_refs = [], [], [], []
    prov_app_ids = {a.get("id") for a in (p.get("apps") or []) if a.get("id")}

    for ws in (p.get("workspaces") or []):
        wid = ws.get("id")
        if not _ok_id(wid):
            errs.append(f"workspace id 非法: {wid}"); continue
        ws_ids.append(wid)
        if ws.get("spec") and not os.path.isfile(os.path.join(pkg_dir, ws["spec"])):
            errs.append(f"workspace {wid} 的 spec 不存在: {ws['spec']}")
        if ws.get("imports") and not os.path.isfile(os.path.join(pkg_dir, ws["imports"])):
            errs.append(f"workspace {wid} 的 imports 不存在: {ws['imports']}")
        for aid in (ws.get("apps") or []):
            if aid not in prov_app_ids:
                unresolved_refs.append(aid)

    for a in (p.get("apps") or []):
        aid = a.get("id")
        if not _ok_id(aid):
            errs.append(f"app id 非法: {aid}"); continue
        app_ids.append(aid)
        if not (a.get("label") or "").strip():
            errs.append(f"app {aid} 缺 label")
        for err in validate_app_operability(a, required=True):
            errs.append(f"app {aid}: {err}")
        if a.get("agentic"):
            write_apps.append(aid)
            for k in ("who", "what", "how"):
                if not (a.get(k) or "").strip():
                    errs.append(f"agentic app {aid} 缺 {k}")
            sk = a.get("skill")
            if sk and not _has_skill(sk):
                unmet_skills.append(sk)
        else:
            pg = a.get("page")
            if not pg or not os.path.isfile(os.path.join(pkg_dir, pg)):
                errs.append(f"non-agentic app {aid} 页面不存在: {pg}")
            else:
                html_sources.append(pg)
        for asset in (a.get("assets") or []):
            if not os.path.exists(os.path.join(pkg_dir, asset)):
                errs.append(f"app {aid} 声明的 asset 不存在: {asset}")

    for kb in (p.get("kb") or []):
        if kb.get("name") and (kb.get("url") or kb.get("path")):
            kb_names.append(kb.get("name"))
        else:
            errs.append("kb 条目缺 name 或 url/path")

    req = data.get("requires") or {}
    for sk in (req.get("skills") or []):
        if not _has_skill(sk) and sk not in unmet_skills:
            unmet_skills.append(sk)

    # requires.kb:装时缺则按 url clone,已有则跳过(幂等)。这里只审查、不动盘。
    kb_will_install, kb_have = [], []
    for kb in _required_kbs(data):
        (kb_have if _kb_present(kb["name"]) else kb_will_install).append(kb["name"])

    review = {
        "name": data.get("name"), "version": data.get("version"),
        "description": data.get("description"), "author": data.get("author"),
        "provides": {"workspaces": ws_ids, "apps": app_ids, "kb": kb_names},
        "requires": req, "write_apps": write_apps, "html_sources": html_sources,
        "unmet_skills": unmet_skills, "app_refs_unresolved": sorted(set(unresolved_refs)),
        "kb_will_install": kb_will_install, "kb_have": kb_have,
    }
    return errs, review


def _has_skill(name):
    return os.path.isdir(os.path.join(REPO_ROOT, ".claude", "skills", name))


# ---- 解析(clone + 读清单,plan/install/browse 共用) ----

def _resolve(addr, recurse=False):
    """addr → 解析包(clone + 读清单)。返回 (info, err);info 含 url/ref/subpath/slug/pkg_dir/commit/kind/data。"""
    url, ref, subpath = _parse_address(addr)
    if not url:
        return None, "空地址"
    # 本地目录(任意本地路径):免 clone,直接读
    if os.path.isdir(url):
        pkg_dir = os.path.join(url, subpath) if subpath else url
        if not os.path.isdir(pkg_dir):
            return None, f"子路径不存在: {subpath}"
        kind, dat = _read_manifest(pkg_dir)
        if kind is None:
            return None, "未找到 desktron-pkg.yaml / desktron-catalog.yaml"
        return {"url": url, "ref": ref, "subpath": subpath, "slug": None, "repo_dir": url,
                "pkg_dir": pkg_dir, "commit": "", "kind": kind, "data": dat, "local": True}, ""
    slug = _kb_name_from_url(url) or "pkg"
    repo_dir = os.path.join(PKG_HOME, slug)
    ok, err = _clone(url, ref, repo_dir, recurse=recurse)
    if not ok:
        return None, f"clone 失败: {err}"
    pkg_dir = os.path.join(repo_dir, subpath) if subpath else repo_dir
    if not os.path.isdir(pkg_dir):
        return None, f"子路径不存在: {subpath}"
    kind, dat = _read_manifest(pkg_dir)
    if kind is None:
        return None, "未找到 desktron-pkg.yaml / desktron-catalog.yaml"
    return {"url": url, "ref": ref, "subpath": subpath, "slug": slug, "repo_dir": repo_dir,
            "pkg_dir": pkg_dir, "commit": _head_commit(repo_dir), "kind": kind, "data": dat}, ""


# ---- plan(只读审查)----

def plan(addr):
    """只读预演:clone + 读清单 + 校验,返回审查对象(供安全闸面板)。不写任何注册表。"""
    r, err = _resolve(addr)
    if err:
        return {"ok": False, "kind": None, "address": addr, "errors": [err]}
    if r["kind"] == "catalog":
        return {"ok": True, "kind": "catalog", "address": addr,
                "name": r["data"].get("name"), "entries": r["data"].get("entries") or []}
    errs, review = _review_package(r["data"], r["pkg_dir"])
    review.update({"ok": not errs, "kind": "package", "address": addr, "errors": errs,
                   "url": r["url"], "ref": r["ref"], "subpath": r["subpath"],
                   "repo_slug": r["slug"], "resolved_commit": r["commit"]})
    return review


# ---- 注册(install confirm 后)----

def _link(src, dst, keep_real=False):
    """幂等软链 src → dst(materialize:spec/imports/page 软链回 packages/<slug>)。
    keep_real=True:目标已是真实文件(非软链)则原样保留——运行态覆盖优先。工作区说明(spec .md)
    被落地页编辑后落成运行态真实文件,开机对齐的重注册若照旧清掉重链,每次重启都会把用户
    编辑的说明顶回包内版本。"""
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.lexists(dst):
            if os.path.islink(dst) and os.readlink(dst) == src:
                return True
            if keep_real and not os.path.islink(dst):
                return True
            os.remove(dst)
        os.symlink(src, dst)
        return True
    except Exception:
        return False


def _pkg_author(author):
    """包作者 → uid 命名空间段(清洗)。缺省回落本机用户。"""
    return re.sub(r"[^a-z0-9_-]", "", (author or "").lower())[:24] or _author_id()


def _existing_uid(app_id, author=None):
    """同作者同 id 的已注册 app uid(更新时保 uid 稳定)。无则 None。
    author 给定(装包,用包作者命名空间)→ 只匹配该命名空间;不碰用户自建 app(本机用户命名空间)。"""
    pre = _pkg_author(author) + "." + app_id + "."
    for a in _wb_apps_read():
        if (a.get("uid") or "").startswith(pre):
            return a["uid"]
    return None


def _register_workspace(ws, pkg_dir):
    wid = ws.get("id")
    if not _ok_id(wid):
        return False
    role = str(ws.get("desk_role") or "").strip()
    if role == "diagnose" or not _ok_id(role):
        role = ""
    doms = [d for d in _wb_domains_read() if d.get("id") != wid]
    doms.append({"id": wid, "name": ws.get("name") or wid, "desc": ws.get("desc") or "",
                 "desk_role": role, "icon": ws.get("icon") or "box", "apps": []})
    _wb_domains_write(doms)
    if ws.get("spec"):
        _link(os.path.join(pkg_dir, ws["spec"]), os.path.join(WB_WS_DIR, wid + ".md"), keep_real=True)
    if ws.get("imports"):
        _link(os.path.join(pkg_dir, ws["imports"]), os.path.join(WB_WS_DIR, wid + ".yaml"))
    return True


def _register_app(a, pkg_dir, targets, author=None):
    """注册一个包的 app。uid 用包作者命名空间(_pkg_author),与用户自建 app 区隔——
    同 id 也不互相覆盖。返回 uid(None=跳过)。"""
    aid = a.get("id")
    if not _ok_id(aid):
        return None
    if wb_app_removed(app_id=aid):
        return None      # 用户删过它:开机兜底与 reconcile 都不再把它装回来(显式安装会先清墓碑)
    app = dict(a)
    app["targets"] = targets
    if not app.get("agentic") and a.get("page"):
        if not _link(os.path.join(pkg_dir, a["page"]), os.path.join(_PAGES_DIR, aid + ".html")):
            return None
        app["page"] = aid
    for asset in (a.get("assets") or []):
        src = os.path.join(pkg_dir, asset)
        dst = os.path.join(_APPS_DIR, aid, os.path.basename(asset))
        _link(src, dst)
    # 规范校验:缺 skill 不阻断(只是待补),故剥掉缺失 skill 再过 canonical 闸
    probe = dict(app)
    if app.get("skill") and not _has_skill(app["skill"]):
        probe.pop("skill", None)
    # 安装包也是新写入，不能用“历史兼容”绕过可操作性声明。
    if _app_validate(probe, require_operability=True):
        return None
    app["uid"] = _existing_uid(aid, author) or _app_uid(aid, _pkg_author(author))
    apps = [x for x in _wb_apps_read() if x.get("uid") != app["uid"]]
    apps.append(app)
    _wb_apps_write(apps)
    return app["uid"]


def _register_kb(kb):
    name = kb.get("name")
    if not name or not (kb.get("url") or kb.get("path")):
        return False
    if kb.get("url"):
        ok, _ = _kb_git_clone(kb["url"], name)
        if not ok:
            return False
        reg = _kb_git_read()
        if not any(x.get("name") == name for x in reg):
            reg.append({"name": name, "url": kb["url"]})
            _kb_git_write(reg)
    return bool(kb.get("url") or os.path.exists(kb.get("path") or ""))


def _register(info, target=None):
    """把 package 的 provides 落进 .workbench/ 各注册表,返回已注册的 provides 记录。
    target 给定(从某工作区的「应用」面板装 app 时):app 的 targets 落到该工作区,而非默认 _desktop。"""
    data, pkg_dir = info["data"], info["pkg_dir"]
    p = data.get("provides") or {}
    author = data.get("author")
    prov = {"workspaces": [], "apps": [], "app_uids": [], "kb": []}
    targets_map = {}
    for ws in (p.get("workspaces") or []):
        for aid in (ws.get("apps") or []):
            targets_map.setdefault(aid, []).append(ws.get("id"))
    # 工作流(原「工作区」)已退出包模型:它是本地主动创建的文件夹(POST /workbench/domains),
    # 不再随包注册。包里残留的 workspaces 声明忽略(uninstall 仍认旧 installed.json 记录,能清理历史安装)。
    for a in (p.get("apps") or []):
        tg = [target] if target else (targets_map.get(a.get("id")) or ["_desktop"])
        uid = _register_app(a, pkg_dir, tg, author=author)
        if uid:
            prov["apps"].append(a.get("id"))
            prov["app_uids"].append(uid)
    for kb in (p.get("kb") or []):
        if _register_kb(kb):
            prov["kb"].append(kb.get("name"))
    for wid in prov["workspaces"]:
        try:
            from domains import _ensure_domain_dir
            _ensure_domain_dir(wid)   # 物化 cwd 与工作流本地资产
        except Exception:
            pass
    return prov


# ---- install / uninstall / update / 列举 ----

def install(addr, confirm=False, target=None):
    """confirm=False → 等于 plan(只读审查);confirm=True 且无错 → 注册 + 记账。
    target:从某工作区「应用」面板装 app 时传该工作区 id,app 落到它而非 _desktop。"""
    review = plan(addr)
    if not confirm or not review.get("ok") or review.get("kind") != "package":
        return review
    r, err = _resolve(addr)
    if err:
        review["ok"] = False
        review.setdefault("errors", []).append(err)
        return review
    for _a in ((r.get("data") or {}).get("provides") or {}).get("apps") or []:
        wb_app_tombstone_clear(app_id=_a.get("id") or "")   # 显式安装是更近的意图,清掉旧的删除记号
    # 先确保依赖的 KB 与 skill 就位:已有跳过(幂等),缺的按来源拉取。失败只上报、不回滚主安装。
    kb_res = _ensure_required_kbs(r["data"])
    sk_res = _ensure_required_skills(r["data"])
    prov = _register(r, target)
    rec = {"name": r["data"].get("name"), "address": addr, "url": r["url"], "ref": r["ref"],
           "subpath": r["subpath"], "repo_slug": r["slug"], "resolved_commit": r["commit"],
           "version": r["data"].get("version"), "provides": prov, "dep_apps": prov["apps"],
           "requires_kb": kb_res, "requires_skills": sk_res, "target": target, "installed_at": _now()}
    recs = [x for x in _wb_installed_read() if x.get("name") != rec["name"]]
    recs.append(rec)
    _wb_installed_write(recs)
    review["installed"] = True
    review["provides_registered"] = prov
    review["kb_result"] = kb_res
    review["skills_result"] = sk_res
    if kb_res["failed"]:
        review.setdefault("warnings", []).append(
            "部分依赖 KB 安装失败: " + ", ".join(f"{x['name']}({x['error']})" for x in kb_res["failed"]))
    if sk_res["failed"]:
        review.setdefault("warnings", []).append(
            "部分依赖 skill 安装失败: " + ", ".join(f"{x['name']}({x['error']})" for x in sk_res["failed"]))
    return review


def uninstall(name):
    """按 installed.json 记录逆注册(引用计数:被其它已装包共享的 workspace/app/clone 不真删)。"""
    recs = _wb_installed_read()
    rec = next((x for x in recs if x.get("name") == name), None)
    if not rec:
        return {"ok": False, "errors": [f"未装: {name}"]}
    others = [x for x in recs if x.get("name") != name]
    prov = rec.get("provides") or {}
    keep_ws = {w for x in others for w in (x.get("provides") or {}).get("workspaces", [])}
    for wid in prov.get("workspaces", []):
        if wid in keep_ws:
            continue
        _wb_domains_write([d for d in _wb_domains_read() if d.get("id") != wid])
        _rm(os.path.join(WB_WS_DIR, wid + ".md"))
        _rm(os.path.join(WB_WS_DIR, wid + ".yaml"))
        _rmtree(os.path.join(REPO_ROOT, "domains", wid))
    # app:按 uid 精确删(不碰用户自建/同 id 的别的包);page 文件按 id 计数(别的包仍提供该 id 则留)
    held_uids = {u for x in others for u in (x.get("provides") or {}).get("app_uids", [])}
    held_ids = {a for x in others for a in (x.get("provides") or {}).get("apps", [])}
    uids = prov.get("app_uids") or []
    for idx, aid in enumerate(prov.get("apps", [])):
        uid = uids[idx] if idx < len(uids) else None
        if uid:
            if uid not in held_uids:
                _wb_apps_write([a for a in _wb_apps_read() if a.get("uid") != uid])
        elif aid not in held_ids:   # 旧记录无 app_uids:兼容回落按 id 删(本包自身的)
            _wb_apps_write([a for a in _wb_apps_read() if a.get("id") != aid])
        if aid not in held_ids:
            _rm(os.path.join(_PAGES_DIR, aid + ".html"))
            _rmtree(os.path.join(_APPS_DIR, aid))
    slug = rec.get("repo_slug")
    if slug and not any(x.get("repo_slug") == slug for x in others):
        _rmtree(os.path.join(PKG_HOME, slug))
    _wb_installed_write(others)
    return {"ok": True, "uninstalled": name}


def update(name):
    """pull + 重注册(地址不变,confirm 隐含——已装即已信任)。
    v0 为覆盖式:新版移除的 provides 暂不清(留 diff 清理给 v0.5)。"""
    rec = next((x for x in _wb_installed_read() if x.get("name") == name), None)
    if not rec:
        return {"ok": False, "errors": [f"未装: {name}"]}
    return install(rec["address"], confirm=True)


def installed():
    return _wb_installed_read()


def browse(catalog_addr):
    """读一个目录(catalog)仓 → 条目列表,供浏览。不注册。"""
    r, err = _resolve(catalog_addr)
    if err:
        return {"ok": False, "errors": [err]}
    if r["kind"] != "catalog":
        return {"ok": False, "errors": ["不是目录(catalog)仓"]}
    return {"ok": True, "name": r["data"].get("name"), "entries": r["data"].get("entries") or []}


# ---- 小工具 ----

def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _rm(path):
    try:
        if os.path.lexists(path):
            os.remove(path)
    except Exception:
        pass


def _rmtree(path):
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.lexists(path):
            os.remove(path)
    except Exception:
        pass
