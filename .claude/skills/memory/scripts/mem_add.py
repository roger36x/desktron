#!/usr/bin/env python3
"""新增一条记忆。薄封装:emoji 预检 → 调 curator add 过闸(三档由 source 自动派生,
verified 不在此设)。桶 sealed(红线封存,用户全局;旧名 private 兼容)/ shared(本工作区,topic=ws);
journal 层免闸流水不经本命令,走 curator journal。工作层路径取自会话 1 契约,不自拼。
2026-08-03 知识库并入 SOT:领域事实用 --kind fact 写进同一层,带 doc-type / keywords / status。"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mem_common import (TYPES, TIERS, TIER_ALIAS, ID_RE, SCOPE_ALIAS, die,
                         domain_for_cwd, curator_target, ensure_instance, run_curator, check_emoji, dup_check)


def main():
    ap = argparse.ArgumentParser(description="新增一条记忆(judgment)")
    ap.add_argument("--tier", choices=TIERS, help="sealed=红线封存 / pinned=用户确认约束 / fact=领域事实(sot 为物理兼容名)")
    ap.add_argument("--scope", help="兼容旧名:area/local→shared、shared/journal→private(等价 --tier)")
    ap.add_argument("--ws", help="工作区 id(项目容器也是一个工作区;temp/shared 用它定 topic)。默认按 cwd 反查;不在项目 cwd 里跑的外部 / 跨区同步务必显式传(如 --ws c-my-project),否则落全局 _desktop")
    ap.add_argument("--id", required=True, help="kebab-slug,实例内唯一")
    ap.add_argument("--type", choices=TYPES, help="user/feedback/project/reference")
    ap.add_argument("--source", help="出处(file:line/url/commit/时间戳);给了即自动 documented")
    ap.add_argument("--user-confirmed", action="store_true", help="声明用户已明确确认；--tier pinned 必填")
    ap.add_argument("--gaps", help="中段的不全标注:缺什么")
    ap.add_argument("--links", help="关联 id,逗号分隔")
    ap.add_argument("--summary", required=True, help="一句话,进索引")
    ap.add_argument("--body", help="正文;省略则读 stdin")
    ap.add_argument("--force", action="store_true", help="跳过写前查重(确属新条目)")
    ap.add_argument("--kind", choices=("judgment", "fact"), default="judgment",
                    help="judgment=判断(默认) / fact=领域事实(知识库并入后走这个)")
    ap.add_argument("--doc-type", dest="doc_type", help="仅 fact:glossary/aliases/landscape/reference/runbook")
    ap.add_argument("--keywords", help="仅 fact:检索标签,逗号分隔")
    ap.add_argument("--status", choices=("current", "deprecated"), help="仅 fact:事实状态")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true", help="只预演,不写盘")
    a = ap.parse_args()

    tier = a.tier or (SCOPE_ALIAS.get(a.scope) if a.scope else None)
    if not tier:
        die("缺桶:--tier sealed|pinned|fact(或兼容旧名 sot/private/shared)")
    if tier == "fact" and a.kind != "fact":
        die("--tier fact 必须配 --kind fact")
    if TIER_ALIAS.get(tier, tier) == "sot" and a.kind != "fact" and not a.user_confirmed:
        die("pinned judgment 必须由用户明确确认；确认后加 --user-confirmed")
    if not ID_RE.match(a.id):
        die("id 非法(仅小写字母数字与连字符,字母 / 数字开头)")
    body = a.body if a.body is not None else sys.stdin.read()
    if not body.strip():
        die("正文为空")
    hits = check_emoji(body)
    if hits:
        die("正文不合规:\n  - " + "\n  - ".join(hits))

    ws = a.ws or domain_for_cwd(os.getcwd()) or "_desktop"
    root, ktier, topic = curator_target(ws, tier)
    ensure_instance(root)
    # 写前查重(记忆无 keywords,按 summary 查)。命中要 --force。
    dup_check(root, [a.summary], a.id, a.force)

    args = ["--band", ktier, "--kind", a.kind, "--id", a.id, "--summary", a.summary]
    if topic:
        args += ["--topic", topic]
    if a.kind == "fact":
        if a.doc_type:
            args += ["--doc-type", a.doc_type]
        if a.keywords:
            args += ["--keywords", a.keywords]
        if a.status:
            args += ["--status", a.status]
    if a.type:
        args += ["--type", a.type]
    if a.source:
        args += ["--source", a.source]
    if a.gaps:
        args += ["--gaps", a.gaps]
    if a.links:
        args += ["--links", a.links]
    if a.user_confirmed:
        args += ["--user-confirmed"]
    if a.dry_run:
        args += ["--dry-run"]
    run_curator(root, "add", args, body=body)
    if tier in ("private", "sealed") and not a.dry_run:
        print("  sealed 是用户全局红线封存区,内容不出本机;verified 仅你本人经 mem_verify 升。")


if __name__ == "__main__":
    main()
