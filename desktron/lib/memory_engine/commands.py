"""记忆命令的库函数:add/update/verify/lint 的纪律层(薄封装 curator CLI)。

纪律在此收口:tier 别名解析、id 合法性、emoji 预检、写前查重;三档置信度派生、
置信度推导、表达黑名单、索引维护在 curator 内核。宿主的命令壳(如 Desk 的
mem_add.py)只做 cwd→(root, ws) 解析与 argparse,判定与执行全在这里。

出错以 EngineError 抛出(带用户可读信息),不直接 exit:进程语义归宿主壳。
"""
import re
import sys
from pathlib import Path

from . import layout

TYPES = ("user", "feedback", "project", "reference")
TIERS = ("sealed", "pinned", "fact", "sot", "shared", "private")
# pinned 与 fact 共用旧 sot 物理 band；sot/shared/private 只保留命令兼容。
TIER_ALIAS = {"private": "sealed", "shared": "sot", "pinned": "sot", "fact": "sot"}
# 旧作用域 → 新桶映射(--scope 旧名入口,命令面兼容;新写法用 --tier)
SCOPE_ALIAS = {"area": "sot", "local": "sot", "shared": "sealed", "private": "sealed", "journal": "sealed"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF☀-➿←-⇿⬀-⯿]")


class EngineError(Exception):
    """用户可读的失败(壳层 die 用);exit code 语义:1 一般失败,透传 curator 的非零码。"""

    def __init__(self, msg, rc=1):
        super().__init__(msg)
        self.rc = rc


def check_emoji(body):
    if EMOJI_RE.search(body or ""):
        raise EngineError("正文不合规:\n  - 正文含 emoji / 装饰符号")


def curator_target(root, ws, tier):
    """(root, ws, tier) → curator 的 (band, topic)。sealed 全局无 topic,其余 topic=ws。"""
    band = TIER_ALIAS.get(tier, tier)
    if band not in layout.workspace_layout(root, ws):
        raise EngineError(f"未知桶 {tier}")
    topic = "" if band == "sealed" else ws
    return band, topic


class _Result:
    """兼容 subprocess.CompletedProcess 的返回形状(调用方读 stdout / returncode)。"""

    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def run_curator(root, sub, args, body=None, echo=True):
    """调 curator 内核。

    2026-08-04 并仓后改为同进程函数调用:curator 与本包同在 Desk 的 lib 下,不必再 spawn
    一个 python 解释器,也不必解析子进程的 stderr 与退出码。正文经 stdin 传(curator 的
    --body-stdin 之类仍读 sys.stdin),故临时替换 stdin/stdout 捕获。失败抛 EngineError。"""
    import contextlib
    import io

    from curator.cli import main as curator_main
    from curator.util import CuratorError

    argv = ["--root", str(root), sub] + [str(a) for a in args]
    buf, err = io.StringIO(), io.StringIO()
    old_stdin = sys.stdin
    if body is not None:
        sys.stdin = io.StringIO(body)
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = curator_main(argv)
    except CuratorError as e:
        raise EngineError(str(e), rc=2)
    except SystemExit as e:            # argparse 的用法错误走 SystemExit
        raise EngineError((err.getvalue() or str(e)).strip() or "curator 参数错误", rc=2)
    finally:
        sys.stdin = old_stdin
    out = buf.getvalue()
    if echo and out:
        sys.stdout.write(out)
    if rc:
        raise EngineError((err.getvalue() or out or "curator 失败").strip(), rc=rc)
    return _Result(out, rc)


def _search_ids(root, term, fields="keywords,summary"):
    try:
        r = run_curator(root, "search", [str(term), "--fields", fields, "--paths"], echo=False)
    except EngineError:
        return []
    return [Path(p.strip()).stem for p in (r.stdout or "").splitlines() if p.strip().endswith(".md")]


def dup_check(root, terms, self_id, force=False):
    """写前查重:按 terms 搜,命中已有条目(≠自身)则抛错要求 --force。粗查,假阳由 force 兜。"""
    if force:
        return
    hits = {}
    for t in terms:
        t = (t or "").strip()
        if len(t) < 2:
            continue
        for i in _search_ids(root, t):
            if i and i != self_id:
                hits.setdefault(i, set()).add(t)
    if hits:
        lines = [f"  {i}  (命中:{', '.join(sorted(ts))})" for i, ts in sorted(hits.items())]
        raise EngineError("疑似重复(写前查重):\n" + "\n".join(lines)
                          + "\n→ 命中已有条目就改用 update;确属新条目加 --force 重试。")


