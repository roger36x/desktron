#!/usr/bin/env python3
"""构建随 Desktron 发行的 Excalidraw 与 Monaco IDE 静态资产(跨平台)。

与 build-builtin-apps.sh 等价；Windows 没有 bash 时由 sync-home 与发行打包调用。
"""
import glob
import os
import shutil
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
APP_ROOT = os.path.join(ROOT, "frontend", "app-assets")
REQUIRED = (
    "excalidraw/vendor/app.bundle.js", "excalidraw/vendor/index.css",
    "excalidraw/vendor/fonts/Virgil/Virgil-Regular.woff2",
    "files/vendor/app.bundle.js", "files/vendor/app.bundle.css", "files/vendor/codicon.ttf",
    "files/vendor/editor.worker.js", "files/vendor/json.worker.js", "files/vendor/css.worker.js",
    "files/vendor/html.worker.js", "files/vendor/ts.worker.js",
)


def main(argv):
    out_root = os.path.abspath(argv[0]) if argv else APP_ROOT
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        print("缺少 node 或 npm，无法构建内置 App", file=sys.stderr)
        return 1
    modules = os.path.join(APP_ROOT, "node_modules")
    if not all(os.path.isdir(os.path.join(modules, *p.split("/")))
               for p in ("@excalidraw/excalidraw", "monaco-editor", "esbuild")):
        print("→ 安装内置 App 的锁定构建依赖")
        # 必须在包目录内运行 npm ci，原因见 build-builtin-apps.sh。
        if subprocess.run([npm, "ci", "--ignore-scripts"], cwd=APP_ROOT).returncode != 0:
            return 1
    outputs = {"excalidraw": os.path.join(out_root, "excalidraw", "vendor"),
               "files": os.path.join(out_root, "files", "vendor")}
    for target in outputs.values():
        shutil.rmtree(target, ignore_errors=True)
        os.makedirs(target)
    for name, target in outputs.items():
        script = os.path.join(APP_ROOT, name, "src", "build.mjs")
        if subprocess.run([node, script, target]).returncode != 0:
            return 1
    for rel in REQUIRED:
        path = os.path.join(out_root, *rel.split("/"))
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            print("内置 App 构建产物缺失或为空: " + rel, file=sys.stderr)
            return 1
    if not glob.glob(os.path.join(outputs["excalidraw"], "locales", "zh-CN-*.js")):
        print("内置 App 构建产物缺失: excalidraw/vendor/locales/zh-CN-*.js", file=sys.stderr)
        return 1
    print("→ 2 个内置 App 的静态资产已就绪")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
