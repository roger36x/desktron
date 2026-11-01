#!/usr/bin/env python3
"""更新一条记忆的字段或正文。薄封装 curator update:非 verified 按 source 重算三档;verified 在正文
实质改(hash 变)时失效落回中段——这套纪律全在 curator,本命令不重写。confidence 不手工设,无 --confidence。
curator 按 id 全实例定位,不需指定桶。"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mem_common import die, work_root, ensure_instance, run_curator, check_emoji


def main():
    ap = argparse.ArgumentParser(description="更新一条记忆")
    ap.add_argument("--id", required=True)
    ap.add_argument("--summary")
    ap.add_argument("--source")
    ap.add_argument("--gaps")
    ap.add_argument("--links", help="整量覆盖 links,逗号分隔")
    ap.add_argument("--body", help="替换正文;省略则不动正文")
    ap.add_argument("--body-stdin", dest="body_stdin", action="store_true", help="正文从 stdin 读")
    ap.add_argument("--user-confirmed", action="store_true", help="声明用户已明确确认；更新 pinned judgment 必填")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true")
    a = ap.parse_args()

    body = None
    if a.body is not None:
        body = a.body
    elif a.body_stdin:
        body = sys.stdin.read()
    if body is not None:
        hits = check_emoji(body)
        if hits:
            die("正文不合规:\n  - " + "\n  - ".join(hits))

    root = work_root()
    ensure_instance(root)
    from memory_engine.commands import _update_is_pinned_judgment
    if _update_is_pinned_judgment(root, a.id) and not a.user_confirmed:
        die("pinned judgment 必须由用户明确确认；确认后加 --user-confirmed")
    args = ["--id", a.id]
    if a.summary:
        args += ["--summary", a.summary]
    if a.source:
        args += ["--source", a.source]
    if a.gaps:
        args += ["--gaps", a.gaps]
    if a.links is not None:
        args += ["--links", a.links]
    if body is not None:
        args += ["--body-stdin"]
    if a.user_confirmed:
        args += ["--user-confirmed"]
    if a.dry_run:
        args += ["--dry-run"]
    run_curator(root, "update", args, body=body)


if __name__ == "__main__":
    main()
