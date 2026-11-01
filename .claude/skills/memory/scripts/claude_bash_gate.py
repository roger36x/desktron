#!/usr/bin/env python3
"""claude PreToolUse(Bash)准入闸。claude 的写/读闸只盖 Write/Edit/Read 工具(按 tool_input.file_path),
Bash 直读写(`echo x > knowledge/...md`、`cat 笔记`、`sed -i ... memory/...`)会绕过。本脚本补 Bash:
尽力扫命令里的写/读目标,命中受管路径 → deny。

判定复用 claude 自己的 mem_gate_hook(MEM_BLOCK / kn_blocked)与 kn_read_hook(git_inside_root),
单一判定源;shell 扫描形态与 codex_gate 的 Bash 闸一致。shell 写法无穷,做不到密不透风
(纵深防御一层,跨 git 线另有提交期 gate 兜底)。deny = claude 风格 stdout JSON。"""
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                                                    # memory/scripts
sys.path.insert(0, os.path.join(_HERE, "..", "..", "knowledge", "scripts"))   # knowledge/scripts
from mem_gate_hook import (MEM_BLOCK, JOURNAL_MSG, kn_blocked,
                           session_journal_blocked)   # noqa: E402
try:   # 归档回读闸同源于 memory_engine/gate.py;engine 缺失时降级为不拦
    from memory_engine.gate import (SNAPSHOT_MSG, ARCHIVE_MSG,
                                    snapshot_blocked_target,
                                    archive_bulk_blocked_target)   # noqa: E402
except Exception:
    SNAPSHOT_MSG = ARCHIVE_MSG = ""
    def snapshot_blocked_target(value, cwd=""):
        return False
    def archive_bulk_blocked_target(value, cwd=""):
        return None
import kn_read_hook                               # noqa: E402  读闸:git 内笔记判定

# Bash 写:重定向 > / >>;tee / sed -i / cp / mv / dd of= 的目标(捕获从宽,真假由准入闸判定过滤)
_SH_WRITE = re.compile(
    r"(?:>>?|\btee\b|\bsed\b[^|;&\n]*?-[a-z]*i[a-z]*|\bcp\b|\bmv\b|\bdd\b[^|;&\n]*?\bof=)"
    r"[\s=]*([\"']?)([^\s\"';|&>()]+)\1")
# Bash 读:读类命令的路径参数
_SH_READ = re.compile(r"\b(?:cat|head|tail|less|more|bat|nl|xxd|od)\b\s+((?:-[^\s]+\s+)*)([\"']?)([^\s\"';|&]+)\2")
# 整份读回归档只认真正会吐出全文的命令;head/tail/less 自带截断,不算放大。
_SH_BULK_READ = re.compile(r"\b(?:cat|bat|nl|xxd|od)\b\s+((?:-[^\s]+\s+)*)([\"']?)([^\s\"';|&]+)\2")
# 重定向读入(`cmd < file`)同样把全文带进上下文。
_SH_STDIN = re.compile(r"<\s*([\"']?)([^\s\"';|&<>()]+)\1")
_DIRECT_JOURNAL = re.compile(r"\bcurator\b[^\n;&|]*\bjournal\s+append\b")


def _abs(p, cwd):
    p = os.path.expanduser(p)
    if not os.path.isabs(p) and cwd:
        p = os.path.join(cwd, p)
    return p


def _skip(p):
    n = p.replace("\\", "/")
    return ".claude/skills/" in n


def _deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if (data.get("tool_name") or "") != "Bash":
        sys.exit(0)
    cmd = (data.get("tool_input") or {}).get("command")
    cwd = data.get("cwd") or ""
    if not isinstance(cmd, str) or not cmd:
        sys.exit(0)
    if os.environ.get("DESKTRON_RECAP_REQUEST_PATH") and _DIRECT_JOURNAL.search(cmd):
        _deny(JOURNAL_MSG)

    for m in _SH_WRITE.finditer(cmd):
        raw = m.group(2)
        p = _abs(raw, cwd)
        if _skip(p):
            continue
        if session_journal_blocked(p):
            _deny(JOURNAL_MSG)
        if MEM_BLOCK.search(p):
            _deny(f"准入闸拦截(Bash):命令试图写 memory 受管路径 {raw} —— 必经 mem_add.py(用 curator 过闸),不要 shell 直写。journal 主会话只请求隔离 recap。")
        if kn_blocked(p):
            _deny(f"准入闸拦截(Bash):命令试图写已退役的 knowledge 路径 {raw} —— 新事实改用 mem_add.py --tier fact --kind fact,不要 shell 直写。staging 免闸。")

    # 请求快照:任何读法都拒,包括 head/tail 这类截断读和 `cmd < file` 重定向读入。
    for raw in ([m.group(3) for m in _SH_READ.finditer(cmd)]
                + [m.group(2) for m in _SH_STDIN.finditer(cmd)]):
        if not _skip(_abs(raw, cwd)) and snapshot_blocked_target(raw, cwd):
            _deny(SNAPSHOT_MSG)

    for m in _SH_BULK_READ.finditer(cmd):
        raw = m.group(3)
        if _skip(_abs(raw, cwd)):
            continue
        hit = archive_bulk_blocked_target(raw, cwd)
        if hit:
            _deny(ARCHIVE_MSG.format(path=raw, size=hit[1]))

    for m in _SH_READ.finditer(cmd):
        raw = m.group(3)
        p = _abs(raw, cwd)
        if _skip(p):
            continue
        root = kn_read_hook.git_inside_root(p)
        if root:
            nid = os.path.basename(raw)[:-3]
            _deny(f"准入闸拦截(Bash):裸读 git 内笔记 {raw} —— 走 `curator --root {root} show --id {nid}` "
                  "(计访问 + 摆 confidence/freshness/就近覆盖/冲突)。工作层与缓存目录可直接读。")
    sys.exit(0)


if __name__ == "__main__":
    main()
