"""curator update --id —— 改字段/正文。confidence 不可手设,每次按 source 重新推导。"""

from __future__ import annotations

import sys

import os

from ..config import Instance
from ..confidence import derive
from ..gate import check_body, check_companion, check_enums, check_required
from ..index import rebuild_index
from ..note import write_note
from ..util import CuratorError, today

# update 可改的简单字段。confidence 不在内:三档全由 source 的独立来源数自动推导。
SETTABLE = ["summary", "gaps", "source", "status", "doc_type"]


def _csv(s):
    return [k.strip() for k in s.split(",") if k.strip()]


def _apply_list(cur, adds, rms):
    """先删后加,去重保序。"""
    out = list(cur or [])
    for r in rms or []:
        if r in out:
            out.remove(r)
    for a in adds or []:
        if a not in out:
            out.append(a)
    return out


def run(inst: Instance, args) -> int:
    found = inst.find_by_id(args.id)
    if not found:
        raise CuratorError(f"id 不存在:{args.id}")
    path, fm, body = found

    loc = inst.location_of(path) or {}
    memory_pinned = (inst.ontology.get("name") == "memory-worklayer"
                     and loc.get("kind") == "work" and loc.get("band") == "sot"
                     and fm.get("kind", "judgment") == "judgment")
    if memory_pinned and not args.user_confirmed:
        raise CuratorError("memory pinned judgment 必须由用户明确确认；请加 --user-confirmed")
    if memory_pinned:
        fm["user_confirmed"] = True

    cur_conf = fm.get("confidence")

    changed = []
    for f in SETTABLE:
        v = getattr(args, f, None)
        if v is not None:
            fm[f] = v
            changed.append(f)
    # keywords / links:整量覆盖(--keywords/--links)或增删(--add-*/--rm-*)
    if args.keywords is not None:
        fm["keywords"] = _csv(args.keywords)
        changed.append("keywords")
    elif args.add_keyword or args.rm_keyword:
        fm["keywords"] = _apply_list(fm.get("keywords"), args.add_keyword, args.rm_keyword)
        changed.append("keywords")
    if args.links is not None:
        fm["links"] = _csv(args.links)
        changed.append("links")
    elif args.add_link or args.rm_link:
        fm["links"] = _apply_list(fm.get("links"), args.add_link, args.rm_link)
        changed.append("links")
    if args.tracks is not None:
        fm["tracks"] = _csv(args.tracks)
        changed.append("tracks")
    elif args.add_track or args.rm_track:
        fm["tracks"] = _apply_list(fm.get("tracks"), args.add_track, args.rm_track)
        changed.append("tracks")

    if args.body is not None:
        body = args.body
        changed.append("body")
    elif args.body_stdin:
        body = sys.stdin.read()
        changed.append("body")

    if args.touch:
        fm["last_verified"] = today()
        changed.append("last_verified")

    # confidence 每次按 source 重算。三档都是推导出来的,没有需要保护的人工签名。
    # 旧 verified 遗留的签名字段一并清掉,它们已无判据可对。
    for stale in ("verified_at_hash", "verified_by"):
        if fm.pop(stale, None) is not None:
            changed.append(f"清理 {stale}(verified 已退役)")
    newc = derive(fm, inst.source_patterns)
    if newc != cur_conf:
        fm["confidence"] = newc
        changed.append("confidence")

    # topic 只迁文件、不改字段,所以它要在「无改动」这道判断之前先算出来,否则永远走不到迁移那一步
    new_path = _refile(inst, path, args.id, getattr(args, "topic", None))
    if not changed and new_path == path:
        raise CuratorError("无改动:指定 --summary / --source / --body / --topic 等")

    problems = []
    problems += check_enums(inst, fm)
    problems += check_required(inst, fm)
    problems += check_companion(inst, fm)
    problems += check_body(inst, body, fm.get("tier"))
    if problems:
        raise CuratorError("闸未过:\n  - " + "\n  - ".join(problems))

    if new_path != path:
        changed.append(f"topic→{args.topic or '(层根)'}")

    if args.dry_run:
        print(f"[dry-run] 将更新 {args.id}:{', '.join(changed)}")
        if new_path != path:
            print(f"[dry-run] 迁到 {os.path.relpath(new_path, inst.root)}")
        return 0

    if new_path != path:
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        os.replace(path, new_path)
        _prune_empty(os.path.dirname(path), inst.root)
        n = _retarget_graph(inst, path, new_path)
        if n:
            changed.append(f"图谱 profile_doc {n} 处")
        path = new_path
    write_note(path, fm, body)
    rebuild_index(inst)
    print(f"已更新 {args.id}:{', '.join(changed)}")
    return 0


