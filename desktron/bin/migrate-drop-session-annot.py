#!/usr/bin/env python3
"""一次性迁移:清掉 TASKS.md 里的 `s:` 注记(任务↔会话绑定下线)。

任务不再归属会话之后,`s:` 只剩历史意义,而 read_tasks 仍会把它读进条目、
_fmt_task_line 仍会把它写回,留着就会一直往前带。清掉的同时其余注记原样保留。

用法:
    migrate-drop-session-annot.py <workspace>            # 预演,只报会改哪些行
    migrate-drop-session-annot.py <workspace> --apply    # 实改,先写 .bak
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import tasks  # noqa: E402


def scan(path):
    """返回 [(行号, 原行, 新行)];只列真的带 s: 的任务行。"""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    out = []
    for i, raw in enumerate(lines):
        parsed = tasks._split_task_line(raw)
        if not parsed:
            continue
        ch, title, annot = parsed
        if not annot.get("s"):
            continue
        annot = dict(annot)
        annot.pop("s")
        out.append((i, raw, tasks._fmt_task_line(ch, title, annot)))
    return lines, out


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    ws = os.path.abspath(argv[0])
    apply = "--apply" in argv[1:]
    root = os.path.join(ws, "domains")
    if not os.path.isdir(root):
        print(f"没有 {root}")
        return 1

    total = 0
    for dom in sorted(os.listdir(root)):
        path = os.path.join(root, dom, tasks.TASKS_MD_NAME)
        if not os.path.isfile(path):
            continue
        lines, hits = scan(path)
        if not hits:
            continue
        total += len(hits)
        print(f"{dom}: {len(hits)} 行")
        for i, old, new in hits:
            print(f"  {i + 1}: {old.strip()[:90]}")
            lines[i] = new
        if apply:
            shutil.copy2(path, path + ".bak")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")

    print(f"\n合计 {total} 行" + ("(已改,原文件留 .bak)" if apply else "(预演,未落盘;加 --apply 实改)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
