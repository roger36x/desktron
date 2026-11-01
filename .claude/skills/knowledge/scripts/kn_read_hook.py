#!/usr/bin/env python3
"""Claude PreToolUse 钩子：拦截 Read 裸读 git 内成品笔记，引导走 curator show。

Git 内成品是含 ontology.yaml 的 curator 实例中，位于子知识库角色层且不属于工作层或
缓存的笔记。裸读会绕过访问计数与读侧信任校准，因此必须使用 `curator show --id`。
工作层、缓存、索引、README 和技能代码不受影响。
"""
import json
import os
import sys
from pathlib import Path

# 归档回读闸与记忆写闸同源；engine 缺失时降级为不拦，避免辅助判定阻断正常读取。
_ENGINE_LIB = str(Path(__file__).resolve().parents[4] / "desktron" / "lib")
if _ENGINE_LIB not in sys.path:
    sys.path.insert(0, _ENGINE_LIB)
try:
    from memory_engine.gate import (SNAPSHOT_MSG, ARCHIVE_MSG,
                                    snapshot_blocked_target, archive_bulk_blocked_target)
except Exception:
    SNAPSHOT_MSG = ARCHIVE_MSG = ""

    def snapshot_blocked_target(value, cwd=""):
        return False

    def archive_bulk_blocked_target(value, cwd=""):
        return None


EXEMPT_DIRS = {"cache", "deepwiki", "alert-rules", "coverage", "__pycache__",
               "temp", "staging", "local", ".curator"}
EXEMPT_NAMES = {"INDEX.md", "KB-INDEX.md", "index.md", "README.md", "access-gaps.md"}
ROOT_MARKER = "ontology.yaml"


def _instance_root(path):
    """从笔记路径向上查找最近的 curator 实例根目录。"""
    directory = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.exists(os.path.join(directory, ROOT_MARKER)):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def git_inside_root(path):
    """若路径是 git 内成品笔记，返回实例根；否则返回 None。"""
    if not path.endswith(".md"):
        return None
    normalized = path.replace("\\", "/")
    if "/.claude/skills/" in normalized:
        return None
    root = _instance_root(path)
    if not root:
        return None
    relative = os.path.relpath(os.path.abspath(path), root).replace("\\", "/")
    segments = relative.split("/")
    if segments[-1] in EXEMPT_NAMES:
        return None
    if any(segment in EXEMPT_DIRS for segment in segments):
        return None
    return root


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
        sys.exit(0)
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path") or ""
    if isinstance(path, str) and path:
        cwd = data.get("cwd") or ""
        if snapshot_blocked_target(path, cwd):
            deny(SNAPSHOT_MSG)
            sys.exit(0)
        # 带 offset/limit 的定位读不会放大上下文，只拦截整份回读。
        if not (tool_input.get("offset") or tool_input.get("limit")):
            hit = archive_bulk_blocked_target(path, cwd)
            if hit:
                deny(ARCHIVE_MSG.format(path=path, size=hit[1]))
                sys.exit(0)
        root = git_inside_root(path)
        if root:
            note_id = os.path.basename(path)[:-3]
            deny(f"拒绝裸读 git 内笔记：使用 `curator --root {root} show --id {note_id}` 读取"
                 "（记录访问并展示 confidence、freshness、就近覆盖和冲突）。"
                 "工作层与缓存目录可直接读取。")
    sys.exit(0)


if __name__ == "__main__":
    main()
