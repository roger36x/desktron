"""资产清单 / manifest 解析 + 工作台文件浏览 + markdown / 文件查看器。

依赖 deskcfg(路径)+ workbench(WB_EXT/KB_GIT_HOME/_kb_*)+ domains(_domain_dir),单向向下。
ui_server 的 assets/file/view 路由 import 回这些名字调用。
"""
import os, json, re, subprocess, time, urllib.parse

from deskcfg import REPO_ROOT, FRONTEND, is_protected_dir
from workbench import (WB_EXT, KB_GIT_HOME, _kb_git_link, _kb_git_read, _kb_repo_scan,
                       _wb_domains_read, _builtin_domain_ids)
from domains import _domain_dir


def _frontmatter(path):
    """读 markdown frontmatter 的顶层 key: value(单行值,够用即可)。"""
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read(8192)
    except Exception:
        return {}
    if not txt.startswith("---"):
        return {}
    end = txt.find("\n---", 3)
    if end < 0:
        return {}
    fm = {}
    for ln in txt[3:end].splitlines():
        if ln and not ln[0].isspace() and ":" in ln:
            k, _, v = ln.partition(":")
            fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm


def _parse_manifest(root):
    """从 manifest.yaml 提取 assets 下各组条目(缩进敏感的窄解析,不引第三方 yaml)。"""
    groups = {}
    try:
        with open(os.path.join(root, "manifest.yaml"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        return groups
    in_assets, group, item = False, None, None
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip())
        if indent == 0:
            in_assets, group, item = (s == "assets:"), None, None
            continue
        if not in_assets:
            continue
        if indent == 2 and s.endswith(":"):
            group, item = s[:-1], None
            groups.setdefault(group, [])
            continue
        if group is None:
            continue
        if s.startswith("- "):
            item = {}
            groups[group].append(item)
            s = s[2:]
        if item is not None and ":" in s:
            k, _, v = s.partition(":")
            v = v.split(" #")[0].strip().strip('"')
            if v:
                item[k.strip()] = v
    return groups


def _wb_assets():
    root = REPO_ROOT
    out = {"skills": [], "skills_installed": [], "artifacts": [], "sops": []}
    sk = os.path.join(root, ".claude", "skills")
    if os.path.isdir(sk):
        for name in sorted(os.listdir(sk)):
            f = os.path.join(sk, name, "SKILL.md")
            if os.path.isfile(f):
                fm = _frontmatter(f)
                out["skills"].append({"id": name, "name": fm.get("name", name),
                                      "desc": fm.get("description", "")[:240],
                                      "path": f".claude/skills/{name}/SKILL.md"})
    # 已安装:全局 ~/.claude/skills(引擎装的共享技能,如 lark-*);路径用 gskill: 前缀,经专用端点读
    gsk = os.path.expanduser("~/.claude/skills")
    if os.path.isdir(gsk):
        for name in sorted(os.listdir(gsk)):
            f = os.path.join(gsk, name, "SKILL.md")
            if os.path.isfile(f):
                fm = _frontmatter(f)
                out["skills_installed"].append({"id": name, "name": fm.get("name", name),
                                                "desc": fm.get("description", "")[:240],
                                                "path": f"gskill:{name}"})
    kn = os.path.join(root, "knowledge")
    for dirpath, dirs, files in os.walk(kn, followlinks=True):  # workspace 里 knowledge 是软链
        dirs[:] = [d for d in dirs if d not in ("deepwiki",) and not d.startswith(".")]  # deepwiki=缓存,非产物
        for f in files:
            if f.endswith(".md"):
                full = os.path.join(dirpath, f)
                out["artifacts"].append({"name": f,
                                         "path": os.path.relpath(full, kn) and "knowledge/" + os.path.relpath(full, kn),
                                         "mtime": int(os.path.getmtime(full))})
    out["artifacts"].sort(key=lambda a: -a["mtime"])
    out["artifacts"] = out["artifacts"][:100]
    m = _parse_manifest(root)
    out["observers"] = m.get("observers", [])
    out["executors"] = m.get("executors", [])
    out["agents"] = m.get("agents", [])
    # 知识入口三段:用户显式安装的 git 库 / 随 Desk 发行的导引 / 升级机器上的本地遗留。
    _nat = lambda s: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]
    kb = {"git": [], "desk": [], "local": {"subkbs": [], "common": [], "domain": [], "deepwiki": []}}
    # local:兼容升级机器的旧 knowledge 目录；新记忆与事实写入工作区 .memory。
    if os.path.isdir(kn):
        _kn_skip = ("deepwiki", "alert-rules", "coverage", "__pycache__")
        _kn_skip_files = ("README.md", "index.md", "KB-INDEX.md", "INDEX.md", "access-gaps.md")

        def _kn_subkb_desc(base):
            """子 KB 一句话说明:取 README.md(退回 index.md)标题后的首个非空段,截断到一行。"""
            for fn in ("README.md", "index.md"):
                p = os.path.join(base, fn)
                if not os.path.isfile(p):
                    continue
                try:
                    with open(p, encoding="utf-8") as f:
                        lines = f.read(4096).splitlines()
                except Exception:
                    continue
                for ln in lines:
                    s = ln.strip()
                    if s and not s.startswith("#"):
                        s = re.sub(r"\*\*|`", "", s)
                        return s[:80] + ("…" if len(s) > 80 else "")
            return ""

        def _kn_notes(sub):
            """一条笔记连 frontmatter 一起给前端:知识页要按置信度/类型分诊,只有文件名不够。"""
            base = os.path.join(kn, sub)
            out = []
            if not os.path.isdir(base):
                return out
            for dp, dirs, fs in os.walk(base, followlinks=True):
                dirs[:] = [d for d in dirs if d not in _kn_skip and not d.startswith(".")]
                for f in sorted(fs):
                    if f.endswith(".md") and f not in _kn_skip_files:
                        full = os.path.join(dp, f)
                        rel = os.path.relpath(full, kn)
                        fm = _frontmatter(full)
                        seg = rel.split(os.sep)
                        out.append({
                            "name": f, "path": "knowledge/" + rel,
                            "id": fm.get("id") or f[:-3],
                            "summary": fm.get("summary", ""),
                            "doc_type": fm.get("doc_type", ""),
                            "confidence": fm.get("confidence", ""),
                            "role": seg[1] if len(seg) > 2 else "",     # global | domain
                            "cat": seg[2] if len(seg) > 3 else "",      # 类目目录
                            "updated": time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(full))),
                        })
            return out
        # 遗留子目录按实际内容扫描，不写死历史库名。
        for sub in sorted(os.listdir(kn)):
            if sub in _kn_skip or sub.startswith(".") or not os.path.isdir(os.path.join(kn, sub)):
                continue
            files = _kn_notes(sub)
            if files:
                dist = {"corroborated": 0, "documented": 0, "reported": 0}
                for f in files:
                    dist[f["confidence"]] = dist.get(f["confidence"], 0) + 1
                kb["local"]["subkbs"].append({
                    "name": sub, "count": len(files), "dist": dist,
                    "updated": max((f["updated"] for f in files), default=""),
                    "desc": _kn_subkb_desc(os.path.join(kn, sub)),
                    "files": files,
                })
        # deepwiki 缓存 2026-08-04 删除,页面不再展示;manifest 的 cache.deepwiki 管道声明仍在,
        # 目录若被重新拉起,这段扫描照旧产出(前端已撤展示位)。
        dw = os.path.join(kn, "deepwiki")
        if os.path.isdir(dw):
            for repo in sorted(os.listdir(dw)):
                rdir = os.path.join(dw, repo)
                if not os.path.isdir(rdir):
                    continue
                files = []
                for dirpath, dirs, fs in os.walk(rdir, followlinks=True):
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    for f in fs:
                        if f.endswith(".md"):
                            full = os.path.join(dirpath, f)
                            files.append({"name": f, "path": "knowledge/" + os.path.relpath(full, kn)})
                files.sort(key=lambda x: _nat(x["name"]))
                kb["local"]["deepwiki"].append({"repo": repo, "count": len(files), "files": files})
    # desk:发行版自带的 Desk 导引文档(desk-docs/*.md;经 workspace 软链读取)
    desk_dir = os.path.join(root, "desk-docs")
    if os.path.isdir(desk_dir):
        for f in sorted(os.listdir(desk_dir)):
            if f.endswith(".md"):
                kb["desk"].append({"name": f, "path": f"desk-docs/{f}"})
    # git:注册表里 clone 的远端知识库(desktron-family-kb 已退役,不再内置)
    for reg in _kb_git_read():
        nm = reg.get("name")
        if not nm:
            continue
        _kb_git_link(nm)
        gdir = os.path.join(KB_GIT_HOME, nm)
        if not os.path.isdir(gdir):
            kb["git"].append({"name": nm, "url": reg.get("url", ""), "head": "",
                              "root_files": [], "groups": [], "missing": True})
            continue
        ent = _kb_repo_scan(gdir, f"kb/{nm}/")
        ent.update({"name": nm, "url": reg.get("url", "")})
        kb["git"].append(ent)
    out["kb"] = kb
    return out




