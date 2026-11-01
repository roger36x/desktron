"""curator graph —— sot 校验与通用查询。无领域名词,只认类型化实体/边/悬空校验/backref。"""

from __future__ import annotations

import os

from ..config import Instance
from ..graphdata import Graph
from ..util import CuratorError


def _target_sub_kb(inst: Instance, args) -> str:
    """graph 写命令的目标分区:--sub-kb 优先;只有一个子 KB 时默认它;否则要求显式指定。
    无子 KB 的实例(记忆工作层)只有实例根一个分区,返回 None、不加命名空间。"""
    sk = getattr(args, "sub_kb", None)
    if not inst.sub_kbs:
        if sk:
            raise CuratorError(f"本实例无子 KB,不接受 --sub-kb:{sk}")
        return None
    if sk:
        if sk not in inst.sub_kbs:
            raise CuratorError(f"sub_kb 不在实例:{sk}({inst.sub_kbs})")
    elif len(inst.sub_kbs) == 1:
        sk = inst.sub_kbs[0]
    else:
        raise CuratorError(f"多子 KB,需 --sub-kb 指定目标分区:{inst.sub_kbs}")
    inst.assert_writable(sk)  # 只读挂载(ro)的子 KB 不能写 graph
    return sk


def _target_partition_opt(inst: Instance, args):
    """查询用的可选分区:--sub-kb 给则限定,否则 None(全实例)。"""
    sk = getattr(args, "sub_kb", None)
    if sk and sk not in inst.sub_kbs:
        raise CuratorError(f"sub_kb 不在实例:{sk}({inst.sub_kbs})")
    return sk


def _qid(inst: Instance, eid: str) -> str:
    """查询用:裸 id 在单子 KB 下补命名空间;多子 KB 下要求传 `<sub-kb>:<id>`。"""
    if eid is None or ":" in eid:
        return eid
    if len(inst.sub_kbs) == 1:
        return f"{inst.sub_kbs[0]}:{eid}"
    return eid


def cmd_check(inst: Instance, args) -> int:
    g = Graph(inst)
    entity_types = inst.entity_types
    edge_types = inst.edge_types
    errors = 0
    warns = 0

    def err(msg):
        nonlocal errors
        errors += 1
        print(f"ERROR {msg}")

    def warn(msg):
        nonlocal warns
        warns += 1
        print(f"WARN  {msg}")

    seen = set()
    for e in g.entities:
        eid = e.get("id")
        if not eid:
            err(f"实体缺 id:{e}")
            continue
        if eid in seen:
            err(f"实体 id 重复:{eid}")
        seen.add(eid)
        et = e.get("type")
        if et not in entity_types:
            err(f"实体 {eid}:type={et} 未在 ontology.entities 声明")
            continue
        # 该有却缺:requires 字段
        for f in entity_types[et].get("requires", []):
            if not e.get(f):
                warn(f"实体 {eid}:缺 requires 字段 {f}")
        # 悬空 backref:profile_doc 指向的文件须存在
        doc = e.get("profile_doc")
        if doc and not os.path.exists(inst.resolve_doc(doc)):
            err(f"实体 {eid}:profile_doc 悬空,文件不存在 {doc}")

    for edge in g.edges:
        et = edge.get("type")
        frm = edge.get("from")
        to = edge.get("to")
        if et not in edge_types:
            err(f"边 {frm}-{et}->{to}:type 未在 ontology.edges 声明")
            continue
        spec = edge_types[et]
        # 悬空:端点实体须存在(硬失败)
        for end, ev in (("from", frm), ("to", to)):
            if ev not in g.by_id:
                err(f"边 {frm}-{et}->{to}:{end} 端点 {ev} 不存在(悬空)")
        # 端点类型须匹配 ontology.edges 的 from/to
        if frm in g.by_id and spec.get("from"):
            at = g.by_id[frm].get("type")
            if at != spec["from"]:
                err(f"边 {frm}-{et}->{to}:from 类型 {at} != 声明 {spec['from']}")
        if to in g.by_id and spec.get("to"):
            at = g.by_id[to].get("type")
            if at != spec["to"]:
                err(f"边 {frm}-{et}->{to}:to 类型 {at} != 声明 {spec['to']}")

    _check_clusters(g, err, warn)

    print(
        f"\n实体 {len(g.entities)},边 {len(g.edges)},簇 {len(g.clusters)}:"
        f"{errors} ERROR,{warns} WARN"
    )
    return 1 if errors else 0


