"""引擎生命周期:GUI 适配器的按需实例化、事件总线、随壳退出的收尾。

依赖 deskcfg(端口/路径)与 engine_adapters(各引擎适配器与事件总线)。单向向下,
不 import ui_server。side-effect(atexit/signal 注册、parent watchdog 启动)留 ui_server,
本模块只给定义,import 即无副作用。

GUI 侧走各引擎自己的 CLI 子进程或 app-server,不持有独立的反代引擎服务。
"""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import os, time
from concurrent.futures import ThreadPoolExecutor, wait

from deskcfg import REPO_ROOT, DATA_HOME
import engine_adapters

_bus = engine_adapters.EventBus()
_adapters = {}
_shepherd_adapter = None
_shepherd_trial_adapter = None
_cleanup_started = False
_adapters_lock = __import__("threading").RLock()


# 有 GUI 适配器的引擎。加引擎时只改这里与 _adapter 的表——别处一律遍历它,
# 免得又出现「某处枚举漏了一个,那个引擎的 GUI 回合永远显示空闲」这类分散的漏网。
GUI_ENGINES = ("claude", "codex", "pi")


def _shepherd():
    """Internal Conversation facade; deliberately absent from GUI_ENGINES."""
    global _shepherd_adapter
    with _adapters_lock:
        if _shepherd_adapter is None:
            if _cleanup_started:
                raise RuntimeError('Desk 正在退出，不能启动 Shepherd')
            _shepherd_adapter = engine_adapters.ShepherdAdapter(DATA_HOME, REPO_ROOT, _bus)
        return _shepherd_adapter


def _shepherd_trial():
    global _shepherd_trial_adapter
    with _adapters_lock:
        if _shepherd_trial_adapter is None:
            if _cleanup_started:
                raise RuntimeError('Desk 正在退出，不能启动 Shepherd')
            _shepherd_trial_adapter = engine_adapters.ShepherdTrialAdapter(DATA_HOME, REPO_ROOT, _bus)
        return _shepherd_trial_adapter


def _adapter(name):
    with _adapters_lock:   # ThreadingTCPServer 下防止双实例(索引互相覆盖)
        if name not in _adapters:
            if _cleanup_started:
                raise RuntimeError('Desk 正在退出，不能启动引擎')
            cls = {"claude": engine_adapters.ClaudeAdapter,
                   "codex": engine_adapters.CodexAdapter,
                   "pi": engine_adapters.PiAdapter}[name]
            adapter = cls(DATA_HOME, REPO_ROOT, _bus)
            _adapters[name] = adapter
            if hasattr(adapter, "resume_pending_recalls"):
                adapter.resume_pending_recalls()
        return _adapters[name]


def _cleanup(*_, reason="exit", deadline=None):
    global _cleanup_started
    if _cleanup_started:
        return
    _cleanup_started = True
    import lifecycle
    lifecycle.seal_for_exit()
    # PTY 与安排的一次性 CLI 都是 ui_server 的直接子进程，退出时一并带走，不留孤儿。
    # _cleanup 走正常退出路径(SIGTERM/shell 关闭/父进程看护/atexit)。
    # Seal admission on every existing adapter before waiting for any one of
    # them. A queued request must not start while another engine is draining.
    with _adapters_lock:
        adapters = list(_adapters.values())
    for adapter in adapters:
        for ctx in list(adapter._live.values()):
            ctx['_shutdown_reason'] = 'desk_exit'
        if not callable(getattr(adapter, 'shutdown', None)):
            adapter._shutting_down = True
        # Seal hidden peer work at the same admission boundary, not later in
        # atexit after other adapters have drained.  Reply commit rechecks both
        # this bit and the cancellation token under the adapter lock.
        try:
            adapter._peer_cancel_all()
        except Exception:
            pass
    targets = [adapter for adapter in (*adapters, _shepherd_adapter, _shepherd_trial_adapter)
               if adapter is not None]
    deadline = deadline or (time.monotonic() + 3.5)
    def stop_adapter(adapter):
        try:
            shutdown = getattr(adapter, 'shutdown', None)
            if callable(shutdown):
                shutdown(timeout=max(0, deadline - time.monotonic()))
        except TypeError:
            # Older adapters have no timeout argument; admission was already sealed.
            try: shutdown()
            except Exception: pass
        except Exception:
            import logging
            logging.exception('Engine shutdown incomplete; durable recovery required')
    if targets:
        pool = ThreadPoolExecutor(max_workers=len(targets), thread_name_prefix='desk-shutdown')
        futures = [pool.submit(stop_adapter, adapter) for adapter in targets]
        wait(futures, timeout=max(0, deadline - time.monotonic()))
        pool.shutdown(wait=False, cancel_futures=True)
    try:
        from shepherd.unified_archive import _ArchiveConnections
        _ArchiveConnections.close_all()
    except Exception:
        pass
    try:
        import termpty
        termpty.kill_all()
    except Exception:
        pass
    try:
        from larkbot import runner
        runner.cancel_all()
    except Exception:
        pass


