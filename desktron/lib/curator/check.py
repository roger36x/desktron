"""蒸馏准入闸:蒸馏产物写盘前,核它 tracks 指向的源是否兑现(可核断言)。

地图(distilled)须落在真实疆域上。它 track 的叶子若不存在、外部源若解析不出,
这是出生缺陷:拒收 —— 不写进成品,改追加到 temp 的 rejected 缓冲(带拒因),
供下一轮蒸馏回避,而非静默写一条悬空的地图。

可核判据(借 freshness 的 tracks 词汇,但只判"现在是否兑现",不判"是否前进"):
- `note:<id>` —— 引擎自检:被 track 的笔记须存在;不存在 → 拒。
- `repo:`/`config:`/`doc:` 外部源 —— 配了 instance.check_plugin 才可核:
  `<cmd> <ref>` 退出码 0 = 源已解析 / 兑现,非 0 = 拒(命令输出作拒因)。未配 → 该 ref 不可核,跳过。

不可核(无 tracks,或只有未配插件的外部 track)→ 不拦,维持现状
(仍按证据写为 reported / documented,蒸馏产物绝不 verified)。
"""

from __future__ import annotations

import os
import subprocess

from .freshness import parse_track
from .note import dump_note
from .util import today


def check_plugin_for(inst):
    return (inst.ontology.get("instance", {}) or {}).get("check_plugin")


def _plugin_resolves(plugin, ref: str) -> tuple[bool, str]:
    cmd = plugin.split() if isinstance(plugin, str) else list(plugin)
    try:
        r = subprocess.run(cmd + [ref], capture_output=True, text=True, timeout=30)
    except Exception as e:  # noqa: BLE001 —— 插件不可用即不可核,记为拒因
        return False, f"check_plugin 调用失败:{e}"
    if r.returncode == 0:
        return True, ""
    return False, (r.stdout or r.stderr or "").strip() or f"退出码 {r.returncode}"


def gate_distilled(inst, fm: dict, notes_by_id: dict, plugin=None) -> list[str]:
    """蒸馏产物准入:返回拒因列表(空 = 过闸)。只核可判的 tracks;非蒸馏直接放行。"""
    if not fm.get("distilled"):
        return []
    reasons: list[str] = []
    for ref in fm.get("tracks") or []:
        kind, target = parse_track(ref)
        if kind == "note":
            if target not in notes_by_id:
                reasons.append(f"tracks {ref}:被追笔记不存在(地图悬在空疆域)")
        elif plugin:
            ok, why = _plugin_resolves(plugin, ref)
            if not ok:
                reasons.append(f"tracks {ref}:源未兑现({why})")
        # 外部 ref 无插件 → 不可核,跳过(维持现状)
    return reasons


def append_rejected(inst, fm: dict, body: str, reasons: list[str], dest: str) -> str:
    """被拒蒸馏追加进免闸缓冲 rejected.md(免闸 append、gitignored),带拒因 + 原文。返回该文件路径。
    免闸 band 是 topical({topic}/s0 这类,无单目录)时,缓冲落实例状态区 .curator/rejected.md。"""
    if inst.band_is_topical(inst.temp_band):
        from .state import resolve_dir
        path = os.path.join(resolve_dir(inst.root), "rejected.md")
    else:
        path = os.path.join(inst.temp_dir, "rejected.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [
        f"## {today()} 拒收蒸馏:{fm.get('id')}",
        f"- 落点:{dest}",
        "- 拒因:",
    ]
    lines += [f"  - {r}" for r in reasons]
    lines += ["", "```", dump_note(fm, body).rstrip(), "```", "", ""]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path
