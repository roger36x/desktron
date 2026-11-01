"""curator trace --id —— 重建一个对象的生命周期时间线。

身份(id)是贯穿始终的 through-line。数据源拼三处:
- frontmatter 当前态(type / tier / confidence / created / next_review ...)。
- sidecar 事件日志(created / promoted / verified / removed),跨 band 与跨 git 线都覆盖,
  git 外对象也有(它们没有 git 历史)。
- git 内对象额外补 git log(其生命线由 commit 天然记录)。
- 访问计数(sidecar)。

只读。对象即便已 rm,事件墓碑仍在,trace 仍可回溯。
"""

from __future__ import annotations

import os
import subprocess

from ..config import Instance
from ..state import read_access, read_events


def _git_log(inst: Instance, rel: str) -> list[str]:
    r = subprocess.run(
        ["git", "log", "--follow", "--date=short", "--format=%h %ad %s", "--", rel],
        cwd=inst.root,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.splitlines() if line.strip()]


def run(inst: Instance, args) -> int:
    found = inst.find_by_id(args.id)
    events = read_events(inst, args.id)
    acc = read_access(inst, args.id)

    if not found and not events:
        print(f"无 {args.id} 的任何踪迹(笔记不存在、无事件)")
        return 2

    print(f"trace {args.id}")

    if found:
        path, fm, _ = found
        rel = os.path.relpath(path, inst.root)
        bits = [
            f"桶 {fm.get('tier')}",
            f"kind {fm.get('kind')}",
        ]
        if fm.get("confidence"):
            bits.append(f"confidence {fm['confidence']}")
        for k in ("created", "last_verified", "next_review", "review_every"):
            if fm.get(k):
                bits.append(f"{k} {fm[k]}")
        print(f"当前: {' · '.join(bits)}")
        print(f"  文件: {rel}")
    else:
        print("当前: (已删除,仅存事件墓碑)")
        rel = None

    if events:
        print("事件(sidecar):")
        for e in events:
            extra = " ".join(
                f"{k}={v}" for k, v in e.items() if k not in ("id", "event", "date")
            )
            print(f"  {e.get('date', '—')}  {e.get('event')}" + (f"  ({extra})" if extra else ""))

    if acc:
        print(f"访问: {acc.get('count', 0)} 次,最近 {acc.get('last_access', '—')}")

    # git 内对象补 git 历史
    if found and rel and inst.is_git_inside_path(path):
        log = _git_log(inst, rel)
        if log:
            print("git 历史(git 内):")
            for line in log:
                print(f"  {line}")

    return 0


def add_parser(sub):
    p = sub.add_parser("trace", help="重建一个对象的生命周期时间线(只读)")
    p.add_argument("--id", required=True)
    p.set_defaults(func=run)
