"""curator lint —— 扫成品(子 KB 角色层 + 工作层 private/shared):必备字段、伴随字段、
表达黑名单、freshness、id 唯一与格式、tier/sub_kb 与路径一致、子目录归档、正文字数。

ERROR 阻断(退出码非零),WARN / 提示(INFO)不阻断。temp 免闸,不扫。
scan() 抽出供 gate-staged 复用(git_inside_only 限于 git 内笔记)。
"""

from __future__ import annotations

import os

from ..config import Instance
from ..gate import check_body, check_companion, check_enums, check_required
from ..graphdata import Graph, is_entity_backed
from ..note import read_note
from ..util import CuratorError, months_between, parse_date, today, valid_id


def _container_root(inst: Instance, loc: dict) -> str:
    if loc["kind"] == "git_inside":
        return inst.role_dir(loc["sub_kb"], loc["role"])
    return inst.work_dir(loc["band"], loc.get("topic"))


def scan(inst: Instance, git_inside_only: bool = False) -> list[tuple[str, str, str]]:
    """扫笔记,返回 [(level, rel, msg), ...]。git_inside_only=True 只扫 git 内笔记。"""
    out: list[tuple[str, str, str]] = []
    seen_ids: dict[str, str] = {}
    today_d = parse_date(today())
    fs = inst.file_structure
    soft_chars, warn_chars = fs["soft_chars"], fs["warn_chars"]

    # 实体型笔记须在 graph 有对应实体。仅在实例声明了 entity_backed_doc_types 时启用。
    # 存量欠账用 WARN 兜(棘轮:CI 数这个 WARN,只许降不许升),不阻断单次操作。
    registered_docs = None
    if inst.entity_backed_doc_types:
        g = Graph(inst)
        registered_docs = {e.get("profile_doc") for e in g.entities if e.get("profile_doc")}

    for path in inst.note_paths():
        loc = inst.location_of(path)
        if git_inside_only and not (loc and loc.get("git") == "inside"):
            continue
        rel = os.path.relpath(path, inst.root)
        try:
            fm, body = read_note(path)
        except CuratorError as e:
            out.append(("ERROR", rel, str(e)))
            continue

        # 归档:成品应在子目录(子域 / topic)下,不平铺在角色层 / 工作层根。
        if loc and os.path.relpath(os.path.dirname(path), _container_root(inst, loc)) == ".":
            out.append(("WARN", rel, "未归入子目录(直接躺在层根,易散乱)"))

        # frontmatter 与路径一致(tier / sub_kb)
        if loc:
            exp_tier = loc["role"] if loc["kind"] == "git_inside" else loc["band"]
            if fm.get("tier") and fm.get("tier") != exp_tier:
                out.append(("ERROR", rel, f"tier={fm.get('tier')} 与路径不符(应 {exp_tier})"))
            if loc["kind"] == "git_inside" and fm.get("sub_kb") and fm.get("sub_kb") != loc["sub_kb"]:
                out.append(("ERROR", rel, f"sub_kb={fm.get('sub_kb')} 与路径不符(应 {loc['sub_kb']})"))

        nid = fm.get("id")
        if not nid:
            out.append(("ERROR", rel, "缺 id"))
        else:
            if not valid_id(nid):
                out.append(("ERROR", rel, f"id 非法:{nid}"))
            # 唯一性按层:同层重复是 ERROR;跨层同 id 是就近覆盖(见 curator conflicts),不报。
            lk = loc["sub_kb"] if loc and loc["kind"] == "git_inside" else "work"
            key = (lk, nid)
            if key in seen_ids:
                out.append(("ERROR", rel, f"id 在本层重复:{nid}(另见 {seen_ids[key]})"))
            else:
                seen_ids[key] = rel

        for msg in check_enums(inst, fm):
            out.append(("ERROR", rel, msg))
        for msg in check_required(inst, fm):
            out.append(("ERROR", rel, msg))
        for msg in check_companion(inst, fm):
            out.append(("ERROR", rel, msg))
        for msg in check_body(inst, body, fm.get("tier")):
            out.append(("ERROR", rel, msg))

        # 单笔记字数纪律:小颗粒 facts-only,超长多半是多条揉一起,该拆或蒸馏。
        # 按非空白字符计(行长不定,字数才稳);软档提示、硬档 WARN,均不阻断。
        chars = len("".join((body or "").split()))
        if chars > warn_chars:
            out.append(("WARN", rel, f"正文 {chars} 字 > {warn_chars},应拆分或蒸馏"))
        elif chars > soft_chars:
            out.append(("INFO", rel, f"正文 {chars} 字 > {soft_chars},建议拆分"))

        # 实体型笔记未登记 graph:WARN(棘轮兜存量),不阻断。新增由提交期闸硬挡。
        if registered_docs is not None and is_entity_backed(inst, fm):
            if inst.doc_key(path) not in registered_docs:
                out.append((
                    "WARN", rel,
                    f"实体型笔记({fm.get('doc_type')})未登记 graph:无实体 profile_doc 指向它"
                    "(graph add-entity 补登记)",
                ))

        # 蒸馏产物纪律:地图非疆域,综合物绝不 verified,须 track 覆盖的笔记可回源。
        if fm.get("distilled"):
            if fm.get("confidence") == "corroborated":
                out.append(("ERROR", rel, "蒸馏产物绝不 corroborated(地图非疆域,要真值下钻到叶子笔记)"))
            if not fm.get("tracks"):
                out.append(("WARN", rel, "蒸馏产物应 track 它覆盖的笔记(底层变即重蒸馏)"))

        lv = parse_date(fm.get("last_verified"))
        if fm.get("last_verified") and lv is None:
            out.append(("ERROR", rel, f"last_verified 非日期:{fm.get('last_verified')}"))
        elif lv and today_d and lv > today_d:
            out.append(("ERROR", rel, f"last_verified 晚于今天:{lv}"))

        if fm.get("kind") == "fact":
            if not lv:
                out.append(("WARN", rel, "无 last_verified,无法评估鲜度(经 update --touch 设)"))
            elif today_d:
                age = months_between(lv, today_d)
                th = inst.freshness_threshold(fm.get("doc_type", ""))
                if age > th.get("aging", 12):
                    out.append(("WARN", rel, f"过期:last_verified {age:.0f} 月前 > aging {th.get('aging')}"))
                elif age > th.get("fresh", 6):
                    out.append(("WARN", rel, f"老化:last_verified {age:.0f} 月前 > fresh {th.get('fresh')}"))

    return out


def run(inst: Instance, args) -> int:
    findings = scan(inst)
    errors = sum(1 for lv, _, _ in findings if lv == "ERROR")
    warns = sum(1 for lv, _, _ in findings if lv == "WARN")
    infos = sum(1 for lv, _, _ in findings if lv == "INFO")
    pads = {"ERROR": "ERROR", "WARN": "WARN ", "INFO": "提示 "}
    for level, rel, msg in findings:
        print(f"{pads.get(level, 'WARN ')} {rel}: {msg}")
    summary = f"\n扫 {len(inst.note_paths())} 条笔记:{errors} ERROR,{warns} WARN"
    if infos:
        summary += f",{infos} 提示"
    print(summary)
    return 1 if errors else 0


def add_parser(sub):
    p = sub.add_parser("lint", help="扫必备字段、置信度伴随、freshness、表达黑名单、路径一致")
    p.set_defaults(func=run)
