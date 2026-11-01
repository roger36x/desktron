"""Desktron 自更新：后台检查、点击下载、确认安装；兼容旧版强制更新暂存包。

本模块不覆盖正在运行的产品目录。它只管理外部状态与暂存包；真正的目录切换由
self_update_apply.py 在脱离 Desk 进程树后完成。开发副本不自动下载发行包；开发壳仅可通过显式的宿主事务激活。
"""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import datetime
import contextlib
import hashlib
import json
import os
import platform
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request


CHECK_INTERVAL_S = 6 * 60 * 60
FIRST_CHECK_DELAY_S = 5 * 60
QUIET_AFTER_S = 10 * 60
QUIET_CONFIRM_S = 2 * 60
REQUEST_TIMEOUT_S = 10
APPLY_DRAIN_STALE_S = 3 * 60
_LOCK = threading.RLock()
_CHECKING = False
_STARTED = False
_LAST_ACTIVITY = time.time()
_QUIET_SINCE = 0.0
_DRAINING = False
_ACTIVE_WORK = 0
_ACTIVE_SWITCH_STATES = ("applying", "switching", "verifying", "accepted_pending_commit")


def _base_dir():
    try:
        from deskcfg import DATA_HOME
        return os.path.dirname(DATA_HOME)
    except Exception:
        return os.path.expanduser("~/.local/share/desktron")


def _updates_dir():
    return os.path.join(_base_dir(), "updates")


def _state_path():
    return os.path.join(_updates_dir(), "state.json")


def _payload_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path, value):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    oscompat.fsync_directory(os.path.dirname(path) or ".")


