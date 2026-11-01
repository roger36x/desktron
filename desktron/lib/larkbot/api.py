"""larkbot 领域路由(owns/handle,与 remote/api.py 同一套约定)。

/workbench/lark-bot/event 是 Node sidecar 的入口,其余是 Desk 面板在用的管理面。

event 那条走后台线程:一轮引擎可能跑几分钟,而飞书要求事件处理尽快返回,
sidecar 那侧也不等我们(见 lark-ws.mjs 的 forward 不 await)。同步处理会让
HTTP 连接挂着几分钟,期间 Desk 的其他请求排在后面——ui_server 是 ThreadingHTTPServer,
不会真的堵死,但一条消息占一个线程几分钟仍然不划算。
"""
import json
import os
import threading
import time

import deskcfg
from deskcfg import REPO_ROOT, UI_PORT

from . import handler, larkapi, peers, sidecar, store

PREFIX = "/workbench/lark-bot"

# 飞书 SDK 会处理短暂断线，但默认下一次重试可能要等两分钟以上；Node sidecar
# 自己退出时更是没人再拉。这里看护的是「声明仍启用」这件事，而不是某个 PID。
# 短抖动先留给 SDK，持续未恢复才换进程；失败后退避，避免网络长期不可用时频繁拉起。
WATCH_INTERVAL_S = 10
WATCH_STUCK_S = 75
WATCH_BACKOFF_MAX_S = 300
_watch_lock = threading.Lock()
_watch_started = False
_watch_bad_since = {}
_watch_retry_at = {}
_watch_failures = {}
_FATAL_CONNECT_ERRORS = ("invalid appid", "invalid appsecret", "app secret",
                         "app_id is needed", "appsecret is needed",
                         "permission denied", "forbidden", "unauthorized")


def owns(p, method):
    return p == PREFIX or p.startswith(PREFIX + "/")


def _body(h):
    try:
        ln = int(h.headers.get("Content-Length", 0))
        return json.loads(h.rfile.read(ln) or b"{}")
    except Exception:
        return {}


def _held(limit=50):
    try:
        with open(store.held_path(REPO_ROOT), encoding="utf-8") as f:
            d = json.load(f)
        return (d.get("items") or [])[-limit:][::-1]
    except Exception:
        return []


def _me():
    """这一份 Desk 在 backends.json 里叫什么。前端要按它把操作发回对的那一侧。"""
    try:
        import remote_auth
        return "server" if remote_auth.role() == "remote" else "desktop"
    except Exception:
        return "desktop"


def _query(q, key):
    for kv in (q or "").split("&"):
        k, _, v = kv.partition("=")
        if k == key:
            import urllib.parse
            return urllib.parse.unquote(v)
    return ""


def _q_forward(q):
    """转发时带过去的查询串:摘掉 backend,其余照原样。

    其余参数必须带上——/held?count=1 若退化成不带参数,对面会把被拦下的原文整份发过来,
    而那正是不该跨机器传的那类文本。
    backend 必须摘掉——对面按它自己的名字答,留着它会看见一个不是自己的名字又往外转
    一次(hop 头拦得住,但白跑一趟)。
    """
    keep = [kv for kv in (q or "").split("&") if kv and kv.partition("=")[0] != "backend"]
    return "&".join(keep)


def _sync_avatar(a, aid, app_id):
    """把开放平台的应用头像同步进声明。取不到就算了——头像是锦上添花,
    不该让它把「启动成功」或「自检就绪」变成失败。"""
    if not app_id:
        return
    try:
        info, _e = larkapi.bot_info(app_id, a.get("domain") or "")
        if info.get("avatar") and info["avatar"] != a.get("avatar"):
            decl = dict(a)
            decl.pop("id", None)
            decl["avatar"] = info["avatar"]
            store.save(REPO_ROOT, aid, decl)
    except Exception:
        pass


def _save_enabled(a, enabled):
    """启动/停止是跨重启意愿，落回声明；不把运行态 PID 写进配置。"""
    decl = dict(a or {})
    aid = str(decl.pop("id", "") or "")
    if not aid:
        return None, "缺 agent id"
    decl["enabled"] = bool(enabled)
    return store.save(REPO_ROOT, aid, decl)


