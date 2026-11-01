"""提交期准入闸。

gate-staged:读暂存区,挡三件事——
  1. git 外 band(temp/private/shared)内容被暂存(防 `git add -f` 绕过 gitignore,永不上远端);
  2. 进 git 内 band 的暂存笔记带 lint ERROR(脏内容不进版本控制);
  3. 暂存的实体型笔记(doc_type ∈ entity_backed_doc_types)在 graph 无对应实体(强制建 graph)。
install-hooks:写 .git/hooks/pre-commit,提交时自动跑 gate-staged。

一个实例 = 一个 git 仓,实例根即仓根。
"""

from __future__ import annotations

import os
import subprocess

from ..config import Instance
from ..graphdata import Graph, is_entity_backed
from ..note import read_note
from ..util import CuratorError
from .lint import scan

HOOK = """#!/bin/sh
# curator 提交期准入闸:挡 git 外 band(temp/private/shared)内容进提交,并校验进 git 内的笔记过 lint。
# 由 `curator install-hooks` 写出。curator 不在 PATH 时跳过(准入闸是纵深防御,非唯一防线)。
command -v curator >/dev/null 2>&1 || { echo "curator 不在 PATH,跳过准入闸"; exit 0; }
exec curator --root "$(git rev-parse --show-toplevel)" gate-staged
"""


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True
    )


def _repo_root(inst: Instance) -> str:
    r = _git(inst.root, "rev-parse", "--show-toplevel")
    if r.returncode != 0:
        raise CuratorError("不在 git 仓库内,提交期准入闸无从校验")
    return r.stdout.strip()


def gate_staged(inst: Instance, args) -> int:
    repo_root = _repo_root(inst)
    r = _git(inst.root, "diff", "--cached", "--name-only")
    if r.returncode != 0:
        raise CuratorError("读暂存区失败:" + r.stderr.strip())
    staged = [line for line in r.stdout.splitlines() if line.strip()]
    # 用 realpath 统一(macOS /var → /private/var 等符号链接,git toplevel 已解析)。
    staged_abs = {os.path.realpath(os.path.join(repo_root, rel)) for rel in staged}

    # 1. git 外 band 泄漏。topical band({topic} 占位)按实际目录展开;普通 band 用声明目录
    # (glob 只回已存在的,普通 band 目录可能还没建,保留字面路径兜底)。
    blocked = []
    for band in inst.work_layer:
        for d in inst.work_scan_dirs(band):
            blocked.append(os.path.realpath(d))
        if not inst.band_is_topical(band):
            blocked.append(os.path.realpath(inst.work_dir(band)))
    blocked = sorted(set(blocked))
    offenders = []
    for rel in staged:
        # README.md 是桶目录占位说明,是 gitignore 的例外项,允许进 git。
        if os.path.basename(rel) == "README.md":
            continue
        ap = os.path.realpath(os.path.join(repo_root, rel))
        if any(ap == b or ap.startswith(b + os.sep) for b in blocked):
            offenders.append(rel)

    rc = 0
    if offenders:
        rc = 3
        print("拒绝提交:git 外 band(temp/private/shared)内容被暂存,永不上远端:")
        for o in offenders:
            print(f"  {o}")
        print("撤出暂存:git restore --staged <file>。内容若要进 git,先 promote 过清洗闸到 git 内桶。")

    # 2. 进 git 内子 KB 的暂存笔记须过 lint(只看本次暂存的文件)
    errors = [
        (rel, msg)
        for level, rel, msg in scan(inst, git_inside_only=True)
        if level == "ERROR"
        and os.path.realpath(os.path.join(inst.root, rel)) in staged_abs
    ]
    if errors:
        rc = 3
        print("拒绝提交:进 git 内 band 的暂存笔记有 lint ERROR:")
        for rel, msg in errors:
            print(f"  {rel}: {msg}")

    # 3. 暂存的实体型笔记须在 graph 有对应实体(仅当实例声明 entity_backed_doc_types)。
    #    读 graph 当前状态:实体若在同一 commit 里一起加好,核对即过。
    unbacked = _staged_unbacked_entity_notes(inst, staged, repo_root)
    if unbacked:
        rc = 3
        print("拒绝提交:暂存的实体型笔记在 graph 无对应实体(须先登记):")
        for rel in unbacked:
            print(f"  {rel}")
        print("补登记:curator graph add-entity --id <id> --type <type> "
              "--set profile_doc=<该笔记的 doc 键>,与笔记同一次提交。")

    if rc == 0:
        print("gate-staged 通过:暂存区无 git 外 band 内容,git 内暂存笔记 lint 干净,实体型笔记已登记 graph")
    return rc


def _staged_unbacked_entity_notes(inst: Instance, staged, repo_root: str) -> list[str]:
    """暂存文件里,实体型但 graph 无实体指向的笔记 rel 列表。entity_backed_doc_types 空则空。"""
    if not inst.entity_backed_doc_types:
        return []
    g = Graph(inst)
    registered = {e.get("profile_doc") for e in g.entities if e.get("profile_doc")}
    out = []
    for rel in staged:
        if os.path.basename(rel) in ("README.md", "index.md", "INDEX.md"):
            continue
        if not rel.endswith(".md"):
            continue
        # 用 inst.root 拼路径(非 git 的 realpath 根):location_of / doc_key 按 inst.root
        # 比对,macOS 上 /var 与 /private/var 不混淆。staged rel 相对仓根 = inst.root 同目录。
        ap = os.path.join(inst.root, rel)
        if not (os.path.exists(ap) and inst.is_git_inside_path(ap)):
            continue
        try:
            fm, _ = read_note(ap)
        except CuratorError:
            continue
        if is_entity_backed(inst, fm) and inst.doc_key(ap) not in registered:
            out.append(rel)
    return out


def install_hooks(inst: Instance, args) -> int:
    gd = _git(inst.root, "rev-parse", "--git-dir")
    if gd.returncode != 0:
        raise CuratorError("不在 git 仓库内,无法装 hook;先 git init")
    git_dir = gd.stdout.strip()
    if not os.path.isabs(git_dir):
        git_dir = os.path.join(inst.root, git_dir)
    hooks_dir = os.path.join(git_dir, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    hook_path = os.path.join(hooks_dir, "pre-commit")

    if os.path.exists(hook_path) and not args.force:
        with open(hook_path, encoding="utf-8") as f:
            existing = f.read()
        if "curator" not in existing:
            print(f"已存在非 curator 的 pre-commit:{hook_path}")
            print("加 --force 覆盖,或手动把 gate-staged 接进去。")
            return 3

    with open(hook_path, "w", encoding="utf-8") as f:
        f.write(HOOK)
    os.chmod(hook_path, 0o755)
    print(f"已装 pre-commit 准入闸:{os.path.relpath(hook_path, inst.root)}")
    return 0


def add_parser(sub):
    gp = sub.add_parser("gate-staged", help="提交期准入闸:校验暂存区(供 pre-commit 调)")
    gp.set_defaults(func=gate_staged)

    ip = sub.add_parser("install-hooks", help="装 pre-commit 准入闸 hook")
    ip.add_argument("--force", action="store_true", help="覆盖已存在的 pre-commit")
    ip.set_defaults(func=install_hooks)
