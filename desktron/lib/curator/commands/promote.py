"""curator promote —— 沿成熟度轴跨闸。

位置:工作层(private/shared,git 外)或 git 内(某子 KB 的角色层)。

- private → shared(工作层内,出 containment):脱敏闸,须 --confirm。
- private/shared → git 内子 KB:清洗闸(git 线),须 --to-sub-kb 选目标子 KB + --confirm;
  containment(private)不能一步跨线,先脱敏到 shared。
- git 内 → git 内(换子 KB / 角色层):重分类,无闸。
- git 内 → 工作层:拒(光谱单向,不向 git 外回退)。

temp 不参与 promote(无 frontmatter / id);temp → 成品走 add / gc。
"""

from __future__ import annotations

import glob
import os

from ..config import Instance
from ..graphdata import Graph
from ..index import rebuild_index
from ..note import write_note
from ..state import record_event
from ..util import CuratorError

CLEANSE_CHECKLIST = [
    "去除私有 / 个人 / 政治判断(这些只进工作层)",
    "去除内部路径、凭据、未公开标识",
    "source 可对外、不暴露私有出处",
    "措辞合规,可随子 KB 分发并被其他用户引用",
    "选对目标子 KB 与角色层(global/domain)",
]

DESENSITIZE_CHECKLIST = [
    "去除个人身份、私聊摘要、对具体个人的评价",
    "去除红线内容(政治判断等);脱不掉的留在 private,不要移出",
    "确认内容可在本地其他工作层流通(仍不上远端)",
]


def _dir_topics(d: str) -> list[str]:
    topics = set()
    for p in glob.glob(os.path.join(d, "**", "*.md"), recursive=True):
        if os.path.basename(p) in ("README.md", "index.md"):
            continue
        sub = os.path.relpath(os.path.dirname(p), d)
        if sub != ".":
            topics.add(sub.split(os.sep)[0])
    return sorted(topics)


def _resolve_dst(inst: Instance, args) -> dict:
    """目标位置:--to-role(配 --to-sub-kb,进 git 内角色层)XOR --to-band(进工作层)。"""
    if args.to_role and args.to_band:
        raise CuratorError("--to-role 与 --to-band 互斥(git 内角色层 / 工作层 band,二选一)")
    if args.to_role:
        if not args.to_sub_kb:
            raise CuratorError("--to-role 进 git 内角色层,须配 --to-sub-kb 选目标子 KB")
        if args.to_sub_kb not in inst.sub_kbs:
            raise CuratorError(f"目标子 KB 不在实例:{args.to_sub_kb}({inst.sub_kbs})")
        if args.to_role not in inst.role_layers:
            raise CuratorError(f"--to-role 须是角色层 {inst.role_layers},得到 {args.to_role}")
        return {"kind": "git_inside", "sub_kb": args.to_sub_kb, "role": args.to_role, "git": "inside"}
    if args.to_band:
        if args.to_sub_kb:
            raise CuratorError("--to-band 是工作层(git 外),不配 --to-sub-kb")
        if args.to_band not in inst.work_products:
            raise CuratorError(f"--to-band 须是工作层成品 {inst.work_products},得到 {args.to_band}")
        return {"kind": "work", "band": args.to_band, "git": "outside"}
    raise CuratorError("须给 --to-role(配 --to-sub-kb,进 git 内)或 --to-band(进工作层)其一")


def _same(src: dict, dst: dict) -> bool:
    if src["kind"] != dst["kind"]:
        return False
    if src["kind"] == "git_inside":
        return src["sub_kb"] == dst["sub_kb"] and src["role"] == dst["role"]
    return src["band"] == dst["band"]


def _container(inst: Instance, loc: dict, topic: str = None) -> str:
    if loc["kind"] == "git_inside":
        return inst.role_dir(loc["sub_kb"], loc["role"])
    return inst.work_dir(loc["band"], topic or loc.get("topic"))