def _fatal_connect_error(state):
    """只有明确需要改配置的错误才停止自动重试；网络错误仍由看护恢复。"""
    error = str((state or {}).get("error") or "").lower()
    return bool(error and any(mark in error for mark in _FATAL_CONNECT_ERRORS))


def _watch_once(now=None):
    """检查一次所有应运行的 Bot；测试也直接调用这一层，不启动真实线程。"""
    now = time.time() if now is None else float(now)
    wanted = set()
    for a in store.list_all(REPO_ROOT, enabled_only=True):
        app_id = str(a.get("lark_app_id") or "")
        if not app_id or not larkapi.app_secret(app_id):
            continue
        wanted.add(app_id)
        pid = sidecar.running(app_id)
        state = sidecar.conn_state(app_id)
        conn = state.get("status") or ""
        healthy = bool(pid and state.get("fresh") and conn == "connected")
        if healthy:
            _watch_bad_since.pop(app_id, None)
            _watch_retry_at.pop(app_id, None)
            _watch_failures.pop(app_id, None)
            continue

        # 明确的凭证/配置拒绝不是网络抖动。重复启动不会变好，只会刷日志；等人
        # 修配置后点启动，或换凭证后由下一次检查重新判断。
        if conn == "failed" and _fatal_connect_error(state):
            _watch_bad_since.pop(app_id, None)
            continue

        since = _watch_bad_since.setdefault(app_id, now)
        if pid and now - since < WATCH_STUCK_S:
            continue                    # 短断线先让官方 SDK 自己恢复
        if now < _watch_retry_at.get(app_id, 0):
            continue

        if pid:
            sidecar.stop(app_id)        # 只换进程；enabled 仍为真，看护意愿不变
        new_pid, err = sidecar.start(app_id, a.get("domain") or "", UI_PORT,
                                     install_missing=True)
        failures = _watch_failures.get(app_id, 0)
        if new_pid:
            _watch_bad_since[app_id] = now
            _watch_retry_at[app_id] = now + min(15 * (2 ** failures), WATCH_BACKOFF_MAX_S)
            _watch_failures[app_id] = min(failures + 1, 8)
            deskcfg.log("larkbot", f"{a.get('id')} 长连接已自动重启 pid={new_pid}")
        else:
            failures = min(failures + 1, 8)
            _watch_failures[app_id] = failures
            _watch_retry_at[app_id] = now + min(15 * (2 ** failures), WATCH_BACKOFF_MAX_S)
            deskcfg.log("larkbot", f"{a.get('id')} 自动重连失败:{str(err)[:160]}")

    # 声明被停用或删除后，把旧的退避状态一并忘掉；以后重新启用从即时检查开始。
    for app_id in set(_watch_bad_since) | set(_watch_retry_at) | set(_watch_failures):
        if app_id not in wanted:
            _watch_bad_since.pop(app_id, None)
            _watch_retry_at.pop(app_id, None)
            _watch_failures.pop(app_id, None)


def _watch_loop():
    boot()
    # 登录态检查本身也要启动一次 CLI（Claude 本机实测约 1.8s）。放到后台看护线程预热，
    # 第一条真实消息就命中十分钟缓存，不把这段冷启动算在用户等待里。
    try:
        from . import runner
        engines = {a.get("engine") or "claude"
                   for a in store.list_all(REPO_ROOT, enabled_only=True)}
        for engine in engines:
            runner.ready(engine)
    except Exception as e:
        deskcfg.log("larkbot", f"引擎登录态预热失败:{str(e)[:160]}")
    while True:
        time.sleep(WATCH_INTERVAL_S)
        try:
            _watch_once()
        except Exception as e:
            deskcfg.log("larkbot", f"看护检查失败:{str(e)[:160]}")


def start_background():
    """启动一次 Bot 看护；重复调用无害。"""
    global _watch_started
    with _watch_lock:
        if _watch_started:
            return False
        _watch_started = True
    threading.Thread(target=_watch_loop, daemon=True, name="larkbot-watch").start()
    return True