def _load_state():
    try:
        with open(_state_path(), encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_state(value):
    value = dict(value or {})
    value["updated_at"] = int(time.time())
    _atomic_json(_state_path(), value)
    return value


def _current():
    base = _base_dir()
    rev = _read_text(os.path.join(base, ".update-rev"))
    build = _read_text(os.path.join(base, ".update-build"))
    try:
        build = int(build or 0)
    except ValueError:
        build = 0
    # REPO_ROOT 是用户工作流目录，不是 Desk 源码目录；用当前模块所在 payload 向上找
    # .git 才能区分 checkout 直跑与发行安装。工作流自己是 Git 仓库不能误判为开发态。
    dev = os.path.isdir(os.path.join(_payload_root(), ".git"))
    return rev, build, bool(dev or rev.startswith("payload:"))


def _channel_base():
    """发布通道地址:环境变量优先,其次 manifest 的 update_base。空串表示未配置通道。"""
    override = os.environ.get("DESKTRON_UPDATE_BASE", "").strip()
    if override:
        return override.rstrip("/")
    try:
        text = _read_text(os.path.join(_payload_root(), "manifest.yaml"))
        m = re.search(r'(?m)^update_base:\s*["\']?([^"\'\s#]+)', text)
        if m:
            return m.group(1).rstrip("/")
    except Exception:
        pass
    return ""


def _iso_time(value):
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def _manifest(raw, base_url=None, now=None):
    if not isinstance(raw, dict):
        raise ValueError("发布清单不是 JSON 对象")
    if raw.get("signed") is not True:
        raise ValueError("发布清单未声明稳定签名，拒绝更新")
    rev = str(raw.get("rev") or "").strip()
    build = raw.get("build_number")
    if not re.fullmatch(r"[0-9a-f]{7,64}", rev, re.I):
        raise ValueError("发布清单缺少有效 rev")
    try:
        build = int(build)
    except (TypeError, ValueError):
        raise ValueError("发布清单缺少有效 build_number")
    if build <= 0:
        raise ValueError("build_number 必须大于 0")
    expires_raw = str(raw.get("expires_at") or "").strip()
    expires = _iso_time(expires_raw)
    if expires_raw and expires is None:
        raise ValueError("expires_at 不是有效时间")
    if expires is not None and (now or time.time()) >= expires:
        raise ValueError("更新通道已过期（%s）" % raw.get("expires_at"))
    arch = platform.machine().lower()
    if arch in ("aarch64", "arm64"):
        arch = "arm64"
    elif arch in ("amd64", "x86_64"):
        arch = "x86_64"
    artifact = (raw.get("artifacts") or {}).get(arch)
    if not isinstance(artifact, dict):
        raise ValueError("当前机器架构没有发布包：%s" % arch)
    file_name = str(artifact.get("file") or "").strip()
    sha = str(artifact.get("sha256") or "").strip().lower()
    try:
        size = int(artifact.get("bytes") or 0)
    except (TypeError, ValueError):
        size = 0
    if not file_name or not re.fullmatch(r"[0-9a-f]{64}", sha) or size <= 0:
        raise ValueError("发布包描述不完整")
    origin = (base_url or _channel_base()).rstrip("/") + "/"
    url = urllib.parse.urljoin(origin, file_name)
    parsed_url, parsed_origin = urllib.parse.urlparse(url), urllib.parse.urlparse(origin)
    if (parsed_url.scheme, parsed_url.netloc) != (parsed_origin.scheme, parsed_origin.netloc):
        raise ValueError("发布包不能跳转到其他来源")
    mandatory_raw = str(raw.get("mandatory_after") or "").strip()
    mandatory_at = _iso_time(mandatory_raw)
    if mandatory_raw and mandatory_at is None:
        raise ValueError("mandatory_after 不是有效时间")
    try:
        min_build = int(raw.get("min_supported_build") or 0)
    except (TypeError, ValueError):
        raise ValueError("min_supported_build 无效")
    return {
        "rev": rev.lower(), "build_number": build, "arch": arch,
        "file": file_name, "url": url, "sha256": sha, "bytes": size,
        "channel": str(raw.get("channel") or ""),
        "built_at": str(raw.get("built_at") or ""),
        "expires_at": str(raw.get("expires_at") or ""),
        "mandatory_after": mandatory_raw,
        "mandatory_at": mandatory_at or 0,
        "min_supported_build": max(0, min_build),
        "reason": str(raw.get("reason") or "")[:500],
        "signed": bool(raw.get("signed")),
    }


def _download(info, state):
    key = "%d-%s" % (info["build_number"], info["rev"][:12])
    folder = os.path.join(_updates_dir(), key)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    final = os.path.join(folder, "desktron.tgz")
    part = final + ".part"
    state.update(state="downloading", progress=0, staged_rev="", staged_build=0)
    _save_state(state)
    got = 0
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(info["url"], timeout=REQUEST_TIMEOUT_S) as response, \
                open(part, "wb") as out:
            actual = urllib.parse.urlparse(response.geturl())
            expected = urllib.parse.urlparse(info["url"])
            if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
                raise ValueError("发布包重定向到了其他来源")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk); digest.update(chunk); got += len(chunk)
                if got > info["bytes"]:
                    raise ValueError("下载大小超过发布清单，已丢弃")
                progress = min(99, int(got * 100 / info["bytes"]))
                if progress >= int(state.get("progress") or 0) + 5:
                    state["progress"] = progress; _save_state(state)
        if got != info["bytes"]:
            raise ValueError("下载大小不符：期望 %d，实际 %d" % (info["bytes"], got))
        if digest.hexdigest() != info["sha256"]:
            raise ValueError("下载校验失败，已丢弃")
        os.replace(part, final)
    except Exception:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    state.update(state="staged", progress=100, staged_rev=info["rev"],
                 staged_build=info["build_number"], archive=final, sha256=info["sha256"])
    return _save_state(state)


def _check_claimed(download=False):
    """检查不下载；仅显式下载请求才暂存经过校验的发行包。"""
    global _CHECKING
    try:
        base = _channel_base()
        if not base:
            state = _load_state()
            state.update(state="idle", hasUpdate=False, progress=0, reachable=False, unknown=True,
                         checked_at=int(time.time()), error="未配置更新通道",
                         fix="设置 DESKTRON_UPDATE_BASE 或 manifest.yaml 的 update_base")
            return _save_state(state)
        with urllib.request.urlopen(base + "/release.json", timeout=REQUEST_TIMEOUT_S) as response:
            raw = json.loads(response.read().decode("utf-8"))
        info = _manifest(raw, base)
        current_rev, current_build, dev = _current()
        state = _reconcile_pending_commit(_load_state(), current_rev, current_build,
                                          str(os.environ.get("DESKTRON_HOST_REV") or "").strip(),
                                          _safe_int(os.environ.get("DESKTRON_HOST_BUILD")))
        if state.get("state") in _ACTIVE_SWITCH_STATES or state.get("acceptance_unknown"):
            return state
        state.update(current_rev=current_rev, current_build=current_build, latest_rev=info["rev"],
                     latest_build=info["build_number"], checked_at=int(time.time()),
                     reachable=True, unknown=False, channel=info["channel"], error="", fix="",
                     mandatory_after=info["mandatory_after"],
                     min_supported_build=info["min_supported_build"], reason=info["reason"],
                     signed=info["signed"])
        # build_number 是唯一的发布顺序；rev 只校验包与清单是否属于同一份源码。
        newer = info["build_number"] > current_build
        if dev:
            state.update(state="dev", hasUpdate=newer, progress=0)
            return _save_state(state)
        if not newer:
            state.update(state="idle", hasUpdate=False, progress=0, staged_rev="", staged_build=0)
            return _save_state(state)
        state["hasUpdate"] = True
        if download:
            state["manual_install"] = True
        state["mandatory"] = bool(
            (info["mandatory_at"] and time.time() >= info["mandatory_at"]) or
            (info["min_supported_build"] and current_build < info["min_supported_build"]))
        if (state.get("staged_rev") == info["rev"] and
                state.get("staged_build") == info["build_number"] and
                os.path.isfile(state.get("archive") or "")):
            state["state"] = "staged"
            return _save_state(state)
        if not download:
            state.update(state="available", progress=0, staged_rev="", staged_build=0)
            return _save_state(state)
        return _download(info, state)
    except Exception as exc:
        state = _load_state()
        if state.get("state") in _ACTIVE_SWITCH_STATES or state.get("acceptance_unknown"):
            return state
        staged = (state.get("state") == "staged" and
                  os.path.isfile(state.get("archive") or ""))
        state.update(state="staged" if staged else "error", checked_at=int(time.time()),
                     reachable=False, error=str(exc)[:500],
                     fix="稍后重试；连续失败请检查内网与发布通道")
        return _save_state(state)
    finally:
        with _LOCK:
            _CHECKING = False