MAX_CLUSTER_DEPTH = 3  # 实体之上最多三层簇,按人的归类认知负担定。


def _check_clusters(g, err, warn):
    """簇视图校验:归属悬空、id 唯一、contains 与字段一致、深度、成环、未分簇。"""
    cluster_ids = set()
    for c in g.clusters:
        cid = c.get("id")
        if not cid:
            err(f"簇缺 id:{c}")
            continue
        if cid in cluster_ids:
            err(f"簇 id 重复:{cid}")
        cluster_ids.add(cid)
        if cid in g.by_id:
            err(f"簇 id 与实体 id 冲突:{cid}")

    # 归属指向必须是已声明的簇(实体与簇同理)
    for node in g.entities + g.clusters:
        c = node.get("cluster")
        if c and c not in g.cluster_by_id:
            err(f"{node.get('id')}:cluster 指向不存在的簇 {c}")

    # 成环 + 深度:沿 cluster 字段上溯
    for c in g.clusters:
        cid = c.get("id")
        chain = g.cluster_chain(cid)
        # 环:链回到自身,cluster_chain 已截断,这里显式探测
        cur = c.get("cluster")
        seen = {cid}
        looped = False
        while cur is not None:
            if cur in seen:
                err(f"簇 cluster 字段成环:{cid}")
                looped = True
                break
            seen.add(cur)
            cur = g._parent_cluster(cur)
        if not looped and len(chain) + 1 > MAX_CLUSTER_DEPTH:
            err(f"簇 {cid} 父链超过 {MAX_CLUSTER_DEPTH} 层(实体之上第 {len(chain) + 1} 层)")

    # contains 与按字段算出的成员一致
    for c in g.clusters:
        declared = list(c.get("contains") or [])
        actual = sorted(g.direct_members(c.get("id")))
        if sorted(declared) != actual:
            err(f"簇 {c.get('id')}:contains 与成员不一致,跑 graph sync-clusters 重新生成")

    # 实体未分簇:渐进填,不阻断
    for e in g.entities:
        if not e.get("cluster"):
            warn(f"实体 {e.get('id')}:未归入任何簇")


def cmd_neighbors(inst: Instance, args) -> int:
    g = Graph(inst)
    eid = _qid(inst, args.id)
    if eid not in g.by_id:
        raise CuratorError(f"实体不存在:{eid}")
    rows = g.neighbors(eid, edge_type=args.edge, direction=args.dir)
    if not rows:
        print("(无)")
        return 0
    for nid, et, d in rows:
        arrow = "->" if d == "out" else "<-"
        print(f"{eid} {arrow}{et}{arrow} {nid}")
    return 0


def cmd_path(inst: Instance, args) -> int:
    g = Graph(inst)
    chain = g.path(_qid(inst, args.src), _qid(inst, args.dst))
    if chain is None:
        print(f"无路径:{args.src} -> {args.dst}")
        return 1
    print(" -> ".join(chain))
    return 0


def cmd_backref(inst: Instance, args) -> int:
    g = Graph(inst)
    eid = _qid(inst, args.id)
    if eid not in g.by_id:
        raise CuratorError(f"实体不存在:{eid}")
    ins = g.in_edges(eid)
    if not ins:
        print("(无入边)")
        return 0
    for e in ins:
        print(f"{e.get('from')} -{e.get('type')}-> {eid}")
    return 0


def cmd_edges(inst: Instance, args) -> int:
    g = Graph(inst)
    rows = [e for e in g.edges if args.type is None or e.get("type") == args.type]
    if not rows:
        print("(无)")
        return 0
    for e in rows:
        print(f"{e.get('from')} -{e.get('type')}-> {e.get('to')}")
    return 0


