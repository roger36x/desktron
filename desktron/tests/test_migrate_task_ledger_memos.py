import importlib.util
import json
import os
from pathlib import Path

import notepad_store
import task_ledger
import tasks


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "migrate-task-ledger-memos.py"
spec = importlib.util.spec_from_file_location("migrate_task_ledger_memos", SCRIPT)
migrate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrate)


def _repo(tmp_path):
    root = tmp_path / "workspace"
    data = tmp_path / "data"
    (root / "domains" / "alpha").mkdir(parents=True)
    (root / ".workbench").mkdir()
    (root / ".workbench" / "domains.json").write_text(
        json.dumps([{"id": "alpha", "name": "甲工作流"}], ensure_ascii=False), encoding="utf-8")
    item, _, err = task_ledger.apply(str(data), str(root), "alpha",
        {"intent": "create", "title": "交付功能", "done": "代码与测试均完成", "next": "等待发布"},
        {"id": "s1"}, "create-1")
    assert not err
    return root, data, item


def _item(root, tid):
    return next(x for x in tasks.read_tasks(tasks.tasks_md_path("alpha", str(root))) if x["t"] == tid)


def test_scan_is_read_only_and_skips_existing_automatic_memo(tmp_path):
    root, data, item = _repo(tmp_path)
    before_tasks = (root / "domains" / "alpha" / "TASKS.md").read_bytes()
    assert migrate.scan(str(root), str(data))[0]["reason"] == "新建"
    assert not (root / ".workbench" / "notepad.json").exists()
    assert (root / "domains" / "alpha" / "TASKS.md").read_bytes() == before_tasks

    nid = notepad_store.ensure_task_note(str(root), "甲工作流", item["title"])
    assert not tasks.task_annot_set(tasks.tasks_md_path("alpha", str(root)), item["line"], item["title"], "note", nid)
    assert not notepad_store.set_note_content(str(root), nid, item["title"] + "\n\n自动整理：已有人工核对摘要。\n")
    row = migrate.scan(str(root), str(data))[0]
    assert row["reason"] == "已有自动整理"
    backup, written = migrate.apply_rows(str(root), str(data), [row])
    assert backup == "" and written == 0
    assert "已有人工核对摘要" in notepad_store.get_note(str(root), nid)["content"]


def test_apply_creates_or_appends_without_overwriting_and_is_idempotent(tmp_path):
    root, data, item = _repo(tmp_path)
    rows = migrate.scan(str(root), str(data))
    backup, written = migrate.apply_rows(str(root), str(data), rows)
    assert written == 1 and Path(backup, "domains", "alpha", "TASKS.md").is_file()
    current = _item(root, item["t"])
    note = notepad_store.get_note(str(root), current["note"])
    assert note["folder"] == "甲工作流"
    assert note["content"].startswith("交付功能\n\n做什么:")
    assert "自动整理：进展：代码与测试均完成。下一步：等待发布。" in note["content"]

    again = migrate.scan(str(root), str(data))[0]
    assert again["reason"] == "已有自动整理"
    _, written = migrate.apply_rows(str(root), str(data), [again])
    assert written == 0

    second, _, err = task_ledger.apply(str(data), str(root), "alpha",
        {"intent": "create", "title": "保留手写", "done": "修复完成"}, {"id": "s2"}, "create-2")
    assert not err
    nid = notepad_store.ensure_task_note(str(root), "甲工作流", second["title"])
    assert not tasks.task_annot_set(tasks.tasks_md_path("alpha", str(root)), second["line"], second["title"], "note", nid)
    assert not notepad_store.set_note_content(str(root), nid, "保留手写\n\n这是用户手写内容。\n")
    row = next(x for x in migrate.scan(str(root), str(data)) if x["item"]["t"] == second["t"])
    assert row["reason"] == "追加"
    migrate.apply_rows(str(root), str(data), [row])
    content = notepad_store.get_note(str(root), nid)["content"]
    assert "这是用户手写内容。" in content
    assert content.rstrip().endswith("自动整理：进展：修复完成。")
