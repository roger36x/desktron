#!/usr/bin/env python3
"""codex PreToolUse 准入闸。解析 hook payload,拦截对 memory/knowledge 受管路径的
裸读裸写,强制走 curator 命令(计访问 + 闸控)。

覆盖的工具:
- apply_patch(tool_input.command=patch 文本):抽 `*** Add/Update/Delete File: <路径>` → 写闸。
- Bash(tool_input.command=shell):扫写形态(> / >> / tee / sed -i / cp / mv)与读形态
  (cat/head/tail/less/more/bat/nl),目标落在受管路径 → deny。shell 写法无穷,做不到密不透风;
  真要跨 git 线另有提交期 gate 兜底。

deny = 退出码 2 + stderr 文案(引擎显示成 "Command blocked by PreToolUse hook: ...")。
"""
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

# apply_patch 目标:`*** Add File: <p>` / `Update File:` / `Delete File:`
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+?)\s*$", re.M)
# Bash 写:重定向 > / >>;tee / sed -i / cp / mv / dd of= 的目标(尽力,捕获从宽,真假由准入闸判定过滤)
_SH_WRITE = re.compile(
    r"(?:>>?|\btee\b|\bsed\b[^|;&\n]*?-[a-z]*i[a-z]*|\bcp\b|\bmv\b|\bdd\b[^|;&\n]*?\bof=)"
    r"[\s=]*([\"']?)([^\s\"';|&>()]+)\1")
# Bash 读:读类命令的路径参数(尽力)
_SH_READ = re.compile(r"\b(?:cat|head|tail|less|more|bat|nl|xxd|od)\b\s+((?:-[^\s]+\s+)*)([\"']?)([^\s\"';|&]+)\2")

try:
    from memory_engine.gate import (JOURNAL_MSG, MEM_BLOCK,
                                    session_journal_blocked)   # 单一源
except Exception:
    MEM_BLOCK = re.compile(
        r"(^|/)\.memory/(_sealed|[\w.-]+/(sot|shared)|local/(private|shared))(/|$)"
        r"|(^|/)memory/(private|shared)(/|$)")   # 新布局 _sealed/<ws>/shared + 旧布局兜底;s0/s1 免闸
    _JOURNAL_BLOCK = re.compile(r"(^|/)\.memory/[^/]+/(journal|s0)(/|$)")
    JOURNAL_MSG = ("拒绝:受管 Desk 主会话不能直接写 journal。"
                   "请运行 journal_append.py --request，由隔离 recap 支线生成正文。")
    def session_journal_blocked(path):
        return bool(os.environ.get("DESKTRON_RECAP_REQUEST_PATH")
                    and _JOURNAL_BLOCK.search(path or ""))
KN_CACHE = {"cache", "deepwiki", "alert-rules", "coverage", "__pycache__", "temp", "staging"}   # temp/staging 免闸(与 mem_gate_hook.KN_CACHE 对齐;codex 版此前漏 temp)
KN_EXEMPT = {"INDEX.md", "KB-INDEX.md", "index.md", "README.md", "access-gaps.md"}
KN_RE = re.compile(r"(?:^|/)knowledge/(.+)$")
READ_EXEMPT_DIRS = KN_CACHE | {"local", ".curator"}
ROOT_MARKER = "ontology.yaml"
try:   # 归档回读闸同源于 memory_engine/gate.py;engine 缺失时降级为不拦
    from memory_engine.gate import (SNAPSHOT_MSG, ARCHIVE_MSG,
                                    snapshot_blocked_target, archive_bulk_blocked_target)
except Exception:
    SNAPSHOT_MSG = ARCHIVE_MSG = ""
    def snapshot_blocked_target(value, cwd=""):
        return False
    def archive_bulk_blocked_target(value, cwd=""):
        return None

# 整份读回归档只认真正会吐出全文的命令;head/tail/less 自带截断,不算放大。
_SH_BULK_READ = re.compile(r"\b(?:cat|bat|nl|xxd|od)\b\s+((?:-[^\s]+\s+)*)([\"']?)([^\s\"';|&]+)\2")
# 重定向读入(`cmd < file`)同样把全文带进上下文。
_SH_STDIN = re.compile(r"<\s*([\"']?)([^\s\"';|&<>()]+)\1")


def _check_archive_read(value, cwd, bulk=True, deny=None):
    """读操作数的归档闸:快照硬拒;bulk=True 时另拦整份读回超阈值归档。

    deny 可替换,好让 cursor 用自己的 JSON 输出格式复用同一份判定。
    """
    deny = deny or _deny
    if not value or _skip(_abs(value, cwd)):
        return
    if snapshot_blocked_target(value, cwd):
        deny(SNAPSHOT_MSG)
    if bulk:
        hit = archive_bulk_blocked_target(value, cwd)
        if hit:
            deny(ARCHIVE_MSG.format(path=value, size=hit[1]))


