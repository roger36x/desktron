"""系统设置领域路由:引擎选择与选项、设置面板、浏览器操控装配、内网登录态、阅读主题、日志悬浮窗。
拆分自 ui_server._workbench;owns() 按路径认领,
handle(h) 内 h 为 Handler 实例,行为与原 if 链一致。"""
import os, json, time, subprocess, threading, urllib.parse
import logbuf
import runtime_identity
import engine_adapters
import engines as engine_specs
from deskcfg import ENGINES
from ui_static import _frontend_token
from workbench import _engine_pref, _wb_state_patch, _wb_state_read
from settings import _settings_get, _settings_apply


# A normal restart no longer drains every conversation.  The unified store and
# exact Writer fences recover interrupted work; only explicitly critical work blocks.
_RESTART_OPERATION_LOCK = threading.Lock()


def _local_orchestrator(h):
    if getattr(getattr(h, "server", None), "serve_proxy", False):
        return False   # Tailscale Serve 转发的环回来源不是本机部署脚本
    host = str((getattr(h, "client_address", None) or ("",))[0]).split("%")[0]
    return (host in ("127.0.0.1", "::1", "localhost") and
            h.headers.get("X-Desktron-Orchestrator") == "deploy")


def restart_backend(operation_id='', expected_boot_id=''):
    import lifecycle
    if not _RESTART_OPERATION_LOCK.acquire(blocking=False):
        return {'ok': False, 'error': '另一重启请求正在提交'}
    try:
        prepared = lifecycle.prepare_restart(operation_id, expected_boot_id)
        if not prepared.get('ok'):
            return prepared
        try:
            result = lifecycle.submit_host_restart(operation_id)
        except Exception as exc:
            if lifecycle.cancel_unclaimed(operation_id, runtime_identity.facts()):
                return {'ok': False, 'state': 'rejected', 'error': str(exc)[:300]}
            result = {'ok': False, 'state': 'unknown', 'error': str(exc)[:300]}
        if not result.get('ok') and result.get('state') not in ('accepted','completed','unknown'):
            lifecycle.reopen(operation_id)
        if result.get('state') in ('accepted','unknown'):
            threading.Thread(target=lifecycle.reconcile_host_restart,
                             args=(operation_id, runtime_identity.facts()), daemon=True,
                             name='restart-reconcile').start()
        # Unknown means the ACK may have been lost after acceptance.  Admission
        # stays closed; never accept work that a pending TERM could interrupt.
        return {**result, 'operation_id': operation_id,
                'boot_id': runtime_identity.BOOT_ID, 'restart_contract': 4}
    finally:
        _RESTART_OPERATION_LOCK.release()


def _trial_policy():
    from deskcfg import DATA_HOME
    from shepherd.trial import policy
    return policy(DATA_HOME)


def runtime_status():
    import self_update
    import lifecycle
    return {**runtime_identity.facts(), "frontend_rev": _frontend_token(),
            "active_work": self_update.active_work_count(),
            "lifecycle": lifecycle.status()}


# 外部工具登录态:后台刷,前台永远即时返回。
#
# 登录状态 CLI 可能要几十秒才返回。请求这一侧只读缓存、立刻回;缓存过期就顺手起一个
# 后台线程去刷。第一次打开设置页会看到「查询中」,稍后再开就是真结果。超时标 unknown,
# 不写成未登录。
_INTRANET_TTL = 300      # 结果的保鲜期
_INTRANET_WAIT = 60      # 后台刷时给每条 CLI 的上限
_intranet = {"at": 0.0, "v": None, "busy": False}
_intranet_lock = threading.Lock()


def invalidate_intranet_status():
    """丢掉设置页的展示缓存，让下一次 status 重新问各 CLI。

    登录动作发生在独立终端里，Desk 不会收到“授权完成”事件；如果仍沿用五分钟缓存，
    用户刚登完会继续看到“未登录”。这里只失效读缓存，不碰任何凭证。
    """
    with _intranet_lock:
        _intranet["at"] = 0.0
        _intranet["v"] = None


