"""首次设置的检查清单:把已有的探测编排成有依赖顺序的节点。

不新写探测。装没装、登没登各自已有一份判断,而且都带缓存:
  - 运行时与引擎装机     check_deps.probe(manifest 的 requirements)
  - 引擎登录             settings._settings_get()(各引擎并行,子进程探测)
  - 可选外部工具登录     system.api.intranet_status()(只读缓存,后台刷)
再写一份就会漂:设置页说已登录、这一页说没有,人不知道该信哪个。所以这里只做编排——
取那几份的结果,按依赖排成节点,算出"当前该做哪一步"。

分两档(见 REQUIRED):必要项没配齐 Desk 用不起来;外部工具是可选的,
不用它的人不该被算成"没配完"。进度只按必要项算。
"""
import json
import os
import subprocess
import threading

_MARK = "setup-seen"          # 首启标记:落在 DATA_HOME 同级,与 .update-rev 同处


def _mark_path():
    from deskcfg import DATA_HOME
    return os.path.join(os.path.dirname(DATA_HOME), "." + _MARK)


def seen():
    """这台机器是否已经打开过设置页。首启弹一次,之后只从设置页进(用户定的)。"""
    return os.path.exists(_mark_path())


def mark_seen():
    """打开即记,不等配完——配不配得完是用户的事,弹不弹是我们的事。
    配到一半关掉、下次启动又被弹一次,那是骚扰。"""
    try:
        p = _mark_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("1\n")
        return True
    except Exception:
        return False


def refresh():
    """补齐可用 Provider、失效短期探测缓存，再返回真实状态。"""
    try:
        from system.api import invalidate_intranet_status
        invalidate_intranet_status()
    except Exception:
        pass
    try:
        import settings
        settings.invalidate_auth_probe_caches()
    except Exception:
        pass
    return status()


def _engines():
    """引擎装机 + 登录。装机看 which,登录取 settings 那份并行探测的结果。"""
    import shutil
    out = []
    installs = {}
    try:
        import check_deps
        from deskcfg import REPO_ROOT
        installs = {r["name"]: str(r.get("install") or "")
                    for r in check_deps.parse_reqs(REPO_ROOT)
                    if r.get("kind") == "engine"}
    except Exception:
        pass
    try:
        import settings
        cred = settings._settings_get()
    except Exception:
        cred = {}
    # 登录字段各家不同名:claude 报 subscription(钥匙串条目),codex/pi 报 logged_in
    # (见 engines/<key>.py 的 credentials)。只认 logged_in 会把 claude 判成未登录——
    # 而人正是通过 claude 在用 Desk,这种误报比不报更坏(2026-09-01 用户指出)。
    LOGIN_KEYS = ("logged_in", "subscription")
    for key, label in (("claude", "Claude Code"), ("codex", "Codex"), ("pi", "Pi")):
        c = cred.get(key) if isinstance(cred.get(key), dict) else {}
        # api_key_set 也算配好:claude/codex 两条路都能用,只认 OAuth 会把用 key 的人判成未配。
        ok = any(c.get(k) for k in LOGIN_KEYS) or bool(c.get("api_key_set"))
        out.append({"key": key, "label": label,
                    "installed": bool(shutil.which(key)),
                    "logged_in": ok,
                    "install": installs.get(key, ""),
                    # 查不到 ≠ 没登录(超时那一档),照实传给前端,别写成"未登录"配一颗"去登录"的钮
                    "unknown": bool(c.get("unknown"))})
    return out


def _intranet():
    """可选外部工具登录。读 system.api 的缓存,不触发同步探测。"""
    try:
        from system.api import intranet_status
        st = intranet_status() or {}
    except Exception:
        st = {}
    rows, pending = [], False
    for key, label, hint in (("lark", "lark-cli", "飞书文档 / 消息 / 日历"),):
        v = st.get(key) if isinstance(st.get(key), dict) else {}
        # pending 挂在每一行里(第一次访问、后台还在问那几条 CLI),不是顶层字段。
        pending = pending or bool(v.get("pending"))
        rows.append({"key": key, "label": label, "hint": hint,
                     "installed": bool(v.get("installed")),
                     "logged_in": bool(v.get("logged_in")),
                     # 查不到与没登录是两回事:超时那一档标 unknown,界面照实说"查询中/查不到",
                     # 不写成"未登录"——照它去登录是白跑一趟(system.api 那边同样的取舍)。
                     "unknown": bool(v.get("unknown") or v.get("pending")),
                     # 钥匙串读不到又是第三档:登着、也不是查不到,而是凭证取不出来。
                     # 它有确定的修复动作(fix),界面据此把「登录」换成那条命令。
                     "keychain": bool(v.get("keychain")),
                     "fix": str(v.get("fix") or ""),
                     "why": str(v.get("why") or "")})
    return {"rows": rows, "pending": pending}


