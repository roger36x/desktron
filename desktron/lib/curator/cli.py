"""curator 命令行入口。子命令分发,行为读实例 ontology.yaml + rules.yaml。"""

from __future__ import annotations

import argparse
import sys

from .config import Instance
from .commands import add as add_cmd
from .commands import authority as authority_cmd
from .commands import commitgate as commitgate_cmd
from .commands import compact as compact_cmd
from .commands import conflicts as conflicts_cmd
from .commands import temp as temp_cmd
from .commands import doctor as doctor_cmd
from .commands import gc as gc_cmd
from .commands import graph_cmd
from .commands import init_cmd
from .commands import journal as journal_cmd
from .commands import lint as lint_cmd
from .commands import promote as promote_cmd
from .commands import reconcile as reconcile_cmd
from .commands import retrieval as retrieval_cmd
from .commands import rm as rm_cmd
from .commands import subkb as subkb_cmd
from .commands import suggest as suggest_cmd
from .commands import trace as trace_cmd
from .commands import update as update_cmd
from .commands import verify as verify_cmd
from .util import CuratorError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="curator", description="文件式、ontology 驱动的知识库引擎")
    p.add_argument("--root", default=".", help="单仓实例根(默认 cwd)")
    p.add_argument("--view", help="视图 manifest:跨实例挂载多子 KB + 消费方工作层")
    sub = p.add_subparsers(dest="cmd", required=True)

    init_cmd.add_parser(sub)
    add_cmd.add_parser(sub)
    update_cmd.add_parser(sub)
    verify_cmd.add_parser(sub)
    promote_cmd.add_parser(sub)
    rm_cmd.add_parser(sub)
    lint_cmd.add_parser(sub)
    doctor_cmd.add_parser(sub)
    commitgate_cmd.add_parser(sub)
    gc_cmd.add_parser(sub)
    temp_cmd.add_parser(sub)
    journal_cmd.add_parser(sub)
    retrieval_cmd.add_parser(sub)
    authority_cmd.add_parser(sub)
    compact_cmd.add_parser(sub)
    conflicts_cmd.add_parser(sub)
    suggest_cmd.add_parser(sub)
    subkb_cmd.add_parser(sub)
    reconcile_cmd.add_parser(sub)
    trace_cmd.add_parser(sub)
    graph_cmd.add_parser(sub)
    return p


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        # init 不绑已存在实例,单独走。
        if getattr(args, "func_init", None):
            return args.func_init(args)
        inst = Instance(view=args.view) if getattr(args, "view", None) else Instance(args.root)
        return args.func(inst, args)
    except CuratorError as e:
        print(f"错误:{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
