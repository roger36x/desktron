"""Desktron 启动器的 Windows 实现。

macOS / Linux 继续使用 bash 启动器 `desktron/bin/desktron`；两者子命令与语义保持一致。
Windows 没有 exec：`ui` 子命令在本进程内运行后端，保证原生宿主看到的直接子进程就是后端本身，
父进程校验、Job Object 与停止事件才能精确作用于它。

用法(由 `desktron.cmd` 或原生壳调用):
  python desk_launcher.py ui [端口]
  python desk_launcher.py prepare | install [--upgrade] | deps | validate | doctor
  python desk_launcher.py install-check | pi-subagents-setup | provider-setup
  python desk_launcher.py sync-home | app-install | smoke
"""
import json
import os
import shutil
import subprocess
import sys
import time

LIB = os.path.dirname(os.path.abspath(__file__))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import oscompat  # noqa: E402

PROJECT_ROOT = os.path.abspath(os.path.join(LIB, "..", ".."))
HOME = os.path.expanduser("~")
SHARE = os.path.join(HOME, ".local", "share", "desktron")
LOCAL_BIN = os.path.join(HOME, ".local", "bin")
PRODUCT_LINK_ROOT = os.environ.get("DESKTRON_CANONICAL_HOME") or PROJECT_ROOT
ENGINE_HOME = os.environ.get("DESKTRON_ENGINE_HOME") or os.path.join(SHARE, "engine-home")
DATA_HOME = os.environ.get("DESKTRON_DATA_HOME") or os.path.join(SHARE, "data")
WORKSPACE = os.environ.get("DESKTRON_WORKSPACE") or os.path.join(SHARE, "workspace")
SECRETS = os.path.join(HOME, ".config", "desktron", "env")
# 产品命令行工具。Git Bash(Claude Code 的 Bash 工具)与 PowerShell/cmd(Codex)各用一套入口。
TOOLS = ("desktron", "curator", "kbkit", "desk-artifact", "desk-msg", "desk-session",
         "desk-task", "shepherd")
_PYTHON_MODULE_TOOLS = {"shepherd": "shepherd.cli", "curator": "curator.cli"}
_STARTED = time.monotonic()


def phase(name):
    print("[startup] launcher %-24s epoch=%d" % (name, int(time.time())), flush=True)


def fail(message, code=1):
    print("desktron: " + message, file=sys.stderr, flush=True)
    return code


# ------------------------------------------------------------------------------------------
# 运行时
# ------------------------------------------------------------------------------------------
def python_runtime_spec():
    with open(os.path.join(LIB, "provision", "python-runtime.json"), encoding="utf-8") as stream:
        return json.load(stream)


def managed_python_home():
    spec = python_runtime_spec()
    import platform
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
        platform.machine().lower(), "x64")
    os_name = "windows" if oscompat.IS_WINDOWS else sys.platform
    return os.path.join(os.path.dirname(DATA_HOME), "runtime",
                        "python-%s-%s-%s" % (spec["version"], os_name, arch))


def git_bash_dir():
    """Git for Windows 的 bin 目录(含 bash.exe)。

    技能脚本以 `bash xxx.sh` 调用；Windows 的 System32\\bash.exe 是 WSL 启动器，
    运行在另一套文件系统与环境里。Git Bash 必须排在它前面。Claude Code 也依赖 Git Bash。
    """
    if not oscompat.IS_WINDOWS:
        return ""
    candidates = []
    git = shutil.which("git")
    if git:
        root = os.path.dirname(os.path.dirname(os.path.realpath(git)))
        candidates.append(os.path.join(root, "bin"))
    for base in (os.environ.get("PROGRAMFILES", ""), os.environ.get("LOCALAPPDATA", "")):
        if base:
            candidates.append(os.path.join(base, "Git", "bin"))
            candidates.append(os.path.join(base, "Programs", "Git", "bin"))
    for folder in candidates:
        if os.path.isfile(os.path.join(folder, "bash.exe")):
            return folder
    return ""


