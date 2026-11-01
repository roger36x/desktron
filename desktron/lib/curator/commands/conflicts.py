"""curator conflicts —— 列就近覆盖与冲突。

视图下消费方本地内容会盖挂载的共享子 KB。覆盖要可见、危险的要浮出:未核实的本地猜测
不许静默盖掉共享里 corroborated 的事实。只读;有冲突退出码非零(可作闸)。
"""

from __future__ import annotations

import os
from collections import defaultdict

from ..config import Instance


def run(inst: Instance, args) -> int:
    by_id = defaultdict(list)
    for p, fm, _ in inst.iter_notes():
        nid = fm.get("id")
        if nid:
            by_id[nid].append((p, fm))
    overrides = {nid: ms for nid, ms in by_id.items() if len(ms) > 1}
    if not overrides:
        print("(无跨层覆盖)")
        return 0

    danger = 0
    for nid, ms in sorted(overrides.items()):
        ms = sorted(ms, key=lambda m: inst.layer_rank(m[0]))
        near_p, near_fm = ms[0]
        near_loc = inst.location_of(near_p)
        near_work = bool(near_loc and near_loc.get("git") == "outside")
        shadowed_strong = [
            p for p, fm in ms[1:]
            if (inst.location_of(p) or {}).get("git") == "inside" and fm.get("confidence") == "corroborated"
        ]
        bad = bool(near_work and near_fm.get("confidence") != "corroborated" and shadowed_strong)
        if bad:
            danger += 1
        print(f"{'冲突' if bad else '覆盖'}  {nid}  最近 [{inst.band_label_for(near_p)}] conf={near_fm.get('confidence')}")
        for p, fm in ms[1:]:
            print(f"    被盖 [{inst.band_label_for(p)}] conf={fm.get('confidence')}  {os.path.relpath(p, inst.root)}")

    print(f"\n{len(overrides)} 处覆盖,{danger} 处冲突(弱证据本地盖共享 corroborated)。")
    return 1 if danger else 0


def add_parser(sub):
    p = sub.add_parser("conflicts", help="列跨层覆盖与冲突(弱证据本地盖共享 corroborated)")
    p.set_defaults(func=run)