def _refile(inst: Instance, path: str, note_id: str, topic):
    """算出改归 topic 之后的落点;topic 为 None 表示不迁,返回原路径。

    此前只有 add 能指定 topic,建完就再也挪不动,而 lint 会对平铺在层根的笔记报
    「未归入子目录」——它警告的事情,工具本身没有提供修法(2026-08-21 实测 25 条卡在这里)。
    topic 传空字符串表示迁回层根。
    """
    if topic is None:
        return path
    from .lint import _container_root

    topic = topic.strip().strip("/")
    if topic and (os.path.isabs(topic) or ".." in topic.split("/")):
        raise CuratorError(f"topic 非法:{topic}(须相对子目录,不含 .. 或绝对路径)")
    loc = inst.location_of(path)
    if not loc:
        raise CuratorError(f"定位不到 {note_id} 所在的层,无法改 topic")
    base = _container_root(inst, loc)
    return os.path.join(base, *topic.split("/"), f"{note_id}.md") if topic \
        else os.path.join(base, f"{note_id}.md")


def _retarget_graph(inst: Instance, old_path: str, new_path: str) -> int:
    """迁完文件顺手改图谱里指向它的 `profile_doc`。

    实体用 profile_doc 指向叙事笔记,迁子目录不改它就会悬空。`graph check` 本来就会报
    「profile_doc 悬空」,但那是事后告警;迁的时候顺手改掉,指针不必先断一次。
    """
    old_rel = os.path.relpath(old_path, inst.root)
    new_rel = os.path.relpath(new_path, inst.root)
    n = 0
    for root, _dirs, files in os.walk(inst.root):
        if "entities.yaml" not in files or os.path.basename(root) != "graph":
            continue
        fp = os.path.join(root, "entities.yaml")
        try:
            raw = open(fp, encoding="utf-8").read()
        except OSError:
            continue
        if old_rel not in raw:
            continue
        n += raw.count(old_rel)
        open(fp, "w", encoding="utf-8").write(raw.replace(old_rel, new_rel))
    return n


def _prune_empty(d: str, root: str):
    """迁走之后清掉空目录,但不越过实例根。"""
    while os.path.realpath(d).startswith(os.path.realpath(root) + os.sep):
        try:
            os.rmdir(d)
        except OSError:
            return
        d = os.path.dirname(d)


def add_parser(sub):
    p = sub.add_parser("update", help="改字段/正文")
    p.add_argument("--id", required=True)
    p.add_argument("--summary")
    p.add_argument("--gaps")
    p.add_argument("--source")
    p.add_argument("--status")
    p.add_argument("--doc-type", dest="doc_type")
    p.add_argument("--keywords", help="整量覆盖 keywords(逗号分隔)")
    p.add_argument("--links", help="整量覆盖 links(逗号分隔)")
    p.add_argument("--add-keyword", dest="add_keyword", action="append", help="追加一个 keyword(可重复)")
    p.add_argument("--rm-keyword", dest="rm_keyword", action="append", help="移除一个 keyword(可重复)")
    p.add_argument("--add-link", dest="add_link", action="append", help="追加一个 link(可重复)")
    p.add_argument("--rm-link", dest="rm_link", action="append", help="移除一个 link(可重复)")
    p.add_argument("--tracks", help="整量覆盖 tracks(逗号分隔)")
    p.add_argument("--add-track", dest="add_track", action="append", help="追加一个 track(可重复)")
    p.add_argument("--rm-track", dest="rm_track", action="append", help="移除一个 track(可重复)")
    p.add_argument("--body")
    p.add_argument("--body-stdin", dest="body_stdin", action="store_true", help="正文从 stdin 读")
    p.add_argument("--topic", help="改归的 topic 子目录(迁文件);传空串迁回层根")
    p.add_argument("--touch", action="store_true", help="刷新 last_verified 为今天")
    p.add_argument("--user-confirmed", action="store_true", help="memory pinned judgment:声明用户已明确确认")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="只预演,不写盘")
    p.set_defaults(func=run)