def check_now(download=False):
    """同步检查；下载必须显式指定，开发副本仍不可下载。"""
    global _CHECKING
    with _LOCK:
        if _CHECKING:
            return status()
        _CHECKING = True
    return _check_claimed(download=download)


def trigger_check(download=False):
    global _CHECKING
    with _LOCK:
        if download and _current()[2]:
            return {"ok": False, "error": "当前为源码同步版，请通过源码同步更新，避免覆盖本地改动"}
        if _CHECKING:
            return status()
        _CHECKING = True
    def run():
        _check_claimed(download=download)
    threading.Thread(target=run, daemon=True, name="desk-update-check").start()
    value = status(); value["checking"] = True
    return value


def note_activity():
    global _LAST_ACTIVITY, _QUIET_SINCE
    with _LOCK:
        _LAST_ACTIVITY = time.time()
        _QUIET_SINCE = 0.0
    return {"ok": True}


def set_external_drain(enabled, lease_s=0):
    """Compatibility shim: process shutdown now uses the global lifecycle gate."""
    import lifecycle
    if enabled:
        lifecycle.seal_for_exit()
        return True
    lifecycle.reopen_legacy_gate()
    return not lifecycle.admitted()



def external_draining():
    import lifecycle
    return not lifecycle.admitted()


def active_work_count():
    """Return already-admitted work that must settle after an external fence."""
    with _LOCK:
        return int(_ACTIVE_WORK)


@contextlib.contextmanager
def admit_work(user_activity=False):
    """把新工作登记与更新封闸放在同一临界区，消除静默复核后的启动竞态。"""
    import lifecycle
    global _LAST_ACTIVITY, _QUIET_SINCE
    lifecycle_admitted = False
    with _LOCK:
        saved = _load_reconciled_state()
        switching = saved.get("state") in _ACTIVE_SWITCH_STATES
        uncertain = bool(saved.get("acceptance_unknown"))
        recent_switch = time.time() - _safe_int(saved.get("updated_at")) < APPLY_DRAIN_STALE_S
        updater_alive = _pid_alive(_safe_int(saved.get("apply_pid")))
        if (_DRAINING or not lifecycle.admitted()
                or uncertain or (switching and (updater_alive or recent_switch))):
            yield False
            return
        if not lifecycle.begin(critical="admission"):
            yield False
            return
        lifecycle_admitted = True
        try:
            if user_activity:
                _LAST_ACTIVITY = time.time()
                _QUIET_SINCE = 0.0
            yield True
        finally:
            if lifecycle_admitted:
                lifecycle.finish(critical="admission")


