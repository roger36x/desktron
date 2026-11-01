"""检索/浏览命令:show / list / search / reindex / review-due。只读(reindex 除外)。"""

from __future__ import annotations

import os

from .. import freshness
from ..config import Instance
from ..index import rebuild_index
from ..note import dump_note
from ..state import read_access, record_access
from ..util import CuratorError, parse_date, today


_FRESH_LABEL = {"fresh": "新鲜", "aging": "老化", "stale": "陈旧",
                "unknown": "未知", "na": "—"}


def _kind_tag(fm: dict) -> str:
    if fm.get("kind") == "fact" and fm.get("doc_type"):
        return fm["doc_type"]
    return fm.get("kind", "")


def _matches(fm: dict, args) -> bool:
    for field, want in (
        ("tier", getattr(args, "tier", None)),
        ("kind", getattr(args, "kind", None)),
        ("confidence", getattr(args, "confidence", None)),
        ("doc_type", getattr(args, "doc_type", None)),
        ("status", getattr(args, "status", None)),
    ):
        if want and fm.get(field) != want:
            return False
    return True


def _line(inst: Instance, path: str, fm: dict) -> str:
    rel = os.path.relpath(path, inst.root)
    loc_label = inst.band_label_for(path)
    bits = [b for b in (loc_label, _kind_tag(fm), fm.get("confidence")) if b]
    if fm.get("status") == "deprecated":
        bits.append("deprecated")
    return f"{fm.get('id')}  [{' · '.join(bits)}]  {rel}\n    {fm.get('summary', '')}"


def cmd_list(inst: Instance, args) -> int:
    rows = [(p, fm) for p, fm, _ in inst.iter_notes(include_indexed=True) if _matches(fm, args)]
    rows.sort(key=lambda e: (e[1].get("tier", ""), e[1].get("id", "")))
    if not rows:
        print("(无匹配)")
        return 0
    if args.paths:
        for p, _ in rows:
            print(os.path.relpath(p, inst.root))
    else:
        for p, fm in rows:
            print(_line(inst, p, fm))
    return 0


def cmd_show(inst: Instance, args) -> int:
    found = inst.find_by_id(args.id)
    if not found:
        raise CuratorError(f"id 不存在:{args.id}")
    path, fm, body = found
    # 操作性读经接口累计访问计数(动态可达性信号;裸读文件不计数)。
    # caller 口径:task 计热度;engine-internal/audit 只落 trace(机器自读不计使用)。
    record_access(inst, args.id, getattr(args, "caller", "task") or "task")
    if getattr(args, "raw", False):
        print(dump_note(fm, body))
        return 0
    print(f"# {os.path.relpath(path, inst.root)}\n")
    print(dump_note(fm, body))
    # 反向:哪些笔记 links 到它
    refs = [
        f2.get("id")
        for _, f2, _ in inst.iter_notes(include_indexed=True)
        if args.id in (f2.get("links") or [])
    ]
    if refs:
        print(f"\n被 links 引用:{', '.join(sorted(refs))}")
    # 就近覆盖:同 id 多层都有时只显示最近一层,其余浮出;弱证据本地盖共享 corroborated 要提醒。
    others = [m for m in inst.find_all_by_id(args.id)
              if os.path.abspath(m[0]) != os.path.abspath(path)]
    if others:
        print(f"\n就近覆盖:当前为最近层 [{inst.band_label_for(path)}],另有 {len(others)} 处被覆盖:")
        for op, ofm, _ in sorted(others, key=lambda m: inst.layer_rank(m[0])):
            print(f"  [{inst.band_label_for(op)}] {os.path.relpath(op, inst.root)}(confidence={ofm.get('confidence')})")
        near_loc = inst.location_of(path)
        near_is_work = bool(near_loc and near_loc.get("git") == "outside")
        shadowed_strong = [
            op for op, ofm, _ in others
            if (inst.location_of(op) or {}).get("git") == "inside" and ofm.get("confidence") == "corroborated"
        ]
        if near_is_work and fm.get("confidence") != "corroborated" and shadowed_strong:
            print("  冲突:证据更弱的本地内容盖住了共享里 corroborated 的事实——需人判(贡献回去或修正本地)。")
    acc = read_access(inst, args.id)
    if acc:
        print(f"访问:{acc.get('count', 0)} 次,最近 {acc.get('last_access', '—')}")
    # 新鲜度:与 confidence 正交的派生时效信号(只读,不改 confidence)。
    by_id = {f2.get("id"): f2 for _, f2, _ in inst.iter_notes(include_indexed=True) if f2.get("id")}
    state, age, reason = freshness.verdict(
        inst, fm, by_id, freshness.plugin_for(inst), parse_date(today()))
    bits = [b for b in (None if age is None else f"{age:.0f} 月", reason) if b]
    print(f"新鲜度:{_FRESH_LABEL.get(state, state)}" + (f"({' · '.join(bits)})" if bits else ""))
    return 0