_FS_SKIP = {".git", "node_modules", ".venv", "__pycache__", ".DS_Store", ".next", "dist", "build"}


def _fs_avoid(path, root):
    """自动遍历该不该绕开这个目录。

    绕的是 deskcfg.PROTECTED_DIRS 那批要系统授权的位置。但根本身就在里面时不绕:那说明
    用户已经把文件应用开在那儿了,权限也已经问过,再绕就成了「打开的目录里什么都搜不到」。
    """
    return is_protected_dir(path) and not is_protected_dir(root)


def _fs_base(root_id):
    """文件浏览根目录:工作区 id → 它的目录。项目=外部真实仓;普通工作区=$WORKSPACE/domains/<id>;
    desktop/空=REPO_ROOT。`abs:<绝对路径>`=用户经右栏"+"挑的任意目录(本机单用户,直接放行已存在目录)。
    目录不存在则不给浏览。"""
    root_id = root_id or ""
    if root_id.startswith("abs:"):
        d = os.path.abspath(os.path.expanduser(root_id[4:]))
        return d if os.path.isdir(d) else None
    if not re.fullmatch(r"[\w:-]+", root_id):   # 允许「容器:子区」(隔离态编码子区看代码=列其 worktree)
        return None
    d = _domain_dir(root_id)   # 冒号形式由 _domain_dir 解析(容器子区 → worktree / 项目内目录)
    return os.path.abspath(d) if d and os.path.isdir(d) else None


def _fs_resolve(root_id, rel):
    """把工作区内相对路径解析成绝对路径,限定在根内(防穿越)。
    用词法规范化(abspath,不解析软链):既挡 .. 逃逸,又允许工作区里的软链资产
    (domains/<id> 下的 common / knowledge / imports 等都是指向仓外的软链)。"""
    base = _fs_base(root_id)
    if not base:
        return None, None
    rel = (rel or "").strip("/")
    if ".." in rel.split("/"):
        return None, None
    full = os.path.abspath(os.path.join(base, rel))
    if not (full == base or full.startswith(base + os.sep)):
        return None, None
    return base, full


def _fs_has_visible_children(path):
    """目录树是否有可展示内容；被文件树过滤的构建噪声不算内容。

    受保护目录不探:探一下就是打开它,系统当场弹框。一次展开家目录会挨个进「文稿」「下载」
    「音乐」「照片」,连弹好几个(2026-09-09 用户实见)。直接当作有内容,展开箭头照常给——
    用户真点进去时再由系统问他一次,那时问的是他自己刚做的事。
    """
    if is_protected_dir(path):
        return True
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name in _FS_SKIP:
                    continue
                if entry.is_dir() or entry.is_file():
                    return True
            return False
    except Exception:
        return False


