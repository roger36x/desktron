"""图中心性:纯标准库,无外部依赖。在 (nodes, edges) 上算,不认领域名词。

- PageRank:幂迭代,质量沿有向边汇聚,被指向多者得分高。边语义(depends_on / owns …)
  由实例定,本模块不解释方向;调用方需要时自行反向。
- betweenness:Brandes,有向无权,按 (n-1)(n-2) 归一。
- 弱连通分量:忽略方向的连通块。

数百实体规模下足够快,与 curator「files + stdlib,不上图数据库」一致。
"""

from __future__ import annotations

from collections import defaultdict, deque


def _succ(nodes, edges):
    s = {v: [] for v in nodes}
    indeg = {v: 0 for v in nodes}
    outdeg = {v: 0 for v in nodes}
    for a, b in edges:
        if a in s and b in s:
            s[a].append(b)
            outdeg[a] += 1
            indeg[b] += 1
    return s, indeg, outdeg


def pagerank(nodes, succ, damping=0.85, iters=100, tol=1e-9):
    n = len(nodes)
    if n == 0:
        return {}
    pr = {v: 1.0 / n for v in nodes}
    for _ in range(iters):
        new = {v: (1 - damping) / n for v in nodes}
        dangling = sum(pr[v] for v in nodes if not succ[v])
        for v in nodes:
            new[v] += damping * dangling / n
        for v in nodes:
            outs = succ[v]
            if outs:
                share = damping * pr[v] / len(outs)
                for w in outs:
                    new[w] += share
        if max(abs(new[v] - pr[v]) for v in nodes) < tol:
            return new
        pr = new
    return pr


def betweenness(nodes, succ):
    """Brandes,有向无权。归一化除以 (n-1)(n-2);n<=2 不归一(全 0)。"""
    bc = {v: 0.0 for v in nodes}
    for s in nodes:
        stack = []
        pred = {w: [] for w in nodes}
        sigma = {w: 0.0 for w in nodes}
        sigma[s] = 1.0
        dist = {w: -1 for w in nodes}
        dist[s] = 0
        q = deque([s])
        while q:
            v = q.popleft()
            stack.append(v)
            for w in succ[v]:
                if dist[w] < 0:
                    dist[w] = dist[v] + 1
                    q.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    pred[w].append(v)
        delta = {w: 0.0 for w in nodes}
        while stack:
            w = stack.pop()
            for v in pred[w]:
                delta[v] += (sigma[v] / sigma[w]) * (1 + delta[w])
            if w != s:
                bc[w] += delta[w]
    n = len(nodes)
    if n > 2:
        scale = 1.0 / ((n - 1) * (n - 2))
        for v in nodes:
            bc[v] *= scale
    return bc


def weak_components(nodes, edges):
    adj = defaultdict(set)
    nodeset = set(nodes)
    for a, b in edges:
        if a in nodeset and b in nodeset:
            adj[a].add(b)
            adj[b].add(a)
    seen, comps = set(), []
    for v in nodes:
        if v in seen:
            continue
        comp, q = [], deque([v])
        seen.add(v)
        while q:
            x = q.popleft()
            comp.append(x)
            for y in adj[x]:
                if y not in seen:
                    seen.add(y)
                    q.append(y)
        comps.append(comp)
    return comps


def centrality(nodes, edges):
    """返回 {pagerank, betweenness, indeg, outdeg, components}。"""
    succ, indeg, outdeg = _succ(nodes, edges)
    return {
        "pagerank": pagerank(nodes, succ),
        "betweenness": betweenness(nodes, succ),
        "indeg": indeg,
        "outdeg": outdeg,
        "components": weak_components(nodes, edges),
    }
