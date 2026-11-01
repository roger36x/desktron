#!/usr/bin/env python3
"""分拆工具箱:把大 python 文件按段纯搬移到多个模块,机械保证行为不变。

与 split_engine_adapters 的第一版相比,名字解析升级为作用域精确:那一版把「全文件
任何位置的绑定」都算数,函数 A 里的局部 import 会掩掉函数 B 里同名全局读——
engine_adapters 的 deskcfg 潜伏 NameError 就是这么漏进原文件的。ui_server 的测试
大多只锚源码、很少真执行,解析漏一个名,门禁全绿也能带病上线,所以这里必须精确。

三件套:
  segments(src)        —— 顶层段表(语句+前导注释),自带拼回自证
  class_members(cls)   —— 类体成员段,同样前导归属
  undefined_names(src) —— 作用域精确的未定义全局名(函数各自的局部绑定不外溢)
  import_stmt_of(src)  —— 名 → 原文件里绑定它的那条 import 语句原文(逐条复刻用)
"""
import ast
import builtins

BUILTIN = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__"}


def segments(src):
    """[(start, end, node)],1 起算闭区间;并集=全文件(除文件尾空行),调用方可自证。"""
    lines = src.split("\n")
    tree = ast.parse(src)
    out, prev = [], 0
    for n in tree.body:
        out.append((prev + 1, n.end_lineno, n))
        prev = n.end_lineno
    rebuilt = "\n".join("\n".join(lines[a - 1:b]) for a, b, _ in out) \
        + "\n".join([""] + lines[prev:])
    assert rebuilt == src, "段覆盖自证失败"
    return out


def class_members(cls):
    """类体成员段(成员+它前面的注释)。首成员的段从类语句次行起。"""
    out, prev_end = [], cls.lineno
    for m in cls.body:
        ln0 = min([m.lineno] + [d.lineno for d in getattr(m, "decorator_list", [])])
        out.append({"start": prev_end + 1, "def_line": ln0, "end": m.end_lineno, "node": m})
        prev_end = m.end_lineno
    return out


def top_names(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Assign):
        out = []
        for t in node.targets:
            if isinstance(t, ast.Name):
                out.append(t.id)
            elif isinstance(t, ast.Tuple):
                out += [e.id for e in t.elts if isinstance(e, ast.Name)]
        return out
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [(a.asname or a.name.split(".")[0]) for a in node.names]
    if isinstance(node, (ast.Try, ast.If, ast.With)):
        out = []
        for b in (getattr(node, "body", []) + getattr(node, "orelse", [])
                  + getattr(node, "finalbody", [])
                  + [s for h in getattr(node, "handlers", []) for s in h.body]):
            out += top_names(b)
        return out
    return []


def _scope_bound(node):
    """一个函数/推导式作用域自己的绑定:参数、局部赋值、局部 import、嵌套 def/class。"""
    bound = set()
    args = node.args
    for a in args.args + args.kwonlyargs + args.posonlyargs:
        bound.add(a.arg)
    if args.vararg: bound.add(args.vararg.arg)
    if args.kwarg: bound.add(args.kwarg.arg)

    def walk(n):
        for c in ast.iter_child_nodes(n):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(c.name)          # 名字绑定在本作用域;其内部不进来
                continue
            if isinstance(c, ast.Lambda):
                continue
            if isinstance(c, ast.Name) and isinstance(c.ctx, (ast.Store, ast.Del)):
                bound.add(c.id)
            elif isinstance(c, (ast.Import, ast.ImportFrom)):
                for a in c.names:
                    bound.add(a.asname or (a.name.split(".")[0] if isinstance(c, ast.Import) else a.name))
            elif isinstance(c, ast.ExceptHandler) and c.name:
                bound.add(c.name)
            elif isinstance(c, ast.Global):
                for g in c.names:
                    bound.discard(g)
            walk(c)
    walk(node)
    return bound


def undefined_names(src):
    """被读取、但按作用域规则解析不到的全局名。类体与模块层按顺序性从宽处理:
    只要模块里任何顶层绑定过就算有(顶层顺序错误由 import 冒烟兜)。"""
    tree = ast.parse(src)
    module_bound = set()
    for n in tree.body:
        module_bound.update(top_names(n))
    missing = set()

    def check_scope(node, enclosing):
        bound = _scope_bound(node) | enclosing
        def walk(n):
            for c in ast.iter_child_nodes(n):
                if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    check_scope(c, bound)
                    continue
                if isinstance(c, ast.Lambda):
                    lb = set(a.arg for a in c.args.args) | bound
                    for cc in ast.walk(c.body):
                        if isinstance(cc, ast.Name) and isinstance(cc.ctx, ast.Load) \
                                and cc.id not in lb and cc.id not in module_bound \
                                and cc.id not in BUILTIN:
                            missing.add(cc.id)
                    continue
                if isinstance(c, ast.Name) and isinstance(c.ctx, ast.Load):
                    if c.id not in bound and c.id not in module_bound and c.id not in BUILTIN:
                        missing.add(c.id)
                walk(c)
        walk(node)

    # 模块层(含类体):读到的名只要模块层绑定过就算有
    def module_walk(n, class_depth=0):
        for c in ast.iter_child_nodes(n):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)):
                check_scope(c, set())
                continue
            if isinstance(c, ast.Name) and isinstance(c.ctx, ast.Load):
                if c.id not in module_bound and c.id not in BUILTIN:
                    missing.add(c.id)
            module_walk(c, class_depth + (1 if isinstance(c, ast.ClassDef) else 0))
    module_walk(tree)
    return missing


def import_stmt_of(src):
    """名 → 绑定它的那条顶层 import 语句的原文。分拆时按名逐条复刻,不整段搬。"""
    lines = src.split("\n")
    out = {}
    for n in ast.parse(src).body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            text = "\n".join(lines[n.lineno - 1:n.end_lineno])
            for a in n.names:
                nm = a.asname or (a.name.split(".")[0] if isinstance(n, ast.Import) else a.name)
                out[nm] = (text, n.lineno)
    return out
