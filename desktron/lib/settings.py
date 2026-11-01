"""设置面板后端:各引擎登录态探测 + 读写凭证 / 网关配置。

组合 credstore 的文件 I/O 暴露给设置面板;依赖 credstore,单向向下。
"""
import os, json, re, subprocess, time, threading

import claude_auth
from credstore import _secrets_read, _secrets_write, _mask_key, secrets_env
from workbench import _wb_state_patch, _wb_state_read
from pi_extensions import (
    prompt_isolation_args, provider_extension_args, without_legacy_web_secrets,
)


def _claude_subscription():
    """订阅登录态以独立 CLI 的结论为准；None 表示当前无法可靠探测。"""
    return claude_auth.subscription_status()


def _codex_logged_in():
    """codex 登录态:~/.codex/auth.json 存在且非空(codex login 写在那)。"""
    try:
        return os.path.getsize(os.path.expanduser("~/.codex/auth.json")) > 2
    except Exception:
        return False


# Pi 的模型目录由 provider、models.json、OAuth/API key 共同决定，不能从一份静态配置猜。
# RPC 只取公开的模型元数据与当前选择；不请求、不打印 credentials。设置页与会话菜单共用缓存，
# 避免每开一段会话都冷起一次 node 进程。
_PI_PROBE_LOCK = threading.Lock()
_PI_PROBE_CACHE = {"at": 0.0, "value": None}
_PI_CRED_CACHE = {"at": 0.0, "value": None}
# 冷探测要起 node 进程，实测 2–5 秒。缓存过期后先返回上一份目录，
# 由后台单飞刷新；探测本身串行，读缓存不被探测阻塞。
_PI_REFRESH_LOCK = threading.Lock()
_PI_BACKGROUND = {"probe": False, "credentials": False}
_PI_CATALOG_FILE = "pi-model-catalog.json"


def _pi_catalog_path():
    try:
        import deskcfg
        return os.path.join(deskcfg.DATA_HOME, "cache", _PI_CATALOG_FILE)
    except Exception:
        return ""


def _pi_catalog_load():
    """读上一份成功目录（只有公开模型元数据），供重启后首次切换立即可用。"""
    path = _pi_catalog_path()
    if not path or os.path.islink(path):
        return None
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
        at, value = float(data.get("at") or 0), data.get("value")
    except (OSError, TypeError, ValueError, AttributeError):
        return None
    if (not isinstance(value, dict) or value.get("unknown")
            or not isinstance(value.get("models"), list) or not value["models"]):
        return None
    return at, value


def _pi_catalog_save(at, value):
    if not isinstance(value, dict) or value.get("unknown") or not value.get("models"):
        return
    path = _pi_catalog_path()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        temp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"at": at, "value": value}, stream, ensure_ascii=False)
        os.replace(temp, path)
    except OSError:
        try:
            os.unlink(temp)
        except (OSError, UnboundLocalError):
            pass


def _pi_catalog_forget():
    path = _pi_catalog_path()
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _pi_background(kind, target):
    """同一类刷新同时只跑一个；失败只影响下次读取，不影响已返回的旧值。"""
    with _PI_PROBE_LOCK:
        if _PI_BACKGROUND[kind]:
            return
        _PI_BACKGROUND[kind] = True

    def run():
        try:
            target()
        except Exception as error:
            _pi_probe_log(f"后台刷新失败：{type(error).__name__}")
        finally:
            with _PI_PROBE_LOCK:
                _PI_BACKGROUND[kind] = False

    threading.Thread(target=run, name=f"pi-{kind}-refresh", daemon=True).start()


def invalidate_auth_probe_caches():
    """让首次设置页在用户完成登录后读取新状态；只清探测结果，不清凭证。"""
    from engine_adapters.claude import ClaudeAdapter
    with ClaudeAdapter._ENV_LOCK:
        ClaudeAdapter._ENV_CACHE.clear()
    with _PI_PROBE_LOCK:
        _PI_PROBE_CACHE.update(at=0.0, value=None)
        _PI_CRED_CACHE.update(at=0.0, value=None)
    # 登录变化后旧目录可能含已失效 provider，下一次必须同步重探。
    _pi_catalog_forget()


def _pi_env():
    try:
        env = secrets_env(os.environ)
    except Exception:
        env = dict(os.environ)
    return without_legacy_web_secrets(env)


_PI_PROBE_TOTAL_TIMEOUT = 12
_PI_PROBE_FIRST_TIMEOUT = 9