def run(inst: Instance, args) -> int:
    found = inst.find_by_id(args.id)
    if not found:
        raise CuratorError(f"id 不存在:{args.id}")
    path, fm, body = found

    src = inst.location_of(path)
    if not src:
        raise CuratorError(f"{args.id} 不在受管位置,无法 promote")
    dst = _resolve_dst(inst, args)
    if dst["kind"] == "git_inside":
        inst.assert_writable(dst["sub_kb"])  # 跨 git 线只能写可写挂载(rw),ro 拒
    if _same(src, dst):
        raise CuratorError("源与目标同位置")

    memory_judgment = (inst.ontology.get("name") == "memory-worklayer"
                       and fm.get("kind", "judgment") == "judgment")
    source_pinned = memory_judgment and src["kind"] == "work" and src["band"] == "sot"
    target_pinned = memory_judgment and dst["kind"] == "work" and dst["band"] == "sot"
    if (source_pinned or target_pinned) and not args.user_confirmed:
        raise CuratorError("memory pinned judgment 的迁入或迁出必须由用户明确确认；请加 --user-confirmed")
    if target_pinned:
        fm["user_confirmed"] = True

    # 光谱单向:不向 git 外回退
    if src["git"] == "inside" and dst["git"] == "outside":
        raise CuratorError(f"promote 不向 git 外回退:{inst.band_label_for(path)} -> {dst}")

    gate = None  # None / cleanse / desensitize
    if src["git"] == "outside" and dst["git"] == "outside":
        if inst.is_containment(src["band"]) and not inst.is_containment(dst["band"]):
            gate = "desensitize"
    elif src["git"] == "outside" and dst["git"] == "inside":
        if inst.is_containment(src["band"]):
            raise CuratorError(
                f"containment 桶 {src['band']} 不能一步跨 git 线;先脱敏 promote 到 shared,再跨线"
            )
        gate = "cleanse"

    if gate == "cleanse" and not args.confirm:
        print(f"{args.id}:{src['band']} -> {dst['sub_kb']}/{dst['role']} 跨 git 线(提交进子 KB、跨用户),需清洗确认。")
        print("核对后加 --confirm 重跑:")
        for item in CLEANSE_CHECKLIST:
            print(f"  - {item}")
        return 3
    if gate == "desensitize" and not args.confirm:
        print(f"{args.id}:{src['band']} -> {dst['band']} 出 containment 桶,需脱敏确认。")
        print("核对后加 --confirm 重跑:")
        for item in DESENSITIZE_CHECKLIST:
            print(f"  - {item}")
        return 3

    topic = None
    if args.topic:
        topic = args.topic.strip().strip("/")
        if not topic or os.path.isabs(args.topic) or ".." in topic.split("/"):
            raise CuratorError(f"topic 非法:{args.topic}(须相对子目录,不含 .. 或绝对路径)")
    dst_dir = _container(inst, dst, topic)
    src_sub = os.path.relpath(os.path.dirname(path), _container(inst, src, src.get("topic")))
    if topic:
        # Topical work bands already consume topic in dst_dir. Other targets keep
        # the historical behavior of using it as a subdirectory.
        dst_sub = "" if dst["kind"] == "work" and inst.band_is_topical(dst["band"]) else topic
    else:
        dst_sub = "" if src_sub == "." else src_sub

    if not args.topic:
        existing = _dir_topics(dst_dir)
        if existing:
            tgt = f"{dst['sub_kb']}/{dst['role']}" if dst["kind"] == "git_inside" else dst["band"]
            print(f"目标 {tgt} 已有 topic:{', '.join(existing)}")
            print(f"本次归入:{dst_sub or '(根)'};要并入已有用 --topic 指定。")

    dst_path = os.path.join(dst_dir, dst_sub, f"{args.id}.md") if dst_sub else os.path.join(dst_dir, f"{args.id}.md")

    # 落点 stamp:tier = 角色层 / 工作层 band;git 内补 sub_kb,工作层去掉。
    if dst["kind"] == "git_inside":
        fm["tier"] = dst["role"]
        fm["sub_kb"] = dst["sub_kb"]
    else:
        fm["tier"] = dst["band"]
        fm.pop("sub_kb", None)

    if os.path.exists(dst_path) and os.path.abspath(dst_path) != os.path.abspath(path):
        raise CuratorError(f"目标已存在,不覆盖:{os.path.relpath(dst_path, inst.root)}")

    src_rel = inst.doc_key(path)        # profile_doc 规范键(视图下也不退化成 ../..)
    dst_rel = inst.doc_key(dst_path)
    g = Graph(inst)
    doc_refs = g.entities_with_doc(src_rel)

    gate_note = {"cleanse": " (跨 git 线,已清洗确认)", "desensitize": " (出 containment,已脱敏确认)"}.get(gate, "")
    dst_label = f"{dst['sub_kb']}/{dst['role']}" if dst["kind"] == "git_inside" else dst["band"]

    if args.dry_run:
        print(f"[dry-run] 将 promote {args.id} -> {dst_label}{gate_note}")
        print(f"  {src_rel} -> {dst_rel}")
        if doc_refs:
            print(f"  并更新 profile_doc 引用:{', '.join(doc_refs)}")
        return 0

    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    write_note(dst_path, fm, body)
    if os.path.abspath(dst_path) != os.path.abspath(path):
        os.remove(path)
    changed = g.retarget_doc(src_rel, dst_rel)
    if changed:
        g.save(changed)
    rebuild_index(inst)
    record_event(inst, args.id, "promoted", to=dst_label, gate=gate or "none")

    print(f"已 promote {args.id} -> {dst_label}{gate_note}")
    print(f"  {src_rel} -> {dst_rel}")
    if doc_refs:
        print(f"  已更新 profile_doc 引用:{', '.join(doc_refs)}")
    return 0


def add_parser(sub):
    p = sub.add_parser("promote", help="沿成熟度轴跨闸(工作层↔git 内子 KB)")
    p.add_argument("--id", required=True)
    p.add_argument("--to-role", dest="to_role", help="进 git 内角色层(global/domain),配 --to-sub-kb")
    p.add_argument("--to-band", dest="to_band", help="进工作层 band(private/shared)")
    p.add_argument("--to-sub-kb", dest="to_sub_kb", help="进 git 内时的目标子 KB(配 --to-role)")
    p.add_argument("--topic", help="归入目标的此 topic 子目录(默认沿用源;优先并入已有)")
    p.add_argument("--confirm", action="store_true", help="确认清洗 / 脱敏已完成")
    p.add_argument("--user-confirmed", action="store_true", help="memory pinned judgment:声明用户已明确确认")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="只预演,不写盘")
    p.set_defaults(func=run)
