"""curator doctor —— 实例结构自检。monorepo 结构 / 路径 / ontology 一致性,不碰内容。"""

from __future__ import annotations

import os

from ..config import BASE_CONFIDENCE, Instance


def _under_root(inst: Instance, rel: str) -> bool:
    root = os.path.abspath(inst.root)
    target = os.path.abspath(os.path.join(root, rel))
    return target == root or target.startswith(root + os.sep)


def run(inst: Instance, args) -> int:
    problems = []
    notes = []

    def err(m):
        problems.append(m)

    def note(m):
        notes.append(m)

    # 子 KB:声明非空;目录在根内
    if not inst.sub_kbs:
        note("instance.sub_kbs 为空:无 git 内子 KB(纯工作层实例?)")
    for sk in inst.sub_kbs:
        if not os.path.isdir(inst.sub_kb_dir(sk)):
            note(f"子 KB 目录不存在:{sk} -> {inst.sub_kb_dir(sk)}")
        mode = inst.mount_mode(sk)
        if mode == "ro":
            note(f"子 KB {sk}:只读挂载(ro,只消费)")

    if not inst.role_layers:
        err("instance.role_layers 为空:子 KB 内无角色层")

    # 工作层:temp/private/shared 目录在根内;须有一段免闸 temp
    wl = inst.work_layer
    for band, spec in wl.items():
        rel = (spec or {}).get("dir") or band
        if not _under_root(inst, rel):
            err(f"工作层 {band} 目录越出实例根:{rel}")
    if not inst.temp_band or wl.get(inst.temp_band, {}).get("gated") is not False:
        note("工作层无 gated:false 的 temp:免闸暂存缺位")
    # 免闸 ≠ 草稿。gc 与 temp clean 按 draft_bands 作用,不按 ungated_bands——前者是「可回收
    # 的草稿」,后者只是「写入绕过交互闸」。两者曾经等价(内核模板里唯一的免闸 band 就叫 temp),
    # v3 记忆布局引入第二个免闸 band 之后不再成立:notes 是永久判断档案、journal 是 append-only
    # 流水,都免闸但都不可回收。此处提示差集,让「这个 band 会不会被 gc 吞掉」在体检里看得见。
    reclaimable = set(inst.draft_bands)
    protected = [b for b in inst.ungated_bands if b not in reclaimable]
    if protected:
        note(f"免闸但不参与 gc/temp-clean 的 band(已标 draft:false):{'、'.join(protected)}")
    if reclaimable:
        note(f"gc/temp-clean 会回收的 band:{'、'.join(sorted(reclaimable))}")
    if not inst.work_products:
        note("工作层无成品 band(private/shared)")

    # gc.promote_to(若声明)须是工作层成品 band 且非 containment——自动晋升只到此
    gc_cfg = inst.ontology.get("gc", {}) or {}
    pt = gc_cfg.get("promote_to")
    if pt is not None:
        if pt not in inst.work_products:
            err(f"gc.promote_to={pt} 不是工作层成品 band {inst.work_products}")
        elif inst.is_containment(pt):
            err(f"gc.promote_to={pt} 是 containment 桶,不能作自动晋升目标")

    # index 路径在根内(各子 KB 的 graph 路径由 sub_kbs 检查覆盖)
    if not _under_root(inst, inst.index_file):
        err(f"index_file 越出实例根:{inst.index_file}")

    # confidence_levels 须含基线三档
    for c in BASE_CONFIDENCE:
        if c not in inst.confidence_levels:
            err(f"confidence_levels 缺基线档 {c}:{inst.confidence_levels}")

    # 边端点类型须在 entities 声明
    et = inst.entity_types
    for name, spec in inst.edge_types.items():
        for end in ("from", "to"):
            ref = spec.get(end)
            if ref and ref not in et:
                err(f"边 {name} 的 {end}={ref} 未在 ontology.entities 声明")

    for name, spec in et.items():
        fields = set(spec.get("fields", []))
        for r in spec.get("requires", []):
            if r not in fields:
                note(f"实体 {name}:requires {r} 不在 fields {sorted(fields)}")

    if not inst.rules.get("required_fields"):
        err("rules.required_fields 为空,基础闸失效")

    for m in notes:
        print(f"NOTE  {m}")
    for m in problems:
        print(f"ERROR {m}")
    print(f"\n实例 {inst.root}:{len(problems)} ERROR,{len(notes)} NOTE")
    return 1 if problems else 0


def add_parser(sub):
    p = sub.add_parser("doctor", help="实例结构自检:monorepo / 路径 / ontology 一致性")
    p.set_defaults(func=run)