def _pi_probe_log(message):
    try:
        import deskcfg
        deskcfg.log("pi-models", message)
    except Exception:
        pass


def _pi_probe_once(cmd, frames, env, timeout):
    result = {"models": [], "current_model": "", "thinking": "medium", "unknown": False}
    try:
        proc = subprocess.run(cmd, input=frames, text=True, capture_output=True,
                              env=env, timeout=max(.1, timeout))
        for line in (proc.stdout or "").splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("type") != "response" or not obj.get("success"):
                continue
            data = obj.get("data") or {}
            if obj.get("command") == "get_state":
                model = data.get("model") or {}
                if model.get("provider") and model.get("id"):
                    result["current_model"] = f'{model["provider"]}/{model["id"]}'
                result["thinking"] = str(data.get("thinkingLevel") or "medium")
            elif obj.get("command") == "get_available_models":
                clean = []
                for model in data.get("models") or []:
                    if not isinstance(model, dict) or not model.get("provider") or not model.get("id"):
                        continue
                    # Match Pi getSupportedThinkingLevels: omitted standard
                    # levels inherit defaults; extended levels need an entry;
                    # explicit null disables any level, including off.
                    level_map = model.get("thinkingLevelMap") or {}
                    if model.get("reasoning"):
                        levels = [level for level in
                                  ("off", "minimal", "low", "medium", "high", "xhigh", "max")
                                  if (level not in level_map or level_map[level] is not None)
                                  and (level not in ("xhigh", "max") or level in level_map)]
                    else:
                        levels = ["off"]
                    clean.append({"id": f'{model["provider"]}/{model["id"]}',
                                  "provider": str(model["provider"]),
                                  "model": str(model["id"]),
                                  "name": str(model.get("name") or model["id"]),
                                  "reasoning": bool(model.get("reasoning")),
                                  "thinking_levels": levels,
                                  "input": [str(x) for x in (model.get("input") or [])],
                                  "context_window": int(model.get("contextWindow") or 0)})
                result["models"] = clean
        if proc.returncode not in (0, None) and not result["models"]:
            result["unknown"] = True
    except Exception:
        result["unknown"] = True
    return result


def _pi_probe(force=False):
    now = time.time()
    env = _pi_env()
    with _PI_PROBE_LOCK:
        cached = _PI_PROBE_CACHE.get("value")
        if cached is None and not force:
            # 进程重启后内存为空：用磁盘上的上一份成功目录，过期判定照常进行。
            disk = _pi_catalog_load()
            if disk:
                _PI_PROBE_CACHE.update(at=disk[0], value=disk[1])
                cached = disk[1]
        cached_at = _PI_PROBE_CACHE.get("at", 0)
    cache_ttl = 5 if isinstance(cached, dict) and cached.get("unknown") else 60
    cache_fresh = cached is not None and now - cached_at < cache_ttl
    if not force and cached is not None:
        if not cache_fresh:
            # 过期但可用：立即返回旧目录，后台刷新；已下线模型由发送时的真实错误暴露。
            _pi_background("probe", lambda: _pi_probe_refresh(env, now))
        return json.loads(json.dumps(cached))
    return _pi_probe_refresh(env, None if force else now)


def _pi_probe_refresh(env, since):
    """真实探测。since 非空时，若等锁期间别处已刷新过，直接复用该结果。"""
    with _PI_REFRESH_LOCK:
        with _PI_PROBE_LOCK:
            cached = _PI_PROBE_CACHE.get("value")
            if since is not None and cached is not None and _PI_PROBE_CACHE.get("at", 0) > since:
                return json.loads(json.dumps(cached))
        cmd = ["pi", "--mode", "rpc", "--no-session", "--offline", "--no-extensions",
               "--no-prompt-templates", "--no-themes", "--no-approve"]
        cmd += prompt_isolation_args()
        cmd += provider_extension_args()
        frames = "\n".join(json.dumps(x) for x in (
            {"type": "get_state"}, {"type": "get_available_models"})) + "\n"
        deadline = time.monotonic() + _PI_PROBE_TOTAL_TIMEOUT
        result = _pi_probe_once(cmd, frames, dict(env), min(
            _PI_PROBE_FIRST_TIMEOUT, max(.1, deadline - time.monotonic())))
        at = time.time()
        with _PI_PROBE_LOCK:
            _PI_PROBE_CACHE.update(at=at, value=result)
        _pi_catalog_save(at, result)
        return json.loads(json.dumps(result))


