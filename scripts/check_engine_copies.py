#!/usr/bin/env python3
"""memory_engine 双份拷贝的差异报告。默认只报告不判错(退出 0),--strict 才把差异当失败。

背景:memory_engine 同时存在两份可导入的活代码——

  A. desktron/lib/memory_engine   仓库自带,随 Desk 分发
  B. ~/work/memory-engine/src/...    pip editable 装的开发副本

哪一份生效取决于 `import memory_engine` 那一刻 sys.path 的状态:app 后台线程里
ui_server 已把 desktron/lib 插到位置 0,命中 A;skill 脚本经 _mem_common 的模块级
早期 import,命中 B。两条路都在跑,谁也不是死代码。

为什么默认不判错:两份的差异有相当一部分是对不同运行环境的**有意适配**,不是漂移。
最典型的是 run_curator——A 在 app 进程内,`curator` 包可 import,于是同进程直接调
`curator.cli.main`,不 spawn 解释器;B 给独立脚本用,不能假设 curator 可 import,
只能走子进程。这类差异不该被判成错。2026-08-21 起初把本脚本写成一律非零退出,
9 个文件全红——一个永远失败的检查等于没有检查,人会学着忽略它,连真漂移一起忽略。

它要解决的真实问题是「看不出两份是否一致」:同一天就踩过,在 B 里改完
dream_dir/resolve_bin 以为修好了调度器,而调度器走的是 A,靠 inspect.getsource
核对 __file__ 才发现。所以这里给的是一份可读的差异清单,改任一份之后扫一眼,
判断该不该同步到另一份——判断仍归人,脚本只负责让差异可见。

用法:
    python3 scripts/check_engine_copies.py            # 报告,恒 0
    python3 scripts/check_engine_copies.py --strict   # 有差异即非零(想强制两份一致时用)
"""
import hashlib
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1] / "desktron" / "lib" / "memory_engine"
DEV = Path.home() / "work" / "memory-engine" / "src" / "memory_engine"
SKIP_DIRS = {"__pycache__", "seeds"}


def collect(root: Path) -> dict:
    if not root.is_dir():
        return {}
    out = {}
    for p in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()[:12]
    return out


def diff_lines(rel: str) -> int:
    try:
        r = subprocess.run(["diff", str(DEV / rel), str(REPO / rel)],
                           capture_output=True, text=True)
        return sum(1 for ln in r.stdout.splitlines() if ln[:1] in "<>")
    except Exception:
        return -1


def main() -> int:
    strict = "--strict" in sys.argv
    a, b = collect(REPO), collect(DEV)
    if not b:
        print(f"跳过:开发副本不存在({DEV});本机只有仓库自带一份,无差异可言")
        return 0
    if not a:
        print(f"✗ 仓库自带副本不存在:{REPO}", file=sys.stderr)
        return 1

    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    differ = sorted(k for k in set(a) & set(b) if a[k] != b[k])

    if not (only_a or only_b or differ):
        print(f"两份 memory_engine 的 {len(a)} 个 .py 完全一致")
        return 0

    print("memory_engine 两份拷贝的差异清单(部分差异是对不同运行环境的有意适配,不等于漏改):")
    print(f"  A 仓库自带(app 进程内): {REPO}")
    print(f"  B 开发副本(独立脚本):   {DEV}")
    print()
    for k in differ:
        n = diff_lines(k)
        print(f"  内容不同  {k:<16} {n if n >= 0 else '?'} 行")
    for k in only_a:
        print(f"  只在 A 有  {k}")
    for k in only_b:
        print(f"  只在 B 有  {k}")
    print()
    print("改过其中一份之后对一遍:属于环境适配的留着,属于修 bug 的同步过去。")
    return 1 if strict else 0


if __name__ == "__main__":
    sys.exit(main())
