#!/usr/bin/env python3
"""compact 记忆 s0 层 journal:把超过最近窗口(--keep-days)的老日子正文折叠成一行 stub,保持文件有界。

薄壳:折叠逻辑单一源在 memory-engine(memory_engine.journal.compact)。机械操作,只折不蒸——
蒸馏先于折叠(默认由蒸馏管家自动做),折叠丢全文。
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "memory" / "scripts"))
from _mem_common import die, domain_for_cwd, work_root  # noqa: E402
from memory_engine import journal as eng_journal  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="compact s0 journal 的老日子")
    ap.add_argument("--ws", help="工作区 id(默认按 cwd 反查)")
    ap.add_argument("--keep-days", dest="keep_days", type=int, default=7, help="保留最近几天全文(默认 7)")
    a = ap.parse_args()

    ws = a.ws or domain_for_cwd(os.getcwd()) or "_desktop"
    root = work_root()
    try:
        n = eng_journal.compact(str(root), ws, keep_days=a.keep_days)
    except ValueError as e:
        die(str(e))
    if n is None:
        die(f"工作区 {ws} 无 journal.md")
    print(f"compact:折叠 {n} 个老日子(保留最近 {a.keep_days} 天全文)。{eng_journal.path_of(str(root), ws)}")


if __name__ == "__main__":
    main()
