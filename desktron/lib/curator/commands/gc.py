"""curator gc —— 年轻代(temp)GC + 成品复验触发。建在生命周期基底上。

模型借 JVM 分代:temp = 年轻代,成品 = 老年代(GC 不回收)。本命令:
- 晋升 temp 草稿:pretenure(质量 ≥ 阈)或 tenuring(age ≥ aging 且仍可达)→ 过 add 闸落 promote_to 桶。
- 淘汰 temp:不可达 + 陈旧 + 低质,三者皆需(容量 triage 是唯一例外)。
- 触发成品复验:next_review 到期 → 报出(挂 review-due),不动 confidence、不删、不改。

红线:自动晋升只到 git 外桶(promote_to,默认首个非 containment 工作层成品 band),绝不自动跨 git 线;跨线仍人工
`promote --confirm`。containment 桶不自动外移。淘汰删文件不可逆,默认只预览,--yes 才执行。
"""

from __future__ import annotations

import datetime
import glob
import math
import os

from ..config import Instance
from ..confidence import derive
from ..gate import check_body, check_companion, check_enums, check_required
from ..index import rebuild_index
from ..note import read_note, write_note
from ..state import read_access, record_event
from ..util import CuratorError, add_duration, parse_date, today
from . import authority as authority_mod

DEFAULTS = {
    "promote_to": None,          # None → 首个非 containment 的 git 外桶
    "aging": "90d",              # tenuring:草稿存活到此年龄且仍可达 → 晋升
    "pretenure_quality": 0.7,    # pretenure:位置无关质量分 ≥ 此值 → 直接晋升
    "temp_budget": 200,          # temp 文件数预算,涨满强制 triage
    "evict": {"stale": "180d", "min_quality": 0.3, "min_access": 1},
}


def _cfg(inst: Instance) -> dict:
    c = dict(DEFAULTS)
    raw = inst.ontology.get("gc", {}) or {}
    c.update({k: v for k, v in raw.items() if k != "evict"})
    ev = dict(DEFAULTS["evict"])
    ev.update(raw.get("evict", {}) or {})
    c["evict"] = ev
    return c


def _promote_to(inst: Instance, cfg: dict) -> str:
    """gc 自动晋升只到工作层成品(默认首个非 containment 工作层成品 band);跨 git 线进子 KB 永远人工。"""
    if cfg.get("promote_to"):
        return cfg["promote_to"]
    for b in inst.work_products:
        if not inst.is_containment(b):
            return b
    raise CuratorError("无可自动晋升的目标(需一个非 containment 的工作层成品 band)")


def _created(path: str, fm) -> datetime.date:
    if fm:
        d = parse_date(fm.get("created"))
        if d:
            return d
    return datetime.date.fromtimestamp(os.path.getmtime(path))


def _quality(fm, indeg: int, acfg: dict) -> float:
    """位置无关质量分(0–1):frontmatter 齐全度 × 新鲜度 × (基底+引用+印证)。

    不含 band 先验(那会按位置封顶,pretenure 判的是内容好坏)。裸料(无 fm)→ 0。
    """
    if not fm:
        return 0.0
    ff = authority_mod._field_coverage(fm)
    fr = authority_mod._freshness(fm, acfg)
    cite_ref = acfg["cite_ref"]
    cite = min(math.log1p(indeg) / math.log1p(cite_ref), 1.0) if cite_ref > 0 and indeg else 0.0
    corrob = acfg["corroboration_neutral"]
    raw = ff * fr * (acfg["w_base"] + acfg["w_cite"] * cite + acfg["w_corrob"] * corrob)
    return round(max(0.0, min(raw, 1.0)), 3)


