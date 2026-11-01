"""sot 层:多子 KB 的 graph 分区 union 成一张图,实体 id 命名空间化为 `<sub-kb>:<id>`。

每个子 KB 一份 `<sub-kb>/graph/entities.yaml`,文件内 id 写裸值;加载时按所在子 KB 加前缀,
union 后查询/校验都用命名空间 id。跨子 KB 边成立(`a-kb:x depends_on b-kb:y`)。
写回按分区:同分区的 id 落裸值,跨分区端点保留命名空间。类型在 ontology 声明,本模块不认领域名词。
"""

from __future__ import annotations

import os
from collections import deque

import yaml

from .config import Instance
from .util import CuratorError, atomic_write


def is_entity_backed(inst: Instance, fm: dict) -> bool:
    """笔记是否为实体型:kind=fact 且 doc_type 在 ontology.entity_backed_doc_types。

    实体型笔记须在 graph 有对应实体(某实体 profile_doc 指向它)。
    entity_backed_doc_types 空 / 不声明时恒 False,强制关闭,向后兼容。
    """
    return (
        fm.get("kind") == "fact"
        and fm.get("doc_type") in inst.entity_backed_doc_types
    )


def _qualify(eid: str, partition: str) -> str:
    """裸 id → `<partition>:<id>`;已带命名空间(含 :)的保持不变。
    partition 为 None(单分区实例)时不加前缀。"""
    if eid is None or partition is None:
        return eid
    return eid if ":" in str(eid) else f"{partition}:{eid}"


def _strip(qid: str, partition: str) -> str:
    """写回某分区时:本分区前缀去掉(落裸值),跨分区保留命名空间。"""
    if partition is None:
        return qid
    pre = f"{partition}:"
    return qid[len(pre):] if isinstance(qid, str) and qid.startswith(pre) else qid