def runtime_path():
    """Desk 进程及其引擎子进程使用的 PATH：托管运行时在前，用户原有 PATH 在后。"""
    parts = []
    extra = os.environ.get("DESKTRON_RUNTIME_PATH", "")
    if extra:
        parts.extend(extra.split(os.pathsep))
    parts.append(os.path.dirname(sys.executable))
    parts.append(LOCAL_BIN)
    bash_dir = git_bash_dir()
    if bash_dir:
        parts.append(bash_dir)
        os.environ.setdefault("CLAUDE_CODE_GIT_BASH_PATH", os.path.join(bash_dir, "bash.exe"))
    try:
        from provision.setup_runtime import _managed_node_home
        parts.append(_managed_node_home())
    except Exception:
        pass
    seen, out = set(), []
    for item in parts + os.environ.get("PATH", "").split(os.pathsep):
        key = os.path.normcase(item)
        if item and key not in seen:
            seen.add(key)
            out.append(item)
    return os.pathsep.join(out)


def load_secrets(environ):
    """读取 ~/.config/desktron/env(KEY=value 每行一条)。PATH 只由启动器管理，不被覆盖。"""
    try:
        with open(SECRETS, encoding="utf-8") as stream:
            lines = stream.read().splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key.replace("_", "").isalnum() or key.upper() == "PATH":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        environ[key] = value


def pythonpath():
    current = os.environ.get("PYTHONPATH", "")
    return LIB + (os.pathsep + current if current else "")


def prepare_runtime():
    """准备托管 Node，并把它放到本进程及后续子进程的 PATH 前面。"""
    from provision import setup_runtime
    node = setup_runtime.prepare_sync()["node"]
    if not os.path.isfile(node):
        raise RuntimeError("Node 准备结果不可用，安装已停止。")
    os.environ["PATH"] = os.path.dirname(node) + os.pathsep + os.environ.get("PATH", "")
    return node


def ensure_python_alias():
    """托管运行时只带 python.exe；Desk 的技能与脚本按 python3 调用，补一个同目录副本。

    python.exe 只是加载同目录 python3XX.dll 的启动器，复制后行为一致；不使用 .cmd 包装，
    避免命令行参数再经过一次 cmd.exe 解析。
    """
    if not oscompat.IS_WINDOWS:
        return
    folder = os.path.dirname(sys.executable)
    target = os.path.join(folder, "python3.exe")
    if os.path.basename(sys.executable).lower() == "python.exe" and not os.path.exists(target):
        try:
            shutil.copy2(sys.executable, target)
        except OSError as error:
            print("desktron: 未能创建 python3 别名：%s" % error, file=sys.stderr)


# ------------------------------------------------------------------------------------------
# 工作区
# ------------------------------------------------------------------------------------------
def _same_target(link, target):
    current = oscompat.read_link(link)
    if current is None:
        return False
    strip = lambda p: os.path.normcase(os.path.abspath(p.replace("\\\\?\\", "")))
    return strip(current) == strip(target)


def _link_asset(target, link, is_dir):
    """目录用符号链接或 junction；文件在无法创建符号链接时复制，并在内容变化时刷新。"""
    if oscompat.is_link(link):
        if _same_target(link, target):
            return
        oscompat.remove_link(link)
    elif os.path.exists(link):
        if is_dir:
            return   # 用户自己的同名目录不动
        try:
            with open(link, "rb") as a, open(target, "rb") as b:
                if a.read() == b.read():
                    return
        except OSError:
            return
        marker = link + ".desk-copy"
        if not os.path.exists(marker):
            return   # 不是 Desk 复制的文件，不覆盖
        os.unlink(link)
    try:
        oscompat.link_path(target, link, target_is_directory=is_dir)
    except OSError:
        if is_dir:
            raise
        shutil.copyfile(target, link)
        with open(link + ".desk-copy", "w", encoding="utf-8") as stream:
            stream.write(target)


