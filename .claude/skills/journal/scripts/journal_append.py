#!/usr/bin/env python3
"""Desk 主会话请求 recap；外部维护环境兼容直接追加 journal。

Desk 为受管会话注入 DESKTRON_RECAP_REQUEST_PATH。该环境中本脚本不采用调用方写的摘要，
只落请求标记；回合收尾后由隔离 recap 支线读取完整会话并写 journal。没有该变量的迁移、
维护 shell 仍可显式追加，格式单一源继续在 memory_engine.journal。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "memory" / "scripts"))
from _mem_common import domain_for_cwd, work_root, ensure_instance  # noqa: E402
from memory_engine import journal as eng_journal  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="请求当前 Desk 会话通过 recap 写 journal")
    ap.add_argument("--ws", help="工作区 id(默认按 cwd 反查)")
    ap.add_argument("--request", action="store_true", help="请求本回合收尾后立即运行隔离 recap")
    ap.add_argument("--note", help="仅供非 Desk 维护环境直接追加；Desk 会话中忽略正文")
    ap.add_argument("--key", help="若是关键条目,同时给一句话进关键信息索引(指回当天锚点)")
    a = ap.parse_args()

    marker = os.environ.get("DESKTRON_RECAP_REQUEST_PATH", "").strip()
    if marker:
        path = Path(marker)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
        tmp.write_text(json.dumps({"requested_at": time.time()}, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)
        print("recap requested; journal 正文将在本回合收尾后由隔离支线生成")
        return

    if a.request:
        ap.error("当前不在受管 Desk 会话中，无法请求会话 recap")
    if not a.note:
        ap.error("外部维护环境直接追加时必须提供 --note")

    ws = a.ws or domain_for_cwd(os.getcwd()) or "_desktop"
    root = work_root()
    ensure_instance(root)   # journal 可能是新工作层第一笔,引擎命令要认得这个根
    path = eng_journal.append(str(root), ws, a.note, key=a.key)
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M")
    print(f"journal += {ts} {a.note.strip()}" + (f"  (key: {a.key.strip()})" if a.key else "") + f"\n  {path}")


if __name__ == "__main__":
    main()