def _parent_death_watchdog():
    """Reject an already orphaned native backend, then watch the exact host parent."""
    parent = os.getppid()
    if os.environ.get("DESKTRON_NATIVE_LAUNCH") == "1":
        try:
            expected = int(os.environ.get("DESKTRON_NATIVE_HOST_PID") or 0)
        except ValueError:
            expected = 0
        if expected <= 1 or parent != expected:
            raise RuntimeError("原生宿主已退出，拒绝启动孤儿后端")
    def parent_gone():
        # 壳已死(macOS 被 launchd 收养;Windows 父进程句柄已触发)。
        # Use the same main-thread SIGTERM path as an explicit quit.
        # A second cleanup/os._exit here would cut off the first
        # cleanup's grace period while its cancel watchdogs are active.
        if not _cleanup_started:
            oscompat.request_self_shutdown()
    if oscompat.IS_WINDOWS:
        # Windows 没有跨进程 SIGTERM。宿主以命名事件请求正常退出，同样走主线程收尾。
        oscompat.watch_stop_event(os.environ.get("DESKTRON_HOST_STOP_EVENT", ""), parent_gone)
    oscompat.watch_parent(parent, parent_gone)


def refresh_auth_notices(engine_name, provider=''):
    """Recheck a specific login source. Missing/unknown probes never clear errors."""
    import subprocess
    import settings
    started = int(time.time() * 1000)
    provider = provider or engine_name
    settings.invalidate_auth_probe_caches()
    ready = None
    if engine_name == 'pi' and provider == 'pi':
        # Pi's login screen can manage several Providers. Recheck only providers
        # with outstanding facts; never equate “any provider ready” with all ready.
        with _adapters_lock:
            targets = list(_adapters.values()) + [_shepherd_adapter, _shepherd_trial_adapter]
        providers = set()
        for ad in targets:
            if ad is not None:
                with ad._lock:
                    for row in ad.index.values():
                        for issue in (row.get('environment_issues') or {}).values():
                            if issue.get('stage') == 'auth' and issue.get('engine') == 'pi':
                                value = issue.get('provider')
                                if value and value != 'pi':
                                    providers.add(value)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda p: refresh_auth_notices('pi', p), sorted(providers)[:8]))
        return {'ok':bool(results) and all(r['ok'] for r in results), 'results':results}
    if engine_name == 'claude' and provider == 'claude':
        import claude_auth
        # A subscription probe says nothing about an API-key/gateway failure.
        env = settings.secrets_env(os.environ)
        ready = None if env.get('ANTHROPIC_API_KEY') or env.get('ANTHROPIC_AUTH_TOKEN') else claude_auth.subscription_status()
    elif engine_name == 'codex' and provider == 'codex':
        try:
            proc = subprocess.run(['codex', 'login', 'status'], capture_output=True,
                                  text=True, timeout=8, env=settings.secrets_env(os.environ))
            ready = proc.returncode == 0
        except Exception:
            pass
    elif engine_name == 'pi' and provider and provider != 'pi':
        ready = settings._pi_auth_ready(provider, settings._pi_env())
    if ready is True:
        with _adapters_lock:
            targets = list(_adapters.values()) + [_shepherd_adapter, _shepherd_trial_adapter]
        for ad in targets:
            if ad is not None:
                ad.resolve_auth({'engine':engine_name, 'provider':provider}, started)
    return {'ok':ready is True, 'status':'ready' if ready is True else
            'unavailable' if ready is False else 'unknown', 'engine':engine_name, 'provider':provider}