def _fs_ls(root_id, rel):
    """懒列一层目录:目录在前、文件在后,跳过 .git/node_modules 等噪声。"""
    base, full = _fs_resolve(root_id, rel)
    if not full or not os.path.isdir(full):
        return None
    items = []
    try:
        for n in sorted(os.listdir(full)):
            if n in _FS_SKIP:
                continue
            p = os.path.join(full, n)
            r = (rel.strip("/") + "/" + n) if rel and rel.strip("/") else n
            if os.path.isdir(p):
                items.append({"name": n, "type": "dir", "path": r,
                              "has_children": _fs_has_visible_children(p)})
            elif os.path.isfile(p):
                try: sz = os.path.getsize(p)
                except Exception: sz = 0
                items.append({"name": n, "type": "file", "path": r, "size": sz})
    except Exception:
        return None
    items.sort(key=lambda x: (x["type"] != "dir", x["name"]))
    return items


_FS_GREP_MAX_BYTES = 1_500_000     # 单文件超过这么大就跳过:多半是产物或数据,不是要读的代码
_FS_GREP_EXT_SKIP = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
                     ".tar", ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".mov", ".mp3", ".wav",
                     ".so", ".dylib", ".a", ".o", ".pyc", ".class", ".icns", ".jsonl"}


def _fs_grep(root_id, kw, limit=200, per_file=5):
    """按内容递归搜。与 _fs_search 的分工:那个搜文件名,这个搜正文。

    不外挂 ripgrep:它未必在,而这里的规模(单个工作区)自己走一遍足够。
    二进制与大文件按扩展名与大小先筛掉——不是为了快,是为了不把产物里的乱码当命中扔回界面。
    每个文件最多回 per_file 行,总数封顶 limit;截断了明说,不假装搜全了。
    """
    base, full = _fs_resolve(root_id, "")
    if not full or not os.path.isdir(full):
        return None
    kw = (kw or "").strip()
    if not kw:
        return {"items": [], "truncated": False}
    low = kw.lower()
    out, truncated = [], False
    for dirpath, dirs, files in os.walk(full):
        dirs[:] = [d for d in dirs if d not in _FS_SKIP and not d.startswith(".")
                   and not _fs_avoid(os.path.join(dirpath, d), full)]
        for n in sorted(files):
            if os.path.splitext(n)[1].lower() in _FS_GREP_EXT_SKIP:
                continue
            ab = os.path.join(dirpath, n)
            try:
                if os.path.getsize(ab) > _FS_GREP_MAX_BYTES:
                    continue
                with open(ab, encoding="utf-8", errors="strict") as f:
                    hits = 0
                    for i, line in enumerate(f, 1):
                        if low not in line.lower():
                            continue
                        out.append({"path": os.path.relpath(ab, full), "name": n,
                                    "line": i, "text": line.rstrip("\n")[:300]})
                        hits += 1
                        if hits >= per_file or len(out) >= limit:
                            break
            except (OSError, UnicodeDecodeError):
                continue   # 读不了或不是文本:跳过,不报错——搜索不该因为一个二进制文件中断
            if len(out) >= limit:
                truncated = True
                break
        if truncated:
            break
    return {"items": out, "truncated": truncated}


def _fs_search(root_id, kw, limit=200):
    """按文件名递归搜(不看内容):目录树是懒加载的,只过滤已展开的行等于搜不到,必须走一遍磁盘。
    命中判据是路径任一段包含关键词(不分大小写),跳过 _FS_SKIP 那批噪声目录。
    limit 是硬上限:大仓走满会卡住 UI,截断后由前端提示"还有更多"。"""
    base, full = _fs_resolve(root_id, "")
    if not full or not os.path.isdir(full):
        return None
    kw = (kw or "").strip().lower()
    if not kw:
        return []
    out, truncated = [], False
    for dirpath, dirs, files in os.walk(full):
        dirs[:] = [d for d in dirs if d not in _FS_SKIP and not d.startswith(".")
                   and not _fs_avoid(os.path.join(dirpath, d), full)]
        for n in sorted(dirs) + sorted(files):
            if kw not in n.lower():
                continue
            ab = os.path.join(dirpath, n)
            rel = os.path.relpath(ab, full)
            isdir = os.path.isdir(ab)
            out.append({"name": n, "type": "dir" if isdir else "file", "path": rel})
            if len(out) >= limit:
                truncated = True
                break
        if truncated:
            break
    out.sort(key=lambda x: (x["type"] != "dir", x["path"]))
    return {"items": out, "truncated": truncated}


def _fs_file(root_id, rel):
    """读项目内一个文件(文本,限 512KB;二进制/超限返回提示)。"""
    base, full = _fs_resolve(root_id, rel)
    if not full or not os.path.isfile(full):
        return None
    try:
        if os.path.getsize(full) > 512 * 1024:
            return "(文件超过 512KB,未在此预览)"
        with open(full, "rb") as f:
            data = f.read()
        if b"\x00" in data[:4096]:
            return "(二进制文件,未预览)"
        return data.decode("utf-8", "replace")
    except Exception:
        return None