def begin_work(user_activity=False, *, restart_blocking=True, critical=""):
    """在启动异步线程前登记；默认后台工作对手动重启是不可中断事务。"""
    import lifecycle
    critical = critical or ("background_work" if restart_blocking else "")
    global _ACTIVE_WORK, _LAST_ACTIVITY, _QUIET_SINCE
    with _LOCK:
        saved = _load_reconciled_state()
        switching = saved.get("state") in _ACTIVE_SWITCH_STATES
        uncertain = bool(saved.get("acceptance_unknown"))
        recent_switch = time.time() - _safe_int(saved.get("updated_at")) < APPLY_DRAIN_STALE_S
        updater_alive = _pid_alive(_safe_int(saved.get("apply_pid")))
        if (_DRAINING or not lifecycle.admitted()
                or uncertain or (switching and (updater_alive or recent_switch))):
            return False
        if not lifecycle.begin(critical=critical):
            return False
        if user_activity:
            _LAST_ACTIVITY = time.time()
            _QUIET_SINCE = 0.0
        _ACTIVE_WORK += 1
        return True


def finish_work(*, restart_blocking=True, critical=""):
    import lifecycle
    critical = critical or ("background_work" if restart_blocking else "")
    global _ACTIVE_WORK
    with _LOCK:
        _ACTIVE_WORK = max(0, _ACTIVE_WORK - 1)
        lifecycle.finish(critical=critical)


@contextlib.contextmanager
def tracked_work(user_activity=False, *, critical="", restart_blocking=True):
    """登记后台任务；可恢复维护可显式不阻断手动重启。"""
    admitted = begin_work(user_activity=user_activity, critical=critical,
                          restart_blocking=restart_blocking)
    if not admitted:
        yield False
        return
    try:
        yield True
    finally:
        finish_work(critical=critical, restart_blocking=restart_blocking)


def _work_is_quiet():
    """只在所有已知工作面都明确空闲时返回真；任何探测异常都按不空闲处理。"""
    try:
        with _LOCK:
            if _ACTIVE_WORK:
                return False
        import term
        tools = term._term_list()
        if tools:
            return False  # 活工具终端可能在静默执行长任务，不能仅凭最近无输出判为空闲。
        rows = term._term_persisted()
        if any(r.get("busy") or int(r.get("agents") or 0) > 0 for r in rows):
            return False
        # 缓存可能尚未填充，直接问仍持有进程的 GUI 会话，宁可晚更也不误杀。
        for eng, sid in term._agents_targets(rows):
            if int(term._agents_adapter(eng).running_agents(sid) or 0) > 0:
                return False
        # 主回合结束后，隔离 Recap 仍可能在使用模型并准备落 journal。它不在终端表里，
        # 直接读适配器的权威运行集合；等待中的 debounce 计时器不算运行。
        try:
            from engine import _adapters, _adapters_lock
            with _adapters_lock:
                adapters = list(_adapters.values())
            if any(bool(getattr(ad, "_recap_runs", {}))
                   or bool(getattr(ad, "_peer_runs", {})) for ad in adapters):
                return False
        except Exception:
            return False
        from app_control.runtime import runtime
        if runtime().pending_all(1):
            return False
        from arrangement import scheduler
        if scheduler.has_running():
            return False
        return True
    except Exception:
        return False


def quiet_ready(now=None):
    global _QUIET_SINCE
    now = now or time.time()
    with _LOCK:
        if now - _LAST_ACTIVITY < QUIET_AFTER_S:
            _QUIET_SINCE = 0.0; return False
    if not _work_is_quiet():
        with _LOCK:
            _QUIET_SINCE = 0.0
        return False
    with _LOCK:
        if not _QUIET_SINCE:
            _QUIET_SINCE = now
            return False
        return now - _QUIET_SINCE >= QUIET_CONFIRM_S


