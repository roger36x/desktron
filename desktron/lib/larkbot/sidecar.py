"""长连接 sidecar 的生命周期(bin/lark-ws.mjs 的父)。

一个飞书 app 一条长连接、一个 Node 进程。不合并成一个进程管多个 app:SDK 的 WSClient
是按 app 建连的,合起来只会在我们这层再写一遍它已经做过的事,而且一个 app 的凭证出问题
会牵连其他的。

进程用 pid 文件守卫，由飞书 Bot 看护模块负责启动与退出。
凭证经环境变量传给子进程,不进 argv——argv 在 ps 里人人可见。
"""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import json
import os
import signal
import subprocess
import sys
import threading
import time

_BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "bin", "lark-ws.mjs")
_deps_lock = threading.Lock()


def home():
    return os.path.expanduser(os.environ.get(
        "DESKTRON_LARKBOT_HOME", "~/.local/share/desktron/larkbot"))


def _pid_path(app_id):
    return os.path.join(home(), f"ws-{app_id}.pid")


def _state_path(app_id):
    return os.path.join(home(), f"ws-{app_id}.state.json")


def _log_path(app_id):
    return os.path.join(home(), f"ws-{app_id}.log")


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def running(app_id):
    """→ pid 或 0。pid 文件在但进程没了就顺手清掉,免得状态一直显示"在跑"。"""
    p = _pid_path(app_id)
    try:
        with open(p) as f:
            pid = int((f.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return 0
    if pid and _alive(pid):
        return pid
    try:
        os.remove(p)
    except OSError:
        pass
    return 0


def deps_ok():
    """→ (ok, 说明)。Node 与 SDK 都要在,缺哪个说哪个——
    "启动失败"这四个字对排查毫无帮助。"""
    import shutil
    if not shutil.which("node"):
        return False, "node 不在 PATH 上"
    if not os.path.isdir(os.path.join(home(), "node_modules", "@larksuiteoapi", "node-sdk")):
        return False, f"飞书 SDK 未安装(在 {home()} 里跑 npm install)"
    # 接入向导的自动那条要它画二维码。缺了不影响收发消息,但「新建」会当场退回
    # 六步引导——分开报,免得人以为长连接坏了。
    if not os.path.isdir(os.path.join(home(), "node_modules", "qrcode-terminal")):
        return False, f"qrcode-terminal 未安装(新建要它画二维码;在 {home()} 里跑 npm install)"
    if not os.path.isfile(_BIN):
        return False, f"找不到 {_BIN}"
    return True, "就绪"


def ensure_deps():
    """幂等地把依赖装好。→ (ok, 说明)。装机脚本与设置面板都调它。"""
    # 首次部署后，装机、接入向导和后台看护可能同时发现依赖缺失。npm install 会改
    # package.json 与 node_modules，必须串行；第二个调用拿到锁后会由 deps_ok 直接返回。
    with _deps_lock:
        return _ensure_deps_unlocked()


def _ensure_deps_unlocked():
    ok, why = deps_ok()
    if ok:
        return True, why
    import shutil
    if not shutil.which("node"):
        return False, "node 不在 PATH 上,先装 node(≥18)"
    d = home()
    os.makedirs(d, exist_ok=True)
    pkg = os.path.join(d, "package.json")
    if not os.path.isfile(pkg):
        with open(pkg, "w", encoding="utf-8") as f:
            json.dump({"name": "desktron-larkbot", "private": True, "type": "module",
                       "dependencies": {"@larksuiteoapi/node-sdk": "^1.64.0",
                                        "qrcode-terminal": "^0.12.0"}}, f, indent=2)
    else:
        # 老 package.json 里可能没有后加的依赖(qrcode-terminal 是 2026-08-25 加的)。
        # 直接补进去再 install,不要求人手动改文件。
        try:
            with open(pkg, encoding="utf-8") as f:
                cur = json.load(f)
            deps = cur.setdefault("dependencies", {})
            wanted = {"@larksuiteoapi/node-sdk": "^1.64.0",
                      "qrcode-terminal": "^0.12.0"}
            missing = {name: version for name, version in wanted.items() if name not in deps}
            if missing:
                deps.update(missing)
                with open(pkg, "w", encoding="utf-8") as f:
                    json.dump(cur, f, indent=2)
        except Exception:
            pass          # 文件坏了就让下面的 npm install 去报,不在这里判死
    try:
        r = subprocess.run(["npm", "install", "--no-audit", "--no-fund"], cwd=d,
                           capture_output=True, text=True, timeout=600)
    except Exception as e:
        return False, f"npm install 跑不起来:{str(e)[:160]}"
    if r.returncode != 0:
        return False, f"npm install 失败:{(r.stderr or r.stdout or '')[-300:]}"
    return deps_ok()


def start(app_id, domain="", ui_port=None, install_missing=False):
    """→ (pid, err)。已经在跑就直接返回那个 pid,重复调用无害。

    install_missing 只给后台看护使用：迁移来的声明和凭证可能已经齐全，但这台新机器
    还没有 Node 包。安装在看护线程中进行，不阻塞 Desk 启动与页面请求。
    """
    if not app_id:
        return 0, "缺 app_id"
    pid = running(app_id)
    if pid:
        return pid, ""
    ok, why = deps_ok()
    if not ok and install_missing:
        ok, why = ensure_deps()
    if not ok:
        return 0, why

    from . import larkapi
    sec = larkapi.app_secret(app_id)
    if not sec:
        return 0, ("未配置 app_secret(在 ~/.config/desktron/env 加 "
                   + larkapi.SECRET_KEY_FMT.format(app_id=app_id) + "=…)")

    port = str(ui_port or os.environ.get("DESKTRON_UI_PORT") or 7765)
    env = dict(os.environ)
    env["LARK_APP_SECRET"] = sec           # 经环境变量,不进 argv(ps 里人人可见)
    d = home()
    os.makedirs(d, exist_ok=True)
    argv = ["node", _BIN, "--app-id", app_id, "--port", port,
            "--domain", domain or larkapi.DEFAULT_DOMAIN]
    # 上一个进程留下的 failed/exited 是历史，不属于即将启动的新进程。若不先清，
    # 下面启动确认循环会在 Node 来得及写 starting 之前读到旧 failed，当场误报失败；
    # 实际新进程已经起来，界面与看护随后会得到互相矛盾的状态。
    try:
        os.remove(_state_path(app_id))
    except OSError:
        pass
    try:
        with open(_log_path(app_id), "a") as lg:
            p = subprocess.Popen(argv, cwd=d, env=env, stdout=lg, stderr=lg,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        return 0, f"起不来:{str(e)[:200]}"
    with open(_pid_path(app_id), "w") as f:
        f.write(str(p.pid))
    # 凭证错、权限不足这些不是启动失败:进程能起来,几秒后才被飞书拒掉然后自己退。
    # 只等 0.6 秒会报"启动成功",然后面板显示未启动,人再点一次还是一样,看不出原因。
    # 所以多等一会儿并读状态文件——lark-ws.mjs 退出时会把 SDK 那条错误写进去。
    for _ in range(24):
        time.sleep(0.25)
        st = conn_state(app_id)
        if st["status"] == "failed":
            return 0, "飞书拒绝了这个连接:" + (st["error"] or "原因见日志")
        if not _alive(p.pid):
            return 0, "启动后退出了。" + (st["error"] or f"日志尾部:{log_tail(app_id, 6)[-300:]}")
        if st["status"] and st["status"] not in ("starting", "unknown"):
            break          # 已经连上或进入重连,不必再等
    return p.pid, ""


def _clear(app_id):
    """清掉 pid 与状态文件。两处都要清:状态文件留着的话,面板会一直读到上一次的
    connected——而进程可能是被 kill -9、崩溃、或机器重启后残留下来的,那时 running()
    返回 0、stop() 提前返回,状态文件就永远留在那儿了。"""
    for f in (_pid_path(app_id), _state_path(app_id)):
        try:
            os.remove(f)
        except OSError:
            pass


def stop(app_id):
    pid = running(app_id)
    if not pid:
        _clear(app_id)      # 进程已经不在了,残留的文件仍要收干净
        return True
    try:
        # 自成进程组(start_new_session),killpg 才收得干净
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    for _ in range(20):
        if not _alive(pid):
            break
        time.sleep(0.1)
    _clear(app_id)
    return True


def conn_state(app_id, stale=30):
    """长连接的真实状态。→ {"status":str, "at":int, "fresh":bool}

    与 running() 是两件事:凭证过期、权限没开、网络被中间设备拦掉时,进程照样活着
    (SDK 一直在重连),但一条消息也收不到。只看 pid 的话面板会显示"在线",而人在群里
    @ 它没有任何反应,无从下手。状态由 lark-ws.mjs 每 5 秒写一次。

    fresh=False 表示状态文件太旧——进程可能刚起来还没写第一次,也可能卡死了。
    """
    p = _state_path(app_id)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        at = int(d.get("at") or 0)
        st = str(d.get("status") or "unknown")
        if st == "[object Object]":
            # 2026-08-27 之前的 lark-ws.mjs 拿 SDK 的状态对象去 String() 留下的痕迹。
            # 源头已修,但没重启的旧 sidecar 进程还会写一阵子——归一成 unknown,
            # 别让面板把一个在线的 bot 显示成这串垃圾。
            st = "unknown"
        # failed / exited 是终态,不按新鲜度判:进程已经不在了,时间戳只会越来越旧,
        # 而这两个状态恰恰是最需要被看见的——它们带着退出的原因。
        fresh = True if st in ("failed", "exited") else (time.time() - at) < stale
        return {"status": st, "at": at, "fresh": fresh, "error": str(d.get("error") or "")[:300]}
    except Exception:
        return {"status": "", "at": 0, "fresh": False, "error": ""}


def log_tail(app_id, n=40):
    try:
        with open(_log_path(app_id), encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""


def status(agents):
    """所有 agent 的长连接状态。agents 是声明列表。"""
    out = []
    for a in agents:
        app_id = a.get("lark_app_id") or ""
        pid = running(app_id) if app_id else 0
        # 进程不在也要读一次:失败退出时的原因就留在状态文件里,那正是最该显示的一档。
        cs = conn_state(app_id) if app_id else {"status": "", "at": 0, "fresh": False, "error": ""}
        if not pid and cs.get("status") not in ("failed", "exited"):
            cs = {"status": "", "at": 0, "fresh": False, "error": ""}
        out.append({"id": a.get("id"), "name": a.get("name") or a.get("id"),
                    "app_id": app_id, "enabled": bool(a.get("enabled")),
                    "pid": pid, "conn": cs.get("status") or "",
                    "conn_fresh": bool(cs.get("fresh")),
                    "conn_error": cs.get("error") or "",
                    "has_secret": bool(app_id) and _has_secret(app_id)})
    return out


def _has_secret(app_id):
    from . import larkapi
    return bool(larkapi.app_secret(app_id))