def _fs_git_root(root_id, rel=""):
    """这份文件所属的 git 仓根,以及它相对仓根的路径。→ {"root": <绝对目录>, "rel": <相对路径>}。

    从会话里点开一份文件时,原来把它所在的那一层当成 IDE 的根:树上只有同目录那几个文件,
    差异也无从谈起。要打开的其实是 .git 所在的那一层(2026-08-19 用户提)。

    相对路径在这一侧算,不交给前端:根可能写成 `abs:~/work/…`,而 git 报的是展开后的绝对
    路径,两边字符串对不上;这里手上就是展开过的那一份。不在仓里时 root 给空串,调用方原样用。
    """
    base, full = _fs_resolve(root_id, rel)
    if not base:
        return {"root": "", "rel": rel}
    try:
        r = subprocess.run(["git", "-C", base, "rev-parse", "--show-toplevel"],
                           capture_output=True, timeout=20)
    except Exception:
        return {"root": "", "rel": rel}
    if r.returncode:
        return {"root": "", "rel": rel}
    top = r.stdout.decode("utf-8", "replace").strip()
    if not top:
        return {"root": "", "rel": rel}
    # git 的向上查找不限层数,一路能走到文件系统根。家目录被人拿来版本化 dotfiles 是常见做法,
    # 那时 ~ 底下任何一份文件都会把 IDE 的根定在家目录上——左树要铺开 Library、缓存与全部下载,
    # 比原来「只看文件所在那一层」更糟。这两个位置不当项目根用,退回调用方原来的选择。
    if top in (os.path.expanduser("~").rstrip(os.sep), os.sep):
        return {"root": "", "rel": rel}
    # 两侧都取真实路径再比:git 报的 toplevel 是解析过软链的,而 _fs_resolve 有意不解析
    # (那是给访问控制用的,要放行工作区里指向仓外的软链资产)。直接比字符串的话,凡是路径里
    # 有一段软链就对不上——macOS 的 /tmp 就是 /private/tmp,升不到仓根却毫无提示。
    target = os.path.realpath(full or base)
    top = os.path.realpath(top)
    if target != top and not target.startswith(top + os.sep):
        return {"root": "", "rel": rel}
    return {"root": top, "rel": os.path.relpath(target, top) if target != top else ""}


def _fs_git_file(root_id, rel, rev="HEAD"):
    """这份文件在某个 git 版本里的样子(默认 HEAD)。→ (正文, 说明)。

    差异视图原来比的是「缓冲区 vs 磁盘」:改了没存时它回答「我刚改了什么」,没改时两边一样,
    一句话也说不出。真正要看的是「相对上一次提交我改了什么」,那得问 git(2026-08-19 用户提)。

    说明非空表示没有可比的版本(不在仓里 / 这个版本里还没有这份文件),正文一律给空串——
    差异视图那侧看起来就是整份新增,与「未跟踪的新文件」本来的样子一致。
    """
    base, full = _fs_resolve(root_id, rel)
    if not full or not os.path.isfile(full):
        return None, "找不到这个文件"
    # rev 来自客户端,限成一个保守的字符集:git 的版本表达式够用,又不至于被当成选项
    rev = str(rev or "HEAD").strip() or "HEAD"
    if not re.fullmatch(r"[\w./^~@{}-]{1,64}", rev):
        return "", "版本写法不认得"
    d = os.path.dirname(full)

    def git(*args):
        try:
            return subprocess.run(["git", "-C", d, *args], capture_output=True, timeout=20)
        except Exception:
            return None

    r = git("rev-parse", "--show-toplevel")
    if not r or r.returncode:
        return "", "这个目录不在 git 仓里"
    top = r.stdout.decode("utf-8", "replace").strip()
    if not top:
        return "", "这个目录不在 git 仓里"
    r = git("show", "%s:%s" % (rev, os.path.relpath(full, top)))
    if not r or r.returncode:
        return "", "%s 里还没有这份文件" % rev
    data = r.stdout
    if len(data) > 512 * 1024:
        return "", "这个版本的文件超过 512KB,未取"
    if b"\x00" in data[:4096]:
        return "", "二进制文件"
    return data.decode("utf-8", "replace"), ""


def _fs_write_file(root_id, rel, text):
    """写项目内一个文本文件(路径校验同 _fs_resolve,不能穿越根目录)。文件浏览器就地编辑保存用,
    取代原先"退出去开 nvim"那条路径——nvim 之外没有别的写入口,故这里不加体积/二进制限制(读预览的
    512KB 上限是防误显示大文件,写的是编辑器里已经拿到手的文本,体积不是这里要防的风险)。"""
    base, full = _fs_resolve(root_id, rel)
    if not full:
        return False
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(text)
        return True
    except Exception:
        return False


def _wb_html(pid):
    """页面双轨解析:本地定制(.workbench/pages/)覆盖发行版(frontend/pages/)。"""
    if not re.fullmatch(r"[a-z0-9_-]+", pid or ""):
        return None, None
    local = os.path.join(REPO_ROOT, ".workbench", "pages", pid + ".html")
    official = os.path.join(FRONTEND, "pages", pid + ".html")
    for path, source in ((local, "local"), (official, "official")):
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return f.read(1024 * 1024), source
            except Exception:
                pass
    fallback = ('<!doctype html><html><body style="background:transparent;color:#5c5c5c;'
                'font:14px ui-sans-serif,system-ui;padding:24px">还没有此页。'
                '在会话里让 agent 写:.workbench/pages/' + pid + ".html</body></html>")
    return fallback, "none"


def _wb_file(rel):
    """按 workspace 相对路径读文件;禁越界、限扩展名。软链(资产→repo)允许。"""
    norm = os.path.normpath(urllib.parse.unquote(rel))
    if os.path.isabs(norm) or norm.startswith(".."):
        return None
    if not norm.endswith(WB_EXT):
        return None
    full = os.path.join(REPO_ROOT, norm)
    if not os.path.isfile(full):
        return None
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            return f.read(512 * 1024)
    except Exception:
        return None


# ---- 悬浮框内嵌查看:工作区内文件/目录渲染成可内嵌页(文件=文本/图片,目录=可点列表)----
_VIEW_IMG = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
             ".webp": "image/webp", ".svg": "image/svg+xml", ".bmp": "image/bmp", ".ico": "image/x-icon"}
# 文件夹图标(与前端 ICONS.folder 同一路径);文件无图标,留 15px 空位与目录名对齐
_FOLDER_SVG = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
               'stroke-linecap="round" stroke-linejoin="round">'
               '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>')