def _intranet_probe():
    """真正去问外部 CLI。只在后台线程里跑,请求线程不等它。"""
    import shutil as _sh
    from concurrent.futures import ThreadPoolExecutor

    def cli(cmd, judge):
        if not _sh.which(cmd[0]):
            return {"installed": False, "logged_in": False}
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=_INTRANET_WAIT)
        except subprocess.TimeoutExpired:
            # 查不到 ≠ 没登录。并成一档的话,界面会对着一个登着的账号写「未登录」,
            # 旁边那枚钮还写着「登录」——照它去做是白跑一趟。
            return {"installed": True, "logged_in": False, "unknown": True,
                    "why": "%s 秒内没有返回" % _INTRANET_WAIT}
        except Exception as e:
            return {"installed": True, "logged_in": False, "unknown": True, "why": str(e)[:80]}
        got = judge((r.stdout or "") + (r.stderr or ""))
        # 判据可以只回真假,也可以回一整档状态(钥匙串读不到就是这种:登着,但取不到凭证)
        if isinstance(got, dict):
            return {"installed": True, **got}
        return {"installed": True, "logged_in": bool(got)}

    def lark_ok(out):
        try:
            d = json.loads(out)
            user = ((d.get("identities") or {}).get("user") or {})
            # lark-cli identitydiag 将 needs_refresh 标为 available=true：访问凭证
            # 到期后下次用户 API 调用会自动续期，不需要重新授权。--verify 续期成功
            # 后也可能保留这个 status，不能只认 ready，更不能只看 tokenStatus。
            status = user.get("status")
            if status in ("ready", "needs_refresh"):
                return (user.get("available") is not False
                        and user.get("verified") is not False)
            if status in ("missing", "not_configured", "unauthorized"):
                return {"logged_in": False}
            if status == "verify_failed":
                return {"logged_in": False, "unknown": True,
                        "why": "lark-cli 登录验证失败，请重新检查"}
        except Exception:
            pass
        # lark-cli 的主密钥存在 macOS 钥匙串里。读不到钥匙串的进程(交付到别人机器上时
        # 实见)拿不到凭证,auth status 直接失败——它不是「没登录」,点登录也修不好
        # (2026-09-04 用户交付后实见:界面显示未登录,人反复登录仍收不到飞书通知)。
        # lark-cli 自带解法:在交互式终端里跑一次 config keychain-downgrade,把主密钥
        # 落成本地文件,此后自动化环境读文件即可。这一档单列出来,连同那条命令一起报。
        if "keychain" in (out or "").lower():
            return {"logged_in": False, "keychain": True,
                    "why": "读不到 macOS 钥匙串,拿不到 lark-cli 的凭证",
                    "fix": "lark-cli config keychain-downgrade"}
        return {"logged_in": False, "unknown": True,
                "why": "无法识别 lark-cli 登录状态，请重新检查"}

    jobs = {
        "lark": lambda: cli(["lark-cli", "auth", "status", "--json"], lark_ok),
    }
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = {k: ex.submit(fn) for k, fn in jobs.items()}
        return {k: f.result() for k, f in futs.items()}


def _intranet_refresh():
    try:
        got = _intranet_probe()
    except Exception:
        got = None
    with _intranet_lock:
        _intranet["busy"] = False
        if got:
            _intranet["v"] = got
            _intranet["at"] = time.time()


def intranet_status():
    """立刻可回的那一份。缓存过期就顺手起一轮后台刷,自己不等。"""
    with _intranet_lock:
        v, at, busy = _intranet["v"], _intranet["at"], _intranet["busy"]
        stale = v is None or time.time() - at > _INTRANET_TTL
        if stale and not busy:
            _intranet["busy"] = True
            threading.Thread(target=_intranet_refresh, daemon=True,
                             name="intranet-status").start()
    if v is not None:
        return dict(v, stale=stale)
    # 一次都还没问出结果:告诉界面「在查」,它会过几秒再来取(见 app-settings.js)
    return {k: {"installed": True, "logged_in": False, "pending": True}
            for k in ("lark",)}


def owns(p, method):
    return p in ("/workbench/engine", "/workbench/engine-update", "/workbench/settings",
                 "/workbench/browser", "/workbench/intranet-status", "/workbench/claude-theme",
                 "/workbench/logs", "/workbench/runtime", "/workbench/restart",
                 "/workbench/shepherd-unified", "/workbench/network-proxy", "/workbench/network-proxy/test",
                 "/workbench/tailscale", "/workbench/lan")


