"""变更驱动 freshness:笔记声明 `tracks`,被 track 的源前进则陈旧。

变更驱动优先,doc_type 时间半衰期作 fallback。staleness 不改 confidence,只触发复验。

tracks 条目:
- `note:<id>` —— 追踪另一条笔记。curator 自检:被追笔记在本笔记上次复验之后又核实过 → 陈旧。
  (蒸馏地图 track 它覆盖的笔记即用此:底层笔记一变,地图标陈旧、重蒸馏。)
- `repo:<path>@<commit>` / `config:<key>@<ver>` / `doc:<id>@<rev>` —— 外部源。检测靠实例插件:
  `ontology.instance.freshness_plugin` 配了命令则逐条 shell 调(`<cmd> <ref> <anchor-date>`,
  退出码 0 = 源已前进 / 陈旧);未配则该 ref 不可判,落回时间 fallback。

curator 只定字段 + 检测接口 + note 自检 + 时间 fallback,不内置具体平台查询。
"""

from __future__ import annotations

import subprocess

from .util import months_between, parse_date


def parse_track(ref: str) -> tuple[str, str]:
    s = str(ref)
    if ":" in s:
        kind, rest = s.split(":", 1)
        return kind, rest
    return "note", s  # 裸 id 当 note 追踪


def _anchor(fm: dict):
    return (parse_date(fm.get("last_review"))
            or parse_date(fm.get("last_verified"))
            or parse_date(fm.get("created")))


def _note_advanced(tracked_fm: dict, anchor) -> bool:
    adv = parse_date(tracked_fm.get("last_verified")) or parse_date(tracked_fm.get("created"))
    return bool(adv and anchor and adv > anchor)


def _plugin_advanced(plugin, ref: str, anchor) -> bool:
    cmd = plugin.split() if isinstance(plugin, str) else list(plugin)
    try:
        r = subprocess.run(
            cmd + [ref, anchor.isoformat() if anchor else ""],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0
    except Exception:
        return False


def track_eval(inst, fm: dict, notes_by_id: dict, plugin=None) -> tuple[list[str], int]:
    """返回 (陈旧原因列表, 可判 track 数)。可判 = note 追踪,或外部 track 且配了插件。

    可判 track 全不陈旧 → 变更驱动判定为 fresh(跳过时间 fallback)。
    一条都不可判(只有未配插件的外部 track,或无 tracks)→ checkable=0,落回时间。
    """
    tracks = fm.get("tracks") or []
    anchor = _anchor(fm)
    stale: list[str] = []
    checkable = 0
    for ref in tracks:
        kind, target = parse_track(ref)
        if kind == "note":
            checkable += 1
            t = notes_by_id.get(target)
            if t is None:
                stale.append(f"tracks {ref}:被追笔记不存在")
            elif _note_advanced(t, anchor):
                stale.append(f"tracks {ref}:源笔记已前进")
        elif plugin:
            checkable += 1
            if _plugin_advanced(plugin, ref, anchor):
                stale.append(f"tracks {ref}:源已前进(插件)")
    return stale, checkable


def verdict(inst, fm: dict, notes_by_id: dict, plugin, today_d) -> tuple[str, float | None, str]:
    """单条新鲜度判定,读侧(show)与 review-due 共用。返回 (state, age_月, reason)。

    state ∈ {fresh, aging, stale, unknown, na};age 为 None 表示无时间量(变更驱动 / 无锚点)。
    判定顺序同 review-due:变更驱动(tracks 源前进)→ next_review 到期 → kind=fact 半衰期。
    不改 confidence —— 这是与真值轴正交的派生时效信号。
    """
    if fm.get("tracks"):
        stale, checkable = track_eval(inst, fm, notes_by_id, plugin)
        if stale:
            return "stale", None, "变更驱动陈旧:" + "; ".join(stale)
        if checkable:
            return "fresh", None, "变更驱动新"
    nr = parse_date(fm.get("next_review"))
    if nr and today_d and nr <= today_d:
        return "stale", months_between(nr, today_d), "next_review 到期"
    if fm.get("kind") != "fact":
        return "na", None, "无时间评估(judgment)"   # 无时间轴,不催复验
    lv = parse_date(fm.get("last_verified"))
    if not lv:
        return "unknown", None, "无 last_verified"
    age = months_between(lv, today_d) if today_d else 0.0
    th = inst.freshness_threshold(fm.get("doc_type", ""))
    if age > th.get("aging", 12):
        return "stale", age, "过期"
    if age > th.get("fresh", 6):
        return "aging", age, "老化"
    return "fresh", age, ""


def plugin_for(inst):
    return (inst.ontology.get("instance", {}) or {}).get("freshness_plugin")