def _parse_set(pairs):
    """--set key=value。两种写法给列表:`key=[a,b]`,或同一个 key 重复给。

    此前只能产出字符串,而实体里 `domains` 这类字段是列表——既有那些列表是导入脚本
    直接写 YAML 造的,CLI 写不出来,写出来的是字符串,消费方按列表遍历就会逐字符拆开
    (2026-08-21 踩到)。
    """
    fields = {}
    for kv in pairs or []:
        if "=" not in kv:
            raise CuratorError(f"--set 需 key=value:{kv}")
        k, v = kv.split("=", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            val = [x.strip() for x in v[1:-1].split(",") if x.strip()]
        else:
            val = v
        if k in fields:   # 重复给同一个 key:累积成列表
            cur = fields[k] if isinstance(fields[k], list) else [fields[k]]
            fields[k] = cur + (val if isinstance(val, list) else [val])
        else:
            fields[k] = val
    return fields


def cmd_add_entity(inst: Instance, args) -> int:
    g = Graph(inst)
    sk = _target_sub_kb(inst, args)
    fields = _parse_set(args.set)
    if getattr(args, "cluster", None):
        fields["cluster"] = args.cluster
    g.add_entity(args.id, args.type, fields, sk)
    g.sync_clusters()
    g.save({sk})
    miss = [
        f
        for f in inst.entity_types.get(args.type, {}).get("requires", [])
        if not fields.get(f)
    ]
    print(f"加实体 {sk}:{args.id} (type={args.type})")
    if miss:
        print(f"  注:缺 requires 字段 {miss},graph check 会 WARN")
    return 0


def cmd_set_entity(inst: Instance, args) -> int:
    g = Graph(inst)
    sk = _target_sub_kb(inst, args)
    fields = _parse_set(args.set)
    if getattr(args, "cluster", None):
        fields["cluster"] = args.cluster
    changed = g.set_entity(args.id, fields, getattr(args, "unset", None), sk)
    if not changed:
        print(f"实体 {sk}:{args.id} 无改动")
        return 0
    g.sync_clusters()
    g.save({sk})
    print(f"改实体 {sk}:{args.id}:{', '.join(changed)}")
    return 0


def cmd_remove_entity(inst: Instance, args) -> int:
    g = Graph(inst)
    sk = _target_sub_kb(inst, args)
    g.remove_entity(args.id, sk)
    g.save({sk})
    print(f"移除实体 {sk}:{args.id}")
    return 0


def cmd_add_edge(inst: Instance, args) -> int:
    g = Graph(inst)
    sk = _target_sub_kb(inst, args)
    src = getattr(args, "source", None)
    g.add_edge(args.src, args.type, args.dst, sk, source=src)
    g.save({sk})
    # 取刚加的那条(add_edge 追加在末尾),不是同类型的第一条——后者会命中存量老边,
    # 它们没有 confidence 字段,取出来是 None。
    conf = g.edges[-1].get("confidence", "reported") if g.edges else "reported"
    tail = f",置信度 {conf}" + ("" if src else ";未给 --source,按无独立来源计 reported")
    print(f"加边 {args.src} -{args.type}-> {args.dst}(分区 {sk}{tail})")
    return 0


def cmd_remove_edge(inst: Instance, args) -> int:
    g = Graph(inst)
    sk = _target_sub_kb(inst, args)
    n = g.remove_edge(args.src, args.type, args.dst, sk)
    g.save({sk})
    print(f"移除 {n} 条边 {args.src} -{args.type}-> {args.dst}(分区 {sk})")
    return 0


def _fmt_types(types) -> str:
    """簇边的底层类型集合:同质显单一类型,混合标 mixed。"""
    ts = sorted(t for t in types if t)
    return ts[0] if len(ts) == 1 else "mixed"


def _print_cluster_edges(agg) -> None:
    if not agg:
        print("  (无簇边)")
        return
    for (a, b), info in sorted(agg.items()):
        print(f"  {a} -{_fmt_types(info['types'])}({info['count']})-> {b}")


def cmd_map(inst: Instance, args) -> int:
    """顶层簇及其派生边——快路由看的粗图。"""
    g = Graph(inst)
    partition = _target_partition_opt(inst, args)
    tops = g.top_clusters(partition)
    if not tops:
        print("(无顶层簇;先 graph add-cluster 建簇)")
        return 0
    print("顶层簇:")
    for cid in sorted(tops):
        cl = g.cluster_by_id[cid]
        desc = f" — {cl['desc']}" if cl.get("desc") else ""
        n = len(cl.get("contains") or g.direct_members(cid))
        print(f"  {cid}{desc}（{n} 成员）")
    print("派生簇边:")
    _print_cluster_edges(g.derived_cluster_edges(parent_id=None, partition=partition))
    return 0


def cmd_expand(inst: Instance, args) -> int:
    """展开一个簇:落到它的直接成员;成员是子簇则给子簇间派生边,是实体则给实体边。"""
    g = Graph(inst)
    cid = _qid(inst, args.cluster)
    if cid not in g.cluster_by_id:
        raise CuratorError(f"簇不存在:{cid}")
    members = g.direct_members(cid)
    sub_clusters = [m for m in members if m in g.cluster_by_id]
    entities = [m for m in members if m in g.by_id]
    cl = g.cluster_by_id[cid]
    desc = f" — {cl['desc']}" if cl.get("desc") else ""
    print(f"{cid}{desc}")
    if sub_clusters:
        print("子簇:")
        for m in sorted(sub_clusters):
            print(f"  {m}")
        print("子簇间派生边:")
        _print_cluster_edges(g.derived_cluster_edges(parent_id=cid))
    if entities:
        print("成员实体:")
        for m in sorted(entities):
            print(f"  {m}")
        inside = set(entities)
        rows = [
            e for e in g.edges
            if e.get("from") in inside and e.get("to") in inside
        ]
        print("实体间边:")
        if rows:
            for e in rows:
                print(f"  {e.get('from')} -{e.get('type')}-> {e.get('to')}")
        else:
            print("  (无)")
    if not members:
        print("(空簇)")
    return 0


def cmd_centrality(inst: Instance, args) -> int:
    """图中心性:PageRank、介数、出入度,加孤立点与弱连通分量提示。

    方向按边的存储方向(A -type-> B)。PageRank 质量沿边汇聚,被指向多者高;
    边语义由实例定,--reverse 反向。--edge 限定单一边类型(混类型会把不同关系搅在一起)。
    """
    from ..graphmetrics import centrality

    g = Graph(inst)
    partition = _target_partition_opt(inst, args)
    nodes = [
        e["id"] for e in g.entities
        if partition is None or e.get("_partition") == partition
    ]
    if not nodes:
        print("无实体,图为空,中心性无从算。先 graph add-entity 建图。")
        return 0
    nodeset = set(nodes)
    edges = [
        (e["from"], e["to"]) for e in g.edges
        if (partition is None or e.get("_partition") == partition)
        and (args.edge is None or e.get("type") == args.edge)
        and e.get("from") in nodeset and e.get("to") in nodeset
    ]
    if args.reverse:
        edges = [(b, a) for a, b in edges]

    m = centrality(nodes, edges)
    pr, bc, indeg, outdeg = m["pagerank"], m["betweenness"], m["indeg"], m["outdeg"]
    comps = m["components"]

    scope = f"分区 {partition}" if partition else "全实例"
    etag = f",边类型 {args.edge}" if args.edge else ""
    rev = ",已反向" if args.reverse else ""
    print(f"图({scope}{etag}{rev}):{len(nodes)} 实体,{len(edges)} 边,"
          f"{len(comps)} 个弱连通分量")
    print(f"分量规模:{sorted((len(c) for c in comps), reverse=True)}\n")

    top = args.top
    ranked = sorted(nodes, key=lambda v: (pr[v], bc[v], indeg[v]), reverse=True)[:top]
    print(f"中心性(按 PageRank 排序,前 {min(top, len(nodes))}):")
    print(f"  {'实体':<22}{'PageRank':>10}{'介数':>10}{'入度':>6}{'出度':>6}")
    for v in ranked:
        print(f"  {v:<22}{pr[v]:>10.4f}{bc[v]:>10.4f}{indeg[v]:>6}{outdeg[v]:>6}")

    isolated = [v for v in nodes if indeg[v] == 0 and outdeg[v] == 0]
    if isolated:
        print(f"\n孤立点(无边,{len(isolated)} 个):{', '.join(sorted(isolated))}")
        print("  无边的实体:或本就独立,或依赖关系尚未画进 graph。")
    return 0


def cmd_sync_clusters(inst: Instance, args) -> int:
    g = Graph(inst)
    changed = g.sync_clusters()
    if not changed:
        print("contains 已与 cluster 字段一致,无需重写")
        return 0
    g.save(changed)
    print(f"已重写 contains 索引,分区:{sorted(changed)}")
    return 0


def cmd_add_cluster(inst: Instance, args) -> int:
    g = Graph(inst)
    sk = _target_sub_kb(inst, args)
    g.add_cluster(args.id, args.desc, args.cluster, sk)
    g.sync_clusters()
    g.save({sk})
    parent = f",归入 {args.cluster}" if args.cluster else ""
    print(f"加簇 {sk}:{args.id}{parent}")
    return 0


def cmd_remove_cluster(inst: Instance, args) -> int:
    g = Graph(inst)
    sk = _target_sub_kb(inst, args)
    g.remove_cluster(args.id, sk)
    g.save({sk})
    print(f"移除簇 {sk}:{args.id}")
    return 0


def cmd_set_cluster(inst: Instance, args) -> int:
    g = Graph(inst)
    sk = _target_sub_kb(inst, args)
    g.set_membership(args.id, args.cluster, sk)
    changed = g.sync_clusters()
    g.save(changed | {sk})
    if args.cluster:
        print(f"{sk}:{args.id} 归入 {args.cluster}")
    else:
        print(f"{sk}:{args.id} 已清除簇归属")
    return 0


def run(inst: Instance, args) -> int:
    return args.graph_func(inst, args)


def add_parser(sub):
    p = sub.add_parser("graph", help="sot 校验与通用查询(neighbors/path/backref/edges/check)")
    gsub = p.add_subparsers(dest="graph_cmd", required=True)

    c = gsub.add_parser("check", help="悬空引用硬失败、该有却缺 WARN")
    c.set_defaults(graph_func=cmd_check)

    n = gsub.add_parser("neighbors", help="直连实体")
    n.add_argument("id")
    n.add_argument("--edge", help="按边类型过滤")
    n.add_argument("--dir", choices=["out", "in", "both"], default="both")
    n.set_defaults(graph_func=cmd_neighbors)

    pa = gsub.add_parser("path", help="边图上 BFS 最短路")
    pa.add_argument("src")
    pa.add_argument("dst")
    pa.set_defaults(graph_func=cmd_path)

    b = gsub.add_parser("backref", help="谁指向它(入边)")
    b.add_argument("id")
    b.set_defaults(graph_func=cmd_backref)

    e = gsub.add_parser("edges", help="按边类型反查")
    e.add_argument("type", nargs="?")
    e.set_defaults(graph_func=cmd_edges)

    ae = gsub.add_parser("add-entity", help="加实体(类型须在 ontology 声明)")
    ae.add_argument("--id", required=True)
    ae.add_argument("--type", required=True)
    ae.add_argument("--sub-kb", dest="sub_kb", help="目标子 KB 分区(单子 KB 可省)")
    ae.add_argument("--cluster", help="归入的簇 id(须先 add-cluster)")
    ae.add_argument("--set", action="append", help="字段 key=value(可重复)")
    ae.set_defaults(graph_func=cmd_add_entity)

    se = gsub.add_parser("set-entity", help="改已有实体的字段(id / type 不可改)")
    se.add_argument("--id", required=True)
    se.add_argument("--sub-kb", dest="sub_kb", help="目标子 KB 分区(单子 KB 可省)")
    se.add_argument("--cluster", help="改归的簇 id(须先 add-cluster)")
    se.add_argument("--set", action="append", help="字段 key=value(可重复)")
    se.add_argument("--unset", action="append", help="删掉一个字段(可重复)")
    se.set_defaults(graph_func=cmd_set_entity)

    re = gsub.add_parser("remove-entity", help="移除实体(仍被边引用则拒)")
    re.add_argument("--id", required=True)
    re.add_argument("--sub-kb", dest="sub_kb", help="目标子 KB 分区(单子 KB 可省)")
    re.set_defaults(graph_func=cmd_remove_entity)

    aedge = gsub.add_parser("add-edge", help="加边(类型/端点须合法;端点可裸可命名空间)")
    aedge.add_argument("--from", dest="src", required=True)
    aedge.add_argument("--type", required=True)
    aedge.add_argument("--to", dest="dst", required=True)
    aedge.add_argument("--sub-kb", dest="sub_kb", help="边写入的分区(单子 KB 可省)")
    aedge.add_argument("--source", help="来源(与笔记同一套判据:0 个独立来源 reported、1 个 documented、≥2 个 corroborated)")
    aedge.set_defaults(graph_func=cmd_add_edge)

    redge = gsub.add_parser("remove-edge", help="移除边")
    redge.add_argument("--from", dest="src", required=True)
    redge.add_argument("--type", required=True)
    redge.add_argument("--to", dest="dst", required=True)
    redge.add_argument("--sub-kb", dest="sub_kb", help="边所在分区(单子 KB 可省)")
    redge.set_defaults(graph_func=cmd_remove_edge)

    # —— 簇视图(实体之上的分组层) ——
    m = gsub.add_parser("map", help="顶层簇及派生边(快路由粗图)")
    m.add_argument("--sub-kb", dest="sub_kb", help="限定分区(多子 KB 时)")
    m.set_defaults(graph_func=cmd_map)

    ex = gsub.add_parser("expand", help="展开簇到直接成员(子簇间派生边 / 实体间边)")
    ex.add_argument("cluster")
    ex.set_defaults(graph_func=cmd_expand)

    sc = gsub.add_parser("sync-clusters", help="按 cluster 字段重写所有 contains 索引")
    sc.set_defaults(graph_func=cmd_sync_clusters)

    ct = gsub.add_parser("centrality", help="图中心性:PageRank、介数、出入度,加孤立点/分量提示")
    ct.add_argument("--sub-kb", dest="sub_kb", help="限定分区(多子 KB 时)")
    ct.add_argument("--edge", help="限定单一边类型(默认全部,混类型会搅在一起)")
    ct.add_argument("--top", type=int, default=15, help="榜单显示前 N(默认 15)")
    ct.add_argument("--reverse", action="store_true", help="反向边再算(按你的边语义决定方向)")
    ct.set_defaults(graph_func=cmd_centrality)

    ac = gsub.add_parser("add-cluster", help="加簇(可归入上层簇)")
    ac.add_argument("--id", required=True)
    ac.add_argument("--desc", help="簇说明")
    ac.add_argument("--cluster", help="上层簇 id(归入更高一层)")
    ac.add_argument("--sub-kb", dest="sub_kb", help="目标子 KB 分区(单子 KB 可省)")
    ac.set_defaults(graph_func=cmd_add_cluster)

    rc = gsub.add_parser("remove-cluster", help="移除簇(仍有成员则拒)")
    rc.add_argument("--id", required=True)
    rc.add_argument("--sub-kb", dest="sub_kb", help="目标子 KB 分区(单子 KB 可省)")
    rc.set_defaults(graph_func=cmd_remove_cluster)

    sm = gsub.add_parser("set-cluster", help="改实体/簇的归属(--cluster 留空清除)")
    sm.add_argument("--id", required=True)
    sm.add_argument("--cluster", default="", help="目标簇 id;留空清除归属")
    sm.add_argument("--sub-kb", dest="sub_kb", help="目标子 KB 分区(单子 KB 可省)")
    sm.set_defaults(graph_func=cmd_set_cluster)

    p.set_defaults(func=run)
