#!/usr/bin/env python3
"""claude PreToolUse 钩子:拦 Write/Edit 直接写受管记忆与存量 knowledge/ 手写笔记,
引导走 memory 命令(命令底层调 curator 过闸)。这是纵深防御而非最终防线。
temp 免闸不拦;缓存(deepwiki/ 等)不拦;命令经 Bash 跑,
不走 Write/Edit,不受影响。"""
import json
import os
import re
import sys
from pathlib import Path

# memory_engine 钉在 Desk 自带那份(desktron/lib),不吃开发机上另装的 pip 版本——
# 两份曾经独立漂移过,细节见 _mem_common.py 同一处注释。
_ENGINE_LIB = str(Path(__file__).resolve().parents[4] / "desktron" / "lib")
if _ENGINE_LIB not in sys.path:
    sys.path.insert(0, _ENGINE_LIB)

# 记忆写闸判定单一源在 memory-engine(gate.py);engine 未装时用本地兜底(内容以 engine 为准)。
try:
    from memory_engine.gate import (MEM_BLOCK, MEM_MSG, JOURNAL_MSG,
                                    session_journal_blocked)
except Exception:
    MEM_BLOCK = re.compile(
        r"(^|/)\.memory/(_sealed|[\w.-]+/(sot|shared)|local/(private|shared))(/|$)"
        r"|(^|/)memory/(private|shared)(/|$)")
    MEM_MSG = ("拒绝:_sealed 与 <ws>/sot 是记忆准入闸管控目录,不能直接写/编辑。"
               "请走 memory skill 的 mem_add/mem_update；pinned 新增和更新须用户确认，journal 走 recap。")
    _JOURNAL_BLOCK = re.compile(r"(^|/)\.memory/[^/]+/(journal|s0)(/|$)")
    JOURNAL_MSG = ("拒绝:受管 Desk 主会话不能直接写 journal。"
                   "请运行 journal_append.py --request，由隔离 recap 支线生成正文。")
    def session_journal_blocked(path):
        return bool(os.environ.get("DESKTRON_RECAP_REQUEST_PATH")
                    and _JOURNAL_BLOCK.search(path or ""))

# 存量 knowledge/:缓存目录与索引/文档文件豁免,其余 .md 禁止继续直接写。
# 与 .claude/skills/knowledge/scripts/_kn_common.py 的边界保持一致。
KN_CACHE = {"cache", "deepwiki", "alert-rules", "coverage", "__pycache__", "temp", "staging"}
KN_EXEMPT = {"INDEX.md", "KB-INDEX.md", "index.md", "README.md", "access-gaps.md"}
KN_RE = re.compile(r"(?:^|/)knowledge/(.+)$")
KN_MSG = (
    "拒绝:knowledge/ 是已退役的存量目录,不能继续写/编辑。"
    "新增事实请走 memory skill 的 mem_add.py --tier fact --kind fact；更新用 mem_update.py。"
    "置信度由 source 自动派生。缓存目录(cache/ 等)仍可直接写。"
)


def kn_blocked(path):
    if "/imports/" in path:   # 外部知识库挂在 imports/,有自己的 kb 命令闸,不归这道
        return False
    m = KN_RE.search(path)
    if not m:
        return False
    parts = m.group(1).split("/")
    if not parts[-1].endswith(".md") or parts[-1] in KN_EXEMPT:
        return False
    return not any(p in KN_CACHE or p.startswith(".") for p in parts[:-1])  # 缓存目录任意层级豁免


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)   # 读不到输入就放行,不阻断正常流程
    path = (data.get("tool_input") or {}).get("file_path") or ""
    if isinstance(path, str) and path:
        if ".claude/skills/" in path:
            sys.exit(0)   # skill / 引擎代码不是 KB 数据,闸只护数据目录,不护代码(含本 skill 的 SKILL.md/scripts)
        if session_journal_blocked(path):
            deny(JOURNAL_MSG)
        elif MEM_BLOCK.search(path):
            deny(MEM_MSG)
        elif kn_blocked(path):
            deny(KN_MSG)
    sys.exit(0)


if __name__ == "__main__":
    main()