def request_development_app(stage_name, confirm):
    """Activate a staged development host through the native update transaction."""
    if sys.platform != "darwin":
        return {"ok": False, "error": "开发壳激活仅支持 macOS"}
    current_rev, current_build, dev = _current()
    if not dev:
        return {"ok": False, "error": "发行安装请使用设置页更新"}
    import runtime_identity
    if runtime_identity.facts().get("supervisor") != "native":
        return {"ok": False, "error": "当前未由原生宿主监督，不能安全激活开发壳"}
    if not isinstance(stage_name, str) or not re.fullmatch(r"dev-host\.[A-Za-z0-9_-]{6,64}", stage_name):
        return {"ok": False, "error": "开发壳暂存标识无效"}
    updates = os.path.realpath(_updates_dir())
    stage = os.path.realpath(os.path.join(updates, stage_name))
    if os.path.dirname(stage) != updates:
        return {"ok": False, "error": "开发壳暂存路径无效"}
    app = os.path.join(stage, "Desktron.app")
    binary = os.path.join(app, "Contents", "MacOS", "desktron")
    host_rev = _read_text(os.path.join(app, "Contents", "Resources", "release-rev"))
    host_build = _safe_int(_read_text(os.path.join(app, "Contents", "Resources", "release-build")))
    if (not os.path.isfile(binary) or not host_rev or host_rev != str(confirm or "")
            or host_build <= 0):
        return {"ok": False, "error": "开发壳暂存内容、版本或构建号不一致"}
    try:
        from macos_codesign import verify_stable_app
        verify_stable_app(app)
    except Exception as error:
        return {"ok": False, "error": "开发壳稳定签名无效：" + str(error)[:240]}
    with _LOCK:
        active = _load_reconciled_state()
        if (active.get("state") in _ACTIVE_SWITCH_STATES or active.get("acceptance_unknown")
                or _CHECKING):
            return {"ok": False, "error": "已有版本切换正在执行"}
        state = dict(active)
        state.update(state="staged", mode="host-only", staged_app=stage,
                     staged_rev=current_rev, staged_build=current_build,
                     staged_host_rev=host_rev, staged_host_build=host_build,
                     staged_host_sha256=_sha256(binary), hasUpdate=False,
                     error="", fix="", acceptance_unknown=False)
        _save_state(state)
    return _request_native_apply(state, automatic=False, mandatory=False)


def request_apply(confirm, automatic=False):
    global _DRAINING
    native_request = None
    with _LOCK:
        if _CHECKING:
            return {"ok": False, "error": "更新检查尚未完成，请稍后重试"}
        state = _load_reconciled_state()
        if _current()[2]:
            return {"ok": False, "error": "开发副本不支持自动安装，请通过源码同步更新"}
        if state.get("acceptance_unknown"):
            return {"ok": False, "error": "上次更新事务未确认，禁止覆盖或重试"}
        if state.get("state") != "staged":
            return {"ok": False, "error": "没有已准备好的更新"}
        if str(confirm or "") != str(state.get("staged_rev") or ""):
            return {"ok": False, "error": "界面版本与待安装版本不一致，请重新检查"}
        current_build = _current()[1]
        mandatory_at = _iso_time(state.get("mandatory_after")) or 0
        min_build = _safe_int(state.get("min_supported_build"))
        mandatory = bool((mandatory_at and time.time() >= mandatory_at) or
                         (min_build > 0 and current_build < min_build))
        if automatic and (not mandatory or not quiet_ready()):
            return {"ok": False, "error": "尚未进入安全静默窗口"}
        if automatic and state.get("manual_install"):
            return {"ok": False, "error": "手动下载的更新等待用户确认安装"}
        if sys.platform != "darwin":
            return {"ok": False, "error": "当前仅支持桌面端应用更新"}
        import runtime_identity
        if runtime_identity.facts().get("supervisor") == "native":
            native_request = (dict(state), mandatory)
        else:
            import lifecycle
            update_lock = lifecycle.try_update_lock()
            if update_lock is None:
                return {"ok": False, "error": "Desk 正在重启或执行另一项版本切换"}
            source_script = os.path.join(os.path.dirname(__file__), "self_update_apply.py")
            script = os.path.join(_updates_dir(), "self_update_apply.py")
            script_tmp = script + ".tmp"
            log_path = os.path.join(_updates_dir(), "apply.log")
            _DRAINING = True
            operation_id = secrets.token_hex(16)
            state.update(state="applying", automatic=bool(automatic), mandatory=mandatory,
                         operation_id=operation_id, apply_pid=0, error="", fix="",
                         completion_id="", completion_outcome="", completed_at=0)
            _save_state(state)
            try:
                with open(source_script, "rb") as src, open(script_tmp, "wb") as dst:
                    dst.write(src.read())
                os.chmod(script_tmp, 0o700)
                os.replace(script_tmp, script)
                log = open(log_path, "w", encoding="utf-8")
                proc = subprocess.Popen(
                    [sys.executable, script, "--state", _state_path()],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True, pass_fds=(update_lock,))
                log.close()
                # flock is inherited by the updater through pass_fds.  The parent
                # closes its duplicate without LOCK_UN; the lock remains until the
                # updater exits, including switching/verifying/rollback.
                os.close(update_lock)
                update_lock = None
            except Exception as exc:
                lifecycle.release_update_lock(update_lock)
                _DRAINING = False
                try:
                    os.remove(script_tmp)
                except OSError:
                    pass
                state.update(state="staged", operation_id="",
                             error="无法启动独立更新器：" + str(exc)[:300])
                _save_state(state)
                return {"ok": False, "error": state["error"]}
            threading.Thread(target=_watch_apply, args=(proc,), daemon=True,
                             name="desk-update-apply-watch").start()
            return {"ok": True, "pid": proc.pid, "operation_id": operation_id,
                    "rev": state.get("staged_rev")}
    # prepare_restart may wait for short critical sections whose completion also
    # enters _LOCK. Never hold the updater lock across that lifecycle boundary.
    state, mandatory = native_request
    return _request_native_apply(state, automatic=automatic, mandatory=mandatory)


