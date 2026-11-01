#!/usr/bin/env python3
"""一次性把旧任务账本摘要迁入任务备忘录的正常正文。

旧版任务详情从 task-ledger.json 派生独立 ``auto_summary`` 区块。该区块撤回后，
可用本工具把当时可见的简短摘要写进正常备忘录。迁移不会持续同步账本，也不会覆盖
手写内容；已有“自动整理”段或相同摘要一律跳过。

用法:
    migrate-task-ledger-memos.py <workspace> <data_home>            # 预演
    migrate-task-ledger-memos.py <workspace> <data_home> --apply    # 备份后实改
"""
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import notepad_store  # noqa: E402
import task_ledger  # noqa: E402
import tasks  # noqa: E402


MARKER = "自动整理："


def _sentence(value, limit):
    value = task_ledger._clip(value)
    value = re.split(r"[。！？\n]", value, maxsplit=1)[0].strip(" ;；.")
    return value[:limit].rstrip() + ("…" if len(value) > limit else "")


def legacy_brief(item, row):
    """Reproduce the removed auto_summary wording without restoring it to the API."""
    parts = []
    if row.get("done"):
        parts.append("进展：" + _sentence(row["done"], 65) + "。")
    if item.get("state") != "done":
        field = "blocker" if row.get("blocker") else "next"
        if row.get(field):
            parts.append(("待解除：" if field == "blocker" else "下一步：")
                         + _sentence(row[field], 45) + "。")
    return "".join(parts)[:120]


def _domain_names(workspace):
    out = {}
    path = os.path.join(workspace, ".workbench", "domains.json")
    try:
        import json
        with open(path, encoding="utf-8") as stream:
            rows = json.load(stream)
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("id"):
                out[str(row["id"])] = str(row.get("name") or row["id"])
    except Exception:
        pass
    return out


def scan(workspace, data_home):
    """Return bounded migration rows; reading never creates task ids or notes."""
    ledger = task_ledger.read(data_home).get("tasks", {})
    names = _domain_names(workspace)
    out = []
    domains = os.path.join(workspace, "domains")
    try:
        domain_ids = sorted(os.listdir(domains))
    except OSError:
        return out
    for domain in domain_ids:
        path = tasks.tasks_md_path(domain, workspace)
        if not path or not os.path.isfile(path):
            continue
        for item in tasks.read_tasks(path):
            tid = str(item.get("t") or "")
            row = ledger.get(f"{domain}:{tid}", {}) if tid else {}
            brief = legacy_brief(item, row) if isinstance(row, dict) else ""
            if not brief:
                continue
            note = notepad_store.get_note(workspace, item.get("note"))
            content = str((note or {}).get("content") or "")
            if MARKER in content:
                reason = "已有自动整理"
            elif brief in content:
                reason = "摘要已存在"
            else:
                reason = "追加" if note else "新建"
            out.append({"domain": domain, "folder": names.get(domain, domain),
                        "path": path, "item": item, "brief": brief,
                        "note": note, "reason": reason})
    return out


def _backup(workspace, data_home, rows):
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + str(time.time_ns() % 1_000_000_000).zfill(9)
    root = os.path.join(data_home, "migration-backups", "task-ledger-memos-" + stamp)
    os.makedirs(root, exist_ok=False)
    note_path = os.path.join(workspace, ".workbench", "notepad.json")
    if os.path.isfile(note_path):
        shutil.copy2(note_path, os.path.join(root, "notepad.json"))
    seen = set()
    for row in rows:
        path = row["path"]
        if path in seen:
            continue
        seen.add(path)
        target = os.path.join(root, "domains", row["domain"], tasks.TASKS_MD_NAME)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(path, target)
    return root


def apply_rows(workspace, data_home, rows):
    changes = [row for row in rows if row["reason"] in ("新建", "追加")]
    if not changes:
        return "", 0
    backup = _backup(workspace, data_home, changes)
    written = 0
    for row in changes:
        item = row["item"]
        note = row["note"]
        nid = str((note or {}).get("id") or "")
        if not nid:
            nid = notepad_store.ensure_task_note(workspace, row["folder"], item["title"])
            err = tasks.task_annot_set(row["path"], item["line"], item["title"], "note", nid)
            if err:
                raise RuntimeError(f"{row['domain']}:{item.get('t')} 绑定备忘失败:{err}")
            note = notepad_store.get_note(workspace, nid)
        content = str((note or {}).get("content") or item["title"])
        text = content.rstrip() + "\n\n" + MARKER + row["brief"] + "\n"
        err = notepad_store.set_note_content(workspace, nid, text)
        if err:
            raise RuntimeError(f"{row['domain']}:{item.get('t')} 写备忘失败:{err}")
        written += 1
    return backup, written


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    workspace = os.path.abspath(argv[0])
    data_home = os.path.abspath(argv[1])
    do_apply = "--apply" in argv[2:]
    rows = scan(workspace, data_home)
    counts = {}
    for row in rows:
        counts[row["reason"]] = counts.get(row["reason"], 0) + 1
        print(f"{row['domain']}:{row['item'].get('t')} {row['reason']} {row['item']['title']}")
    print("合计 " + ", ".join(f"{key} {value}" for key, value in sorted(counts.items())))
    if not do_apply:
        print("预演未落盘；发布撤回自动说明的版本后，再加 --apply 执行。")
        return 0
    backup, written = apply_rows(workspace, data_home, rows)
    print(f"已迁移 {written} 条；备份:{backup or '(无改动)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