def cmd_search(inst: Instance, args) -> int:
    q = args.query.lower()
    fields = args.fields.split(",") if args.fields else ["summary", "keywords", "body"]
    hits = []
    for p, fm, body in inst.iter_notes(include_indexed=True):
        if not _matches(fm, args):
            continue
        where = []
        if "summary" in fields and q in str(fm.get("summary", "")).lower():
            where.append("summary")
        if "keywords" in fields and q in " ".join(fm.get("keywords", []) or []).lower():
            where.append("keywords")
        if "body" in fields and q in body.lower():
            where.append("body")
        if where:
            hits.append((p, fm, where))
    if not hits:
        print("(无匹配)")
        return 0
    if args.paths:
        for p, _, _ in hits:
            print(os.path.relpath(p, inst.root))
    else:
        for p, fm, where in hits:
            rel = os.path.relpath(p, inst.root)
            print(f"{fm.get('id')}  ({'/'.join(where)})  {rel}\n    {fm.get('summary', '')}")
    return 0


def cmd_reindex(inst: Instance, args) -> int:
    rebuild_index(inst)
    print(f"已重建 {inst.index_file}")
    return 0


def cmd_review_due(inst: Instance, args) -> int:
    """列到期复验:变更驱动(tracks 源前进)优先,next_review 次之,doc_type 半衰期 fallback。

    判定逻辑收敛到 freshness.verdict(读侧 show 共用):列出 verdict 非 fresh / na 的条目。
    """
    today_d = parse_date(today())
    notes = list(inst.iter_notes())
    by_id = {fm.get("id"): fm for _, fm, _ in notes if fm.get("id")}
    plugin = freshness.plugin_for(inst)
    due = []  # (age_or_None, reason, path, fm)
    for p, fm, _ in notes:
        state, age, reason = freshness.verdict(inst, fm, by_id, plugin, today_d)
        if state in ("fresh", "na"):
            continue
        due.append((age, reason, p, fm))
    if not due:
        print("(无到期复审)")
        return 0
    due.sort(key=lambda e: (-1 if e[0] is None else -e[0]))
    for age, reason, p, fm in due:
        rel = os.path.relpath(p, inst.root)
        age_s = "—" if age is None else f"{age:.0f} 月"
        print(f"{reason}  {age_s}  {fm.get('id')}  ({_kind_tag(fm)})  {rel}")
    return 0


def _filter_args(p):
    p.add_argument("--tier")
    p.add_argument("--kind", choices=["judgment", "fact"])
    p.add_argument("--confidence")
    p.add_argument("--doc-type", dest="doc_type")
    p.add_argument("--status")


def add_parser(sub):
    lp = sub.add_parser("list", help="按 tier/kind/confidence/doc-type/status 过滤浏览")
    _filter_args(lp)
    lp.add_argument("--paths", action="store_true", help="只输出文件路径")
    lp.set_defaults(func=cmd_list)

    sp = sub.add_parser("show", help="看单条:路径 + frontmatter + 正文 + 反向引用")
    sp.add_argument("--raw", action="store_true", help="只输出笔记原文(frontmatter+正文),无路径头/引用/覆盖脚注")
    sp.add_argument("--caller", default="task", choices=["task", "engine-internal", "audit"],
                    help="读取方口径:task 计热度;engine-internal/audit 不计、只落 trace")
    sp.add_argument("--id", required=True)
    sp.set_defaults(func=cmd_show)

    sep = sub.add_parser("search", help="在 summary/keywords/body 里子串搜索")
    sep.add_argument("query")
    sep.add_argument("--fields", help="逗号分隔,默认 summary,keywords,body")
    _filter_args(sep)
    sep.add_argument("--paths", action="store_true", help="只输出文件路径")
    sep.set_defaults(func=cmd_search)

    rp = sub.add_parser("reindex", help="重建 INDEX.md")
    rp.set_defaults(func=cmd_reindex)

    dp = sub.add_parser("review-due", help="列老化/过期/无 last_verified 的 fact")
    dp.set_defaults(func=cmd_review_due)
