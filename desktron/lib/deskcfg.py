"""desktron ui_server 的配置基座:argv / env 派生的常量与无状态共享工具。

被 ui_server.py 及拆出的各职责模块(workbench/assets/settings/term/engine)统一 import,
避免循环依赖。这里只放纯常量与无副作用工具(外加一次 _augment_path() 对 PATH 的补全);
有运行态可变状态的(引擎进程、会话表)留各自模块。

注:与 ui_server.py 一样,本模块在 import 时读 sys.argv,只在作为 ui_server 进程的
依赖被加载时有效(脚本式启动);不做防御性兜底,保持与原行为一致。
"""
import sys, os, time

from protected_paths import PROTECTED_DIRS, is_protected_dir

# 本进程的起点。开机头一分钟是页面取首屏数据的同一段时间,几处后台工作按它避让
# (网络探测等);放基座层,谁都能读,不必各自记一份。
PROCESS_STARTED = time.monotonic()

# argv[3] 是已停用的引擎服务端口占位。为兼容壳与测试的既有 argv 契约暂时保留,
# 当前运行时不读取这一位。
FRONTEND, UI_PORT = sys.argv[1], int(sys.argv[2])
REPO_ROOT = sys.argv[4] if len(sys.argv) > 4 else os.getcwd()
# 开发态:仓库带 .git(发行 payload 排除 .git)。开发态下前端可自动重载,免重启打断 agent。
_DEV = os.path.isdir(os.path.join(REPO_ROOT, ".git"))
DATA_HOME = os.environ.get("DESKTRON_DATA_HOME",
                           os.path.expanduser("~/.local/share/desktron/data"))
# 引擎注册表(静态元数据)。放基座层:engine 与 workbench 都要读它,放任一方都会成环。
ENGINES = {
    "claude": {"label": "Claude Code"},
    "codex": {"label": "Codex"},
    "pi": {"label": "Pi Coding"},
}


# macOS TCC 与 Windows 自动遍历保护目录由 protected_paths 统一定义。
# 界面仍只阻止自动扫描；用户主动打开目录的行为保持不变。


def _dev_token():
    """前端可编辑文件的最新 mtime(ms):变了 = 前端改过,触发 webview reload。
    监视顶层所有 .js/.css/.html(app.js 已按区域拆成 app-*.js,逐一监视)+ pages 目录。"""
    mt = 0.0
    try:
        for fn in os.listdir(FRONTEND):
            if fn.endswith((".js", ".css", ".html")):
                try:
                    mt = max(mt, os.path.getmtime(os.path.join(FRONTEND, fn)))
                except Exception:
                    pass
    except Exception:
        pass
    try:
        pg = os.path.join(FRONTEND, "pages")
        for fn in os.listdir(pg):
            mt = max(mt, os.path.getmtime(os.path.join(pg, fn)))
    except Exception:
        pass
    return int(mt * 1000)


def _augment_path():
    """GUI 点图标启动的 ui_server 不经 shell rc,PATH 缺用户级 bin(nvm/asdf 等惰性注入的)。
    先补 ~/.local/bin(claude/codex 等引擎 CLI 默认装在此;GUI 受限 PATH 不含它),
    再探测 nvm 已装 node 的 bin,均 append 进 PATH(不抢系统/brew 优先,只兜底补),
    让这些引擎 CLI 既能被检测到、也能被启动。default 版本排前。
    从 terminal 起时 PATH 已含则此处去重无副作用。"""
    # Desk 自动准备的 Node 不依赖 Homebrew，也不改用户 shell；双击启动时需主动补进 PATH。
    try:
        from provision.setup_runtime import node_command
        node = node_command()
    except Exception:
        node = ""
    _cur = os.environ.get("PATH", "")
    if node:
        path = os.path.dirname(node)
        if path not in _cur.split(os.pathsep):
            os.environ["PATH"] = path + os.pathsep + _cur
    # 用户级 bin:双击 .app 的受限 PATH(/usr/bin:/bin:...)不含它,append 兜底
    _local = os.path.expanduser("~/.local/bin")
    _cur = os.environ.get("PATH", "")
    if os.path.isdir(_local) and _local not in _cur.split(os.pathsep):
        os.environ["PATH"] = (_cur + os.pathsep + _local) if _cur else _local
    nvm = os.path.expanduser("~/.nvm/versions/node")
    if not os.path.isdir(nvm):
        return
    try:
        default = ""
        af = os.path.expanduser("~/.nvm/alias/default")
        if os.path.exists(af):
            with open(af, encoding="utf-8") as f:
                default = f.read().strip().lstrip("v")
        vers = sorted((d for d in os.listdir(nvm)
                       if os.path.isdir(os.path.join(nvm, d, "bin"))), reverse=True)
        vers.sort(key=lambda v: v.lstrip("v") != default)   # default 版本提前(稳定排序)
        cur = os.environ.get("PATH", "")
        have = set(cur.split(os.pathsep))
        add = [os.path.join(nvm, v, "bin") for v in vers
               if os.path.join(nvm, v, "bin") not in have]
        if add:
            os.environ["PATH"] = os.pathsep.join([cur, *add]) if cur else os.pathsep.join(add)
    except Exception:
        pass


_augment_path()

def log(mod, msg):
    """统一进程日志:[模块] 消息,一行一条。时间戳由 logbuf 捕获层统一加,这里不重复。
    模块名是日志悬浮窗(logdock)的过滤键,新增后台输出一律经此,不再裸 print。"""
    print(f"[{mod}] {msg}", flush=True)