def _runtime():
    """运行时体检。缺了 Desk 起不来——能看到这一页说明多半是过的,但换机后 node 掉版这类
    情况仍要报出来,否则引擎装不上时人会去怀疑网络。

    判据与 `desktron deps` 同源:manifest 的 requirements 里 kind=runtime 那几条,
    probe() 逐个取版本再与 min 比。探测有子进程(每条 8 秒上限),而 runtime 只有三条
    且都是本机二进制,实测毫秒级,可以挂在请求线程上。"""
    rows = []
    try:
        import check_deps
        from deskcfg import REPO_ROOT
        for req in check_deps.parse_reqs(REPO_ROOT):
            if req.get("kind") != "runtime":
                continue
            name = req["name"]
            found, ver = check_deps.probe(name)
            need = req.get("min") or ""
            ok = found and (not need or check_deps.ver_tuple(ver) >= check_deps.ver_tuple(need))
            # hint 给人读(含备选),install 给引导页预填成一条可执行命令。缺一不可:
            # 只有 hint 按不了,只有 install 在它失败时(比如没装 brew)没有退路可看。
            rows.append({"name": name, "ok": bool(ok), "version": ver, "min": need,
                         "hint": str(req.get("hint") or ""),
                         "install": str(req.get("install") or "")})
    except Exception:
        # 解析不了 manifest 时不谎报失败:这一页的价值是指路,不是拦路。
        return {"ok": True, "rows": [], "missing": [], "unknown": True}
    bad = [r["name"] for r in rows if not r["ok"]]
    return {"ok": not bad, "rows": rows, "missing": bad}


# 必要项:没配齐 Desk 用不起来。外部工具不在内——不用它的人不该被算成没配完。
REQUIRED = ("runtime", "engine_login")


def status():
    """给设置页与 desktron setup 共用的一份。节点顺序即依赖顺序。"""
    rt = _runtime()
    engines = _engines()
    intra = _intranet()
    eng_installed = [e for e in engines if e["installed"]]
    # 首次设置不绑定特定模型或厂商：至少装好并登录一家即可，其余均可稍后添加。
    eng_login_ok = any(e["logged_in"] for e in eng_installed)
    groups = {
        "runtime": {"ok": rt["ok"], "detail": rt},
        "engines": {"ok": bool(eng_installed), "detail": engines},
        "engine_login": {"ok": eng_login_ok, "detail": engines},
        # 还在查(pending)时不判失败:那会让首启第一眼就显示两个红叉,几十秒后自己变绿,
        # 中间这段人已经点进去登录了。判 ok 但标 pending,界面显示"查询中"。
        # 钥匙串受阻不算就绪:它正是「看着像登着、实际用不了」的那一档。
        "intranet": {"ok": all((r["logged_in"] or r["unknown"]) and not r.get("keychain")
                               for r in intra["rows"]),
                     "detail": intra["rows"], "pending": intra["pending"]},
    }
    need = [k for k in REQUIRED if not groups[k]["ok"]]
    from provision import setup_tasks
    try:
        operation = setup_tasks.latest()
        # 历史失败/中断保留在安装记录中，不代表当前环境仍需配置。
        operation_pending = bool(operation and operation['state'] in
                                 ('starting', 'waiting_terminal', 'running'))
    except (OSError, ValueError):
        operation_pending = True  # Unreadable operation state must retain the recovery entry.
    return {
        "operation_pending": operation_pending,
        "groups": groups,
        "required": list(REQUIRED),
        "done": len(REQUIRED) - len(need),
        "total": len(REQUIRED),
        "complete": not need,
        "next": need[0] if need else "",
        "seen": seen(),
    }