def _is_pinned_judgment(tier, kind):
    """旧别名最终落 sot 时，judgment 仍具有 pinned 语义，不能绕过确认。"""
    return TIER_ALIAS.get(tier, tier) == "sot" and kind != "fact"


def _update_is_pinned_judgment(root, note_id):
    from curator.config import Instance

    found = Instance(str(root)).find_by_id(note_id)
    if not found:
        return False
    _path, fm, _body = found
    return fm.get("tier") in {"sot", "shared"} and fm.get("kind", "judgment") != "fact"


def add(root, ws, tier, note_id, summary, body, *, type_=None, source=None,
        gaps=None, links=None, force=False, dry_run=False, confirmed_by_user=False,
        kind="judgment", doc_type=None, keywords=None, status=None, topic=None):
    """新增一条记忆,过 curator add 闸。

    kind 默认 judgment(记「现在该信什么」)。2026-08-03 知识库并入 SOT 之后,
    领域事实用 kind=fact 写进同一层,带 doc_type / keywords / status 三个事实字段;
    topic 显式给时覆盖按工作区推出的默认值(事实按主题归档,不按工作流)。"""
    if tier == "fact" and kind != "fact":
        raise EngineError("--tier fact 必须配 --kind fact")
    if _is_pinned_judgment(tier, kind) and not confirmed_by_user:
        raise EngineError("pinned judgment 必须由用户明确确认；请在确认后传 confirmed_by_user=True")
    if not ID_RE.match(note_id):
        raise EngineError("id 非法(仅小写字母数字与连字符,字母 / 数字开头)")
    if not (body or "").strip():
        raise EngineError("正文为空")
    check_emoji(body)
    band, default_topic = curator_target(root, ws, tier)
    topic = topic or default_topic
    layout.ensure_instance(root)
    dup_check(root, [summary], note_id, force)
    if kind not in ("judgment", "fact"):
        raise EngineError(f"kind 只接受 judgment / fact,得到 {kind}")
    args = ["--band", band, "--kind", kind, "--id", note_id, "--summary", summary]
    if topic:
        args += ["--topic", topic]
    if kind == "fact":
        if doc_type:
            args += ["--doc-type", doc_type]
        if keywords:
            args += ["--keywords", keywords]
        if status:
            args += ["--status", status]
    if type_:
        args += ["--type", type_]
    if source:
        args += ["--source", source]
    if gaps:
        args += ["--gaps", gaps]
    if links:
        args += ["--links", links]
    if confirmed_by_user:
        args += ["--user-confirmed"]
    if dry_run:
        args += ["--dry-run"]
    return run_curator(root, "add", args, body=body)


def update(root, note_id, *, summary=None, source=None, gaps=None, links=None,
           body=None, dry_run=False, confirmed_by_user=False):
    """更新一条记忆；pinned judgment 的修改同样要求用户明确确认。"""
    if body is not None:
        check_emoji(body)
    layout.ensure_instance(root)
    if _update_is_pinned_judgment(root, note_id) and not confirmed_by_user:
        raise EngineError("pinned judgment 必须由用户明确确认；请在确认后传 confirmed_by_user=True")
    args = ["--id", note_id]
    if summary:
        args += ["--summary", summary]
    if source:
        args += ["--source", source]
    if gaps:
        args += ["--gaps", gaps]
    if links is not None:
        args += ["--links", links]
    if body is not None:
        args += ["--body-stdin"]
    if confirmed_by_user:
        args += ["--user-confirmed"]
    if dry_run:
        args += ["--dry-run"]
    return run_curator(root, "update", args, body=body)


def verify(root, note_id, by, dry_run=False):
    """已退役(2026-08-21):顶档由 verified 改为 corroborated,按 source 的独立来源数自动推导。
    转调内核的退役入口,由它给出该改成什么的提示。"""
    layout.ensure_instance(root)
    args = ["--id", note_id, "--by", by]
    if dry_run:
        args += ["--dry-run"]
    return run_curator(root, "verify", args)


def lint(root):
    layout.ensure_instance(root)
    return run_curator(root, "lint", [])