def setup_workspace():
    os.makedirs(WORKSPACE, exist_ok=True)
    if not os.path.isdir(os.path.join(WORKSPACE, ".git")):
        try:
            for argv in (["git", "init", "-q"], ["git", "config", "user.email", "desktron@local"],
                         ["git", "config", "user.name", "desktron"]):
                subprocess.run(argv, cwd=WORKSPACE, capture_output=True, timeout=20,
                               **oscompat.no_window_kwargs())
        except Exception:
            pass
    for asset in (".claude", ".desk", "manifest.yaml", "prompts", "AGENTS.md", "AGENTS.en.md",
                  "common", "desk-docs"):
        source = os.path.join(PROJECT_ROOT, asset)
        if not os.path.exists(source):
            continue
        target = os.path.join(PRODUCT_LINK_ROOT, asset)
        try:
            _link_asset(target, os.path.join(WORKSPACE, asset), os.path.isdir(source))
        except OSError as error:
            print("desktron: 工作区资产 %s 未能链接：%s" % (asset, error), file=sys.stderr)


def engine_env():
    os.environ["XDG_CONFIG_HOME"] = ENGINE_HOME
    os.environ["XDG_DATA_HOME"] = DATA_HOME
    setup_workspace()


# ------------------------------------------------------------------------------------------
# 命令行入口
# ------------------------------------------------------------------------------------------
def _tool_source(tool):
    return os.path.join(PRODUCT_LINK_ROOT, "desktron", "bin", tool)


def install_tool_shims():
    """在 ~/.local/bin 生成两套入口：无扩展名的 sh 脚本给 Git Bash，.cmd 给 PowerShell/cmd。"""
    os.makedirs(LOCAL_BIN, exist_ok=True)
    python = sys.executable
    lib = os.path.join(PRODUCT_LINK_ROOT, "desktron", "lib")
    for tool in TOOLS:
        source = _tool_source(tool)
        if not os.path.exists(os.path.join(PROJECT_ROOT, "desktron", "bin", tool)):
            continue
        if tool == "desktron":
            py_args = [os.path.join(lib, "desk_launcher.py")]
        elif tool in _PYTHON_MODULE_TOOLS:
            py_args = ["-m", _PYTHON_MODULE_TOOLS[tool]]
        elif tool == "kbkit":
            py_args = ["-m", "curator.cli"]
        else:
            py_args = [source]
        posix = lambda p: p.replace("\\", "/")
        sh = ("#!/bin/sh\n# Desk 生成的入口；重新运行 desktron prepare 会覆盖。\n"
              "PYTHONPATH=\"%s${PYTHONPATH:+;$PYTHONPATH}\" exec \"%s\" %s \"$@\"\n"
              % (posix(lib), posix(python), " ".join('"%s"' % posix(a) for a in py_args)))
        cmd = ("@echo off\r\nrem Generated by Desk; rerun \"desktron prepare\" to refresh.\r\n"
               "set \"PYTHONPATH=%s;%%PYTHONPATH%%\"\r\n\"%s\" %s %%*\r\n"
               % (lib, python, " ".join('"%s"' % a for a in py_args)))
        with open(os.path.join(LOCAL_BIN, tool), "w", encoding="utf-8", newline="\n") as stream:
            stream.write(sh)
        with open(os.path.join(LOCAL_BIN, tool + ".cmd"), "wb") as stream:
            stream.write(_batch_bytes(cmd))


def _batch_bytes(text):
    """cmd.exe 按控制台(OEM)代码页读批处理。路径含该代码页表示不了的字符时改用 UTF-8 并先切换代码页。"""
    if not oscompat.IS_WINDOWS:
        return text.encode("utf-8")
    import ctypes
    codepage = "cp%d" % ctypes.windll.kernel32.GetOEMCP()
    try:
        return text.encode(codepage)
    except (UnicodeEncodeError, LookupError):
        return ("@chcp 65001 >nul\r\n" + text).encode("utf-8")