def handle(h, p, q, method):
    rest = p[len(PREFIX):].strip("/")

    # 接入终端属于当前 Desk，目标设备记在记录里，不随当前页面的筛选改变。
    if rest == "onboarding" or rest.startswith("onboarding/"):
        from . import onboarding
        seg = rest.split("/")
        if len(seg) > 2:
            return h._send_json({"error": "接入路径无效"}, 404)
        try:
            if method == "GET":
                return h._send_json({"items": onboarding.listing()} if len(seg) == 1
                                    else onboarding.get(seg[1]))
            if method == "POST":
                import self_update
                with self_update.admit_work(user_activity=True) as admitted:
                    if not admitted:
                        return h._send_json({"error": "Desk 正在更新，请稍后继续接入"}, 409)
                    b = _body(h)
                    job = onboarding.start(b.get("backend") or "", bool(b.get("have_app")),
                                           seg[1] if len(seg) == 2 else None)
                return h._send_json(job)
            if method == "DELETE" and len(seg) == 2:
                return h._send_json(onboarding.cancel(seg[1]))
        except (ValueError, FileNotFoundError):
            return h._send_json({"error": "接入记录或目标设备无效"}, 400)
        except Exception:
            return h._send_json({"error": "无法读取或启动接入，请稍后重试；已保存的进度不会被清除"}, 500)
        return h._send_json({"error": "不支持的接入操作"}, 405)

    # 指名道姓要操作另一侧的:带票转过去,原样把结果倒回来。只转 larkbot 这几个端点
    # (peers.ALLOW_PREFIX),不做通用代理——那意味着拿到本机的票就能借道打另一台。
    want = _query(q, "backend")
    if want and want != _me() and not peers.is_hop(h.headers):
        pr, err = peers.find(want, f"http://127.0.0.1:{UI_PORT}", _me())
        if err:
            return h._send_json({"ok": False, "error": err}, 404)
        body = _body(h) if method in ("POST", "PUT", "PATCH") else None
        fwd = _q_forward(q)
        d, err2 = peers.call(pr, p + ("?" + fwd if fwd else ""), method, body, timeout=40)
        if err2:
            return h._send_json({"ok": False, "error": f"{want}:{err2}"}, 502)
        if rest == "held" and _query(q, "count") == "1" and isinstance(d, dict) and "items" in d:
            # 对面还是旧版本,不认 count,把被拦下的原文整份发了过来。就地丢掉只留条数。
            # 它已经过了一次网线,这里挡不住那一步——挡的是它进到这一台的界面上、
            # 被当成本机的记录看。要看原文去那一侧,那也是唯一该看得到它的地方。
            d = {"count": len(d.get("items") or []), "stale_peer": True}
        return h._send_json(d if isinstance(d, dict) else {"ok": True})

    # ---- sidecar 的入口 ----
    if rest == "event" and method == "POST":
        ev = _body(h)
        threading.Thread(target=handler.handle, args=(REPO_ROOT, ev, deskcfg.log),
                         daemon=True, name="larkbot-event").start()
        # 立刻回:处理结果由 Desk 的日志与面板呈现,不经这条响应回给 sidecar。
        # sidecar 那侧只需要知道"收到了",它不做重试(重推与去重各在飞书与 handler 那侧)。
        return h._send_json({"ok": True, "accepted": True})

    # ---- 管理面 ----
    if rest == "agents" and method == "GET":
        ags = store.list_all(REPO_ROOT)
        me = _me()
        # 卡片「最近」块的数据:最后活跃、近 7 天条数、最后一轮问句与用量、红线拦截数。
        # 都是本机状态文件里现成的,跨侧的 bot 由它自己那侧的 Desk 算好带过来。
        act = store.activity(REPO_ROOT)
        held_by = {}
        for it in _held(limit=10000):
            held_by[it.get("agent") or ""] = held_by.get(it.get("agent") or "", 0) + 1
        out = {"items": [dict(a, backend=me,
                              activity=act.get(a.get("id") or "", {}),
                              held_count=held_by.get(a.get("id") or "", 0)) for a in ags],
               "status": [dict(x, backend=me) for x in sidecar.status(ags)],
               "backend": me, "peers": []}
        # 从另一侧转过来的请求不再往外聚合:两台互相指着对方时会无限转下去,
        # 而那种配置(各自把对方写进 backends.json)本来就是正常的。
        if not peers.is_hop(h.headers):
            for pr in peers.others(f"http://127.0.0.1:{UI_PORT}", me):
                d, err = peers.call(pr, PREFIX + "/agents")
                if err or not isinstance(d, dict):
                    # 那一侧关机、不在内网、票过期都很正常。如实标出来,不要静默吞掉——
                    # 少显示几个 bot 与「那边连不上」是两回事,后者要人去处理。
                    out["peers"].append({"name": pr["name"], "ok": False, "error": err or "返回异常"})
                    continue
                out["peers"].append({"name": pr["name"], "ok": True,
                                     "count": len(d.get("items") or [])})
                out["items"] += [dict(a, backend=pr["name"]) for a in (d.get("items") or [])]
                out["status"] += [dict(x, backend=pr["name"]) for x in (d.get("status") or [])]
        return h._send_json(out)

    if rest == "agents" and method == "POST":
        b = _body(h)
        aid = str(b.pop("id", "") or "").strip().lower()
        if not aid:
            return h._send_json({"ok": False, "error": "缺代号"}, 422)
        saved, err = store.save(REPO_ROOT, aid, b)
        if err:
            return h._send_json({"ok": False, "error": err}, 422)
        return h._send_json({"ok": True, "agent": saved})

    if rest.startswith("agents/"):
        seg = rest.split("/")
        aid = seg[1] if len(seg) > 1 else ""
        act = seg[2] if len(seg) > 2 else ""
        a = store.load(REPO_ROOT, aid)
        if not a:
            return h._send_json({"ok": False, "error": f"没有 agent {aid}"}, 404)
        app_id = a.get("lark_app_id") or ""

        if not act and method == "DELETE":
            sidecar.stop(app_id)
            return h._send_json({"ok": store.delete(REPO_ROOT, aid)})
        if not act and method == "GET":
            # used:最近几轮实际读到了什么。它是「配置调得对不对」的唯一依据——
            # 开关显示「开」不等于真读到了东西(检索词一个都没命中就是 0 条)。
            return h._send_json({"agent": a, "log": sidecar.log_tail(app_id),
                                 "used": store.used_recent(REPO_ROOT, aid)})
        if act == "preview" and method == "GET":
            # 按已保存的配置现拼一份上下文,给人看「它每条消息实际收到什么」。
            # 没有这一眼,第 0 段(自动生成)与检索效果都是盲配——开关显示「开」,
            # 内容长什么样、role 排在哪儿,全靠猜(2026-08-27 用户指出 UI 无任何体现)。
            # 不落盘、不进会话:build 本身无副作用,anchor 随便给一个不存在的。
            from . import context as ctx_mod
            demo = _query(q, "text") or "你能做什么"
            prompt, used = ctx_mod.build(REPO_ROOT, a, demo, "preview:none")
            # recent:最近三轮实况用量。与现拼的 prompt 并排,「配的」与「实际发生的」
            # 一处看全——开关开着不等于实际读到了内容。
            return h._send_json({"prompt": prompt, "used": used, "text": demo,
                                 "recent": store.used_recent(REPO_ROOT, aid, n=3)})
        if act == "ping" and method == "POST":
            if not app_id:
                return h._send_json({"ok": False, "error": "声明里没写 lark_app_id"}, 422)
            ok, why = larkapi.ping(app_id, a.get("domain") or "")
            if ok:
                _sync_avatar(a, aid, app_id)
                from . import runner
                engine_ok, engine_why = runner.ready(a.get("engine") or "claude", force=True)
                if not engine_ok:
                    return h._send_json({"ok": False, "detail": engine_why,
                                         "lark_ok": True, "engine_ok": False})
                why = (why + "；" if why else "") + f"{a.get('engine') or 'claude'} 可用"
            return h._send_json({"ok": ok, "detail": why})
        if act == "start" and method == "POST":
            _saved, save_err = _save_enabled(a, True)
            if save_err:
                return h._send_json({"ok": False, "pid": 0, "error": save_err}, 422)
            pid, err = sidecar.start(app_id, a.get("domain") or "", UI_PORT,
                                     install_missing=True)
            # 启动时也补一次头像。此前只挂在自检上,而自检是偶发动作——重建 bot 之后
            # 没人点自检,卡片就一直是通用气泡(2026-08-27 用户实见)。启动是每个 bot
            # 的必经之路,挂在这里才不用人记得做什么。
            if pid:
                _sync_avatar(a, aid, app_id)
            return h._send_json({"ok": not err, "pid": pid, "error": err})
        if act == "stop" and method == "POST":
            _saved, save_err = _save_enabled(a, False)
            if save_err:
                return h._send_json({"ok": False, "error": save_err}, 422)
            sidecar.stop(app_id)
            return h._send_json({"ok": True})
        if act == "secret" and method == "POST":
            # 只写不读:凭证进 SECRETS 之后,任何接口都不再把它吐回来
            sec = str(_body(h).get("secret") or "").strip()
            if not sec:
                return h._send_json({"ok": False, "error": "缺 secret"}, 422)
            if not app_id:
                return h._send_json({"ok": False, "error": "先填 lark_app_id"}, 422)
            try:
                from credstore import _secrets_write
                _secrets_write({larkapi.SECRET_KEY_FMT.format(app_id=app_id): sec})
            except Exception as e:
                return h._send_json({"ok": False, "error": str(e)[:200]}, 500)
            larkapi._tok_cache.pop(app_id, None)     # 换了 secret,旧 token 作废
            ok, why = larkapi.ping(app_id, a.get("domain") or "")
            return h._send_json({"ok": ok, "detail": why})
        return h._send_json({"error": "method"}, 405)

    if rest == "held" and method == "GET":
        # count=1 只回条数。跨侧看这个清单时用的就是它:被拦下的正是红线要挡的那类文本,
        # 不该为了在另一台上显示一行就跨机器传一趟——要看原文,去那一侧的 Desk 看。
        if _query(q, "count") == "1":
            return h._send_json({"count": len(_held(limit=10000))})
        return h._send_json({"items": _held()})

    if rest == "deps" and method == "GET":
        ok, why = sidecar.deps_ok()
        return h._send_json({"ok": ok, "detail": why, "home": sidecar.home()})

    if rest == "deps" and method == "POST":
        ok, why = sidecar.ensure_deps()
        return h._send_json({"ok": ok, "detail": why})

    if rest == "engines" and method == "GET":
        from . import runner
        out = []
        for name in sorted(runner.ENGINES):
            path, err = runner.available(name)
            out.append({"name": name, "ok": not err, "path": path, "error": err})
        return h._send_json({"items": out})

    # 某个引擎在这一侧能用哪些模型。现问引擎,可能要几秒——所以单独一个端点,
    # 不并进上面那条:列引擎是每次画面板都调的,不该被列模型拖慢。
    if rest.startswith("engines/") and rest.endswith("/models") and method == "GET":
        from . import runner
        eng = rest.split("/")[1]
        models, err = runner.list_models(eng)
        return h._send_json({"items": models, "error": err})

    return h._send_json({"error": "unknown"}, 404)


