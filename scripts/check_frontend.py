#!/usr/bin/env python3
"""前端静态检查门。
无构建体系的最低防线:JS 语法、CSS 括号平衡与孤儿规则、index.html 资源引用存在。
desktron ui 启动前强制执行,失败拒启——B 类根因(语法错静默失效)从调试数小时变为启动报错行号。
退出码:0 通过;1 失败(stderr 给出文件与行号)。
"""
import os
import glob
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FRONTEND = os.path.join(ROOT, "frontend")
LIB = os.path.join(ROOT, "desktron", "lib")
errors = []
STARTED = time.perf_counter()
CACHE = os.environ.get(
    "DESKTRON_FRONTEND_CHECK_CACHE",
    os.path.expanduser("~/.local/share/desktron/data/cache/frontend-check.json"),
)

# index.html 既可以引用 frontend/ 下的实体文件，也可以引用 ui_static 在运行时
# 拼出的虚拟合包。两边共用 frontend_bundle 的声明，避免服务端已经认、启动门却仍按
# “必须有同名实体文件”拒启（2026-08-28 实见：五个合包把 Desk 直接 brick）。
try:
    sys.path.insert(0, LIB)
    from frontend_bundle import ASSET_BUNDLES
except Exception as exc:
    ASSET_BUNDLES = {}
    errors.append(f"前端虚拟合包声明加载失败: {exc}")


def source_fingerprint(node):
    """检查器、前端源码和 node 二进制任一变化都会使启动缓存失效。"""
    paths = [__file__, os.path.join(LIB, "frontend_bundle.py")]
    paths.extend(
        os.path.join(FRONTEND, name)
        for name in sorted(os.listdir(FRONTEND))
        if name.endswith((".js", ".css", ".html"))
    )
    h = hashlib.sha256()
    for path in paths:
        h.update(os.path.relpath(path, ROOT).encode())
        with open(path, "rb") as f:
            while chunk := f.read(128 * 1024):
                h.update(chunk)
    if node:
        real = os.path.realpath(node)
        st = os.stat(real)
        h.update(f"node:{real}:{st.st_size}:{st.st_mtime_ns}".encode())
    else:
        h.update(b"node:none")
    return h.hexdigest()


def cache_hit(fingerprint):
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f).get("fingerprint") == fingerprint
    except (OSError, ValueError, AttributeError):
        return False


def cache_store(fingerprint):
    try:
        parent = os.path.dirname(CACHE)
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="frontend-check.", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"fingerprint": fingerprint}, f)
            os.replace(tmp, CACHE)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass  # 缓存只是加速层；不可写不能改变检查结论。


def check_js(node):
    if not node:
        # node 缺失是环境问题,不是前端缺陷:跳过 JS 语法检查并告警,不拒启。
        # (GUI 启动的 .app 若 PATH 不含 node 会落到这里;不能因 lint 工具缺失而 brick 启动)
        print("  ! node 不可用,跳过 JS 语法检查(CSS/引用检查仍执行)", file=sys.stderr)
        return
    paths = [
        os.path.join(FRONTEND, name)
        for name in sorted(os.listdir(FRONTEND))
        if name.endswith(".js")
    ]
    # node --check 一次只收一个文件。逐片启动 43 个 node 在冷启动上花 1.6~2.5 秒；
    # 用同一个 V8 进程逐文件编译，语义仍是“每片独立脚本”，不把变量作用域串起来。
    program = r"""
const fs = require('fs');
const vm = require('vm');
let failed = false;
for (const path of process.argv.slice(1)) {
  try { new vm.Script(fs.readFileSync(path, 'utf8'), { filename: path }); }
  catch (err) { console.error(err && err.stack ? err.stack : String(err)); failed = true; }
}
if (failed) process.exit(1);
"""
    r = subprocess.run([node, "-e", program, "--", *paths], capture_output=True, text=True)
    if r.returncode != 0:
        errors.append(f"JS 语法错误:\n{r.stderr.strip()[:2000]}")