# 目录/文件查看页:对齐前端 fsView 的 .fb-* 观感(文件夹图标 + 缩进对齐 + 文件名等宽 + hover)
_VIEW_PAGE = ("<!doctype html><meta charset=utf-8>"
              "<meta name=viewport content='width=device-width,initial-scale=1,viewport-fit=cover'>"
              "<meta name=color-scheme content='dark light'>"
              # 同源 iframe 先读宿主的显式主题,早于 style 落态,避免浅色下首帧先闪出白字。
              # 独立打开时才回退到 URL / 系统主题,与 Monaco 文件应用同一口径。
              "<script>(()=>{{let m='';try{{m=parent.document.documentElement.dataset.theme||''}}catch{{}}"
              "if(!m)m=new URLSearchParams(location.search).get('theme')||'';"
              "if(!m&&matchMedia('(prefers-color-scheme: light)').matches)m='light';"
              "document.documentElement.dataset.theme=m==='light'?'light':'dark'}})()</script>"
              # 背景透明:同源 iframe 透出宿主 #overlay 的毛玻璃(与 fsView 透明 .overlay-content 一致)
              "<style>:root{{--view-text:#eaeaea;--view-muted:#aeaeae;--view-faint:#5c5c5c;"
              "--view-hover:rgba(255,255,255,.07);--view-hover-text:#fafafa}}"
              ":root[data-theme='light']{{--view-text:#333333;--view-muted:#6b6b6b;--view-faint:#777777;"
              "--view-hover:rgba(0,0,0,.055);--view-hover-text:#171717}}"
              "html,body{{margin:0;background:transparent;color:var(--view-text);"
              "font:14px/1.5 ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif;-webkit-font-smoothing:antialiased}}"
              ".wrap{{padding:10px 12px 40px}}"
              ".fb-crumb{{display:flex;flex-wrap:wrap;align-items:center;gap:2px;font-size:13px;"
              "font-family:ui-monospace,Menlo,Monaco,monospace;color:var(--view-muted);margin:0 0 8px;word-break:break-all}}"
              ".fb-crumb a{{color:var(--view-muted);text-decoration:none;padding:1px 3px;border-radius:3px}}"
              ".fb-crumb a:hover{{color:var(--view-hover-text);background:var(--view-hover)}}"
              ".fb-sep{{color:var(--view-faint)}}"
              ".fb-list{{display:flex;flex-direction:column}}"
              ".fb-row{{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:5px;"
              "cursor:pointer;color:var(--view-text);text-decoration:none}}"
              ".fb-row:hover{{background:var(--view-hover)}}"
              ".fb-glyph{{display:inline-flex;flex:0 0 auto;width:15px;color:var(--view-faint)}}"
              ".fb-glyph svg{{width:15px;height:15px;display:block}}"
              ".fb-name{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}"
              ".fb-file .fb-name{{font-family:ui-monospace,Menlo,Monaco,monospace;font-size:12.5px}}"
              "pre{{white-space:pre-wrap;word-break:break-word;margin:0;"
              "font:12.5px/1.55 ui-monospace,Menlo,Monaco,monospace}}img{{max-width:100%}}"
              ".muted{{color:var(--view-faint)}}</style>"
              # 打开期间切换外观也即时跟随,不重载目录页或丢掉当前位置。
              "<script>(()=>{{try{{const h=parent!==window&&parent.document.documentElement;if(!h)return;"
              "new MutationObserver(()=>document.documentElement.dataset.theme="
              "h.dataset.theme==='light'?'light':'dark').observe(h,"
              "{{attributes:true,attributeFilter:['data-theme']}})}}catch{{}}}})()</script>"
              "<div class=wrap>{body}</div>")

# Markdown 文件查看:同源页引 vendor 的 markdown-it + highlight 客户端渲染(与主 app 一致)
_MD_VIEW_CSS = (
    "html,body{margin:0;background:transparent;color:#eaeaea;font:14px/1.6 ui-sans-serif,system-ui,"
    "-apple-system,'Segoe UI',sans-serif;-webkit-font-smoothing:antialiased}"
    # 页边距与会话页对齐:#term-host 是 max-width 1040 + 左 72(右 31 加 .xterm 的 41px 滚动条轨道也是 72),
    # 这里照抄同一组数,两边正文列同为 896px、左右缘同起点,来回切不跳。
    # border-box + 1040(= 896 内容 + 左右各 72 padding):编辑态的 .wrap 也是 border-box,
    # 两态同一套盒模型,文字列才能同宽同起点;写成 content-box 时编辑态会窄一截并右偏
    ".wrap{padding:18px 72px 48px;max-width:1040px;margin:0 auto;box-sizing:border-box}"
    # 类名跟 crumb() 实际产出的 .fb-crumb 对齐(此前写成 .crumb,md 页面包屑一直吃的是浏览器默认链接色)
    ".fb-crumb{color:#aeaeae;font-size:12px;margin:0 0 16px;word-break:break-all;"
    "font-family:ui-monospace,Menlo,Monaco,monospace}"
    ".fb-crumb a{color:#b0b9f9;text-decoration:none}.fb-crumb a:hover{text-decoration:underline}"
    ".fb-sep{color:#5c5c5c}"
    ".md>:first-child{margin-top:0}"
    ".md h1,.md h2,.md h3,.md h4{color:#fafafa;font-weight:500;margin:20px 0 8px;line-height:1.35}"
    ".md h1{font-size:22px}.md h2{font-size:18px;border-bottom:1px solid rgba(255,255,255,.08);padding-bottom:5px}"
    ".md h3{font-size:16px}.md h4{font-size:14px}"
    ".md p{margin:0 0 12px}.md a{color:#b0b9f9;text-decoration:none}.md a:hover{text-decoration:underline}"
    ".md ul,.md ol{margin:0 0 12px;padding-left:22px}.md li{margin:3px 0}"
    ".md code{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,monospace;font-size:12.5px;"
    "background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.08);border-radius:4px;padding:1px 5px}"
    ".md pre.hljs{background:#0d0b0b;border:1px solid rgba(255,255,255,.08);border-radius:6px;"
    "padding:12px 14px;overflow-x:auto;margin:0 0 12px;font-size:12.5px;line-height:1.55}"
    ".md pre.hljs code{background:none;border:none;padding:0;font-size:inherit}"
    ".md blockquote{margin:0 0 12px;padding:2px 14px;border-left:3px solid rgba(255,255,255,.14);color:#aeaeae}"
    ".md table{border-collapse:collapse;margin:2px 0 12px;font-size:13px;display:block;overflow-x:auto}"
    ".md th,.md td{border:1px solid rgba(255,255,255,.14);padding:5px 10px;text-align:left}.md th{font-weight:500}"
    ".md img{max-width:100%}.md hr{border:none;border-top:1px solid rgba(255,255,255,.08);margin:18px 0}"
)
_MD_VIEW_JS = (
    "var md=window.markdownit({html:false,linkify:true,breaks:false,highlight:function(s,l){"
    "try{if(l&&hljs.getLanguage(l))return '<pre class=\"hljs\"><code>'+hljs.highlight(s,{language:l}).value+'</code></pre>';"
    "return '<pre class=\"hljs\"><code>'+md.utils.escapeHtml(s)+'</code></pre>';}catch(e){return ''}}});"
    "md.linkify.set({fuzzyLink:false});"
    # 渲染挂 window.__vwRender:保存后就地重渲,不用整页刷新(编辑栏的保存动作调它)
    "window.__vwRender=function(s){"
    "document.getElementById('md').innerHTML=md.render(s);"
    # md 里的相对链接原样是相对当前 iframe URL(/workbench/view?path=...)解析,没有路径段可退回→404。
    # 改写成相对当前文件所在目录拼出的 workspace 路径,统一走 /workbench/view 打开。
    "var base=__BASEDIR__;"
    "function joinRel(base,rel){"
    "var parts=rel[0]==='/'?[]:(base?base.split('/').filter(Boolean):[]);"
    "rel.split('/').forEach(function(p){"
    "if(p===''||p==='.')return;"
    "if(p==='..')parts.pop();else parts.push(p);});"
    "return parts.join('/');}"
    "document.querySelectorAll('#md a[href]').forEach(function(a){"
    "var h=a.getAttribute('href');"
    "if(!h||/^([a-z][a-z0-9+.-]*:|#)/i.test(h))return;"
    "var hash='';var hi=h.indexOf('#');if(hi>=0){hash=h.slice(hi);h=h.slice(0,hi);}"
    "if(!h)return;"
    "a.setAttribute('href','/workbench/view?path='+encodeURIComponent(joinRel(base,h))+hash);"
    "});};"
    "window.__vwRender(SRC);"
)

