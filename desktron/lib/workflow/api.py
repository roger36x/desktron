"""工作流领域路由:工作区/容器注册表、说明、Context 资料区、子区 worktree、隐藏、指标面板桥。
拆分自 ui_server._workbench;owns() 按路径认领,
handle(h) 内 h 为 Handler 实例,行为与原 if 链一致。"""
import os, re, json, time, urllib.parse
import deskcfg
from deskcfg import REPO_ROOT, ENGINES
from workbench import (WB_WS_DIR, _wb_domains_read, _wb_domains_write, _builtin_domain_ids,
                       _wb_hidden_read, _wb_hidden_write, _workspace_md_path)
from domains import (_domain_entry, _domain_dir, _domain_dir_override, _ensure_domain_dir,
                     _repo_items, _repo_name)


def _desk_role(value):
    """工作流的可选 Desk 角色；缺省、旧 diagnose 与非法值都表示无角色。"""
    value = str(value or "").strip()
    return value if value != "diagnose" and re.fullmatch(r"[a-z0-9_-]+", value) else ""


def _repo_scan_sessions(wd):
    """一个引用目录下的引擎原生会话数:逐引擎 store.list_by_cwd 汇总;单引擎失败(未装/serve 未起)跳过。"""
    n = 0
    import engines as _eng
    for key in ENGINES:
        try:
            sp = _eng.get(key)
            st = getattr(sp, "store", None)
            if st is not None:
                n += len(st.list_by_cwd(wd) or [])
        except Exception:
            pass
    return n


def _list_repo_sessions(wd, engine=""):
    """wd 下的引擎原生会话候选:[{id, engine, title, time, domain}]。
    engine 给了就只列该引擎——工作流的会话列表本身按当前引擎过滤,导入别的引擎
    等于导入了在界面上看不见的东西。domain 是现有归属('' = 未归属)。按时间倒序。"""
    import engines as _eng
    from term import _term_meta_read
    meta = _term_meta_read()
    out = []
    for key in ([engine] if engine else list(ENGINES)):
        if key not in ENGINES:
            continue
        try:
            sp = _eng.get(key)
            st = getattr(sp, "store", None)
            rows = st.list_by_cwd(wd) if st is not None else []
        except Exception:
            continue
        for s in rows or []:
            sid = s.get("id")
            if not sid:
                continue
            out.append({"id": sid, "engine": key, "title": s.get("title") or sid[:8],
                        "time": s.get("time") or 0,
                        "domain": (meta.get(sid) or {}).get("domain") or ""})
    out.sort(key=lambda x: -(x["time"] or 0))
    return out


def _import_repo_sessions(wd, did, ids=None, engine=""):
    """把 wd 下的会话「关联」到工作流 did:在 term meta 里加一条 refs,不动 domain。

    引用不是归属(与任务↔会话同一形状):会话的家仍在它自己的目录,transcript、resume
    的 cwd、原有分组都不变,工作流只是多一条指向它的链接。改写 domain 那种「搬家」做法
    会让 claude/cursor 从域目录 resume 找不到 transcript(见 term._transcript_cwd)。
    返回 (新增关联数, 已关联跳过数)。"""
    from term import _term_meta_mutate
    want = set(ids) if ids is not None else None
    sessions = _list_repo_sessions(wd, engine)
    result = {"added": 0, "skipped": 0}
    def add_refs(meta):
        for s in sessions:
            if want is not None and s["id"] not in want:
                continue
            m = dict(meta.get(s["id"]) or {})
            refs = [r for r in (m.get("refs") or []) if isinstance(r, str) and r]
            if did in refs or s["domain"] == did:
                result["skipped"] += 1
                continue
            refs.append(did)
            m["refs"] = refs
            m.setdefault("engine", s["engine"])
            meta[s["id"]] = m
            result["added"] += 1
        return result["added"]
    _term_meta_mutate(add_refs)
    return result["added"], result["skipped"]


def owns(p, method):
    if p.startswith("/workbench/domains/") and p.endswith("/tasks") and method in ("GET", "POST"):
        return False   # 任务域路由(task/api.py),调度序在本模块之前
    return (p == "/workbench/domains" or p.startswith("/workbench/domains/") or
            p in ("/workbench/hidden", "/workbench/workspace", "/workbench/artifacts"))