def _pi_auth_ready(provider, env):
    try:
        proc = subprocess.run(["pi", "auth", "check", "--provider", provider,
                               "--json", "--no-refresh"], capture_output=True, text=True,
                              env=env, timeout=8)
        obj = json.loads((proc.stdout or "").strip() or "{}")
        return obj.get("status") == "ready"
    except Exception:
        return None


def _pi_credentials():
    now = time.time()
    with _PI_PROBE_LOCK:
        cached = _PI_CRED_CACHE.get("value")
        at = _PI_CRED_CACHE.get("at", 0)
    if cached is not None:
        if now - at >= 60:
            # 各 provider 的 auth check 最长 8 秒；过期时先答旧状态，后台复核。
            _pi_background("credentials", lambda: _pi_credentials_refresh(now))
        value = json.loads(json.dumps(cached))
        if "models" in value:
            value["models"] = _pi_visible_model_ids(value["models"])
        return value
    return _pi_credentials_refresh(now)


def _pi_credentials_refresh(now):
    probe = _pi_probe()
    providers = []
    current_provider = (probe.get("current_model") or "").split("/", 1)[0]
    if current_provider:
        providers.append(current_provider)
    for model in probe.get("models") or []:
        provider = model.get("provider")
        if provider and provider not in providers:
            providers.append(provider)
    providers = providers[:8]
    ready, unknown = [], bool(probe.get("unknown"))
    if providers:
        from concurrent.futures import ThreadPoolExecutor
        env = _pi_env()
        with ThreadPoolExecutor(max_workers=min(4, len(providers))) as pool:
            checks = list(pool.map(lambda p: _pi_auth_ready(p, env), providers))
        ready = [provider for provider, ok in zip(providers, checks) if ok is True]
        unknown = unknown or all(ok is None for ok in checks)
    value = {"logged_in": bool(ready), "unknown": unknown,
             "providers": ready,
             "models": [model.get("id") for model in _pi_filter_codex_models(probe.get("models") or [])],
             "current_model": probe.get("current_model") or "",
             "thinking": probe.get("thinking") or "medium"}
    with _PI_PROBE_LOCK:
        _PI_CRED_CACHE.update(at=now, value=value)
    return json.loads(json.dumps(value))


def _pi_filter_codex_models(rows):
    """Restrict OpenAI menu entries to Codex's visible catalog, not its API catalog.

    Keep the raw Pi probe/cache intact and reapply the intersection on each read,
    so a removed Codex model cannot survive through the cached Pi catalog. Other
    providers and Pi's own model IDs/capabilities are unchanged. No model request
    is made; catalog() uses the same non-blocking metadata cache as Codex's menu.
    """
    providers = {"openai", "openai-codex"}
    if not any(row.get("provider") in providers for row in rows):
        return list(rows)
    try:
        from codex_appserver import catalog
        raw = catalog(binary="codex")
        allowed = {row.get("model") or row.get("id")
                   for row in (raw.get("models") or []) if isinstance(row, dict)
                   and not row.get("hidden") and not row.get("isHidden")}
        allowed = {model for model in allowed if isinstance(model, str) and model}
    except Exception:
        allowed = set()
    if not allowed:
        _pi_probe_log("Codex visible model catalog unavailable or empty; Pi OpenAI menu entries hidden")
    return [row for row in rows if row.get("provider") not in providers
            or (row.get("model") or str(row.get("id") or "").partition("/")[2]) in allowed]


def _pi_visible_model_ids(ids):
    rows = [{"id": model, "provider": str(model or "").partition("/")[0],
             "model": str(model or "").partition("/")[2]} for model in ids]
    return [row["id"] for row in _pi_filter_codex_models(rows)]


def _pi_model_catalog():
    return _pi_filter_codex_models(_pi_probe().get("models") or [])


def _pi_models():
    return [model.get("id") for model in _pi_model_catalog()]


def _pi_current_model():
    return _pi_probe().get("current_model") or ""


def _pi_thinking_level():
    return _pi_probe().get("thinking") or "medium"


def _pi_logged_in():
    return bool(_pi_credentials().get("logged_in"))


# 与前端 app-core.js 的 THEME_PRESET_IDS 一一对应。不在表里的值写入被丢弃、读取回落到默认,
# 所以新增主题必须两边同时加——"light" 只加了前端那一份时,开关点得动、界面也当场变了,
# 但写不进 state,一刷新或一打开设置页就掉回深色(2026-08-15)。
# 现在只有两个:默认深色与翻面的浅色。紫、绿、橙、玫红、灰那五套换色随配色选择器一起
# 退场——选择器早已不在界面上,常量与 CSS 却都留着(2026-08-16 清理)。
UI_THEME_PRESETS = {"cyan", "light"}
UI_THEME_DEFAULT = "cyan"


