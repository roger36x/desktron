"""curator subkb add <name> —— 在既有实例里加一个子 KB。

把原先手动的三件事收成一条命令:改 ontology.sub_kbs、落骨架(角色层 + graph + index)、
提示 CODEOWNERS。引擎只动结构,不写任何领域内容。ontology.yaml 按行文本编辑(只改/插一行),
不经 yaml.safe_dump,保住注释。
"""

from __future__ import annotations

import os
import re

from ..config import Instance
from ..util import CuratorError, atomic_write, valid_id

_GRAPH_STUB = (
    "# sot 层:类型化实体 + 类型化有向边。类型在 ontology.yaml 声明,本文件只放实例数据。\n"
    "# profile_doc 单向 backref 指向叙事笔记;叙事不反指 sot。\n\n"
    "entities: []\n\nedges: []\n"
)


def _index_stub(name: str, roles: list[str]) -> str:
    lines = [f"# {name}", "", "子 KB 入口与导航(只放导航,不放事实)。", ""]
    hint = {
        "global": "概览、术语表、服务与仓库目录(供人阅读)",
        "domain": "业务事实,按子域划分(只放事实;接口 / 字段 / 口径用 doc_type=reference 承载,结构关系进 graph)",
    }
    for r in roles:
        lines.append(f"- `{r}/` — {hint.get(r, '角色层')}")
    lines.append("- `graph/entities.yaml` — sot:类型化实体、类型化边、指向笔记的反向引用")
    lines.append("")
    return "\n".join(lines)


def _add_to_sub_kbs(onto_path: str, name: str) -> None:
    """把 name 加进 ontology 的 sub_kbs 列表,只动那一处,保住其余文本与注释。"""
    with open(onto_path, encoding="utf-8") as f:
        lines = f.readlines()
    # 行内列表:sub_kbs: [a, b]
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)sub_kbs:\s*\[(.*)\]\s*$", line.rstrip("\n"))
        if m:
            indent, inner = m.group(1), m.group(2).strip()
            items = [x.strip() for x in inner.split(",") if x.strip()]
            if name in items:
                return
            items.append(name)
            lines[i] = f"{indent}sub_kbs: [{', '.join(items)}]\n"
            atomic_write(onto_path, "".join(lines))
            return
    # 块状列表:sub_kbs:\n  - a\n  - b
    for i, line in enumerate(lines):
        if re.match(r"^(\s*)sub_kbs:\s*$", line.rstrip("\n")):
            j, last, item_indent = i + 1, i, None
            while j < len(lines):
                mm = re.match(r"^(\s*)-\s*(\S+)\s*$", lines[j])
                if not mm:
                    break
                if mm.group(2).strip() == name:
                    return
                item_indent, last = mm.group(1), j
                j += 1
            if item_indent:
                lines.insert(last + 1, f"{item_indent}- {name}\n")
                atomic_write(onto_path, "".join(lines))
                return
            # dict 形式(sub_kbs:\n  common-kb:\n    gate_level: ...):找末尾追加
            dk, dk_indent = i + 1, None
            while dk < len(lines):
                dm = re.match(r"^(\s+)(\S+):\s*$", lines[dk])
                if dm:
                    dk_indent = dm.group(1)
                    dk += 1
                    while dk < len(lines) and re.match(r"^\s{" + str(len(dk_indent) + 2) + r",}", lines[dk]):
                        dk += 1
                else:
                    break
            if dk_indent:
                lines.insert(dk, f"{dk_indent}{name}:\n{dk_indent}  gate_level: normal\n")
                atomic_write(onto_path, "".join(lines))
                return
    raise CuratorError("ontology.yaml 未找到 sub_kbs 声明;请手动加入后重试")


def run_subkb_add(inst: Instance, args) -> int:
    name = args.name
    if not valid_id(name):
        raise CuratorError(f"子 KB 名非法:{name}(须 ^[a-z0-9][a-z0-9-]*$)")
    if name in inst.sub_kbs:
        raise CuratorError(f"子 KB 已声明:{name}")
    skd = inst.sub_kb_dir(name)
    if os.path.exists(skd):
        raise CuratorError(f"目录已存在:{skd}(避免覆盖,先移走或换名)")

    roles = list(inst.role_layers)
    graph_rel = inst.sub_kb_graph
    index_rel = inst.sub_kb_index
    planned = [f"{name}/{r}/.gitkeep" for r in roles]
    planned += [f"{name}/{graph_rel}", f"{name}/{index_rel}"]

    if getattr(args, "dry_run", False):
        print(f"[dry-run] 将建子 KB:{name}")
        for c in planned:
            print(f"  + {c}")
        print(f"  ~ ontology.yaml: sub_kbs += {name}")
        return 0

    for r in roles:
        atomic_write(os.path.join(inst.role_dir(name, r), ".gitkeep"), "")
    atomic_write(os.path.join(skd, graph_rel), _GRAPH_STUB)
    atomic_write(os.path.join(skd, index_rel), _index_stub(name, roles))
    _add_to_sub_kbs(os.path.join(inst.gov_root, "ontology.yaml"), name)

    print(f"已建子 KB:{name}")
    for c in planned:
        print(f"  + {c}")
    print(f"  ~ ontology.yaml: sub_kbs += {name}")
    print("\n下一步:")
    print(f"  - CODEOWNERS 加一行定归属,例:/{name}/  @your-team")
    print(f"  - curator add --sub-kb {name} --role {roles[0]} ...")
    print("  - curator doctor 核一致性")
    return 0


def add_parser(sub):
    p = sub.add_parser("subkb", help="子 KB 管理(在既有实例里加子 KB)")
    ssub = p.add_subparsers(dest="subkb_cmd", required=True)
    ap = ssub.add_parser("add", help="加一个子 KB:改 ontology.sub_kbs + 落骨架")
    ap.add_argument("name", help="子 KB id(顶层目录名,^[a-z0-9][a-z0-9-]*$)")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true", help="只预演不写盘")
    ap.set_defaults(func=run_subkb_add)
