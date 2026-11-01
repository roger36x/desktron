"""置信度纪律与内容闸:必备字段、伴随字段、表达黑名单。

add / update / lint 共用。校验函数返回问题列表(字符串),空列表即通过。
"""

from __future__ import annotations

import re

from .config import KINDS, Instance


def required_fields(inst: Instance, kind: str) -> list[str]:
    req = list(inst.rules.get("required_fields", []))
    if kind == "fact":
        req += list(inst.rules.get("required_fields_fact", []))
    return req


def check_required(inst: Instance, fm: dict) -> list[str]:
    kind = fm.get("kind")
    out = []
    for f in required_fields(inst, kind):
        if not fm.get(f):
            out.append(f"缺必备字段 {f}")
    return out


def check_companion(inst: Instance, fm: dict) -> list[str]:
    """置信度伴随字段:documented→source,verified→verified_by+verified_at_hash(rules.require_companion)。"""
    companion = inst.rules.get("require_companion", {}) or {}
    conf = fm.get("confidence")
    out = []
    for f in companion.get(conf, []):
        if not fm.get(f):
            out.append(f"confidence={conf} 必附 {f}")
    return out


def check_enums(inst: Instance, fm: dict) -> list[str]:
    out = []
    tier = fm.get("tier")
    if tier and tier not in inst.valid_tiers:
        out.append(f"tier={tier} 不在允许值 {inst.valid_tiers}(角色层或工作层成品 band)")
    sub_kb = fm.get("sub_kb")
    if sub_kb and sub_kb not in inst.sub_kbs:
        out.append(f"sub_kb={sub_kb} 不在实例 sub_kbs {inst.sub_kbs}")
    kind = fm.get("kind")
    if kind and kind not in KINDS:
        out.append(f"kind={kind} 非法(judgment/fact)")
    conf = fm.get("confidence")
    if conf and conf not in inst.confidence_levels:
        out.append(f"confidence={conf} 不在 {inst.confidence_levels}")
    dt = fm.get("doc_type")
    if dt and inst.doc_types and dt not in inst.doc_types:
        out.append(f"doc_type={dt} 不在 ontology.doc_types")
    return out


def forbidden_list(inst: Instance, tier: str = None) -> list:
    """正文黑名单。rules.forbidden_overrides[tier] 若有则替换默认(如 journal 写演化过程,
    放宽推理类用词,只留浮夸类)。"""
    overrides = inst.rules.get("forbidden_overrides", {}) or {}
    if tier and tier in overrides:
        return overrides.get(tier) or []
    return inst.rules.get("forbidden_in_body", []) or []


def _match_forbidden(entry: str, body: str) -> bool:
    if entry.startswith("re:"):
        return bool(re.search(entry[3:], body))
    return entry in body


def check_body(inst: Instance, body: str, tier: str = None) -> list[str]:
    """正文黑名单(字面或 re: 前缀正则),按 tier 取适用清单。"""
    out = []
    for bad in forbidden_list(inst, tier):
        if _match_forbidden(bad, body):
            out.append(f"正文含禁用表达「{bad}」")
    return out