# ------------------------------------------------------------------------------------------
# 子命令
# ------------------------------------------------------------------------------------------
def native_host_alive():
    if os.environ.get("DESKTRON_NATIVE_LAUNCH") != "1":
        return True
    try:
        expected = int(os.environ.get("DESKTRON_NATIVE_HOST_PID") or 0)
    except ValueError:
        return False
    return expected > 1 and os.getppid() == expected and oscompat.process_alive(expected)


def cmd_ui(args):
    phase("begin")
    host_token = os.environ.pop("DESKTRON_APP_CONTROL_HOST_TOKEN", "")
    if not native_host_alive():
        return 75
    from provision.setup_runtime import node_command
    node = node_command()
    if not node:
        return fail("缺少原生 Node ≥22.19，请运行 desktron prepare。")
    os.environ["PATH"] = os.path.dirname(node) + os.pathsep + os.environ["PATH"]
    check = subprocess.run([sys.executable, os.path.join(PROJECT_ROOT, "scripts", "check_frontend.py")],
                           **oscompat.no_window_kwargs())
    if check.returncode != 0:
        print("修复上述前端问题后再启动", flush=True)
        return 1
    if not native_host_alive():
        return 75
    phase("frontend check ready")
    engine_env()
    phase("workspace ready")
    port = int(args[0]) if args else 7765
    os.chdir(WORKSPACE)
    if os.environ.get("DESKTRON_NATIVE_LAUNCH") != "1":
        # 原生壳自己持有休眠抑制；命令行直接运行时由后端进程持有。
        oscompat.inhibit_sleep()
    if host_token:
        os.environ["DESKTRON_APP_CONTROL_HOST_TOKEN"] = host_token
    phase("exec ui server")
    server = os.path.join(LIB, "ui_server.py")
    sys.argv = [server, os.path.join(PROJECT_ROOT, "frontend"), str(port), str(port + 1), WORKSPACE]
    import runpy
    runpy.run_path(server, run_name="__main__")
    return 0


def cmd_prepare(_args):
    engine_env()
    ensure_python_alias()
    install_tool_shims()
    prepare_runtime()
    cmd_pi_subagents_setup([], prepared=True)
    print("desktron: Desk 工作区、运行环境与可用 Provider 已准备好。")
    return 0


def cmd_pi_subagents_setup(_args, prepared=False):
    if not prepared:
        prepare_runtime()
    from pi_subagents import ensure
    ok, detail = ensure()
    if not ok:
        return fail("Pi 子代理固定依赖准备失败：%s" % str(detail)[:300])
    print("desktron: Pi 子代理固定依赖已就绪。")
    result = subprocess.run([sys.executable, "-m", "shepherd.cli", "prepare-claude"],
                            env=dict(os.environ, PYTHONPATH=pythonpath()), capture_output=True,
                            text=True, **oscompat.no_window_kwargs())
    if result.returncode != 0:
        print("desktron: Shepherd 的 Claude SDK 未就绪，可稍后运行 shepherd prepare-claude：%s"
              % (result.stdout + result.stderr)[:300], file=sys.stderr)
    else:
        print("desktron: Shepherd 的 Claude SDK 已就绪。")
    return 0


def _run_install_command(command):
    argv = oscompat.command_shell_argv(command)
    return subprocess.run(argv).returncode == 0


