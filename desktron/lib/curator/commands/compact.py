"""curator compact-due —— 列老年代成品的 compact / 蒸馏候选:低 authority + 陈旧。

成品不删(老年代不回收)。候选交 agent 去 compact(同主题归并、去近重复)或蒸馏(压密)。
冗余(近重复)由 agent 判,curator 只标"低 authority + 陈旧"这两个可算信号。蒸馏产物的纪律
(distilled: true → reported、带 tracks、绝不 corroborated)由 lint 校验。只读。

judgment 的陈旧判据(2026-08-21 补,用户拍板):事实有 last_verified 过期这条路,判断没有——
kind=judgment 不落 _stale 原有的两条分支,过去恒不陈旧,226 条 notes 里最老的两个月只增不减。
不能套事实那套"过期"语义(判断不会过期),改用"久远且没用上"两条交集:created 超过
JUDGMENT_STALE_MONTHS(3)个月,且 access.json 里从未记过一次 task 读取。两条都要满足才算
候选——单看年龄会把还在用的老判断也圈进去,单看零访问在读取路径刚修好(08-21)时会把
几乎所有条目都圈进来。
"""

from __future__ import annotations

import os

from .. import freshness, state
from ..config import Instance
from ..util import months_between, parse_date, today
from . import authority as authority_mod

# git 内 authority 下限约 0.42(位置先验 1.0),故阈值取 0.55:分开"被引 + 完整"的强 note 与弱 note。
DEFAULTS = {"min_authority": 0.55}
JUDGMENT_STALE_MONTHS = 3


def _cfg(inst: Instance) -> dict:
    c = dict(DEFAULTS)
    c.update(inst.ontology.get("compact", {}) or {})
    return c


def _stale(inst: Instance, fm: dict, by_id: dict, plugin, today_d, access: dict) -> str | None:
    if fm.get("tracks"):
        stale, checkable = freshness.track_eval(inst, fm, by_id, plugin)
        if stale:
            return "变更驱动"
        if checkable:
            return None  # 变更驱动判 fresh
    if fm.get("kind") == "fact":
        lv = parse_date(fm.get("last_verified"))
        th = inst.freshness_threshold(fm.get("doc_type", ""))
        if not lv:
            return "无 last_verified"
        if today_d and months_between(lv, today_d) > th.get("aging", 12):
            return "过期"
    if fm.get("kind") == "judgment":
        created = parse_date(fm.get("created"))
        if created and today_d and months_between(created, today_d) > JUDGMENT_STALE_MONTHS:
            rec = access.get(fm.get("id")) or {}
            if not rec.get("count"):
                return f"久远({JUDGMENT_STALE_MONTHS} 个月以上)且从未取用"
    return None


def run(inst: Instance, args) -> int:
    cfg = _cfg(inst)
    acfg = authority_mod._cfg(inst)
    indeg = authority_mod._indegree(inst)
    today_d = parse_date(today())
    plugin = freshness.plugin_for(inst)
    access = state.read_access(inst)
    notes = list(inst.iter_notes())
    by_id = {fm.get("id"): fm for _, fm, _ in notes if fm.get("id")}

    young = set(inst.ontology.get("compact", {}).get("young_bands") or ["journal"])
    cands = []
    for path, fm, _ in notes:
        loc = inst.location_of(path)
        if not loc:
            continue
        # 老年代按层定义,不看 git。原判据是「在 git 跟踪的子 KB 里才算成品」,那套建立在
        # 「本地是草稿、贡献进共享库才是成品」的假设上。git 贡献线退场后 sub_kbs 为空,
        # 所有笔记都是「git 外」,这条命令恒定返回空——它该处理的久远记忆一条都看不到。
        # 现在除 journal 外的成品层都算老年代。
        if loc.get("band") in young:
            continue
        score = authority_mod._score(inst, fm, indeg.get(fm.get("id"), 0), acfg)["score"]
        st = _stale(inst, fm, by_id, plugin, today_d, access)
        if score < cfg["min_authority"] and st:
            cands.append((score, st, path, fm))

    if not cands:
        print("(无 compact / 蒸馏候选)")
        return 0
    cands.sort(key=lambda e: e[0])
    print(f"compact / 蒸馏候选(authority < {cfg['min_authority']} 且陈旧;冗余由你判):")
    for score, st, path, fm in cands:
        print(f"  authority {score}  陈旧:{st}  {fm.get('id')}  {os.path.relpath(path, inst.root)}")
    print("compact = 同主题归并去重;蒸馏 = 压密(产物 distilled: true、reported、带 tracks、绝不 corroborated)。")
    return 0


def add_parser(sub):
    p = sub.add_parser("compact-due", help="列老年代 compact / 蒸馏候选(只读)")
    p.set_defaults(func=run)
