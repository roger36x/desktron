"""curator temp —— 草稿暂存区的便利操作。这里只列/清,晋升一条到成品桶仍走 gated add。

作用范围是 inst.draft_bands,不是全部免闸 band(gc.py 与 config.py 的 draft_bands 有完整说明)。
两者曾经等价——内核模板只有一个免闸 band、就叫 temp——直到 v3 记忆布局引入第二个免闸 band
(notes,当时由后台自动写的永久判断档案；现在是唯一的项目模型)。当时这里仍按全部免闸 band 扫,`temp clean --yes` 因此会
不问青红皂白删掉全部 notes(2026-08-21 查明,属未执行的高危缺口,连同 gc 的同类问题一并修)。
"""

from __future__ import annotations

import glob
import os

from ..config import Instance


def _temp_files(inst: Instance):
    """全部草稿 band(可能多个/topical,如 {topic}/temp)的文件。返回 (展示用根, files)。"""
    bases = []
    for band in inst.draft_bands:
        bases += inst.work_scan_dirs(band)
    files = []
    for base in bases:
        for p in sorted(glob.glob(os.path.join(base, "**", "*"), recursive=True)):
            if os.path.isfile(p) and os.path.basename(p) != "README.md":
                files.append(p)
    return (bases[0] if bases else inst.root), sorted(files)


def cmd_list(inst: Instance, args) -> int:
    base, files = _temp_files(inst)
    if not files:
        print("(temp 空)")
        return 0
    for p in files:
        print(os.path.relpath(p, inst.root))
    return 0


def cmd_clean(inst: Instance, args) -> int:
    base, files = _temp_files(inst)
    if not files:
        print("(temp 空)")
        return 0
    if not args.yes:
        print(f"将删除 temp 下 {len(files)} 个文件(保留 README.md):")
        for p in files:
            print(f"  {os.path.relpath(p, inst.root)}")
        print("确认请加 --yes。")
        return 3
    for p in files:
        os.remove(p)
    print(f"已清 {len(files)} 个文件")
    return 0


def run(inst: Instance, args) -> int:
    return args.temp_func(inst, args)


def add_parser(sub):
    p = sub.add_parser("temp", help="免闸暂存区:列 / 清")
    csub = p.add_subparsers(dest="temp_cmd", required=True)

    lp = csub.add_parser("list", help="列 temp 下的文件")
    lp.set_defaults(temp_func=cmd_list)

    cp = csub.add_parser("clean", help="清 temp(保留 README,需 --yes)")
    cp.add_argument("--yes", action="store_true", help="确认删除")
    cp.set_defaults(temp_func=cmd_clean)

    p.set_defaults(func=run)