def cmd_install(args):
    upgrade = bool(args and args[0] == "--upgrade")
    prepare_runtime()
    print("== Pi Subagents 固定依赖(幂等)==")
    cmd_pi_subagents_setup([], prepared=True)
    print("\n== 依赖体检 ==")
    import check_deps
    deps = [sys.executable, os.path.join(LIB, "check_deps.py"), PROJECT_ROOT]
    if subprocess.run(deps).returncode != 0:
        print("\n== 补装缺失的运行时 ==")
        missing = subprocess.run(deps + ["--emit-missing-runtime"], capture_output=True, text=True).stdout
        for line in missing.splitlines():
            name, _, command = line.partition("\t")
            if name and command:
                print("  %s: 缺,尝试安装…  (%s)" % (name, command))
                if not _run_install_command(command):
                    print("  %s: 自动安装没成,按上面的提示手动装一次。" % name, file=sys.stderr)
        if subprocess.run(deps, capture_output=True).returncode != 0:
            return fail("必备运行时仍然缺失,按上面命令补齐后重跑 desktron install。")
        print("desktron: 运行时已补齐,继续。")
    print("\n== 飞书 Bot 运行依赖(幂等)==")
    try:
        from larkbot import sidecar
        ok, why = sidecar.ensure_deps()
        print("  " + ("ok: " if ok else "未就绪: ") + why)
    except Exception as error:
        print("desktron: 飞书 Bot 依赖暂未装好；不影响 Desk 使用，后台会继续重试。(%s)" % error,
              file=sys.stderr)
    print("\n== 引擎(claude/codex/pi,追最新)==")
    for req in check_deps.parse_reqs(PROJECT_ROOT):
        if req.get("kind") != "engine" or not req.get("install"):
            continue
        name, command = req["name"], req["install"]
        installed = bool(shutil.which(name))
        ready = installed and not upgrade and check_deps.engine_ready(name)
        if ready:
            print("  %s: 已装;协议能力已确认,跳过" % name)
            continue
        verb = "修复" if installed and not upgrade else ("升级" if installed else "安装")
        print("  %s: %s最新…  (%s)" % (name, verb, command))
        if _run_install_command(command):
            if not check_deps.engine_ready(name):
                print("desktron: %s 已安装但协议探针仍未通过；不会把它标记为可用。" % name, file=sys.stderr)
        else:
            print("desktron: %s 安装失败,手动跑上面命令。" % name, file=sys.stderr)
    print("\n== 命令行入口与工作区 ==")
    ensure_python_alias()
    install_tool_shims()
    engine_env()
    print("  ok: %s" % WORKSPACE)
    print("\n== 还需用户自备(登录态/密钥,无法自动)==")
    print("  - 登录:claude auth login(订阅或 API key)、codex login、pi 内 /login")
    print("  - 本地密钥:%s" % SECRETS)
    return 0


def _exec_python(path, argv):
    sys.argv = [path] + list(argv)
    import runpy
    runpy.run_path(path, run_name="__main__")
    return 0


def cmd_host_update_apply(args):
    """Windows 尚未接入原生宿主的自更新事务；保留入口并明确拒绝，不执行任何替换。"""
    return fail("Windows 版暂不支持应用内更新；请运行新版安装包。", 2)


def cmd_doctor(_args):
    print("project   : %s" % PROJECT_ROOT)
    count = 0
    try:
        with open(SECRETS, encoding="utf-8") as stream:
            count = sum(1 for line in stream if "=" in line and not line.lstrip().startswith("#"))
    except OSError:
        pass
    print("credentials: SECRETS=%d 项(本机,不随 git);各引擎登录态见设置页" % count)
    print("isolation : config=%s" % ENGINE_HOME)
    print("          : data=%s(会话隔离)" % DATA_HOME)
    print("workspace : %s(agent cwd;资产链接自 repo)" % WORKSPACE)
    for label, sub in (("agents", ".desk/agents"), ("skills", ".claude/skills")):
        folder = os.path.join(PROJECT_ROOT, sub)
        print("%-10s: %s" % (label, ("%d 个" % len(os.listdir(folder))) if os.path.isdir(folder) else "缺失"))
    result = subprocess.run([sys.executable, os.path.join(LIB, "validate_manifest.py"), PROJECT_ROOT],
                            capture_output=True, text=True)
    lines = (result.stdout or "").strip().splitlines()
    print("manifest  : %s" % (lines[-1] if lines else "无输出"))
    return 0


