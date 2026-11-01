"""备忘文件夹随工作流改名迁移(#75 隐患修复):文件夹名 = 工作流名是散备忘蒸馏的匹配键。"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import notepad_store


def _seed(root, notes, folders):
    d = os.path.join(root, ".workbench")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "notepad.json"), "w", encoding="utf-8") as f:
        json.dump({"notes": notes, "folders": folders, "activeId": None}, f, ensure_ascii=False)


def test_rename_moves_notes_and_folder(tmp_path):
    root = str(tmp_path)
    _seed(root, [
        {"id": "a1", "content": "x" * 50, "updated": "2026-07-01T00:00:00Z", "folder": "全球容灾"},
        {"id": "b2", "content": "y" * 50, "updated": "2026-07-01T00:00:00Z", "folder": "其他"},
    ], ["全球容灾", "其他"])
    moved = notepad_store.rename_folder(root, "全球容灾", "全球容灾二期")
    assert moved == 1
    doc = notepad_store.load(root)
    assert "全球容灾" not in doc["folders"]
    assert "全球容灾二期" in doc["folders"]
    by_id = {n["id"]: n for n in doc["notes"]}
    assert by_id["a1"]["folder"] == "全球容灾二期"
    assert by_id["b2"]["folder"] == "其他"


def test_rename_merges_into_existing_folder(tmp_path):
    root = str(tmp_path)
    _seed(root, [
        {"id": "a1", "content": "x" * 50, "updated": "2026-07-01T00:00:00Z", "folder": "旧名"},
        {"id": "b2", "content": "y" * 50, "updated": "2026-07-01T00:00:00Z", "folder": "新名"},
    ], ["旧名", "新名"])
    moved = notepad_store.rename_folder(root, "旧名", "新名")
    assert moved == 1
    doc = notepad_store.load(root)
    assert doc["folders"].count("新名") == 1
    assert "旧名" not in doc["folders"]
    assert all(n["folder"] == "新名" for n in doc["notes"])


def test_rename_noop_cases(tmp_path):
    root = str(tmp_path)
    _seed(root, [{"id": "a1", "content": "x", "updated": "", "folder": "甲"}], ["甲"])
    assert notepad_store.rename_folder(root, "甲", "甲") == 0     # 同名
    assert notepad_store.rename_folder(root, "不存在", "乙") == 0  # 无此文件夹
    assert notepad_store.rename_folder(root, "", "乙") == 0        # 空入参
    doc = notepad_store.load(root)
    assert doc["folders"] == ["甲"]