def handle(h, p, q, method):
    if p in ('/workbench/tailscale', '/workbench/lan'):
        if not h._lan_local_request() or h.headers.get('Sec-Fetch-Site') == 'cross-site':
            return h._send_json({'error': '连接设置仅允许本机同源访问。'}, 403)
        if p == '/workbench/lan':
            import lan_settings as connection_settings
            active = getattr(h.server, 'lan_interface', '')
        else:
            import tailscale_settings as connection_settings
            active = getattr(h.server, 'serve_port', 0)
        import durable_json
        try:
            if method == 'GET':
                return h._send_json(connection_settings.status(active))
            if method != 'PUT':
                return h._send_json({'error': 'method not allowed'}, 405)
            if h.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                return h._send_json({'error': 'expected application/json'}, 400)
            try:
                length = int(h.headers.get('Content-Length', 0))
            except (ValueError, TypeError):
                return h._send_json({'error': 'bad body size'}, 400)
            if not 1 <= length <= 1024:
                return h._send_json({'error': 'bad body size'}, 400)
            body = json.loads(h.rfile.read(length))
            if not isinstance(body, dict) or set(body) != {'config', 'expected_revision'}:
                return h._send_json({'error': 'bad body'}, 400)
            return h._send_json(connection_settings.save(body['config'], body['expected_revision'],
                               h.server.server_address[1], active))
        except durable_json.ConflictError:
            return h._send_json({'error': '连接设置已更新，请刷新后再保存。'}, 409)
        except connection_settings.SaveOutcomeUnknown:
            return h._send_json({'state': 'unknown', 'error': '保存结果未知，请刷新状态核对后再操作。'}, 503)
        except ValueError as exc:
            return h._send_json({'error': '连接配置格式错误。'
                                if isinstance(exc, json.JSONDecodeError) else str(exc)}, 400)
        except OSError:
            return h._send_json({'error': '连接配置读取或保存失败，请检查本机文件权限。'}, 500)
    if p in ("/workbench/network-proxy", "/workbench/network-proxy/test"):
        # Network destinations are local-machine policy, never remote/browser cross-site writes.
        if not h._lan_local_request() or h.headers.get('Sec-Fetch-Site') == 'cross-site':
            return h._send_json({'error': '网络代理设置仅允许本机同源访问。'}, 403)
        import network_proxy
        import durable_json
        try:
            if p == '/workbench/network-proxy' and method == 'GET':
                return h._send_json(network_proxy.status())
            if (p.endswith('/test') and method != 'POST') or (not p.endswith('/test') and method != 'PUT'):
                return h._send_json({'error': 'method not allowed'}, 405)
            if h.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                return h._send_json({'error': 'expected application/json'}, 400)
            try:
                length = int(h.headers.get('Content-Length', 0))
            except (ValueError, TypeError):
                return h._send_json({'error': 'bad body size'}, 400)
            if not 1 <= length <= 16384:
                return h._send_json({'error': 'bad body size'}, 400)
            body = json.loads(h.rfile.read(length))
            if not isinstance(body, dict):
                return h._send_json({'error': 'bad body'}, 400)
            if p.endswith('/test'):
                result = network_proxy.test_connection(body.get('config'))
            else:
                result = network_proxy.save(body.get('config'), body.get('expected_revision'))
            return h._send_json(result)
        except durable_json.ConflictError:
            return h._send_json({'error': '网络设置已更新，请重新读取后再保存。'}, 409)
        except ValueError as exc:
            # JSON decoder errors can include input; only our validated errors are user-facing.
            message = '网络配置格式错误。' if isinstance(exc, json.JSONDecodeError) else str(exc)
            return h._send_json({'error': message}, 400)
        except OSError:
            return h._send_json({'error': '网络配置读取或保存失败；请检查本机文件权限。'}, 500)
    if p == "/workbench/runtime" and method == "GET":
        return h._send_json(runtime_status())
    if p == "/workbench/restart" and method == "POST":
        if not _local_orchestrator(h):
            return h._send_json({"error": "forbidden"}, 403)
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
        except Exception:
            return h._send_json({"error": "bad body"}, 400)
        result = restart_backend((body or {}).get("operation_id", ""),
                                 (body or {}).get("expected_boot_id", ""))
        return h._send_json(result, 200 if result.get("ok") else 409)
    if p == "/workbench/shepherd-unified" and method in ("GET", "POST"):
        if not _local_orchestrator(h):
            return h._send_json({"error": "forbidden"}, 403)
        import engine
        rows = engine._shepherd().promote_sessions()
        return h._send_json({"ok": True,
                             "complete": all(row["state"] == "unified" for row in rows),
                             "sessions": rows})
    if p == "/workbench/engine-update" and method == "POST":
        # 一键更新:命令取自 manifest 那一份声明,不收客户端传来的命令行。
        # 同步跑完再回,npm / curl 那条通常几十秒;界面在这期间显示「更新中」。
        import engine_updates
        from deskcfg import REPO_ROOT as _root
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        return h._send_json(engine_updates.run_update(_root, str((body or {}).get("engine") or "")))
    if p == "/workbench/engine":
        if method == "GET":
            # 可用性问引擎描述符,不另立一张表:上一版这里写着「别再逐个写」,底下却硬列了
            # 三家,于是 2026-08-17 接 pi 时照样漏了——图标出来了、菜单里却切不过去。
            # spec.available() 与适配器那份判据相同(都是 which(bin)),少一处要同步的地方。
            avail = {k: bool(sp.available()) for k, sp in engine_specs.ENGINES.items()}
            st = _wb_state_read()
            # manifest:每引擎前端元数据(图标/色/描述/几何/字形/设置卡形态),前端据此免硬编码 per-engine 常量。
            return h._send_json({"engine": _engine_pref(), "engines": ENGINES,
                                    "available": avail, "opts": st.get("engine_opts", {}),
                                    "shepherd_engine": st.get("shepherd_engine", ""),
                                    "shepherd_mode": bool(st.get("shepherd_mode")),
                                    # Trial storage remains readable by its session IDs, but
                                    # new conversations always enter the formal unified desk.
                                    "shepherd_trial": False,
                                    "shepherd_trial_policy": _trial_policy(),
                                    "manifest": engine_specs.manifest()})
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
        except Exception:
            return h._send_json({"error": "bad body"}, 400)
        name = body.get("engine")
        if name is not None and name not in ENGINES:
            return h._send_json({"error": "bad engine"}, 400)
        st = _wb_state_read()
        patch = {}
        if "shepherd_engine" in body:
            if body["shepherd_engine"] not in ("claude", "codex", "pi"):
                return h._send_json({"error": "bad shepherd_engine"}, 400)
            patch["shepherd_engine"] = body["shepherd_engine"]
        if name:
            patch["engine"] = name
        if "shepherd_mode" in body:
            if not isinstance(body.get("shepherd_mode"), bool):
                return h._send_json({"error": "bad shepherd_mode"}, 400)
            patch["shepherd_mode"] = body["shepherd_mode"]
            if not body['shepherd_mode']: patch['shepherd_trial'] = False
        if 'shepherd_trial' in body:
            if type(body['shepherd_trial']) is not bool:
                return h._send_json({'error':'bad shepherd_trial'},400)
            if body['shepherd_trial'] and not body.get('shepherd_mode',st.get('shepherd_mode')):
                return h._send_json({'error':'trial requires shepherd_mode'},400)
            patch['shepherd_trial'] = body['shepherd_trial']
        if isinstance(body.get("opts"), dict):
            eo = st.setdefault("engine_opts", {})
            for k, v in body["opts"].items():
                if k in ENGINES and isinstance(v, dict):
                    target = eo.setdefault(k, {})
                    target.pop("desk_role", None)  # 清理曾误写的存量值
                    target.update({name: value for name, value in v.items()
                                   if name != "desk_role"})
            patch["engine_opts"] = eo
        if patch:
            _wb_state_patch(patch)
        current = _wb_state_read()
        return h._send_json({"ok": True, "engine": _engine_pref(),
                             "shepherd_mode": bool(current.get("shepherd_mode")),
                             "shepherd_trial": bool(current.get("shepherd_mode") and current.get("shepherd_trial"))})
    if p == "/workbench/logs" and method == "GET":
        # 日志悬浮窗轮询:?since=<seq> 增量取本进程 stdout/stderr 捕获(见 logbuf.py)
        qp = urllib.parse.parse_qs(h.path.split("?", 1)[1] if "?" in h.path else "")
        try:
            since = int(qp.get("since", ["0"])[0])
        except ValueError:
            since = 0
        return h._send_json(logbuf.tail(since))
    if p == "/workbench/logs" and method == "POST":
        # 前端往同一条流里写。浏览器的 console 进不了这个面板,而前端状态恰恰是最难取证的
        # 那一部分——没有这条路,每次都得开 Web 检查器。
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
        except Exception:
            return h._send_json({"error": "bad body"}, 400)
        logbuf.push_client(body.get("line") or body.get("text") or "")
        return h._send_json({"ok": True})
    if p == "/workbench/settings":
        if method == "GET":
            return h._send_json(_settings_get())
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
        except Exception:
            return h._send_json({"error": "bad body"}, 400)
        res = _settings_apply(body)
        return h._send_json(res)
    if p == "/workbench/browser":
        # 外部 Chrome 的 Playwright MCP 兼容入口。默认设置页已改用 Desk App Control；
        # 这里暂时保留，避免覆盖已有用户手工启用的全局引擎配置。
        import browser_mcp
        if method == "GET":
            return h._send_json(browser_mcp.status_all())
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
        except Exception:
            return h._send_json({"error": "bad body"}, 400)
        try:
            return h._send_json(browser_mcp.set_engine(
                body.get("engine", ""), bool(body.get("enabled")), body.get("token") or ""))
        except Exception as e:
            return h._send_json({"error": str(e)}, 400)
    if p == "/workbench/intranet-status" and method == "GET":
        # 只读缓存、立刻回;真正去问 CLI 的活在后台线程里(见 intranet_status)。
        return h._send_json(intranet_status())
    if p == "/workbench/claude-theme" and method == "GET":
        # Desk 阅读主题状态:CC 自定义主题(~/.claude/themes/desk-reading.json)是否启用
        try:
            with open(os.path.expanduser("~/.claude/settings.json"), encoding="utf-8") as f:
                _cc = json.load(f)
        except Exception:
            _cc = {}
        return h._send_json({"enabled": _cc.get("theme") == "custom:desk-reading",
                                "theme": _cc.get("theme") or ""})
    if p == "/workbench/claude-theme" and method == "POST":
        # 开关 Desk 阅读主题:开=确保主题文件在位并选中;关=回落 dark。只动 theme 键,其余配置不碰。
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        _thdir = os.path.expanduser("~/.claude/themes")
        _thfile = os.path.join(_thdir, "desk-reading.json")
        _spath = os.path.expanduser("~/.claude/settings.json")
        try:
            if body.get("enable"):
                os.makedirs(_thdir, exist_ok=True)
                if not os.path.exists(_thfile):
                    with open(_thfile, "w", encoding="utf-8") as f:
                        json.dump({"name": "Desk 阅读", "base": "dark", "overrides": {
                            "text": "#d8d8d4", "inactive": "#9a9a96", "subtle": "#5c6066",
                            "claude": "#d97757", "success": "#8cc265", "error": "#e05561",
                            "warning": "#e6c07b", "suggestion": "#4aa5f0", "permission": "#4aa5f0",
                            "planMode": "#42b3c2", "promptBorder": "#565b65",
                            "diffAdded": "#243524", "diffRemoved": "#3a2426",
                            "diffAddedDimmed": "#20291f", "diffRemovedDimmed": "#2b2022",
                            "diffAddedWord": "#3a5a36", "diffRemovedWord": "#6b3a3e",
                        }}, f, ensure_ascii=False, indent=2)
            _cc = {}
            if os.path.exists(_spath):
                with open(_spath, encoding="utf-8") as f:
                    _cc = json.load(f)
            _cc["theme"] = "custom:desk-reading" if body.get("enable") else "dark"
            with open(_spath, "w", encoding="utf-8") as f:
                json.dump(_cc, f, ensure_ascii=False, indent=2)
        except Exception as e:
            return h._send_json({"error": str(e)}, 500)
        return h._send_json({"ok": True, "enabled": bool(body.get("enable"))})
    return h._send_json({"error": "unknown"}, 404)