def _staged_identity(state):
    """Immutable fields that identify exactly one staged update transaction."""
    return tuple(str(state.get(key) or "") for key in (
        "mode", "staged_rev", "staged_build", "staged_app", "staged_host_rev",
        "staged_host_build", "staged_host_sha256", "archive", "sha256"))


def _request_native_apply(state, *, automatic, mandatory):
    """Stage a fixed helper, then transfer update ownership to the native host."""
    global _DRAINING
    import lifecycle
    import runtime_identity
    operation_id = secrets.token_hex(16)
    source_script = os.path.join(os.path.dirname(__file__), "self_update_apply.py")
    helper_digest = _sha256(source_script)
    identity = runtime_identity.facts()
    required_identity = {key: str(identity.get(key) or "") for key in
                         ("boot_id", "host_instance_id", "launch_generation")}
    previous_payload_rev, previous_payload_build, _ = _current()
    previous_host_rev = str(os.environ.get("DESKTRON_HOST_REV") or "")
    previous_host_build = _safe_int(os.environ.get("DESKTRON_HOST_BUILD"))
    if not all(required_identity.values()) or not previous_host_rev or not previous_host_build:
        return {"ok": False, "state": "rejected",
                "error": "当前原生宿主缺少可验证的版本、构建号或生命周期身份，拒绝切换"}
    expected_stage = _staged_identity(state)
    prepared = lifecycle.prepare_restart(operation_id, required_identity["boot_id"])
    if not prepared.get("ok"):
        return {"ok": False, "state": "rejected",
                "error": prepared.get("error") or "仍有不可中断操作",
                "blockers": prepared.get("blockers") or []}
    with _LOCK:
        current = _load_state()
        if (current.get("state") != "staged" or current.get("acceptance_unknown")
                or _staged_identity(current) != expected_stage):
            lifecycle.reopen(operation_id)
            return {"ok": False, "state": "rejected", "error": "待安装版本状态已经变化"}
        state = current
        _DRAINING = True
        state.update(state="applying", automatic=bool(automatic), mandatory=mandatory,
                     operation_id=operation_id, apply_pid=0, error="", fix="",
                     completion_id="", completion_outcome="", completed_at=0,
                     helper_contract=2, helper_sha256=helper_digest,
                     previous_boot_id=required_identity["boot_id"],
                     previous_host_instance_id=required_identity["host_instance_id"],
                     previous_launch_generation=required_identity["launch_generation"],
                     previous_host_rev=previous_host_rev,
                     previous_host_build=previous_host_build,
                     previous_payload_rev=previous_payload_rev,
                     previous_payload_build=previous_payload_build)
        _save_state(state)
    source_script = os.path.join(os.path.dirname(__file__), "self_update_apply.py")
    script = os.path.join(_updates_dir(), "self_update_apply.py")
    script_tmp = script + ".tmp"
    try:
        os.makedirs(_updates_dir(), mode=0o700, exist_ok=True)
        with open(source_script, "rb") as src, open(script_tmp, "wb") as dst:
            dst.write(src.read())
            dst.flush(); os.fsync(dst.fileno())
        if _sha256(script_tmp) != helper_digest:
            raise RuntimeError("准备期间更新器身份变化")
        os.chmod(script_tmp, 0o700)
        os.replace(script_tmp, script)
        described = subprocess.run([sys.executable, script, "--describe-contract"],
                                   capture_output=True, text=True, timeout=5, check=True)
        metadata = json.loads(described.stdout)
        if metadata != {"helper_contract": 2, "sha256": helper_digest}:
            raise RuntimeError("固定更新器不支持当前事务契约")
    except Exception as exc:
        lifecycle.reopen(operation_id)
        try: os.remove(script_tmp)
        except OSError: pass
        with _LOCK:
            _DRAINING = False
            state.update(state="staged", operation_id="", error="无法准备更新器：" + str(exc)[:300])
            _save_state(state)
        return {"ok": False, "state": "rejected", "error": state["error"]}
    result = lifecycle.submit_host_update(operation_id)
    if result.get("ok"):
        threading.Thread(target=_watch_native_apply, args=(operation_id,), daemon=True,
                         name="desk-native-update-watch").start()
        return {"ok": True, "state": result.get("state", "accepted"),
                "operation_id": operation_id, "rev": state.get("staged_rev")}
    if result.get("state") in ("rejected", "failed", "cancelled", "not_found"):
        lifecycle.reopen(operation_id)
        with _LOCK:
            _DRAINING = False
            state.update(state="staged", operation_id="", error=str(result.get("error") or "宿主拒绝更新")[:300])
            _save_state(state)
    else:
        # Unknown acceptance cannot be replayed safely. Keep admission sealed and
        # expose the uncertainty instead of launching a second updater.
        with _LOCK:
            state.update(acceptance_unknown=True,
                         error=str(result.get("error") or "更新接收状态未知")[:300],
                         fix="不要重复安装；重新打开 Desk 后检查更新结果")
            _save_state(state)
        threading.Thread(target=_watch_native_apply, args=(operation_id,), daemon=True,
                         name="desk-native-update-reconcile").start()
    return {"ok": False, **result}