# 就地编辑(md 与纯文本视图共用):默认只读,点「编辑」切 textarea,保存回写同一路径。
# 目录/图片视图不挂这套 UI(后端 _wb_view_write 也拒写这两类)。
_EDIT_CSS = (
    # 按钮紧跟路径末尾(不是靠最右),随文字流走:路径转 inline,按钮 inline-flex 接在后面
    ".vw-head{margin:0 0 12px}"
    ".vw-head .fb-crumb,.vw-head .crumb{display:inline;margin:0}"
    # 按钮盒高 = 路径那行的行高(1lh),两者中线由构造重合;原先 28 高的盒比 22.4 的行高出一截,
    # 上下各多挂 2.8px,读起来就是"按钮压着文字低了"
    ".vw-bar{display:inline-flex;gap:8px;align-items:stretch;height:1lh;vertical-align:middle;margin-left:10px}"
    ".vw-btn{background:#2e2e30;color:#eaeaea;border:1px solid rgba(255,255,255,.1);border-radius:7px;"
    "padding:0 11px;font-size:12.5px;line-height:1;cursor:pointer;font-family:inherit;white-space:nowrap;"
    "display:inline-flex;align-items:center}"
    ".vw-btn:hover{background:#3a3a3d}"
    ".vw-btn.pri{background:#1f6f5c;border-color:transparent}.vw-btn.pri:hover{background:#268a72}"
    ".vw-msg{font-size:12px;color:#5c5c5c;white-space:nowrap;display:inline-flex;align-items:center}"
    "#vw-save,#vw-cancel{display:none}"
    "body.editing #vw-save,body.editing #vw-cancel{display:inline-flex}body.editing #vw-edit{display:none}"
    "#vw-ed{display:none;width:100%;box-sizing:border-box;background:#141313;color:#eaeaea;"
    "border:1px dashed rgba(255,255,255,.12);border-radius:8px;padding:12px 14px;"
    "font:14px/1.7 ui-monospace,Menlo,Monaco,monospace;resize:none;outline:none}"
    "#vw-ed:focus{border-color:rgba(255,255,255,.24)}"
    "body.editing #vw-ed{display:block}body.editing .vw-view{display:none}"
    # 编辑态:wrap 撑满视口高、textarea 吃掉剩余空间一直到底,整页不滚(textarea 自己滚)
    "body.editing{overflow:hidden}"
    "body.editing .wrap{display:flex;flex-direction:column;height:100vh;box-sizing:border-box;padding-bottom:14px}"
    # 框往外撑 15px(= textarea 自身 padding 14 + 边框 1),让编辑态文字列和预览态的起始位重合;
    # width 交回 auto,由 flex 拉伸算宽,否则 100% 会把负 margin 的增量又吃回去
    "body.editing #vw-ed{flex:1;min-height:0;width:auto;margin:0 -15px}"
    # iOS 对小于 16px 的 textarea 聚焦会放大整个页面。保留 viewport 的手势缩放能力，
    # 只在窄屏触控设备修正文档编辑区与按钮的输入/点击尺寸。
    "@media (pointer:coarse) and (max-width:820px),(pointer:coarse) and (max-height:520px){"
    "body.editing .wrap{height:100dvh;padding:12px 18px calc(14px + env(safe-area-inset-bottom))}"
    "#vw-ed{font-size:16px;line-height:1.65}"
    ".vw-btn{min-height:40px;padding:0 12px;font-size:14px;touch-action:manipulation}"
    "}"
)
_EDIT_BTNS = ('<div class="vw-bar">'
              '<button class="vw-btn" id="vw-edit">编辑</button>'
              '<button class="vw-btn pri" id="vw-save">保存</button>'
              '<button class="vw-btn" id="vw-cancel">取消</button>'
              '<span class="vw-msg" id="vw-msg"></span></div>')
