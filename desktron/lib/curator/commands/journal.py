"""curator journal —— 免闸 band 的 append-only 流水入口。

单入口模型下,消费方(agent)不直接持有工作层文件路径;往免闸 band(如 s0)追加流水
走本命令。只追加、不过 add 闸、不进索引;整理(蒸馏/晋升/GC)由引擎的其它动词回头处理。

- append:往 <免闸 band>[/topic]/journal.md 追加一段,带时间戳小节头。
- show:读回该 journal(供接力/自检;算 engine 侧读取,不计使用热度)。
"""

from __future__ import annotations

import datetime
import os

from ..config import Instance
from ..util import CuratorError

JOURNAL_FILE = "journal.md"


def _journal_path(inst: Instance, band: str, topic: str) -> str:
    band = band or inst.temp_band
    if band not in inst.ungated_bands:
        raise CuratorError(f"journal 只写免闸 band,{band} 不是(免闸:{inst.ungated_bands})")
    d = inst.work_dir(band, topic) if inst.band_is_topical(band) else inst.work_dir(band)
    return os.path.join(d, JOURNAL_FILE)


def cmd_append(inst: Instance, args) -> int:
    text = args.text
    if text is None:
        import sys
        text = sys.stdin.read()
    if not (text or "").strip():
        raise CuratorError("内容为空(--text 或 stdin)")
    path = _journal_path(inst, args.band, args.topic)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    head = f"## {stamp}" + (f" {args.title}" if args.title else "")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{head}\n\n{text.rstrip()}\n\n")
    print(f"已追加 → {os.path.relpath(path, inst.root)}")
    return 0


def cmd_show(inst: Instance, args) -> int:
    path = _journal_path(inst, args.band, args.topic)
    if not os.path.exists(path):
        print("(journal 空)")
        return 0
    with open(path, encoding="utf-8") as f:
        txt = f.read()
    if args.tail and args.tail > 0:
        lines = txt.splitlines()
        txt = "\n".join(lines[-args.tail:])
    print(txt)
    return 0


def run(inst: Instance, args) -> int:
    return args.journal_func(inst, args)


def add_parser(sub):
    p = sub.add_parser("journal", help="免闸流水:append-only 追加 / 读回(单入口,不过闸不进索引)")
    csub = p.add_subparsers(dest="journal_cmd", required=True)

    ap = csub.add_parser("append", help="追加一段(带时间戳小节头)")
    ap.add_argument("--topic", help="topic(topical 免闸 band 必填,如工作区 id)")
    ap.add_argument("--band", help="目标免闸 band(默认第一个免闸 band)")
    ap.add_argument("--title", help="小节标题(时间戳后缀)")
    ap.add_argument("--text", help="内容;省略则读 stdin")
    ap.set_defaults(journal_func=cmd_append)

    sp = csub.add_parser("show", help="读回 journal")
    sp.add_argument("--topic", help="topic(topical 免闸 band 必填)")
    sp.add_argument("--band", help="免闸 band(默认第一个)")
    sp.add_argument("--tail", type=int, help="只看末尾 N 行")
    sp.set_defaults(journal_func=cmd_show)

    p.set_defaults(func=run)