def _watch_native_apply(operation_id):
    """Reopen only when the same host transaction definitively fails preflight."""
    global _DRAINING
    import lifecycle
    import runtime_identity
    facts = runtime_identity.facts()
    response = os.path.join(lifecycle.control_root(), "update-response-%s.json" % operation_id)
    deadline = time.monotonic() + 940
    while time.monotonic() < deadline:
        try:
            with open(response, encoding="utf-8") as handle:
                value = json.load(handle)
            if not lifecycle._response_matches(value, operation_id, facts):
                time.sleep(.1); continue
            if value.get("state") in ("failed", "rejected", "cancelled"):
                with _LOCK:
                    state = _load_state()
                    if (state.get("operation_id") != operation_id
                            or state.get("state") not in ("applying", "switching", "error")):
                        return
                    # Also protect migration from an older host which reports failed just
                    # after TERM. A still-live helper cannot authorize reopening admission.
                    if _pid_alive(_safe_int(state.get("apply_pid"))):
                        time.sleep(.1)
                        continue
                    state.update(state="error", apply_pid=0, acceptance_unknown=False,
                                 completion_id=operation_id, completion_outcome="failed",
                                 completed_at=int(time.time()),
                                 error=state.get("error") or "更新准备失败，当前版本未切换",
                                 fix="当前版本未切换；修正准备失败原因后重试")
                    _save_state(state)
                    lifecycle.reopen(operation_id)
                    _DRAINING = False
                return
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            pass
        time.sleep(.1)


def _watch_apply(proc):
    """预检失败时旧 Desk 不会退出；解除排空，避免当前进程永久拒绝新工作。"""
    global _DRAINING
    try:
        proc.wait()
    finally:
        with _LOCK:
            _DRAINING = False


def _safe_int(value, default=0):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _pid_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _reconcile_pending_commit(value, current_rev, current_build, host_rev, host_build):
    # v2 deliberately reuses the protected *verifying* envelope. Old rollback readers
    # reject it, whereas their accepted_pending_commit path would prematurely reopen work.
    if value.get("state") != "verifying" or value.get("pending_protocol") != 2:
        return value
    if not (str(value.get("pending_rev") or "") == current_rev
            and _safe_int(value.get("pending_build")) == current_build
            and str(value.get("pending_host_rev") or "") == host_rev
            and _safe_int(value.get("pending_host_build")) == host_build):
        return value
    outcome = str(value.get("pending_outcome") or "")
    if outcome not in ("idle", "rolled_back"):
        return value
    import lifecycle
    import runtime_identity
    identity = value.get("pending_identity")
    facts = runtime_identity.facts()
    fields = ("boot_id", "host_instance_id", "launch_generation", "pid", "host_pid")
    if (not isinstance(identity, dict) or not value.get("operation_id")
            or any(not identity.get(key) or identity[key] != facts.get(key) for key in fields)
            or facts.get("host_pid") != os.getppid()
            or _pid_alive(_safe_int(value.get("apply_pid")))):
        return value
    # The live updater and status/admit readers cannot commit concurrently. A persisted
    # post-release receipt AND exact boot identity are needed after acquiring its lock.
    lock = lifecycle.try_update_lock()
    if lock is None:
        return value
    try:
        with _LOCK:
            if _load_state() != value:
                return _load_state()
            try:
                with open(_state_path() + ".commit", encoding="utf-8") as handle:
                    receipt = json.load(handle)
            except (OSError, ValueError):
                return value
            keys = ("operation_id", "pending_protocol", "pending_outcome", "pending_rev", "pending_build",
                    "pending_host_rev", "pending_host_build", "pending_identity")
            if not isinstance(receipt, dict) or any(receipt.get(key) != value.get(key) for key in keys):
                return value
            operation_id = str(value.get("operation_id") or "")
            value.update(state=outcome, current_rev=current_rev, current_build=current_build,
                         current_host_rev=host_rev, current_host_build=host_build,
                         completion_id=operation_id,
                         completion_outcome="succeeded" if outcome == "idle" else "failed",
                         completed_at=int(time.time()), apply_pid=0, operation_id="",
                         acceptance_unknown=False,
                         pending_outcome="", pending_rev="", pending_build=0,
                         pending_host_rev="", pending_host_build=0, pending_identity={}, pending_protocol=0)
            if outcome == "idle":
                value.update(hasUpdate=False, staged_rev="", staged_build=0,
                             staged_host_rev="", staged_host_build=0, error="", fix="")
            else:
                value["fix"] = "旧版已恢复"
            return _save_state(value)
    finally:
        lifecycle.release_update_lock(lock)


