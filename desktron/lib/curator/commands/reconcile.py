"""curator reconcile —— 对账 events.jsonl 与文件系统,报告脱节,可选 --fix 补事件。"""

from __future__ import annotations

import os

from ..config import Instance
from ..note import read_note
from ..state import read_events, record_event
from ..util import CuratorError


def _expected_ids(events: list) -> set[str]:
    """从事件流推算当前应存在的 id 集合:created 加入,removed 移除。"""
    alive: set[str] = set()
    for ev in events:
        nid = ev.get("id")
        if not nid:
            continue
        etype = ev.get("event", "")
        if etype in ("created", "reconciled_created"):
            alive.add(nid)
        elif etype in ("removed", "reconciled_removed"):
            alive.discard(nid)
    return alive


def _actual_ids(inst: Instance) -> dict[str, str]:
    """扫子 KB + 工作层,返回 {id: path}。"""
    out: dict[str, str] = {}
    for p in inst.note_paths():
        try:
            fm, _ = read_note(p)
        except Exception:
            continue
        nid = fm.get("id")
        if nid:
            out[nid] = p
    return out


def run(inst: Instance, args) -> int:
    events = read_events(inst)
    expected = _expected_ids(events)
    actual = _actual_ids(inst)

    orphan_events = sorted(expected - set(actual))
    untracked_files = sorted(set(actual) - expected)

    if not orphan_events and not untracked_files:
        print("events.jsonl 与文件系统一致,无差异")
        return 0

    if orphan_events:
        print(f"orphan_event({len(orphan_events)}):事件存在但文件不存在")
        for nid in orphan_events:
            print(f"  {nid}")
    if untracked_files:
        print(f"untracked_file({len(untracked_files)}):文件存在但无对应事件")
        for nid in untracked_files:
            rel = os.path.relpath(actual[nid], inst.root)
            print(f"  {nid}  {rel}")

    if not args.fix:
        print(f"\n共 {len(orphan_events)} orphan + {len(untracked_files)} untracked,加 --fix 自动补事件")
        return 1

    for nid in orphan_events:
        record_event(inst, nid, "reconciled_removed", reason="文件不存在,对账补记")
    for nid in untracked_files:
        fm, _ = read_note(actual[nid])
        record_event(inst, nid, "reconciled_created",
                     reason="文件存在但无事件,对账补记",
                     kind=fm.get("kind"), tier=fm.get("tier"), sub_kb=fm.get("sub_kb"))
    print(f"已补 {len(orphan_events)} 条 reconciled_removed + {len(untracked_files)} 条 reconciled_created")
    return 0


def add_parser(sub):
    p = sub.add_parser("reconcile", help="对账 events.jsonl 与文件系统(报告差异,--fix 补事件)")
    p.add_argument("--fix", action="store_true", help="自动补事件(不改文件)")
    p.set_defaults(func=run)
