"""INDEX.md 重建。按位置分组:子 KB 的角色层(git 内)+ 工作层 private/shared(git 外)。

graph 段暂用单图入口(Phase 4 改 union)。
"""

from __future__ import annotations

import os

from .config import Instance
from .util import atomic_write


def _meta(fm: dict) -> str:
    bits = []
    if fm.get("kind") == "fact" and fm.get("doc_type"):
        bits.append(fm["doc_type"])
    elif fm.get("kind"):
        bits.append(fm["kind"])
    if fm.get("confidence"):
        bits.append(fm["confidence"])
    if fm.get("status") == "deprecated":
        bits.append("deprecated")
    return " · ".join(bits)


def rebuild_index(inst: Instance) -> str:
    groups: dict[str, list] = {}
    labels: dict[str, str] = {}
    for path, fm, _ in inst.iter_notes(include_indexed=True):
        loc = inst.location_of(path)
        if not loc:
            continue
        if loc["kind"] == "git_inside":
            key = f"{loc['sub_kb']}/{loc['role']}"
            labels[key] = f"{key}(git 内)"
        else:
            key = f"~{loc['band']}"
            labels[key] = f"{loc['band']}(git 外)"
        groups.setdefault(key, []).append((path, fm))

    lines = ["# 索引(自动维护,勿手改;源:各文件 frontmatter)", ""]
    for key in sorted(groups):
        lines.append(f"## {labels[key]}")
        lines.append("")
        for path, fm in sorted(groups[key], key=lambda e: e[1].get("id", "")):
            rel = os.path.relpath(path, inst.root)
            meta = _meta(fm)
            meta = f"({meta})" if meta else ""
            lines.append(f"- [{fm.get('id')}]({rel}){meta} — {fm.get('summary', '')}")
        lines.append("")

    from .graphdata import Graph

    g = Graph(inst)
    if g.entities or g.edges:
        lines.append("## graph(sot 实体,跨子 KB union)")
        lines.append("")
        lines.append("源:各子 KB 的 `graph/entities.yaml`,id 命名空间化;查询见 `curator graph`。")
        lines.append("")
        if g.entities:
            by_type: dict[str, list] = {}
            for e in g.entities:
                by_type.setdefault(e.get("type", "?"), []).append(e.get("id"))
            for etype in sorted(by_type):
                ids = ", ".join(sorted(filter(None, by_type[etype])))
                lines.append(f"- 实体 `{etype}`({len(by_type[etype])}):{ids}")
        if g.edges:
            edge_count: dict[str, int] = {}
            for e in g.edges:
                edge_count[e.get("type", "?")] = edge_count.get(e.get("type", "?"), 0) + 1
            summary = ", ".join(f"{t} ×{n}" for t, n in sorted(edge_count.items()))
            lines.append(f"- 边:{summary}")
        if not g.entities and not g.edges:
            lines.append("(空)")
        lines.append("")

    text = "\n".join(lines).rstrip("\n") + "\n"
    atomic_write(inst.abspath(inst.index_file), text)
    return text
