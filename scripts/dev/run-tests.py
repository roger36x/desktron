#!/usr/bin/env python3
"""Layered, isolated test runner for Desktron.

Default is the reproducible offline full gate. ``--changed`` selects direct
coverage for edit-time feedback, ``--module`` adds split-module neighbours,
``--native`` runs installed-engine protocol checks, and ``--desktop`` explicitly
opts into serial browser/GUI acceptance. Daily regression never runs GUI tests. One pytest process
per file prevents argv/import/signal leakage; a set keeps overlapping selections
from running the same file twice.
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import importlib.util
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import warnings


ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = ROOT / "desktron" / "tests"
sys.path.insert(0, str(ROOT / "desktron" / "lib"))
import oscompat  # Windows worker groups and command resolution; POSIX is unchanged.

DEFAULT_JOBS = min(8, os.cpu_count() or 4)
SUMMARY_RE = re.compile(r"(\d+) (passed|skipped)")
# 分拆后的源码仍属于同一前端/后端模块。增量选择按模块扩散，避免只命中直接读文件的
# 测试、漏掉通过 _srcmap 读取原合包的契约。
SOURCE_GROUPS = (
    ("frontend/app-chat-", ("_srcmap.chat_src()", "app-chat.js")),
    ("frontend/app-panel-", ("_srcmap.panel_src()", "app-panel.js")),
    ("frontend/app-rail-", ("_srcmap.rail_src()", "app-rail.js")),
    ("frontend/app-assets-", ("_srcmap.assets_src()", "app-assets.js")),
    ("frontend/app-settings-", ("_srcmap.settings_src()", "app-settings.js")),
    ("desktron/lib/engine_adapters/", ("_srcmap.engine_adapters_src()",)),
    ("desktron/lib/ui_", ("_srcmap.ui_server_src()",)),
    ("desktron/lib/term", ("_srcmap.term_src()",)),
)
RUNTIME_PREFIXES = ("frontend/", "desktron/lib/", "desktron/bin/", "scripts/")


def discover_tests(paths: list[str] | None = None) -> list[Path]:
    roots = [Path(p).resolve() for p in paths] if paths else [TEST_ROOT]
    found: set[Path] = set()
    for root in roots:
        if not root.exists():
            raise ValueError(f"测试路径不存在：{root}")
        if root.is_file():
            if root.name.startswith("test_") and root.suffix == ".py":
                found.add(root)
            else:
                raise ValueError(f"不是测试文件：{root}")
            continue
        if root.is_dir():
            found.update(p.resolve() for p in root.rglob("test_*.py"))
    return sorted(found)


def changed_paths(ref: str = "HEAD") -> list[str]:
    """Return tracked and untracked changes once, normalized relative to ROOT."""
    commands = (
        ["git", "diff", "--name-only", "--diff-filter=ACDMR", ref, "--"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    )
    found: set[str] = set()
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        if result.returncode:
            raise ValueError((result.stderr or result.stdout).strip() or "无法读取 Git 改动")
        found.update(line.strip() for line in result.stdout.splitlines() if line.strip())
    return sorted(found)


def _directly_references(source: str, changed: str) -> bool:
    path = Path(changed)
    tokens = {changed, path.name}
    if path.suffix == ".py":
        # Match imports and qualified module use, not arbitrary substrings (pi.py must not match
        # every "compile" or "skip"). Tests named after the module are also direct coverage.
        module = path.stem
        patterns = (
            rf"(?m)^\s*(?:from\s+[\w.]*\b{re.escape(module)}\b\s+import|import\s+[\w., ]*\b{re.escape(module)}\b)",
            rf"\b{re.escape(module)}\.",
        )
        return any(token in source for token in tokens) or any(re.search(p, source) for p in patterns)
    return any(token in source for token in tokens)


def select_changed_tests(changes: list[str], tests: list[Path] | None = None,
                         *, module: bool = False) -> list[Path]:
    """Select a deduplicated direct suite; module=True includes split-bundle neighbours."""
    tests = discover_tests() if tests is None else tests
    normalized = {Path(path).as_posix().lstrip("./") for path in changes}
    if any(path.startswith("desktron/tests/")
           and (Path(path).name == "conftest.py" or Path(path).name.startswith("_"))
           for path in normalized):
        return tests
    selected: set[Path] = set()
    for test in tests:
        rel = test.relative_to(ROOT).as_posix()
        if rel in normalized:
            selected.add(test)
            continue
        source = test.read_text(encoding="utf-8", errors="ignore")
        for changed in normalized:
            if _directly_references(source, changed):
                selected.add(test)
                break
            markers = next((items for prefix, items in SOURCE_GROUPS if changed.startswith(prefix)), ())
            if module and any(marker in source for marker in markers):
                selected.add(test)
                break
    return sorted(selected)


def _has_marker(path: Path, name: str) -> bool:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return False
    for node in ast.walk(tree):
        decorators = list(getattr(node, 'decorator_list', ()))
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == 'pytestmark' for target in node.targets):
            decorators.extend(node.value.elts if isinstance(node.value, (ast.List, ast.Tuple))
                              else [node.value])
        for decorator in decorators:
            if (isinstance(decorator, ast.Attribute) and decorator.attr == name
                    and isinstance(decorator.value, ast.Attribute)
                    and decorator.value.attr == "mark"
                    and isinstance(decorator.value.value, ast.Name)
                    and decorator.value.value.id == "pytest"):
                return True
    return False


def _has_native_marker(path: Path) -> bool:
    return _has_marker(path, 'native')


def native_tests(tests: list[Path]) -> list[Path]:
    return [path for path in tests if _has_native_marker(path)]


def desktop_tests(tests: list[Path]) -> list[Path]:
    return [path for path in tests if _has_marker(path, 'desktop')]


DEFAULT_MARKER = 'not native and not desktop'


def parse_counts(output: str) -> tuple[int, int]:
    counts = {"passed": 0, "skipped": 0}
    for amount, kind in SUMMARY_RE.findall(output):
        counts[kind] += int(amount)
    return counts["passed"], counts["skipped"]


def worker_env(marker: str = DEFAULT_MARKER) -> dict[str, str]:
    env = os.environ.copy()
    # A developer's -k/-m or --lf must not silently narrow the full gate.
    env.pop("PYTEST_ADDOPTS", None)
    # Ambient opt-in variables must never turn daily regression into GUI work.
    for name in ('DESKTRON_DESKTOP_TESTS', 'DESK_WEBKIT_TESTS', 'DESK_SHEPHERD_IMAGE_UI', 'PWDEBUG'):
        env.pop(name, None)
    if marker == 'desktop':
        env['DESKTRON_DESKTOP_TESTS'] = '1'
        env['DESK_WEBKIT_TESTS'] = '1'
        env['DESK_SHEPHERD_IMAGE_UI'] = '1'
    # Native fixtures own their writer identity; never inherit the hosting Desk
    # conversation's lock name into otherwise isolated test data directories.
    env.pop("DESKTRON_NATIVE_WRITER_SESSION_ID", None)
    env.pop("DESKTRON_DATA_HOME", None)  # conftest owns an isolated data root.
    paths = [str(ROOT / "desktron" / "lib"), str(TEST_ROOT)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONHASHSEED", "0")
    return env


def run_worker(path: Path, marker: str = DEFAULT_MARKER) -> int:
    # deskcfg is intentionally argv-driven at import time.  Give isolated tests
    # the same valid five-field contract as ui_server before pytest collects.
    sys.argv = ["test", str(ROOT / "frontend"), "0", "0", str(ROOT)]
    try:
        import pytest
    except ImportError:
        print("pytest 未安装；先运行 python3 -m pip install pytest", file=sys.stderr)
        return 2
    # 外部临时用例不能以临时目录的共同祖先为根，否则会扫描整台机器的临时文件。
    path = path.resolve()
    test_root = ROOT if path.is_relative_to(ROOT) else path.parent
    class CollectionCount:
        count = 0

        @pytest.hookimpl(tryfirst=True)
        def pytest_collection_modifyitems(self, items):
            # Count before pytest's marker filter removes GUI/native items.
            self.count = len(items)

    collected = CollectionCount()
    code = int(pytest.main(["-q", "--tb=short", "--color=no", "--rootdir", str(test_root),
                            "-p", "no:cacheprovider", "-o", "addopts=",
                            "-o", "markers=native: explicit installed-engine protocol test\ndesktop: explicit browser or native GUI test",
                            "-m", marker, str(path)], plugins=[collected]))
    # 全文件都是 native 时，默认离线层会全部 deselect，pytest 返回 5。该文件不是空测试，
    # 不应让 full 失败；真正的空测试文件仍保留退出码 5，由门禁拒绝。
    if code == 5 and collected.count > 0 and marker == DEFAULT_MARKER and (
            _has_native_marker(path) or _has_marker(path, 'desktop')):
        print('桌面/原生用例已排除；使用 --desktop / --native 显式验收')
        return 0
    return code


def run_file(path: Path, timeout: float,
             cancelled: threading.Event | None = None,
             marker: str = DEFAULT_MARKER) -> tuple[Path, int, int, int, float, str]:
    started = time.monotonic()
    if cancelled is not None and cancelled.is_set():
        return path, 130, 0, 0, 0.0, "已取消"
    try:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", str(path),
             "--marker", marker],
            cwd=ROOT,
            env=worker_env(marker),
            text=True, encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        return path, 2, 0, 0, time.monotonic() - started, str(exc)
    while True:
        stopped = cancelled is not None and cancelled.is_set()
        remaining = timeout - (time.monotonic() - started)
        if stopped or remaining <= 0:
            # Each worker owns a separate process group, including its Node/shell
            # children. Kill only that group so an interrupted run leaves no tests.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            output, _ = proc.communicate()
            code = 130 if stopped else 124
            output += "\n已取消\n" if stopped else f"\n超时：{timeout:g} 秒\n"
            break
        try:
            output, _ = proc.communicate(timeout=min(0.2, remaining))
            code = proc.returncode
            break
        except subprocess.TimeoutExpired:
            continue
    passed, skipped = parse_counts(output)
    return path, code, passed, skipped, time.monotonic() - started, output


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="隔离并行运行 Desktron 测试")
    ap.add_argument("paths", nargs="*", help="可选的测试文件或目录")
    ap.add_argument("-j", "--jobs", type=int, default=None)
    ap.add_argument("--timeout", type=float, default=None, help="单文件超时秒数（默认 120，可用 DESKTRON_TEST_TIMEOUT 覆盖；native 600）")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--changed", nargs="?", const="HEAD", metavar="REF",
                      help="quick：只运行 REF 到当前工作树改动直接影响的测试")
    mode.add_argument("--module", nargs="?", const="HEAD", metavar="REF",
                      help="module：在 --changed 基础上包含分拆合包的邻接测试")
    mode.add_argument("--native", action="store_true",
                      help="只运行显式标记的真实引擎/原生协议慢测")
    mode.add_argument('--desktop', action='store_true',
                      help='显式运行浏览器/原生窗口测试（可能影响程序坞，固定单路）')
    ap.add_argument("--list", action="store_true", help="只列出会运行的测试文件")
    ap.add_argument("--worker", metavar="FILE", help=argparse.SUPPRESS)
    ap.add_argument("--marker", default=DEFAULT_MARKER, help=argparse.SUPPRESS)
    return ap


def resolve_jobs(args) -> int:
    """Native files share installed-engine resident pools; serialize by default."""
    if args.desktop:
        return 1
    if args.jobs is not None:
        return args.jobs
    if args.native:
        return 1
    try:
        return int(os.environ.get("DESKTRON_TEST_JOBS", DEFAULT_JOBS))
    except ValueError:
        return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    args.jobs = resolve_jobs(args)
    if args.worker:
        return run_worker(Path(args.worker), args.marker)
    change_ref = args.changed if args.changed is not None else args.module
    if change_ref is not None and args.paths:
        print("--changed/--module 不能与显式测试路径同时使用", file=sys.stderr)
        return 2
    if args.timeout is not None:
        timeout = args.timeout
    elif args.native:
        timeout = 600
    else:
        try:
            timeout = float(os.environ.get("DESKTRON_TEST_TIMEOUT", 120))
        except ValueError:
            timeout = 0
    if args.jobs < 1 or not math.isfinite(timeout) or timeout <= 0:
        print("--jobs 和 --timeout 必须为正数", file=sys.stderr)
        return 2
    try:
        files = discover_tests(args.paths)
        if change_ref is not None:
            changes = changed_paths(change_ref)
            files = select_changed_tests(changes, files, module=args.module is not None)
            if not files:
                runtime = [path for path in changes if path.startswith(RUNTIME_PREFIXES)]
                if runtime:
                    raise ValueError("运行时代码有改动但未找到对应测试：" + ", ".join(runtime))
                print("改动不涉及可执行代码，无需运行 pytest")
                return 0
        elif args.native:
            files = native_tests(files)
        elif args.desktop:
            files = desktop_tests(files)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not files:
        print("没有找到测试文件", file=sys.stderr)
        return 2
    if args.list:
        for path in files:
            print(os.path.relpath(path, ROOT))
        return 0
    if importlib.util.find_spec("pytest") is None:
        print("pytest 未安装；先运行 python3 -m pip install pytest", file=sys.stderr)
        return 2

    if args.desktop:
        print('桌面验收：可能短暂显示窗口或程序坞图标；浏览器测试固定单路。')
    else:
        desktop_files = len(desktop_tests(files))
        print(f'后台回归：排除桌面浏览器用例（涉及 {desktop_files} 个文件）；另用 --desktop 显式验收。')
    started = time.monotonic()
    results = []
    cancelled = threading.Event()
    old_handlers = {sig: signal.signal(sig, lambda *_: cancelled.set())
                    for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            marker = 'desktop' if args.desktop else 'native and not desktop' if args.native else DEFAULT_MARKER
            futures = {pool.submit(run_file, path, timeout, cancelled, marker): path for path in files}
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                results.append(result)
                if not cancelled.is_set():
                    print("." if result[1] == 0 else "F", end="", flush=True)
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    print()
    if cancelled.is_set():
        print("测试已中断，工作进程已清理", file=sys.stderr)
        return 130

    failures = sorted((r for r in results if r[1]), key=lambda r: str(r[0]))
    for path, code, _passed, _skipped, duration, output in failures:
        print(f"\nFAIL {os.path.relpath(path, ROOT)} ({duration:.2f}s, exit {code})")
        print(output.rstrip())

    slow = sorted(results, key=lambda r: r[4], reverse=True)[:10]
    elapsed = time.monotonic() - started
    print(
        f"{len(files)} files: {sum(r[2] for r in results)} passed, "
        f"{sum(r[3] for r in results)} skipped, {len(failures)} failed files; "
        f"{elapsed:.2f}s with {args.jobs} workers"
    )
    print("Slowest files: " + ", ".join(
        f"{r[0].name} {r[4]:.1f}s" for r in slow if r[4] >= 1
    ))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
