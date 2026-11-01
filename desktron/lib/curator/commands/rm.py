"""curator rm --id —— 删除一条误建笔记。需 --yes 确认。

设计上事实失效用 status=deprecated,不删;rm 是为误建(typo id / 错加)留的清理口。
不可逆,默认只预演,加 --yes 才真删。
"""

from __future__ import annotations

import os

from ..config import Instance
from ..graphdata import Graph
from ..index import rebuild_index
from ..state import record_event
from ..util import CuratorError


def run(inst: Instance, args) -> int:
    found = inst.find_by_id(args.id)
    if not found:
        raise CuratorError(f"id 不存在:{args.id}")
    path, fm, _ = found
    loc = inst.location_of(path) or {}
    memory_pinned = (inst.ontology.get("name") == "memory-worklayer"
                     and loc.get("kind") == "work" and loc.get("band") == "sot"
                     and fm.get("kind", "judgment") == "judgment")
    if memory_pinned and not args.user_confirmed:
        raise CuratorError("memory pinned judgment 的删除必须由用户明确确认；请加 --user-confirmed")
    rel = inst.doc_key(path)  # profile_doc 规范键(视图下也不退化成 ../..)

    # 被 graph profile_doc 引用 → 删了会留悬空 backref,默认阻断,需 --force
    doc_refs = Graph(inst).entities_with_doc(rel)
    if doc_refs and not args.force:
        print(f"{rel} 被实体 profile_doc 引用:{', '.join(doc_refs)}")
        print("删除会留悬空 backref。先处理这些实体,或加 --force 强删。")
        return 3

    if not args.yes:
        print(f"将删除 {rel}(tier={fm.get('tier')} confidence={fm.get('confidence')})")
        if doc_refs:
            print(f"注:仍被实体引用 {', '.join(doc_refs)},删后 graph check 会悬空。")
        print("不可逆。确认请加 --yes。")
        return 3

    os.remove(path)
    record_event(inst, args.id, "removed", tier=fm.get("tier"))
    rebuild_index(inst)
    print(f"已删除 {rel}")
    if doc_refs:
        print(f"警告:实体 {', '.join(doc_refs)} 的 profile_doc 现已悬空,graph check 会报。")
    return 0


def add_parser(sub):
    p = sub.add_parser("rm", help="删除一条误建笔记(需 --yes)")
    p.add_argument("--id", required=True)
    p.add_argument("--yes", action="store_true", help="确认删除(不可逆)")
    p.add_argument("--force", action="store_true", help="即便被实体 profile_doc 引用也删")
    p.add_argument("--user-confirmed", action="store_true", help="memory pinned judgment:声明用户已明确确认")
    p.set_defaults(func=run)
