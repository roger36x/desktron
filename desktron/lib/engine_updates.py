"""引擎有没有新版本。

Desk 此前对这件事完全无感:`available()` 只是 `shutil.which(bin)`,答的是「装没装」;
manifest 里 kind=engine 的四条只有 install、没有 min,所以连比对的对象都没有;而
`desktron deps` 读得出本机版本,却从不问上游,且只有命令行入口。人知道引擎更新了,
靠的是在终端里跑它时它自己提示(2026-08-21 与用户核过)。

这里补上「问上游」这一步,结果挂到输入框上方那条系统信息(见 app-chat.js 的 hsEnvironment)。

形状照 system/api.py 的内网登录态:请求只读缓存立刻回,真正去问 npm 的活在后台线程里。
理由相同——`npm view` 是网络调用,本机实测 0.7~3.7 秒,阻塞在请求线程上就是每次开会话
都付一遍。TTL 取 6 小时:引擎发版以天计,查得再勤也没有新东西。

不在名单里的引擎宁可不报,也不猜一个包名——猜错会稳定地报一个假的「有新版本」。
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time

#: 引擎 → (npm 包名, registry)。registry 留空用默认源。
#: 版本以 npm 为准而不是各家自己的 install 脚本:脚本要跑起来才知道版本,而 npm view 只问不装。
#: claude 的分发是 install.sh,但同一版本同时发在 npm 上(2026-08-21 核对:本机 2.1.235,
#: npm 上 2.1.238,量级与发布节奏对得上)。
NPM = {
    "claude": ("@anthropic-ai/claude-code", ""),
    "codex": ("@openai/codex", ""),
    # Pi 是公开 npm 包。不要继承机器默认源：内网镜像可能先同步 packument、后同步 tarball，
    # 此时 `npm view` 会报有新版本，但安装稳定 404（2026-09-20 的 telemetry 0.86.0）。
    "pi": ("@earendil-works/pi-coding-agent", "https://registry.npmjs.org/"),
}

TTL = 6 * 3600        # 结果保鲜期
WAIT = 30             # 给每次 npm view 的上限(本机实测最慢 3.7 秒,留足余量)
_cache = {}           # engine → {"have", "latest", "at"}
_lock = threading.Lock()
_busy = set()


def _ver(s):
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", str(s or ""))
    return m.group(1) if m else ""


def _newer(a, b):
    """a 是否比 b 新。按数字分段比,段数不同的短的那个补零。"""
    a, b = _ver(a), _ver(b)
    if not a or not b:
        return False
    pa = [int(x) for x in a.split(".")]
    pb = [int(x) for x in b.split(".")]
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return pa > pb


def _latest(pkg, registry=""):
    cmd = ["npm", "view", pkg, "version"]
    if registry:
        cmd += ["--registry", registry]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=WAIT)
    except Exception:
        return ""
    # 判定只看输出不看 returncode:本进程有 SIGCHLD 收割器,rc 两个方向都不可信(同 deskcfg 的注释)
    return _ver((r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else "")


def _refresh(engine):
    have = latest = ""
    try:
        import check_deps
        _, have = check_deps.probe(engine)
        pkg, reg = NPM.get(engine, ("", ""))
        if pkg:
            latest = _latest(pkg, reg)
    except Exception:
        pass
    with _lock:
        _busy.discard(engine)
        if have or latest:
            _cache[engine] = {"have": have, "latest": latest, "at": time.time()}


def status(engine, install_cmd=""):
    """立刻可回的那一份;过期就顺手起一轮后台查,自己不等。

    没查出结果时回空字典而不是「已是最新」——那两件事不一样,界面不该替它下结论。
    """
    engine = str(engine or "")
    if engine not in NPM:
        return {}
    with _lock:
        row = _cache.get(engine)
        stale = not row or time.time() - row["at"] > TTL
        if stale and engine not in _busy:
            _busy.add(engine)
            threading.Thread(target=_refresh, args=(engine,), daemon=True,
                             name="engine-update-" + engine).start()
    if not row:
        return {}
    out = {"engine": engine, "have": row["have"], "latest": row["latest"],
           "hasUpdate": _newer(row["latest"], row["have"])}
    if out["hasUpdate"] and install_cmd:
        out["how"] = install_cmd
    return out


def install_cmd(repo_root, engine):
    """manifest 里那条安装命令。单一源:与 `desktron install` 用的是同一份声明。"""
    try:
        import check_deps
        for r in check_deps.parse_reqs(repo_root):
            if r.get("kind") == "engine" and r.get("name") == engine:
                return (r.get("install") or "").strip()
    except Exception:
        pass
    return ""

def _owning_prefix(engine):
    """当前生效的引擎装在哪个 npm 前缀下；不是 npm 全局包时返回空串。

    版本探测走后端 PATH 上的同名命令，更新必须落到同一处；否则装到别处，界面仍报旧版本。
    """
    pkg = NPM.get(engine, ("", ""))[0]
    found = shutil.which(engine)
    if not (pkg and found):
        return ""
    real = os.path.realpath(found)
    marker = os.sep + os.path.join("lib", "node_modules", *pkg.split("/")) + os.sep
    i = real.find(marker)
    return real[:i] if i > 0 else ""


def _npm_argv(cmd, engine):
    """把 manifest 的 npm 安装命令落成可直接执行的 argv 与 PATH；不是 npm 命令返回 None。

    不经登录 shell：`bash -l` 读的是 bash 配置，用户在 zsh 里配的 nvm 不生效，PATH 会落到
    /usr/local/bin 的 x86_64 node，在没有 Rosetta 的 Apple 芯片上报 Bad CPU type
    （2026-10-15 实测），配置里其他命令的报错也会混进输出。这里改用拥有该引擎的前缀里的
    npm；找不到时用 Desk 解析到的 npm。node 取 npm 同目录，保证 `env node` 与 npm 同架构。
    """
    words = shlex.split(os.path.expandvars(cmd))
    if not words or words[0] != "npm":
        return None
    prefix = _owning_prefix(engine)
    npm = os.path.join(prefix, "bin", "npm") if prefix else ""
    if not (npm and os.access(npm, os.X_OK)):
        import cli_path
        npm = cli_path.which("npm") or ""
    if not npm:
        return None
    if prefix and "--prefix" in words:
        k = words.index("--prefix")
        if k + 1 < len(words):
            words[k + 1] = prefix
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(os.path.abspath(npm)) + os.pathsep + env.get("PATH", "")
    return [npm] + words[1:], env


def run_update(repo_root, engine, timeout=600):
    """按引擎渠道更新；执行准入另由协议能力检查负责。→ {"ok": bool, "out": 尾部输出, "cmd": 跑的那条}。

    命令取自 manifest 的同一份声明(install_cmd),不接受调用方传进来的命令行——那等于开一个
    任意执行的口子。能升的只有 NPM 那张表里登记过的几家,别的一律拒。

    跑完把缓存作废:界面下一次问「有没有新版本」时要看到升完的结果,而不是等 TTL 过期。
    输出只回尾部:npm 一路刷几百行进度,界面上要的是「成没成、失败那几行说了什么」。
    """
    engine = str(engine or "")
    if engine not in NPM:
        return {"ok": False, "out": "不认识这家引擎:" + engine, "cmd": ""}
    cmd = install_cmd(repo_root, engine)
    if not cmd:
        return {"ok": False, "out": "manifest 里没有这家引擎的安装命令", "cmd": ""}
    try:
        planned = _npm_argv(cmd, engine)
        if planned:
            argv, env = planned
            cmd = shlex.join(argv)
            r = subprocess.run(argv, capture_output=True, timeout=timeout, env=env)
        else:
            import oscompat
            r = subprocess.run(oscompat.login_shell_argv(cmd), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "out": "超过 %d 秒还没跑完,已放弃" % timeout, "cmd": cmd}
    except Exception as exc:
        return {"ok": False, "out": str(exc)[:400], "cmd": cmd}
    out = (r.stdout + b"\n" + r.stderr).decode("utf-8", "replace").strip()
    with _lock:
        _cache.pop(engine, None)   # 作废缓存:下一次问要问到升完之后的版本
    return {"ok": r.returncode == 0, "out": out[-1200:], "cmd": cmd}
