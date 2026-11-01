#!/usr/bin/env python3
"""前端大文件切片器:把一个经典脚本切成若干连续切片,按加载顺序替换 script 标签。

经典脚本共享全局作用域,加载顺序不变 ⇒ 程序不变。所以这里用最强的不变量:
每一份是原文件的连续切片,拼接(去掉每份首行的来源注释)必须逐字节等于原文件。
配套三道检查:切点必须落在顶层语句边界(逐片 node --check 兜底)、拼接自证、
index.html 标签替换后引用存在性由 check_frontend 把关。

用法:split_frontend.py <原文件名> <切点表:名字=起始行,…>(起始行 1 起算,首片省略)
"""
import io
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FRONTEND = os.path.join(ROOT, "frontend")


def split(orig_name, plan):
    """plan: [(新文件名, 起始行)],第一项起始行必须是 1;终点=下一片起始-1。"""
    src_path = os.path.join(FRONTEND, orig_name)
    src = io.open(src_path, encoding="utf-8").read()
    # 每行都带自己的终止符表示,避免 join 的尾双关(空行的换行会被吃掉)
    lines = src.split("\n")
    if lines and lines[-1] == "":
        lines.pop()                      # 尾部换行的幻影行
    assert plan[0][1] == 1
    bounds = [ln for _, ln in plan] + [len(lines) + 1]
    made = []
    for i, (fname, a) in enumerate(plan):
        b = bounds[i + 1] - 1
        body = "".join(l + "\n" for l in lines[a - 1:b])
        banner = f"// 由 {orig_name} 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 {i+1}/{len(plan)} 片\n"
        io.open(os.path.join(FRONTEND, fname), "w", encoding="utf-8").write(banner + body)
        made.append(fname)
    # 拼接自证:去掉每份首行 banner 后必须逐字节还原原文件
    rebuilt = "".join(io.open(os.path.join(FRONTEND, f), encoding="utf-8").read().split("\n", 1)[1]
                      for f in made)
    orig_norm = src if src.endswith("\n") else src + "\n"
    assert rebuilt == orig_norm, "拼接自证失败:切片并未逐字节还原原文件"
    # 逐片语法
    for fname in made:
        r = subprocess.run(["node", "--check", os.path.join(FRONTEND, fname)],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"{fname} 语法不完整(切点落在语句中间?)\n{r.stderr[:400]}"
    # index.html 标签替换(保持原缩进与位置)
    idx_path = os.path.join(FRONTEND, "index.html")
    idx = io.open(idx_path, encoding="utf-8").read()
    tag = f'<script src="/{orig_name}"></script>'
    assert idx.count(tag) == 1, f"index.html 里找不到唯一的 {tag}"
    at = idx.index(tag)
    indent = idx[:at].rsplit("\n", 1)[-1]
    repl = ("\n" + indent).join(f'<script src="/{f}"></script>' for f in made)
    io.open(idx_path, "w", encoding="utf-8").write(idx.replace(tag, repl))
    os.remove(src_path)
    for i, (fname, a) in enumerate(plan):
        b = bounds[i + 1] - 1
        print(f"  {fname:<28} {a:>5}-{b:<5} ({b - a + 1} 行)")
    print(f"{orig_name} 已移除,index.html 标签已替换。")


if __name__ == "__main__":
    name = sys.argv[1]
    plan = []
    for kv in sys.argv[2].split(","):
        f, ln = kv.split("=")
        plan.append((f, int(ln)))
    split(name, plan)