def handle(h, p, q, method):
    if p == "/workbench/domains":
        # GET=运行态工作区注册表;POST=新建一个工作区(写注册表 + 建独立目录 + 落说明 markdown)
        if method == "GET":
            return h._send_json(_wb_domains_read())
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
            assert isinstance(body, dict)
        except Exception:
            return h._send_json({"error": "invalid json"}, 400)
        did = (body.get("id") or "").strip()
        name = (body.get("name") or "").strip()
        if not re.fullmatch(r"[a-z0-9_-]+", did):
            return h._send_json({"ok": False, "error": "id 缺失或非法(仅 a-z0-9_-)"}, 422)
        if not name:
            return h._send_json({"ok": False, "error": "name 缺失"}, 422)
        if did in _builtin_domain_ids() or did == "_desktop":
            return h._send_json({"ok": False, "error": f"id 与分发版工作区冲突:{did}"}, 422)
        doms = _wb_domains_read()
        entry = {"id": did, "name": name, "desc": (body.get("desc") or "").strip(),
                 "desk_role": _desk_role(body.get("desk_role")), "icon": body.get("icon") or "layers",
                 "apps": []}
        _dir = (body.get("dir") or "").strip()
        if _dir:
            if _dir != "@desk-source" and not os.path.isdir(os.path.abspath(os.path.expanduser(_dir))):
                return h._send_json({"ok": False, "error": f"dir 不是有效目录:{_dir}"}, 422)
            entry["dir"] = _dir   # 外部真实目录(如 @desk-source);cwd 指它、不做脚手架
        if (body.get("kind") or "").strip():
            entry["kind"] = body["kind"].strip()   # project=coding 项目;container=跨职能项目容器(子区收在其内)
        if isinstance(body.get("subareas"), list):   # 容器的子区清单:{id,name,ref?,note?}(机器数据,任务/进度已随备忘录退役)
            clean = []
            for s in body["subareas"]:
                if isinstance(s, dict) and re.fullmatch(r"[\w-]+", s.get("id") or "") and (s.get("name") or "").strip():
                    item = {"id": s["id"], "name": s["name"].strip(),
                            "ref": (s.get("ref") or "").strip(), "note": (s.get("note") or "").strip()}
                    br = (s.get("branch") or "").strip()   # 有 branch=隔离态编码子区(cwd 落 worktree);无=共享/职能子区
                    if br:
                        item["branch"] = br
                    clean.append(item)
            entry["subareas"] = clean
        existing = next((d for d in doms if d.get("id") == did), None)
        if existing:
            for k in ("name", "desc", "desk_role", "icon"):
                existing[k] = entry[k]
            existing.pop("agent", None)   # 旧工作流字段不再代表 Desk 角色
            for k in ("dir", "kind", "subareas"):
                if k in entry:
                    existing[k] = entry[k]
        else:
            doms.append(entry)
        # 说明 markdown:有则落运行态(不进仓库分发物),供落地页与 AGENTS.md 合成读取
        fivew = body.get("fivew")
        if isinstance(fivew, str) and fivew.strip():
            try:
                os.makedirs(WB_WS_DIR, exist_ok=True)
                mdp = os.path.join(WB_WS_DIR, did + ".md")
                if os.path.islink(mdp):
                    os.remove(mdp)   # 同 /workbench/workspace POST:不写穿内容包软链
                with open(mdp, "w", encoding="utf-8") as f:
                    f.write(fivew.strip() + "\n")
            except Exception:
                pass
        _wb_domains_write(doms)
        _ensure_domain_dir(did)   # 建独立 cwd 目录 + 合成 AGENTS.md(此时说明已落盘)
        return h._send_json({"ok": True, "id": did})
    if p.startswith("/workbench/domains/") and p.endswith("/rename") and method == "POST":
        # 重命名工作区/容器:运行态条目直接改 name;分发版工作区(无运行态条目)落一条最小覆盖条目
        # (仅 id+name),前端 mergeDomains 按 id 覆盖显示名、其余字段仍用分发版定义,不碰官方文件。
        did = urllib.parse.unquote(p.split("/")[3])
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
            assert isinstance(body, dict)
        except Exception:
            return h._send_json({"error": "invalid json"}, 400)
        new_name = (body.get("name") or "").strip()
        if not new_name:
            return h._send_json({"ok": False, "error": "name 缺失"}, 422)
        doms = _wb_domains_read()
        entry = next((d for d in doms if d.get("id") == did), None)
        old_name = (entry or {}).get("name") or ""
        if entry:
            entry["name"] = new_name
        elif did in _builtin_domain_ids():
            doms.append({"id": did, "name": new_name})
        else:
            return h._send_json({"ok": False, "error": f"未知工作区:{did}"}, 404)
        _wb_domains_write(doms)
        try:   # 散备忘按「文件夹名 = 工作流名」归属,改名要同步迁文件夹,否则备忘蒸馏断链
            import notepad_store
            if old_name:
                notepad_store.rename_folder(REPO_ROOT, old_name, new_name)
        except Exception as e:
            deskcfg.log("notepad", f"改名迁移备忘文件夹失败:{str(e)[:120]}")
        return h._send_json({"ok": True})
    if p.startswith("/workbench/domains/") and p.endswith("/context"):
        # 工作区/专项的项目参考资料(Context,仿 Claude project):上传的文档落 domains/<id>/context/,
        # 飞书文档/表格链接存 context/.links.json;清单经 AGENTS.md 注入(按需读取,不整份进上下文)。
        # 必须排在下方 DELETE /workbench/domains/<id> 通配之前,否则 DELETE context 会被吞成注销工作区。
        did = urllib.parse.unquote(p.split("/")[3])
        if not re.fullmatch(r"[a-z0-9_-]+", did or ""):
            return h._send_json({"error": "bad id"}, 400)
        entry = _domain_entry(did) or {}
        if entry.get("dir"):   # 指向外部真实目录的域:不往用户的仓里塞资料
            return h._send_json({"error": "指向外部目录的工作区不支持 context 上传(不污染该目录)"}, 400)
        ctx_dir = os.path.join(_domain_dir(did), "context")
        links_path = os.path.join(ctx_dir, ".links.json")
        def _links_read():
            try:
                with open(links_path, encoding="utf-8") as f:
                    d = json.load(f)
                    return d if isinstance(d, list) else []
            except Exception:
                return []
        def _links_write(ls):
            os.makedirs(ctx_dir, exist_ok=True)
            with open(links_path, "w", encoding="utf-8") as f:
                json.dump(ls, f, ensure_ascii=False, indent=2)
        if method == "GET":
            files = []
            try:
                for n in sorted(os.listdir(ctx_dir)):
                    fp = os.path.join(ctx_dir, n)
                    if n.startswith(".") or not os.path.isfile(fp):
                        continue
                    files.append({"name": n, "size": os.path.getsize(fp)})
            except Exception:
                pass
            notes = ""
            try:
                with open(os.path.join(ctx_dir, ".notes.md"), encoding="utf-8") as f:
                    notes = f.read()
            except Exception:
                pass
            return h._send_json({"files": files, "links": _links_read(), "notes": notes})
        if method == "POST":
            length = int(h.headers.get("Content-Length", 0))
            if length > 32 * 1024 * 1024:
                return h._send_json({"error": "文件过大(限 20MB)"}, 413)
            try:
                body = json.loads(h.rfile.read(length) or b"{}")
                op = body.get("op")
            except Exception:
                return h._send_json({"error": "invalid json"}, 400)
            if op == "file":
                name = os.path.basename((body.get("name") or "").strip())
                name = re.sub(r"[^\w.一-鿿-]+", "-", name).strip("-.")
                if not name:
                    return h._send_json({"error": "文件名无效"}, 400)
                try:
                    import base64
                    data = base64.b64decode(body.get("data_b64") or "")
                except Exception:
                    return h._send_json({"error": "data_b64 无效"}, 400)
                # sub=assets:正文贴图落子目录,不进附件清单/注入清单(正文里的图片标记引用它)
                tgt = os.path.join(ctx_dir, "assets") if body.get("sub") == "assets" else ctx_dir
                os.makedirs(tgt, exist_ok=True)
                final, n = name, 1
                while os.path.exists(os.path.join(tgt, final)):   # 同名不覆盖,自动加序号
                    stem, ext = os.path.splitext(name)
                    n += 1
                    final = f"{stem}-{n}{ext}"
                with open(os.path.join(tgt, final), "wb") as f:
                    f.write(data)
                rel = ("assets/" + final) if tgt.endswith("assets") else final
                return h._send_json({"ok": True, "name": final, "rel": rel})
            if op == "notes":
                # Context 正文(备忘同款实时编辑区):context/.notes.md,链接以 [标题](url) 标记存文本
                text = body.get("text")
                if not isinstance(text, str) or len(text) > 256 * 1024:
                    return h._send_json({"error": "text 需为字符串(≤256KB)"}, 400)
                os.makedirs(ctx_dir, exist_ok=True)
                tmp = os.path.join(ctx_dir, ".notes.md.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(tmp, os.path.join(ctx_dir, ".notes.md"))
                return h._send_json({"ok": True})
            if op == "link":
                url = (body.get("url") or "").strip()
                if not re.match(r"https?://", url):
                    return h._send_json({"error": "链接需以 http(s):// 开头"}, 400)
                ls = _links_read()
                if any(x.get("url") == url for x in ls):
                    return h._send_json({"ok": True, "dup": True})
                ls.append({"url": url, "title": (body.get("title") or "").strip()})
                _links_write(ls)
                return h._send_json({"ok": True})
            return h._send_json({"error": "op 需为 file|link|notes"}, 400)
        if method == "DELETE":
            q2 = urllib.parse.parse_qs(q)
            name = (q2.get("name") or [""])[0]
            url = (q2.get("url") or [""])[0]
            if name:
                safe = os.path.basename(name)
                fp = os.path.join(ctx_dir, safe)
                if safe.startswith(".") or not os.path.isfile(fp):
                    return h._send_json({"error": "not found"}, 404)
                os.remove(fp)
                return h._send_json({"ok": True})
            if url:
                ls = [x for x in _links_read() if x.get("url") != url]
                _links_write(ls)
                return h._send_json({"ok": True})
            return h._send_json({"error": "需要 name 或 url"}, 400)
    if p == "/workbench/domains/orphans" and method == "GET":
        # 孤儿工作流目录:domains/ 下存在但未注册的目录。删除工作流只摘注册表条目、目录保留,
        # 这里把它们列出来供「新建工作流」弹层导入找回;name 取说明 md 首个一级标题,没有则用目录名
        reg = {d.get("id") for d in _wb_domains_read()} | set(_builtin_domain_ids())
        root = os.path.join(REPO_ROOT, "domains")
        out = []
        try:
            names = sorted(os.listdir(root))
        except OSError:
            names = []
        for did in names:
            if did.startswith(".") or did.startswith("_") or did in reg:
                continue
            dp = os.path.join(root, did)
            if not os.path.isdir(dp):
                continue
            nm = did
            try:
                with open(_workspace_md_path(did), encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("# "):
                            nm = line[2:].strip() or did
                            break
            except OSError:
                pass
            out.append({"id": did, "name": nm, "mtime": int(os.path.getmtime(dp))})
        return h._send_json(out)
    if p.startswith("/workbench/domains/") and p.endswith("/repos"):
        # 工作流引用清单(代码仓):数据存注册表 entry.repos
        # [{path}],变更后重合成 AGENTS.md(注入「本工作流涵盖的代码仓」段)。
        # GET ?scan=1 附各仓会话数(逐引擎 list_by_cwd)与 AGENTS.md 在位;
        # POST {op:add|remove|import-sessions}。worktree 隔离已退役:cwd 恒为工作流目录后,
        # 开不开分支是 agent 在具体任务里的判断,Desk 不预先替它决定。
        # 会话 cwd 恒为工作流目录,引用只是告知,不迁移任何进程。
        did = urllib.parse.unquote(p.split("/")[3])
        doms = _wb_domains_read()
        entry = next((x for x in doms if x.get("id") == did), None)
        if entry is None and did in _builtin_domain_ids():
            return h._send_json({"error": "内置工作区不支持引用代码仓"}, 422)
        if entry is None or entry.get("kind"):
            return h._send_json({"error": f"未知工作流:{did}"}, 404)
        if method == "GET":
            items = _repo_items(did)
            if (urllib.parse.parse_qs(q).get("scan") or [""])[0] == "1":
                for it in items:
                    it["exists"] = os.path.isdir(it["dir"])
                    it["agents"] = any(os.path.isfile(os.path.join(it["dir"], f))
                                       for f in ("AGENTS.md", "CLAUDE.md"))
                    it["sessions"] = _repo_scan_sessions(it["dir"]) if it["exists"] else 0
            return h._send_json({"repos": items})
        if method != "POST":
            return h._send_json({"error": "method"}, 405)
        ln = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(ln) or b"{}")
            assert isinstance(body, dict)
        except Exception:
            return h._send_json({"error": "invalid json"}, 400)
        op = body.get("op")
        src = os.path.abspath(os.path.expanduser(str(body.get("path") or "").strip()))
        repos = entry.setdefault("repos", [])
        cur = next((r for r in repos if isinstance(r, dict)
                    and os.path.abspath(os.path.expanduser(str(r.get("path") or ""))) == src), None)
        if op == "add":
            if not os.path.isdir(src):
                return h._send_json({"ok": False, "error": f"不是有效目录:{src}"}, 422)
            if cur is not None:
                return h._send_json({"ok": False, "error": "该目录已在引用清单里"}, 422)
            repos.append({"path": src})
        elif op == "remove":
            if cur is None:
                return h._send_json({"ok": False, "error": "引用不存在"}, 404)
            repos.remove(cur)
        elif op == "list-sessions":
            # 列该目录下的会话候选供用户勾选(带现有归属,前端把已归属的置灰不选)
            if cur is None:
                return h._send_json({"ok": False, "error": "引用不存在"}, 404)
            rows = _list_repo_sessions(src, str(body.get("engine") or ""))
            from term import _term_meta_read as _mr
            _meta = _mr()
            for r in rows:   # linked = 已被本工作流引用(或本来就归属这里),前端据此置灰
                _refs = (_meta.get(r["id"]) or {}).get("refs") or []
                r["linked"] = (did in _refs) or (r["domain"] == did)
            return h._send_json({"ok": True, "sessions": rows})
        elif op == "import-sessions":
            # 把选中的会话关联到本工作流:只追加 meta refs,不改 domain/cwd。
            # ids 缺省则关联该目录下的全部候选会话。
            if cur is None:
                return h._send_json({"ok": False, "error": "引用不存在"}, 404)
            ids = body.get("ids")
            ids = [str(x) for x in ids] if isinstance(ids, list) else None
            n, skip = _import_repo_sessions(src, did, ids, str(body.get("engine") or ""))
            return h._send_json({"ok": True, "imported": n, "skipped": skip,
                                 "repos": _repo_items(did)})
        else:
            return h._send_json({"error": "未知 op(add/remove/list-sessions/import-sessions)"}, 400)
        _wb_domains_write(doms)
        _ensure_domain_dir(did)   # 重合成 AGENTS.md:注入段跟引用清单即时对齐
        # 引用清单变了不必再重启会话。TUI 那条路把 repos[] 拼进启动 argv(--add-dir),改了
        # 只能杀进程重来;GUI 这条路每回合按 repos[] 现算 runtimeWorkspaceRoots
        # (CodexAdapter._session_roots),下一轮即生效。
        return h._send_json({"ok": True, "repos": _repo_items(did),
                             "refreshing_codex_sessions": 0})
    if p.startswith("/workbench/domains/") and method == "DELETE":
        # /workbench/domains/<id> 注销运行态工作区(只摘注册表,目录/会话另由 term 清理)
        did = urllib.parse.unquote(p.split("/")[3])
        doms = [d for d in _wb_domains_read() if d.get("id") != did]
        _wb_domains_write(doms)
        return h._send_json({"ok": True})
    if p == "/workbench/hidden":
        # GET=本机隐藏的工作区 id 列表;POST {id, hidden}=隐藏/恢复(本机安装状态,不动官方定义)
        if method == "GET":
            return h._send_json(_wb_hidden_read())
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
            assert isinstance(body, dict)
        except Exception:
            return h._send_json({"error": "invalid json"}, 400)
        did = (body.get("id") or "").strip()
        if not re.fullmatch(r"[a-z0-9_-]+", did):
            return h._send_json({"ok": False, "error": "id 非法"}, 422)
        hid = set(_wb_hidden_read())
        if body.get("hidden"):
            hid.add(did)
        else:
            hid.discard(did)
        _wb_hidden_write(hid)
        return h._send_json({"ok": True, "hidden": sorted(hid)})
    if p == "/workbench/workspace" and method == "GET":
        # 工作区说明 markdown 文本(运行态优先,回落分发版),落地页「说明」块用;也是 AGENTS.md 合成时注入的职责 grounding
        q = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)
        did = (q.get("id") or [""])[0]
        if not re.fullmatch(r"[a-z0-9_-]+", did or ""):
            return h._send_json({"error": "bad id"}, 400)
        try:
            with open(_workspace_md_path(did), encoding="utf-8") as f:
                return h._send_text(f.read(), "text/markdown; charset=utf-8")
        except Exception:
            return h._send_json({"error": "not found"}, 404)
    if p == "/workbench/workspace" and method == "POST":
        # 就地编辑工作区说明:写运行态 md(不动分发版),随后重合成该工作区 AGENTS.md(说明是注入段之一)
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
            did = (body.get("id") or "").strip()
            text = body["text"]
            assert isinstance(text, str) and re.fullmatch(r"[a-z0-9_-]+", did)
        except Exception:
            return h._send_json({"error": "需要 {id, text}"}, 400)
        try:
            os.makedirs(WB_WS_DIR, exist_ok=True)
            mdp = os.path.join(WB_WS_DIR, did + ".md")
            if os.path.islink(mdp):
                os.remove(mdp)   # 安装器给包装工作区留的软链指向内容包物化目录:写穿会被包刷新回退,解链落成运行态覆盖
            with open(mdp, "w", encoding="utf-8") as f:
                f.write(text.rstrip() + "\n")
            _ensure_domain_dir(did)
        except Exception as e:
            return h._send_json({"error": f"写入失败:{str(e)[:120]}"}, 500)
        return h._send_json({"ok": True})
    if p == "/workbench/artifacts":
        # 一个工作流做出过什么:文档、demo、预览页。清单以目录为准,索引只补外部链接与说明。
        import artifacts
        if method == "GET":
            did = (urllib.parse.parse_qs(q).get("domain") or [""])[0]
            if not artifacts.artifacts_dir(did):
                return h._send_json({"error": "domain 非法"}, 422)
            directory = artifacts.artifacts_dir(did)
            try:
                display_dir = os.path.relpath(directory, REPO_ROOT)
            except ValueError:
                # Windows source and workflow data may live on different drives.
                display_dir = os.path.abspath(directory)
            return h._send_json({"items": artifacts.list_items(did), "dir": display_dir})
        ln = int(h.headers.get("Content-Length", 0))
        try:
            jb = json.loads(h.rfile.read(ln) or b"{}")
            assert isinstance(jb, dict)
        except Exception:
            return h._send_json({"error": "invalid json"}, 400)
        did = (jb.get("domain") or "").strip()
        if not artifacts.artifacts_dir(did):
            return h._send_json({"ok": False, "error": "domain 非法"}, 422)
        op = (jb.get("op") or "link").strip()
        if op == "link":
            row = artifacts.add_link(did, jb.get("url"), jb.get("title") or "",
                                     jb.get("kind") or "link", jb.get("note") or "",
                                     jb.get("session") or "")
            if not row:
                return h._send_json({"ok": False, "error": "url 缺失或不是 http(s) 链接"}, 422)
        elif op == "meta":
            row = artifacts.annotate(did, jb.get("ref"), jb.get("title") or "",
                                     jb.get("note") or "", jb.get("kind") or "")
            if not row:
                return h._send_json({"ok": False, "error": "ref 缺失,或没有要改的字段"}, 422)
        elif op in ("hide", "show"):
            row = artifacts.hide(did, jb.get("ref"), op == "hide")
            if not row:
                return h._send_json({"ok": False, "error": "ref 缺失"}, 422)
        else:
            return h._send_json({"ok": False, "error": f"未知操作:{op}"}, 422)
        return h._send_json({"ok": True, "entry": row, "items": artifacts.list_items(did)})
    return h._send_json({"error": "unknown"}, 404)
