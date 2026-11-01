"""按 PATH 找能直接启动的命令。

shutil.which 只看可执行位。npm 安装 Claude Code 时若跳过了平台原生的可选依赖，
bin/claude.exe 会是一段没有 shebang 的提示脚本。Python 按命令名启动时遇到 ENOEXEC
会继续搜索 PATH，所以普通 Claude 会话看起来正常；按绝对路径启动(Node spawn、
subprocess 传入 which 的结果)则直接失败。2026-10-01 实见：/usr/local 下的残缺安装
排在 PATH 前面，Shepherd 因此报「无法确认协议能力」。

这里跳过这类文件并记一行日志，继续找 PATH 上的下一个；全都不可用时返回 None，
由调用方按「未安装」报错。
"""
import os
import sys
import threading

# 能被内核直接执行的文件头：脚本 shebang、ELF、Mach-O(32/64 位、两种字节序)与通用二进制。
_HEADERS = (b"#!", b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
            b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")
_reported = set()
_reported_lock = threading.Lock()


def directly_executable(path):
    try:
        with open(path, "rb") as stream:
            head = stream.read(4)
    except OSError:
        return False
    return head.startswith(_HEADERS)


def _report_skip(path):
    with _reported_lock:
        if path in _reported:
            return
        _reported.add(path)
    message = "跳过无法直接执行的命令:%s(缺少 shebang 或不是可执行程序,可能是残缺安装)" % path
    try:
        import deskcfg
        deskcfg.log("cli", message)
    except Exception:
        print("[cli] " + message, file=sys.stderr)


def _windows_candidates(directory, name):
    """Windows 按 PATHEXT 顺序展开候选；已有扩展名时只取原名。

    WindowsApps 下的 python/python3 是商店占位程序，不是可用解释器，跳过。
    """
    if "windowsapps" in directory.lower():
        return []
    base = os.path.join(directory or os.curdir, name)
    if os.path.splitext(name)[1]:
        return [base]
    exts = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
    return [base + ext.lower() for ext in exts]


def _windows_executable(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".exe", ".com"):
        try:
            with open(path, "rb") as stream:
                return stream.read(2) == b"MZ"
        except OSError:
            return False
    return ext in (".cmd", ".bat")


def which(name, path=None):
    search = os.environ.get("PATH", os.defpath) if path is None else path
    if sys.platform == "win32":
        for directory in search.split(os.pathsep):
            for candidate in _windows_candidates(directory, name):
                if not os.path.isfile(candidate):
                    continue
                if _windows_executable(candidate):
                    return candidate
                _report_skip(candidate)
        return None
    for directory in search.split(os.pathsep):
        candidate = os.path.join(directory or os.curdir, name)
        if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
            continue
        if directly_executable(candidate):
            return candidate
        _report_skip(candidate)
    return None
