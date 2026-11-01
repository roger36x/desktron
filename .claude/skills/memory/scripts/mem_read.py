#!/usr/bin/env python3
"""记忆读入口:把读子命令转给 curator,替调用方解析工作层实例根。

写侧已有 mem_add / mem_update 替调用方定位实例根,读侧一直没有,agent 只能自己拼
`curator --root <相对路径>`——脚手架工作流里是 `../../.memory`,仓内工作流里是 `.memory`,
两处不同且没人记得住。裸跑 `curator show` 会按 cwd 找实例并报「不像 curator 实例」,
失败一次之后 agent 就绕过读闸直接开文件,访问计数与冲突提示随之失效。

用法与 curator 完全一致,只是省掉 --root:
    mem_read.py show --id <id>
    mem_read.py search <词> [--tier notes]
    mem_read.py list [--confidence documented]
    mem_read.py graph neighbors <实体>
只放行只读子命令;写走 mem_add / mem_update / mem_verify。
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mem_common import work_root  # noqa: E402

READ_ONLY = {"show", "search", "list", "graph", "conflicts", "trace", "authority", "review-due"}
# graph 自己还有写子命令(add-entity/add-edge/...),这里只放行查询那几个。
GRAPH_READ = {"check", "neighbors", "path", "backref", "edges", "map", "expand", "centrality"}


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    sub = argv[0]
    if sub not in READ_ONLY:
        print(f"✗ {sub} 不是只读子命令;写记忆走 mem_add / mem_update / mem_verify。"
              f"\n  可用:{' '.join(sorted(READ_ONLY))}", file=sys.stderr)
        return 2
    if sub == "graph" and (len(argv) < 2 or argv[1] not in GRAPH_READ):
        got = argv[1] if len(argv) > 1 else "(空)"
        print(f"✗ graph {got} 会改图;本入口只放行查询:{' '.join(sorted(GRAPH_READ))}", file=sys.stderr)
        return 2
    return subprocess.call(["curator", "--root", str(work_root())] + argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
