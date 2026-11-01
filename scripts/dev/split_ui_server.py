#!/usr/bin/env python3
"""ui_server.py(1462 行)按职责拆五个 mixin 模块,入口文件保留(bin 直接 exec 它)。

纯搬移:成员段逐字节保留;胶水只有 mixin 类语句、按名复刻的 import、Handler 的
class 行改挂五个 mixin。_WB_API 在原类体里定义了两次(280 影子/482 生效),
两份同去 ui_boot 且保持相对顺序——跨 mixin 拆开会让 MRO 把影子那份翻出来。
"""
import ast
import io
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _splitkit import segments, class_members, top_names, undefined_names, import_stmt_of

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
SRC = io.open(os.path.join(LIB, "ui_server.py"), encoding="utf-8").read()
LINES = SRC.split("\n")


def text(a, b):
    return "\n".join(LINES[a - 1:b])


SEGS = segments(SRC)
HANDLER = next(n for _, _, n in SEGS if isinstance(n, ast.ClassDef) and n.name == "Handler")
MEMBERS = class_members(HANDLER)

MIX = {
    "ui_static.py": ("UiStaticMixin",
        ["_GZ_EXT", "_GZ_MAX", "_gz_cache", "send_head", "handle_one_request",
         "end_headers", "_sse_prime", "JSON_GZIP_MIN", "_send_json",
         "_send_json_bytes", "_send_text"]),
    "ui_boot.py": ("UiBootMixin",
        ["BOOT_PARTS", "_Capture", "_capture_get", "_boot", "_boot_data",
         "TASKS_BUNDLE_MAX", "_tasks_bundle", "_workbench", "_WB_API",
         "_is_workbench", "_serve_manifest", "_serve_index"]),
    "ui_contract.py": ("UiContractMixin",
        ["_is_contract", "_adapter_owning", "_session_belonging", "_sessions_listing",
         "_adapter_route", "_adapter_sse"]),
    "ui_remote_gate.py": ("UiRemoteGateMixin",
        ["_gate", "_probe", "_peer", "_FP_SAID", "_fp_drift"]),
    "ui_term_ws.py": ("UiTermWsMixin",
        ["_term_ws", "_ws_frame", "_ws_parse"]),
}
# 模块级搬移:静态令牌一族去 ui_static,_engine_update 去 ui_contract
TOP_MOVE = {
    "_frontend_token": "ui_static.py", "_ASSET_FP": "ui_static.py",
    "_asset_token": "ui_static.py", "BUILD_TOKEN": "ui_static.py",
    "_ASSET_CACHEABLE": "ui_static.py", "_engine_update": "ui_contract.py",
}

mname = {}                       # 成员名 → mixin 文件
for f, (_, names) in MIX.items():
    for n in names:
        assert n not in mname, n
        mname[n] = f


def member_name(m):
    node = m["node"]
    nm = getattr(node, "name", None)
    if nm:
        return nm
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                return t.id
    return None


moved, kept = {f: [] for f in MIX}, []
for m in MEMBERS:
    nm = member_name(m)
    f = mname.get(nm)
    (moved[f] if f else kept).append(m)
assigned = {member_name(m) for f in moved for m in moved[f]}
assert assigned == set(mname), f"缺员: {set(mname) - assigned}"

# ---- 生成 mixin 与搬走的模块级段 ----
IMPORTS = import_stmt_of(SRC)
symtab = {}                      # ui_server 顶层名 → 归宿文件("ui_server.py"=留守)
for a, b, n in SEGS:
    for nm in top_names(n):
        symtab[nm] = TOP_MOVE.get(nm, "ui_server.py")
for f, (_, names) in MIX.items():
    for n in names:
        symtab[n] = f            # 类成员按 self 访问,不该出现在全局名单;防御登记

OUT = {}
for f, (mixname, _) in MIX.items():
    chunks = []
    for nm, dest in TOP_MOVE.items():
        if dest == f:
            seg = next((a, b) for a, b, n in SEGS if nm in top_names(n))
            chunks.append(text(*seg))
    body = "\n".join(text(m["start"], m["end"]) for m in moved[f])
    chunks.append(f"class {mixname}:\n" + body)
    OUT[f] = chunks

BANNER = "# 由 ui_server.py 分拆(2026-08-27,纯搬移;见 specs/2026-08-27-repo-partition-2.md)\n"
for f in OUT:
    src_body = "\n\n\n".join(c.strip("\n") for c in OUT[f]) + "\n"
    need = undefined_names(src_body)
    imp, unresolved = [], []
    for n in sorted(need):
        if n in IMPORTS:
            stmt = IMPORTS[n][0]
            if stmt not in imp:
                imp.append(stmt)
        elif symtab.get(n) not in (None, f, "ui_server.py"):
            imp.append(f"from {symtab[n][:-3]} import {n}")
        elif symtab.get(n) == "ui_server.py":
            unresolved.append(n + "(留守 ui_server,成环)")
        else:
            unresolved.append(n)
    assert not unresolved, f"{f}: {unresolved}"
    io.open(os.path.join(LIB, f), "w", encoding="utf-8").write(
        BANNER + ("\n".join(sorted(set(imp))) + "\n\n\n" if imp else "") + src_body)

# ---- 重写 ui_server.py:留守段按原顺序,Handler 换 class 行,mixin import 插在类前 ----
moved_top_spans = set()
for nm, dest in TOP_MOVE.items():
    seg = next((a, b) for a, b, n in SEGS if nm in top_names(n))
    moved_top_spans.add(seg)

out = []
for a, b, n in SEGS:
    if (a, b) in moved_top_spans:
        continue
    if n is HANDLER:
        out.append("# 五个 mixin 是 ui_server 的职责分册,只被这里 import;"
                   "加载顺序契约:必须晚于上面的 sys.path 与 wiring。")
        for f, (mixname, _) in MIX.items():
            out.append(f"from {f[:-3]} import {mixname}")
        out.append("")
        head = text(a, n.lineno - 1) if n.lineno > a else ""
        mixes = ", ".join(mx for _, (mx, _) in MIX.items())
        body = "\n".join(text(m["start"], m["end"]) for m in kept)
        out.append((head + "\n" if head else "")
                   + f"class Handler({mixes}, http.server.SimpleHTTPRequestHandler):\n" + body)
        continue
    out.append(text(a, b))
io.open(os.path.join(LIB, "ui_server.py"), "w", encoding="utf-8").write("\n".join(out) + "\n")

for f in list(MIX) + ["ui_server.py"]:
    print(f"  {f:<20} {io.open(os.path.join(LIB, f)).read().count(chr(10))} 行")