def _scan_temp(inst: Instance) -> list:
    """扫草稿 band(inst.draft_bands,不是全部免闸 band——见该属性的说明)。每项带 band/topic
    (来自 location_of),供晋升定落点。

    裸文件(无 frontmatter/无 id)照收:草稿区的用途就是放临时 Markdown、工具输出、拉取的
    派生物,淘汰这类陈旧无引用的散落文件正是 GC 的本职(见 template/temp/README)。
    2026-08-21 曾在这里加过「只收 is_note」,想挡住 journal.md 被容量 triage 误删——那是
    用错了地方:journal 属于 notes/journal 这类不可回收的 band,已由 draft_bands 整体排除在
    扫描之外;在这里再挡一层,顺带把真草稿区的裸文件也豁免了,GC 对它们就此失效
    (test_evict_old_raw 抓到)。"""
    items = []
    for band in inst.draft_bands:
        for base in inst.work_scan_dirs(band):
            for p in sorted(glob.glob(os.path.join(base, "**", "*.md"), recursive=True)):
                if os.path.basename(p) == "README.md":
                    continue
                try:
                    fm, _body = read_note(p)
                    is_note = isinstance(fm, dict) and bool(fm.get("id"))
                except CuratorError:
                    fm, _body, is_note = None, None, False
                loc = inst.location_of(p) or {}
                items.append({"path": p, "fm": fm if is_note else None, "is_note": is_note,
                              "band": loc.get("band") or band, "topic": loc.get("topic") or "",
                              "base": base})
    return items


def _indegree(inst: Instance, temp_items: list) -> dict:
    """links in-degree:成品桶 + temp 草稿,谁的 links 指向某 id。"""
    ids = {}
    sources = [fm for _, fm, _ in inst.iter_notes()]
    sources += [it["fm"] for it in temp_items if it["fm"]]
    for fm in sources:
        nid = fm.get("id")
        if nid:
            ids.setdefault(nid, 0)
    for fm in sources:
        for ref in fm.get("links", []) or []:
            if ref in ids:
                ids[ref] += 1
    return ids


