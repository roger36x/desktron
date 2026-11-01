"""内容笔记的 frontmatter 读写。

字段顺序按基类(SCHEMA.md)固定模板排,保稳定 diff;模板外字段追加在后。
"""

from __future__ import annotations

import re

import yaml

from .util import CuratorError, atomic_write

FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)

# 基类字段顺序(SCHEMA.md)。写出时按此排,其余追加。
FIELD_ORDER = [
    "id",
    "type",
    "sub_kb",
    "tier",
    "kind",
    "user_confirmed",
    "confidence",
    "gaps",
    "source",
    "verified_by",
    "verified_at_hash",
    "last_verified",
    "created",
    "review_every",
    "next_review",
    "last_review",
    "summary",
    "doc_type",
    "status",
    "keywords",
    "links",
    "tracks",
    "distilled",
]


def read_note(path: str) -> tuple[dict, str]:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = FM_RE.match(text)
    if not m:
        raise CuratorError(f"{path}: 缺 frontmatter(--- 包裹)")
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        # 免闸层由机器直写,frontmatter 可能不合法。统一成 CuratorError,
        # 让调用方的 except CuratorError 兜得住,不让 yaml 异常穿透到 CLI 顶层。
        raise CuratorError(f"{path}: frontmatter 解析失败:{e}")
    if not isinstance(fm, dict):
        raise CuratorError(f"{path}: frontmatter 不是映射")
    return fm, m.group(2)


def _ordered(fm: dict) -> dict:
    out = {}
    for k in FIELD_ORDER:
        if k in fm and fm[k] is not None:
            out[k] = fm[k]
    for k, v in fm.items():
        if k not in out and v is not None:
            out[k] = v
    return out


def dump_note(fm: dict, body: str) -> str:
    head = yaml.safe_dump(
        _ordered(fm),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    body = body.rstrip("\n")
    return f"---\n{head}---\n\n{body}\n"


def write_note(path: str, fm: dict, body: str) -> None:
    atomic_write(path, dump_note(fm, body))