def boot():
    """开机把 enabled 且配了凭证的 agent 的长连接拉起来。
    在 ui_server 的启动路径上调一次;失败只记日志,不该拖住整个 Desk 起不来。

    不按「该在哪一侧跑」过滤:本机与服务器的 workspace 各自独立(sync-home 同步的是
    home/ 也就是代码,不是运行态),声明本来就不跨侧——在哪一侧配就在哪一侧跑。"""
    try:
        import remote_auth
        me = remote_auth.desk_id()
        for a in store.list_all(REPO_ROOT, enabled_only=True):
            app_id = a.get("lark_app_id") or ""
            if not app_id or not larkapi.app_secret(app_id):
                continue
            born = a.get("created_on") or ""
            if born and me and born != me:
                # 只提示不拦:这份声明是从别处拷过来的。拷过来接着用是正当需求,
                # 但如果那台也还开着,同一个 app 就有两条长连接、bot 答两遍——
                # 那个现象很难反推到「另一台也在跑」,所以在日志里留一条线索。
                deskcfg.log("larkbot", f"{a.get('id')} 的声明建于另一台机器;"
                                       f"若那台仍在跑同一个 app,消息会被处理两次")
            pid, err = sidecar.start(app_id, a.get("domain") or "", UI_PORT)
            deskcfg.log("larkbot", f"{a.get('id')} 长连接 " + (f"pid={pid}" if pid else f"未起:{err}"))
    except Exception as e:
        deskcfg.log("larkbot", f"boot 失败:{str(e)[:200]}")