_DIRECT_JOURNAL = re.compile(r"\bcurator\b[^\n;&|]*\bjournal\s+append\b")


def _abs(p, cwd):
    p = os.path.expanduser(p)
    if not os.path.isabs(p) and cwd:
        p = os.path.join(cwd, p)
    return p


def _skip(p):
    norm = p.replace("\\", "/")
    return ".claude/skills/" in norm


def _kn_blocked(path):
    norm = path.replace("\\", "/")
    if "/imports/" in norm:
        return False
    m = KN_RE.search(norm)
    if not m:
        return False
    parts = m.group(1).split("/")
    if not parts[-1].endswith(".md") or parts[-1] in KN_EXEMPT:
        return False
    return not any(p in KN_CACHE or p.startswith(".") for p in parts[:-1])


def _instance_root(path):
    d = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.exists(os.path.join(d, ROOT_MARKER)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _git_inside_root(path):
    if not path.endswith(".md") or _skip(path):
        return None
    root = _instance_root(path)
    if not root:
        return None
    rel = os.path.relpath(os.path.abspath(path), root).replace("\\", "/")
    segs = rel.split("/")
    if segs[-1] in KN_EXEMPT:
        return None
    if any(s in READ_EXEMPT_DIRS for s in segs):
        return None
    return root


def _blocked_write(path):
    """落在 memory/knowledge 写闸内则返回 'memory'/'knowledge',否则 None。"""
    if _skip(path):
        return None
    if MEM_BLOCK.search(path):
        return "memory"
    if _kn_blocked(path):
        return "knowledge"
    return None


def _deny(msg):
    sys.stderr.write(msg + "\n")
    sys.exit(2)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    tn = data.get("tool_name") or ""
    ti = data.get("tool_input") or {}
    cwd = data.get("cwd") or ""

    cmd = ti.get("command")
    if not isinstance(cmd, str) or not cmd:
        sys.exit(0)

    if tn == "apply_patch":
        for m in _PATCH_FILE.finditer(cmd):
            p = m.group(1).strip().strip("\"'")
            if session_journal_blocked(_abs(p, cwd)):
                _deny(JOURNAL_MSG)
            where = _blocked_write(_abs(p, cwd))
            if where:
                _deny(f"准入闸拦截:apply_patch 改 {where} 受管路径 {p} —— 必经 "
                      f"{'mem_add.py' if where == 'memory' else 'mem_add.py --tier fact --kind fact'} 命令(底层调 curator 过闸),"
                      "不要直接改文件。journal 主会话只请求隔离 recap，knowledge/staging 免闸。")
        sys.exit(0)

    if tn == "Bash":
        if os.environ.get("DESKTRON_RECAP_REQUEST_PATH") and _DIRECT_JOURNAL.search(cmd):
            _deny(JOURNAL_MSG)
        for m in _SH_WRITE.finditer(cmd):
            p = m.group(2)
            if session_journal_blocked(_abs(p, cwd)):
                _deny(JOURNAL_MSG)
            where = _blocked_write(_abs(p, cwd))
            if where:
                _deny(f"准入闸拦截:命令试图写 {where} 受管路径 {p} —— 必经 "
                      f"{'mem_add.py' if where == 'memory' else 'mem_add.py --tier fact --kind fact'} 命令(用 curator 过闸),不要 shell 直写。temp 免闸。")
        # 请求快照:任何读法都拒,包括 head/tail 截断读与 `cmd < file` 重定向读入。
        for raw in ([m.group(3) for m in _SH_READ.finditer(cmd)]
                    + [m.group(2) for m in _SH_STDIN.finditer(cmd)]):
            _check_archive_read(raw, cwd, bulk=False)
        for m in _SH_BULK_READ.finditer(cmd):
            _check_archive_read(m.group(3), cwd)
        for m in _SH_READ.finditer(cmd):
            p = m.group(3)
            if _skip(_abs(p, cwd)):
                continue
            root = _git_inside_root(_abs(p, cwd))
            if root:
                nid = os.path.basename(p)[:-3]
                _deny(f"准入闸拦截:裸读 git 内笔记 {p} —— 走 `curator --root {root} show --id {nid}` "
                      "(计访问 + 摆 confidence/freshness/就近覆盖/冲突)。工作层与缓存目录可直接读。")
    sys.exit(0)


if __name__ == "__main__":
    main()
