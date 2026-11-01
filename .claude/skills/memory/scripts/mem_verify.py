#!/usr/bin/env python3
"""已退役(2026-08-21)。

原来它把一条记忆升到 verified:用户本人核实、签名、绑正文 hash。线上跑到 405 条记忆时
签名数为 0——这道工序没人做,顶档等于不存在。顶档已改为 corroborated,判据是 source 里
引用了几个独立来源,随 mem_add / mem_update 自动推导,没有人工入口。

要升顶档,补第二个独立来源:
    python .claude/skills/memory/scripts/mem_update.py --id <id> --source "<原来源>;<另一个独立来源>"

本文件留着只为报清楚这件事:旧 skill 文本与历史会话里还写着 mem_verify,直接删掉会得到
一句 FileNotFoundError,读不出该改成什么。
"""
import sys

MSG = __doc__.strip()

if __name__ == "__main__":
    print("✗ mem_verify 已退役。\n\n" + MSG, file=sys.stderr)
    sys.exit(2)