def _gateway_section(login_probe, key_name, base_name):
    """claude/codex 共形设置段:登录态 + API key 状态 + 网关 baseURL(key 经 SECRETS 注入 env)。"""
    sec = _secrets_read()
    k = sec.get(key_name, "")
    login_state = login_probe[1]()
    out = {login_probe[0]: login_state is True,
           "api_key_set": bool(k), "api_key": k, "api_key_mask": _mask_key(k),
           "base_url": sec.get(base_name, "")}
    if login_state is None:
        out["unknown"] = True
    return out


def _ui_settings_section():
    """UI 外观设置:只存本机运行态,不改发行版 workbench.json。"""
    ui = _wb_state_read().get("ui") or {}
    theme = ui.get("theme") if isinstance(ui, dict) else None
    if theme not in UI_THEME_PRESETS:
        theme = UI_THEME_DEFAULT
    return {"theme": theme,
            "log_overlay": bool(isinstance(ui, dict) and ui.get("log_overlay"))}


def _settings_get():
    """各引擎设置段 = spec.credentials()(逐引擎探测在 engines/<key>.py,委托本模块的 helper)。
    并行探测:各引擎的登录探测里有子进程,串行首访 2s+;线程池并行后取最慢一家。"""
    from engines import ENGINES
    from concurrent.futures import ThreadPoolExecutor
    out = {"ui": _ui_settings_section()}
    items = list(ENGINES.items())
    ex = ThreadPoolExecutor(max_workers=max(1, len(items)))
    try:
        futs = {k: ex.submit(sp.credentials) for k, sp in items}
        # 12 秒是整趟的期限,不是每家的。逐个写 result(timeout=12) 时四家串起来最坏 48 秒,
        # 而前端 15 秒就超时了——界面报"读取失败",后端还在跑。这里换成共享截止时间。
        deadline = time.monotonic() + 12
        for k, f in futs.items():
            try:
                out[k] = f.result(timeout=max(0.1, deadline - time.monotonic()))
            except Exception:
                # 探测没跑完 ≠ 没登录。调用方(provision/setup.py)按 unknown 区分这两档,
                # 空段会被读成"未登录"并配一颗去登录的钮,比不报更坏。
                out[k] = {"unknown": True}
    finally:
        # 不能用 with:它退出时 shutdown(wait=True),会一直等到最慢的探测跑完,上面那个
        # 期限等于白设——某家 CLI 卡住时整个接口跟着挂着,新装的机器上尤其容易撞上。
        # wait=False 只是不等,已经起来的探测线程仍会自己跑完并退出。
        ex.shutdown(wait=False)
    return out


def _settings_apply(body):
    # UI 外观:预设主题色 / 日志悬浮窗开关 → workbench state,立即供前端下次启动/设置页读取。
    if isinstance(body.get("ui"), dict):
        theme = body["ui"].get("theme")
        up = {}
        if theme in UI_THEME_PRESETS:
            up["theme"] = theme
        if "log_overlay" in body["ui"]:
            up["log_overlay"] = bool(body["ui"]["log_overlay"])
        if up:
            _wb_state_patch({"ui": up})
    # claude:key 与网关 → SECRETS(适配器每回合注入 env,即时生效)
    if isinstance(body.get("claude"), dict):
        c = body["claude"]
        up = {}
        if "api_key" in c:
            up["ANTHROPIC_API_KEY"] = c["api_key"] or None
        if "base_url" in c:
            up["ANTHROPIC_BASE_URL"] = c["base_url"] or None
        if up:
            _secrets_write(up)
    # codex:OPENAI_API_KEY / 网关 → SECRETS(同 claude,env 注入,下回合生效)
    if isinstance(body.get("codex"), dict):
        c = body["codex"]
        up = {}
        if "api_key" in c:
            up["OPENAI_API_KEY"] = c["api_key"] or None
        if "base_url" in c:
            up["OPENAI_BASE_URL"] = c["base_url"] or None
        if up:
            _secrets_write(up)
    # 交互式登录:claude/codex 是 OAuth,代开 Terminal 走浏览器
    if body.get("login") in ("claude", "codex"):
        cmd = {"claude": "claude auth login", "codex": "codex login"}[body["login"]]
        try:
            import oscompat
            oscompat.open_system_terminal(cmd)
        except Exception:
            pass
    return {"ok": True}
