"""Desk 记忆调度器接线(薄壳):项目模型生成与候选分析都在 memory-engine。

调度语义(项目模型生成/候选分析/可观测契约)见 memory_engine.scheduler。env 开关沿用 DESKTRON_*(引擎按
MEMORY_ENGINE_* 优先、DESKTRON_* 兼容的顺序读)。

CLI(与旧版兼容):
    python3 desktron/lib/memory_scheduler.py --once [--root <数据根>] / --status
"""


def _host():
    from memory_host import build_host
    return build_host()


def dream_tick(verbose=False):
    from memory_engine import scheduler
    import self_update
    with self_update.tracked_work(restart_blocking=False) as admitted:
        return scheduler.tick(_host(), verbose=verbose) if admitted else None


def start_background():
    """ui_server 调用:daemon 线程按间隔 tick(首轮延后、失败落 last-error 短间隔重试)。

    备忘经 Host.memos 注入，与 journal、上一版项目模型一起生成新的完整项目模型。"""
    from memory_engine import scheduler
    import self_update
    return scheduler.start_background(
        _host(), guard=lambda: self_update.tracked_work(restart_blocking=False))


if __name__ == "__main__":
    import argparse
    import os
    import sys
    ap = argparse.ArgumentParser(description="Desk 记忆调度器(dreaming 位,委托 memory-engine)")
    ap.add_argument("--once", action="store_true", help="手动跑一轮并打印报告")
    ap.add_argument("--status", action="store_true", help="查调度器工作状态(上次 tick/失败痕/报告)")
    ap.add_argument("--rebuild", action="store_true", help="全量重建各工作区项目记忆")
    ap.add_argument("--reset-legacy", action="store_true", help="重建成功后清理该工作区旧 notes/SOT/journal")
    ap.add_argument("--workspace", action="append", help="仅重建指定工作区，可重复")
    ap.add_argument("--root", help="数据根(默认 DESKTRON_REPO_ROOT 或 cwd)")
    a = ap.parse_args()
    root = os.path.abspath(a.root or os.environ.get("DESKTRON_REPO_ROOT") or os.getcwd())
    sys.argv = [sys.argv[0], "cli", "0", "0", root]   # deskcfg 的进程形状契约
    from memory_engine import scheduler
    if a.once:
        scheduler.tick(_host(), verbose=True)
        raise SystemExit(0)
    if a.status:
        print("\n".join(scheduler.status_lines(_host())))
        raise SystemExit(0)
    if a.rebuild:
        from memory_engine import project_memory
        if a.workspace:
            lines = []
            for i in range(0, len(a.workspace), project_memory.MAX_WS):
                lines.append(project_memory.refresh(_host(), workspaces=a.workspace[i:i + project_memory.MAX_WS],
                                                    force=True, reset_legacy=a.reset_legacy))
            print("\n".join(lines))
        else:
            print("\n".join(project_memory.rebuild_all(_host(), reset_legacy=a.reset_legacy)))
        raise SystemExit(0)
    print("用 --once 手动跑、--rebuild 全量重建、--status 查状态;常驻由 ui_server 起(start_background)")
