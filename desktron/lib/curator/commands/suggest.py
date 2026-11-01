"""curator suggest —— 读/查时的可执行建议汇总。只读。

把散在各命令里的信号汇成一处:promote 候选、compact/蒸馏候选、待复验、孤儿、冲突。
复用 authority / compact / freshness / conflicts 的判定,不重复造轮子。
"""

from __future__ import annotations

from collections import defaultdict

from .. import freshness
from ..config import Instance
from ..util import months_between, parse_date, today
from . import authority as authority_mod
from . import compact as compact_mod


def run(inst: Instance, args) -> int:
    acfg = authority_mod._cfg(inst)
    ccfg = compact_mod._cfg(inst)
    indeg = authority_mod._indegree(inst)
    today_d = parse_date(today())
    plugin = freshness.plugin_for(inst)
    notes = list(inst.iter_notes())
    by_id = {fm.get("id"): fm for _, fm, _ in notes if fm.get("id")}

    promote_c, compact_c, stale_c, orphan_c = [], [], [], []
    for path, fm, _ in notes:
        loc = inst.location_of(path)
        nid = fm.get("id")
        score = authority_mod._score(inst, fm, indeg.get(nid, 0), acfg)["score"]
        # promote 候选:工作层 shared 且 documented(有证据,够格贡献回子 KB)
        if loc and loc.get("kind") == "work" and loc.get("band") == "shared" and fm.get("confidence") == "documented":
            promote_c.append(nid)
        if loc and loc.get("git") == "inside":
            # compact 候选:低 authority + 陈旧
            if score < ccfg["min_authority"] and compact_mod._stale(inst, fm, by_id, plugin, today_d):
                compact_c.append(nid)
            # 孤儿:git 内成品无人引用(in-degree 0)
            if nid and indeg.get(nid, 0) == 0:
                orphan_c.append(nid)
        # 待复验:变更驱动陈旧 / next_review 到期 / fact 超 aging
        due = False
        if fm.get("tracks"):
            s, _c = freshness.track_eval(inst, fm, by_id, plugin)
            due = bool(s)
        nr = parse_date(fm.get("next_review"))
        if nr and today_d and nr <= today_d:
            due = True
        if not due and fm.get("kind") == "fact":
            lv = parse_date(fm.get("last_verified"))
            th = inst.freshness_threshold(fm.get("doc_type", ""))
            if not lv or (today_d and months_between(lv, today_d) > th.get("aging", 12)):
                due = True
        if due and nid:
            stale_c.append(nid)

    # 冲突:弱证据本地盖共享 corroborated
    bid = defaultdict(list)
    for p, fm, _ in notes:
        if fm.get("id"):
            bid[fm["id"]].append((p, fm))
    conflict_c = []
    for nid, ms in bid.items():
        if len(ms) < 2:
            continue
        ms = sorted(ms, key=lambda m: inst.layer_rank(m[0]))
        np_, nf = ms[0]
        nloc = inst.location_of(np_)
        if nloc and nloc.get("git") == "outside" and nf.get("confidence") != "corroborated":
            if any((inst.location_of(p) or {}).get("git") == "inside" and f.get("confidence") == "corroborated"
                   for p, f in ms[1:]):
                conflict_c.append(nid)

    def section(title, ids, hint):
        if ids:
            uniq = sorted(set(ids))
            shown = ", ".join(uniq[:20]) + (" …" if len(uniq) > 20 else "")
            print(f"{title}({len(uniq)}):{shown}")
            print(f"  → {hint}")

    print("可执行建议:")
    section("promote 候选", promote_c, "工作层 shared 已有证据,考虑 promote 到子 KB(过清洗闸、选目标)")
    section("compact / 蒸馏候选", compact_c, "低 authority + 陈旧;curator compact-due 看详情")
    section("待复验", stale_c, "curator review-due 看详情")
    section("孤儿(git 内无人引用)", orphan_c, "curator authority --orphans;考虑补 links 或淘汰")
    section("冲突", conflict_c, "curator conflicts;弱证据本地盖了共享 corroborated,需人判")
    if not any([promote_c, compact_c, stale_c, orphan_c, conflict_c]):
        print("  (无)")
    return 0


def add_parser(sub):
    p = sub.add_parser("suggest", help="可执行建议汇总:promote / compact / 复验 / 孤儿 / 冲突(只读)")
    p.set_defaults(func=run)
