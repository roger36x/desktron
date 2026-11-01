#!/usr/bin/env python3
"""从 sealed / notes 正文里抽关系候选,产出候选清单供人审。不写 entities.yaml。

为什么是候选而不是直接建边:图谱在起手注入里被声明为「关系以图谱为准,不从笔记正文推断」,
即它是权威来源。而来源材料是 kind=judgment、confidence=reported 的画像笔记——agent 依据
对话观察写的判断,不是权威组织数据。从 reported 的材料自动生成"权威"关系,等于把置信度洗白。

抽取只做定位,不做判断:命中含关系词的句子就收,连同原文与出处一并列出,由人裁决。
误报由人否掉,成本是读一行;漏报靠扩关键词补。宁可多报。
"""
import os, re, sys, glob, json
import yaml

ROOT = sys.argv[1] if len(sys.argv) > 1 else ".memory"

# 关系词表。故意宽松:抽取阶段追求召回,精确率交给人审那一步。
PATTERNS = [
    ("reports_to", r"汇报给|向\s*([^\s,，。;;]{2,6})\s*汇报|直属|下属|\+1\b|\+2\b|老板"),
    ("reports_to", r"reports?\s+to"),
    ("member_of",  r"团队|组内|编制|归属.{0,4}(团队|组)"),
    ("owns",       r"\bown\b|owner|负责|主导|牵头"),
    ("leads",      r"带队|leader|负责人|指派|立项"),
]
# 明确的否定/未确证标记:命中则整句降级为「明确非关系」,单独列出提醒不要建边
NEGATIVE = r"未\s*verify|未确证|不确定|待确认|\?|？|存疑|未见|不清楚|非直接"


def load_entities(root):
    p = os.path.join(root, "graph", "entities.yaml")
    d = yaml.safe_load(open(p, encoding="utf-8")) or {}
    ents = d.get("entities") or []
    edges = d.get("edges") or []
    have = {(e["from"], e["type"], e["to"]) for e in edges}
    # 名字 → id 的查找表:中文名与 id 本身都算
    name2id = {}
    for e in ents:
        name2id[e["id"]] = e["id"]
        cn = e.get("chinese_name")
        if cn:
            name2id[cn] = e["id"]
    return ents, have, name2id


def iter_source_notes(root):
    for band in ("_sealed", "*/notes"):
        for p in glob.glob(os.path.join(root, band, "**", "*.md"), recursive=True):
            yield p


def main():
    ents, have, name2id = load_entities(ROOT)
    linked = {x for e in have for x in (e[0], e[2])}
    cands, negatives = [], []

    for path in iter_source_notes(ROOT):
        rel = os.path.relpath(path, ROOT)
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            continue
        nid = None
        m = re.search(r"^id:\s*(\S+)", text, re.M)
        if m:
            nid = m.group(1)
        # 逐句扫,句子里同时出现两个已知实体名 + 一个关系词才算候选
        for line in text.splitlines():
            s = line.strip()
            if len(s) < 6 or s.startswith(("#", "---", "source:", "id:")):
                continue
            hits = sorted({name2id[n] for n in name2id if n in s}, key=len)
            if len(hits) < 2:
                continue
            for etype, pat in PATTERNS:
                if not re.search(pat, s, re.I):
                    continue
                neg = bool(re.search(NEGATIVE, s))
                rec = {"type": etype, "entities": hits, "note": nid, "path": rel,
                       "text": s[:200]}
                (negatives if neg else cands).append(rec)
                break

    # 去重:同一组实体 + 同一关系类型只留一条(取最长原文,信息最多)
    def dedup(rows):
        best = {}
        for r in rows:
            k = (r["type"], tuple(sorted(r["entities"])))
            if k not in best or len(r["text"]) > len(best[k]["text"]):
                best[k] = r
        return sorted(best.values(), key=lambda r: (r["type"], r["entities"]))

    cands, negatives = dedup(cands), dedup(negatives)
    # 已经有边的不再报
    def already(r):
        es = r["entities"]
        return any((a, r["type"], b) in have or (b, r["type"], a) in have
                   for a in es for b in es if a != b)
    fresh = [r for r in cands if not already(r)]

    print(f"# 关系候选(共 {len(fresh)} 条待审;另有 {len(negatives)} 条命中未确证标记,单列在后)")
    print(f"# 图谱现有 {len(have)} 条边,{len(ents)} 个实体,其中 {len(ents)-len(linked)} 个无边")
    print("# 用法:逐条判断。确认的执行 curator graph add-edge --from A --type T --to B")
    print()
    for r in fresh:
        print(f"[{r['type']}] {' + '.join(r['entities'])}")
        print(f"  出处: {r['note'] or r['path']}")
        print(f"  原文: {r['text']}")
        print()
    if negatives:
        print("=" * 60)
        print("# 以下命中了未确证/否定标记,原文自己就说关系没定,不要据此建边:")
        print()
        for r in negatives:
            print(f"[{r['type']}] {' + '.join(r['entities'])}")
            print(f"  出处: {r['note'] or r['path']}")
            print(f"  原文: {r['text']}")
            print()


if __name__ == "__main__":
    main()