def run(inst: Instance, args) -> int:
    cfg = _cfg(inst)
    acfg = authority_mod._cfg(inst)
    target = _promote_to(inst, cfg)
    today_d = parse_date(today())
    access = read_access(inst)
    min_access = cfg["evict"]["min_access"]

    temp_items = _scan_temp(inst)
    indeg = _indegree(inst, temp_items)

    promote, evict, keep = [], [], []
    for it in temp_items:
        fm, path = it["fm"], it["path"]
        nid = fm.get("id") if fm else None
        created = _created(path, fm)
        ind = indeg.get(nid, 0) if nid else 0
        acc = (access.get(nid) or {}).get("count", 0) if nid else 0
        reachable = ind > 0 or acc >= min_access
        quality = _quality(fm, ind, acfg)
        aged = add_duration(created, cfg["aging"]) <= today_d
        stale = add_duration(created, cfg["evict"]["stale"]) <= today_d
        low_q = quality < cfg["evict"]["min_quality"]

        rel = os.path.relpath(path, inst.root)
        survival = quality + (0.5 if reachable else 0.0)
        rec = {"it": it, "id": nid, "rel": rel, "quality": quality, "reachable": reachable,
               "survival": survival}

        if it["is_note"] and quality >= cfg["pretenure_quality"]:
            rec["reason"] = "pretenure"
            promote.append(rec)
        elif it["is_note"] and aged and reachable:
            rec["reason"] = "tenuring"
            promote.append(rec)
        elif (not reachable) and stale and low_q:
            rec["reason"] = "无引用+陈旧+低质"
            evict.append(rec)
        else:
            keep.append(rec)

    # 容量 triage:超预算时强制删最弱的 keep(越过"三者皆需")
    over = len(temp_items) - len(promote) - cfg["temp_budget"]
    triaged = []
    if over > 0:
        for rec in sorted(keep, key=lambda r: r["survival"])[:over]:
            rec["reason"] = "容量 triage(超预算,最弱)"
            triaged.append(rec)
        keep = [r for r in keep if r not in triaged]
    evict += triaged

    # 成品复验触发:next_review 到期
    due = []
    for p, fm, _ in inst.iter_notes():
        nr = parse_date(fm.get("next_review"))
        if nr and today_d and nr <= today_d:
            due.append((fm.get("id"), os.path.relpath(p, inst.root)))

    # —— 报告 ——
    if getattr(args, "json", False):
        import json as _json
        def rec_row(rec):
            it = rec["it"]
            return {"id": rec["id"], "rel": rec["rel"], "quality": rec["quality"],
                    "reachable": rec["reachable"], "reason": rec.get("reason", "keep"),
                    "band": it.get("band", ""), "topic": it.get("topic", "")}
        print(_json.dumps({
            "total": len(temp_items), "budget": cfg["temp_budget"], "promote_to": target,
            "promote": [rec_row(r) for r in promote],
            "evict": [rec_row(r) for r in evict],
            "keep": len(keep),
            "review_due": [{"id": nid, "rel": rel} for nid, rel in due],
            "executed": bool(args.yes and not args.dry_run),
        }, ensure_ascii=False))
        if args.dry_run or not args.yes:
            return 0
        # --json --yes:执行但不再打人读报告(结构已输出;执行结果经 events 可审)

    if not getattr(args, "json", False):
        def line(rec):
            idp = rec["id"] or rec["rel"]
            return f"  {idp}  (质量 {rec['quality']}{'·可达' if rec['reachable'] else ''})  {rec['reason']}"

        print(f"GC 预览(temp {len(temp_items)} 项,预算 {cfg['temp_budget']},目标桶 {target}):")
        print(f"晋升 → {target}({len(promote)}):")
        for r in promote:
            print(line(r))
        print(f"淘汰({len(evict)}):")
        for r in evict:
            print(line(r))
        if due:
            print(f"复验触发(next_review 到期,{len(due)};挂 review-due,不自动改):")
            for nid, rel in due:
                print(f"  {nid}  {rel}")
        if args.dry_run or not args.yes:
            tail = "(--dry-run 强制预览)" if args.dry_run else "加 --yes 执行晋升/淘汰(淘汰删文件,不可逆)"
            print(f"\n保留 {len(keep)} 项。预览模式,{tail}。")
            return 0
    if args.dry_run or not args.yes:
        return 0

    # —— 执行 ——
    done_p, done_e, skipped = 0, 0, []
    for r in promote:
        it = r["it"]
        fm, body = read_note(it["path"])
        fm = dict(fm)
        if (inst.ontology.get("name") == "memory-worklayer" and target == "sot"
                and fm.get("kind", "judgment") == "judgment"):
            skipped.append(f"{r['id']}(pinned judgment 须用户明确确认，GC 不自动写入)")
            continue
        fm["tier"] = target
        # 晋升落地按证据定 confidence(temp 草稿产不出 verified)。
        fm["confidence"] = derive(fm, inst.source_patterns)
        # 保留 topic 分组,不平铺进桶根。topical 目标 band:topic 进模板;普通目标 band:topic 作子目录。
        src_sub = os.path.relpath(os.path.dirname(it["path"]), it.get("base") or inst.temp_dir)
        src_sub = "" if src_sub in (".", "") else src_sub
        topic = it.get("topic") or ""
        if inst.band_is_topical(target):
            base = inst.work_dir(target, topic or "_untopiced")
            dst = os.path.join(base, src_sub, f"{r['id']}.md") if src_sub else os.path.join(base, f"{r['id']}.md")
        else:
            base = inst.work_dir(target)
            parts = [x for x in (topic, src_sub) if x]
            dst = os.path.join(base, *parts, f"{r['id']}.md") if parts else os.path.join(base, f"{r['id']}.md")
        if os.path.exists(dst):
            skipped.append(f"{r['id']}(目标已存在)")
            continue
        # temp 没过闸,晋升即过闸:用真实正文复跑校验。
        problems = (check_enums(inst, fm) + check_required(inst, fm)
                    + check_companion(inst, fm) + check_body(inst, body, fm.get("tier")))
        if problems:
            skipped.append(f"{r['id']}(闸未过:{problems[0]})")
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        write_note(dst, fm, body)
        os.remove(it["path"])
        record_event(inst, r["id"], r["reason"], **{"to": target})
        done_p += 1
    for r in evict:
        os.remove(r["it"]["path"])
        if r["id"]:
            record_event(inst, r["id"], "evicted", reason=r["reason"])
        done_e += 1

    if done_p:
        rebuild_index(inst)
    if getattr(args, "json", False):
        import json as _json
        print(_json.dumps({"done_promote": done_p, "done_evict": done_e,
                           "skipped": skipped}, ensure_ascii=False))
    else:
        print(f"\n已执行:晋升 {done_p}、淘汰 {done_e}" + (f"、跳过 {len(skipped)}" if skipped else ""))
        for s in skipped:
            print(f"  跳过 {s}")
    return 0


def add_parser(sub):
    p = sub.add_parser("gc", help="年轻代 GC:晋升 temp 草稿 / 淘汰 / 触发成品复验")
    p.add_argument("--yes", action="store_true", help="执行晋升与淘汰(淘汰删文件,不可逆)")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="只预览(等价不加 --yes)")
    p.add_argument("--json", action="store_true", help="机器可读输出(调度器/dreaming 位消费)")
    p.set_defaults(func=run)