def cmd_sync_home(args):
    if args:
        return fail("sync-home 不接受参数 %s" % args[0], 2)
    if not os.path.isdir(os.path.join(PROJECT_ROOT, "scripts", "dev")):
        return fail("sync-home 是开发态命令；本机产品更新请重新运行安装包。", 2)
    prepare_runtime()
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts", "dev"))
    import payload
    print("desktron: 构建内置 App 静态资源…")
    if subprocess.run([sys.executable, os.path.join(PROJECT_ROOT, "scripts", "dev", "build_builtin_apps.py")]
                      ).returncode != 0:
        return fail("内置 App 构建失败；已安装 home 保持原样。")
    dest = os.path.join(SHARE, "home")
    payload.sync(PROJECT_ROOT, dest, delete=True)
    digest = payload.fingerprint(dest)
    for path, text in ((os.path.join(dest, ".payload-rev"), "payload:%s\n" % digest),
                       (os.path.join(SHARE, ".update-rev"), "payload:%s\n" % digest),
                       (os.path.join(SHARE, ".update-build"), "0\n")):
        with open(path, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    print("desktron: 已同步到 %s" % dest)
    return subprocess.run([sys.executable, os.path.join(dest, "desktron", "lib", "desk_launcher.py"),
                           "pi-subagents-setup"]).returncode


def cmd_app_install(args):
    """从源码构建 Windows 原生壳并安装到 %LOCALAPPDATA%\\Programs\\Desktron。"""
    crate = os.path.join(PROJECT_ROOT, "desktop", "windows")
    if not os.path.isfile(os.path.join(crate, "Cargo.toml")):
        return fail("缺少 Windows 原生壳源码(desktop/windows)。", 1)
    if not shutil.which("cargo"):
        return fail("需要 Rust 工具链(cargo)才能从源码构建原生壳：https://rustup.rs", 1)
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts", "dev"))
    import windows_host
    return windows_host.build_and_install(PROJECT_ROOT, args)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command, args = (argv[0], argv[1:]) if argv else ("", [])
    os.environ["PATH"] = runtime_path()
    load_secrets(os.environ)
    from network_proxy import apply_environment
    try:
        apply_environment(os.environ)
    except (ValueError, OSError):
        return fail("网络代理配置无法读取或不受支持；未回退直连。请检查本机 network.json。")
    os.environ["PATH"] = runtime_path()
    os.makedirs(ENGINE_HOME, exist_ok=True)
    os.makedirs(DATA_HOME, exist_ok=True)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ["PYTHONPATH"] = pythonpath()
    lib_script = lambda name: os.path.join(LIB, name)
    table = {
        "ui": cmd_ui,
        "prepare": cmd_prepare,
        "install": cmd_install,
        "pi-subagents-setup": cmd_pi_subagents_setup,
        "provider-setup": lambda _a: 0,
        "_host-update-apply": cmd_host_update_apply,
        "doctor": cmd_doctor,
        "sync-home": cmd_sync_home,
        "app-install": cmd_app_install,
        "install-check": lambda a: _exec_python(lib_script(os.path.join("provision", "install_check.py")),
                                                ["--root", PROJECT_ROOT, *a]),
        "validate": lambda _a: _exec_python(lib_script("validate_manifest.py"), [PROJECT_ROOT]),
        "deps": lambda _a: (print("desktron: 依赖体检(声明见 manifest.yaml requirements)"),
                            _exec_python(lib_script("check_deps.py"), [PROJECT_ROOT]))[1],
        "update": lambda _a: fail("自动更新通道已下线；请重新运行新版安装包。", 2),
    }
    handler = table.get(command)
    if handler is None:
        print(__doc__.split("用法", 1)[-1], file=sys.stderr)
        return 2
    return handler(args) or 0


if __name__ == "__main__":
    sys.exit(main())
