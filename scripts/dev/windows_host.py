#!/usr/bin/env python3
"""构建并安装 Windows 原生壳(开发态 `desktron app-install` 在 Windows 上的实现)。

- 构建：`cargo build --release`(desktop/windows)，产物 Desktron.exe；
- 安装：复制到 %LOCALAPPDATA%\\Programs\\Desktron，旁边写 release-rev / release-build /
  desktron_home(开发态指向源码仓，对应 macOS .app 的 Resources/desktron_home)；
- 目标正在运行时拒绝覆盖：Windows 不允许替换运行中的可执行文件，且替换会中断回合。
"""
import os
import shutil
import subprocess
import sys

CRATE = os.path.join("desktop", "windows")


def git_rev(root):
    try:
        out = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        rev = out.stdout.strip()
        return rev if out.returncode == 0 and rev else "dev"
    except Exception:
        return "dev"


def build_exe(root, target=None):
    """构建 Desktron.exe，返回路径。target 为空时使用本机默认目标。"""
    crate = os.path.join(root, CRATE)
    argv = ["cargo", "build", "--release", "--locked"] if os.path.exists(os.path.join(crate, "Cargo.lock")) \
        else ["cargo", "build", "--release"]
    if target:
        argv += ["--target", target]
    result = subprocess.run(argv, cwd=crate)
    if result.returncode != 0:
        raise RuntimeError("cargo build 失败")
    parts = [crate, "target"] + ([target] if target else []) + ["release", "Desktron.exe"]
    exe = os.path.join(*parts)
    if not os.path.isfile(exe):
        raise RuntimeError("构建完成但找不到 Desktron.exe")
    return exe


def install_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Programs", "Desktron")


def running(app_dir):
    """目标目录下的 Desktron.exe 是否正在运行(按进程映像路径判断)。"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "desktron", "lib"))
    import oscompat
    target = os.path.normcase(os.path.join(app_dir, "Desktron.exe"))
    for pid, _ppid, name in oscompat.process_table():
        if name.lower() != "desktron.exe":
            continue
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "(Get-Process -Id %d).Path" % pid], capture_output=True, text=True, timeout=10)
            if os.path.normcase(out.stdout.strip()) == target:
                return True
        except Exception:
            return True   # 无法确认时按运行中处理，拒绝覆盖
    return False


def write_resources(folder, rev, build, home=None):
    with open(os.path.join(folder, "release-rev"), "w", encoding="utf-8", newline="\n") as stream:
        stream.write(rev + "\n")
    with open(os.path.join(folder, "release-build"), "w", encoding="utf-8", newline="\n") as stream:
        stream.write(str(build) + "\n")
    if home:
        with open(os.path.join(folder, "desktron_home"), "w", encoding="utf-8", newline="\n") as stream:
            stream.write(home)


def build_and_install(root, args):
    if sys.platform != "win32":
        print("desktron: Windows 原生壳只能在 Windows 上安装；macOS 请使用 Swift 壳。", file=sys.stderr)
        return 2
    if args and args[0] == "--activate":
        print("desktron: Windows 版暂不支持经宿主事务热替换原生壳；请在应用内退出后运行 app-install。",
              file=sys.stderr)
        return 2
    app_dir = install_dir()
    if running(app_dir):
        print("desktron: 目标原生壳正在运行，拒绝原地覆盖；请先在应用内退出 Desktron。", file=sys.stderr)
        return 2
    exe = build_exe(root)
    os.makedirs(app_dir, exist_ok=True)
    staged = os.path.join(app_dir, "Desktron.exe.new")
    shutil.copy2(exe, staged)
    os.replace(staged, os.path.join(app_dir, "Desktron.exe"))
    write_resources(app_dir, os.environ.get("DESKTRON_APP_REV") or git_rev(root),
                    os.environ.get("DESKTRON_APP_BUILD") or "1", home=root)
    print("已生成 %s(Windows 原生壳)。仓库路径已写入 desktron_home，移动仓库后重新 app-install。"
          % os.path.join(app_dir, "Desktron.exe"))
    return 0
