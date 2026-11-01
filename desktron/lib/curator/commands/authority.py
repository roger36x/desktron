"""curator authority —— 来源权重(0–1),候选源里先信谁。只读,绝不设或升 confidence。

authority ≠ confidence:权威度是来源之间的软排序权重,不是单条信息的置信度。
公式(透明、权重可调,常量放 ontology.authority,引擎给默认):

    score = 结构先验 × 新鲜度 × (w_base + w_cite × 引用广度 + w_corrob × 交叉印证)

本地信号(飞书 uv/评论那类外部信号留实例插件,curator 不内置):
- 结构先验:band 位置(git 内 > git 外,containment 略低)× frontmatter 齐全度。
- 新鲜度:0.5^(age/半衰期),无 last_verified 置中性 1.0,旧降权不归零。
- 引用广度:links in-degree(多少笔记指向它),log 归一。
- 交叉印证:v0 置中性(本地无语义聚类的等价信号),待实例插件补。
"""

from __future__ import annotations

import math
import os

from ..config import Instance
from ..util import months_between, parse_date, today

DEFAULTS = {
    "half_life_months": 12,
    "cite_ref": 5,
    "w_base": 0.5,
    "w_cite": 0.3,
    "w_corrob": 0.2,
    "corroboration_neutral": 0.5,
    "band_prior": {"inside": 1.0, "outside": 0.6},
    "containment_factor": 0.8,
}


def _cfg(inst: Instance) -> dict:
    c = dict(DEFAULTS)
    c.update(inst.ontology.get("authority", {}) or {})
    bp = dict(DEFAULTS["band_prior"])
    bp.update((inst.ontology.get("authority", {}) or {}).get("band_prior", {}) or {})
    c["band_prior"] = bp
    return c


def _field_coverage(fm: dict) -> float:
    """frontmatter 字段齐全度 → [0.7, 1.0],缺字段不致命。authority / gc 共用。

    这是「结构齐全度」,不是已删除的 completeness 质量轴——别混。
    """
    checks = [bool(fm.get("source")), bool(fm.get("last_verified"))]
    if fm.get("kind") == "fact":
        checks += [bool(fm.get("doc_type")), bool(fm.get("status"))]
    return 0.7 + 0.3 * (sum(checks) / len(checks)) if checks else 1.0


def _structure_prior(inst: Instance, fm: dict, cfg: dict) -> float:
    tier = fm.get("tier")
    # 角色层 = git 内,工作层成品 = git 外;据此取位置先验。
    if tier in inst.role_layers:
        git = "inside"
    elif tier in inst.work_products:
        git = "outside"
    else:
        git = None
    base = cfg["band_prior"].get(git, 0.6) if git else 0.6
    if tier in inst.work_layer and inst.is_containment(tier):
        base *= cfg["containment_factor"]
    return base * _field_coverage(fm)


def _freshness(fm: dict, cfg: dict) -> float:
    lv = parse_date(fm.get("last_verified"))
    td = parse_date(today())
    if not lv or not td:
        return 1.0  # 无 last_verified:无从评估衰减,置中性
    age = months_between(lv, td)
    return 0.5 ** (age / cfg["half_life_months"])


def _indegree(inst: Instance) -> dict:
    """links in-degree:每个存在的笔记 id 被多少篇笔记的 links 指向。"""
    ids = {}
    notes = list(inst.iter_notes())
    for _, fm, _b in notes:
        nid = fm.get("id")
        if nid:
            ids.setdefault(nid, 0)
    for _, fm, _b in notes:
        for ref in fm.get("links", []) or []:
            if ref in ids:
                ids[ref] += 1
    return ids


def _score(inst: Instance, fm: dict, indeg: int, cfg: dict) -> dict:
    sp = _structure_prior(inst, fm, cfg)
    fr = _freshness(fm, cfg)
    cite_ref = cfg["cite_ref"]
    cite = min(math.log1p(indeg) / math.log1p(cite_ref), 1.0) if cite_ref > 0 and indeg else 0.0
    corrob = cfg["corroboration_neutral"]
    raw = sp * fr * (cfg["w_base"] + cfg["w_cite"] * cite + cfg["w_corrob"] * corrob)
    score = round(max(0.0, min(raw, 1.0)), 3)
    return {
        "score": score,
        "structure_prior": round(sp, 3),
        "freshness": round(fr, 3),
        "cite_breadth": round(cite, 3),
        "corroboration": round(corrob, 3),
        "indegree": indeg,
    }


def run(inst: Instance, args) -> int:
    cfg = _cfg(inst)
    indeg = _indegree(inst)
    rows = []  # (nid, path, fm, score_dict)
    for path, fm, _b in inst.iter_notes():
        nid = fm.get("id")
        if not nid:
            continue
        rows.append((nid, path, fm, _score(inst, fm, indeg.get(nid, 0), cfg)))

    if args.id:
        match = [r for r in rows if r[0] == args.id]
        if not match:
            print(f"id 不存在:{args.id}")
            return 2
        nid, _p, fm, s = match[0]
        print(f"{nid}  authority={s['score']}  (桶 {fm.get('tier')})")
        print(f"  结构先验 {s['structure_prior']} × 新鲜度 {s['freshness']} ×")
        print(f"  ({cfg['w_base']} + {cfg['w_cite']}×引用广度 {s['cite_breadth']}"
              f"[in-degree={s['indegree']}] + {cfg['w_corrob']}×交叉印证 {s['corroboration']})")
        return 0

    if args.orphans:
        rows = [r for r in rows if r[3]["indegree"] == 0]
        if not rows:
            print("(无孤儿:每篇都被引用)")
            return 0

    rows.sort(key=lambda r: r[3]["score"], reverse=True)
    if args.paths:
        for _nid, path, _fm, _s in rows:
            print(os.path.relpath(path, inst.root))
        return 0
    for nid, _p, fm, s in rows:
        print(f"{s['score']:.3f}  {nid}  (桶 {fm.get('tier')} · in-degree={s['indegree']}) — {fm.get('summary', '')}")
    return 0


def add_parser(sub):
    p = sub.add_parser("authority", help="来源权重(只读,不碰 confidence)")
    p.add_argument("--id", help="只看单条 + 信号拆解")
    p.add_argument("--orphans", action="store_true", help="只列无人引用的(in-degree 0)")
    p.add_argument("--paths", action="store_true", help="只输出文件路径")
    p.set_defaults(func=run)
