"""装机/包领域路由:包安装器(plan/browse/install/update/卸载)、市场源、资产清单、自更新与版本检查。
拆分自 ui_server._workbench;owns() 按路径认领,
handle(h) 内 h 为 Handler 实例,行为与原 if 链一致。"""
import os, re, json, shlex, subprocess, sys, urllib.parse
from deskcfg import FRONTEND, DATA_HOME, REPO_ROOT
from assets import _wb_assets
from term import _shell_term_open
from provision import setup_tasks
import pkgstore


def owns(p, method):
    return (p == "/workbench/packages" or p.startswith("/workbench/packages/") or
            p == "/workbench/favicon" or
            p in ("/workbench/assets",
                  "/workbench/self-update", "/workbench/update-check",
                  "/workbench/update/check", "/workbench/update/download", "/workbench/update/apply",
                  "/workbench/update/dev-app-apply", "/workbench/update/activity",
                  "/workbench/setup", "/workbench/auth-refresh"))


def handle(h, p, q, method):
    if p == "/workbench/packages":
        # GET=已装包列表(installed.json);装/审/更新/卸载见下面的 /packages/* 路由
        if method == "GET":
            return h._send_json(pkgstore.installed())
        return h._send_json({"error": "method"}, 405)
    if p == "/workbench/packages/catalog" and method == "GET":
        # 兼容旧前端调用。官方 App 已随 Desk 发行，不再从独立内容仓提供目录。
        return h._send_json({"ok": True, "kind": "catalog", "entries": [], "sources": {}})
    if p in ("/workbench/packages/plan", "/workbench/packages/browse") and method == "POST":
        # plan=只读审查一个包地址(安全闸第一步,无副作用);browse=读一个目录(catalog)仓的条目
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
            addr = (body.get("address") or "").strip()
            assert addr
        except Exception:
            return h._send_json({"error": "需要 {address}"}, 400)
        res = (pkgstore.plan if p.endswith("/plan") else pkgstore.browse)(addr)
        return h._send_json(res)   # 审查结果即交付物,ok/errors 在 body 里,总是 200(前端 api() 非 2xx 会丢 body)
    if p == "/workbench/packages/install" and method == "POST":
        # {address, confirm}:confirm=false 等于 plan(只读);confirm=true 且无错才注册进 .workbench/
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
            addr = (body.get("address") or "").strip()
            assert addr
        except Exception:
            return h._send_json({"error": "需要 {address}"}, 400)
        res = pkgstore.install(addr, confirm=bool(body.get("confirm")), target=(body.get("target") or None))
        return h._send_json(res)   # 审查结果即交付物,ok/errors 在 body 里,总是 200(前端 api() 非 2xx 会丢 body)
    if p == "/workbench/packages/update" and method == "POST":
        # {name}:按 installed.json 记的地址 pull + 重注册(已装即已信任,隐含 confirm)
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
            name = (body.get("name") or "").strip()
            assert name
        except Exception:
            return h._send_json({"error": "需要 {name}"}, 400)
        res = pkgstore.update(name)
        return h._send_json(res)   # 审查结果即交付物,ok/errors 在 body 里,总是 200(前端 api() 非 2xx 会丢 body)
    if p.startswith("/workbench/packages/") and method == "DELETE":
        # /workbench/packages/<name> 卸载:按 installed.json 逆注册(引用计数,不误删共享 app/工作区)
        name = urllib.parse.unquote(p.split("/")[-1])
        if not name:
            return h._send_json({"error": "bad path"}, 400)
        res = pkgstore.uninstall(name)
        return h._send_json(res)   # 审查结果即交付物,ok/errors 在 body 里,总是 200(前端 api() 非 2xx 会丢 body)
    if p == "/workbench/favicon" and method == "GET":
        # 站点图标的兜底解析。浏览器不是只看根路径那一个文件,它会解析页面 HTML 里的
        # <link rel="icon">;而我们够不到那段 HTML(原生嵌入不回传图标,同源策略挡着 iframe)。
        # 于是由后端抓一次首页、把声明的图标地址解析出来交给前端去加载。
        # 只回地址不回图片:图标多半在 CDN 上,让浏览器自己取、自己缓存。
        import favicon as _fav
        params = urllib.parse.parse_qs(q)
        return h._send_json(_fav.resolve((params.get("url") or [""])[0]))
    if p == "/workbench/assets":
        return h._send_json(_wb_assets())
    if p == "/workbench/auth-refresh":
        if method != 'POST':
            return h._send_json({'error':'method'}, 405)
        try:
            body = json.loads(h.rfile.read(int(h.headers.get('Content-Length', 0))) or b'{}')
            engine_name = body.get('engine')
            provider = body.get('provider') or engine_name
            if engine_name not in ('claude','codex','pi') or not isinstance(provider, str):
                raise ValueError()
            if len(provider) > 120 or not re.fullmatch(r'[a-zA-Z0-9_.-]+', provider):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            return h._send_json({'error':'认证来源无效'}, 400)
        from engine import refresh_auth_notices
        return h._send_json(refresh_auth_notices(engine_name, provider))
    if p == "/workbench/setup":
        # 首次设置的检查清单。GET 取状态；POST {seen:true} 记下"已经打开过"，
        # POST {refresh:true} 失效短期探测缓存并重新检查。
        # 状态是编排出来的,不新做探测——三处判断各自已有缓存,再写一份就会与设置页说法不一
        # (见 provision/setup.py 的说明)。首启弹一次、之后只从设置页进,是产品决定;
        # 标记只管"弹不弹",与"配没配完"无关,所以打开即记,不等它配完。
        from provision import setup as _setup
        if method == "GET":
            return h._send_json(_setup.status())
        ln = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(ln) or b"{}")
        except Exception:
            body = {}
        if body.get("seen"):
            return h._send_json({"ok": _setup.mark_seen(), "seen": True})
        if body.get("refresh"):
            return h._send_json(_setup.refresh())
        return h._send_json({"error": "未知操作(seen/refresh)"}, 400)
    if p == "/workbench/update-check" and method == "GET":
        import self_update
        return h._send_json(self_update.status())
    if p in ("/workbench/self-update", "/workbench/update/check") and method == "POST":
        # self-update 是旧前端兼容入口：不再打开已下线的命令，统一触发后台检查。
        import self_update
        return h._send_json(self_update.trigger_check())
    if p == "/workbench/update/activity" and method == "POST":
        import self_update
        return h._send_json(self_update.note_activity())
    if p == "/workbench/update/download" and method == "POST":
        import self_update
        result = self_update.trigger_check(download=True)
        return h._send_json(result, 409 if result.get("ok") is False else 200)
    if p in ("/workbench/update/apply", "/workbench/update/dev-app-apply") and method == "POST":
        import self_update
        ln = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(ln) or b"{}")
        except Exception:
            body = {}
        if p.endswith("/dev-app-apply"):
            result = self_update.request_development_app(body.get("stage"), body.get("confirm"))
        else:
            result = self_update.request_apply(body.get("confirm"), automatic=False)
        return h._send_json(result, 200 if result.get("ok") else 409)
    return h._send_json({"error": "unknown"}, 404)
