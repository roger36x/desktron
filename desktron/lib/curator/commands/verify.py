"""curator verify —— 已退役(2026-08-21)。

原来它是升 verified 的唯一入口:用户逐条核实、签名、绑正文 hash。线上跑到 405 条记忆时
签名数为 0,这道工序没人做。顶档改为 corroborated,判据是 source 里引用了几个独立来源,
随 add / update 自动推导(见 confidence.py)。要升顶档就补第二个来源:

    curator update --id <id> --source "<原来源>;<另一个独立来源>"

入口留着只为报清楚这件事——旧 skill、hook 与历史会话里还写着 verify,直接删掉会得到一句
「invalid choice」,读不出该改成什么。
"""

from __future__ import annotations

from ..config import Instance
from ..util import CuratorError


def run(inst: Instance, args) -> int:
    raise CuratorError(
        "verify 已退役:顶档由 verified 改为 corroborated,判据是独立来源数,不再由人签名。\n"
        f"  要升顶档,给 {args.id} 补第二个独立来源:\n"
        f"  curator update --id {args.id} --source \"<原来源>;<另一个独立来源>\""
    )


def add_parser(sub):
    p = sub.add_parser("verify", help="已退役:顶档改为 corroborated,按独立来源数自动推导")
    p.add_argument("--id", required=True)
    p.add_argument("--by", required=False, help="(已忽略)")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="(已忽略)")
    p.set_defaults(func=run)