_EDIT_TA = '<textarea id="vw-ed" spellcheck=false></textarea>'
_EDIT_JS = (
    "(function(){"
    "var path=__PATH__,base=__BASE__,src=__SRC__;"
    "var ed=document.getElementById('vw-ed'),msg=document.getElementById('vw-msg');"
    "function leave(){document.body.classList.remove('editing');msg.textContent='';}"
    "document.getElementById('vw-edit').onclick=function(){"
    "ed.value=src;document.body.classList.add('editing');ed.focus();};"
    "document.getElementById('vw-cancel').onclick=leave;"
    "function save(){var t=ed.value;msg.textContent='保存中…';"
    "fetch('/workbench/view',{method:'POST',headers:{'content-type':'application/json'},"
    "body:JSON.stringify({path:path,base:base,text:t})})"
    ".then(function(r){return r.json();}).then(function(j){"
    "if(j&&j.ok){src=t;if(window.__vwRender)window.__vwRender(t);leave();}"
    "else{msg.textContent=(j&&j.error)||'保存失败';}})"
    ".catch(function(){msg.textContent='保存失败';});}"
    "document.getElementById('vw-save').onclick=save;"
    "ed.addEventListener('keydown',function(e){"
    "if((e.metaKey||e.ctrlKey)&&e.key==='s'){e.preventDefault();save();}"   # Cmd/Ctrl+S 保存
    "else if(e.key==='Escape'){e.preventDefault();leave();}});"
    "})();"
)


def _view_roots():
    """允许查看的根。本机形态返回 None = 不设限;远端形态才给白名单。

    判据从「路径在不在某个根里」换成「这一份 Desk 是本机还是服务器上常驻的那份」。
    本机形态下限制不增加任何安全性——那是用户自己的机器、自己的文件,终端里 cat 一下
    就能看,查看器拦着只是制造摩擦(2026-08-19 实见:新 clone 的仓虽已被工作流显式引用,
    仍打不开)。远端形态不同:那时接口经隧道对外,持令牌者可读的范围就是这里放开的范围,
    ~/.ssh 与各类凭证都在其中,白名单必须留着。

    顺带去掉了「活会话的 cwd 也算根」那一条。它是为绕开本限制打的补丁,效果是「你在哪儿
    开过会话哪儿就能看」——挡不住什么,又让同一个文件开着会话时打得开、关了就 404。"""
    try:
        import remote_auth
        if remote_auth.role() != "remote":
            return None
    except Exception:
        pass   # 判不出角色就按远端处理,宁可严一档
    roots = [os.path.abspath(REPO_ROOT),
             os.path.abspath(os.path.expanduser("~/.claude"))]
    ids = set()
    try: ids |= set(_builtin_domain_ids())
    except Exception: pass
    try:
        for d in _wb_domains_read():
            if d.get("id"): ids.add(d["id"])
    except Exception: pass
    for i in ids:
        try:
            dd = _domain_dir(i)
            if dd: roots.append(os.path.abspath(dd))
        except Exception: pass
    # 工作流引用的代码仓:远端形态下同样该能看——它们是这条工作流显式声明的工作范围。
    try:
        for d in _wb_domains_read():
            for r in (d.get("repos") or []):
                pth = (r or {}).get("path") if isinstance(r, dict) else None
                if pth:
                    roots.append(os.path.abspath(os.path.expanduser(str(pth))))
    except Exception:
        pass
    return roots


def _wb_view_resolve(rel, base=None, root_id=None):
    """传入相对或绝对路径 → (abs, reldisp);越界(不在允许根内)/不存在 → None。空=工作区根。
    用词法规范化(abspath,不解析软链):既挡 .. 逃逸,又允许 workspace 软链资产与注册项目目录里的文件。
    工作区内 → 相对显示(面包屑可回根);工作区外的项目路径 → 绝对显示。
    base=发起链接的终端所属工作区 id:终端里打印的相对路径是相对它自己的 cwd,不是桌面根,
    否则跑在别的工作区/外部项目里的会话一贴相对路径就按桌面根拼、大概率 404。"""
    raw = urllib.parse.unquote(rel or "")
    if raw.startswith("~"):
        raw = os.path.expanduser(raw)
    # root_id:与 /workbench/fs 同一个口径(见 _fs_base)。文件应用那一侧本来就认
    # `abs:<目录>`——用户自己挑的目录,本机单用户直接放行;这一侧不认的话,同一份文件
    # 会「编辑器打得开、浏览器打不开」。放行的仍是调用方显式指名的那个目录,不是整盘。
    if root_id:
        base_dir = _fs_base(root_id)
        if not base_dir:
            return None
        ab = os.path.abspath(os.path.join(base_dir, raw)) if raw else base_dir
        if ab != base_dir and not ab.startswith(base_dir + os.sep):
            return None                      # .. 逃逸
        return (ab, ab) if os.path.exists(ab) else None
    root = os.path.abspath(REPO_ROOT)
    join_root = root
    if base and not os.path.isabs(raw):
        try:
            join_root = os.path.abspath(_domain_dir(base))
        except Exception:
            join_root = root
    ab = join_root if not raw else os.path.abspath(raw if os.path.isabs(raw) else os.path.join(join_root, raw))
    allowed = _view_roots()   # None = 本机形态,不设限(见 _view_roots)
    if allowed is not None:
        hit = next((r for r in allowed if ab == r or ab.startswith(r + os.sep)), None)
        if not hit:
            return None
    if not os.path.exists(ab):
        return None
    if ab == root or ab.startswith(root + os.sep):
        rd = os.path.relpath(ab, root)
        return (ab, "" if rd == "." else rd)
    return (ab, ab)   # 工作区外项目文件:绝对路径作显示与导航(crumb/子项链接均用绝对路径,再解析仍走本函数)


