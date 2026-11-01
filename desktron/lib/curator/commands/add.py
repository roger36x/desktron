"""curator add —— 新增笔记。闸:查重、置信度纪律、表达黑名单。"""

from __future__ import annotations

import os
import sys

from ..config import Instance
from ..confidence import derive
from ..gate import check_body, check_companion, check_enums, check_required
from ..index import rebuild_index
from ..note import dump_note, write_note
from ..state import record_event
from ..util import CuratorError, add_duration, parse_date, today, valid_id


def _read_body(args) -> str:
    if args.body is not None:
        return args.body
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise CuratorError("缺正文:用 --body 或经 stdin 传入")


def _resolve_tier(inst: Instance, args) -> str:
    """位置由两个不混的入口给:--role(配 --sub-kb,写 git 内角色层)XOR --band(写工作层)。

    frontmatter 仍单字段 tier,存解析出的值(角色层值或 band 值)。
    """
    if args.role and args.band:
        raise CuratorError("--role 与 --band 互斥(git 内角色层 / 工作层 band,二选一)")
    if args.role:
        if not args.sub_kb:
            raise CuratorError("--role 写 git 内角色层,须配 --sub-kb 指定子 KB")
        if args.role not in inst.role_layers:
            raise CuratorError(f"--role 须是角色层 {inst.role_layers},得到 {args.role}")
        return args.role
    if args.band:
        if args.sub_kb:
            raise CuratorError("--band 是工作层(git 外),不配 --sub-kb")
        if args.band not in inst.work_products:
            raise CuratorError(f"--band 须是工作层成品 {inst.work_products},得到 {args.band}")
        return args.band
    raise CuratorError("须给 --role(配 --sub-kb,写 git 内)或 --band(写工作层)其一")


def run(inst: Instance, args) -> int:
    note_id = args.id
    if not valid_id(note_id):
        raise CuratorError(f"id 非法:{note_id}(须 ^[a-z0-9][a-z0-9-]*$)")
    tier = _resolve_tier(inst, args)
    memory_pinned = (inst.ontology.get("name") == "memory-worklayer"
                     and tier == "sot" and args.kind == "judgment")
    if memory_pinned and not args.user_confirmed:
        raise CuratorError("memory pinned judgment 必须由用户明确确认；请加 --user-confirmed")
    # 查重按层:同 id 在目标层(同子 KB / 工作层)已存在则拒;跨层同 id 是就近覆盖,允许。
    target_key = args.sub_kb if args.sub_kb else "work"
    for p, _f, _b in inst.find_all_by_id(note_id):
        loc = inst.location_of(p)
        key = loc["sub_kb"] if loc and loc["kind"] == "git_inside" else "work"
        if key == target_key:
            raise CuratorError(f"id 已存在于本层({target_key}):{note_id}(查重硬失败)")

    fm: dict = {
        "id": note_id,
        "tier": tier,
        "kind": args.kind,
        "summary": args.summary,
    }
    if args.sub_kb:
        fm["sub_kb"] = args.sub_kb
    if args.type:
        fm["type"] = args.type  # 可选自由分类:引擎不解释、不校验、不默认
    if memory_pinned:
        fm["user_confirmed"] = True
    if args.gaps:
        fm["gaps"] = args.gaps
    if args.source:
        fm["source"] = args.source
    # confidence 不手工设:按证据推导中段(有 source → documented,否则 reported)。
    # verified 仅经 verify 升,add 产不出。
    fm["confidence"] = derive(fm, inst.source_patterns)
    # 生命周期核:created 自动盖;review_every 给定则算 next_review(GC 触发时刻)。
    created = today()
    fm["created"] = created
    if args.review_every:
        fm["review_every"] = args.review_every
        fm["next_review"] = add_duration(parse_date(created), args.review_every).isoformat()
    # last_verified 语义是"核实时间",不在 add 设;经 verify / update --touch 写。
    if args.kind == "fact":
        if args.doc_type:
            fm["doc_type"] = args.doc_type
        fm["status"] = args.status or "current"
        if args.keywords:
            fm["keywords"] = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if args.links:
        fm["links"] = [k.strip() for k in args.links.split(",") if k.strip()]
    if args.tracks:
        fm["tracks"] = [k.strip() for k in args.tracks.split(",") if k.strip()]
    if args.distilled:
        fm["distilled"] = True

    body = _read_body(args)

    problems = []
    problems += check_enums(inst, fm)
    problems += check_required(inst, fm)
    problems += check_companion(inst, fm)
    problems += check_body(inst, body, fm.get("tier"))

    elevated = args.sub_kb and inst.gate_level(args.sub_kb) == "elevated"
    if elevated:
        if fm.get("confidence") == "reported":
            problems.append(f"elevated 子 KB({args.sub_kb})要求 source(不接受 reported)")

    if problems:
        raise CuratorError("闸未过:\n  - " + "\n  - ".join(problems))

    # 落点:git 内(--role + --sub-kb)或工作层(--band)。tier 取值已在 _resolve_tier 校验。
    if args.sub_kb:
        if args.sub_kb not in inst.sub_kbs:
            raise CuratorError(f"sub_kb 不在实例:{args.sub_kb}({inst.sub_kbs})")
        inst.assert_writable(args.sub_kb)
        d = inst.role_dir(args.sub_kb, tier)
    else:
        d = None   # 工作层:见下(topical band 由 {topic} 模板定位,普通 band 是 topic 子目录)
    if args.topic:
        topic = args.topic.strip().strip("/")
        if not topic or os.path.isabs(args.topic) or ".." in topic.split("/"):
            raise CuratorError(f"topic 非法:{args.topic}(须相对子目录,不含 .. 或绝对路径)")
    else:
        topic = ""
    if d is None:
        if inst.band_is_topical(tier):
            d = inst.work_dir(tier, topic)   # topic 进模板({topic}/s0 这类),缺 topic 由 work_dir 报错
            path = os.path.join(d, f"{note_id}.md")
        else:
            d = inst.work_dir(tier)
            path = os.path.join(d, *topic.split("/"), f"{note_id}.md") if topic else os.path.join(d, f"{note_id}.md")
    else:
        path = os.path.join(d, *topic.split("/"), f"{note_id}.md") if topic else os.path.join(d, f"{note_id}.md")

    # 蒸馏准入闸:地图须落在真实疆域。可核 tracks 不兑现 → 拒收(不写成品),原文 + 拒因入 rejected 缓冲。
    if fm.get("distilled") and not args.force:
        from ..check import append_rejected, check_plugin_for, gate_distilled

        by_id = {f.get("id"): f for _p, f, _b in inst.iter_notes(include_indexed=True) if f.get("id")}
        reasons = gate_distilled(inst, fm, by_id, check_plugin_for(inst))
        if reasons:
            print("蒸馏准入闸未过(地图悬空,未写成品):")
            for r in reasons:
                print(f"  - {r}")
            if args.dry_run:
                print("[dry-run] 未写 rejected 缓冲")
                return 4
            dest = f"{args.sub_kb}/{tier}" if args.sub_kb else tier
            if args.topic:
                dest += f" [{args.topic}]"
            rp = append_rejected(inst, fm, body, reasons, dest)
            print(f"已记入 {os.path.relpath(rp, inst.root)}(下一轮蒸馏请回避此条,或先补齐叶子再蒸)")
            return 4

    if args.dry_run:
        print(f"[dry-run] 将写入 {os.path.relpath(path, inst.root)}\n")
        print(dump_note(fm, body))
        return 0
    write_note(path, fm, body)
    record_event(inst, note_id, "created", kind=args.kind, tier=tier, sub_kb=args.sub_kb)
    rebuild_index(inst)
    msg = f"写入 {os.path.relpath(path, inst.root)}"
    if elevated:
        msg += " [elevated gate]"
    print(msg)
    return 0