class Graph:
    def __init__(self, inst: Instance):
        self.inst = inst
        self.entities = []   # 每个含命名空间 id + _partition
        self.edges = []      # from/to 命名空间 id + _partition
        self.clusters = []   # 簇:实体之上的分组层,含命名空间 id + _partition
        self.by_id = {}
        self.cluster_by_id = {}
        for sk, base in inst.graph_partitions:
            path = os.path.join(base, inst.sub_kb_graph)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            for e in data.get("entities") or []:
                ent = dict(e)
                ent["id"] = _qualify(e.get("id"), sk)
                if ent.get("cluster"):
                    ent["cluster"] = _qualify(e.get("cluster"), sk)
                ent["_partition"] = sk
                self.entities.append(ent)
                self.by_id[ent["id"]] = ent
            for ed in data.get("edges") or []:
                edge = dict(ed)
                edge["from"] = _qualify(ed.get("from"), sk)
                edge["to"] = _qualify(ed.get("to"), sk)
                edge["_partition"] = sk
                self.edges.append(edge)
            for c in data.get("clusters") or []:
                cl = dict(c)
                cl["id"] = _qualify(c.get("id"), sk)
                if cl.get("cluster"):
                    cl["cluster"] = _qualify(c.get("cluster"), sk)
                if cl.get("contains"):
                    cl["contains"] = [_qualify(x, sk) for x in c["contains"]]
                cl["_partition"] = sk
                self.clusters.append(cl)
                self.cluster_by_id[cl["id"]] = cl

    # —— 遍历 ——

    def out_edges(self, eid):
        return [e for e in self.edges if e.get("from") == eid]

    def in_edges(self, eid):
        return [e for e in self.edges if e.get("to") == eid]

    def neighbors(self, eid, edge_type=None, direction="both"):
        out = []
        if direction in ("out", "both"):
            for e in self.out_edges(eid):
                if edge_type is None or e.get("type") == edge_type:
                    out.append((e.get("to"), e.get("type"), "out"))
        if direction in ("in", "both"):
            for e in self.in_edges(eid):
                if edge_type is None or e.get("type") == edge_type:
                    out.append((e.get("from"), e.get("type"), "in"))
        return out

    # —— 簇:实体之上的分组层 ——

    def _parent_cluster(self, node_id):
        """节点(实体或簇)的直接上层簇 id(cluster 字段值);无则 None。"""
        if node_id in self.cluster_by_id:
            return self.cluster_by_id[node_id].get("cluster")
        if node_id in self.by_id:
            return self.by_id[node_id].get("cluster")
        return None

    def cluster_chain(self, node_id):
        """从节点直接上层簇起,沿 cluster 字段向上的簇 id 链(自底向上)。遇环即止,不重复。"""
        chain = []
        seen = {node_id}
        cur = self._parent_cluster(node_id)
        while cur is not None and cur not in seen:
            chain.append(cur)
            seen.add(cur)
            cur = self._parent_cluster(cur)
        return chain

    def _child_under(self, node_id, parent_id):
        """parent_id 的直接成员里,等于 node_id 或为 node_id 祖先的那一个;node_id 不在 parent_id 之下则 None。

        parent_id=None 时求顶层归属:返回 node_id 链上无上层簇的那个簇(顶层簇)。
        """
        cur = node_id
        seen = {None}
        while cur is not None and cur not in seen:
            par = self._parent_cluster(cur)
            if par == parent_id:
                return cur
            seen.add(cur)
            cur = par
        return None

    def direct_members(self, cluster_id):
        """直接挂在某簇下的成员(实体 + 子簇)id 列表,按 cluster 字段算。"""
        out = [e["id"] for e in self.entities if e.get("cluster") == cluster_id]
        out += [c["id"] for c in self.clusters if c.get("cluster") == cluster_id]
        return out

    def top_clusters(self, partition=None):
        """顶层簇(无 cluster 字段)。partition 限定分区。"""
        return [
            c["id"]
            for c in self.clusters
            if not c.get("cluster")
            and (partition is None or c.get("_partition") == partition)
        ]

    def derived_cluster_edges(self, parent_id=None, partition=None):
        """某一层上的派生簇边。

        层由 parent_id 定:None = 顶层簇之间;否则 = parent_id 的直接子簇/直接成员实体之间。
        把每条实体边的两端各映射到该层的成员,两端落在不同成员且都非空即产生一条簇边。
        返回 {(a, b): {"types": set, "count": int}},types 为底层边类型集合,count 为底层边条数。
        """
        agg = {}
        for ed in self.edges:
            if partition is not None and ed.get("_partition") != partition:
                continue
            a = self._child_under(ed.get("from"), parent_id)
            b = self._child_under(ed.get("to"), parent_id)
            if not a or not b or a == b:
                continue
            # 簇边只在簇之间;未分簇的实体上溯到自身,不算簇节点,跳过。
            if a not in self.cluster_by_id or b not in self.cluster_by_id:
                continue
            slot = agg.setdefault((a, b), {"types": set(), "count": 0})
            slot["types"].add(ed.get("type"))
            slot["count"] += 1
        return agg

    def sync_clusters(self):
        """按 cluster 字段重算每个簇的 contains 索引。返回被改动的分区集合。"""
        changed = set()
        for cl in self.clusters:
            members = sorted(self.direct_members(cl["id"]))
            if list(cl.get("contains") or []) != members:
                changed.add(cl.get("_partition"))
            if members:
                cl["contains"] = members
            else:
                cl.pop("contains", None)
        return changed

    def add_cluster(self, cid, desc, parent, sub_kb):
        qid = _qualify(cid, sub_kb)
        if qid in self.cluster_by_id:
            raise CuratorError(f"簇已存在:{qid}")
        if qid in self.by_id:
            raise CuratorError(f"簇 id 与实体 id 冲突:{qid}")
        cl = {"id": qid, "_partition": sub_kb}
        if desc:
            cl["desc"] = desc
        if parent:
            qp = _qualify(parent, sub_kb)
            if qp not in self.cluster_by_id:
                raise CuratorError(f"上层簇不存在:{qp}")
            cl["cluster"] = qp
        # 字段顺序:id, desc, cluster, _partition
        ordered = {"id": qid}
        if desc:
            ordered["desc"] = desc
        if parent:
            ordered["cluster"] = cl["cluster"]
        ordered["_partition"] = sub_kb
        self.clusters.append(ordered)
        self.cluster_by_id[qid] = ordered

    def remove_cluster(self, cid, sub_kb):
        qid = _qualify(cid, sub_kb)
        if qid not in self.cluster_by_id:
            raise CuratorError(f"簇不存在:{qid}")
        members = self.direct_members(qid)
        if members:
            raise CuratorError(f"簇 {qid} 仍有 {len(members)} 个直接成员,先改它们的归属")
        self.clusters = [c for c in self.clusters if c.get("id") != qid]
        del self.cluster_by_id[qid]

    def set_membership(self, node_id, cluster, sub_kb):
        """改实体或簇的 cluster 归属;cluster 为空清除归属。返回受影响分区。"""
        qid = _qualify(node_id, sub_kb)
        node = self.cluster_by_id.get(qid) or self.by_id.get(qid)
        if node is None:
            raise CuratorError(f"实体或簇不存在:{qid}")
        if cluster:
            qc = _qualify(cluster, sub_kb)
            if qc not in self.cluster_by_id:
                raise CuratorError(f"目标簇不存在:{qc}")
            if qc == qid:
                raise CuratorError("不能把节点归入自身")
            node["cluster"] = qc
        else:
            node.pop("cluster", None)
        return node.get("_partition")

    # —— 写回(按分区,各落各的文件) ——

    def save(self, partitions=None) -> None:
        """只重写 `partitions` 指定的分区(None = 全部子 KB)。

        只写实际变更的分区:不碰未涉及分区,免得格式化掉手写的注释/占位 entities.yaml。
        拒写 ro 挂载分区(写命令入口已 assert_writable,这里是兜底)。
        """
        all_parts = [sk for sk, _base in self.inst.graph_partitions]
        targets = set(all_parts) if partitions is None else set(partitions)
        base_of = {sk: base for sk, base in self.inst.graph_partitions}
        parts = {sk: {"entities": [], "clusters": [], "edges": []} for sk in targets}
        for e in self.entities:
            sk = e.get("_partition")
            if sk not in parts:
                continue
            d = {k: v for k, v in e.items() if k != "_partition"}
            d["id"] = _strip(d["id"], sk)
            if d.get("cluster"):
                d["cluster"] = _strip(d["cluster"], sk)
            parts[sk]["entities"].append(d)
        for cl in self.clusters:
            sk = cl.get("_partition")
            if sk not in parts:
                continue
            # 固定字段序 id, desc, cluster, contains,余下原样附后,稳定 diff。
            d = {"id": _strip(cl["id"], sk)}
            if cl.get("desc"):
                d["desc"] = cl["desc"]
            if cl.get("cluster"):
                d["cluster"] = _strip(cl["cluster"], sk)
            if cl.get("contains"):
                d["contains"] = [_strip(x, sk) for x in cl["contains"]]
            for k, v in cl.items():
                if k not in d and k != "_partition":
                    d[k] = v
            parts[sk]["clusters"].append(d)
        for ed in self.edges:
            sk = ed.get("_partition")
            if sk not in parts:
                continue
            d = {k: v for k, v in ed.items() if k != "_partition"}
            d["from"] = _strip(d["from"], sk)
            d["to"] = _strip(d["to"], sk)
            parts[sk]["edges"].append(d)
        for sk, part in parts.items():
            if sk is not None:
                self.inst.assert_writable(sk)  # ro 挂载不写(单分区实例无挂载概念)
            path = os.path.join(base_of.get(sk) or self.inst.abspath("."), self.inst.sub_kb_graph)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # 簇为空的分区不写 clusters 键,免得给不用簇的子 KB 平添空段。
            data = {"entities": part["entities"], "edges": part["edges"]}
            if part["clusters"]:
                data = {
                    "entities": part["entities"],
                    "clusters": part["clusters"],
                    "edges": part["edges"],
                }
            text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
            atomic_write(path, text)

    def entities_with_doc(self, rel: str) -> list:
        return [e.get("id") for e in self.entities if e.get("profile_doc") == rel]

    def retarget_doc(self, old_rel: str, new_rel: str) -> set:
        """把指向 old_rel 的 profile_doc 改到 new_rel,返回被改动的分区集合(供按区 save)。"""
        changed = set()
        for e in self.entities:
            if e.get("profile_doc") == old_rel:
                e["profile_doc"] = new_rel
                changed.add(e.get("_partition"))
        return changed

    # —— 写入(指定目标分区 sub_kb;端点可裸可命名空间) ——

    def add_entity(self, eid: str, etype: str, fields: dict, sub_kb: str) -> None:
        qid = _qualify(eid, sub_kb)
        if qid in self.by_id:
            raise CuratorError(f"实体已存在:{qid}")
        if etype not in self.inst.entity_types:
            raise CuratorError(f"type={etype} 未在 ontology.entities 声明")
        ent = {"id": qid, "type": etype}
        ent.update(fields)
        if ent.get("cluster"):
            qc = _qualify(ent["cluster"], sub_kb)
            if qc not in self.cluster_by_id:
                raise CuratorError(f"归属簇不存在:{qc}(先 graph add-cluster)")
            ent["cluster"] = qc
        ent["_partition"] = sub_kb
        self.entities.append(ent)
        self.by_id[qid] = ent

    def set_entity(self, eid: str, fields: dict, unset: list, sub_kb: str) -> list:
        """改已有实体的字段。返回实际变动的键。

        此前只能 add-entity(拒已存在)与 remove-entity(连边一起没),要补一个字段得先删再建,
        边会跟着丢。图是渐进填的,改字段应当是常规动作而不是重建(2026-08-21 补)。
        id 与 type 不给改:前者是主键,后者变了 requires 也跟着变,应当走删建。
        """
        qid = _qualify(eid, sub_kb)
        ent = self.by_id.get(qid)
        if ent is None:
            raise CuratorError(f"实体不存在:{qid}")
        changed = []
        for k in unset or []:
            if k in ("id", "type", "_partition"):
                raise CuratorError(f"{k} 不可改,要换请走 remove-entity 再 add-entity")
            if k in ent:
                del ent[k]
                changed.append(f"-{k}")
        for k, v in (fields or {}).items():
            if k in ("id", "type", "_partition"):
                raise CuratorError(f"{k} 不可改,要换请走 remove-entity 再 add-entity")
            if k == "cluster" and v:
                qc = _qualify(v, sub_kb)
                if qc not in self.cluster_by_id:
                    raise CuratorError(f"归属簇不存在:{qc}(先 graph add-cluster)")
                v = qc
            if ent.get(k) != v:
                ent[k] = v
                changed.append(k)
        return changed

    def remove_entity(self, eid: str, sub_kb: str) -> None:
        qid = _qualify(eid, sub_kb)
        if qid not in self.by_id:
            raise CuratorError(f"实体不存在:{qid}")
        refs = [e for e in self.edges if e.get("from") == qid or e.get("to") == qid]
        if refs:
            raise CuratorError(f"实体 {qid} 仍被 {len(refs)} 条边引用,先移除边")
        self.entities = [e for e in self.entities if e.get("id") != qid]
        del self.by_id[qid]

    def add_edge(self, frm: str, etype: str, to: str, sub_kb: str, source=None) -> None:
        if etype not in self.inst.edge_types:
            raise CuratorError(f"边 type={etype} 未在 ontology.edges 声明")
        qf, qt = _qualify(frm, sub_kb), _qualify(to, sub_kb)
        spec = self.inst.edge_types[etype]
        for end, ev in (("from", qf), ("to", qt)):
            if ev not in self.by_id:
                raise CuratorError(f"{end} 端点实体不存在:{ev}")
        if spec.get("from") and self.by_id[qf].get("type") != spec["from"]:
            raise CuratorError(f"from 类型 {self.by_id[qf].get('type')} != 声明 {spec['from']}")
        if spec.get("to") and self.by_id[qt].get("type") != spec["to"]:
            raise CuratorError(f"to 类型 {self.by_id[qt].get('type')} != 声明 {spec['to']}")
        for e in self.edges:
            if e.get("from") == qf and e.get("type") == etype and e.get("to") == qt:
                raise CuratorError(f"边已存在:{qf} -{etype}-> {qt}")
        # 边的置信度与笔记同一套判据(独立来源数 0/1/≥2 → reported/documented/corroborated),
        # 复用 confidence.derive,不另立标准。2026-08-21 补:此前边只有「有」和「没有」两种状态,
        # 而它的来源材料是 kind=judgment、confidence=reported 的画像笔记——从 reported 的材料
        # 生成没有置信度的边,等于在图谱这一层把不确定性洗掉了。而起手注入恰恰声明「关系以图谱
        # 为准,不从笔记正文推断」,即图谱被当作权威,洗白的代价由下游承担。
        from .confidence import derive
        edge = {"from": qf, "type": etype, "to": qt, "_partition": sub_kb}
        if source:
            edge["source"] = source
        edge["confidence"] = derive({"source": source}, self.inst.source_patterns)
        self.edges.append(edge)

    def remove_edge(self, frm: str, etype: str, to: str, sub_kb: str) -> int:
        qf, qt = _qualify(frm, sub_kb), _qualify(to, sub_kb)
        before = len(self.edges)
        self.edges = [
            e for e in self.edges
            if not (e.get("from") == qf and e.get("type") == etype and e.get("to") == qt)
        ]
        removed = before - len(self.edges)
        if not removed:
            raise CuratorError(f"无此边:{qf} -{etype}-> {qt}")
        return removed

    def path(self, src: str, dst: str):
        if src not in self.by_id or dst not in self.by_id:
            missing = [x for x in (src, dst) if x not in self.by_id]
            raise CuratorError(f"实体不存在:{', '.join(missing)}")
        if src == dst:
            return [src]
        prev = {src: None}
        q = deque([src])
        while q:
            cur = q.popleft()
            for e in self.out_edges(cur):
                nxt = e.get("to")
                if nxt is not None and nxt not in prev:
                    prev[nxt] = cur
                    if nxt == dst:
                        chain = [dst]
                        while prev[chain[-1]] is not None:
                            chain.append(prev[chain[-1]])
                        return list(reversed(chain))
                    q.append(nxt)
        return None