def _wb_view_write(rel, text, base=None):
    """查看器里就地编辑保存(md / 纯文本)。路径解析与 _wb_view 同源(_view_roots 限定),
    不额外放开范围;目录、图片、不存在的路径一律拒写(只改已经打开的那个文件,不新建)。"""
    res = _wb_view_resolve(rel, base)
    if not res:
        return False
    ab, _rd = res
    if not os.path.isfile(ab) or os.path.splitext(ab)[1].lower() in _VIEW_IMG:
        return False
    try:
        with open(ab, "w", encoding="utf-8") as f:
            f.write(text)
        return True
    except Exception:
        return False


def _wb_view(rel, raw=False, base=None, root_id=None, download=False):
    """工作区内文件/目录 → (content_type, bytes)。越界/不存在 → None。"""
    import html as _html
    res = _wb_view_resolve(rel, base, root_id)
    if not res:
        return None
    ab, rd = res
    ext = os.path.splitext(ab)[1].lower()
    if download:
        # 另存为必须保留原始字节；预览可能转码、渲染或截断，不能拿来下载。
        if not os.path.isfile(ab):
            return None
        try:
            with open(ab, "rb") as f:
                return ("application/octet-stream", f.read())
        except OSError:
            return None
    if raw:   # 原始字节:图片直出给 <img>
        if os.path.isfile(ab) and ext in _VIEW_IMG:
            try:
                with open(ab, "rb") as f:
                    return (_VIEW_IMG[ext], f.read(8 * 1024 * 1024))
            except Exception:
                return None
        return None

    def crumb(rd):
        is_abs = rd.startswith(os.sep)   # 绝对路径(工作区外项目文件):累积段要带回前导 /
        parts = [p for p in rd.split(os.sep) if p]
        segs = ['<a href="/workbench/view?path=">工作区</a>']
        for i, part in enumerate(parts):
            acc = os.sep.join(parts[:i + 1])
            if is_abs: acc = os.sep + acc
            segs.append(f'<a href="/workbench/view?path={urllib.parse.quote(acc)}">{_html.escape(part)}</a>')
        return '<div class="fb-crumb">' + '<span class="fb-sep">/</span>'.join(segs) + '</div>'

    def row(href, name, isd):   # 对齐 fsView:目录有文件夹图标,文件无图标(占位缩进),文件名等宽
        glyph = _FOLDER_SVG if isd else ""
        cls = "fb-dir" if isd else "fb-file"
        return (f'<a class="fb-row {cls}" href="/workbench/view?path={urllib.parse.quote(href)}">'
                f'<span class="fb-glyph">{glyph}</span>'
                f'<span class="fb-name">{_html.escape(name)}{"/" if isd else ""}</span></a>')

    if os.path.isdir(ab):
        try:
            names = sorted(os.listdir(ab), key=lambda n: (not os.path.isdir(os.path.join(ab, n)), n.lower()))
        except Exception:
            return None
        rows = []
        if rd:
            rows.append(row(os.path.dirname(rd), "..", True))
        for n in names:
            if n in _FS_SKIP:   # 只跳 .git/node_modules 等噪声(与 fsView 一致),其余含 dotfile 都列出
                continue
            child = (rd + os.sep + n) if rd else n
            rows.append(row(child, n, os.path.isdir(os.path.join(ab, n))))
        body = crumb(rd) + '<div class="fb-list">' + "".join(rows) + "</div>"
        return ("text/html; charset=utf-8", _VIEW_PAGE.format(body=body).encode("utf-8"))
    if ext in _VIEW_IMG:
        body = crumb(rd) + f'<img src="/workbench/view?path={urllib.parse.quote(rd)}&raw=1">'
        return ("text/html; charset=utf-8", _VIEW_PAGE.format(body=body).encode("utf-8"))
    if ext in (".html", ".htm"):   # HTML 直接当页面渲染:本地工作区文件,与 /workbench/page 直出一致,不再 escape 成源码
        try:
            with open(ab, "rb") as f:
                return ("text/html; charset=utf-8", f.read(4 * 1024 * 1024))
        except Exception:
            return None
    try:
        with open(ab, encoding="utf-8", errors="replace") as f:
            txt = f.read(1024 * 1024)
    except Exception:
        return None
    src = json.dumps(txt).replace("<", "\\u003c")   # 防 </script> 截断:转义所有 <
    edit_js = (_EDIT_JS.replace("__PATH__", json.dumps(rd))
               .replace("__BASE__", json.dumps(base or ""))
               .replace("__SRC__", src))
    head = '<div class="vw-head">' + crumb(rd) + _EDIT_BTNS + "</div>" + _EDIT_TA
    if ext in (".md", ".markdown", ".mdx"):
        base_dir = os.path.dirname(rd).replace(os.sep, "/")
        md_js = _MD_VIEW_JS.replace("__BASEDIR__", json.dumps(base_dir)).replace("SRC", src)
        page = (
            "<!doctype html><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1,viewport-fit=cover'>"
            "<meta name=color-scheme content=dark>"
            '<link rel=stylesheet href="/vendor/github-dark.min.css">'
            "<style>" + _MD_VIEW_CSS + _EDIT_CSS + "</style>"
            "<div class=wrap>" + head + '<div class="md vw-view" id="md"></div></div>'
            '<script src="/vendor/markdown-it.min.js"></script>'
            '<script src="/vendor/highlight.min.js"></script>'
            "<script>" + md_js + edit_js + "</script>"
        )
        return ("text/html; charset=utf-8", page.encode("utf-8"))
    # 纯文本:同一套编辑栏,渲染退化成写回 <pre> 的 textContent
    body = ("<style>" + _EDIT_CSS + "</style>" + head
            + '<pre class="vw-view" id="vw-pre">' + _html.escape(txt) + "</pre>"
            + "<script>window.__vwRender=function(s){document.getElementById('vw-pre').textContent=s;};"
            + edit_js + "</script>")
    return ("text/html; charset=utf-8", _VIEW_PAGE.format(body=body).encode("utf-8"))