def add_parser(sub):
    p = sub.add_parser("add", help="新增笔记")
    p.add_argument("--role", help="git 内角色层(global/domain),须配 --sub-kb")
    p.add_argument("--band", help="工作层 band(private/shared),不配 --sub-kb")
    p.add_argument("--kind", required=True, choices=["judgment", "fact"])
    p.add_argument("--id", required=True)
    p.add_argument("--sub-kb", dest="sub_kb", help="写入该子 KB 的 git 内角色层(配 --role)")
    p.add_argument("--type", help="可选:实例自定义的自由分类(如记忆 user/feedback/project/reference);引擎不解释、不校验、不默认")
    p.add_argument("--topic", help="归入的 topic 子目录(<桶>/<topic>/<id>.md),不平铺在桶根")
    p.add_argument("--review-every", dest="review_every", help="复验周期(14d / 2w / 6mo),给定则算 next_review")
    p.add_argument("--gaps")
    p.add_argument("--source")
    p.add_argument("--summary", required=True)
    p.add_argument("--doc-type", dest="doc_type")
    p.add_argument("--status")
    p.add_argument("--keywords")
    p.add_argument("--links")
    p.add_argument("--tracks", help="变更驱动源(逗号分隔):note:<id> / repo:<p>@<commit> / config:.. / doc:..")
    p.add_argument("--distilled", action="store_true", help="标记为蒸馏产物(地图非疆域:reported、带 tracks、不 verified)")
    p.add_argument("--force", action="store_true", help="跳过蒸馏准入闸(可核 tracks 不兑现也照写)")
    p.add_argument("--user-confirmed", action="store_true", help="memory pinned judgment:声明用户已明确确认")
    p.add_argument("--body")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="只预演,不写盘")
    p.set_defaults(func=run)
