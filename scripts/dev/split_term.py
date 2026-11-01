#!/usr/bin/env python3
"""term.py(1745 行)切三个兄弟模块 + 门面尾段。纯搬移:连续切片,段序不变,
import 时执行序不变(_tcache_load() 那一句仍在 titles 模块尾部原位执行)。
门面自动 re-export 全部顶层名:ui_server 的 from term import (…)、engine_adapters
的函数级 lazy import、12 个测试的 import term 全都不用动。"""
import ast
import io
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _splitkit import segments, top_names, undefined_names, import_stmt_of

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
SRC = io.open(os.path.join(LIB, "term.py"), encoding="utf-8").read()
LINES = SRC.split("\n")

SLICES = [  # (模块文件, 起, 止)
    ("term_titles.py", 15, 655),
    ("term_records.py", 656, 1080),
    ("term_sessions.py", 1081, 1458),
]
TAIL = (1459, 1745)
HEADER = "\n".join(LINES[0:14])

SEGS = segments(SRC)
symtab = {}
for a, b, n in SEGS:
    # 归属按定义行判,不按段起点:段起点含前导注释,会在切片界附近错位
    # (_tcache_load() 调用在 653,下一段的注释从 654 起、def 却在 656)。
    ln = n.lineno
    dest = "term.py"
    for f, s0, s1 in SLICES:
        if s0 <= ln <= s1:
            dest = f
    for nm in top_names(n):
        symtab[nm] = dest
IMPORTS = import_stmt_of(SRC)

BACKINJECT = []   # (要名的模块, 名, 定义它的模块)
BANNER = "# 由 term.py 分拆(2026-08-27,纯搬移;见 specs/2026-08-27-repo-partition-2.md)\n"
order = [f for f, _, _ in SLICES]
for f, s0, s1 in SLICES:
    body = "".join(l + "\n" for l in LINES[s0 - 1:s1])
    need = undefined_names(body)
    imp, unresolved = [], []
    for n in sorted(need):
        if n in IMPORTS:
            if IMPORTS[n][0] not in imp:
                imp.append(IMPORTS[n][0])
        elif symtab.get(n) not in (None, f) and symtab[n] != "term.py" \
                and order.index(symtab[n]) < order.index(f):
            imp.append(f"from {symtab[n][:-3]} import {n}")
        elif symtab.get(n) not in (None, "term.py", f):
            # 反向依赖(定义在后面的分册):原单文件靠调用时全局查找。这些名经核对
            # 从不重绑(无 global 重赋值),由门面加载完回注绑定,共享同一对象。
            BACKINJECT.append((f, n, symtab[n]))
        else:
            unresolved.append(f"{n}→{symtab.get(n)}")
    assert not unresolved, f"{f}: {unresolved}"
    io.open(os.path.join(LIB, f), "w", encoding="utf-8").write(
        BANNER + ("\n".join(sorted(set(imp))) + "\n\n\n" if imp else "") + body)

tail_body = "".join(l + "\n" for l in LINES[TAIL[0] - 1:TAIL[1]])
re_exports = {}
for nm, dest in symtab.items():
    if dest != "term.py":
        re_exports.setdefault(dest, []).append(nm)
glue = ["", "# 三个分册按原段序 import(执行序与单文件一致);顶层名全量 re-export,",
        "# 外部的 from term import … 与函数级 lazy import 不用动。"]
for f, _, _ in SLICES:
    glue.append(f"from {f[:-3]} import " + ", ".join(sorted(set(re_exports.get(f, [])))))
if BACKINJECT:
    glue.append("# 分拆胶水:前册函数按全局名引用后册的态(调用时才查),回注绑定;这些名从不重绑。")
    mods = sorted({m for m, _, _ in BACKINJECT} | {d for _, _, d in BACKINJECT})
    glue.append("import " + ", ".join(x[:-3] for x in mods))
    for m, n, d in sorted(set(BACKINJECT)):
        glue.append(f"{m[:-3]}.{n} = {d[:-3]}.{n}")
glue.append("")
io.open(os.path.join(LIB, "term.py"), "w", encoding="utf-8").write(
    HEADER + "\n" + "\n".join(glue) + "\n" + tail_body)

for f in order + ["term.py"]:
    print(f"  {f:<18} {io.open(os.path.join(LIB, f)).read().count(chr(10))} 行")
