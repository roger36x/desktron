"""memory-engine CLI:tick(手动跑一轮)/ status(查调度器死活)/ journal(流水读写)。

单独使用形态:`memory-engine --root <数据根> tick`。宿主(如 Desk)常驻由
scheduler.start_background(host) 起。会话 recap 由 Desk 引擎适配器负责,不在本 CLI 扫描。
"""
import argparse
import os
import sys

from . import journal as journal_mod
from . import providers, scheduler


def _host(root):
    return providers.Host(repo_root=root)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="memory-engine", description="记忆引擎(curator 内核之上)")
    ap.add_argument("--root", help="数据根(默认 MEMORY_ENGINE_ROOT 或 cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tick", help="手动跑一轮 dreaming(候选分析+管家)").add_argument(
        "--quiet", action="store_true", help="不打印报告正文")
    sub.add_parser("status", help="调度器工作状态(上次 tick/失败痕/报告)")
    jp = sub.add_parser("journal", help="s0 流水追加")
    jp.add_argument("--ws", required=True)
    jp.add_argument("--note", required=True)
    jp.add_argument("--key")
    a = ap.parse_args(argv)

    root = os.path.abspath(a.root or os.environ.get("MEMORY_ENGINE_ROOT")
                           or os.environ.get("DESKTRON_REPO_ROOT") or os.getcwd())
    host = _host(root)
    if a.cmd == "tick":
        scheduler.tick(host, verbose=not a.quiet)
        return 0
    if a.cmd == "status":
        print("\n".join(scheduler.status_lines(host)))
        return 0
    if a.cmd == "journal":
        p = journal_mod.append(host.mem_root, a.ws, a.note, key=a.key)
        print(f"journal += {a.note}\n  {p}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