def _load_reconciled_state():
    current_rev, current_build, _dev = _current()
    host_rev = str(os.environ.get("DESKTRON_HOST_REV") or "").strip()
    host_build = _safe_int(os.environ.get("DESKTRON_HOST_BUILD"))
    return _reconcile_pending_commit(_load_state(), current_rev, current_build,
                                     host_rev, host_build)


def status():
    current_rev, current_build, dev = _current()
    host_rev = str(os.environ.get("DESKTRON_HOST_REV") or "").strip()
    host_build = _safe_int(os.environ.get("DESKTRON_HOST_BUILD"))
    value = _reconcile_pending_commit(_load_state(), current_rev, current_build,
                                      host_rev, host_build)
    value.setdefault("state", "dev" if dev else "idle")
    value.update(current_rev=current_rev, current_build=current_build, dev=dev,
                 checking=bool(_CHECKING))
    if dev and value.get("state") not in _ACTIVE_SWITCH_STATES \
            and not value.get("acceptance_unknown"):
        value["state"] = "dev"
        value["hasUpdate"] = False
    elif value.get("state") == "downloading" and not _CHECKING:
        # 进程在下载中退出后，磁盘状态不能让按钮永久停在不可点击的进度上。
        value.update(state="error", error="上次下载已中断", fix="点击重试下载")
    value["current"] = current_rev[:12]
    value["host_current"] = host_rev[:12]
    value["host_rev"] = host_rev
    value["host_build"] = host_build
    value["update_mode"] = "source" if dev else "release"
    value["host_identity_known"] = bool(host_rev and host_build)
    value["host_outdated"] = bool(not dev and host_rev and (
        (host_build and current_build and host_build < current_build) or
        (host_build == current_build and current_rev and host_rev != current_rev)))
    value["latest"] = str(value.get("latest_rev") or "")[:12]
    value["hasUpdate"] = False if dev else bool(value.get("hasUpdate"))
    mandatory_at = _iso_time(value.get("mandatory_after")) or 0
    value["mandatory"] = bool(value.get("hasUpdate") and (
        (mandatory_at and time.time() >= mandatory_at) or
        (_safe_int(value.get("min_supported_build")) > 0 and
         current_build < _safe_int(value.get("min_supported_build")))))
    value["draining"] = bool(_DRAINING or external_draining())
    value["unknown"] = not bool(value.get("checked_at"))
    value.setdefault("reachable", False)
    return value


def _loop():
    if FIRST_CHECK_DELAY_S:
        time.sleep(FIRST_CHECK_DELAY_S)
    while True:
        check_now()
        end = time.time() + CHECK_INTERVAL_S
        while time.time() < end:
            state = status()
            if (state.get("state") == "staged" and state.get("mandatory")
                    and not state.get("manual_install") and quiet_ready()):
                result = request_apply(state.get("staged_rev"), automatic=True)
                if result.get("ok"):
                    # 正常路径会很快终止本进程；若独立更新器预检失败，监视线程会解除排空，
                    # 本循环仍在，后续检查可以重新下载或重试。
                    time.sleep(1)
            time.sleep(min(30, max(1, end - time.time())))


def start_background():
    global _STARTED
    if sys.platform != "darwin":
        return False
    with _LOCK:
        if _STARTED:
            return False
        _STARTED = True
    threading.Thread(target=_loop, daemon=True, name="desk-self-update").start()
    return True
