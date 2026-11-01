"""应用领域路由:本地应用注册表(targets 展开/注册/移除)、注册表版本号、应用资产目录定位。
拆分自 ui_server._workbench;owns() 按路径认领,
handle(h) 内 h 为 Handler 实例,行为与原 if 链一致。"""
import os, re, json, time, urllib.parse
from deskcfg import FRONTEND
from workbench import (WB_APPS, WB_DOMAINS, _wb_apps_read, _wb_apps_write, _wb_apps_expand,
                       _app_validate, _app_uid, _author_id)


def owns(p, method):
    return (p in ("/workbench/apps", "/workbench/apps-rev", "/workbench/app-asset",
                  "/workbench/canvases") or
            p.startswith("/workbench/apps/") or p.startswith("/workbench/canvases/"))


def handle(h, p, q, method):
    if p == "/workbench/apps-rev" and method == "GET":
        # 注册表版本号(apps.json + domains.json 的 mtime 最大值,毫秒)。
        # 前端轻量轮询此值,变了就重拉——使带外注册(agent 在对话里部署 app、
        # reconcile 补默认工作区依赖)即时显现,不必切工作区才刷新。
        rev = 0
        for fp in (WB_APPS, WB_DOMAINS):
            try:
                rev = max(rev, int(os.path.getmtime(fp) * 1000))
            except Exception:
                pass
        return h._send_json({"rev": rev})
    if p == "/workbench/apps":
        # GET=本地应用注册表(按 targets 展开成 {域:[apps]},前端渲染不变);
        # POST=注册一个本地应用(扁平存储 + targets;注册前结构校验,不合法拒绝)
        if method == "GET":
            return h._send_json(_wb_apps_expand())
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
            domain = body.get("domain")
            app = body.get("app")
            assert isinstance(app, dict)
        except Exception:
            return h._send_json({"error": "需要 {app[, domain|targets]}"}, 400)
        # 新建与按 uid 更新都必须显式选择 Agent 可操作性；旧注册表仅在读取时兼容。
        errs = _app_validate(app, require_operability=True)
        if errs:
            return h._send_json({"ok": False, "errors": errs}, 422)
        # 目标:app.targets / body.targets 优先;否则回退旧入参 domain → [domain];都没有 → [_desktop]
        tg = app.get("targets") or body.get("targets")
        if not (isinstance(tg, list) and tg):
            tg = [domain] if isinstance(domain, str) and domain else ["_desktop"]
        app["targets"] = [t for t in tg if isinstance(t, str) and t]
        apps = _wb_apps_read()
        now = int(time.time() * 1000)
        existing = next((x for x in apps if app.get("uid") and x.get("uid") == app["uid"]), None)
        if existing:
            # 带 uid = 优化已有应用:保留 uid/author/created,覆盖其余(会话持续跟踪靠 uid,不靠名)
            app["uid"] = existing["uid"]
            app["author"] = existing.get("author", _author_id())
            app["created"] = existing.get("created", now)
            app["updated"] = now
            apps = [x for x in apps if x.get("uid") != app["uid"]]
        else:
            # 新应用:生成全局唯一 uid(同名也不撞)
            app["uid"] = _app_uid(app["id"])
            app["author"] = _author_id()
            app["created"] = now
            app["updated"] = now
        app.setdefault("icon", "monitor")
        app["source"] = "local"
        apps.append(app)
        _wb_apps_write(apps)
        return h._send_json({"ok": True, "uid": app["uid"], "id": app["id"], "targets": app["targets"]})
    if p.startswith("/workbench/apps/") and method == "DELETE":
        # /workbench/apps/<domain>/<uid> 从某工作区移除应用:删该 target;targets 空了(或 domain=*)才真删定义
        seg = p.split("/")
        if len(seg) == 5:
            dom = urllib.parse.unquote(seg[3])
            key = urllib.parse.unquote(seg[4])
            apps, out = _wb_apps_read(), []
            for a in apps:
                if a.get("uid") == key or a.get("id") == key:
                    tg = [t for t in (a.get("targets") or []) if t != dom]
                    if dom == "*" or not tg:
                        # 真删:记墓碑。否则开机兜底(bootstrap_default_workspace_apps)与
                        # reconcile 会按发行版声明与安装记录把它装回来,用户的删除等于没删。
                        try:
                            from workbench import wb_app_tombstone
                            wb_app_tombstone(app_id=a.get("id") or "", uid=a.get("uid") or "")
                        except Exception:
                            pass
                        continue
                    a = dict(a); a["targets"] = tg
                out.append(a)
            _wb_apps_write(out)
            return h._send_json({"ok": True})
        return h._send_json({"error": "bad path"}, 400)
    if p == "/workbench/canvases" and method == "GET":
        # 画布应用(Excalidraw)清单:不含元素本体,前端侧栏列表用
        import canvas_store
        from deskcfg import REPO_ROOT
        return h._send_json({"items": canvas_store.listing(REPO_ROOT)})
    if p.startswith("/workbench/canvases/"):
        # /workbench/canvases/<id>:GET 取整张,POST 存(带 svg 则同步导出图),DELETE 删
        import canvas_store
        from deskcfg import REPO_ROOT
        cid = urllib.parse.unquote(p[len("/workbench/canvases/"):])
        if method == "GET":
            rec = canvas_store.load(REPO_ROOT, cid)
            return h._send_json(rec) if rec else h._send_json({"error": "画布不存在"}, 404)
        if method == "POST":
            length = int(h.headers.get("Content-Length", 0))
            try:
                body = json.loads(h.rfile.read(length) or b"{}")
                assert isinstance(body, dict)
            except Exception:
                return h._send_json({"error": "需要 JSON body"}, 400)
            err = canvas_store.save(REPO_ROOT, cid, body, body.get("svg"))
            if err:
                return h._send_json({"error": err}, 400)
            return h._send_json({"ok": True, "id": cid, "svg": canvas_store.svg_rel(cid)})
        if method == "DELETE":
            err = canvas_store.remove(REPO_ROOT, cid)
            return h._send_json({"ok": True}) if not err else h._send_json({"error": err}, 400)
    if p == "/workbench/app-asset" and method == "GET":
        params = urllib.parse.parse_qs(q)
        aid = (params.get("id") or [""])[0]
        if aid and re.fullmatch(r"[a-z0-9_-]+", aid):
            from pkgstore import _APPS_DIR
            for d in (os.path.join(FRONTEND, "app-assets", aid), os.path.join(_APPS_DIR, aid)):
                if os.path.isdir(d):
                    return h._send_json({"ok": True, "path": d})
        return h._send_json({"ok": False}, 404)
    return h._send_json({"error": "unknown"}, 404)