def check_css():
    for name in sorted(os.listdir(FRONTEND)):
        if not name.endswith(".css"):
            continue
        path = os.path.join(FRONTEND, name)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        # 1) 括号平衡 + 定位首个失衡行
        depth, line = 0, 1
        first_bad = None
        for ch in src:
            if ch == "\n":
                line += 1
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0 and first_bad is None:
                    first_bad = line
                    depth = 0
        if first_bad is not None:
            errors.append(f"CSS 多余 '}}' {name}:{first_bad} 行(其后规则将整块失效)")
        elif depth != 0:
            errors.append(f"CSS 缺少 '}}' {name}(文件末 depth={depth})")
        # 2) 孤儿规则体启发:顶层(花括号外)出现 '属性: 值;' 形态的行
        depth = 0
        for i, ln in enumerate(src.splitlines(), 1):
            stripped = ln.strip()
            opens = ln.count("{")
            closes = ln.count("}")
            if depth == 0 and stripped and not stripped.startswith(("/*", "*", "@", "}")):
                if re.match(r"^[a-z-]+\s*:\s*[^;{]+;", stripped):
                    errors.append(f"CSS 孤儿声明(花括号外) {name}:{i} 行: {stripped[:60]}")
            depth += opens - closes
            if depth < 0:
                depth = 0


def check_html_refs():
    idx = os.path.join(FRONTEND, "index.html")
    try:
        with open(idx, encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        errors.append("index.html 缺失")
        return
    refs = [m.group(1) for m in re.finditer(r'(?:href|src)="(/[^"?]+)', html)]
    for virtual, parts in ASSET_BUNDLES.items():
        if virtual not in refs:
            continue
        if not parts:
            errors.append(f"前端虚拟合包没有组成文件: {virtual}")
            continue
        for part in parts:
            if not os.path.isfile(os.path.join(FRONTEND, part)):
                errors.append(f"前端虚拟合包缺少组成文件: {virtual} -> {part}")
    for ref in refs:
        if ref in ASSET_BUNDLES:
            continue
        rel = ref.lstrip("/")
        if not os.path.exists(os.path.join(FRONTEND, rel)):
            errors.append(f"index.html 引用不存在的资源: /{rel}")


def check_builtin_apps():
    """官方清单与离线静态资产必须完整；缺一项就拒启，避免用户点开后才看到空页。"""
    manifest = os.path.join(FRONTEND, "workbench.json")
    try:
        with open(manifest, encoding="utf-8") as f:
            apps = json.load(f).get("desktopApps") or []
    except Exception as exc:
        errors.append(f"官方 App 清单不可读: {exc}")
        return
    expected = {
        "notepad", "excalidraw", "shell",
        "browser", "files", "file-browser", "taskpaper",
    }
    actual = {a.get("id") for a in apps if isinstance(a, dict)}
    if actual != expected:
        errors.append(f"官方 App 清单应为 {len(expected)} 项，缺少={sorted(expected - actual)}，多出={sorted(actual - expected)}")
    required = [
        "pages/notepad.html", "pages/excalidraw.html", "pages/files.html",
        "pages/file-browser.html", "pages/taskpaper.html",
        "app-assets/excalidraw/vendor/app.bundle.js",
        "app-assets/excalidraw/vendor/index.css",
        "app-assets/excalidraw/vendor/fonts/Virgil/Virgil-Regular.woff2",
        "app-assets/files/vendor/app.bundle.js", "app-assets/files/vendor/app.bundle.css",
        "app-assets/files/vendor/codicon.ttf", "app-assets/files/vendor/editor.worker.js",
        "app-assets/files/vendor/json.worker.js", "app-assets/files/vendor/css.worker.js",
        "app-assets/files/vendor/html.worker.js", "app-assets/files/vendor/ts.worker.js",
    ]
    for rel in required:
        path = os.path.join(FRONTEND, rel)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            errors.append(f"官方 App 静态资产缺失: {rel}（运行 scripts/dev/build-builtin-apps.sh）")
    if not glob.glob(os.path.join(FRONTEND, "app-assets/excalidraw/vendor/locales/zh-CN-*.js")):
        errors.append("官方 App 静态资产缺失: Excalidraw 中文语言包")


def main():
    node = shutil.which("node")
    check_builtin_apps()
    fingerprint = source_fingerprint(node)
    if not errors and cache_hit(fingerprint):
        print(f"前端静态检查缓存命中({time.perf_counter() - STARTED:.3f}s)")
        return 0
    check_js(node)
    check_css()
    check_html_refs()
    if errors:
        print("前端静态检查未通过:", file=sys.stderr)
        for e in errors:
            print(f"  ✗ {e}", file=sys.stderr)
        return 1
    # 检查期间若另一会话正好改了源码，不缓存这一轮；下次会重新完整检查。
    if node and source_fingerprint(node) == fingerprint:
        cache_store(fingerprint)
    print("前端静态检查通过(js 语法 / css 括号与孤儿规则 / 实体与虚拟资源引用;"
          f"{time.perf_counter() - STARTED:.3f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
